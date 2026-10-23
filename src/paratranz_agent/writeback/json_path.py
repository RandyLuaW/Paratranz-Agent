r"""JSON 路径族写回器：**手术式替换字符串字面量**，不重新序列化。

为什么不能 `json.load` 再 `json.dump`：
- RPG Maker 的 `data/Map001.json` 一改格式，整个文件 diff 爆炸，版本管理全废；
- 原文件可能是 `\uXXXX` 转义、特定缩进、甚至尾随空格，dump 回来都会变；
- 更要紧的是**它无法校验**：我们要确认「文件里这段现在还是不是当初提取的那句原文」，
  而 span 级替换天然拿得到当前字面量的解码值。

做法：递归下降扫描一遍原文，记下每个字符串**值**的 `(起, 止, 解码值)` 与它的路径，
然后按路径查表、从后往前拼接替换。文件里除被替换的字面量内容外，一个字节都不动。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .base import ApplyResult, WriteItem
from .locator import LocatorError, parse_locator

# 需要转义的 JSON 控制字符
_CTRL = {chr(i): "\\u%04x" % i for i in range(0x20)}
_CTRL.update({"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"})
_ESC_NEEDED = re.compile(r'["\\\x00-\x1f]')


def json_escape(text: str) -> str:
    """把译文转成 JSON 字符串字面量内容（不含两侧引号）。"""
    return _ESC_NEEDED.sub(lambda m: _CTRL.get(m.group(0), "\\" + m.group(0)), text)


class JsonScanError(ValueError):
    """原文不是合法 JSON（或被截断）。"""


class _Scanner:
    """极简 JSON 扫描器：只关心字符串字面量的位置与路径。"""

    __slots__ = ("s", "i", "n", "spans")

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)
        self.spans: dict[tuple[Any, ...], tuple[int, int, str]] = {}

    # ---------------------------------------------------------------- 基础
    def _err(self, msg: str) -> JsonScanError:
        return JsonScanError(f"{msg}（位置 {self.i}）")

    def _ws(self) -> None:
        s, n = self.s, self.n
        while self.i < n and s[self.i] in " \t\r\n":
            self.i += 1

    def _string(self) -> str:
        """解析一个字符串字面量，返回解码值，游标移到收尾引号之后。"""
        s, n = self.s, self.n
        if self.i >= n or s[self.i] != '"':
            raise self._err("期望字符串")
        self.i += 1
        out: list[str] = []
        while True:
            if self.i >= n:
                raise self._err("字符串未闭合")
            ch = s[self.i]
            if ch == '"':
                self.i += 1
                return "".join(out)
            if ch == "\\":
                self.i += 1
                if self.i >= n:
                    raise self._err("转义未闭合")
                e = s[self.i]
                if e == "u":
                    hexs = s[self.i + 1 : self.i + 5]
                    if len(hexs) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", hexs):
                        raise self._err("\\u 转义非法")
                    out.append(chr(int(hexs, 16)))
                    self.i += 5
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                            '"': '"', "\\": "\\", "/": "/"}.get(e, e))
                self.i += 1
                continue
            out.append(ch)
            self.i += 1

    def _number(self) -> None:
        m = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", self.s[self.i :])
        if not m:
            raise self._err(f"非法值起始字符 {self.s[self.i]!r}")
        self.i += m.end()

    # ---------------------------------------------------------------- 递归
    def value(self, path: tuple[Any, ...]) -> None:
        self._ws()
        if self.i >= self.n:
            raise self._err("意外的文件结尾")
        ch = self.s[self.i]
        if ch == "{":
            self._object(path)
        elif ch == "[":
            self._array(path)
        elif ch == '"':
            start = self.i
            decoded = self._string()
            self.spans[path] = (start, self.i, decoded)
        elif ch in "tfn":
            for lit in ("true", "false", "null"):
                if self.s.startswith(lit, self.i):
                    self.i += len(lit)
                    return
            raise self._err("非法字面量")
        else:
            self._number()

    def _object(self, path: tuple[Any, ...]) -> None:
        self.i += 1  # {
        self._ws()
        if self.i < self.n and self.s[self.i] == "}":
            self.i += 1
            return
        while True:
            self._ws()
            key = self._string()
            self._ws()
            if self.i >= self.n or self.s[self.i] != ":":
                raise self._err("缺少冒号")
            self.i += 1
            self.value(path + (key,))
            self._ws()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == "}":
                self.i += 1
                return
            raise self._err("对象未正确结束")

    def _array(self, path: tuple[Any, ...]) -> None:
        self.i += 1  # [
        self._ws()
        if self.i < self.n and self.s[self.i] == "]":
            self.i += 1
            return
        idx = 0
        while True:
            self.value(path + (idx,))
            idx += 1
            self._ws()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == "]":
                self.i += 1
                return
            raise self._err("数组未正确结束")


def scan_strings(text: str) -> dict[tuple[Any, ...], tuple[int, int, str]]:
    """扫描 JSON 原文，返回 {路径: (字面量起点, 终点, 解码值)}。

    只收录**字符串值**（不含对象键）；路径用元组表示，如 `("events", 3, "name")`。
    """
    sc = _Scanner(text)
    sc.value(())
    sc._ws()
    if sc.i != sc.n:
        raise JsonScanError(f"顶层值之后还有多余内容（位置 {sc.i}）")
    return sc.spans


class JsonPathWriter:
    """JSON 路径型写回器（`[12].name`、`events[3].pages[0].list[2].parameters[0]`…）。"""

    name = "json"

    def can_handle(self, locator: str) -> bool:
        try:
            return parse_locator(locator).family == "json"
        except LocatorError:
            return False

    def apply(
        self,
        content: str,
        items: Sequence[WriteItem],
        *,
        opts: Mapping[str, str] | None = None,
    ) -> ApplyResult:
        res = ApplyResult()
        try:
            spans = scan_strings(content)
        except JsonScanError as exc:
            # 整个文件解析不了：所有条目都记错误，绝不部分写入
            for it in items:
                res.errors.append((it.key, f"文件不是合法 JSON：{exc}"))
            res.content = content
            return res

        edits: list[tuple[int, int, str]] = []
        for it in items:
            try:
                parsed = parse_locator(it.key.split("::", 1)[-1])
            except LocatorError as exc:
                res.unsupported.append(f"{it.key}（{exc}）")
                continue
            span = spans.get(parsed.steps)
            if span is None:
                res.missing.append(it.key)
                continue
            start, end, current = span
            if current == it.translation:
                # 已经是目标状态：幂等，不重复改
                res.written.append(it.key)
                continue
            # 提取器会把 original `strip()` 后上传（`generic-json` 的 `s = node.strip()`），
            # 所以文件里的字面量可能带首尾空白而平台记录没有。逐字比对会让这类词条
            # **永远写不进去**，而且报错还写成「原文不一致：文件里是 Alice，平台记录是 Alice」。
            if current != it.original and current.strip() != it.original.strip():
                res.mismatched.append(
                    (it.key, f"原文不一致：文件里是 {_short(current)}，平台记录是 {_short(it.original)}")
                )
                continue
            # 只替换引号内的内容：start+1 跳过开引号，end-1 跳过闭引号
            edits.append((start + 1, end - 1, json_escape(it.translation)))
            res.written.append(it.key)

        res.content = _splice(content, edits)
        return res


def _short(text: str, limit: int = 40) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


def _splice(content: str, edits: Sequence[tuple[int, int, str]]) -> str:
    """按 (起, 止, 替换内容) 从后往前拼接；重叠的区间直接丢弃后一个。"""
    out = content
    last_start = len(content) + 1
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        if end > last_start:
            # 与已应用的更靠后的编辑重叠：理论上不该发生（路径互不相同）
            continue
        out = out[:start] + repl + out[end:]
        last_start = start
    return out
