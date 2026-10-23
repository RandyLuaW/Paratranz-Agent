"""定位符解析：把 key 里的 `<locator>` 归一成「结构化路径」。

三个内置提取器产出的定位符分两族：

**JSON 路径族**（走 `json_path` 写回器）
    data/Actors.json::[12].name            → [12, 'name']
    data/x.json::a.b[0].c                  → ['a', 'b', 0, 'c']
    data/System.json::gameTitle            → ['gameTitle']
    data/System.json::terms.basic[0]       → ['terms', 'basic', 0]

其中 rpgmaker-mv 用了三个**紧凑标签**，历史原因写成了 `ev3p0[45]` 这种形态，
必须先展开成真实路径才能定位：

    ev<e>p<p>[<i>]  → ['events', e, 'pages', p, 'list', i, 'parameters', 0]
    ce[<i>]         → ['list', i, 'parameters', 0]
    tp<p>[<i>]      → ['pages', p, 'list', i, 'parameters', 0]

**行号族**（走 `line` 写回器）
    locale/zh.txt::L123      → 行 123 的第一个命中
    locale/zh.txt::L123#2    → 行 123 的第 3 个命中（0 基）

解析失败一律抛 `LocatorError`，由 pipeline 记进 `missing`，绝不猜测写入位置。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 行号族：L<行> 或 L<行>#<序号>。#0 不会出现（0 基无后缀），但写出来也接受。
_LINE_RE = re.compile(r"^L(\d+)(?:#(\d+))?$")

# rpgmaker-mv 的三个紧凑标签
_EV_PAGE_RE = re.compile(r"^ev(\d+)p(\d+)\[(\d+)\]$")
_CE_RE = re.compile(r"^ce\[(\d+)\]$")
_TP_RE = re.compile(r"^tp(\d+)\[(\d+)\]$")

# JSON 路径片段：`.key` 或 `[idx]`；key 允许除 `.` `[` `]` 外的字符
_JSON_TOKEN_RE = re.compile(r"\.([^.\[\]]+)|\[(\d+)\]")


class LocatorError(ValueError):
    """定位符无法解析。"""


@dataclass(frozen=True)
class ParsedLocator:
    family: str                  # "json" | "line"
    steps: tuple[Any, ...] = ()  # json 族：路径步（str 键 / int 索引）
    line: int = 0                # line 族：1-based 行号
    index: int = 0               # line 族：该行内第几个命中（0 基）


def parse_locator(locator: str) -> ParsedLocator:
    """把定位符解析成 `ParsedLocator`；两边都不认就抛 `LocatorError`。"""
    loc = (locator or "").strip()
    if not loc:
        raise LocatorError("定位符为空")

    m = _LINE_RE.match(loc)
    if m:
        line = int(m.group(1))
        if line < 1:
            raise LocatorError(f"行号必须从 1 开始：{loc}")
        return ParsedLocator(family="line", line=line, index=int(m.group(2) or 0))

    return ParsedLocator(family="json", steps=parse_json_path(loc))


def parse_json_path(locator: str) -> tuple[Any, ...]:
    """JSON 路径定位符 → 路径步元组。认不出来就抛 `LocatorError`。"""
    loc = (locator or "").strip()
    if not loc:
        raise LocatorError("JSON 路径为空")

    m = _EV_PAGE_RE.match(loc)
    if m:
        e, p, i = (int(x) for x in m.groups())
        return ("events", e, "pages", p, "list", i, "parameters", 0)
    m = _CE_RE.match(loc)
    if m:
        return ("list", int(m.group(1)), "parameters", 0)
    m = _TP_RE.match(loc)
    if m:
        p, i = (int(x) for x in m.groups())
        return ("pages", p, "list", i, "parameters", 0)

    steps: list[Any] = []
    # 头部可以省略（数组根定位符 `[12].name`），也可以是裸键（`gameTitle`）
    head = re.match(r"^[^.\[\]]+", loc)
    pos = 0
    if head:
        steps.append(head.group(0))
        pos = head.end()
    elif not loc.startswith("["):
        raise LocatorError(f"JSON 路径必须以键或 [索引] 开头：{loc}")
    while pos < len(loc):
        m = _JSON_TOKEN_RE.match(loc, pos)
        if not m:
            raise LocatorError(f"JSON 路径在位置 {pos} 无法解析（{loc[pos:pos + 12]!r}）：{loc}")
        if m.group(1) is not None:
            steps.append(m.group(1))
        else:
            steps.append(int(m.group(2)))
        pos = m.end()
    if not steps:
        raise LocatorError(f"JSON 路径为空：{loc}")
    return tuple(steps)


def steps_to_locator(steps: tuple[Any, ...]) -> str:
    """路径步元组 → 可读定位符（报告与调试用）。"""
    out = ""
    for s in steps:
        out += f"[{s}]" if isinstance(s, int) else (f".{s}" if out else str(s))
    return out
