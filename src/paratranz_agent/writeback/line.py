"""行号族写回器：按 `L<行>[#<序号>]` 把平台译文写回文本文件的原始位置。

与 `extractors/generic-regex/run.py` **严格互逆**，逐条对应关系：

| 提取器                                        | 本写回器                                    |
| --------------------------------------------- | ------------------------------------------- |
| `line = text.count("\n", 0, m.start()) + 1`    | 命中起点落在第几行，就归第几行              |
| `s = m.group(group).strip()` 存进 `original`   | 当前文本按同样规则 strip 后与 `original` 比对 |
| 同一行的命中按出现顺序编号：第 0 个 `L7`，之后 `#1#2…` | `#n` = 该行内第 n 个候选（0 基）     |
| `min_len` / `max_len` / 「至少含一个字母」过滤   | 候选序列按同样规则过滤，序号才对得上        |

**为什么必须知道提取器参数**：行号 + 行内序号只说明「第几行的第几个命中」，
不知道当时的正则就还原不出「命中」是什么。`opts` 就是提取器当时的 args
（`pattern` / `group` / `min_len` / `max_len`），由 pipeline 从提取记录里带过来。

**为什么整份文件扫一遍而不是逐行扫**：提取器的正则是 `re.finditer(整份文本)`，
引号配对可以跨行。把同一份正则只喂给单行，遇到跨行配对（例如一行里引号数是奇数）
匹配序列就会整体错位，`#n` 自然对不上。所以第一优先的解释是「整份扫描 + 按起点归行」，
逐行扫描只作为备用解释（带 `^` 锚点的按行正则需要它）。没有 `pattern` 时退化成
「行内所有引号串按出现顺序」，只对得上顺序，认不出别的过滤条件。

**四条候选来源都走得到，别当冗余删掉**：整份·严格、整份·放宽（原被长度/字母过滤
挡掉的短串）、逐行·按正则（`^` 锚点）、逐行·引号串兜底（无 pattern）。在现有测试集
上挂计数器跑过一遍，四条各命中 6 / 6 / 7 / 25 次，`_pick` 的三种结局（对上原文 /
只能给最可信解释 / 凑不出候选）也都出现过。对应断言分别在
`test_pattern_index_skips_hits_the_extractor_filtered`、
`test_anchored_pattern_on_a_later_line_uses_local_scan` 与三个 `test_fallback_*` 里。

写回语义的三条硬约束：

1. **绝不猜位置**。定位不到 → `missing`；定位到了但文件里当前的文本与 `original`
   不一致 → `mismatched`，一个字符都不写。游戏文件更新过之后错位写入会直接毁数据，
   这是整个功能的安全底线。同理，`original` 为空的条目无法校验，也拒绝写入。
2. **一次调用内所有位置都基于原始 content 计算**，最后从后往前拼接。
   否则先把第 3 行换成更长的译文，第 9 行的偏移就全错了。
3. **纯函数**：不读写文件、不打印、不联网。IO、备份、落盘都由 pipeline 负责。

定位符只认 `L<数字>` 与 `L<数字>#<数字>`；`L12.code401`（rpgmaker-mv 的 JSON
路径族）不在管辖范围内，会落进 `ApplyResult.unsupported`。
"""
from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .base import ApplyResult, WriteItem, split_key

# 行号族定位符：整串全匹配，`L12.code401` 这类 JSON 路径不会被误吞。
_LOCATOR_RE = re.compile(r"L(\d+)(?:#(\d+))?")

# 没有 pattern 时的兜底候选：行内所有双引号串或单引号串（各自不跨行）。
_QUOTED_RE = re.compile(r"\"([^\"\n]*)\"|'([^'\n]*)'")

# 提取器 `extractor.yaml` 里的默认值，opts 没给时沿用，保证序号算得一样。
_DEFAULT_GROUP = 1
_DEFAULT_MIN_LEN = 2
_DEFAULT_MAX_LEN = 5000

# JSON 字符串里需要转义的字符（其余控制字符走 \u00xx）
_JSON_ESCAPE = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


class _Unrepresentable(ValueError):
    """译文无法在目标位置正确表示（例如纯文本行内出现换行）。"""


@dataclass(frozen=True)
class _Hit:
    """一个候选命中；`start`/`end` 一律是整份 content 的绝对坐标。"""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Edit:
    """一条待落地的替换。"""

    seq: int  # 条目在调用参数里的顺序，用于按原顺序回报 written
    key: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Outcome:
    """单条词条的定位结果，四种结局互斥。"""

    edit: _Edit | None = None
    missing: bool = False
    mismatch: str = ""
    error: str = ""


@dataclass(frozen=True)
class _Settings:
    """提取器参数，决定「行内第 n 个命中」到底是哪一个。"""

    rx: re.Pattern[str] | None = None
    group: int = _DEFAULT_GROUP
    min_len: int = _DEFAULT_MIN_LEN
    max_len: int = _DEFAULT_MAX_LEN

    @classmethod
    def from_opts(cls, opts: Mapping[str, str] | None) -> _Settings:
        """按提取器同样的默认值与过滤规则装配；正则/分组不合法抛 `ValueError`。"""
        group = _opt_int(opts, "group", _DEFAULT_GROUP)
        min_len = _opt_int(opts, "min_len", _DEFAULT_MIN_LEN)
        max_len = _opt_int(opts, "max_len", _DEFAULT_MAX_LEN)
        pattern = _opt_str(opts, "pattern")
        if pattern is None:
            return cls(rx=None, group=group, min_len=min_len, max_len=max_len)
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"提取器正则无法编译，定位不了行内命中：{exc}") from exc
        if not 0 <= group <= rx.groups:
            raise ValueError(f"group 参数越界：group={group}，该正则只有 {rx.groups} 个分组")
        return cls(rx=rx, group=group, min_len=min_len, max_len=max_len)

    # ------------------------------------------------------- 整份扫描（严格互逆）
    def whole_catalogs(
        self, content: str, spans: Sequence[tuple[int, int]]
    ) -> list[dict[int, list[_Hit]]]:
        """`re.finditer(整份内容)`，命中按起点所在行归档；返回 [严格, 放宽] 两份。

        严格那份照搬提取器的三道过滤（长度区间 + 至少一个字母），序号才和平台一致。
        """
        if self.rx is None:
            return []
        starts = [start for start, _ in spans]
        catalogs: list[dict[int, list[_Hit]]] = []
        for filtered in (True, False):
            buckets: dict[int, list[_Hit]] = {}
            for anchor, hit in self._iter_hits(self.rx, content, filtered=filtered):
                line_no = bisect_right(starts, anchor)  # 1 基行号
                buckets.setdefault(line_no, []).append(hit)
            catalogs.append(buckets)
        return catalogs

    # --------------------------------------------------------- 逐行扫描（备用解释）
    def local_catalogs(self, line_text: str, offset: int) -> list[list[_Hit]]:
        """只扫这一行；`offset` 是这行在整份 content 里的起点。

        带 `^` 锚点的按行正则需要这条路径（提取器本身用整份文本，那种 key 其实提不出来，
        但手工造的 key 可能这样写）。没有 pattern 时用引号串兜底。
        """
        if self.rx is not None:
            strict = [
                h for _, h in self._iter_hits(self.rx, line_text, filtered=True, offset=offset)
            ]
            loose = [
                h for _, h in self._iter_hits(self.rx, line_text, filtered=False, offset=offset)
            ]
        else:
            loose = [
                _Hit(offset + h.start, offset + h.end, h.text)
                for h in _by_quoted_strings(line_text)
            ]
            strict = [h for h in loose if self._passes(h.text)]
        return [strict, loose] if loose != strict else [strict]

    def _iter_hits(
        self, rx: re.Pattern[str], text: str, *, filtered: bool, offset: int = 0
    ) -> list[tuple[int, _Hit]]:
        """按提取器同样的取组与过滤规则产出 `(命中起点, 候选)`。

        整份扫描与逐行扫描共用这一份实现 —— 两边的取组/过滤规则但凡有一点不一致，
        `#n` 的序号就会整体错位，所以规则只能写一遍。
        """
        hits: list[tuple[int, _Hit]] = []
        for m in rx.finditer(text):
            raw = m.group(self.group)
            if not isinstance(raw, str):  # 可选分组没参与匹配，提取器也会跳过
                continue
            if filtered and not self._passes(raw):
                continue
            start, end = m.span(self.group)
            hits.append((offset + m.start(), _Hit(offset + start, offset + end, raw)))
        return hits

    def _passes(self, value: str) -> bool:
        """提取器的三道过滤：strip 后的长度区间 + 至少一个字母。"""
        text = value.strip()
        return self.min_len <= len(text) <= self.max_len and any(ch.isalpha() for ch in text)


class _Catalog:
    """一次 `apply` 调用里的候选来源：整份文件最多扫一遍（真需要时才扫），按行取用。"""

    def __init__(self, settings: _Settings, content: str, spans: Sequence[tuple[int, int]]) -> None:
        self._settings = settings
        self._content = content
        self._spans = spans
        self._whole: list[dict[int, list[_Hit]]] | None = None
        self._local: dict[int, list[list[_Hit]]] = {}

    def for_line(self, line_no: int) -> list[list[_Hit]]:
        """该行可用的候选序列，按可信度排序：整份扫描在前，逐行扫描在后。"""
        if self._whole is None:
            self._whole = self._settings.whole_catalogs(self._content, self._spans)
        local = self._local.get(line_no)
        if local is None:
            start, end = self._spans[line_no - 1]
            local = self._settings.local_catalogs(self._content[start:end], start)
            self._local[line_no] = local
        return [bucket.get(line_no, []) for bucket in self._whole] + local


def _opt_str(opts: Mapping[str, str] | None, name: str) -> str | None:
    if not opts:
        return None
    raw = opts.get(name)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _opt_int(opts: Mapping[str, str] | None, name: str, default: int) -> int:
    """取整数型参数；取不到或不是整数就沿用默认值（只影响候选挑选，仍会校验原文）。"""
    raw = _opt_str(opts, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _line_spans(content: str) -> list[tuple[int, int]]:
    """按换行符切行，返回每行的 `[start, end)`。

    与提取器的 `text.count("\\n", 0, m.start()) + 1` 完全等价：第 k 行（1 基）
    从第 k-1 个换行符之后开始。行尾的 `\\r`（CRLF 文件）留在行内不剥掉，
    这样替换偏移与原文件逐字对齐。
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for part in content.split("\n"):
        end = start + len(part)
        spans.append((start, end))
        start = end + 1
    return spans


def _by_quoted_strings(line_text: str) -> list[_Hit]:
    """兜底候选：行内所有 `"…"` 与 `'…'`，按出现顺序排列（行内坐标）。"""
    hits: list[_Hit] = []
    for m in _QUOTED_RE.finditer(line_text):
        which = 1 if m.group(1) is not None else 2
        start, end = m.span(which)
        hits.append(_Hit(start, end, m.group(which)))
    return hits


def _escape(text: str, *, json_mode: bool) -> str:
    """把译文转成目标位置能表示的写法。

    `json_mode`（`.json` 文件，或原文本来就被双引号包着）按 JSON 字符串字面量转义：
    反斜杠、双引号、真换行等都要变成转义序列，否则会把引号/结构打断。
    纯文本行内没有换行的表示法 —— 遇到换行宁可报错也不硬塞。
    """
    if json_mode:
        out: list[str] = []
        for ch in text:
            escaped = _JSON_ESCAPE.get(ch)
            if escaped is not None:
                out.append(escaped)
            elif ch < " ":  # 其余控制字符转成 \u00xx，保证仍是合法 JSON
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        return "".join(out)
    if "\n" in text or "\r" in text:
        raise _Unrepresentable("译文含换行，纯文本行内无法表示，请改成单行后重试")
    return text


def apply_line(
    content: str,
    items: Sequence[WriteItem],
    *,
    opts: Mapping[str, str] | None = None,
) -> ApplyResult:
    """行号族（`L<行>` / `L<行>#<序号>`）写回。

    `opts` 是**提取器当时的参数**：行号定位符只知道「第几行第几个命中」，
    不知道当初用什么 pattern/group 提取就还原不出原文，必须靠它。

    把 `items` 里的译文写回 `content`，返回新内容与逐条结果（不碰文件系统）。
    """
    result = ApplyResult(content=content)
    settings, settings_error = _Settings(), ""
    try:
        settings = _Settings.from_opts(opts)
    except ValueError as exc:
        # 参数坏了就一条都定位不了：逐条记 errors，绝不瞎猜位置
        settings_error = str(exc)

    spans = _line_spans(content)
    catalog = _Catalog(settings, content, spans)
    edits: list[_Edit] = []
    for seq, item in enumerate(items):
        rel, locator = split_key(item.key)
        m = _LOCATOR_RE.fullmatch(locator)
        if m is None:
            result.unsupported.append(item.key)
            continue
        if settings_error:
            result.errors.append((item.key, settings_error))
            continue
        outcome = _locate(
            content,
            spans,
            catalog,
            rel=rel,
            key=item.key,
            seq=seq,
            line_no=int(m.group(1)),
            index=int(m.group(2) or 0),
            original=item.original,
            translation=item.translation,
        )
        if outcome.edit is not None:
            edits.append(outcome.edit)
        elif outcome.missing:
            result.missing.append(item.key)
        elif outcome.mismatch:
            result.mismatched.append((item.key, outcome.mismatch))
        else:
            result.errors.append((item.key, outcome.error))

    result.content, accepted = _splice(content, edits, result)
    result.written = [e.key for e in sorted(accepted, key=lambda e: e.seq)]
    return result


def _locate(
    content: str,
    spans: Sequence[tuple[int, int]],
    catalog: _Catalog,
    *,
    rel: str,
    key: str,
    seq: int,
    line_no: int,
    index: int,
    original: str,
    translation: str,
) -> _Outcome:
    """在原文里定位这一条要替换的片段；四种结局：写 / 缺 / 不符 / 报错。"""
    if line_no < 1 or line_no > len(spans):
        return _Outcome(missing=True)  # 文件里根本没这一行（提取后又改过）
    expected = original.strip()
    if not expected:
        # 没原文就无法确认「文件里这段还是不是当初那句」，宁可不动
        return _Outcome(mismatch="平台未提供原文，无法校验当前位置，拒绝写入")

    picked = _pick(catalog.for_line(line_no), index, expected)
    if picked is None:
        return _Outcome(missing=True)  # 这一行凑不出第 index 个命中
    hit, matched = picked
    if not matched:
        return _Outcome(mismatch=f"原文不一致：文件里是 {hit.text.strip()!r}")
    if "\n" in hit.text or "\r" in hit.text:
        # 提取器的正则能跨行配对（引号数为奇数时），这种命中落不了地：
        # 替换它会把换行/缩进一起吃掉，行号写回器不干这种事
        return _Outcome(
            error="命中的原文跨行，行号型写回无法表示（建议修正提取器的 pattern）"
        )

    # 只替换 strip 之后的那一段，保住原文件里的前导缩进与尾随空格
    lead = len(hit.text) - len(hit.text.lstrip())
    tail = len(hit.text) - len(hit.text.rstrip())
    start, end = hit.start + lead, hit.end - tail
    json_mode = rel.lower().endswith(".json") or _wrapped_in_double_quotes(content, start, end)
    try:
        text = _escape(translation, json_mode=json_mode)
    except _Unrepresentable as exc:
        return _Outcome(error=str(exc))
    return _Outcome(edit=_Edit(seq=seq, key=key, start=start, end=end, text=text))


def _pick(
    catalog: Sequence[Sequence[_Hit]], index: int, expected: str
) -> tuple[_Hit, bool] | None:
    """在第 index 位上挑候选：优先能对上 `expected` 的那种解释。

    返回 `(候选, 是否对上原文)`；所有解释都凑不出第 index 个候选时返回 `None`（→ missing）。
    对不上时返回**最可信解释**下的那个候选，用来写清「文件里现在是什么」。
    """
    fallback: _Hit | None = None
    for candidates in catalog:
        if index >= len(candidates):
            continue
        hit = candidates[index]
        if fallback is None:
            fallback = hit
        if hit.text.strip() == expected:
            return hit, True
    return None if fallback is None else (fallback, False)


def _wrapped_in_double_quotes(content: str, start: int, end: int) -> bool:
    """这段文本两侧紧贴着双引号吗（决定要不要按 JSON 字符串转义）。"""
    return (
        start > 0
        and end < len(content)
        and content[start - 1] == '"'
        and content[end] == '"'
    )


def _splice(content: str, edits: list[_Edit], result: ApplyResult) -> tuple[str, list[_Edit]]:
    """先按原坐标查重叠，再从后往前拼接。

    返回 `(新内容, 真正落地的替换列表)`；被重叠挡掉的那条记进 `result.errors`。

    所有 `start`/`end` 都是**原始 content 的坐标**，所以从后往前拼一遍即可，前面改动
    多长都不影响后面。这里攒片段再一次性 `join`（而不是循环里反复切片拼接）：
    大文件 + 上千条目时后者是 O(文件 × 条目) 的字符串拷贝，会慢到不可用。
    """
    if not edits:
        return content, []
    accepted: list[_Edit] = []
    cursor = 0  # 已占用区间的右端（原坐标）
    for edit in sorted(edits, key=lambda e: (e.start, e.end)):
        if edit.start < cursor:
            # 两条译文抢同一段文本：只认先到的那条，另一条报错让人看明白
            result.errors.append((edit.key, "与另一条译文的位置重叠，已跳过"))
            continue
        accepted.append(edit)
        cursor = edit.end

    pieces: list[str] = []
    edge = len(content)
    for edit in sorted(accepted, key=lambda e: e.start, reverse=True):
        pieces.append(content[edit.end : edge])
        pieces.append(edit.text)
        edge = edit.start
    pieces.append(content[:edge])
    return "".join(reversed(pieces)), accepted
