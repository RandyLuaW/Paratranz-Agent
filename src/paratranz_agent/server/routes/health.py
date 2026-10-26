"""服务状态与实时事件：健康探针、工具清单、SSE 日志流。"""

from __future__ import annotations

import asyncio
import json
import queue

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ..deps import Deps
from ..tools import TOOL_SPECS


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    store, bus, runner, mock = (
        d.store,
        d.bus,
        d.runner,
        d.mock,
    )

    @app.get("/api/health")
    def health() -> dict:
        from ... import __version__

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

    @app.get("/api/notice")
    def get_notice() -> dict:
        """首次启动的免费声明：`pending=true` 时前端弹一次，确认后不再弹。"""
        from ..notice import payload

        return payload(d.ctx.state_dir)

    @app.post("/api/notice/ack")
    def ack_notice() -> dict:
        """记下「已确认」。幂等：重复调用只是再写一次同样的标记。"""
        from ..notice import acknowledge, payload

        acknowledge(d.ctx.state_dir)
        return {"ok": True, **payload(d.ctx.state_dir)}

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
