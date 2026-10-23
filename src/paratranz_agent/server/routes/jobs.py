"""后台任务队列：提交、查询、取消。任务本体在 `jobs.py`。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..deps import Deps


class JobCreate(BaseModel):
    tool: str
    args: dict[str, Any] = {}


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    runner = d.runner

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
