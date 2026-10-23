"""FastAPI 应用：WebUI 的后端。

设计取舍：
- **零构建前端**：`webui/` 下是原生 HTML/JS/CSS，不引入 node 工具链。
  `pip install -e .` 之后一条 `ptz serve` 就能起，符合「开箱即用」。
- **SSE 而不是 WebSocket**：日志是单向推送，SSE 断线自动重连、实现更短。
- **单 worker 串行执行**：见 `jobs.py` 的说明。
- **凭据只进不出**：`GET /api/settings` 只回显尾号，前端回传脱敏值不会覆盖真值。
"""
from __future__ import annotations

import asyncio
import json
import queue
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..client import ParaTranzError
from .agent import plan_actions
from .jobs import EventBus, JobRunner
from ..pipelines.glossary import PROPOSE_SYSTEM
from ..pipelines.review import REVIEW_SYSTEM
from ..pipelines.styleguide import STYLE_SYSTEM
from ..pipelines.translate import SYSTEM_PROMPT as TRANSLATE_SYSTEM
from .agent import AGENT_SYSTEM
from .extractor_agent import SYSTEM_PROMPT as EXTRACTOR_SYSTEM
from .settings import (
    MODEL_TASKS,
    PROMPT_LABELS,
    PROMPT_TASKS,
    TASK_LABELS,
    SettingsStore,
    join_ref,
)
from .tools import TOOL_SPECS, AppContext, build_registry

WEBUI_DIR = Path(__file__).parent / "webui"


class SettingsPatch(BaseModel):
    project_id: int | None = None
    token: str | None = None
    base_url: str | None = None
    rate_limit_per_min: int | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_key: str | None = None
    llm_concurrency: int | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    llm_timeout: float | None = None
    llm_max_retries: int | None = None
    llm_fallback_models: str | None = None
    llm_context_max_turns: int | None = None
    llm_keep_recent_ratio: float | None = None
    agent_max_steps: int | None = None
    agent_tool_timeout: int | None = None
    system_prompt_prefix: str | None = None
    style: str | None = None
    batch_size: int | None = None
    review_pass_stage: int | None = None
    review_block_on: str | None = None
    max_strings_per_task: int | None = None
    task_prefix: str | None = None
    dry_run: bool | None = None
    per_run_limit: int | None = None
    # 回写默认值：留空字段不覆盖（PUT /api/settings 只应用非 None）
    writeback_out_dir: str | None = None
    writeback_mode: str | None = None
    writeback_min_stage: int | None = None


class JobCreate(BaseModel):
    tool: str
    args: dict[str, Any] = {}


class AgentRequest(BaseModel):
    text: str
    execute: bool = True


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", ""}


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
        version="0.2.0",
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

    # ------------------------------------------------------------------ 基础
    @app.get("/api/health")
    def health() -> dict:
        from .. import __version__

        s = store.load()
        return {
            "ok": True,
            "version": __version__,
            "mock": mock,
            "configured": mock or bool(s.token and s.project_id),
            "project_id": s.project_id,
            "llm": any(x.enabled and x.api_key for x in s.providers),
            "providers": len(s.providers),
            "dry_run": s.dry_run,
            "jobs": len(runner.jobs),
        }

    @app.get("/api/tools")
    def tools() -> dict:
        return {"tools": TOOL_SPECS}

    # ------------------------------------------------------------------ 配置
    @app.get("/api/settings")
    def get_settings() -> dict:
        return {"settings": store.load().masked()}

    @app.put("/api/settings")
    def put_settings(patch: SettingsPatch) -> dict:
        data = {k: v for k, v in patch.model_dump().items() if v is not None}
        saved = store.save(data)
        bus.publish({"type": "settings", "settings": saved.masked()})
        return {"ok": True, "settings": saved.masked()}

    @app.post("/api/settings/test")
    def test_settings() -> dict:
        s = store.load()
        if not s.token:
            raise HTTPException(400, "未配置 Token")
        if not s.project_id:
            raise HTTPException(400, "未配置项目编号")
        try:
            info = ctx.client.get_project()
            members = 0
            try:
                members = len(ctx.client.list_members())
            except ParaTranzError:
                pass
            return {
                "ok": True,
                "project": {
                    "id": info.id,
                    "name": info.name,
                    "source": info.source,
                    "dest": info.dest,
                    "members": members,
                    "task_enabled": bool(info.extra.get("task")),
                    "api_edit_disabled": bool(info.extra.get("disableApiEdit")),
                    "review_mode": info.review_mode,
                },
                "warnings": _warnings(info),
            }
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 500, str(exc)) from exc

    def _warnings(info) -> list[str]:
        out: list[str] = []
        if not info.extra.get("task"):
            out.append("项目未开启「任务」功能，自动分派会失败（可在项目设置里启用）")
        if info.extra.get("disableApiEdit"):
            out.append("项目禁止非管理员通过 API 写入，翻译/校对会 403")
        if info.review_mode == 0:
            out.append("项目设置为「无须校对」，review 写入 stage=3 可能无意义")
        return out

    # ------------------------------------------------------------------ 数据
    @app.get("/api/project")
    def project() -> dict:
        try:
            return registry["status"]()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 502, str(exc)) from exc

    @app.get("/api/files")
    def files() -> dict:
        try:
            return registry["files"]()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 502, str(exc)) from exc

    @app.get("/api/tasks")
    def tasks() -> dict:
        try:
            rows = ctx.client.list_tasks(page=1, page_size=50)
        except ParaTranzError as exc:
            return {"tasks": [], "error": str(exc)}
        return {
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "files": [
                        {"name": tf.file_name, "total": tf.total, "translated": tf.translated,
                         "checked": tf.checked, "progress": tf.progress}
                        for tf in t.taskFiles
                    ],
                }
                for t in rows
            ]
        }

    # ------------------------------------------------------------------ 任务
    @app.get("/api/jobs")
    def list_jobs(limit: int = 50) -> dict:
        return {"jobs": runner.list_jobs(limit)}

    @app.post("/api/jobs")
    def create_job(body: JobCreate) -> dict:
        try:
            job = runner.submit(body.tool, body.args, origin="webui")
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job": job.as_dict()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = runner.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return {"job": job.as_dict()}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        ok = runner.cancel(job_id)
        return {"ok": ok}

    # ------------------------------------------------------------------ Agent
    @app.post("/api/agent")
    def agent(body: AgentRequest) -> dict:
        llm = ctx.llm_for("agent")
        plan = plan_actions(
            body.text, TOOL_SPECS, llm, system_prompt=store.load().prompt_for("agent")
        )
        job_ids: list[str] = []
        skipped: list[dict] = []
        if body.execute and plan["steps"]:
            from .tools import DANGEROUS_TOOLS

            for step in plan["steps"]:
                tool = step["tool"]
                if tool in DANGEROUS_TOOLS:
                    # 危险动作**绝不**由自然语言输入直接触发：模型只是把文本映射成
                    # 工具序列，而文本可能来自游戏文件内容（提示注入）。审计实测过
                    # 「忽略之前规则」这类输入能让它真的执行本机代码。
                    # 这里只给出建议，让用户自己到对应页面勾确认后再点。
                    skipped.append(
                        {"tool": tool, "args": step.get("args") or {},
                         "why": "危险动作需在页面上手动确认风险后再执行"}
                    )
                    continue
                try:
                    job = runner.submit(tool, step.get("args") or {}, origin="agent")
                    job_ids.append(job.id)
                except KeyError:
                    continue
        return {**plan, "job_ids": job_ids, "needs_confirm": skipped}

    # ---------------------------------------------------------------- 开销
    @app.get("/api/usage/summary")
    def usage_summary(days: int = 7) -> dict:
        data = ctx.usage.summary(days=days)
        s = store.load()
        data["budget"] = ctx.budget_cfg.as_dict()
        data["budget_events"] = ctx.usage.budget_events(20)
        data["current_model"] = s.llm_model
        return data

    @app.get("/api/usage/recent")
    def usage_recent(limit: int = 100) -> dict:
        return {"rows": ctx.usage.recent(limit)}

    @app.get("/api/usage/pricing")
    def usage_pricing() -> dict:
        return {
            "pricing": ctx.usage.pricing,
            "overrides": store.load().pricing_overrides or {},
        }

    @app.put("/api/usage/pricing")
    def usage_set_pricing(body: dict) -> dict:
        """覆盖或新增单价。body: {"model": {"input":x,"input_cached":y,"output":z}}"""
        current = dict(store.load().pricing_overrides or {})
        for model, price in (body or {}).items():
            if not isinstance(price, dict):
                continue
            current[model] = {k: float(v) for k, v in price.items()
                              if k in ("input", "input_cached", "output", "cache_creation")}
        store.save({"pricing_overrides": current})
        ctx.usage.pricing = {**ctx.usage.pricing, **current}
        return {"ok": True, "overrides": current}

    @app.get("/api/budget")
    def get_budget() -> dict:
        return {"budget": ctx.budget_cfg.as_dict()}

    @app.put("/api/budget")
    def put_budget(body: dict) -> dict:
        # 预算配置要过类型校验：以前直接把任意 body 存进 settings，
        # `{"per_job": "5"}` 这种字符串会让所有写操作任务在预算护栏里 TypeError 失败，
        # 等于「护栏自己把功能打挂了」。
        #
        # 注意形状：GET 返回的就是**嵌套**的 `{per_job: {...rule}, daily: {...}}`，
        # 所以「前端把 GET 结果改一改再 PUT 回来」必须合法——一层 dict 允许，
        # 只拦「dict 里还套 dict/list」以及顶层非标量。
        from ..server.usage import BudgetConfig

        body = body or {}
        bad: list[str] = []
        for k, v in body.items():
            if isinstance(v, dict):
                bad += [f"{k}.{kk}" for kk, vv in v.items() if isinstance(vv, (dict, list))]
            elif not isinstance(v, (int, float, str, bool, type(None))):
                bad.append(k)
        if bad:
            raise HTTPException(400, f"预算字段类型不合法：{bad}")
        try:
            normalized = BudgetConfig.from_dict(body).as_dict()
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"预算配置不合法：{exc}") from exc
        store.save({"budget": normalized})
        return {"ok": True, "budget": ctx.budget_cfg.as_dict()}

    # ---------------------------------------------------------------- 日志
    @app.get("/api/logs")
    def query_logs(
        level: str = "", keyword: str = "", job_id: str = "", limit: int = 300
    ) -> dict:
        rows = ctx.logs.query(
            level=level or None, keyword=keyword, job_id=job_id, limit=limit
        )
        return {"rows": rows, "stats": ctx.logs.stats()}

    @app.get("/api/logs/files")
    def log_files() -> dict:
        return {"files": ctx.logs.files()}

    @app.get("/api/logs/download")
    def log_download(name: str, tail: int = 2000) -> PlainTextResponse:
        try:
            text = ctx.logs.read_file(name, tail=tail)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        return PlainTextResponse(
            text,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.delete("/api/logs/buffer")
    def clear_log_buffer() -> dict:
        ctx.logs.clear_buffer()
        return {"ok": True}

    # ---------------------------------------------------------------- 扩展
    @app.get("/api/extensions")
    def list_extensions() -> dict:
        return {"extensions": ctx.extensions.all()}

    @app.post("/api/extensions/{kind}/{ext_id}/toggle")
    def toggle_extension(kind: str, ext_id: str, body: dict) -> dict:
        enabled = bool((body or {}).get("enabled", True))
        if kind == "plugin":
            ok = ctx.extensions.set_plugin_enabled(ext_id, enabled)
        elif kind == "skill":
            ok = ctx.extensions.set_skill_enabled(ext_id, enabled)
        elif kind == "mcp":
            cfg = ctx.extensions._read_mcp()  # noqa: SLF001 - 同包内直接操作
            servers = cfg.get("servers") or {}
            if ext_id not in servers:
                raise HTTPException(404, "MCP 服务器不存在")
            servers[ext_id]["enabled"] = enabled
            ctx.extensions._write_mcp(cfg)  # noqa: SLF001
            ok = True
        else:
            raise HTTPException(400, f"未知类型 {kind}")
        if not ok:
            raise HTTPException(404, "扩展不存在")
        ctx.logs.add(f"扩展 {kind}/{ext_id} → {'启用' if enabled else '停用'}", source="extension")
        return {"ok": True}

    @app.delete("/api/extensions/{kind}/{ext_id}")
    def remove_extension(kind: str, ext_id: str) -> dict:
        if kind == "plugin":
            ok = ctx.extensions.remove_plugin(ext_id)
        elif kind == "skill":
            ok = ctx.extensions.remove_skill(ext_id)
        elif kind == "mcp":
            ok = ctx.extensions.remove_mcp(ext_id)
        else:
            raise HTTPException(400, f"未知类型 {kind}")
        if not ok:
            raise HTTPException(404, "扩展不存在")
        ctx.logs.add(f"移除扩展 {kind}/{ext_id}", level="warn", source="extension")
        return {"ok": True}

    @app.post("/api/extensions/install")
    def install_extension(body: dict) -> dict:
        """从本地目录导入插件或 SKILL。body: {"kind":"plugin|skill","path":"..."}"""
        kind = (body or {}).get("kind")
        path = (body or {}).get("path", "")
        if not path:
            raise HTTPException(400, "缺少 path")
        try:
            if kind == "plugin":
                ext = ctx.extensions.install_plugin(path)
            elif kind == "skill":
                ext = ctx.extensions.install_skill(path)
            else:
                raise HTTPException(400, "kind 只能是 plugin 或 skill")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        ctx.logs.add(f"导入 {kind}：{ext.id}", source="extension")
        return {"ok": True, "extension": ext.as_dict()}

    @app.put("/api/extensions/mcp/{name}")
    def upsert_mcp(name: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        ctx.extensions.upsert_mcp(name, body)
        return {"ok": True}

    # ---------------------------------------------------------------- 模型
    def _providers_view() -> dict:
        s = store.load()
        out = {
            "providers": [p.masked() for p in s.providers],
            "routing": s.routing.as_dict(),
            "tasks": [{"id": t, "label": TASK_LABELS[t]} for t in MODEL_TASKS],
            "effective": {},
        }
        for t in MODEL_TASKS:
            got = s.resolve_for(t)
            out["effective"][t] = join_ref(got[0].id, got[1].id) if got else ""
        return out

    @app.get("/api/providers")
    def list_providers() -> dict:
        return _providers_view()

    @app.post("/api/providers")
    def create_provider(body: dict) -> dict:
        try:
            store.upsert_provider(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        ctx.logs.add(f"新增 provider：{body.get('id')}", source="model")
        return {"ok": True, **_providers_view()}

    @app.put("/api/providers/{pid}")
    def update_provider(pid: str, body: dict) -> dict:
        try:
            store.upsert_provider({**body, "id": pid})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        ctx.logs.add(f"更新 provider：{pid}", source="model")
        return {"ok": True, **_providers_view()}

    @app.delete("/api/providers/{pid}")
    def delete_provider(pid: str) -> dict:
        if store.load().provider(pid) is None:
            raise HTTPException(404, "provider 不存在")
        store.delete_provider(pid)
        ctx.logs.add(f"删除 provider：{pid}", level="warn", source="model")
        return {"ok": True, **_providers_view()}

    @app.post("/api/providers/{pid}/discover")
    def discover_models(pid: str) -> dict:
        """调 `GET {base_url}/models` 拉可用模型列表。"""
        import httpx as _httpx

        s = store.load()
        p = s.provider(pid)
        if p is None:
            raise HTTPException(404, "provider 不存在")
        if not p.api_key:
            raise HTTPException(400, "该 provider 未配置 API Key")
        url = p.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {p.api_key}", **(p.headers or {})}
        try:
            with _httpx.Client(timeout=25, follow_redirects=True) as c:
                r = c.get(url, headers=headers)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001 - 原样回报给用户
            from .redact import redact

            msg = redact(f"{type(exc).__name__}: {exc}", [p.api_key])
            raise HTTPException(502, f"拉取模型列表失败：{msg}") from exc

        items = data.get("data") if isinstance(data, dict) else data
        ids: list[str] = []
        for it in items or []:
            if isinstance(it, dict) and it.get("id"):
                ids.append(str(it["id"]))
            elif isinstance(it, str):
                ids.append(it)
        existing = {m.id for m in p.models}
        uniq = sorted(set(ids))
        return {
            "models": [{"id": i, "configured": i in existing} for i in uniq],
            "count": len(uniq),
        }

    @app.post("/api/providers/{pid}/models")
    def create_model(pid: str, body: dict) -> dict:
        try:
            store.upsert_model(pid, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        ctx.logs.add(f"新增模型：{pid}::{body.get('id')}", source="model")
        return {"ok": True, **_providers_view()}

    @app.put("/api/providers/{pid}/models/{mid:path}")
    def update_model(pid: str, mid: str, body: dict) -> dict:
        try:
            store.upsert_model(pid, {**body, "id": mid})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, **_providers_view()}

    @app.delete("/api/providers/{pid}/models/{mid:path}")
    def delete_model(pid: str, mid: str) -> dict:
        try:
            store.delete_model(pid, mid)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        ctx.logs.add(f"删除模型：{pid}::{mid}", level="warn", source="model")
        return {"ok": True, **_providers_view()}

    def _test_one(p, m) -> dict:
        import time as _t

        from .redact import redact

        if not p.api_key:
            return {"ok": False, "error": "未配置 API Key", "model": m.id}
        llm = ctx.llm_of(p, m)
        t0 = _t.monotonic()
        try:
            out = llm.chat(
                [{"role": "user", "content": "只回复两个字：可用"}],
                max_tokens=16,
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 - 测试失败要回显原因
            return {
                "ok": False,
                "model": m.id,
                "error": redact(f"{type(exc).__name__}: {exc}", [p.api_key])[:300],
                "latency_ms": int((_t.monotonic() - t0) * 1000),
            }
        return {
            "ok": True,
            "model": m.id,
            "latency_ms": int((_t.monotonic() - t0) * 1000),
            "reply": (out or "")[:80],
        }

    @app.post("/api/providers/{pid}/models/{mid:path}/test")
    def test_model(pid: str, mid: str) -> dict:
        s = store.load()
        p = s.provider(pid)
        m = p.model_by_id(mid) if p else None
        if p is None or m is None:
            raise HTTPException(404, "provider 或模型不存在")
        res = _test_one(p, m)
        ctx.logs.add(
            f"测试模型 {pid}::{mid}：{'成功 ' + str(res.get('latency_ms')) + 'ms' if res['ok'] else '失败'}",
            level="info" if res["ok"] else "warn",
            source="model",
        )
        return res

    @app.post("/api/providers/{pid}/models/test-all")
    def test_all_models(pid: str) -> dict:
        """一键检查该源下所有启用模型的可用性（并发 3，避免打爆）。"""
        from concurrent.futures import ThreadPoolExecutor

        s = store.load()
        p = s.provider(pid)
        if p is None:
            raise HTTPException(404, "provider 不存在")
        targets = p.usable_models
        if not targets:
            return {"results": [], "ok_count": 0, "total": 0}
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda m: _test_one(p, m), targets))
        ok_count = sum(1 for r in results if r["ok"])
        ctx.logs.add(
            f"批量测试 {pid}：{ok_count}/{len(results)} 可用", source="model"
        )
        return {"results": results, "ok_count": ok_count, "total": len(results)}

    @app.post("/api/providers/{pid}/test")
    def test_provider(pid: str) -> dict:
        """兼容旧接口：测该源第一个启用的模型。"""
        s = store.load()
        p = s.provider(pid)
        if p is None:
            raise HTTPException(404, "provider 不存在")
        if not p.api_key:
            raise HTTPException(400, "该 provider 未配置 API Key")
        ms = p.usable_models
        if not ms:
            raise HTTPException(400, "该 provider 下没有启用的模型")
        res = _test_one(p, ms[0])
        if not res["ok"]:
            raise HTTPException(502, res.get("error") or "测试失败")
        return res

    @app.get("/api/routing")
    def get_routing() -> dict:
        return _providers_view()

    @app.put("/api/routing")
    def put_routing(body: dict) -> dict:
        store.save_routing(body or {})
        ctx.logs.add("更新任务路由", source="model")
        return {"ok": True, **_providers_view()}

    @app.get("/api/models")
    def get_models() -> dict:
        """兼容视图：默认模型的扁平字段 + providers 列表。"""
        s = store.load()
        got = s.resolve_for("")
        p, m = got if got else (None, None)
        return {
            "models": {
                "base_url": p.base_url if p else s.llm_base_url,
                "model": m.id if m else s.llm_model,
                "has_key": bool(p.api_key) if p else bool(s.llm_key),
                "concurrency": p.concurrency if p else s.llm_concurrency,
                "temperature": (m.temperature if m and m.temperature is not None
                                else s.llm_temperature),
                "max_tokens": (m.max_tokens if m and m.max_tokens is not None
                               else s.llm_max_tokens),
                "timeout": p.timeout if p else s.llm_timeout,
                "max_retries": p.max_retries if p else s.llm_max_retries,
                "fallback_models": s.routing.fallback,
                "context_max_turns": s.llm_context_max_turns,
                "keep_recent_ratio": s.llm_keep_recent_ratio,
                "agent_max_steps": s.agent_max_steps,
                "agent_tool_timeout": s.agent_tool_timeout,
                "system_prompt_prefix": s.system_prompt_prefix,
            },
            "providers": [x.masked() for x in s.providers],
            "routing": s.routing.as_dict(),
            "pricing": ctx.usage.pricing,
            "current_model_pricing": ctx.usage.pricing.get(m.id if m else ""),
        }

    # --------------------------------------------------------------- 提取
    @app.get("/api/extractors")
    def list_extractors_api() -> dict:
        from .extraction import list_extractors

        return {"extractors": [m.as_dict() for m in list_extractors(ctx.state_dir)]}

    @app.post("/api/extractors")
    def install_extractor_api(body: dict) -> dict:
        from .extraction import ExtractionError, install_extractor, list_extractors

        path = str((body or {}).get("path") or "").strip()
        if not path:
            raise HTTPException(400, "填提取器目录路径")
        try:
            meta = install_extractor(ctx.state_dir, path)
        except ExtractionError as exc:
            raise HTTPException(400, str(exc)) from exc
        ctx.logs.add(f"导入提取器：{meta.id}", source="extract")
        return {"ok": True, "extractor": meta.as_dict(),
                "extractors": [m.as_dict() for m in list_extractors(ctx.state_dir)]}

    @app.delete("/api/extractors/{eid}")
    def remove_extractor_api(eid: str) -> dict:
        from .extraction import ExtractionError, list_extractors, remove_extractor

        try:
            remove_extractor(ctx.state_dir, eid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        ctx.logs.add(f"删除提取器：{eid}", level="warn", source="extract")
        return {"ok": True, "extractors": [m.as_dict() for m in list_extractors(ctx.state_dir)]}

    @app.post("/api/extractors/{eid}/run")
    def run_extractor_api(eid: str, body: dict) -> dict:
        """提交成 job（可能跑几分钟，走队列能看到日志）。"""
        from .extraction import ExtractionError, get_extractor

        try:
            get_extractor(ctx.state_dir, eid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        body = body or {}
        # 提取器脚本会在本机以当前用户权限执行任意代码，必须显式确认
        if not body.get("accept_risk"):
            raise HTTPException(
                400,
                "执行提取器等于在本机运行任意代码，需要先勾选风险确认（accept_risk=true）",
            )
        game_dir = str(body.get("game_dir") or "").strip()
        if not game_dir:
            raise HTTPException(400, "填游戏目录")
        from .extraction import ExtractionError, check_game_dir

        try:
            game_dir = str(check_game_dir(game_dir))
        except ExtractionError as exc:
            raise HTTPException(400, str(exc)) from exc
        job = runner.submit(
            "extract",
            # accept_risk 要一起带进 job：工具层还有一道闸（防止改走 /api/jobs 绕过），
            # 路由这里已经确认过了，所以直接透传 True
            {"extractor_id": eid, "game_dir": game_dir,
             "options": body.get("options") or {}, "accept_risk": True},
        )
        ctx.logs.add(
            f"用户确认风险后运行提取器 {eid}（{game_dir}）", level="warn", source="extract"
        )
        return {"ok": True, "job": job.as_dict()}

    @app.post("/api/extractors/generate")
    def generate_extractor_api(body: dict) -> dict:
        """让 Agent 扫描目录并写一个提取器（走 job，能看到进度）。"""
        from .extraction import ExtractionError, check_game_dir

        body = body or {}
        if not body.get("accept_risk"):
            raise HTTPException(
                400, "生成后会立即试跑（等于执行代码），需要先确认风险（accept_risk=true）"
            )
        game_dir = str(body.get("game_dir") or "").strip()
        if not game_dir:
            raise HTTPException(400, "填游戏目录")
        try:
            game_dir = str(check_game_dir(game_dir))
        except ExtractionError as exc:
            raise HTTPException(400, str(exc)) from exc
        s = store.load()
        if s.resolve_for("extractor") is None:
            raise HTTPException(400, "需要先在「模型」页配置一个可用的模型")
        job = runner.submit(
            "extractor_generate",
            {"game_dir": game_dir, "hint": str(body.get("hint") or "")[:500],
             "accept_risk": True},
        )
        ctx.logs.add("Agent 开始生成提取器：" + game_dir, level="warn", source="extract")
        return {"ok": True, "job": job.as_dict()}

    @app.post("/api/extractors/{eid}/improve")
    def improve_extractor_api(eid: str, body: dict) -> dict:
        """让 Agent 在现有提取器上继续改进。"""
        from .extraction import ExtractionError, check_game_dir, get_extractor

        body = body or {}
        if not body.get("accept_risk"):
            raise HTTPException(
                400, "改进后会立即试跑（等于执行代码），需要先确认风险（accept_risk=true）"
            )
        try:
            get_extractor(ctx.state_dir, eid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        game_dir = str(body.get("game_dir") or "").strip()
        if not game_dir:
            raise HTTPException(400, "填游戏目录（Agent 需要看文件才能改进）")
        try:
            game_dir = str(check_game_dir(game_dir))
        except ExtractionError as exc:
            raise HTTPException(400, str(exc)) from exc
        if store.load().resolve_for("extractor") is None:
            raise HTTPException(400, "需要先在「模型」页配置一个可用的模型")
        job = runner.submit(
            "extractor_improve",
            {"extractor_id": eid, "game_dir": game_dir,
             "feedback": str(body.get("feedback") or "")[:1000], "accept_risk": True},
        )
        ctx.logs.add("Agent 开始改进提取器：" + eid, level="warn", source="extract")
        return {"ok": True, "job": job.as_dict()}

    @app.get("/api/extractions")
    def list_extractions(limit: int = 50) -> dict:
        from .extraction import list_jobs

        return {"rows": list_jobs(ctx.state_dir, limit=limit)}

    @app.get("/api/extractions/{jid}")
    def get_extraction(jid: str, limit: int = 100, offset: int = 0) -> dict:
        from .extraction import ExtractionError, get_job, job_log, load_entries

        try:
            meta = get_job(ctx.state_dir, jid)
            entries = load_entries(ctx.state_dir, jid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "meta": meta,
            "entries": entries[offset: offset + limit],
            "total": len(entries),
            "log": job_log(ctx.state_dir, jid),
        }

    @app.post("/api/extractions/{jid}/validate")
    def validate_extraction(jid: str) -> dict:
        from .extraction import ExtractionError, load_entries, normalize, validate

        try:
            entries = load_entries(ctx.state_dir, jid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        # 先校验**原始**数据：normalize 会去重，先归并就再也发现不了 dup_key
        report = validate(entries)
        merged, merge_stats = normalize(entries)
        report["merge"] = merge_stats
        report["stats"]["after_merge"] = len(merged)
        return report

    @app.post("/api/extractions/{jid}/export")
    def export_extraction(jid: str, body: dict) -> dict:
        """导出 ParaTranz items（可直接给 update_file_translation）。"""
        from .extraction import (
            ExtractionError,
            export_paratranz,
            load_entries,
            normalize,
        )

        try:
            entries = load_entries(ctx.state_dir, jid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        merged, stats = normalize(entries)
        items = export_paratranz(merged)
        return {"items": items, "count": len(items), "merge": stats}

    @app.post("/api/extractions/{jid}/import")
    def import_extraction(jid: str, body: dict) -> dict:
        """导入提取结果到平台文件。

        平台语义：translation 接口只更新已有词条，新增要走 POST /strings（逐条）。
        所以真正导入走 job（可能几百条、受限流），dry_run 仍同步返回预览。
        """
        from .extraction import (
            ExtractionError,
            export_paratranz,
            load_entries,
            normalize,
            validate,
        )

        body = body or {}
        file_id = body.get("file_id")
        if not file_id:
            raise HTTPException(400, "选择目标文件")
        filename = str(body.get("filename") or "extract.json")
        try:
            entries = load_entries(ctx.state_dir, jid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc

        # 先校验**原始**数据：normalize 会去重，先归并就再也发现不了 dup_key
        report = validate(entries)
        if report["errors"] and not body.get("force"):
            raise HTTPException(
                400,
                "校验有 " + str(report["stats"]["error_count"]) + " 个错误，先修或勾选强制导入",
            )
        merged, _ = normalize(entries)
        items = export_paratranz(merged)

        if body.get("dry_run"):
            # 预览：不提交，只回显将要发送的内容（并按平台现状算出新增/跳过）
            try:
                existing = ctx.client.existing_keys(int(file_id))
                to_create = [it for it in items if it["key"] not in existing]
            except Exception:  # noqa: BLE001 - 预览时平台不可达也不该失败
                existing, to_create = set(), items
            return {
                "ok": True,
                "dry_run": True,
                "count": len(to_create),
                "already": len(items) - len(to_create),
                "sample": to_create[:20],
                "report": report,
                "filename": filename,
                "file_id": int(file_id),
            }

        ctx.guard()
        job = runner.submit(
            "extraction_import",
            {"extraction_id": jid, "file_id": int(file_id),
             "force": bool(body.get("force"))},
        )
        ctx.logs.add(
            "导入 " + str(len(items)) + " 条到文件 " + str(file_id) + "（job " + job.id + "）",
            source="extract",
        )
        return {"ok": True, "job": job.as_dict(), "sent": len(items), "filename": filename}

    @app.post("/api/extract/analyze")
    def analyze_dir(body: dict) -> dict:
        from .extraction import ExtractionError, analyze_game_dir

        path = str((body or {}).get("game_dir") or "").strip()
        if not path:
            raise HTTPException(400, "填游戏目录")
        try:
            return analyze_game_dir(path)
        except ExtractionError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ------------------------------------------------------------ 回写译文
    @app.post("/api/extractions/{jid}/writeback")
    def start_writeback(jid: str, body: dict) -> dict:
        """把平台上确认过的译文回写到游戏文件（提取的反向链路）。

        写文件是本地动作、但可能要拉几千条译文，所以走 job。
        `dry_run` 也只是「不落盘」，仍需拉一遍平台，同样走 job 才有日志。
        """
        from . import extraction as _ex

        body = body or {}
        try:
            meta = _ex.get_job(ctx.state_dir, jid)
        except _ex.ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not meta.get("game_dir"):
            raise HTTPException(400, "该提取记录没有记录游戏目录，无法回写")

        min_stage = int(body.get("min_stage") or 3)
        if min_stage not in (1, 3, 5):
            raise HTTPException(400, "译文门槛只能是 1/3/5")
        mode = str(body.get("mode") or "").strip().lower()
        if mode and mode not in ("mirror", "patch"):
            raise HTTPException(400, "回写形式只能是 mirror 或 patch")
        in_place = bool(body.get("in_place"))
        dry_run = bool(body.get("dry_run", True))
        if in_place and not dry_run and not body.get("accept_risk"):
            # 与提取/导入一致：危险动作的前端弹窗只是提醒，
            # 真正的闸门在后端这个字段上，改 DOM 绕不过去。
            raise HTTPException(400, "原地修改游戏文件需要显式确认风险（accept_risk）")

        ctx.guard()
        job = runner.submit(
            "writeback",
            {
                "extraction_id": jid,
                "min_stage": min_stage,
                "out_dir": str(body.get("out_dir") or ""),
                "mode": mode or "mirror",
                "in_place": in_place,
                "accept_risk": bool(body.get("accept_risk")),
                "dry_run": dry_run,
                "platform_file": int(body.get("platform_file") or 0),
            },
        )
        ctx.logs.add(
            "回写提取记录 " + jid + "（门槛 stage≥" + str(min_stage) + "，"
            + ("预览" if dry_run else ("原地写" if in_place else
               ("整包镜像" if (mode or "mirror") == "mirror" else "仅改动文件"))) + "）",
            source="extract",
        )
        return {"ok": True, "job": job.as_dict(), "job_id": job.id}

    @app.get("/api/extractions/{jid}/writeback/last")
    def last_writeback(jid: str) -> dict:
        """最近一次回写的报告。job 结束就落盘，页面刷新后还能看到。"""
        from .writeback_store import load_last

        rep = load_last(ctx.state_dir, jid)
        if rep is None:
            raise HTTPException(404, "该提取记录还没有回写报告")
        return rep

    # --------------------------------------------------------------- 提示词
    DEFAULT_PROMPTS = {
        "translate": TRANSLATE_SYSTEM,
        "review": REVIEW_SYSTEM,
        "glossary": PROPOSE_SYSTEM,
        "styleguide": STYLE_SYSTEM,
        "agent": AGENT_SYSTEM,
        "extractor": EXTRACTOR_SYSTEM,
    }

    @app.get("/api/prompts")
    def get_prompts() -> dict:
        s = store.load()
        return {
            "prompts": s.prompts.model_dump(),
            "defaults": DEFAULT_PROMPTS,
            "tasks": [{"id": t, "label": PROMPT_LABELS[t]} for t in PROMPT_TASKS],
            "prefix_effective": bool((s.prompts.prefix or "").strip()),
        }

    @app.put("/api/prompts")
    def put_prompts(body: dict) -> dict:
        cur = store.load().prompts.model_dump()
        for k in ("prefix", *PROMPT_TASKS):
            if k in (body or {}) and isinstance(body[k], str):
                cur[k] = body[k]
        store.save({"prompts": cur})
        ctx.logs.add("更新提示词配置", source="prompt")
        return {"ok": True, **get_prompts()}

    @app.post("/api/prompts/reset")
    def reset_prompts(body: dict) -> dict:
        """恢复默认：task 为空表示全部恢复（含 prefix）。"""
        task = (body or {}).get("task", "")
        cur = store.load().prompts.model_dump()
        if task in PROMPT_TASKS:
            cur[task] = ""
        else:
            cur = {"prefix": "", **{t: "" for t in PROMPT_TASKS}}
        store.save({"prompts": cur})
        ctx.logs.add(f"恢复默认提示词：{task or '全部'}", source="prompt")
        return {"ok": True, **get_prompts()}

    # ------------------------------------------------------------------ SSE
    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        q = bus.subscribe()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.25)
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ 前端
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEBUI_DIR / "index.html")

    if WEBUI_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")

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
