"""测试专用的假 LLM。

**只用于测试，不进产品路径。** 产品代码里没有「无 key 也能跑」的降级：
没有 API Key 时 `build_llm` 返回 `UnavailableLLM`，任何调用直接报错。

这里的实现用来在端到端测试里驱动完整流水线（翻译 → QA → 写回），
不联网、不花钱、结果确定。
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable, Sequence

from paratranz_agent.llm import BaseLLM, LLMError, Usage, UsageEvent, extract_json

# 极小的游戏词汇表：把英文句子里认识的词换成中文，
# 保证端到端流程能被真实驱动，而不是空转。
_MINI_DICT: dict[str, str] = {
    "hello": "你好", "hi": "嗨", "hey": "嘿", "good": "好", "morning": "早上",
    "night": "夜晚", "day": "白天", "yes": "是", "no": "不是", "okay": "好的",
    "thanks": "谢谢", "thank": "谢谢", "you": "你", "please": "请", "sorry": "抱歉",
    "welcome": "欢迎", "friend": "朋友", "farm": "农场", "field": "田地",
    "crop": "作物", "seed": "种子", "water": "水", "harvest": "收获",
    "money": "钱", "coin": "金币", "shop": "商店", "market": "市场",
    "house": "房子", "home": "家", "door": "门", "key": "钥匙",
    "sword": "剑", "shield": "盾", "potion": "药水", "health": "生命",
    "magic": "魔法", "level": "等级", "quest": "任务", "reward": "奖励",
    "village": "村庄", "town": "城镇", "forest": "森林", "river": "河流",
    "mountain": "山", "cave": "洞穴", "monster": "怪物", "enemy": "敌人",
    "attack": "攻击", "defend": "防御", "damage": "伤害", "heal": "治疗",
    "open": "打开", "close": "关闭", "use": "使用", "take": "拿取",
    "give": "给予", "talk": "说话", "go": "去", "come": "来", "wait": "等待",
    "help": "帮助", "find": "找到", "lost": "丢失", "found": "找到",
    "new": "新的", "old": "旧的", "big": "大的", "small": "小的",
    "i": "我", "me": "我", "my": "我的", "we": "我们", "they": "他们",
    "he": "他", "she": "她", "it": "它", "is": "是", "are": "是",
    "have": "有", "has": "有", "do": "做", "did": "做了", "will": "会",
    "can": "能", "not": "不", "very": "非常", "now": "现在", "here": "这里",
    "there": "那里", "what": "什么", "where": "哪里", "why": "为什么",
    "how": "怎么", "who": "谁", "when": "什么时候", "and": "和", "or": "或",
    "the": "", "a": "", "an": "", "of": "的", "to": "", "in": "在",
    "on": "在", "at": "在", "for": "为了", "with": "和", "from": "从",
}


def fake_translate(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        return _MINI_DICT.get(m.group(0).lower(), m.group(0))

    out = re.sub(r"[A-Za-z']+", repl, text)
    out = re.sub(r"\s{2,}", " ", out).strip()
    if not re.search(r"[\u4e00-\u9fff]", out):
        out = f"〔译〕{out}"
    return out


class FakeLLM(BaseLLM):
    """确定性替身。responder 可注入，用来在测试里伪造特定输出。"""

    name = "fake"

    def __init__(self, responder: Callable[[Sequence[dict]], str] | None = None):
        super().__init__()
        self.responder = responder

    def available(self) -> bool:
        return True

    def chat(
        self,
        messages: Sequence[dict],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        t0 = time.monotonic()
        prompt_est = len(str(messages)) // 3
        with self._lock:
            self.usage.add(Usage(calls=1, prompt_tokens=prompt_est))
        out = self._respond(messages)
        self._emit(
            UsageEvent(
                model="fake",
                prompt_tokens=prompt_est,
                completion_tokens=max(1, len(out) // 3),
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        )
        return out

    def _respond(self, messages: Sequence[dict]) -> str:
        if self.responder:
            return self.responder(messages)

        user = ""
        for m in reversed(list(messages)):
            if m.get("role") == "user":
                user = str(m.get("content", ""))
                break

        try:
            payload = extract_json(user)
        except LLMError:
            payload = None

        # 术语抽取批次：{"items":[{"original","translation"}, ...]}（无 id）
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("items"), list)
            and payload["items"]
            and isinstance(payload["items"][0], dict)
            and "id" not in payload["items"][0]
            and "original" in payload["items"][0]
        ):
            found: dict[str, str] = {}
            for it in payload["items"]:
                src = str(it.get("original", ""))
                dst = str(it.get("translation", ""))
                zh = "".join(re.findall(r"[\u4e00-\u9fff]", dst))[:4]
                if not zh:
                    continue
                for m in re.finditer(r"(?<![A-Za-z])([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)(?![A-Za-z])", src):
                    found.setdefault(m.group(1), zh)
            terms = [
                {"term": t, "translation": v, "pos": "noun", "note": "", "variants": []}
                for t, v in list(found.items())[:20]
            ]
            return json.dumps({"terms": terms}, ensure_ascii=False)

        # 校对批次
        if isinstance(payload, dict) and payload.get("task") == "review":
            items = [
                {"id": it.get("id"), "verdict": "pass", "reason": ""}
                for it in payload.get("items", [])
                if isinstance(it, dict)
            ]
            return json.dumps({"items": items}, ensure_ascii=False)

        # 术语定译批次
        if isinstance(payload, dict) and payload.get("task") == "term-resolve":
            items = []
            for it in payload.get("items", []):
                if not isinstance(it, dict):
                    continue
                samples = it.get("samples") or []
                guess = ""
                if samples and isinstance(samples[0], dict):
                    guess = str(samples[0].get("translation", ""))
                items.append({"term": it.get("term"), "translation": guess, "pos": "noun"})
            return json.dumps({"terms": items}, ensure_ascii=False)

        # 翻译批次
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = []
            for it in payload["items"]:
                if not isinstance(it, dict):
                    continue
                items.append(
                    {"id": it.get("id"), "translation": fake_translate(str(it.get("original", "")))}
                )
            return json.dumps({"items": items}, ensure_ascii=False)

        return json.dumps({"result": fake_translate(user)}, ensure_ascii=False)
