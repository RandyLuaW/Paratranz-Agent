"""后台任务队列：提交、查询、取消。任务本体在 `jobs.py`。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
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


def _rows_for_ids(ctx, ids: list[int], *, workers: int = 8) -> list[dict]:
    """并发把这批 id 拉成词条行（删除/失败的单条跳过，不让整页 500）。"""
    from concurrent.futures import ThreadPoolExecutor

    from ...client import ParaTranzError
    from .strings import _case

    client = ctx.client

    def fetch(sid: int):
        try:
            return _case(client.get_string(sid))
        except ParaTranzError:
            return None
        except Exception:      # noqa: BLE001 - 单条失败不该拖垮整个下钻
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for r in pool.map(fetch, ids) if r is not None]


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    runner = d.runner

    @app.get("/api/jobs/{job_id}/strings")
    def job_strings(job_id: str, limit: int = Query(300, ge=1, le=500)) -> dict:
        """「这个任务写了哪些词条」——从账本下钻，再拉这些条目的当前状态。

        为什么要有它：任务报表只写「写入 192 条」，用户没法知道是哪 192 条，
        更没法去改。内存里的 `Job.items` 只留最近 300 条且重启即失；
        账本的 `job` 列是唯一持久、可按任务反查的纽带。
        老任务（job 列为空）用「任务起止时间窗」兜底。
        """
        ctx = d.ctx
        job = runner.get(job_id)
        project = ctx.settings.project_id or None
        rows = ctx.store.ledger_items_for_job(job_id, project=project)
        source = "ledger"
        if not rows and job is not None and job.started_at:
            end = job.finished_at or job.started_at
            rows = ctx.store.ledger_items_between(
                job.started_at - 1, end + 1, pipeline=job.tool, project=project
            )
            source = "time-window"
        if not rows and job is not None:
            rows = [{"item_id": str(i.get("id")), "action": i.get("status", "")}
                    for i in (job.items or []) if i.get("id") is not None]
            source = "memory"
        ids: list[int] = []
        seen: set[int] = set()
        for r in rows:
            try:
                v = int(r["item_id"])
            except (TypeError, ValueError):
                continue
            if v in seen:
                continue
            seen.add(v)
            ids.append(v)
        ids = ids[:limit]
        if not ids:
            return {"job_id": job_id, "count": 0, "items": [], "missing": 0, "source": source}

        got = _rows_for_ids(ctx, ids)
        return {
            "job_id": job_id,
            "count": len(got),
            "missing": len(ids) - len(got),
            "items": got,
            "source": source,
        }

    @app.get("/api/ledger/strings")
    def ledger_strings(
        hours: int = Query(24, ge=1, le=24 * 30),
        pipeline: str = "",
        limit: int = Query(300, ge=1, le=500),
    ) -> dict:
        """按时间窗看「本机最近写了哪些词条」。

        为什么需要它：`job` 列是 v4 才加的，之前写入的记录没有任务号；
        而任务记录只存在内存里，WebUI 一重启就没了。用户此刻想找的那批
        （报表显示写入 192 条）正是这种情况 —— 只能按时间窗反查账本。
        """
        ctx = d.ctx
        import time as _time

        now = _time.time()
        rows = ctx.store.ledger_items_between(
            now - hours * 3600, now + 1, pipeline=pipeline,
            project=ctx.settings.project_id or None,
        )
        ids: list[int] = []
        seen: set[int] = set()
        for r in rows:
            try:
                v = int(r["item_id"])
            except (TypeError, ValueError):
                continue
            if v in seen:
                continue
            seen.add(v)
            ids.append(v)
        ids = ids[:limit]
        got = _rows_for_ids(ctx, ids)
        return {
            "job_id": f"ledger:{hours}h",
            "window_hours": hours,
            "count": len(got),
            "missing": len(ids) - len(got),
            "items": got,
            "source": "ledger-window",
        }

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
