"""模型配置：提供商源、其下模型、任务路由。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..deps import Deps
from ..settings import MODEL_TASKS, TASK_LABELS, TASK_LABELS_EN, join_ref


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx, store = (
        d.ctx,
        d.store,
    )

    def _providers_view() -> dict:
        s = store.load()
        out = {
            "providers": [p.masked() for p in s.providers],
            "routing": s.routing.as_dict(),
            "tasks": [
                {"id": t, "label": TASK_LABELS[t], "label_en": TASK_LABELS_EN.get(t, "")}
                for t in MODEL_TASKS
            ],
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
            from ..redact import redact

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

        from ..redact import redact

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
