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
            from ..tools import agent_needs_confirm

            for step in plan["steps"]:
                tool = step["tool"]
                if agent_needs_confirm(tool):
                    # **写本机文件 / 执行代码 / 不可撤回地批量建词条**这几类，绝不
                    # 由自然语言输入直接触发：模型会把文本映射成「工具名 + 任意 args」，
                    # 而 `writeback` 的 `in_place`+`accept_risk` 就是同一次请求里的
                    # 普通字段（模型照着输出一份就能越过弹窗与 5 秒确认）。
                    # 判定来源是 TOOL_SPECS 的 `agent` 标记（缺标记=拦），
                    # 不是这里再手写一份黑名单。
                    skipped.append(
                        {"tool": tool, "args": step.get("args") or {},
                         "why": "该动作会写本机文件、执行代码或不可撤回地批量写入平台，"
                                "需在对应页面手动确认后再执行"}
                    )
                    continue
                try:
                    job = runner.submit(tool, step.get("args") or {}, origin="agent")
                    job_ids.append(job.id)
                except KeyError:
                    continue
        return {**plan, "job_ids": job_ids, "needs_confirm": skipped}
