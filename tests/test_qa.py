"""确定性 QA 规则的单元测试。"""
from __future__ import annotations

from paratranz_agent.qa import Severity, check_placeholders, run_qa


def test_placeholder_ok():
    assert check_placeholders("Hello \\N[1], you have \\V[2] coins.", "你好 \\N[1]，你有 \\V[2] 枚金币。") == []


def test_placeholder_missing_is_error():
    issues = check_placeholders("You got \\V[2] coins.", "你获得了金币。")
    assert issues and issues[0].severity is Severity.ERROR
    assert "\\V[2]" in issues[0].message


def test_placeholder_extra_is_error():
    issues = check_placeholders("Open the door.", "打开门 \\C[1]。")
    assert issues and issues[0].severity is Severity.ERROR


def test_generic_placeholder():
    assert check_placeholders("Hi {0}, %s left.", "嗨 {0}，%s 离开了。") == []
    assert check_placeholders("Hi {0}.", "嗨。")


def test_identical_and_empty_blocked():
    rep = run_qa("Hello world", "Hello world", block_on=Severity.ERROR)
    assert not rep.ok
    assert any(i.rule == "identical" for i in rep.issues)

    rep2 = run_qa("Hello world", "   ", block_on=Severity.ERROR)
    assert not rep2.ok
    assert any(i.rule == "empty" for i in rep2.issues)


def test_number_lost():
    rep = run_qa("Deals 25 damage.", "造成伤害。", block_on=Severity.ERROR)
    assert not rep.ok
    assert any(i.rule == "number_lost" for i in rep.issues)


def test_term_violation_is_warn_only():
    terms = {"farm": {"translation": "农场", "variants": ["farms"]}}
    rep = run_qa("Welcome to the farm.", "欢迎来到农庄。", terms=terms, block_on=Severity.ERROR)
    assert rep.ok  # warn 不阻断
    assert any(i.rule == "term_violation" for i in rep.issues)


def test_brace_unbalanced():
    rep = run_qa("Value {0} here.", "这里的值 {0 。", block_on=Severity.ERROR)
    assert any(i.rule == "brace_unbalanced" for i in rep.issues)


def test_punctuation_info():
    rep = run_qa("Hello there, friend.", "你好,朋友...", block_on=Severity.ERROR)
    rules = {i.rule for i in rep.issues}
    assert "punct_style" in rules


def test_clean_translation_passes():
    rep = run_qa(
        "The crops are ready to harvest.",
        "作物已经可以收获了。",
        terms={"farm": {"translation": "农场"}},
        block_on=Severity.ERROR,
    )
    assert rep.ok


# ------------- 真实项目（9087 条）跑出来的四类误报，逐一钉死 -------------
# 这四条都是「正确译文被判打回」，修复前全项目硬拦率 24.9%，修复后 8.7%。


def test_twee_var_is_ascii_bounded():
    r"""误报①：`\w` 吃掉了 CJK，`_looks很急切` 被当成一个变量名。

    原文 `<<He>> _looks eager.` / 译文 `<<He>>_looks很急切。`
    → 曾判「丢失 `_looks` + 多出 `_looks很急切`」，228 条栽在这里。
    """
    assert check_placeholders("<<He>> _looks eager.", "<<He>>_looks很急切。") == []


def test_twee_var_requires_word_boundary():
    """误报②：`_name_cap` 是 `cn_name_cap` 的子串，不是独立临时变量。

    原文 `$worn.handheld.name` / 译文 `$worn.handheld.cn_name_cap`（项目自建的中文名补丁变量）
    → 曾判「多出 `_name_cap`」，117 条。
    """
    assert check_placeholders(
        "Your $worn.handheld.name breaks.", "你的 $worn.handheld.cn_name_cap 损坏了。"
    ) == []


def test_twee_var_after_cjk_is_still_a_token():
    """误报③：边界判定**只能按 ASCII**，否则 `你的_hand` 会判成丢失 `_hand`。

    这是修复上一个误报时差点写错的回归：把 `\\w` 写进 lookbehind，
    Python 会把中文也算作前导字符，全项目 `_hand` / `_arm` / `_repairCount`
    这些紧贴中文的变量就全部被误判成丢失。真实抽样 12 条里 12 条中招。
    """
    assert check_placeholders("smack with your _hand.", "用你的_hand拍打。") == []
    assert check_placeholders("need _repairCount to fix.", "需要_repairCount 个才能修复。") == []


def test_brace_block_with_translated_template_literal():
    r"""误报③：`{ ... }` 里的模板字面量本来就该翻译，不能整块逐字比对。

    原文 ``{ _textarray.push(`<<He>> speaks. "Your ... is so warm."`) }``
    → 曾判「丢失整块 + 多出整块」，1413 条 extra_generic 里大半是它。
    """
    src = 'if (V.a >= 1){ _textarray.push(`<<He>> speaks. "Your pussy is so warm."`) };'
    dst = 'if (V.a >= 1){ _textarray.push(`<<He>> 说："你的小穴感觉好温暖。"`) };'
    assert check_placeholders(src, dst) == []


def test_brace_block_still_catches_dropped_code():
    """反向保护：容器归一化不能把「整块代码丢了」也放过。"""
    issues = check_placeholders('{ _textarray.push(`Hello there.`) }', "你好。")
    assert issues, "整块丢失必须报错"


def test_twee_link_keeps_passage_name_only():
    """误报④：`[[显示文本|Passage]]` 里可译的是显示文本，Passage 名不可变。"""
    assert check_placeholders("<<link [[Add Sirris|$passage]]>>", "<<link [[添加 西里斯|$passage]]>>") == []
    # Passage 名被翻掉了 → 必须报
    assert check_placeholders("<<link [[Add Sirris|$passage]]>>", "<<link [[添加 西里斯|$路径]]>>")


def test_brace_balance_is_relative_to_source():
    r"""误报⑤：多行 JS 拆成词条后，片段天生只有一个 `{`。

    `{name: "natural",` → 译成 `{name: "自然",` 本是正确译文，
    以前用「译文自身是否配平」判定，113 条全误报。
    """
    rep = run_qa('{name: "natural",', '{name: "自然",', block_on=Severity.ERROR)
    assert not any(i.rule == "brace_unbalanced" for i in rep.issues), "片段继承原文失衡不算错"
    # 译文自己丢掉一个 } 仍要抓
    rep2 = run_qa("Value {0} here.", "这里的值 {0 在这里。", block_on=Severity.ERROR)
    assert any(i.rule == "brace_unbalanced" for i in rep2.issues)


def test_code_line_identical_is_not_blocking():
    """误报⑥：代码行的「译文=原文」是正确状态，不该按漏翻打回。

    `delay += 1500;` / `taker: "pc",` 这类共 229 条。降为 WARN 交给模型二审，
    真句子保持一致仍是 ERROR。
    """
    rep = run_qa("delay += 1500;", "delay += 1500;", block_on=Severity.ERROR)
    assert rep.ok, "代码行不该被硬拦"
    assert any(i.rule == "identical" and i.severity is Severity.WARN for i in rep.issues)

    rep2 = run_qa("You cannot open the door now.", "You cannot open the door now.", block_on=Severity.ERROR)
    assert not rep2.ok, "英文句子保持原样必须打回"
    assert any(i.rule == "identical" and i.severity is Severity.ERROR for i in rep2.issues)


def test_no_cjk_skips_variable_only_templates():
    """误报⑦：`NNPC #<<print _i>>: _nnpc.nam` 这类模板行没有可译自然语言。"""
    rep = run_qa(
        'NNPC #<<print _i>>: <span class="green">_nnpc.nam</span>',
        'NNPC #<<print _i>>:<span class="green">_nnpc.nam</span>',
        block_on=Severity.ERROR,
    )
    assert not any(i.rule == "no_cjk" for i in rep.issues), "无可译内容的模板行不该判漏翻"
    # 真英文句子没翻译成中文仍要报
    rep2 = run_qa("This door is locked tight.", "This door is locked tight.", block_on=Severity.ERROR)
    assert not rep2.ok
