"""平台数据查询：项目概览、文件列表、任务列表。只读。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...client import ParaTranzError
from ..deps import Deps


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx, registry = (
        d.ctx,
        d.registry,
    )

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
