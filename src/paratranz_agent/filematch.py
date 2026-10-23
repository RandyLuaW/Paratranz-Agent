"""路径白名单匹配：一处实现，两处调用。

「只处理这些文件」这件事在本项目里有两种输入：
- 翻译用 `translate.files`，比对的是**平台文件名**（`www/data/Actors.json`）；
- 回写用 `--only`，比对的是**提取记录里的相对路径**（`data/Actors.json`）。

两者要的是同一条语义：**精确相等，或位于该路径之下**。早先两处各写了一遍，
而且都写成了子串匹配 —— `--only a` 会命中一切含 `a` 的路径，属于「静默筛错」。
"""
from __future__ import annotations

from typing import Sequence


def _norm(path: str) -> str:
    """归一：反斜杠转正斜杠、去掉开头的 `./` 与 `/`、大小写折叠。

    Windows 上同一个文件可能写成 `Data\\A.json` / `data/a.json` / `./data/a.json`，
    不归一的话白名单会因为写法不同而漏掉本该处理的文件。
    """
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/").casefold()


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    """`path` 是否命中白名单里的任意一项（精确相等或以 `项/` 开头）。

    空白名单表示「不筛」，返回 True —— 让调用方不必自己判空。
    """
    wanted = [_norm(a) for a in (patterns or []) if str(a or "").strip()]
    if not wanted:
        return True
    rel = _norm(path)
    for a in wanted:
        if rel == a or rel.startswith(a.rstrip("/") + "/"):
            return True
    return False
