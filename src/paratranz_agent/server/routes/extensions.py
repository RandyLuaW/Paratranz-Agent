"""扩展管理：技能/工具的热启用、安装与 MCP 接入。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..deps import Deps


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx = d.ctx

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
