"""提取链路：提取器管理、运行、产物校验/导出/导入、目录画像。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..deps import Deps
from ..params import as_int


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx, store, runner = (
        d.ctx,
        d.store,
        d.runner,
    )

    @app.get("/api/extractors")
    def list_extractors_api() -> dict:
        from ..extraction import list_extractors

        return {"extractors": [m.as_dict() for m in list_extractors(ctx.state_dir)]}

    @app.post("/api/extractors")
    def install_extractor_api(body: dict) -> dict:
        from ..extraction import ExtractionError, install_extractor, list_extractors

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
        from ..extraction import ExtractionError, list_extractors, remove_extractor

        try:
            remove_extractor(ctx.state_dir, eid)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        ctx.logs.add(f"删除提取器：{eid}", level="warn", source="extract")
        return {"ok": True, "extractors": [m.as_dict() for m in list_extractors(ctx.state_dir)]}

    @app.post("/api/extractors/{eid}/run")
    def run_extractor_api(eid: str, body: dict) -> dict:
        """提交成 job（可能跑几分钟，走队列能看到日志）。"""
        from ..extraction import ExtractionError, get_extractor

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
        from ..extraction import ExtractionError, check_game_dir

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
        from ..extraction import ExtractionError, check_game_dir

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
        from ..extraction import ExtractionError, check_game_dir, get_extractor

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
        from ..extraction import list_jobs

        return {"rows": list_jobs(ctx.state_dir, limit=limit)}

    @app.get("/api/extractions/{jid}")
    def get_extraction(jid: str, limit: int = 100, offset: int = 0) -> dict:
        from ..extraction import ExtractionError, get_job, job_log, load_entries

        try:
            meta = get_job(ctx.state_dir, jid)
            # 只读这一页：原先全量解析 entries.jsonl 再切片，
            # 9087 条的记录约 2 MB，每翻一页就白解析一遍
            entries = load_entries(ctx.state_dir, jid, offset=offset, limit=limit)
        except ExtractionError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "meta": meta,
            "entries": entries,
            # 总条数取自 meta（提取完成时就写好了），不必为此再读一遍文件
            "total": int(meta.get("entries") or 0) or len(entries),
            "log": job_log(ctx.state_dir, jid),
        }

    @app.post("/api/extractions/{jid}/validate")
    def validate_extraction(jid: str) -> dict:
        from ..extraction import ExtractionError, load_entries, normalize, validate

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
        from ..extraction import (
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

        两条路，职责分明：
        - **预览**（`dry_run`）就地算一遍：校验报告 + 按平台现状算新增/跳过 + 前 20 条样例；
        - **正式导入**只校验参数并提交 job —— 取材/校验/写入全部由工具
          `extraction_import` 做一次。原先这里也跑了 load→validate→normalize→export，
          于是同一次导入把 entries.jsonl 解析两遍、校验两遍，纯属重复。
        """
        from ..extraction import (
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
        file_id_int = as_int(file_id, name="file_id")
        filename = str(body.get("filename") or "extract.json")

        if not body.get("dry_run"):
            ctx.guard()
            job = runner.submit(
                "extraction_import",
                {"extraction_id": jid, "file_id": file_id_int,
                 "force": bool(body.get("force"))},
            )
            ctx.logs.add(
                "导入 " + jid + " 到文件 " + str(file_id) + "（job " + job.id + "）",
                source="extract",
            )
            return {"ok": True, "job": job.as_dict(), "filename": filename}

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

        try:
            existing = ctx.client.existing_keys(file_id_int)
        except Exception as exc:  # noqa: BLE001 - 平台不可达要明确报错
            # 以前这里是 `existing, to_create = set(), items`，平台一挂就显示
            # 「将发送全部 N 条」——把失败伪装成成功预览。宁可报错。
            raise HTTPException(
                502, "读取平台现有词条失败，无法给出准确预览：" + str(exc)
            ) from exc
        to_create = [it for it in items if it["key"] not in existing]
        return {
            "ok": True,
            "dry_run": True,
            "count": len(to_create),
            "already": len(items) - len(to_create),
            "sample": to_create[:20],
            "report": report,
            "filename": filename,
            "file_id": file_id_int,
        }

    @app.post("/api/extract/analyze")
    def analyze_dir(body: dict) -> dict:
        from ..extraction import ExtractionError, analyze_game_dir

        path = str((body or {}).get("game_dir") or "").strip()
        if not path:
            raise HTTPException(400, "填游戏目录")
        try:
            return analyze_game_dir(path)
        except ExtractionError as exc:
            raise HTTPException(400, str(exc)) from exc
