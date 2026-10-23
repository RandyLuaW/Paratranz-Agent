"""汉化规范生成与发布。

规范不是空模板：内容由项目实际状态推导——
- 项目概况、文件类型分布、词条/词数统计来自平台；
- 术语章节来自本地术语库（已同步的平台术语 + Agent 发布的）；
- 「常见错误」章节直接来自 QA 规则集，保证"规范里写的"和"自动校对拦的"是同一套；
- 模型只负责补充"本项目特有"的语气与场景说明，模型不可用时模板仍然完整可发。

发布走 `POST /projects/{id}/announcements`，默认 `internal=true`（仅成员可见）。
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from typing import TYPE_CHECKING, Sequence

from ..config import AgentConfig
from ..llm import BaseLLM, LLMError, LLMNotConfigured, build_messages, extract_json
from ..models import FileItem

if TYPE_CHECKING:  # pragma: no cover
    from ..client import ParaTranzClient
    from ..store import Store

STYLE_SYSTEM = """你是汉化组的规范撰写者。基于给定的项目信息，写一段"语言风格与语气"章节（Markdown，不要一级标题）。

要求：
- 结合文件构成判断文本类型（对话 / UI / 道具说明 / 系统提示 / 任务描述），分别给出语气要求；
- 给出 3-5 条可执行的判断标准，不要空话；
- 不要重复通用规则（占位符、标点、数字这些由模板负责）；
- 不超过 400 字。"""


class StyleGuidePipeline:
    name = "styleguide"

    def __init__(
        self,
        client: "ParaTranzClient",
        cfg: AgentConfig,
        store: "Store",
        llm: BaseLLM,
        system_prompt: str | None = None,
    ):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.llm = llm
        # 自定义提示词；空串表示用模块内置默认值
        self.system_prompt = (system_prompt or "").strip() or STYLE_SYSTEM

    # ------------------------------------------------------------------ 数据
    # 点后必须是 1-5 位 ASCII 字母数字才算扩展名，否则记 unknown。
    _EXT_RE = re.compile(r"^[A-Za-z0-9]{1,5}$")

    @classmethod
    def _kind_of(cls, name: str) -> str:
        """从文件名取扩展名（小写），取不到就 `unknown`。

        必须**先取 basename** 再找点：`www/data/v1.2/Map001` 直接
        `rsplit(".", 1)[-1]` 会得到 `2/map001`（把目录名里的点当扩展名），
        文件类型统计里凭空多出一个不存在的类型。
        """
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if "." not in base:
            return "unknown"
        ext = base.rsplit(".", 1)[-1].lower()
        return ext if cls._EXT_RE.match(ext) else "unknown"

    def _file_stats(self) -> tuple[list[FileItem], Counter[str]]:
        files = self.client.list_files()
        kinds: Counter[str] = Counter()
        for f in files:
            kinds[self._kind_of(f.name)] += 1
        return files, kinds

    def _term_table(self, limit: int = 40) -> str:
        terms = self.store.term_all()
        if not terms:
            return "_术语表尚未建立。运行 `ptz glossary build` 后重试。_"
        rows = ["| 原文 | 译法 | 词性 | 备注 |", "| --- | --- | --- | --- |"]
        for term, meta in list(terms.items())[:limit]:
            rows.append(
                f"| `{term}` | {meta['translation']} | {meta.get('pos') or '-'} | {meta.get('note') or ''} |"
            )
        more = f"\n\n> 共 {len(terms)} 条，此处仅列前 {limit} 条，完整列表见项目术语表。" if len(terms) > limit else ""
        return "\n".join(rows) + more

    # ------------------------------------------------------------------ 模型
    def _style_section(self, kinds: Counter[str], sample_pairs: Sequence[tuple[str, str]]) -> str:
        payload = {
            "project": self._project_name(),
            "source_lang": self.cfg.project.source_lang,
            "target_lang": self.cfg.project.target_lang,
            "file_kinds": dict(kinds),
            "style_hint": self.cfg.project.style,
            "samples": [{"original": a, "translation": b} for a, b in sample_pairs[:12]],
        }
        try:
            raw = self.llm.chat(
                build_messages(self.system_prompt, payload),
                json_mode=True,
            )
            data = extract_json(raw)
            if isinstance(data, dict):
                for key in ("section", "content", "result", "markdown"):
                    if isinstance(data.get(key), str):
                        return data[key].strip()
        except LLMNotConfigured:
            raise
        except LLMError:
            pass
        return (
            "### 语气\n\n"
            "- **对话**：口语自然，保留角色性格与口癖，避免书面化长句；一句一义，必要时断句。\n"
            "- **UI 与系统提示**：简洁直给，动词开头，不用敬语堆叠。\n"
            "- **道具/技能说明**：名词化短语 + 效果描述，避免整句叙述。\n"
            "- **任务描述**：先目标后条件，时间/地点信息前置。\n"
            "- 判定标准：读出来像中文原创、不像翻译腔；同一角色在不同文件里语气一致。"
        )

    # ------------------------------------------------------------------ 生成
    def build(self, *, with_llm: bool = True) -> str:
        files, kinds = self._file_stats()
        total = sum(f.total for f in files)
        translated = sum(f.translated for f in files)
        checked = sum(f.checked for f in files)
        reviewed = sum(f.reviewed for f in files)
        words = sum(f.words for f in files)
        pct = (translated / total * 100) if total else 0.0

        samples: list[tuple[str, str]] = []
        try:
            for s in self.client.iter_strings(stage=5, max_items=200):
                if s.translation.strip():
                    samples.append((s.original, s.translation))
                if len(samples) >= 12:
                    break
        except Exception:  # noqa: BLE001
            pass

        style_section = self._style_section(kinds, samples) if with_llm else self._style_section(kinds, [])
        today = _dt.date.today().isoformat()
        kind_line = "、".join(f"`{k}` × {v}" for k, v in kinds.most_common(8)) or "—"

        return f"""# {self._project_name()} 汉化规范

> 版本：{today} · 由 paratranz-agent 依据项目当前状态自动生成
> 项目进度：{translated}/{total} 条已翻译（{pct:.1f}%），已检查 {checked}，已审核 {reviewed}，总词数约 {words}

## 一、项目概况

| 项 | 值 |
| --- | --- |
| 源语言 → 目标语言 | {self.cfg.project.source_lang} → {self.cfg.project.target_lang} |
| 文件数 | {len(files)} |
| 文件类型 | {kind_line} |
| 词条总数 | {total} |

## 二、通用硬规则（违反即打回）

1. **占位符与控制符**：`\\N[1]`、`\\V[1]`、`\\C[2]`、`\\I[3]`、`\\FS[20]`、`%s`、`%d`、`{{0}}`、`{{name}}`、`<b>` 等必须与原文**数量一致、逐个对应**。不得翻译、不得增删、不得改变中英文括号。
2. **数字与单位**：数值保持原值，不做单位换算；原文有数字而译文全部消失视为错误。
3. **空译文与照抄**：译文为空、或与原文完全相同，一律视为未翻译。
4. **换行与空白**：原文含换行的，译文保持相同换行数；原文首尾有空格的不删。
5. **术语**：必须使用项目术语表既定译法（见第四节）。术语表未覆盖的重要名词，先在讨论区提出，不要各自发挥。

## 三、语言规范

{style_section}

### 标点

- 中文语境使用全角标点：`，。！？；：`
- 省略号用 `……`，不用 `...`；破折号用 `——`
- 中英文混排时，英文与数字两侧不加空格（平台按原文行裁剪空格，加了会被吃掉）
- 句末不加多余句号（UI 按钮、标题类）

### 用词

- 同一概念全项目统一一个词，不要"存档/进度/记录"混用
- 不造生僻译名；音译人名首次出现可加注
- 不使用网络流行语与方言，除非角色设定如此

## 四、术语表

{self._term_table()}

## 五、自动校对拦什么

以下规则由 `ptz review` 自动执行，命中 `error` 直接置为「有疑问(2)」：

| 规则 | 级别 | 说明 |
| --- | --- | --- |
| placeholder_missing / extra | error | 占位符或控制符增删 |
| empty / identical | error | 空译文、译文与原文相同 |
| number_lost | error | 原文数字在译文中全部消失 |
| brace_unbalanced | error | 花括号不配对 |
| no_cjk | error | 译文不含中文 |
| term_violation | warn | 未使用术语表译法 |
| length_bloat / shrink | warn | 译文长度异常（>3.5 倍或 <25%） |
| too_much_ascii | warn | 英文残留过多 |
| punct_style | info | 半角标点、英文省略号 |

## 六、流程与验收

1. **翻译**：按任务认领，译完把词条置为「已翻译(1)」。
2. **自检**：提交前跑一遍 `ptz review --dry-run`，确认没有 error。
3. **校对**：校对者复核「有疑问(2)」条目，改完置「已检查(3)」。
4. **审核**：二次校对项目需审核至「已审核(5)」。
5. **验收标准**：无 error 级问题；抽查 10 条语气连贯；术语零违规。

## 七、争议处理

- 术语冲突 → 讨论区开帖，达成一致后由管理员更新术语表；
- 无法确定的原文含义 → 置「有疑问(2)」并在评论写明候选译法，不要留空；
- 规范本身需要修订 → 在本公告下评论，下一版由 Agent 重新生成。
"""

    def _project_name(self) -> str:
        """项目名：配置里没有就用平台真实名字，再退化成「本项目」。

        真实项目里 `cfg.project.name` 常常是空的（yaml 模板默认 ""），
        不兜底的话规范标题会变成「本项目 汉化规范」，发到公告区很难看。
        """
        name = (self.cfg.project.name or "").strip()
        if name:
            return name
        if not hasattr(self, "_cached_project_name"):
            self._cached_project_name = ""
            try:
                self._cached_project_name = (self.client.get_project().name or "").strip()
            except Exception:  # noqa: BLE001 - 拿不到名字不该让规范生成失败
                pass
        return self._cached_project_name or "本项目"

    # ------------------------------------------------------------------ 发布
    # 旧版本把日期写进标题（`... 汉化规范 v2024-05-01`），一天一个新标题 →
    # 每天新建一条公告。这里用固定标题，日期只留在正文（`> 版本：{today}`）。
    _TITLE_VERSION_SUFFIX = re.compile(r"\s*v\d{4}-\d{2}-\d{2}\s*$")

    def _style_title(self) -> str:
        """固定标题：不含日期，才能「已存在则更新」而不是每天新建。"""
        return f"{self._project_name()} 汉化规范"

    @classmethod
    def _title_identity(cls, title: str) -> str:
        """标题归一：剥掉历史遗留的 `vYYYY-MM-DD` 尾巴，用于识别旧公告。"""
        return cls._TITLE_VERSION_SUFFIX.sub("", title).strip()

    def publish(
        self,
        markdown: str | None = None,
        *,
        dry_run: bool = False,
        with_llm: bool = True,
    ) -> dict:
        md = markdown or self.build(with_llm=with_llm)
        cfg = self.cfg.styleguide
        title = self._style_title()
        identity = self._title_identity(title)

        # 已存在同标题公告则更新，避免刷屏。
        # 匹配按归一后的标题，所以旧版带日期的公告也会被接管并改名，
        # 不会在切换版本的当天多出一条。
        existing = None
        try:
            for ann in self.client.list_announcements(page=1, page_size=50):
                if self._title_identity(ann.title) == identity:
                    existing = ann
                    break
        except Exception:  # noqa: BLE001
            pass

        if dry_run:
            return {"title": title, "action": "update" if existing else "create", "dry_run": True,
                    "length": len(md)}

        if existing:
            payload: dict = {"content": md, "bumped": cfg.bumped}
            if existing.title != title:  # 顺手把旧的带日期标题归一
                payload["title"] = title
            self.client.update_announcement(existing.id, payload)
            self.store.ledger_mark(self.name, str(existing.id), "update", title)
            action, ann_id = "update", existing.id
        else:
            ann = self.client.create_announcement(
                title=title,
                content=md,
                category=cfg.category,
                internal=cfg.internal,
                bumped=cfg.bumped,
            )
            self.store.ledger_mark(self.name, str(ann.id), "create", title)
            action, ann_id = "create", ann.id
        self.store.ledger_commit()
        return {"title": title, "action": action, "id": ann_id, "dry_run": False, "length": len(md)}
