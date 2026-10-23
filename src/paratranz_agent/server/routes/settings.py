"""配置类：项目/凭据设置、提示词、预算。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import create_model

from ...client import ParaTranzError
from ...pipelines.glossary import PROPOSE_SYSTEM
from ...pipelines.review import REVIEW_SYSTEM
from ...pipelines.styleguide import STYLE_SYSTEM
from ...pipelines.translate import SYSTEM_PROMPT as TRANSLATE_SYSTEM
from ..agent import AGENT_SYSTEM
from ..deps import Deps
from ..extractor_agent import SYSTEM_PROMPT as EXTRACTOR_SYSTEM
from ..settings import PROMPT_LABELS, PROMPT_TASKS, RuntimeSettings

# 这几个字段各有专用端点（providers/routing/prompts/budget/pricing），
# 不走「打补丁」这条路，所以补丁模型里排除它们。
_PATCH_EXCLUDE = {"providers", "routing", "prompts", "budget", "pricing_overrides"}

# `PUT /api/settings` 的补丁模型：字段表由 `RuntimeSettings` **自动派生**。
# 手抄一份的代价已经付过 —— 新增 `writeback_*` 三项时忘了同步，
# pydantic 把未知字段静默丢掉：PUT 返回 200，配置却没落盘，界面从此对不上。
# 派生之后新增字段自动进补丁，这类漂移不可能再发生。
# 语义：全部字段可选，只应用非 None 的那些（留空 = 不改）。
SettingsPatch = create_model(
    "SettingsPatch",
    **{
        name: (f.annotation | None, None)
        for name, f in RuntimeSettings.model_fields.items()
        if name not in _PATCH_EXCLUDE
    },
)


DEFAULT_PROMPTS = {
    "translate": TRANSLATE_SYSTEM,
    "review": REVIEW_SYSTEM,
    "glossary": PROPOSE_SYSTEM,
    "styleguide": STYLE_SYSTEM,
    "agent": AGENT_SYSTEM,
    "extractor": EXTRACTOR_SYSTEM,
}


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx, store, bus = (
        d.ctx,
        d.store,
        d.bus,
    )

    @app.get("/api/settings")
    def get_settings() -> dict:
        return {"settings": store.load().masked()}

    @app.put("/api/settings")
    def put_settings(patch: SettingsPatch) -> dict:
        data = {k: v for k, v in patch.model_dump().items() if v is not None}
        saved = store.save(data)
        bus.publish({"type": "settings", "settings": saved.masked()})
        return {"ok": True, "settings": saved.masked()}

    @app.post("/api/settings/test")
    def test_settings() -> dict:
        s = store.load()
        if not s.token:
            raise HTTPException(400, "未配置 Token")
        if not s.project_id:
            raise HTTPException(400, "未配置项目编号")
        try:
            info = ctx.client.get_project()
            members = 0
            try:
                members = len(ctx.client.list_members())
            except ParaTranzError:
                pass
            return {
                "ok": True,
                "project": {
                    "id": info.id,
                    "name": info.name,
                    "source": info.source,
                    "dest": info.dest,
                    "members": members,
                    "task_enabled": bool(info.extra.get("task")),
                    "api_edit_disabled": bool(info.extra.get("disableApiEdit")),
                    "review_mode": info.review_mode,
                },
                "warnings": _warnings(info),
            }
        except ParaTranzError as exc:
            raise HTTPException(exc.status or 500, str(exc)) from exc

    @app.get("/api/prompts")
    def get_prompts() -> dict:
        s = store.load()
        return {
            "prompts": s.prompts.model_dump(),
            "defaults": DEFAULT_PROMPTS,
            "tasks": [{"id": t, "label": PROMPT_LABELS[t]} for t in PROMPT_TASKS],
            "prefix_effective": bool((s.prompts.prefix or "").strip()),
        }

    @app.put("/api/prompts")
    def put_prompts(body: dict) -> dict:
        cur = store.load().prompts.model_dump()
        for k in ("prefix", *PROMPT_TASKS):
            if k in (body or {}) and isinstance(body[k], str):
                cur[k] = body[k]
        store.save({"prompts": cur})
        ctx.logs.add("更新提示词配置", source="prompt")
        return {"ok": True, **get_prompts()}

    @app.post("/api/prompts/reset")
    def reset_prompts(body: dict) -> dict:
        """恢复默认：task 为空表示全部恢复（含 prefix）。"""
        task = (body or {}).get("task", "")
        cur = store.load().prompts.model_dump()
        if task in PROMPT_TASKS:
            cur[task] = ""
        else:
            cur = {"prefix": "", **{t: "" for t in PROMPT_TASKS}}
        store.save({"prompts": cur})
        ctx.logs.add(f"恢复默认提示词：{task or '全部'}", source="prompt")
        return {"ok": True, **get_prompts()}

    @app.get("/api/budget")
    def get_budget() -> dict:
        return {"budget": ctx.budget_cfg.as_dict()}

    @app.put("/api/budget")
    def put_budget(body: dict) -> dict:
        # 预算配置要过类型校验：以前直接把任意 body 存进 settings，
        # `{"per_job": "5"}` 这种字符串会让所有写操作任务在预算护栏里 TypeError 失败，
        # 等于「护栏自己把功能打挂了」。
        #
        # 注意形状：GET 返回的就是**嵌套**的 `{per_job: {...rule}, daily: {...}}`，
        # 所以「前端把 GET 结果改一改再 PUT 回来」必须合法——一层 dict 允许，
        # 只拦「dict 里还套 dict/list」以及顶层非标量。
        from ..usage import BudgetConfig

        body = body or {}
        bad: list[str] = []
        for k, v in body.items():
            if isinstance(v, dict):
                bad += [f"{k}.{kk}" for kk, vv in v.items() if isinstance(vv, (dict, list))]
            elif not isinstance(v, (int, float, str, bool, type(None))):
                bad.append(k)
        if bad:
            raise HTTPException(400, f"预算字段类型不合法：{bad}")
        try:
            normalized = BudgetConfig.from_dict(body).as_dict()
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"预算配置不合法：{exc}") from exc
        store.save({"budget": normalized})
        return {"ok": True, "budget": ctx.budget_cfg.as_dict()}

    def _warnings(info) -> list[str]:
        out: list[str] = []
        if not info.extra.get("task"):
            out.append("项目未开启「任务」功能，自动分派会失败（可在项目设置里启用）")
        if info.extra.get("disableApiEdit"):
            out.append("项目禁止非管理员通过 API 写入，翻译/校对会 403")
        if info.review_mode == 0:
            out.append("项目设置为「无须校对」，review 写入 stage=3 可能无意义")
        return out
