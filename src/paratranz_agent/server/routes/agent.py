"""自然语言入口：把一句话翻译成工具调用序列。"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from ..agent import plan_actions
from ..deps import Deps
from ..tools import TOOL_SPECS


class AgentRequest(BaseModel):
    text: str
    execute: bool = True


def register(app: FastAPI, d: Deps) -> None:
    """把本组路由挂到 app 上。"""
    ctx, store, runner = (
        d.ctx,
        d.store,
        d.runner,
    )

    @app.post("/api/agent")
    def agent(body: AgentRequest) -> dict:
        llm = ctx.llm_for("agent")
        plan = plan_actions(
            body.text, TOOL_SPECS, llm, system_prompt=store.load().prompt_for("agent")
        )
        job_ids: list[str] = []
        skipped: list[dict] = []
        if body.execute and plan["steps"]:
            from ..tools import DANGEROUS_TOOLS

            for step in plan["steps"]:
                tool = step["tool"]
                if tool in DANGEROUS_TOOLS:
                    # 危险动作**绝不**由自然语言输入直接触发：模型只是把文本映射成
                    # 工具序列，而文本可能来自游戏文件内容（提示注入）。审计实测过
                    # 「忽略之前规则」这类输入能让它真的执行本机代码。
                    # 这里只给出建议，让用户自己到对应页面勾确认后再点。
                    skipped.append(
                        {"tool": tool, "args": step.get("args") or {},
                         "why": "危险动作需在页面上手动确认风险后再执行"}
                    )
                    continue
                try:
                    job = runner.submit(tool, step.get("args") or {}, origin="agent")
                    job_ids.append(job.id)
                except KeyError:
                    continue
        return {**plan, "job_ids": job_ids, "needs_confirm": skipped}
