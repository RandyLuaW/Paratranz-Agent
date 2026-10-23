"""按定位符族分派写回：**唯一**的入口。

一个文件里可能有跨族的 key（理论上极少，但 `generic-regex` 与 `json` 提取器
可以混装在同一个目录树上），所以这里按 `ParsedLocator.family` 分组，
各族各写一遍，内容依次传递。

分派依据来自 `locator.parse_locator` —— 它本来就是「这个 key 归谁管」的
判定者。早先另写了一套 `Writer` 协议 + `_owns()` 谓词来做同一件事，
等于把同一个判断做了两遍，还多出一层注册表。现在只有这里一处。
"""
from __future__ import annotations

from typing import Mapping, Sequence

from .base import ApplyResult, WriteItem, split_key
from .json_path import apply_json
from .line import apply_line
from .locator import LocatorError, parse_locator


def family_of_key(key: str) -> str:
    """这个 key 的定位符归哪一族；认不出来返回 `"unknown"`。"""
    try:
        return parse_locator(split_key(key)[1]).family
    except LocatorError:
        return "unknown"


def apply_items(
    content: str,
    items: Sequence[WriteItem],
    *,
    opts: Mapping[str, str] | None = None,
) -> ApplyResult:
    """把 `items` 写回 `content`，返回新内容与逐条结果（不碰文件系统）。

    `opts` 是提取器当时的参数，只有行号族用得上（见 `line.apply_line`）。
    """
    groups: dict[str, list[WriteItem]] = {"json": [], "line": [], "unknown": []}
    for it in items:
        groups[family_of_key(it.key)].append(it)

    out = ApplyResult(content=content)
    for family, fn in (("json", apply_json), ("line", apply_line)):
        batch = groups[family]
        if not batch:
            continue
        part = fn(out.content, batch, opts=opts)
        out.content = part.content
        out.merge(part)
    for it in groups["unknown"]:
        out.unsupported.append(it.key)
    return out
