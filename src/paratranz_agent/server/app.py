"""FastAPI 应用：WebUI 的后端。只负责组装，不含业务分支。

分层：`create_app` 建好单例 → 装进 `Deps` → 交给 `routes/` 各模块挂路由。
路由模块只做「校验参数 + 提交 job + 渲染结果」，业务实现在 `pipelines/`、
`writeback/`、`extraction/` 与 `tools.py`。

设计取舍：
- **零构建前端**：`webui/` 下是原生 HTML/JS/CSS，不引入 node 工具链。
  `pip install -e .` 之后一条 `ptz serve` 就能起，符合「开箱即用」。
- **SSE 而不是 WebSocket**：日志是单向推送，SSE 断线自动重连、实现更短。
- **单 worker 串行执行**：见 `jobs.py` 的说明。
- **凭据只进不出**：`GET /api/settings` 只回显尾号，前端回传脱敏值不会覆盖真值。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import routes
from .deps import Deps
from .jobs import EventBus, JobRunner
from .settings import SettingsStore
from .tools import AppContext, build_registry

WEBUI_DIR = Path(__file__).parent / "webui"

# 本机访问白名单。**不要放空串**：那等于给「没写 Host 的裸请求」开一个恒真的口子，
# 白名单就形同虚设了。浏览器总会带 Host，所以去掉它不影响任何正常用法。
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_only(raw: str) -> str:
    """从 `Host` / `Origin` 里取出主机名，正确处理 IPv6 与端口。

    `[::1]:8787` 用 `split(":")[0]` 会得到 `[` —— 于是 IPv6 本机访问被一刀切 403
    （`ptz serve --host ::1` 连首页都打不开）。这里按 RFC 3986 处理方括号。
    """
    h = (raw or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):  # [::1]:8787 / [::1]
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    if h.count(":") == 1:  # 127.0.0.1:8787 / localhost:8787
        return h.split(":", 1)[0]
    if h.count(":") > 1:  # 裸 IPv6 ::1
        return h
    return h


def create_app(
    state_dir: Path | None = None,
    mock: bool = False,
    *,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    state_dir = Path(state_dir or Path.cwd() / ".ptz-state")
    state_dir.mkdir(parents=True, exist_ok=True)

    store = SettingsStore(state_dir / "settings.json")
    ctx = AppContext(store, state_dir, mock=mock)
    bus = EventBus()
    registry = build_registry(ctx)
    runner = JobRunner(bus, registry, sink=ctx.logs)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # 启动：什么都不做（配置与连接都是惰性建立）
        yield
        # 关闭：释放 SQLite 连接与 HTTP 连接池，否则 Windows 上删不掉状态目录
        ctx.close()

    app = FastAPI(
        title="paratranz-agent",
        version=__version__,
        docs_url="/api/docs",
        lifespan=lifespan,
    )
    app.state.mock = mock
    hosts = {h.lower() for h in (allowed_hosts or LOCAL_HOSTS)}

    @app.middleware("http")
    async def security_guard(request: Request, call_next):
        """只接受本机访问，并挡掉跨站请求。

        威胁模型：WebUI 没有登录态，任何能在你机器上打开浏览器的人
        都能操作它；恶意网页也能通过 fetch 打 127.0.0.1（DNS rebinding /
        CSRF）。所以校验 Host 与 Origin，而不是靠前端隐藏按钮。
        """
        host = _host_only(request.headers.get("host") or "")
        if host not in hosts:
            return JSONResponse(
                {"error": f"拒绝来自 {host or '未知主机'} 的访问：WebUI 只允许本机访问"},
                status_code=403,
            )
        origin = request.headers.get("origin")
        if origin:
            origin_host = _host_only(origin.split("//")[-1])
            if origin_host not in hosts:
                return JSONResponse(
                    {"error": f"拒绝跨站请求（Origin: {origin_host}）"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
    app.state.ctx, app.state.bus, app.state.runner = ctx, bus, runner

    routes.register_all(
        app,
        Deps(ctx=ctx, store=store, bus=bus, runner=runner, registry=registry, mock=mock),
    )

    # ------------------------------------------------------------------ 前端
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEBUI_DIR / "index.html")

    if WEBUI_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError):  # pragma: no cover
        """把 FastAPI 的 422 压成和业务错误同一种形状。

        默认响应是 `{"detail": [{"loc": [...], "msg": ...}]}`，而业务错误一律
        `{"error": "..."}`。前端写的是 `data.error || data.detail`，于是 422 时
        `detail` 是个**数组**，toast 里会渲染成 `[object Object]`；脚本调用方
        也得写两套解析。统一成 `{"error", "fields"}`，只改一处。
        """
        from .redact import redact

        fields = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
            fields.append(f"{loc or 'body'}: {err.get('msg', '')}")
        secrets = [store.load().token, store.load().llm_key]
        return JSONResponse(
            {"error": redact("请求参数不合法：" + "；".join(fields), secrets),
             "fields": fields},
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_exc(_: Request, exc: HTTPException):  # pragma: no cover
        from .redact import redact

        secrets = [store.load().token, store.load().llm_key]
        return JSONResponse({"error": redact(str(exc.detail), secrets)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):  # pragma: no cover
        from .redact import redact

        secrets = [store.load().token, store.load().llm_key]
        return JSONResponse(
            {"error": redact(f"{type(exc).__name__}: {exc}", secrets)}, status_code=500
        )

    return app


def serve(host: str = "127.0.0.1", port: int = 8787, state_dir: str | None = None,
          open_browser: bool = True, mock: bool = False) -> None:
    import threading
    import webbrowser

    import uvicorn

    # `mock` 必须传下去：`ptz serve --mock` / `web.bat --mock`（README 与
    # WebUI说明都写着「完整演示、不碰真实项目」）曾经只改了这里没往下传，
    # 结果演示模式实际连的是**真实平台**，只是没人发现——因为 web_smoke
    # 直接调 create_app(mock=True)，从不经过 serve()。
    app = create_app(Path(state_dir) if state_dir else None, mock=mock)
    url = f"http://{host}:{port}"
    print(f"\n  paratranz-agent WebUI  →  {url}"
          f"{'  (演示模式：内置假平台，不碰真实项目)' if mock else ''}\n  按 Ctrl+C 停止\n")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
