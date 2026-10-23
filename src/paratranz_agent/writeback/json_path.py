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
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .base import ApplyResult, WriteItem
from .locator import LocatorError, parse_locator

# 同一条译文被两条词条抢占同一段字面量时的原因文案。
# 与 `line.py` 用完全同一句话：两族写回器对「谁被跳过了」必须给出同样好读的解释。
OVERLAP_REASON = "与另一条译文的位置重叠，已跳过"

# 递归下降扫描器爆栈时给用户的话。嵌套 900 层的文件（事件表套事件表、插件配置
# 层层包 wrapper）真实存在，这种文件只能改用行号族提取器，JSON 路径族扫不动。
NESTED_TOO_DEEP = "文件嵌套过深，扫描器无法处理（建议改用行号族提取器）"

# 需要转义的 JSON 控制字符
_CTRL = {chr(i): "\\u%04x" % i for i in range(0x20)}
_CTRL.update({"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"})
_ESC_NEEDED = re.compile(r'["\\\x00-\x1f]')


def json_escape(text: str) -> str:
    """把译文转成 JSON 字符串字面量内容（不含两侧引号）。"""
    return _ESC_NEEDED.sub(lambda m: _CTRL.get(m.group(0), "\\" + m.group(0)), text)


class JsonScanError(ValueError):
    """原文不是合法 JSON（或被截断）。"""


# JSON 数字：标准形态之外的 `NaN` / `Infinity` / `-Infinity` 也要吃下去。
# 真实游戏文件里这两种写法很常见（JS 的 JSON.stringify 会把 NaN 原样写出来，
# 有些插件配置直接手写成 Infinity）。Python 的 json.loads 默认也接受它们 ——
# 扫描器如果只认标准数字，整个文件会被判成「不是合法 JSON」，一条都写不回去。
_NUMBER_RE = re.compile(r"-?(?:\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|Infinity)|NaN")

# 文件开头的 UTF-8 BOM：`read_text(encoding="utf-8")` 会把它留成 `\ufeff`，
# 不跳过就会在第一个值处报「非法值起始字符」。
_BOM = "\ufeff"


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
        m = _NUMBER_RE.match(self.s, self.i)
        if not m:
            raise self._err(f"非法值起始字符 {self.s[self.i]!r}")
        self.i = m.end()

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
    BOM 只在最开头被跳过 —— 位置偏移仍按原文计算，拼接时不会错位。
    """
    sc = _Scanner(text)
    if text.startswith(_BOM):
        sc.i = 1
    sc.value(())
    sc._ws()
    if sc.i != sc.n:
        raise JsonScanError(f"顶层值之后还有多余内容（位置 {sc.i}）")
    return sc.spans


@dataclass(frozen=True)
class _Edit:
    """一条待落地的替换。

    `seq` 是条目在调用参数里的顺序 —— 只有它能让 `written` 按入参顺序回报
    （重叠被挡掉的条目会从 `written` 里消失，位置不能靠追加顺序碰运气）。
    """

    seq: int
    key: str
    start: int
    end: int
    text: str


def apply_json(
    content: str,
    items: Sequence[WriteItem],
    *,
    opts: Mapping[str, str] | None = None,
) -> ApplyResult:
    """JSON 路径族写回：`[12].name`、`events[3].pages[0].list[2].parameters[0]`…

    `opts` 用不上（路径定位符自带全部信息），保留同一签名是为了让
    `apply.apply_items` 能一视同仁地分派。
    """
    del opts
    res = ApplyResult()
    try:
        spans = scan_strings(content)
    except JsonScanError as exc:
        # 整个文件解析不了：所有条目都记错误，绝不部分写入
        for it in items:
            res.errors.append((it.key, f"文件不是合法 JSON：{exc}"))
        res.content = content
        return res
    except RecursionError:
        # 递归下降扫描器受 Python 栈深限制（默认 1000 帧，一层嵌套吃两帧）。
        # 这是**单文件**的病，不是整轮回写的病：放它穿透出去会把 pipeline 的
        # 整轮循环炸掉，连已经写完的文件都没了报告。按「本文件一条都不写」处理。
        for it in items:
            res.errors.append((it.key, NESTED_TOO_DEEP))
        res.content = content
        return res

    edits: list[_Edit] = []
    # 已经是目标译文的条目：不用改文件，但确实算写成功，最后一起按入参顺序回报
    already: list[tuple[int, str]] = []
    for seq, it in enumerate(items):
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
            already.append((seq, it.key))
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
        edits.append(
            _Edit(seq=seq, key=it.key, start=start + 1, end=end - 1,
                  text=json_escape(it.translation))
        )

    res.content, accepted = _splice(content, edits, res)
    landed = already + [(e.seq, e.key) for e in accepted]
    res.written = [key for _, key in sorted(landed)]
    return res


def _short(text: str, limit: int = 40) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit] + "…"


def _splice(
    content: str, edits: list[_Edit], res: ApplyResult
) -> tuple[str, list[_Edit]]:
    """先按原坐标查重叠，再从后往前拼接；被重叠挡掉的那条记进 `res.errors`。

    返回 `(新内容, 真正落地的替换)`。

    **为什么必须查重叠**：同一个 key 在 entries.jsonl 里出现两次（合并导入、
    手工改过记录都会发生）时，两条定位符指向**同一个字面量**，区间必然重叠。
    早先这里遇到重叠直接 `continue` 扔掉后一条，可 `written` 在挑选阶段就记过了
    —— 报告说「写入 2 条」实际只落地 1 条，正好违反本子系统的第一原则
    （所有没写成的事实都必须出现在报告里）。现在与行号族 `line._splice` 完全一致：
    只认先到的一条，另一条从 `written` 里摘掉并记 errors。

    所有 `start`/`end` 都是**原始 content 的坐标**，所以从后往前拼一遍即可，
    前面改动多长都不影响后面。
    """
    if not edits:
        return content, []
    accepted: list[_Edit] = []
    cursor = 0  # 已接受区间的右端（原坐标）
    for edit in sorted(edits, key=lambda e: (e.start, e.end)):
        if edit.start < cursor:
            res.errors.append((edit.key, OVERLAP_REASON))
            continue
        accepted.append(edit)
        cursor = edit.end

    out = content
    for edit in sorted(accepted, key=lambda e: e.start, reverse=True):
        out = out[: edit.start] + edit.text + out[edit.end :]
    return out, accepted
