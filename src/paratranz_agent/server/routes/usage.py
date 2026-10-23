"""观测类：模型用量与费用、请求日志。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from ..deps import Deps


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx, store = (
        d.ctx,
        d.store,
    )

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
