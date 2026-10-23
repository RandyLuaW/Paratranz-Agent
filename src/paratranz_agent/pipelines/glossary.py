"""术语整理与发布。

两个来源合流：
1. **统计法**（确定性）：从已翻译/已审核词条里找高频英文词与词组；
2. **对齐法**（模型）：拿 (原文, 已定稿译文) 句对让模型判断"这是不是术语、该怎么译"，
   比词频更准，因为它看得到上下文与既有译法。

发布走 `PUT /projects/{id}/terms`（multipart JSON 批量导入，会覆盖同名术语），
冲突保护：已存在且译法一致的跳过；译法不一致的默认不覆盖，除非 `--overwrite`。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from ..config import AgentConfig
from ..llm import BaseLLM, LLMError, LLMNotConfigured, build_messages, extract_json
from ..models import Stage, StringItem

if TYPE_CHECKING:  # pragma: no cover
    from ..client import ParaTranzClient
    from ..store import Store

STOPWORDS = set(
    """a an the and or but if then than that this these those there here
    i me my mine we us our you your he him his she her it its they them their
    is am are was were be been being do does did done have has had having
    will would shall should can could may might must not no nor so as at by for from in into of off on onto out over to up with within without
    what which who whom whose when where why how all any both each few more most other some such only own same too very just now
    oh ah eh hey hi hello yes yeah no ok okay well please thanks thank sorry
    get got go going gone come came make made take took see saw know knew
    want need like look say said tell told give gave put let us
    one two three four five six seven eight nine ten
    """.split()
)

WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
PHRASE = re.compile(r"\b[A-Z][a-zA-Z'\-]*(?:\s+[A-Z][a-zA-Z'\-]*){1,3}\b")

# 取材只认「已定稿」：5=已审核 / 3=已检查。
# stage=1（TRANSLATED）是**机翻**，把它当定稿取材，错译会被固化成强制术语污染全项目。
FINAL_STAGES = (Stage.REVIEWED, Stage.CHECKED)
MT_STAGE = Stage.TRANSLATED

# 单次定译请求最多带多少候选。分批发完，不再只处理前 N 个（截断=静默丢候选）。
RESOLVE_BATCH = 80

# 候选来源优先级：模型对齐 > 词频统计 > 机翻句对。
# 同一 term 出现两种译法时按它选，publish 默认只发最高一级。
SOURCE_RANK = {"llm": 2, "freq": 1, "mt": 0}

PROPOSE_SYSTEM = """你是游戏本地化术语库维护者。给定若干「原文 / 已定稿译文」句对，
找出其中应当统一译法的术语（道具名、技能名、地名、人名、组织名、系统名词等），并给出译法。

规则：
- 只收真正需要全项目统一的专有名词或固定术语；普通词汇、语气词、代词不要收；
- 译法必须能从给定译文里找到依据（译文中实际使用的写法），不要自创；
- 单字术语、纯数字、纯符号不要收；
- pos 取值：noun / verb / adj / adv，不确定填 noun；
- variants 填原文的其他形态（复数、缩写、大小写变体），可为空数组；
- **note 写「怎么用」，不写「是什么意思」**。参考成熟汉化组的写法：
  * 限定适用范围：「用于黑暗领域相关文本时称侍祭，其他国家视情况处理」
  * 标注易混：「人名。注意不要与 阿德恩 Adean 搞混」
  * 说明来历：「纪年法，即灰烬天空之日后（After Day of Ashen Skies）」
  * 给对照：「古精灵大陆，相当于现实世界的美洲」
  * 没有这类信息就留空字符串，**不要写同义反复的废话**。

输出严格 JSON：{"terms":[{"term":"<原文>","translation":"<译法>","pos":"noun","note":"","variants":[]}]}"""

RESOLVE_SYSTEM = """你是游戏本地化术语库维护者。给定若干术语候选及其在已定稿译文中的出现样例，
判断每个候选是否值得进术语表；值得的，从样例译文里找出它实际被译成的中文。

规则：
- 只有确实需要全项目统一的词才保留；普通名词、动词、形容词一般不收；
- 译法必须来自样例译文，不要自创；样例里找不到对应译法的，translation 留空字符串；
- 输出严格 JSON：{"terms":[{"term":"<原文>","translation":"<译法或空串>","pos":"noun","note":""}]}"""


@dataclass
class TermCandidate:
    term: str
    translation: str = ""
    pos: str = "noun"
    note: str = ""
    variants: list[str] = field(default_factory=list)
    occurrences: int = 0
    samples: list[tuple[str, str]] = field(default_factory=list)
    # 来源：llm=模型对齐 / freq=词频统计 / mt=只出现在未校对机翻句对里
    source: str = "freq"

    def as_import(self) -> dict:
        d = {"term": self.term, "translation": self.translation, "pos": self.pos}
        if self.note:
            d["note"] = self.note
        if self.variants:
            d["variants"] = self.variants
        return d


class GlossaryPipeline:
    name = "glossary"

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
        self.system_prompt = (system_prompt or "").strip() or PROPOSE_SYSTEM
        # 本轮统计（build/publish 后读，也随 publish 结果一起返回）：
        # pairs / pairs_final / pairs_mt / proposed / frequency / deduped /
        # resolved / unresolved / built
        # 注意别用 "candidates" 当键名：cli `--json` 与 server 端都用它装候选列表。
        self.stats: dict[str, int] = {}
        # 本轮取自 stage=1（机翻）句对的词条 id，用于给候选标 source="mt"
        self._mt_ids: set[int] = set()
        # 词频统计真实出现次数，供 TermCandidate.occurrences 使用
        self._freq_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ 取材
    def _translated_pairs(self, *, limit: int = 4000) -> list[StringItem]:
        """收集可当"术语依据"的句对。

        默认只用已检查（3）/已审核（5）的句子。stage=1 是机翻，错译会被固化成
        强制术语，比不收更糟；只有在 5/3 不够 `limit` 且配置显式打开
        `glossary.include_mt` 时才补机翻句对，并把它们记进 `_mt_ids`，
        候选据此标 `source="mt"`（publish 默认不提交）。
        """
        include_mt = bool(getattr(self.cfg.glossary, "include_mt", False))
        self._mt_ids = set()
        out: list[StringItem] = []

        def take(stage: int) -> None:
            for s in self.client.iter_strings(stage=stage):
                if s.translation.strip() and len(s.original) <= 160:
                    out.append(s)
                    if len(out) >= limit:
                        return

        for st in FINAL_STAGES:
            take(st)
            if len(out) >= limit:
                break
        self.stats["pairs_final"] = len(out)
        if include_mt and len(out) < limit:
            before = len(out)
            take(MT_STAGE)
            for s in out[before:]:
                self._mt_ids.add(s.id)
        self.stats["pairs_mt"] = len(self._mt_ids)
        self.stats["pairs"] = len(out)
        return out

    # ---------------------------------------------------------------- 统计法
    def _frequency_candidates(self, pairs: Sequence[StringItem], *, top_n: int = 120) -> list[str]:
        """高频词/词组候选。

        `occurrences` 是**真实出现次数**（一条句对里出现一次算一次），
        `score` 只是排序权重（词组更重要，×2）。`min_occurrences` 必须只跟
        真实次数比 —— 之前拿加权分去比，出现 1 次的词组权重是 2，于是
        `min_occurrences=2` 形同虚设。
        """
        occurrences: Counter[str] = Counter()
        score: Counter[str] = Counter()
        for s in pairs:
            for w in WORD.findall(s.original):
                lw = w.lower()
                if lw in STOPWORDS or len(lw) < 3:
                    continue
                occurrences[w] += 1
                score[w] += 1
            for p in PHRASE.findall(s.original):
                occurrences[p] += 1
                score[p] += 2  # 词组权重更高（仅影响排序）
        self._freq_counts = dict(occurrences)
        threshold = self.cfg.glossary.min_occurrences
        picked = [w for w in occurrences if occurrences[w] >= threshold]
        picked.sort(key=lambda w: (-score[w], -occurrences[w], w.casefold()))
        return picked[:top_n]

    def _samples_for(self, term: str, pairs: Sequence[StringItem], k: int = 3) -> list[tuple[str, str]]:
        out = []
        for s in pairs:
            if term in s.original:
                out.append((s.original, s.translation))
                if len(out) >= k:
                    break
        return out

    # ------------------------------------------------------------------ 模型
    def _propose(self, pairs: Sequence[StringItem]) -> list[TermCandidate]:
        if not pairs:
            return []
        payload = {
            "items": [
                {"original": s.original, "translation": s.translation} for s in pairs[:200]
            ]
        }
        try:
            raw = self.llm.chat(
                build_messages(self.system_prompt, payload),
                json_mode=True,
            )
            data = extract_json(raw)
        except LLMNotConfigured:
            raise
        except LLMError:
            return []

        out: list[TermCandidate] = []
        for it in (data.get("terms") if isinstance(data, dict) else data) or []:
            if not isinstance(it, dict):
                continue
            term = str(it.get("term", "")).strip()
            tr = str(it.get("translation", "")).strip()
            if not term or not tr or len(term) < 2:
                continue
            out.append(
                TermCandidate(
                    term=term,
                    translation=tr,
                    pos=str(it.get("pos") or "noun"),
                    note=str(it.get("note") or "")[:200],
                    variants=[str(v) for v in (it.get("variants") or []) if str(v).strip()][:8],
                    source="llm",
                )
            )
        return out

    @staticmethod
    def _term_key(term: str) -> str:
        return term.strip().casefold()

    def _dedupe(self, candidates: Sequence[TermCandidate]) -> list[TermCandidate]:
        """按 term 归并重复候选（`deduped` 记被吞掉的条数）。

        之前模型返回重复 term 就进两条候选，publish 也都提交 —— 译法冲突时
        后者覆盖前者，且谁胜出取决于模型返回顺序。这里：
        - 顺序（term 位置）以**首次出现**为准，term 拼写也保留首次的；
        - 同 term 译法不同时保留来源优先级更高的一条（模型对齐 > 词频 > 机翻）；
        - 归并时 occurrences 取最大，样例/备注补到保留项上，不丢证据。
        """
        out: list[TermCandidate] = []
        at: dict[str, int] = {}
        self.stats["deduped"] = 0
        for c in candidates:
            key = self._term_key(c.term)
            idx = at.get(key)
            if idx is None:
                at[key] = len(out)
                out.append(c)
                continue
            self.stats["deduped"] += 1
            keep = out[idx]
            keep.occurrences = max(keep.occurrences, c.occurrences)
            if not keep.samples:
                keep.samples = list(c.samples)
            if not keep.note and c.note:
                keep.note = c.note
            if c.translation and c.translation != keep.translation:
                if SOURCE_RANK.get(c.source, 0) > SOURCE_RANK.get(keep.source, 0):
                    keep.translation = c.translation
                    keep.pos = c.pos or keep.pos
                    keep.note = c.note or keep.note
                    keep.variants = list(c.variants or keep.variants)
                    keep.source = c.source
            elif not keep.translation and c.translation:
                keep.translation = c.translation
            elif c.variants:
                for v in c.variants:
                    if v not in keep.variants:
                        keep.variants.append(v)
        return out

    def _mark_mt_source(self, candidates: Sequence[TermCandidate], pairs: Sequence[StringItem]) -> None:
        """证据只来自机翻句对（stage=1）的候选标 `source="mt"`，publish 默认不提交。"""
        if not self._mt_ids:
            return
        for c in candidates:
            if c.source == "mt":
                continue
            hits = [s for s in pairs if c.term in s.original]
            if hits and all(s.id in self._mt_ids for s in hits):
                c.source = "mt"

    def _resolve_batch(self, batch: Sequence[TermCandidate]) -> dict[str, dict] | None:
        """一批候选的定译请求，返回 term -> 模型返回项；请求失败返回 None（与"模型没给结果"区分）。"""
        payload = {
            "task": "term-resolve",
            "items": [
                {
                    "term": c.term,
                    "samples": [{"original": a, "translation": b} for a, b in c.samples],
                }
                for c in batch
            ],
        }
        try:
            raw = self.llm.chat(
                build_messages(RESOLVE_SYSTEM, payload),
                json_mode=True,
            )
            data = extract_json(raw)
        except LLMNotConfigured:
            raise
        except LLMError:
            return None
        mapping: dict[str, dict] = {}
        for it in (data.get("terms") if isinstance(data, dict) else data) or []:
            if isinstance(it, dict) and it.get("term"):
                mapping[str(it["term"])] = it
        return mapping

    def _resolve(self, candidates: Sequence[TermCandidate]) -> list[TermCandidate]:
        """给统计法产出的、还没有译文的候选定译（从对齐样例里取）。

        每批 `RESOLVE_BATCH` 条**循环发完**。原来是 `need[:80]` 一刀截断：
        第 81 条以后的候选拿不到译法，随后在 build 里被静默丢弃（实测 84→80）。
        某批请求失败就停手，剩下的保持无译法、由 build 计入 `unresolved`，
        不当成功。
        """
        need = [c for c in candidates if not c.translation]
        if not need:
            self.stats["resolved"] = 0
            return list(candidates)
        for i in range(0, len(need), RESOLVE_BATCH):
            batch = need[i : i + RESOLVE_BATCH]
            mapping = self._resolve_batch(batch)
            if mapping is None:
                break
            for c in batch:
                got = mapping.get(c.term)
                if not got:
                    continue
                c.translation = str(got.get("translation") or "").strip()
                if got.get("pos"):
                    c.pos = str(got["pos"])
                if got.get("note"):
                    c.note = str(got["note"])[:200]
        self.stats["resolved"] = sum(1 for c in need if c.translation)
        return list(candidates)

    # ------------------------------------------------------------------ 执行
    def build(self, *, limit_pairs: int = 2000) -> list[TermCandidate]:
        self.stats = {
            "pairs": 0,
            "pairs_final": 0,
            "pairs_mt": 0,
            "proposed": 0,
            "frequency": 0,
            "deduped": 0,
            "resolved": 0,
            "unresolved": 0,
            "built": 0,
        }
        pairs = self._translated_pairs(limit=limit_pairs)
        existing = self.store.term_all()
        platform_terms = {}
        try:
            platform_terms = {t.term: t for t in self.client.iter_terms()}
        except Exception:  # noqa: BLE001
            pass

        candidates: list[TermCandidate] = []

        # 1) 模型对齐抽取
        if self.cfg.glossary.llm_propose:
            proposed = self._propose(pairs)
            self.stats["proposed"] = len(proposed)
            candidates.extend(proposed)

        # 2) 统计补位（模型没提到但高频的词，只保留句对里有中文对应的）
        seen = {c.term for c in candidates}
        for term in self._frequency_candidates(pairs):
            if term in seen or term in existing or term in platform_terms:
                continue
            samples = self._samples_for(term, pairs, k=2)
            if not samples:
                continue
            candidates.append(
                TermCandidate(
                    term=term,
                    occurrences=self._freq_counts.get(term, len(samples)),
                    samples=samples,
                    source="freq",
                )
            )
            seen.add(term)
        self.stats["frequency"] = len(candidates) - self.stats["proposed"]

        # 3) 机翻来源打标（publish 默认不提交 source="mt"）
        self._mark_mt_source(candidates, pairs)

        # 4) 去重（同 term 归并，译法冲突按来源优先级取舍）
        candidates = self._dedupe(candidates)

        # 5) 统计候选定译（分批发完，不再截断前 80）
        candidates = self._resolve(candidates)

        # 6) 过滤：已有术语、无译文、与平台冲突
        result: list[TermCandidate] = []
        for c in candidates:
            if c.term in existing or c.term in platform_terms:
                continue
            if not c.translation:
                # 之前这里是静默 continue：候选被丢掉但统计里看不见
                self.stats["unresolved"] += 1
                continue
            if not c.samples:
                c.samples = self._samples_for(c.term, pairs, k=2)
            result.append(c)
            if len(result) >= self.cfg.glossary.max_terms:
                break
        self.stats["built"] = len(result)
        return result

    def publish(
        self,
        candidates: Sequence[TermCandidate],
        *,
        dry_run: bool = False,
        overwrite: bool = False,
        include_mt: bool | None = None,
    ) -> dict:
        # 机翻（stage=1）来源的候选默认不提交：没校对过就不该变成强制术语。
        if include_mt is None:
            include_mt = bool(getattr(self.cfg.glossary, "include_mt", False))
        if not candidates:
            return {
                **self.stats,
                "submitted": 0,
                "skipped": 0,
                "skipped_mt": 0,
                "dry_run": dry_run,
            }

        # 冲突判定必须看**平台**术语表：本地库可能从未 sync，
        # 只查本地会把平台已有术语当"不存在"直接覆盖。
        # 拉不到就放弃本次发布 —— 不能当成"平台没有术语"。
        try:
            platform = {t.term: t.translation for t in self.client.iter_terms()}
        except Exception as exc:  # noqa: BLE001 - 宁可失败也不覆盖
            raise RuntimeError(
                "读取平台术语表失败，为避免覆盖人工术语，本次不发布：" + str(exc)
            ) from exc

        existing = self.store.term_all()
        payload: list[dict] = []
        skipped = 0
        skipped_mt = 0
        for c in candidates:
            if c.source == "mt" and not include_mt:
                # 只出现在未校对机翻句对里的候选：默认不发，避免把错译固化成强制术语
                skipped_mt += 1
                continue
            prev_platform = platform.get(c.term)
            prev_local = (existing.get(c.term) or {}).get("translation")
            prev = prev_platform if prev_platform is not None else prev_local
            if prev is not None and not overwrite:
                # 平台/本地已有（不论译法是否相同）都不动
                skipped += 1
                continue
            payload.append(c.as_import())

        if dry_run or not payload:
            return {
                **self.stats,
                "submitted": len(payload),
                "skipped": skipped,
                "skipped_mt": skipped_mt,
                "dry_run": dry_run,
            }

        self.client.import_terms(payload)
        for p in payload:
            self.store.term_upsert(
                p["term"],
                p["translation"],
                pos=p.get("pos"),
                note=p.get("note"),
                variants=p.get("variants") or (),
                source="agent",
            )
            self.store.ledger_mark(self.name, p["term"], "publish", p["translation"])
        self.store.term_commit()
        self.store.ledger_commit()
        return {
            **self.stats,
            "submitted": len(payload),
            "skipped": skipped,
            "skipped_mt": skipped_mt,
            "dry_run": False,
        }

    def to_markdown(self, candidates: Sequence[TermCandidate]) -> str:
        rows = ["| 原文 | 建议译法 | 词性 | 出现 | 备注 |", "| --- | --- | --- | ---: | --- |"]
        for c in candidates:
            rows.append(
                f"| `{c.term}` | {c.translation} | {c.pos} | {c.occurrences or '-'} | {c.note or ''} |"
            )
        return "\n".join(rows)
