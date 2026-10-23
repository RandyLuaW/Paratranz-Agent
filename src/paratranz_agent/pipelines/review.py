"""校对：确定性 QA 打底 + LLM 二审。

平台语义（OpenAPI Stage）：
    1 已翻译 → 3 已检查（一次校对项目）→ 5 已审核（二次校对项目）
    2 有疑问 = 打回

策略：
- 空译文脏数据（stage=1 但 `translation` 为空）→ 默认按 empty 级问题**打回 stage=2**；
  打开 `review.reset_empty` 则退回 stage=0 让翻译流水线重做。这类条目以前被
  `continue` 直接跳过：translate 只取 stage=0、review 又不看它，于是永远卡住，
  还把平台的 `translated` 计数抬成假的 100%。
- 确定性 QA 命中 `error`（占位符丢失、译文=原文、空译文、数字丢失）→ 直接打回 stage=2，
  不浪费模型调用；
- 只有 `warn` / `info` 的进入 LLM 二审；二审判 fail 也打回 stage=2 并留理由；
- 二审通过 → 写入 `pass_stage`（默认 3 已检查）；
- 打回理由可选发到平台（`review.comment_on_fail`，默认 **关闭**）。
  平台侧**没有词条级评论接口**（实测见 `client.create_issue` 的 docstring），
  所以一批打回项汇总成一个**项目级讨论帖**：首次建帖、之后追加回复，
  帖子里是一张打回明细表。发帖失败**不影响** stage 写回。
这样机器能判死的错误永远先被拦下，模型的判断只用于"机器判不了"的部分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Sequence

from ..config import AgentConfig
from ..llm import BaseLLM, LLMError, LLMNotConfigured, build_messages, extract_json
from ..models import Stage, StringItem
from ..qa import Issue, QAReport, Severity, run_qa

if TYPE_CHECKING:  # pragma: no cover
    from ..client import ParaTranzClient
    from ..store import Store

# 平台对帖子正文的长度上限未知，保守截断，避免整条被拒
MAX_COMMENT_CHARS = 800
# 评论里原文 / 译文摘要各自的截断长度
QUOTE_CHARS = 160
# 打回台账帖的固定标题：靠它认出「上次自己发的那条」，从而追加回复而不是每次新建
_DIGEST_TITLE = "[自动校对] 打回明细"
# 台账帖最多列多少行明细，以及正文总长上限（平台会 400 掉超长 body 的概率未知）
_DIGEST_MAX_ROWS = 200
_DIGEST_MAX_CHARS = 20000
# 单轮取材条数上限。`iter_strings` 到上限就 `return`，不会告诉调用方「后面还有」，
# 所以触到上限时必须把「被截断」写进 stats 并在日志里说明（见 _collect / run）。
DEFAULT_MAX_ITEMS = 20000


def _clip(text: str, limit: int = QUOTE_CHARS) -> str:
    """压成一行的摘要：多行原文直接塞进帖子会破坏 markdown 表格。"""
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


# 模型可能回中文/带空白的判定词，这里统一归一。
# **只有明确的「通过」才算 pass**：拿不准一律返回 ""（未判定），交给上层保持原状。
_PASS_WORDS = {"pass", "ok", "通过", "合格", "true", "yes"}
_FAIL_WORDS = {"fail", "failed", "不通过", "不合格", "错误", "false", "no", "reject"}


def _norm_verdict(raw: Any) -> str:
    """把模型回的判定归一成 `"pass"` / `"fail"` / `""`（未判定）。

    以前这里是 `str(it.get("verdict", "pass")).lower()` —— 缺字段就**默认通过**，
    而且主循环只认精确的 `"fail"`，于是「中文不合格」「fail 带空格」「字段缺失」
    全都算通过，未审词条被写成 stage=3（已检查）。这是 review fail-open 的残留通路。
    """
    v = str(raw if raw is not None else "").strip().lower()
    if not v:
        return ""
    if v in _PASS_WORDS:
        return "pass"
    if v in _FAIL_WORDS:
        return "fail"
    # 含中文字样时按包含判断（「译文不通过」「该条不合格」之类）
    if any(w in v for w in ("不通过", "不合格", "错误", "漏译", "错译")):
        return "fail"
    if any(w in v for w in ("通过", "合格")):
        return "pass"
    return ""


def _cell(text: str, limit: int = QUOTE_CHARS) -> str:
    """markdown 表格单元格：转义竖线（原文里 `|` 会把表格拆坏）。"""
    return _clip(text, limit).replace("|", "\\|").replace("\n", " ")


def _qa_reason(rep: QAReport, block_on: Severity) -> str:
    """把「为什么打回」压成一句人能读懂的理由（只取触发打回的那几条）。"""
    hit = [i for i in rep.issues if i.severity >= block_on] or list(rep.issues)
    parts = [f"{i.message}" + (f"（{i.hint}）" if i.hint else "") for i in hit[:4]]
    return "；".join(parts) or "确定性 QA 未通过"

REVIEW_SYSTEM = """你是一名严格的游戏本地化校对员。给定原文与译文，判断译文是否可以直接交付。

判定为 fail 的情形：
- 漏译、错译、语义偏离；
- 术语与给定术语表冲突；
- 占位符/控制符/换行与原文不一致；
- 中文不通顺、有语法错误、有英文残留（专有名词除外）；
- 语气与场景不符（例如系统提示写成了玩笑口吻）。

判定为 pass 的情形：语义准确、术语正确、中文自然、格式一致。轻微风格偏好不算 fail。

输出严格 JSON：{"items":[{"id":<原样回传>,"verdict":"pass"或"fail","reason":"<不超过40字>"}]}"""


@dataclass
class ReviewStats:
    fetched: int = 0
    # 取材被 max_items 截断：本轮只处理前 max_items 条，项目里还有剩余。
    # 没有这两个字段时「只审了前 2 万条」会被报成「已经全审完」。
    truncated: bool = False
    max_items: int = 0
    qa_blocked: int = 0
    llm_blocked: int = 0
    passed: int = 0
    disputed: int = 0
    written: int = 0
    # 模型没回 verdict / 整批失败：保持原状，不算通过
    skipped_no_verdict: int = 0
    failed_batches: int = 0
    # stage=1 但译文为空的脏数据条数（打回或重置，见 run()）
    empty_translation: int = 0
    # 打回明细帖：action = create / reply / ""（没发）；id = 平台讨论 id；
    # error = 发帖失败原因（失败不影响 stage 写回，也绝不静默）
    issue_action: str = ""
    issue_id: int = 0
    issue_error: str = ""
    dry_run: bool = False
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "truncated": self.truncated,
            "max_items": self.max_items,
            "qa_blocked": self.qa_blocked,
            "llm_blocked": self.llm_blocked,
            "passed": self.passed,
            "disputed": self.disputed,
            "written": self.written,
            "skipped_no_verdict": self.skipped_no_verdict,
            "failed_batches": self.failed_batches,
            "empty_translation": self.empty_translation,
            "issue_action": self.issue_action,
            "issue_id": self.issue_id,
            "issue_error": self.issue_error,
            "dry_run": self.dry_run,
        }


class ReviewPipeline:
    name = "review"

    def __init__(
        self,
        client: "ParaTranzClient",
        cfg: AgentConfig,
        store: "Store",
        llm: BaseLLM,
        system_prompt: str | None = None,
        log: "Callable[[str], None] | None" = None,
    ):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.llm = llm
        # 自定义提示词；空串表示用模块内置默认值
        self.system_prompt = (system_prompt or "").strip() or REVIEW_SYSTEM
        # 进度回调：全量校对要跑几十批模型调用，没有进度就是黑箱。
        # CLI 把它打到 stderr（--json 的 stdout 保持干净），WebUI 直接进日志。
        self._log = log or (lambda _msg: None)

    # ------------------------------------------------------------------ 取材
    def _collect(
        self,
        *,
        task: int | None = None,
        limit: int | None = None,
        include_disputed: bool = False,
        max_items: int = DEFAULT_MAX_ITEMS,
        stats: "ReviewStats | None" = None,
    ) -> list[StringItem]:
        """默认只校对「已翻译(1)」。

        「有疑问(2)」是**等人处理**的状态：Agent 已经判过它不合格，
        再拉出来只会得到同样的结论，形成死循环。需要重试时显式开
        `include_disputed`（或先让 translate 重译）。

        **空译文照收**：stage=1 却没有译文属于脏数据，交给 `run()` 按 empty 级问题
        处理（打回或重置）。以前在这里 `continue` 掉，等于让它既不被重译也不被审，
        永久卡死，还会把平台的 translated 计数抬成假的 100%。

        取到的条数顶到 `max_items` 就把 `stats.truncated` 置位：`iter_strings`
        到上限只是安静地 `return`，不记这一笔的话报表会声称「全审完了」。
        """
        stages = (Stage.TRANSLATED, Stage.DISPUTED) if include_disputed else (Stage.TRANSLATED,)
        out: list[StringItem] = []
        for st in stages:
            taken = 0
            for s in self.client.iter_strings(task=task, stage=st, max_items=max_items):
                taken += 1
                out.append(s)
                if limit and len(out) >= limit:
                    return out
            if stats is not None and max_items and taken >= max_items:
                stats.truncated = True
                stats.max_items = max_items
        return out

    # ------------------------------------------------------------------ 二审
    def _build_messages(
        self, batch: Sequence[StringItem], *, terms: dict[str, dict]
    ) -> list[dict]:
        # terms 由 run() 取一次传进来：术语表全表读不能按批重复
        glossary = self.store.glossary_for([s.original for s in batch], terms=terms, limit=40)
        payload = {
            "task": "review",
            "style": self.cfg.project.style,
            "glossary": glossary,
            "items": [
                {
                    "id": s.id,
                    "original": s.original,
                    "translation": s.translation,
                    **({"context": s.context} if s.context else {}),
                }
                for s in batch
            ],
        }
        return build_messages(self.system_prompt, payload)

    def _second_pass(
        self, batch: Sequence[StringItem], *, terms: dict[str, dict]
    ) -> dict[int, tuple[str, str]]:
        try:
            raw = self.llm.chat(self._build_messages(batch, terms=terms), json_mode=True)
            data = extract_json(raw)
        except LLMNotConfigured:
            raise
        except LLMError:
            # 模型不可用时不阻断：确定性 QA 已过的条目按通过处理
            return {}
        out: dict[int, tuple[str, str]] = {}
        items = data.get("items") if isinstance(data, dict) else data
        for it in items or []:
            if not isinstance(it, dict):
                continue
            try:
                sid = int(it["id"])
            except (KeyError, TypeError, ValueError):
                continue
            out[sid] = (_norm_verdict(it.get("verdict")), str(it.get("reason", ""))[:200])
        return out

    # ------------------------------------------------------------------ 执行
    def run(
        self,
        *,
        task: int | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        include_disputed: bool = False,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> ReviewStats:
        cfg = self.cfg.review
        stats = ReviewStats(dry_run=dry_run)
        targets = self._collect(
            task=task,
            limit=limit,
            include_disputed=include_disputed,
            max_items=max_items,
            stats=stats,
        )
        stats.fetched = len(targets)
        if stats.truncated:
            self._log(
                f"取材被上限截断：本轮只取前 {stats.max_items} 条，项目里还有剩余；"
                f"写回后这些词条已离开当前 stage，再跑一轮会接着处理"
            )
        if not targets:
            self._log("没有待校对的词条")
            return stats
        self._log(f"取材完成：{len(targets)} 条待校对（stage=1 已翻译）")

        # 两个开关都只在 review.py 里 getattr 兜底：ReviewCfg 尚未声明它们
        # （config.py 不在本次可改范围）。缺省值都是 False —— 评论端点未经验证，
        # 重置空译文属于行为变更，都不该默认打开。
        comment_on_fail = bool(getattr(cfg, "comment_on_fail", False))
        reset_empty = bool(getattr(cfg, "reset_empty", False))

        # 术语表只取一次：QA 与每批 prompt 的 glossary 共用同一份快照
        terms = self.store.term_all()
        block_on = Severity.parse(cfg.block_on)

        dispute_ids: list[int] = []
        reset_ids: list[int] = []
        to_llm: list[StringItem] = []
        local_pass: list[int] = []
        # 打回理由：QA 规则 message / LLM reason，评论要用
        reasons: dict[int, str] = {}

        # 1) 确定性 QA（空译文的脏数据在最前面拦下）
        for s in targets:
            if not (s.translation or "").strip():
                # stage=1 + 空译文：translate 只取 stage=0，review 以前又跳过它，
                # 两边都不管 → 永久卡死且虚抬 translated。默认打回让人看见；
                # reset_empty 则退回 stage=0 交给翻译流水线重做。
                stats.empty_translation += 1
                if reset_empty:
                    reset_ids.append(s.id)
                    continue
                empty_issue = Issue("empty", Severity.ERROR, "译文为空", "未翻译")
                dispute_ids.append(s.id)
                stats.qa_blocked += 1
                reasons[s.id] = f"{empty_issue.message}（stage=1 却没有译文，需重新翻译）"
                if len(stats.samples) < 30:
                    stats.samples.append(
                        {
                            "id": s.id,
                            "stage": "empty",
                            "original": s.original,
                            "translation": s.translation,
                            "issues": [empty_issue.as_dict()],
                        }
                    )
                continue

            rep = run_qa(
                s.original,
                s.translation,
                terms=terms,
                block_on=block_on,
                length_ratio_limit=cfg.length_ratio_limit,
                ascii_ratio_limit=cfg.ascii_ratio_limit,
            )
            if not rep.ok:
                dispute_ids.append(s.id)
                stats.qa_blocked += 1
                reasons[s.id] = _qa_reason(rep, block_on)
                if len(stats.samples) < 30:
                    stats.samples.append(
                        {
                            "id": s.id,
                            "stage": "qa",
                            "original": s.original,
                            "translation": s.translation,
                            "issues": [i.as_dict() for i in rep.issues],
                        }
                    )
            else:
                to_llm.append(s)

        # 2) LLM 二审
        if cfg.llm_second_pass and to_llm:
            size = max(1, cfg.batch_size)
            batches = [to_llm[i : i + size] for i in range(0, len(to_llm), size)]
            n_batches = len(batches)
            self._log(f"需要模型二审 {len(to_llm)} 条，分 {n_batches} 批（并发 {self.cfg.llm.concurrency}）")
            # 每 5% 报一次进度，批数少时每批都报
            step = max(1, n_batches // 20)

            def _progress(done: int, total: int) -> None:
                if done % step == 0 or done == total:
                    pct = done * 100 // max(1, total)
                    self._log(f"二审进度 {done}/{total} 批（{pct}%）")

            results = self.llm.map_concurrent(
                batches,
                partial(self._second_pass, terms=terms),
                concurrency=self.cfg.llm.concurrency,
                on_done=_progress,
            )
            verdicts: dict[int, tuple[str, str]] = {}
            for res in results:
                if isinstance(res, LLMNotConfigured):
                    raise res
                if isinstance(res, dict):
                    verdicts.update(res)
                else:
                    # 整批失败（LLMError 等）：这批没有 verdict，后续按"未判定"跳过
                    stats.skipped_no_verdict += 0
                    stats.failed_batches += 1
            for s in to_llm:
                # 模型漏回 verdict / 整批失败时**绝不能当通过**——那会把没审的词条
                # 直接置成 stage=3（已检查）。这里用空 verdict 表示"未判定"。
                verdict, reason = verdicts.get(s.id, ("", ""))
                if not verdict:
                    stats.skipped_no_verdict += 1
                    continue
                if verdict == "fail":
                    dispute_ids.append(s.id)
                    stats.llm_blocked += 1
                    reasons[s.id] = (
                        f"模型二审不通过：{reason}" if reason else "模型二审不通过"
                    )
                    if len(stats.samples) < 30:
                        stats.samples.append(
                            {
                                "id": s.id,
                                "stage": "llm",
                                "original": s.original,
                                "translation": s.translation,
                                "reason": reason,
                            }
                        )
                else:
                    local_pass.append(s.id)
        else:
            local_pass = [s.id for s in to_llm]

        stats.passed = len(local_pass)
        stats.disputed = len(dispute_ids)
        self._log(
            f"判定完成：通过 {len(local_pass)} / 打回 {len(dispute_ids)}"
            f"（QA {stats.qa_blocked} · 模型 {stats.llm_blocked} · 空译文 {stats.empty_translation}"
            f" · 未判定 {stats.skipped_no_verdict} · 失败批 {stats.failed_batches}）"
        )

        # 3) 写回 stage
        if dry_run:
            stats.written = len(dispute_ids) + len(local_pass) + len(reset_ids)
            return stats

        def _write(ids: list[int], stage: Stage, action: str, label: str) -> None:
            """写一批 stage，并按**平台报告改动的条数**累加 stats.written。

            不能用 `len(res)`：平台有只回 `{"update": N}` 的形状，那时 id 列表
            为空而 stage 已经改完，报表会写成「写回 0 条」。
            """
            res = self.client.set_stage(ids, stage)
            stats.written += res.reported
            for sid in ids:
                self.store.ledger_mark(self.name, str(sid), action)
            if res.reported > len(res):
                # 条数对不上 id 明细时留痕：数字是真的，但具体改了哪些查不到
                self._log(f"{label}：平台报告更新 {res.reported} 条，未返回 id 明细")

        if dispute_ids:
            _write(dispute_ids, Stage(cfg.fail_stage), "dispute", "打回")
        if reset_ids:
            _write(reset_ids, Stage.UNTRANSLATED, "reset_empty", "重置空译文")
        if local_pass:
            _write(local_pass, Stage(cfg.pass_stage), "pass", "通过")
        self.store.ledger_commit()
        # 用平台实际更新的条数，而不是"我们打算改多少"
        self._log(f"stage 写回完成：{stats.written} 条")

        # 4) 打回明细发到平台讨论区
        # 放在 stage 写回**之后**：发帖失败绝不能连累打回本身。
        # 平台没有词条级评论接口（见 client.create_issue docstring），所以整批
        # 汇成一条讨论帖：首次建帖，之后每次追加回复，帖子成为可追溯的打回台账。
        if comment_on_fail and dispute_ids:
            by_id = {s.id: s for s in targets}
            body = self._digest_markdown(dispute_ids, by_id, reasons, stats)
            try:
                existing = self._find_digest_thread(stats)
                if existing is not None:
                    res = self.client.reply_issue(int(existing["id"]), body)
                    stats.issue_action = "reply"
                    stats.issue_id = int(existing["id"])
                else:
                    res = self.client.create_issue(_DIGEST_TITLE, body)
                    stats.issue_action = "create"
                    stats.issue_id = int(res.get("id") or 0)
                if not stats.issue_id:
                    # 平台没回 id：帖子可能建了也可能没建，留痕别静默
                    stats.issue_error = f"平台未返回讨论 id：{str(res)[:120]}"
            except Exception as exc:  # noqa: BLE001 - 任何传输/接口错误都不中断流水线
                stats.issue_error = f"{type(exc).__name__}: {str(exc)[:200]}"

        return stats

    # ---------------------------------------------------------------- 帖子

    def _find_digest_thread(self, stats: "ReviewStats | None" = None) -> dict | None:
        """在「讨论中」的列表里找自己的打回台账帖。

        查不到（真的没有）返回 None；**接口不可用也返回 None，但会留痕**：
        这时新建帖子可能多出一条同名台账帖，和 styleguide 重复发公告是同一类
        风险。区别在于这里不能中止 —— 校对本身已经做完了，为了发不发帖把整轮
        结果废掉更亏。所以折中：照发，但把原因写进 `stats.issue_error`。
        """
        try:
            issues = self.client.list_issues(status=0)
        except Exception as exc:  # noqa: BLE001
            if stats is not None:
                stats.issue_error = (
                    "无法确认是否已有打回台账帖（"
                    + f"{type(exc).__name__}: {str(exc)[:120]}"
                    + "），本次可能新建了重复的一条"
                )
            return None
        for it in issues:
            if str(it.get("title") or "").strip() == _DIGEST_TITLE:
                return it
        return None

    def _digest_markdown(
        self,
        dispute_ids: Sequence[int],
        by_id: dict[int, StringItem],
        reasons: dict[int, str],
        stats: "ReviewStats",
        *,
        max_rows: int = _DIGEST_MAX_ROWS,
    ) -> str:
        """打回明细表。人工复查的人只看得到 stage=2，所以理由必须自带原文/译文摘要。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        head = [
            f"### 自动校对 · {now}",
            "",
            f"- 本批判回 **{len(dispute_ids)}** 条"
            f"（确定性 QA {stats.qa_blocked} · 模型二审 {stats.llm_blocked} · 空译文 {stats.empty_translation}）",
            "- 词条状态已置为「有疑问(2)」，修正译文后请改回「已翻译(1)」。",
        ]
        body = [
            "",
            "| 词条 ID | key | 打回原因 | 原文 | 当前译文 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for sid in dispute_ids[:max_rows]:
            item = by_id.get(sid)
            key = item.key if item else ""
            original = _clip(item.original, 60) if item else ""
            translation = _clip(item.translation, 60) if item else ""
            reason = _clip(reasons.get(sid, "未通过自动校对"), 60)
            body.append(
                f"| {sid} | `{_cell(key, 40)}` | {_cell(reason, 60)} "
                f"| {_cell(original, 60)} | {_cell(translation) or '（空）'} |"
            )
        if len(dispute_ids) > max_rows:
            body.append("")
            body.append(f"> 共 {len(dispute_ids)} 条，此处只列前 {max_rows} 条，其余见任务日志。")
        return "\n".join(head + body)[:_DIGEST_MAX_CHARS]

    # ---------------------------------------------------------------- 评论
    @staticmethod
    def _comment_text(item: StringItem, reason: str) -> str:
        """单条打回说明（保留给工具/脚本复用；流水线已改为整批台账帖）。

        人工复查的人只看得到 stage=2，所以正文必须自带上下文，
        不能只写"不合格"。
        """
        text = (
            f"[自动校对] 打回原因：{reason}\n"
            f"原文：{_clip(item.original)}\n"
            f"当前译文：{_clip(item.translation) or '（空）'}\n"
            f"确认无误请修正译文后把状态改回「已翻译(1)」。"
        )
        return text[:MAX_COMMENT_CHARS]
