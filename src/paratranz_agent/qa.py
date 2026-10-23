"""确定性 QA：不依赖模型的可判定规则。

这些规则是"自动校对"的地基——模型二审之前，先把机器能判死的错误拦住。
规则集针对游戏文本（RPG Maker MV / Ren'Py / Unity 常见形态）设计，
每条规则返回 Severity + 定位 + 建议，便于回写 stage=2（有疑问）并留评论。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from functools import lru_cache


class Severity(IntEnum):
    INFO = 0
    WARN = 1
    ERROR = 2

    @classmethod
    def parse(cls, name: str) -> "Severity":
        return {"info": cls.INFO, "warn": cls.WARN, "error": cls.ERROR}.get(name.lower(), cls.ERROR)


@dataclass
class Issue:
    rule: str
    severity: Severity
    message: str
    hint: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": int(self.severity),
            "severity_name": self.severity.name.lower(),
            "message": self.message,
            "hint": self.hint,
        }


@dataclass
class QAReport:
    ok: bool = True
    issues: list[Issue] = field(default_factory=list)
    max_severity: Severity = Severity.INFO

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)
        self.max_severity = max(self.max_severity, issue.severity)

    def finalize(self, block_on: Severity) -> "QAReport":
        self.ok = self.max_severity < block_on
        return self


# ---------------------------------------------------------------- 占位符/控制符
# RPG Maker MV 控制符: \N[1] \V[1] \C[2] \I[3] \FS[20] \PX[10] \. \| \! \{ \} \\ 
RM_TOKENS = re.compile(r"\\[A-Za-z]+\[\d+\]|\\[.|!|{|}|\\<>]|\\\\")
# 通用占位符: {0} {name} %s %d %1$s ${x} {{x}} <b> [tag]
GENERIC_TOKENS = re.compile(
    # 标签内**不允许引号**：`<b>`、`<color=red>` 要保护，
    # 但 Twee/SugarCube 的 `<link "Back">` 里引号内是可译文本，不能整体当占位符
    r"\{\{?[^{}]*\}?\}|\$\{[^}]*\}|%[\d$]*[sdfioxX%]|%\{\w+\}"
    r"|<[^<>\"']{1,20}>"
    # `[[…]]` 必须排在单层方括号之前：正则按「最左 + 分支先后」匹配，
    # 顺序反了会把 `[[显示文本|Passage]]` 拆成两个单层标签，译文一改动就误报。
    r"|\[\[[^\]]+\]\]"
    # 单层方括号标签（Unity 富文本 / Yarn / Ink 的 `[wave]`、`[/wave]`）：
    # 注释里一直写着要保护 `[tag]`，但正则只写了 `[[…]]`，于是译文丢了标签也不报。
    # 不许跨行：真正的标签不会断行，允许换行只会把正文当成标签吞掉。
    r"|\[[^\[\]\n]{1,40}\]"
)
# SugarCube/Twee 的变量与宏名：可译文本之外必须原样保留。
# 三个坑都在真实项目里踩到了：
# ① 标识符必须按 ASCII 界定 —— Python 的 `\w` 匹配 CJK，`<<He>>_looks很急切`
#    会被整体当成 token `_looks很急切`（228 条误报）；
# ② `_x` 临时变量必须是**独立词** —— 否则 `$worn.handheld.cn_name_cap` 里的
#    `_name_cap` 会被当成多出来的临时变量（117 条误报）；
# ③ 但「独立」只能按 **ASCII** 判定 —— 用 `\w` 会把中文当成前导字符，
#    `你的_hand` 里的 `_hand` 就被判成丢失（这一条是反例，差点写错）。
TWEE_VAR = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*|(?<![A-Za-z0-9_.])_[A-Za-z][A-Za-z0-9_]*")
COLOR_TAGS = re.compile(r"</?[a-zA-Z][^<>]{0,30}>")

# `{...}` / `[[...]]` 这类容器里的**可译文本**要先抹掉再比对骨架：
# TweeScript 的 `{ _textarray.push(`台词`) }` 和 Twee 链接 `[[显示文本|Passage]]`
# 里本来就该翻译，逐字比对会把正确译文判成「占位符丢失 + 多出」。
_TEMPLATE_LIT = re.compile(r"`(?:[^`\\]|\\.)*`")
_DQUOTED = re.compile(r'"[^"]*"')
_SQUOTED = re.compile(r"'[^']*'")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+")


def _norm_token(tok: str) -> str:
    """把容器 token 里的可译内容抹平，只留下必须原样保留的骨架。"""
    if tok.startswith("{") and tok.endswith("}"):
        t = _TEMPLATE_LIT.sub("`…`", tok)
        t = _DQUOTED.sub('"…"', t)
        t = _SQUOTED.sub("'…'", t)
        return _CJK_RUN.sub("…", t)
    if tok.startswith("[[") and tok.endswith("]]"):
        inner = tok[2:-2]
        if "|" in inner:  # [[显示文本|Passage]]：只有 Passage 名不可变
            left, right = inner.split("|", 1)
            return f"[[…|{right}]]"
        return tok
    return tok

ASCII_WORD = re.compile(r"[A-Za-z]{2,}")
# 「像自然语言」的判定：至少有两个相邻的小写单词。
# 用来把 `delay += 1500;` / `taker: "pc",` 这类**本来就不该翻译**的代码行
# 与真正漏翻的英文句子区分开（项目实测：229 条 identical 全是代码行）。
PROSE_RUN = re.compile(r"[a-z]{2,}\s+[a-z]{2,}")
CJK = re.compile(r"[\u4e00-\u9fff]")
BAD_PUNCT = [
    (re.compile(r"\.\.\."), "英文省略号 ..."),
    (re.compile(r"(?<=[\u4e00-\u9fff]),"), "中文后接半角逗号"),
    (re.compile(r"(?<=[\u4e00-\u9fff])\."), "中文后接半角句号"),
    (re.compile(r"(?<=[\u4e00-\u9fff])!"), "中文后接半角叹号"),
    (re.compile(r"(?<=[\u4e00-\u9fff])\?"), "中文后接半角问号"),
    (re.compile(r"(?<=[\u4e00-\u9fff]):"), "中文后接半角冒号"),
    (re.compile(r"(?<=[\u4e00-\u9fff]);"), "中文后接半角分号"),
]


def _token_multiset(pattern: re.Pattern[str], text: str, *, norm=None) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in pattern.findall(text):
        key = norm(m) if norm else m
        out[key] = out.get(key, 0) + 1
    return out


def check_placeholders(src: str, dst: str) -> list[Issue]:
    issues: list[Issue] = []
    for name, pat, norm in (
        ("rm", RM_TOKENS, None),
        ("generic", GENERIC_TOKENS, _norm_token),
        ("twee", TWEE_VAR, None),
    ):
        a = _token_multiset(pat, src, norm=norm)
        b = _token_multiset(pat, dst, norm=norm)
        missing = {k: v for k, v in a.items() if b.get(k, 0) < v}
        extra = {k: v for k, v in b.items() if a.get(k, 0) < v}
        if missing:
            issues.append(Issue(
                f"placeholder_missing_{name}", Severity.ERROR,
                f"译文丢失占位符/控制符: {', '.join(sorted(missing))}",
                "占位符必须与原文逐一对应，顺序可调整但不得增删",
            ))
        if extra:
            issues.append(Issue(
                f"placeholder_extra_{name}", Severity.ERROR,
                f"译文多出原文没有的占位符/控制符: {', '.join(sorted(extra))}",
                "检查是否误用了 \\ 转义或残留标签",
            ))
    return issues


def check_whitespace(src: str, dst: str) -> list[Issue]:
    issues: list[Issue] = []
    if src != src.strip() and dst == dst.strip() and dst:
        issues.append(Issue("edge_space_missing", Severity.WARN,
                            "原文首尾有空白，译文没有", "某些引擎依赖首尾空格排版"))
    if "\n" in src and "\n" not in dst:
        issues.append(Issue("newline_lost", Severity.WARN,
                            f"原文含 {src.count(chr(10))} 个换行，译文没有换行",
                            "保留换行以避免 UI 溢出"))
    return issues


def check_numbers(src: str, dst: str) -> list[Issue]:
    issues: list[Issue] = []
    a = re.findall(r"\d+(?:\.\d+)?", src)
    b = re.findall(r"\d+(?:\.\d+)?", dst)
    if a and not b:
        issues.append(Issue("number_lost", Severity.ERROR,
                            f"原文数字 {a} 在译文中全部消失", "数值必须保留"))
    elif a and sorted(a) != sorted(b):
        missing = [x for x in a if x not in b]
        if missing:
            issues.append(Issue("number_mismatch", Severity.WARN,
                                f"数字不一致，原文有而译文缺: {missing}", "确认是否需要换算"))
    return issues


def _prose_text(src: str) -> str:
    """剥掉标记/占位符/变量，只留下真正的自然语言。

    `prose` 判定必须在这上面做：`<span class="green">_nnpc.nam` 里的
    `span class` 会被误当成两个小写单词，把代码行判成英文句子。
    """
    t = COLOR_TAGS.sub(" ", src)
    t = GENERIC_TOKENS.sub(" ", t)
    t = TWEE_VAR.sub(" ", t)
    return RM_TOKENS.sub(" ", t)


def check_untranslated(src: str, dst: str, *, ascii_ratio_limit: float = 0.55) -> list[Issue]:
    issues: list[Issue] = []
    if not dst.strip():
        issues.append(Issue("empty", Severity.ERROR, "译文为空", "未翻译"))
        return issues
    prose = bool(PROSE_RUN.search(_prose_text(src)))
    if dst.strip() == src.strip():
        # 原文是代码行/专有名词时「译文=原文」是正确状态（该锁定而不是打回），
        # 降成 WARN 交给模型二审判断；真句子保持一致仍按 ERROR 打回。
        issues.append(Issue(
            "identical",
            Severity.ERROR if prose else Severity.WARN,
            "译文与原文完全相同",
            "疑似漏翻；若这里是代码/专有名词，应锁定该词条而不是留作已翻译",
        ))
        return issues
    if not CJK.search(dst):
        # 纯符号/数字/变量的原文不需要中文：`NNPC #<<print _i>>: _nnpc.nam`
        # 这类模板行没有可译自然语言，判「疑似漏翻」是误报。
        if prose:
            issues.append(Issue("no_cjk", Severity.ERROR, "译文不含任何中文", "疑似漏翻或只改了标点"))
        return issues
    ascii_chars = len(re.findall(r"[A-Za-z]", dst))
    ratio = ascii_chars / max(1, len(dst))
    if ratio > ascii_ratio_limit:
        words = ASCII_WORD.findall(dst)
        issues.append(Issue("too_much_ascii", Severity.WARN,
                            f"译文 ASCII 字母占比 {ratio:.0%}，残留英文词: {words[:6]}",
                            "确认是否为专有名词；否则应译出"))
    return issues


def check_length(src: str, dst: str, *, limit: float = 3.5) -> list[Issue]:
    issues: list[Issue] = []
    if len(src) < 8:
        return issues
    ratio = len(dst) / max(1, len(src))
    if ratio > limit:
        issues.append(Issue("length_bloat", Severity.WARN,
                            f"译文长度是原文的 {ratio:.1f} 倍（{len(src)}→{len(dst)}）",
                            "UI 可能溢出，考虑精简"))
    if ratio < 0.25:
        issues.append(Issue("length_shrink", Severity.WARN,
                            f"译文长度仅为原文的 {ratio:.0%}", "疑似漏译部分内容"))
    return issues


@lru_cache(maxsize=4096)
def _ascii_term_re(variant: str) -> re.Pattern[str]:
    """ASCII 术语的「整词」正则（大小写不敏感）。

    边界**只按 ASCII 字母/数字/下划线**判定，不用 `\\b`：Python 的 `\\b` 认
    Unicode 词字符，`用he` 这种中文紧邻的写法会被判成「没有边界」而漏报，
    而术语表要的是「这两侧不能紧贴英文字母」，与中文相邻恰恰该命中。

    长度 ≤3 的短术语（`he` / `art` / `a`）也必须走这里：子串匹配下
    `Start` 里的 `art`、`The` 里的 `he` 会让一条原文刷出好几条 term_violation，
    WARN 噪音直接淹没真正的问题。
    """
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(variant)}(?![A-Za-z0-9_])", re.IGNORECASE)


def term_hit(text: str, variant: str) -> bool:
    """术语是否出现在 `text` 里：ASCII 走整词，中文走子串。

    **翻译侧的术语筛选也用它**（`store.glossary_for`）。两处口径必须一致：
    校验说"没命中"、筛 prompt 说"命中"，会让原文里压根没有的术语被塞进
    批次上下文，白占 token 还干扰模型。
    """
    if not variant:
        return False
    if not variant.isascii():
        # 中文（含中文的混合术语）没有词的概念，保持子串匹配
        return variant in text
    return _ascii_term_re(variant).search(text) is not None


def check_terms(src: str, dst: str, terms: dict[str, dict]) -> list[Issue]:
    """术语一致性：原文出现术语时，译文必须使用术语表指定译法。

    命中判定见 `_ascii_term_re`：英文按整词、中文按子串。
    """
    issues: list[Issue] = []
    if not terms:
        return issues
    for term, meta in terms.items():
        variants = [term, *(meta.get("variants") or [])]
        hit = any(v and term_hit(src, v) for v in variants)
        if not hit:
            continue
        want = meta.get("translation") or ""
        if want and want not in dst:
            issues.append(Issue("term_violation", Severity.WARN,
                                f"术语「{term}」应译为「{want}」，译文未使用",
                                "按术语表统一，或先提交术语变更"))
    return issues


def check_punctuation(dst: str) -> list[Issue]:
    issues: list[Issue] = []
    for pat, desc in BAD_PUNCT:
        if pat.search(dst):
            issues.append(Issue("punct_style", Severity.INFO, f"标点不规范: {desc}",
                                "中文语境使用全角标点，省略号用「……」"))
    if re.search(r"[，。！？；：]{2,}", dst):
        issues.append(Issue("punct_duplicate", Severity.WARN, "连续重复标点", "检查是否拼接出错"))
    return issues


def check_control_balance(src: str, dst: str) -> list[Issue]:
    """花括号/百分号平衡。

    只比**相对的失衡量**，不比绝对值：多行 JS/CoffeeScript 被拆成多条词条时，
    `{name: "natural",` 这种片段天生只有一个 `{`。以前只看译文自身平衡 →
    113 条正确译文被判「花括号不配对」，全是误报。
    """
    issues: list[Issue] = []
    if dst.count("{") - dst.count("}") != src.count("{") - src.count("}"):
        issues.append(Issue("brace_unbalanced", Severity.ERROR, "花括号不配对", "检查占位符是否截断"))
    if dst.count("%") != len(re.findall(r"%[\d$]*[sdfioxX%]", dst)):
        issues.append(Issue("percent_stray", Severity.WARN, "存在孤立的 % 号", "若需表达百分号请用％"))
    return issues


def run_qa(
    src: str,
    dst: str,
    *,
    terms: dict[str, dict] | None = None,
    block_on: Severity = Severity.ERROR,
    length_ratio_limit: float = 3.5,
    ascii_ratio_limit: float = 0.55,
) -> QAReport:
    rep = QAReport()
    for issue in (
        *check_placeholders(src, dst),
        *check_whitespace(src, dst),
        *check_numbers(src, dst),
        *check_untranslated(src, dst, ascii_ratio_limit=ascii_ratio_limit),
        *check_length(src, dst, limit=length_ratio_limit),
        *check_terms(src, dst, terms or {}),
        *check_punctuation(dst),
        *check_control_balance(src, dst),
    ):
        rep.add(issue)
    return rep.finalize(block_on)
