"""LLM 通道：OpenAI 兼容 chat/completions。

- `json_mode` 的 response_format **要跟网关协商**：标准 OpenAI 认
  `{"type": "json_object"}`，而部分中转站只认 `{"type": "json_schema"}`
  且强制 `strict: true`，给 `json_object` 直接回 400。协商顺序
  `object → schema → none`，学到的结果缓存在实例上，后续批次不再撞同一个墙；
  每次降级都记进 `json_warnings`，由调用方写进任务报表（不允许静默）；
- 返回做围栏剥离与 JSON 修复；
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


class _ResponseFormatRejected(LLMError):
    """网关明确拒绝了这个 response_format 传法 —— 可以换一档再试，不算调用失败。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


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
    # 命中提示缓存的 input token：按 token 汇总成本时不带这一项，
    # 会把「缓存价」当「原价」算，系统性高估花费
    cached_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls
        self.cached_tokens += other.cached_tokens

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "calls": self.calls,
        }


def strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", t, re.S)
    if m:
        return m.group(1).strip()
    return t


def _strip_trailing_commas(frag: str) -> str:
    """删掉 `,}` / `,]` 这种尾逗号，**只动结构、不动字符串内容**。

    以前用正则 `re.sub(r",\\s*([}\\]])", r"\\1")`：它不懂字符串状态，
    `{"translation":"配置示例 {a,} 就这样"}` 会被改成 `{a}` —— 解析成功、
    内容被悄悄改了、还没有任何告警。这里走同一套"是否在字符串内"的扫描。
    """
    out: list[str] = []
    in_string = False
    i, n = 0, len(frag)
    while i < n:
        ch = frag[i]
        if ch == "\\" and in_string:
            out.append(frag[i : i + 2])
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if not in_string and ch == ",":
            j = i + 1
            while j < n and frag[j] in " \t\r\n":
                j += 1
            if j < n and frag[j] in "}]":
                i += 1          # 丢掉这个逗号，保留后面的空白与括号
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_json_strings(frag: str) -> str:
    """把模型输出里"不是合法 JSON、但意图明确"的字符串修补成合法 JSON。

    实测四类（都能让整批解析失败，而根因是模型没转义）：
    1. **值里未转义的半角引号**：`"reason":"原文是"递过衣物篮"，译文…"` ——
       模型把中文引号打成了 `"`；
    2. **游戏控制符里的非法转义**：`"translation":"你好 \\N[1] 再见"` —— JSON 只认
       `\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX`，`\\N` / `\\V` 直接报
       `Invalid \\escape`。**这是本项目的主场景**（RPG Maker 的 `\\N[1] \\V[2] \\C[3]`）；
    3. **字符串内的字面换行/制表符**：多行译文会被模型原样写成真换行，JSON 不允许；
    4. 其它 `\\u` 后面不是四位十六进制的半截转义。

    做法：一次扫描，维护"是否在字符串内"。
    - 字符串内遇到 `"`：向后看第一个非空白字符，若是 `,` `}` `]` `:` 或到结尾才算结束，
      否则补反斜杠（规则 1）；
    - 字符串内遇到 `\\`：后面不是合法转义序列就把它当成字面反斜杠，补成 `\\\\`（规则 2/4）；
    - 字符串内遇到控制字符：转成 `\\n` / `\\r` / `\\t` / `\\uXXXX`（规则 3）；
    - 字符串外的字符原样保留（那里的空白本来就是合法分隔）。
    """
    out: list[str] = []
    in_string = False
    i, n = 0, len(frag)
    valid_simple = '"\\/bfnrt'
    while i < n:
        ch = frag[i]
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            i += 1
            continue
        # —— 以下都在字符串内部 ——
        if ch == "\\":
            nxt = frag[i + 1] if i + 1 < n else ""
            if nxt and nxt in valid_simple:
                out.append(frag[i : i + 2])
                i += 2
                continue
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", frag[i + 2 : i + 6] or ""):
                out.append(frag[i : i + 6])
                i += 6
                continue
            # 非法转义（`\N` `\V` `\C` 或半截 `\u`）：按字面反斜杠处理
            out.append("\\\\")
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and frag[j] in " \t\r\n":
                j += 1
            nxt = frag[j] if j < n else ""
            if nxt in ",}]:" or nxt == "":
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            i += 1
            continue
        if ch == "\n":
            out.append("\\n")
            i += 1
            continue
        if ch == "\r":
            out.append("\\r")
            i += 1
            continue
        if ch == "\t":
            out.append("\\t")
            i += 1
            continue
        if ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract_json(text: str) -> Any:
    """尽力从模型输出里抠出 JSON：先去围栏，再取首个 {...} / [...] 片段。

    失败时把**最后一次解析错误的原因**一起报出来：`Unterminated string`
    / `Unterminated object` 通常意味着输出被 max_tokens 截断（该调大上限或分批），
    而 `Invalid \\escape` / `Expecting ',' delimiter` 才是模型把 JSON 写坏了。
    以前只报"无法解析 + 原文前 300 字"，两种情况的处置完全不同却长得一样。
    """
    t = strip_fences(text)
    last_reason = ""
    try:
        return json.loads(t)
    except json.JSONDecodeError as exc:
        last_reason = str(exc)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        end = t.rfind(closer)
        if start != -1 and end > start:
            frag = t[start : end + 1]
            try:
                return json.loads(frag)
            except json.JSONDecodeError as exc:
                last_reason = str(exc)
                # 常见病分两步治，**不要合并**：先只删尾逗号试一次
                # （`{"t": "it's",}` 这种输出只差尾逗号，把单引号也一起替换会
                # 反而把它改坏，然后报「模型输出无法解析」——原因指向模型却是这里的锅）
                # 注意：删尾逗号也走字符串状态机 —— 用正则会把**字符串内容**里的
                # `{a,}` 改成 `{a}`（解析成功、内容被改坏、没有任何告警）。
                fixed = _strip_trailing_commas(frag)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError as exc2:
                    last_reason = str(exc2)
                # 再治「字符串内容不合 JSON 规矩」：未转义引号、`\N[1]` 这类
                # 非法转义、字面换行。详见 `_repair_json_strings` 的四类场景。
                repaired = _repair_json_strings(fixed)
                if repaired != fixed:
                    try:
                        return json.loads(repaired)
                    except json.JSONDecodeError as exc3:
                        last_reason = str(exc3)
                try:
                    return json.loads(fixed.replace("'", '"'))
                except json.JSONDecodeError as exc4:
                    last_reason = str(exc4)
                    continue
    hint = ""
    # 只有"Unterminated"才说明是被截断。`Expecting value` 在位置 0 是"这压根不是
    # JSON"，混在一起会给用户指错方向（去调 max_tokens 而不是换模型/改提示词）。
    if "Unterminated" in last_reason:
        hint = "（输出像是被截断了：可能是 max_tokens 太小或一次塞的条目太多）"
    raise LLMError(
        f"无法从模型输出解析 JSON{hint}：{last_reason or '未知原因'}；"
        f"原文前 300 字：{text[:300]}"
    )


def build_messages(system: str, payload: Any) -> list[dict]:
    """组装 OpenAI 兼容的 system + user 消息。

    所有环节都是「system 给规则，user 给 JSON 载荷」这一种形状，原先在 7 个模块里
    各写了一遍（`[{"role":"system"...},{"role":"user", json.dumps(payload)}]`）。
    统一到这里之后，将来要加 system 缓存标记、加 tool_calls、换协议都只改这一处。

    `payload` 传 str 视为已序列化（少数环节直接给文本）。
    中文不转义：`ensure_ascii=True` 会把每个汉字变成 6 字节的 `\\uXXXX`，
    在这些 payload 上实测能让输入 token 翻 2~3 倍，纯属浪费。

    system 原样透传（**不做 strip**）：调用方各自构造的 system 可能带尾随换行
    （如 `extractor_agent.SYSTEM_PROMPT`），这里再处理一次就等于把 prompt
    悄悄改了——重构不该顺手改提示词。
    """
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return [
        {"role": "system", "content": system or ""},
        {"role": "user", "content": body},
    ]


class BaseLLM:
    name = "base"

    def __init__(self) -> None:
        self.usage = Usage()
        self._lock = threading.Lock()
        # 由 server 层注册：把每次调用的用量交给成本统计
        self.on_usage: Callable[[UsageEvent], None] | None = None
        # 与网关协商出来的 json 传输方式（"object" / "schema" / "none"）。
        # 空串 = 还没试过。学一次就固定，避免每批都先撞一次 400。
        # **按阶梯分开存**：带 schema 的调用点（翻译）与不带 schema 的调用点
        # （校对/术语/规范）能试的档位不同，共用一个值会让不带 schema 的调用
        # 学到 "none" 之后把翻译的 schema 档永久废掉（那正是唯一能用的档）。
        self.json_kind_schema: str = ""
        self.json_kind_plain: str = ""
        # 协商过程中发生的降级说明。**必须被上层消费**（写进报表/日志），
        # 否则"网关不支持 json_object"这件事就只剩一个看不见的默认值。
        self.json_warnings: list[str] = []

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
        json_schema: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

    def chat_json(
        self, messages: Sequence[dict], *, json_schema: dict | None = None, **kw: Any
    ) -> Any:
        return extract_json(
            self.chat(messages, json_mode=True, json_schema=json_schema, **kw)
        )

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
        # 源级自定义头先铺，`Authorization` / `Content-Type` 最后写：
        # 自定义头可以让网关加标识、换 header 名，但不应该悄悄顶掉我们自己的密钥。
        headers = {k: str(v) for k, v in (cfg.headers or {}).items()}
        headers["Authorization"] = f"Bearer {cfg.api_key}"
        headers["Content-Type"] = "application/json"
        self._client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            timeout=cfg.timeout,
            headers=headers,
            # 留空 = 直连。设置页填了代理就必须真的走代理：
            # 只把值存下来、连接时不用，等于「代理已配置」是 UI 幻觉。
            proxy=(cfg.proxy or None),
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
        json_schema: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        base: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        mt = max_tokens if max_tokens is not None else (self.cfg.max_tokens or 0)
        if mt:
            base["max_tokens"] = mt
        # 模型级自定义参数最后合并，允许覆盖上面的默认值
        if self.cfg.extra_params:
            base.update(self.cfg.extra_params)

        # 用户自己在 `extra_params` 里写了 response_format → 那是他的明确选择，
        # 协商一律让路（否则"配置了但不生效"比没有这个开关更糟）。
        manual_rf = "response_format" in (self.cfg.extra_params or {})
        attempts = ("",) if manual_rf else self._json_attempts(json_mode, json_schema)
        for i, kind in enumerate(attempts):
            body = dict(base)
            rf = self._response_format(kind, json_schema)
            if rf is not None:
                body["response_format"] = rf
            try:
                out = self._post(body)
            except _ResponseFormatRejected as rej:
                nxt = attempts[i + 1] if i + 1 < len(attempts) else ""
                if i + 1 >= len(attempts):
                    # 没有下一档可退：报错，绝不"降级成假装成功"
                    raise LLMError(
                        f"网关拒绝了所有 response_format 传法（最后一档 {kind or '无'}）：{rej.detail}"
                    ) from None
                self._note_downgrade(kind, nxt, rej.detail, bool(json_schema))
                continue
            if json_mode and kind:
                self._remember_kind(kind, bool(json_schema))
            return out
        raise LLMError("模型调用失败：response_format 协商没有可用方案")

    # 网关对 response_format 的支持不一致（实测：某中转站只认 json_schema，
    # 且强制 strict:true，给 json_object 直接回 400）。协商逻辑只在这里，
    # 别处不要再判断"这个网关支持什么"。
    _AUTO_LADDER = ("object", "none")
    _AUTO_LADDER_SCHEMA = ("object", "schema", "none")

    def _remember_kind(self, kind: str, has_schema: bool) -> None:
        if has_schema:
            self.json_kind_schema = kind
        else:
            self.json_kind_plain = kind

    def _json_attempts(self, json_mode: bool, json_schema: dict | None) -> tuple[str, ...]:
        """本次调用依次尝试的 response_format 传法。"""
        if not json_mode or self.cfg.json_mode == "off":
            return ("",)
        has_schema = bool(json_schema)
        want = self.cfg.json_mode
        if want == "object":
            return ("object",)
        if want == "schema":
            if has_schema:
                return ("schema",)
            # 配了 schema 但调用点没给 —— 说清楚，不要静默退成 object
            note = "llm.json_mode=schema，但这个调用点没有提供 json_schema，已按 json_object 处理"
            if note not in self.json_warnings:
                self.json_warnings.append(note)
            return ("object",)
        ladder = self._AUTO_LADDER_SCHEMA if has_schema else self._AUTO_LADDER
        learned = self.json_kind_schema if has_schema else self.json_kind_plain
        # 已经协商出结果：直接从那一档开始，不必每批都先撞一次 400
        if learned and learned in ladder:
            return ladder[ladder.index(learned) :]
        return ladder

    def _note_downgrade(self, kind: str, nxt: str, detail: str, has_schema: bool) -> None:
        self._remember_kind(nxt, has_schema)
        # 提示里用协议里的真名（json_object / json_schema），别用内部档位名：
        # 用户会拿这句话去搜文档、去问网关方。
        wire = {"object": "json_object", "schema": "json_schema", "none": "无", "": "无"}
        target = {
            "object": "json_object",
            "schema": "json_schema(strict)",
            "none": "不传 response_format（仅靠提示词约束 JSON）",
            "": "不传 response_format（仅靠提示词约束 JSON）",
        }.get(nxt, nxt)
        note = f"网关不接受 response_format={wire.get(kind, kind)}（{detail[:120]}），已改用 {target}"
        if note not in self.json_warnings:
            self.json_warnings.append(note)

    @staticmethod
    def _response_format(kind: str, schema: dict | None) -> dict | None:
        if kind == "object":
            return {"type": "json_object"}
        if kind == "schema":
            # strict:true 是硬要求，缺它网关直接 400（实测）
            return {
                "type": "json_schema",
                "json_schema": {"name": "payload", "strict": True, "schema": schema},
            }
        return None

    def _post(self, body: dict[str, Any]) -> str:
        last: Exception | None = None
        t0 = time.monotonic()
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._client.post("/chat/completions", json=body)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                # 用基类收：PoolTimeout / ConnectTimeout / ReadError 都是最典型的
                # 瞬时错误（恰恰最该重试），以前只写了 3 个子类，其余以裸 httpx
                # 异常穿透，`max_retries` 完全不生效，报错也只有 "PoolTimeout"。
                last = exc
                if attempt < self.cfg.max_retries:
                    time.sleep(min(2 ** attempt, 20))
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                last = LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                # 最后一轮还睡就是纯等（最多 20s）然后才报"重试耗尽"，没有意义
                if attempt < self.cfg.max_retries:
                    time.sleep(min(2 ** attempt, 20))
                continue
            if resp.status_code >= 400:
                text = resp.text[:300]
                # 只有"网关明确拒绝 response_format"才降档：限定请求体参数相关的
                # 4xx，并且要真的提到这个字段。放宽会让鉴权失败/模型不存在也
                # 被当成"换个传法就能好"，最后报一个与真实原因无关的错误。
                if resp.status_code in (400, 422) and "response_format" in text:
                    raise _ResponseFormatRejected(f"HTTP {resp.status_code}: {text}") from None
                raise LLMError(f"HTTP {resp.status_code}: {text}")

            try:
                data = resp.json()
            except ValueError as exc:
                # 网关回 200 + HTML（登录页/错误页）时，裸 JSONDecodeError 会绕过
                # 所有 LLMError 分类，上层只能看到 "Expecting value"
                raise LLMError(
                    "模型接口返回的不是 JSON（API 地址可能填错，或请求被网关拦截）："
                    + resp.text[:200].replace("\n", " ")
                ) from exc
            if not isinstance(data, dict):
                raise LLMError(f"响应结构异常: {str(data)[:200]}")
            choice = (data.get("choices") or [{}])[0]
            if not isinstance(choice, dict):
                raise LLMError(f"响应结构异常: {str(data)[:200]}")
            usage = data.get("usage") or {}
            prompt_t = int(usage.get("prompt_tokens", 0) or 0)
            completion_t = int(usage.get("completion_tokens", 0) or 0)
            cached_t = int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            )
            finish = choice.get("finish_reason")
            with self._lock:
                self.usage.add(
                    Usage(
                        prompt_tokens=prompt_t,
                        completion_tokens=completion_t,
                        calls=1,
                        cached_tokens=cached_t,
                    )
                )
            # 被截断 / 被内容过滤的回答不能当成功：截断的 JSON 会解析失败、
            # 截断的译文会少半句，而统计里显示 100% 成功率，没人查得出原因。
            if finish in ("length", "content_filter"):
                self._emit(
                    UsageEvent(
                        model=self.cfg.model,
                        prompt_tokens=prompt_t,
                        completion_tokens=completion_t,
                        cached_tokens=cached_t,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        ok=False,
                    )
                )
                raise LLMError(
                    f"模型输出不完整（finish_reason={finish}）："
                    f"{str(choice.get('message', {}))[:160]}"
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
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                raise LLMError(f"响应结构异常: {str(data)[:200]}")
            return message.get("content") or ""

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

    def chat(
        self,
        messages,
        *,
        json_mode=False,
        json_schema: dict | None = None,
        temperature=None,
        max_tokens=None,
    ) -> str:
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
