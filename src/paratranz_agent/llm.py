"""LLM 通道：OpenAI 兼容 chat/completions。

- `json_mode` 走 response_format={"type": "json_object"}，并对返回做围栏剥离与 JSON 修复；
- 并发用线程池（httpx.Client 线程安全，单 client 复用连接池）；
- 429 / 5xx / 超时走指数退避；
- **没有 api_key 时不降级**：返回 `UnavailableLLM`，调用即报错，避免"看起来在跑其实没翻"。
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import httpx

from .config import LLMCfg


class LLMError(RuntimeError):
    pass


@dataclass
class UsageEvent:
    """一次模型调用的用量快照，供成本统计消费。"""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    latency_ms: int = 0
    ok: bool = True


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "calls": self.calls,
        }


def strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", t, re.S)
    if m:
        return m.group(1).strip()
    return t


def extract_json(text: str) -> Any:
    """尽力从模型输出里抠出 JSON：先去围栏，再取首个 {...} / [...] 片段。"""
    t = strip_fences(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        end = t.rfind(closer)
        if start != -1 and end > start:
            frag = t[start : end + 1]
            try:
                return json.loads(frag)
            except json.JSONDecodeError:
                # 常见病：尾逗号、单引号
                fixed = re.sub(r",\s*([}\]])", r"\1", frag).replace("'", '"')
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    continue
    raise LLMError(f"无法从模型输出解析 JSON: {text[:300]}")


class BaseLLM:
    name = "base"

    def __init__(self) -> None:
        self.usage = Usage()
        self._lock = threading.Lock()
        # 由 server 层注册：把每次调用的用量交给成本统计
        self.on_usage: Callable[[UsageEvent], None] | None = None

    def _emit(self, ev: UsageEvent) -> None:
        if self.on_usage is None:
            return
        try:
            self.on_usage(ev)
        except Exception:  # noqa: BLE001 - 统计失败不能影响翻译
            pass

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
        raise NotImplementedError

    def chat_json(self, messages: Sequence[dict], **kw: Any) -> Any:
        return extract_json(self.chat(messages, json_mode=True, **kw))

    def map_concurrent(
        self,
        items: Iterable[Any],
        fn: Callable[[Any], Any],
        *,
        concurrency: int = 4,
        on_done: Callable[[int, int], None] | None = None,
    ) -> list[Any]:
        """保序并发。异常按位置返回 Exception，由调用方决定如何处理。

        注意：`contextvars` **不会**自动传播到新线程。这里显式
        `copy_context()` 再 `run`，否则上层用来标记「当前任务」的
        ContextVar（成本归因靠它）在子线程里会读到默认值，
        用量就全部挂到空 job_id 上。

        `on_done(已完成数, 总数)` 每完成一项回调一次：9000 条校对要跑几十批，
        没有进度输出时人和 Agent 都分不清「在跑」还是「挂了」（真实踩过：
        一次全量校对静默 20 分钟，被当成卡死中断）。
        """
        items = list(items)
        if not items:
            return []
        results: list[Any] = [None] * len(items)
        # 必须在**主线程**复制上下文：worker 线程里 copy_context() 复制到的是
        # 它自己的空上下文。每个任务一份，避免同一个 Context 被多线程同时 enter。
        contexts = [contextvars.copy_context() for _ in items]

        def worker(idx: int, item: Any) -> None:
            try:
                results[idx] = contexts[idx].run(fn, item)
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                results[idx] = exc

        total = len(items)
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(worker, i, it) for i, it in enumerate(items)]
            for done, f in enumerate(futures, start=1):
                f.result()
                if on_done is not None:
                    on_done(done, total)
        return results


class OpenAICompatLLM(BaseLLM):
    name = "openai-compat"

    def __init__(self, cfg: LLMCfg):
        super().__init__()
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            timeout=cfg.timeout,
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
        )

    def available(self) -> bool:
        return bool(self.cfg.api_key)

    def close(self) -> None:
        self._client.close()

    def chat(
        self,
        messages: Sequence[dict],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        # 模型级自定义参数最后合并，允许覆盖上面的默认值
        if self.cfg.extra_params:
            body.update(self.cfg.extra_params)

        last: Exception | None = None
        t0 = time.monotonic()
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._client.post("/chat/completions", json=body)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last = exc
                time.sleep(min(2 ** attempt, 20))
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                last = LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(min(2 ** attempt, 20))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            data = resp.json()
            usage = data.get("usage") or {}
            prompt_t = int(usage.get("prompt_tokens", 0) or 0)
            completion_t = int(usage.get("completion_tokens", 0) or 0)
            cached_t = int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            )
            with self._lock:
                self.usage.add(
                    Usage(prompt_tokens=prompt_t, completion_tokens=completion_t, calls=1)
                )
            self._emit(
                UsageEvent(
                    model=self.cfg.model,
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    cached_tokens=cached_t,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )
            )
            try:
                return data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError) as exc:
                raise LLMError(f"响应结构异常: {data}") from exc

        raise LLMError(f"LLM 重试耗尽: {last}")


class LLMNotConfigured(LLMError):
    """未配置模型 API。这是配置错误，不是运行时故障，调用方应直接暴露给用户。"""


class UnavailableLLM(BaseLLM):
    """占位实现：没有 API Key 时用它，任何调用都明确报错。

    刻意不提供「无 key 也能跑」的降级——那种产出没有意义，
    还会让人误以为翻译在正常工作。
    """

    name = "unavailable"

    def available(self) -> bool:
        return False

    def chat(self, messages, *, json_mode=False, temperature=None, max_tokens=None) -> str:
        raise LLMNotConfigured(
            "尚未配置模型 API。请到「模型」页填写 API 地址、模型名与 API Key 后再执行任务。"
        )

    def chat_json(self, messages, **kw):
        raise LLMNotConfigured(
            "尚未配置模型 API。请到「模型」页填写 API 地址、模型名与 API Key 后再执行任务。"
        )


def build_llm(cfg: LLMCfg) -> BaseLLM:
    """有 key 用真模型，没 key 返回占位实现（调用即报错）。"""
    if not cfg.api_key:
        return UnavailableLLM()
    return OpenAICompatLLM(cfg)
