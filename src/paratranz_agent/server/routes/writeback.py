"""回写链路：把已确认译文写回游戏文件（提取的反向）。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..deps import Deps


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx, runner = (
        d.ctx,
        d.runner,
    )

    @app.post("/api/extractions/{jid}/writeback")
    def start_writeback(jid: str, body: dict) -> dict:
        """把平台上确认过的译文回写到游戏文件（提取的反向链路）。

        写文件是本地动作、但可能要拉几千条译文，所以走 job。
        `dry_run` 也只是「不落盘」，仍需拉一遍平台，同样走 job 才有日志。
        """
        from .. import extraction as _ex

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
        from ..writeback_store import load_last

        rep = load_last(ctx.state_dir, jid)
        if rep is None:
            raise HTTPException(404, "该提取记录还没有回写报告")
        return rep
