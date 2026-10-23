"""后台任务队列：提交、查询、取消。任务本体在 `jobs.py`。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..deps import Deps
from ..params import LIST_LIMIT


class JobCreate(BaseModel):
    tool: str
    args: dict[str, Any] = {}


# 工具函数签名里已经占用的参数名（`bind` 与 `JobRunner._loop` 会注入它们）。
# 放在 args 里会变成 `TypeError: got multiple values for argument 'ctx'` ——
# 任务终态 failed，而错误信息把内部函数签名暴露给了调用方。
RESERVED_ARG_NAMES = frozenset({"ctx", "job", "runner", "self", "kw"})


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    runner = d.runner

    @app.get("/api/jobs")
    def list_jobs(limit: int = LIST_LIMIT) -> dict:
        return {"jobs": runner.list_jobs(limit)}

    @app.post("/api/jobs")
    def create_job(body: JobCreate) -> dict:
        bad = sorted(RESERVED_ARG_NAMES & set(body.args or {}))
        if bad:
            raise HTTPException(
                400,
                "args 里不能用这些保留参数名：" + "、".join(bad)
                + "（它们由任务调度器注入）",
            )
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
