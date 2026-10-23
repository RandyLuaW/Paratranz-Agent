"""凭据脱敏。

三道防线：
1. **已知密钥精确替换** —— 当前配置里的 token / api_key，只要出现在任何文本里就抹掉；
2. **格式匹配** —— `sk-xxx`、`Bearer xxx`、`api_key=xxx` 这类没登记但明显是密钥的串；
3. **落盘前脱敏** —— 日志写文件之前就替换，而不是只在前端遮。

原则：宁可多遮。日志里出现一串被误遮的普通文本，比泄露一个 key 便宜得多。
"""
from __future__ import annotations

import re
from typing import Iterable

# 常见密钥形态。注意顺序：先匹配更具体的。
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # sk-proj-xxx / sk-xxx（OpenAI、DeepSeek、Moonshot 等）
    # sk-(OpenAI/DeepSeek) / rv_(River) / pk_ / ak_ 等常见前缀
    # 前缀后可能是 - 或 _（River 用 rv_，OpenAI 用 sk-）
    (re.compile(r"\b(?:sk|rv|pk|ak)[-_][A-Za-z0-9_\-]{12,}"), "***REDACTED***"),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 ***REDACTED***"),
    # key=value / "api_key": "value" / token: value
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|password|token)"
            r"(\s*[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._\-]{8,})"
        ),
        r"\1\2***REDACTED***",
    ),
    # 长十六进制串（32 位以上，常见于 hash/密钥）
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "***REDACTED***"),
    # URL 里的 userinfo：https://user:pass@host
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
]

MIN_SECRET_LEN = 6


def mask_tail(value: str, keep: int = 4) -> str:
    """给界面回显用：只露尾号。"""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= keep:
        return "*" * len(v)
    return "*" * 8 + v[-keep:]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """把已知密钥与疑似密钥的串替换成掩码。"""
    if not text:
        return text
    out = str(text)

    # 1) 已知密钥精确替换（长的先替，避免短的把长的切碎）
    known = sorted(
        {s for s in secrets if s and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True
    )
    for s in known:
        if s in out:
            out = out.replace(s, mask_tail(s))

    # 2) 格式匹配
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def make_redactor(secret_provider) -> "callable":
    """返回一个可复用的脱敏函数；每次调用时取当前密钥，配置改了立刻生效。"""

    def _do(text: str) -> str:
        try:
            secrets = secret_provider() or []
        except Exception:  # noqa: BLE001 - 取配置失败也要能脱敏
            secrets = []
        return redact(text, secrets)

    return _do
