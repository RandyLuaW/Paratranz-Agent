"""Agent 层：把「人话」翻译成工具调用序列。

两条路径：
1. **规则优先**（默认）：关键词 → 工具映射，快、可预测、不花钱。
   中文/英文都能命中，多个命中按流水线顺序排列。
2. **模型兜底**：规则一个都没命中时才问 LLM（要求输出 JSON 计划），
   避免每句话都花一次调用。

参数也从话里抽：「翻 100 条」→ limit=100，「只看看别写」→ dry_run=true。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..llm import BaseLLM, LLMError, build_messages, extract_json

# 关键词 → 工具。顺序即优先级（同一句里命中多个时按此顺序排）
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("plan", ("分派", "派任务", "建任务", "分配", "plan", "assign", "task")),
    ("translate", ("翻译", "汉化", "译", "翻", "translate", "localize")),
    ("review", ("校对", "检查", "审", "复核", "review", "proofread", "check")),
    ("glossary", ("术语", "名词", "词表", "glossary", "term")),
    ("styleguide", ("规范", "风格", "指南", "style", "guideline")),
    ("status", ("状态", "进度", "概览", "情况", "status", "progress", "overview")),
    ("files", ("文件", "清单", "列表", "files", "list")),
]

ALL_WORDS = ("全部", "全流程", "一键", "都跑", "一条龙", "全自动", "everything", "all")

DRY_WORDS = ("只预览", "别写", "不要写", "先看看", "预览", "dry", "dry-run", "试运行")

AGENT_SYSTEM = """你是 ParaTranz 汉化项目的操作助理。根据用户的一句话，选出要执行的工具序列。

可用工具：
{tools}

规则：
- 只输出 JSON：{{"steps":[{{"tool":"<工具名>","args":{{}},"why":"<一句话理由>"}}],"reply":"<给用户的简短回复>"}}
- 用户要「全部/全流程」时用 run 一个工具即可，不要拆成五个。
- 用户只问状态时只给 status。
- 不确定时给 status，并在 reply 里说明需要补充什么。
- args 只能包含工具声明的参数，没有就留空对象。"""


@dataclass
class Action:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def as_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args, "why": self.why}


def _extract_args(text: str, tool: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    low = text.lower()

    if any(w in low for w in DRY_WORDS):
        args["dry_run"] = True

    # 数量：「100 条」「翻 50 个」「limit 200」
    m = re.search(r"(\d{1,5})\s*(?:条|个|句|行)", text) or re.search(r"limit\s*[:=]?\s*(\d{1,5})", low)
    if m and tool in ("translate", "review"):
        args["limit"] = min(int(m.group(1)), 5000)

    if tool == "review" and ("疑问" in text or "打回" in text or "disputed" in low):
        args["include_disputed"] = True
    if tool == "glossary" and ("覆盖" in text or "overwrite" in low):
        args["overwrite"] = True

    return args


def rule_plan(text: str, tools: list[dict]) -> list[Action]:
    low = text.lower()
    valid = {t["name"] for t in tools}

    if any(w in low for w in ALL_WORDS):
        if "run" in valid:
            return [Action("run", _extract_args(text, "run"), "用户要求全流程")]

    hits: list[str] = []
    for tool, words in RULES:
        if tool in valid and any(w in low for w in words):
            hits.append(tool)

    # 去重保序
    seen: set[str] = set()
    ordered = [h for h in hits if not (h in seen or seen.add(h))]
    return [Action(t, _extract_args(text, t), "命中关键词") for t in ordered]


def llm_plan(
    text: str, tools: list[dict], llm: BaseLLM, system_prompt: str = ""
) -> list[Action]:
    catalog = "\n".join(
        f"- {t['name']}：{t['description']}" + (f"（参数：{list(t['params'])}）" if t["params"] else "")
        for t in tools
        if t["name"] not in ("qa_check",)
    )
    try:
        system = (system_prompt.strip() or AGENT_SYSTEM).format(tools=catalog)
        raw = llm.chat(
            build_messages(system, text),
            json_mode=True,
        )
        data = extract_json(raw)
    except LLMError:
        return []

    valid = {t["name"] for t in tools}
    steps = data.get("steps") if isinstance(data, dict) else None
    out: list[Action] = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        tool = str(s.get("tool", ""))
        if tool not in valid:
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        out.append(Action(tool, args, str(s.get("why", ""))[:120]))
    return out


def plan_actions(
    text: str, tools: list[dict], llm: BaseLLM | None = None, system_prompt: str = ""
) -> dict:
    """返回 {steps, reply, source}。"""
    text = (text or "").strip()
    if not text:
        return {"steps": [], "reply": "请描述要做什么，例如「把未翻译的翻完并校对」。", "source": "empty"}

    steps = rule_plan(text, tools)
    if steps:
        names = "、".join(s.tool for s in steps)
        return {"steps": [s.as_dict() for s in steps], "reply": f"将执行：{names}", "source": "rule"}

    if llm is not None and llm.available():
        steps = llm_plan(text, tools, llm, system_prompt)
        if steps:
            names = "、".join(s.tool for s in steps)
            return {"steps": [s.as_dict() for s in steps], "reply": f"将执行：{names}", "source": "llm"}

    return {
        "steps": [],
        "reply": "没听懂要做什么。可以试：「翻译」「校对」「整理术语」「出规范」「分派任务」「全流程」。",
        "source": "none",
    }
