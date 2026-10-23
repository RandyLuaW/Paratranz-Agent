"""行号族写回器（`writeback/line.py`）的离线单测。

全部离线：纯函数，不联网、不写文件、不碰临时目录。
重点挡的是「错位写入」——key 认错位置却照样写进去，会直接毁掉游戏数据。
所以几乎每个用例都同时断言「写对了」和「别的行一个字符没动」。
"""
from __future__ import annotations

import json

import pytest

from paratranz_agent.writeback.apply import family_of_key
from paratranz_agent.writeback.base import WriteItem
from paratranz_agent.writeback.line import apply_line

# 纯文本文件：不是 .json，转义走「行内纯文本」分支
REL = "locale/zh.txt"
# 填充行：不含引号，绝不会被当成候选
FILLER = "# 填充行"


def key(locator: str, rel: str = REL) -> str:
    """拼一个平台风格的 key：`<相对路径>::<定位符>`。"""
    return f"{rel}::{locator}"


def doc(*lines: str) -> str:
    """把若干行拼成文件内容（每行以换行结尾，与提取器读到的文本一致）。"""
    return "".join(line + "\n" for line in lines)


def doc_with(targets: dict[int, str]) -> str:
    """造一份文本：`targets` 指定「第几行 = 什么内容」，其余行用填充行占位。

    行号由调用处给，测试里不在别处另写一份数字。
    """
    if not targets:
        return ""
    total = max(targets)
    return doc(*(targets.get(n, FILLER) for n in range(1, total + 1)))


def exact_quoted_pattern() -> str:
    """等价于提取器默认值 `"([^"]{2,})"` 的正则（显式写出，便于用例自述）。"""
    return '"([^"]{2,})"'


# --------------------------------------------------------------- 正常写回
def test_single_hit_single_line() -> None:
    content = doc('name = "Hello World"', 'title = "Second String"')
    items = [WriteItem(key=key("L1"), translation="你好世界", original="Hello World")]

    res = apply_line(content, items)

    assert res.written == [key("L1")]
    assert res.content == doc('name = "你好世界"', 'title = "Second String"')
    assert (res.mismatched, res.missing, res.errors, res.unsupported) == ([], [], [], [])


@pytest.mark.parametrize("reverse", [False, True], ids=["顺序", "倒序"])
def test_two_hits_on_same_line(reverse: bool) -> None:
    """同一行的 `L7` 与 `L7#1` 都要写对，且与条目顺序无关（从后往前拼接）。"""
    line_no = 7
    content = doc_with({line_no: 'a = "First"; b = "Second"'})
    items = [
        WriteItem(key=key(f"L{line_no}"), translation="第一", original="First"),
        WriteItem(key=key(f"L{line_no}#1"), translation="第二", original="Second"),
    ]
    if reverse:
        items.reverse()

    res = apply_line(content, items)

    assert sorted(res.written) == sorted([key(f"L{line_no}"), key(f"L{line_no}#1")])
    assert res.content == doc_with({line_no: 'a = "第一"; b = "第二"'})
    assert (res.mismatched, res.missing, res.errors) == ([], [], [])


def test_longer_translation_does_not_shift_later_edits() -> None:
    """前面某行的译文变长，只影响它自己；后面的条目仍按原始坐标落位。"""
    head, tail = 1, 9
    content = doc_with({head: 'head = "Hi"', tail: 'tail = "End"'})
    items = [
        WriteItem(key=key(f"L{head}"), translation="很长很长的一整句话都塞进来", original="Hi"),
        WriteItem(key=key(f"L{tail}"), translation="结束", original="End"),
    ]

    res = apply_line(content, items)

    assert res.written == [key(f"L{head}"), key(f"L{tail}")]
    assert res.content == doc_with(
        {head: 'head = "很长很长的一整句话都塞进来"', tail: 'tail = "结束"'}
    )


def test_crlf_line_endings_are_preserved() -> None:
    content = 'a = "Hello World"\r\nb = "Second String"\r\n'
    items = [WriteItem(key=key("L1"), translation="你好世界", original="Hello World")]

    res = apply_line(content, items)

    assert res.content == 'a = "你好世界"\r\nb = "Second String"\r\n'
    assert res.written == [key("L1")]


def test_many_edits_in_one_call_stay_in_place() -> None:
    """一次写很多条、译文长短差别很大：位置全按原始坐标算，谁也挤不动谁。"""
    targets = (1, 4, 9, 16)
    content = doc_with({n: f'k{n} = "Source {n}"' for n in targets})
    translations = {n: "译" * n for n in targets}  # 长度从 1 到 16，差异足够大
    items = [
        WriteItem(key=key(f"L{n}"), translation=translations[n], original=f"Source {n}")
        for n in targets
    ]

    res = apply_line(content, items)

    assert sorted(res.written) == sorted(key(f"L{n}") for n in targets)
    assert res.content == doc_with({n: f'k{n} = "{translations[n]}"' for n in targets})


def test_spaces_inside_quotes_are_kept() -> None:
    """提取器存的是 strip 后的原文，写回时只替换那一段，两侧空格留在原处。"""
    content = doc('a = "   Hello World   "')
    items = [WriteItem(key=key("L1"), translation="你好世界", original="Hello World")]

    res = apply_line(content, items)

    assert res.content == doc('a = "   你好世界   "')
    assert res.written == [key("L1")]


# --------------------------------------------------------------- 安全底线
def test_original_mismatch_writes_nothing() -> None:
    content = doc('name = "Hello World"')
    items = [WriteItem(key=key("L1"), translation="你好", original="Hallo Welt")]

    res = apply_line(content, items)

    assert res.written == []
    assert res.content == content
    assert [k for k, _ in res.mismatched] == [key("L1")]
    reasons = res.mismatched[0][1]
    assert "原文不一致" in reasons
    assert "Hello World" in reasons  # 原因里要写清文件里现在是什么


def test_empty_original_is_refused() -> None:
    """没有原文就无法校验位置，宁可不动。"""
    content = doc('a = "Hello World"')
    items = [WriteItem(key=key("L1"), translation="你好", original="")]

    res = apply_line(content, items)

    assert res.written == []
    assert res.content == content
    assert [k for k, _ in res.mismatched] == [key("L1")]


def test_missing_when_line_does_not_exist() -> None:
    content = doc_with({2: 'a = "One"'})
    absent = content.count("\n") + 5  # 文件里根本没有这一行
    items = [WriteItem(key=key(f"L{absent}"), translation="九", original="Nine")]

    res = apply_line(content, items)

    assert res.missing == [key(f"L{absent}")]
    assert res.written == []
    assert res.content == content


def test_missing_when_index_exceeds_hits_on_line() -> None:
    content = doc('a = "One"; b = "Two"')
    items = [WriteItem(key=key("L1#9"), translation="十", original="Ten")]

    res = apply_line(content, items)

    assert res.missing == [key("L1#9")]
    assert res.content == content


def test_duplicate_keys_do_not_double_apply() -> None:
    """同一个位置被两条译文抢：只认先到的，另一条报 error 而不是叠加。"""
    content = doc('a = "Hello World"')
    items = [
        WriteItem(key=key("L1"), translation="甲", original="Hello World"),
        WriteItem(key=key("L1"), translation="乙", original="Hello World"),
    ]

    res = apply_line(content, items)

    assert res.written == [key("L1")]
    assert res.content == doc('a = "甲"')
    assert [k for k, _ in res.errors] == [key("L1")]


def test_non_line_keys_are_unsupported() -> None:
    content = doc('a = "One"')
    items = [
        WriteItem(key=key("L1"), translation="一", original="One"),
        WriteItem(key="data/Actors.json::[12].name", translation="勇者", original="Hero"),
        WriteItem(key="data/Map001.json::L45.code401", translation="文本", original="Text"),
    ]

    res = apply_line(content, items)

    assert res.written == [key("L1")]
    assert res.unsupported == [
        "data/Actors.json::[12].name",
        "data/Map001.json::L45.code401",
    ]
    assert res.content == doc('a = "一"')


def test_mixed_outcomes_in_one_call() -> None:
    """一次调用里「写成功 / 原文不符 / 找不到」三种结局互不污染。"""
    content = doc_with({1: 'a = "One"', 3: 'c = "Three"'})
    items = [
        WriteItem(key=key("L1"), translation="一", original="One"),
        WriteItem(key=key("L3"), translation="三", original="Drei"),
        WriteItem(key=key("L8"), translation="八", original="Eight"),
    ]

    res = apply_line(content, items)

    assert res.written == [key("L1")]
    assert [k for k, _ in res.mismatched] == [key("L3")]
    assert res.missing == [key("L8")]
    assert res.content == doc_with({1: 'a = "一"', 3: 'c = "Three"'})


def test_idempotent_second_round_reports_mismatch() -> None:
    """同一批再写一次：原文已经是译文了，应当报 mismatched，不能重复写入。"""
    content = doc_with({3: 'a = "First"; b = "Second"'})
    items = [
        WriteItem(key=key("L3"), translation="第一", original="First"),
        WriteItem(key=key("L3#1"), translation="第二", original="Second"),
    ]
    first = apply_line(content, items)
    second = apply_line(first.content, items)

    assert sorted(first.written) == sorted([key("L3"), key("L3#1")])
    assert second.written == []
    assert sorted(k for k, _ in second.mismatched) == sorted([key("L3"), key("L3#1")])
    assert second.content == first.content


# --------------------------------------------------------------- 带 pattern 的路径
def test_pattern_and_group_are_honoured() -> None:
    """Ren'Py 那种 `e "台词"`：取第 2 分组，写回只换台词本体。"""
    content = doc('    e "Hello there"  # 注释不动')
    opts = {"pattern": r'^\s*(\w+)\s+"([^"]+)"', "group": "2", "encoding": "utf-8"}
    items = [WriteItem(key=key("L1"), translation="你好呀", original="Hello there")]

    res = apply_line(content, items, opts=opts)

    assert res.written == [key("L1")]
    assert res.content == doc('    e "你好呀"  # 注释不动')


def test_pattern_multiple_hits_use_index() -> None:
    content = doc('x = "One" + "Two" + "Three"')
    opts = {"pattern": exact_quoted_pattern(), "group": "1"}
    items = [
        WriteItem(key=key("L1"), translation="壹", original="One"),
        WriteItem(key=key("L1#2"), translation="叁", original="Three"),
    ]

    res = apply_line(content, items, opts=opts)

    assert sorted(res.written) == sorted([key("L1"), key("L1#2")])
    assert res.content == doc('x = "壹" + "Two" + "叁"')


def test_pattern_index_skips_hits_the_extractor_filtered() -> None:
    """提取器会丢掉短于 min_len / 不含字母的命中，序号里也不含它们。"""
    content = doc('x = "a" + "Long Enough"')  # 单字符命中会被 min_len 过滤掉
    opts = {"pattern": '"([^"]*)"', "group": "1", "min_len": "2"}
    items = [WriteItem(key=key("L1"), translation="够长了", original="Long Enough")]

    res = apply_line(content, items, opts=opts)

    assert res.written == [key("L1")]
    assert res.content == doc('x = "a" + "够长了"')


@pytest.mark.parametrize(
    "opts",
    [
        {"pattern": "(unclosed"},  # 正则本身不合法（右括号缺失）
        {"pattern": exact_quoted_pattern(), "group": "9"},  # 分组不存在
    ],
    ids=["bad-regex", "bad-group"],
)
def test_bad_extractor_opts_report_errors_and_write_nothing(opts: dict[str, str]) -> None:
    content = doc('a = "Hello World"')
    items = [WriteItem(key=key("L1"), translation="你好", original="Hello World")]

    res = apply_line(content, items, opts=opts)

    assert res.written == []
    assert res.content == content
    assert [k for k, _ in res.errors] == [key("L1")]


# --------------------------------------------- 与提取器的整份扫描保持一致（跨行配对）
def test_cross_line_hit_is_refused_not_written() -> None:
    """提取器正则会跨行配对（一行里引号数为奇数时），这种命中落不了地：只报错不动文件。

    第 1 行 `"x"` 太短配不上，于是闭合引号当开头、一路吃到第 2 行的 `"Randy"`，
    提取器因此会产出 `L1#1 original='$ name ='` 这种跨行词条。
    """
    content = doc('x = "short" + "x" + "Version 12"', '$ name = "Randy"')
    opts = {"pattern": exact_quoted_pattern(), "group": "1"}
    items = [WriteItem(key=key("L1#1"), translation="名字", original="$ name =")]

    res = apply_line(content, items, opts=opts)

    assert res.written == []
    assert res.content == content
    assert [k for k, _ in res.errors] == [key("L1#1")]
    assert "跨行" in res.errors[0][1]


def test_anchored_pattern_on_a_later_line_uses_local_scan() -> None:
    """带 `^` 锚点的按行正则：整份扫描只认文件开头，落在第 3 行就得靠逐行扫描。"""
    content = doc_with({3: '    e "Hello there"'})
    opts = {"pattern": r'^\s*(\w+)\s+"([^"]+)"', "group": "2"}
    items = [WriteItem(key=key("L3"), translation="你好呀", original="Hello there")]

    res = apply_line(content, items, opts=opts)

    assert res.written == [key("L3")]
    assert res.content == doc_with({3: '    e "你好呀"'})


# --------------------------------------------------------------- 无 pattern 的兜底
def test_fallback_uses_double_quoted_strings() -> None:
    content = doc('say "Hello World" now')
    items = [WriteItem(key=key("L1"), translation="你好世界", original="Hello World")]

    res = apply_line(content, items, opts={"encoding": "utf-8"})

    assert res.written == [key("L1")]
    assert res.content == doc('say "你好世界" now')


def test_fallback_uses_single_quoted_strings() -> None:
    content = doc("msg = 'Hello World'")
    items = [WriteItem(key=key("L1"), translation="你好世界", original="Hello World")]

    res = apply_line(content, items)

    assert res.written == [key("L1")]
    assert res.content == doc("msg = '你好世界'")


def test_fallback_counts_both_quote_styles_in_order() -> None:
    content = doc("x = \"One\" + 'Two'")
    items = [
        WriteItem(key=key("L1"), translation="壹", original="One"),
        WriteItem(key=key("L1#1"), translation="贰", original="Two"),
    ]

    res = apply_line(content, items)

    assert sorted(res.written) == sorted([key("L1"), key("L1#1")])
    assert res.content == doc("x = \"壹\" + '贰'")


def test_fallback_still_skips_too_short_strings() -> None:
    """兜底也照搬提取器默认的 min_len：单字符不占号，序号才和平台对得上。"""
    content = doc('x = "a" + "Long Enough"')
    items = [WriteItem(key=key("L1"), translation="够长了", original="Long Enough")]

    res = apply_line(content, items)

    assert res.written == [key("L1")]
    assert res.content == doc('x = "a" + "够长了"')


# --------------------------------------------------------------- 转义
def test_escape_quotes_inside_double_quoted_text() -> None:
    """原文处在双引号里，译文里的双引号必须转义，否则字符串会被截断。"""
    content = doc('say "Hello World"')
    items = [WriteItem(key=key("L1"), translation='他说"你好"', original="Hello World")]

    res = apply_line(content, items)

    assert res.content == doc('say "他说\\"你好\\""')
    assert res.written == [key("L1")]


def test_escape_backslash_and_newline_in_json_file() -> None:
    """`.json` 文件里反斜杠与真换行都要变成转义序列，结果必须是合法 JSON。"""
    content = '{\n  "k": "Hello World"\n}\n'
    rel = "locale/zh.json"
    translation = "C:\\存档\\第一行\n第二行"
    items = [WriteItem(key=key("L2", rel=rel), translation=translation, original="Hello World")]

    res = apply_line(content, items)

    assert res.written == [key("L2", rel=rel)]
    assert json.loads(res.content)["k"] == translation  # 转义可逆
    assert res.content.count("\n") == content.count("\n")  # 没漏出真换行


def test_plain_text_line_keeps_backslash_literal() -> None:
    """纯文本行内不做 JSON 转义：反斜杠原样写，别自作聪明加一层。"""
    content = doc("msg Hello World")
    opts = {"pattern": r"^msg (.+)$", "group": "1"}
    items = [WriteItem(key=key("L1"), translation="C:\\存档", original="Hello World")]

    res = apply_line(content, items, opts=opts)

    assert res.written == [key("L1")]
    assert res.content == doc("msg C:\\存档")


def test_newline_in_plain_text_line_is_an_error() -> None:
    """纯文本行内放不下换行：记 errors，原文一个字符都不动，绝不硬塞。"""
    content = doc("msg Hello World")
    opts = {"pattern": r"^msg (.+)$", "group": "1"}
    items = [WriteItem(key=key("L1"), translation="第一行\n第二行", original="Hello World")]

    res = apply_line(content, items, opts=opts)

    assert res.written == []
    assert res.content == content
    assert [k for k, _ in res.errors] == [key("L1")]
    assert "换行" in res.errors[0][1]


# --------------------------------------------------------------- can_handle 严格性
@pytest.mark.parametrize(
    "locator,expected",
    [
        ("L12", True),
        ("L12#3", True),
        ("L1#0", True),  # 0 基带后缀不常见，但写出来认得
        ("L12.code401", False),  # JSON 路径族，绝不能误吞
        ("[3].name", False),
        ("gameTitle", False),
        ("ev3p0[45]", False),
        ("L", False),
        ("#1", False),
        ("l12", False),
        ("L12#", False),
        ("L12x", False),
        ("", False),
    ],
)
def test_line_family_is_strict(locator: str, expected: bool) -> None:
    """行号族的判定必须严：JSON 路径族（`L12.code401`）绝不能被误吞。"""
    assert (family_of_key(f"f.txt::{locator}") == "line") is expected


def test_family_ignores_surrounding_whitespace() -> None:
    """key 拆出来的定位符可能带空格，剥掉后照常认。"""
    assert family_of_key("f.txt::  L12#2  ") == "line"
