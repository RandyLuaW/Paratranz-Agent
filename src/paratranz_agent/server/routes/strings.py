"""词条工作台：列表 / 详情（含平台改动记录）/ 人工改译文与改阶段。

为什么这组路由必须有：翻译与校对都是"模型批量改远端数据"，而模型会出错。
没有词条级视图时，人只能在 ParaTranz 网站里逐条翻，或者全盘信任报表 ——
出了问题既看不到是哪一条，也改不动。

三条设计约束：
- **写回走与流水线相同的入口**（`batch_edit_strings`），不新开一条写路径；
- **平台没确认就等于没写成**：`BatchWrite.ids` 为空时回 502，不报"已保存"；
- **写后回读**：返回的是平台上现在的样子，不是我们提交的那份，
  否则界面会显示一个平台并没有接受的译文。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import ValidationError

from ...client import ParaTranzError
from ...models import STAGE_ZH, Stage, StringItem
from ..deps import Deps
from ..params import MAX_PAGE, as_int

# 一次批量操作的上限。平台侧本身就是分块接口，这里是**界面能一次点选多少条**的口径：
# 500 与平台单次 batchOperateStrings 的上限一致，再大就得考虑分批与失败重试了。
MAX_BATCH = 500


def _ids_of(raw: Any, *, limit: int = MAX_BATCH) -> list[int]:
    """把请求里的 ids 收窄成**去重后**的整数列表（非法值直接 400，不静默丢弃）。

    去重是必须的：平台批量接口按条提交，重复 id 会被算两次 ——
    `confirmed` 就会大于实际条数，界面上"提交 5、确认 6"看着像平台出了错。
    """
    if not isinstance(raw, list) or not raw:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for x in raw[:limit]:
        try:
            v = int(x)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"ids 里混了非整数：{x!r}") from exc
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _case(row: Any) -> dict[str, Any]:
    """词条的统一出参形状（前端只认这一种）。"""
    file_obj = row.file if isinstance(row.file, dict) else {}
    return {
        "id": row.id,
        "key": row.key,
        "original": row.original,
        "translation": row.translation,
        "stage": int(row.stage),
        "stage_zh": STAGE_ZH.get(int(row.stage), str(row.stage)),
        "context": row.context or "",
        "file_id": row.file_id,
        # 真实平台会给 `file: {id,name}`；假平台/精简响应只有 fileId，
        # 这时显示 `#id` 而不是空白——空白会让人以为"这条不属于任何文件"。
        "file_name": file_obj.get("name") or (f"#{row.file_id}" if row.file_id else ""),
    }


def _history_rows(raw: Any) -> list[dict[str, Any]]:
    """平台改动记录 → 界面用形状。`from`/`to` 是 Python 关键字之外的字段名，原样保留。"""
    out: list[dict[str, Any]] = []
    for h in raw or []:
        if not isinstance(h, dict):
            continue
        user = h.get("user") or {}
        out.append(
            {
                "id": h.get("id"),
                "at": h.get("createdAt"),
                "operation": h.get("operation") or "",
                "field": h.get("field") or "",
                "before": h.get("from") or "",
                "after": h.get("to") or "",
                "user": user.get("username") or "",
                "user_id": user.get("id"),
            }
        )
    return out


def register(app: FastAPI, d: Deps) -> None:
    ctx = d.ctx

    def _client():
        try:
            return ctx.client
        except RuntimeError as exc:  # 没填 Token / 项目号
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/strings")
    def list_strings(
        file: int | None = None,
        stage: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=MAX_PAGE),
    ) -> dict:
        """按文件/阶段分页读词条。阶段留空或 `all` = 不筛。"""
        st: int | None = None
        if stage not in (None, "", "all"):
            value = as_int(stage, name="stage")
            try:
                st = int(Stage(value))
            except ValueError as exc:
                raise HTTPException(400, f"stage 取值非法：{value}") from exc
        try:
            rows, meta = _client().page_strings(
                page=page, page_size=page_size, file=[file] if file else None, stage=st
            )
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 502, str(exc)) from exc
        return {
            "items": [_case(r) for r in rows],
            "page": meta.get("page", page),
            "page_size": meta.get("pageSize", page_size),
            "total": meta.get("rowCount", 0),
            "pages": meta.get("pageCount", 0),
        }

    @app.get("/api/strings/{string_id}")
    def get_string(string_id: int) -> dict:
        """单条词条 + 两份记录。

        平台 `history` **只记网页端/人工操作**：用批量接口写回的改动不会进历史
        （实测：改完译文再读该词条，`history` 仍是 0 条）。所以这里同时给本地账本
        `local_history`（我们的流水线对这条做过什么），两者合起来才是完整记录，
        否则界面会显示"没有任何改动记录"——明明刚跑过一次翻译。
        """
        client = _client()
        try:
            raw = client.request("GET", f"/projects/{{projectId}}/strings/{string_id}")
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 502, str(exc)) from exc
        if not isinstance(raw, dict):
            # 真实平台对已删除的词条回 200 + null（不是 404）
            raise HTTPException(404, f"词条 {string_id} 不存在（可能已被删除）")
        # 复用这一次响应，不再发第二次 GET（省一次限流额度，也避免两次之间状态已变）
        try:
            row = StringItem.model_validate(raw)
        except ValidationError as exc:
            raise HTTPException(502, f"词条 {string_id} 的响应形状认不出：{exc}") from exc
        try:
            local = ctx.store.ledger_for_item(string_id, project=ctx.settings.project_id)
        except Exception:  # noqa: BLE001 - 本地账本读失败不该让详情 500
            local = []
        return {
            "item": _case(row),
            "history": _history_rows(raw.get("history")),
            "local_history": local,
            "import_history": len(raw.get("importHistory") or []),
            "comments": raw.get("comments") or [],
        }

    @app.put("/api/strings/{string_id}")
    def update_string(string_id: int, body: dict) -> dict:
        """人工改译文 / 改阶段。

        `dry_run`（顶部「演练模式」）打开时只回显将要提交的内容，不落平台 ——
        一个开关必须对所有写操作一致，否则"演练"就成了选择性演练。
        """
        has_tr = "translation" in body
        has_stage = "stage" in body
        if not has_tr and not has_stage:
            raise HTTPException(400, "至少要给 translation 或 stage 其中一项")
        payload: dict[str, Any] = {"id": int(string_id)}
        if has_tr:
            tr = body.get("translation")
            if not isinstance(tr, str):
                raise HTTPException(400, "translation 必须是字符串")
            payload["translation"] = tr
        if has_stage:
            if body.get("stage") is None:
                # 缺值不等于 0：as_int 的默认 0 会把这条静默打回「未翻译」（审计实测）
                raise HTTPException(400, "改阶段必须给出 stage 值")
            value = as_int(body.get("stage"), name="stage")
            try:
                payload["stage"] = int(Stage(value))
            except ValueError as exc:
                raise HTTPException(400, f"stage 取值非法：{value}") from exc

        client = _client()
        if ctx.settings.dry_run:
            return {"ok": True, "dry_run": True, "would_write": payload}
        try:
            with ctx.write_lock:      # 与批量写互斥：并发写会互相覆盖 stage
                done = client.batch_edit_strings([payload], chunk=1)
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 502, str(exc)) from exc
        if not done:
            # 平台没确认这一条落库（BatchWrite 是 list 子类：len=确知 id 的条数，
            # `.reported` = 平台报告的改动条数）——绝不回 ok，否则界面显示"已保存"。
            raise HTTPException(
                502,
                f"平台没有确认写入（报告更新 {done.reported} 条），这一条可能未生效，请刷新核对",
            )
        # 人工修改也进本地账本：平台 history 不记 API 写回，不记的话
        # 「本机处理记录」里会只有机器动作，看不出人改过什么。
        try:
            what = "改译文" if has_tr else "改阶段"
            # replace=True：人工改同一条可能有很多次，覆盖式记账才看得到最近一次
            ctx.store.ledger_mark(
                "manual", str(string_id), "edit", f"{what}（WebUI 人工）", replace=True
            )
        except Exception:  # noqa: BLE001 - 记账失败不该让写回报错
            pass
        # 回读失败**不能**把一次已成功的写入变成 500：前端会因此丢弃用户输入、
        # 界面与平台不一致（审计实测：429 时 500，而平台值确已变）。
        try:
            fresh = client.get_string(string_id)
        except Exception as exc:  # noqa: BLE001 - 写成功了，只是读不回来
            return {
                "ok": True, "dry_run": False, "item": None,
                "note": f"已写入平台，但回读失败（{type(exc).__name__}: {exc}），请刷新核对",
            }
        return {"ok": True, "dry_run": False, "item": _case(fresh)}

    @app.post("/api/strings/batch")
    def batch_update(body: dict) -> dict:
        """批量操作：改阶段 / 清空译文（重译走 `POST /api/jobs {tool:translate,args:{ids}}`）。

        为什么要有它：校对打回、术语统一、误译清理都是**成片**的活儿。
        逐条点会把一次下午的活干成三天，而平台侧本来就是批量接口。
        """
        ids = _ids_of(body.get("ids"), limit=MAX_BATCH)
        action = str(body.get("action") or "")
        if not ids:
            raise HTTPException(400, "ids 不能为空")
        if action not in ("stage", "clear"):
            raise HTTPException(400, "action 只能是 stage（改阶段）或 clear（清空译文并退回未翻译）")

        items: list[dict[str, Any]] = []
        if action == "stage":
            if body.get("stage") is None:
                raise HTTPException(400, "action=stage 时必须给出 stage 值")
            value = as_int(body.get("stage"), name="stage")
            try:
                stage = int(Stage(value))
            except ValueError as exc:
                raise HTTPException(400, f"stage 取值非法：{value}") from exc
            items = [{"id": i, "stage": stage} for i in ids]
        else:
            items = [{"id": i, "translation": "", "stage": int(Stage.UNTRANSLATED)} for i in ids]

        client = _client()
        if ctx.settings.dry_run:
            return {"ok": True, "dry_run": True, "count": len(items), "would_write": items[:50]}
        try:
            with ctx.write_lock:
                done = client.batch_edit_strings(items)
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 502, str(exc)) from exc
        if not done and not done.reported:
            raise HTTPException(502, "平台没有确认任何一条写入，请刷新核对")
        # 只给"平台确认过"的那些记账：确认不全时把未确认的也记成改过，
        # 界面上就会出现"本机说改过、平台没变"（审计实测 count=3/confirmed=1）。
        confirmed_ids = list(done) or (ids if done.reported >= len(ids) else [])
        try:
            for i in confirmed_ids:
                ctx.store.ledger_mark(
                    "manual", str(i), "edit", f"批量{action}（WebUI）",
                    replace=True, project=ctx.settings.project_id,
                )
        except Exception:  # noqa: BLE001 - 记账失败不影响已完成的写入
            pass
        partial = done.reported < len(items)
        return {
            "ok": not partial, "dry_run": False,
            "count": len(items), "confirmed": done.reported, "partial": partial,
            "note": (
                f"平台只确认了 {done.reported}/{len(items)} 条，其余可能未生效，请刷新核对"
                if partial else ""
            ),
        }

    @app.get("/api/records")
    def records(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=MAX_PAGE),
        tid: int | None = None,
    ) -> dict:
        """项目改动流水（含我们自己的写回）。`tid` = 只看某条词条。"""
        try:
            rows = _client().get_history(page=page, page_size=page_size, tid=tid)
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 502, str(exc)) from exc
        out = []
        for h in rows:
            if not isinstance(h, dict):
                continue
            target = h.get("target") or {}
            out.append(
                {
                    **_history_rows([h])[0],
                    "tid": h.get("tid"),
                    "key": target.get("key") or "",
                    "original": target.get("original") or "",
                    "stage": target.get("stage"),
                }
            )
        return {"items": out, "page": page, "page_size": page_size}
