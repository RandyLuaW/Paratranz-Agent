"""回写模块的公共契约。

链路（与「提取 → 导入」严格对称）：

    提取器 run.py  ──JSONL {key, original, context}──▶  平台（词条）
                                                        │ translate / review 确认
                                                        ▼
    游戏文件  ◀──写回器 apply()──  平台拉回的译文包 {key → translation}

**key 就是定位符**：三个内置提取器都把「文件相对路径 + 文件内位置」编码进 key，
例如 `data/Actors.json::[12].name`、`data/Map001.json::L45.code401`、
`locale/zh.json::L123#1`。所以回写不需要重新解析游戏文件结构，只要按 key
回到原位写入即可 —— 这也是为什么 key 必须全局唯一且稳定。

写回器不碰文件系统：只吃「文件当前内容 + 待写条目」，吐「新内容 + 逐条结果」。
IO、原文校验、备份、输出目录都由 pipeline 统一负责，这样每个写回器都能纯函数式单测。

**只有两族定位符，就只写两个函数**：`json_path.apply_json` 与 `line.apply_line`，
由 `apply.apply_items` 按 `ParsedLocator.family` 一次分派。早先这里定义过一个
`Writer` 协议 + `default_writers()` 注册表 + `_owns()` 谓词，用于「将来可能加
更多写回器」——但定位符家族在 `locator.parse_locator` 里已经判定完了，
那套插件机制没有带来任何扩展性，只是让一次调用变成「遍历列表 + 逐项问归属」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 一条待写回的译文（来自平台）
@dataclass
class WriteItem:
    key: str            # 平台词条 key，形如 `<rel>::<locator>`
    translation: str    # 平台上的译文
    original: str = ""  # 平台记录的原文，用于「文件是否已变化」的校验
    stage: int = 0      # 平台 stage（1 已翻译 / 2 有疑问 / 3 已检查 / 5 已审核）


@dataclass
class ApplyResult:
    """一个文件写完后的结果。字段全是可上报的事实，不含推测。"""

    content: str = ""                                    # 新文件内容（dry_run 时也为新内容）
    written: list[str] = field(default_factory=list)      # 成功写入的 key
    # 定位到但原文与平台记录不一致：文件可能已更新，宁可不写
    mismatched: list[tuple[str, str]] = field(default_factory=list)  # (key, 原因)
    # key 的定位符在本文件里找不到（文件被改过 / 提取器换过版本）
    missing: list[str] = field(default_factory=list)
    # 写回器自身失败（格式不合法等）
    errors: list[tuple[str, str]] = field(default_factory=list)      # (key, 原因)
    # 该写回器接不接这个 key（由 can_handle 预先筛过，这里记录被它跳过的其它 key）
    unsupported: list[str] = field(default_factory=list)

    def merge(self, other: "ApplyResult") -> "ApplyResult":
        self.written += other.written
        self.mismatched += other.mismatched
        self.missing += other.missing
        self.errors += other.errors
        self.unsupported += other.unsupported
        return self

    @property
    def ok(self) -> bool:
        return not self.errors


def split_key(key: str) -> tuple[str, str]:
    """把 `<rel>::<locator>` 拆成 (相对路径, 定位符)。

    用 **第一个** `::` 分隔：定位符里可能出现 `::`（极少），但路径里不会。
    没有分隔符时视为「整文件级」定位符（如 `data/System.json::gameTitle` 之外的
    `meta::version` 这类），返回 ("", key)。

    **这里只做拆分，不校验路径**：写回器是纯函数，压根不碰文件系统，`rel`
    只用来判断「要不要按 JSON 字面量转义」。真正会把它拼到目录上的只有
    `pipeline.py`，越界校验（绝对路径 / `..` / 盘符）也在那里做 —— 见 `_safe_rel`。
    校验放在唯一会落盘的地方，才不会出现「这里拦了、那里漏了」。
    """
    if "::" not in key:
        return "", key
    rel, locator = key.split("::", 1)
    return rel.strip(), locator.strip()
