"""内容汉化（机器翻译 + 翻译记忆 + 术语约束）。

流水线：
    拉未翻译词条 → 本地 TM 精确命中直出 → 其余分批送 LLM
    → 逐条确定性 QA（占位符/数字/空译文硬拦） → batchOperateStrings 写回 stage=1
    → 写入本地 TM + 记账本

为什么先过 QA 再写回：平台对"译文与原文相同"的导入会静默跳过，与其让平台
丢弃，不如在本地就判死并留档，方便下一轮只重试真正需要重试的条目。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Callable, Sequence

from ..client import ParaTranzClient
from ..errors import BudgetExceeded
from ..config import AgentConfig
from ..filematch import matches_any
from ..llm import BaseLLM, LLMError, LLMNotConfigured, build_messages
from ..models import Stage, StringItem
from ..qa import Severity, check_placeholders, run_qa

if TYPE_CHECKING:  # pragma: no cover
    from ..store import Store

SYSTEM_PROMPT = """你是一名资深游戏本地化译者，负责把英文游戏文本译成简体中文。

硬性规则：
1. 只输出译文，不解释、不加注、不翻译控制符。
2. 占位符与控制符必须原样保留且数量一致：\\N[1] \\V[1] \\C[2] \\I[3] \\FS[20] %s %d {0} {name} <b> 等。
3. 术语表给出的译法必须使用，不得同义替换。
4. 保留原文的首尾空白与换行数量。
5. 数字、单位、编号保持原值。
6. 语气贴合游戏语境：对话自然口语，系统提示简洁，道具/技能名名词化。
7. 译文不要出现英文原词（专有名词、UI 缩写除外）。

输出格式：严格 JSON，形如 {"items":[{"id":<原样回传>,"translation":"<译文>"}]}，顺序与输入一致，不得遗漏条目。"""


# 平台 batchOperateStrings 单次上限（与 client.BATCH_LIMIT 对齐，测试里有断言守着）。
# 这里自己切块而不是交给 batch_edit_strings 内部分块：那个方法第 2 块抛错时
# 第 1 块已经写进平台，而调用方拿不到任何账。
WRITE_CHUNK = 500

# 单轮取材条数上限。`iter_strings` 到上限只是安静地 `return`，不告诉调用方
# 「后面还有」—— 触到上限时把 truncated 写进 stats，否则报表会假装全处理完了。
DEFAULT_MAX_ITEMS = 20000

# 逐条事件的单片大小：一次推几千条会让 SSE 事件体积失控（界面一次也只显示一屏）
ITEM_EVENT_CHUNK = 50

# 交给支持 structured outputs 的网关用的严格 schema（strict:true 时 OpenAI
# 与中转站都要求 additionalProperties:false 且每个属性都在 required 里）。
# 有它的好处是输出不会被截成"半句 JSON"；网关不支持时由 llm 层自动降档。
TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "translation": {"type": "string"},
                },
                "required": ["id", "translation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass
class TranslateStats:
    fetched: int = 0
    # 取材被 max_items 截断：本轮只处理前 max_items 条，项目里还有剩余。
    # 缺这两个字段时「只翻了前 2 万条」会被报成「全翻完了」。
    truncated: bool = False
    max_items: int = 0
    tm_hit: int = 0
    llm_batches: int = 0
    llm_ok: int = 0
    written: int = 0
    blocked: int = 0
    failed: int = 0
    # 短于 min_length 被跳过（永远停在 stage=0，必须让用户看见）
    skipped_short: int = 0
    # translate.files 白名单一个都没匹配到（此时刻意不翻任何东西）
    files_unmatched: int = 0
    # 写回前复查发现"已被别人动过"（stage!=0 或已有译文）而放过的人工译文
    skipped_manual: int = 0
    # 复查本身不可用（网络/限流）而保守跳过的条数 —— 非零说明这轮没写任何东西
    manual_risk: int = 0
    # 写回失败的分块数（成功的块已经记账，见 WriteInterrupted）
    write_chunks_failed: int = 0
    # 撞到单任务预算（stop 策略）而提前停下：必须让人看出"是钱花完了，不是翻译失败"
    budget_stopped: bool = False
    # 模型调用的**具体失败原因**（去重、最多留 5 条）。
    # 以前这里只累加 failed 计数，原因被 `except LLMError: return [], [], len(batch)`
    # 直接吞掉：任务日志只剩「处理 0 条」，用户完全看不出是网关 400、
    # 是超时、还是模型名不存在 —— "看起来成功了但事实不是那样"。
    errors: list[str] = field(default_factory=list)
    # 演练模式下的"会写入多少条"。`written` 表示真的写进平台了多少，
    # 演练时恒为 0：两者混用一个字段时，Agent/脚本读到 written>0 会以为已经落库。
    would_write: int = 0
    # 平台术语同步结果 {"ok":bool,"reason":str,"count":int,"retired":int}；空 = 未同步
    terms_sync: dict = field(default_factory=dict)
    # 需要运维看见的告警（复查不可用、写回中断等），不静默
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    blocked_samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "truncated": self.truncated,
            "max_items": self.max_items,
            "tm_hit": self.tm_hit,
            "llm_batches": self.llm_batches,
            "llm_ok": self.llm_ok,
            "written": self.written,
            "blocked": self.blocked,
            "failed": self.failed,
            "skipped_short": self.skipped_short,
            "files_unmatched": self.files_unmatched,
            "skipped_manual": self.skipped_manual,
            "manual_risk": self.manual_risk,
            "write_chunks_failed": self.write_chunks_failed,
            "budget_stopped": self.budget_stopped,
            "would_write": self.would_write,
            "errors": list(self.errors),
            "terms_sync": self.terms_sync,
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }


class WriteInterrupted(RuntimeError):
    """写回中途失败。

    已成功的块**已经写入平台并回填 TM + 账本**，所以这里不吞异常、也不假装
    整轮成功：异常里带上"已写入 N 条"，并把部分成功的 `stats` 挂上去，
    让调用方既能报错又能拿到报表（下一轮靠 TM 命中把这些条目再走一遍）。
    """

    def __init__(self, message: str, stats: TranslateStats):
        super().__init__(message)
        self.stats = stats


class TranslatePipeline:
    name = "translate"

    def __init__(
        self,
        client: "ParaTranzClient",
        cfg: AgentConfig,
        store: "Store",
        llm: BaseLLM,
        system_prompt: str | None = None,
        budget_check: Callable[[], None] | None = None,
    ):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.llm = llm
        # 任务内预算检查点：由 server 层注入（CLI 不注入 = 不检查，行为不变）
        self.budget_check = budget_check
        # 自定义提示词；空串表示用模块内置默认值
        self.system_prompt = (system_prompt or "").strip() or SYSTEM_PROMPT

    # ------------------------------------------------------------------ 术语
    def sync_terms_from_platform(self, *, raise_on_error: bool = True) -> dict:
        """把平台术语表拉进本地，供 QA 与 prompt 使用。

        返回 `{"ok": bool, "reason": str, "count": int, "retired": int}`，**不再吞异常**：
        - `reason="forbidden"` / `"not_found"`：术语表对该成员不可见（403/404，
          如 termsMode=1 且非成员）。这是常态而非故障，返回 ok=False 不抛，
          且**不做任何清理**（拿不到完整表就没有差集的依据）；
        - `reason="error"`：超时/5xx 等真失败。默认抛出（谁都别当没事发生）；
          `raise_on_error=False` 时返回 ok=False，由调用方决定怎么处置。

        同步成功后按 `source="platform"` 做差集，把平台上已删除/改名的术语
        在本地置为 stale —— 否则 `check_terms` 会一直按旧译法报 term_violation。
        """
        from ..client import ParaTranzError

        seen: set[str] = set()
        n = 0
        try:
            for t in self.client.iter_terms():
                self.store.term_upsert(
                    t.term,
                    t.translation,
                    pos=t.pos,
                    note=t.note,
                    variants=t.variants,
                    source="platform",
                )
                seen.add(t.term)
                n += 1
        except Exception as exc:  # noqa: BLE001 - 需要区分"不可见"与"真失败"，见 docstring
            self.store.term_commit()
            status = getattr(exc, "status", None) if isinstance(exc, ParaTranzError) else None
            if status in (403, 404):
                return {
                    "ok": False,
                    "reason": "forbidden" if status == 403 else "not_found",
                    "count": n,
                    "retired": 0,
                }
            if raise_on_error:
                raise
            return {
                "ok": False,
                "reason": "error",
                "count": n,
                "retired": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

        self.store.term_commit()
        retired, note = self._retire_stale_terms(seen)
        out = {"ok": True, "reason": "ok", "count": n, "retired": retired}
        if note:
            out["note"] = note
        return out

    def _retire_stale_terms(self, platform_terms: set[str]) -> tuple[int, str]:
        """把平台上已不存在的 `source="platform"` 术语置为 stale。

        store 只有 `term_upsert` 没有 `term_delete`，所以退的是**状态**而不是行：
        `term_upsert(..., status="stale")` 之后 `term_all()`（默认只取 active）
        就查不到它，QA 与 prompt 立刻不受影响；将来平台再出现同名术语，
        upsert 会把 status 改回 active，天然可逆。

        平台返回空表但本地有平台术语时**不清理**：那多半是接口/权限异常返回了
        空列表，批量置 stale 等于把整张术语表废掉。
        """
        local = self.store.term_all()
        stale = [
            term
            for term, meta in local.items()
            if meta.get("source") == "platform" and term not in platform_terms
        ]
        if not stale:
            return 0, ""
        if not platform_terms:
            return 0, "empty_platform"
        for term in stale:
            meta = local[term]
            self.store.term_upsert(
                term,
                meta.get("translation") or "",
                pos=meta.get("pos"),
                note=meta.get("note"),
                variants=meta.get("variants") or (),
                source="platform",
                status="stale",
            )
        self.store.term_commit()
        return len(stale), ""

    def _glossary_for(
        self, texts: Sequence[str], limit: int = 40, *, terms: dict[str, dict]
    ) -> list[dict]:
        # enforce_terms=false 时不把术语塞进 prompt，让模型自由发挥
        # （校对阶段的 term_violation 仍会按术语表检查，只是不再强推）
        if not self.cfg.translate.enforce_terms:
            return []
        # terms 由 run() 取一次传进来：术语表全表读不能按批重复 227 次
        return self.store.glossary_for(texts, terms=terms, limit=limit, with_pos=True)

    # ------------------------------------------------------------------ 取材
    def _collect(
        self,
        *,
        limit: int | None = None,
        max_items: int = DEFAULT_MAX_ITEMS,
        stats: "TranslateStats | None" = None,
        ids: Sequence[int] | None = None,
    ) -> list[StringItem]:
        """采集本轮要翻的词条。

        取到的条数顶到 `max_items` 就把 `stats.truncated` 置位并留下告警：
        `iter_strings` 到上限只是安静地停下，不记这一笔的话报表会声称
        整个项目「已经全处理完了」（真实项目 2 万条以上就会撞上）。

        `ids` 是「人工点名要重翻的条目」：给了就**只**翻这些，不看阶段、不看
        文件白名单、也不套 min_length —— 人已经明确指了这几条，软件再自作主张
        过滤掉就是在违背指令。代价是每条一次 GET（平台没有按 id 批量读的接口）。
        """
        if ids:
            picked: list[StringItem] = []
            missing = 0
            for sid in list(ids)[:max_items]:
                try:
                    s = self.client.get_string(int(sid))
                except Exception:  # noqa: BLE001 - 单条读不到不该整轮失败
                    missing += 1
                    continue
                picked.append(s)
                if limit and len(picked) >= limit:
                    break
            if stats is not None and missing:
                stats.warnings.append(f"点名重翻的 {missing} 条在平台上读不到（可能已删除），已跳过")
            return picked

        files = self.cfg.translate.files
        file_ids: list[int] | None = None
        if files:
            # 与回写共用同一条白名单语义（精确相等或以 `项/` 开头）。
            # 子串匹配下 `files: ["a"]` 会选中所有含 a 的文件名。
            file_ids = [f.id for f in self.client.list_files()
                        if matches_any(f.name, files)]
            if not file_ids:
                # 白名单配了但一个都没匹配到 —— 绝不能把空列表传给 iter_strings，
                # 那里 `for fid in file or []` 会退化成"不传 file 参数"= 全项目翻译。
                if stats is not None:
                    stats.files_unmatched = 1
                return []

        out: list[StringItem] = []
        taken = 0
        for s in self.client.iter_strings(
            file=file_ids, stage=Stage.UNTRANSLATED, max_items=max_items
        ):
            taken += 1
            if len(s.original.strip()) < self.cfg.translate.min_length:
                if stats is not None:
                    stats.skipped_short += 1
                continue
            if s.translation.strip():
                continue
            out.append(s)
            if limit and len(out) >= limit:
                break
        if stats is not None and max_items and taken >= max_items:
            stats.truncated = True
            stats.max_items = max_items
            stats.warnings.append(
                f"取材被上限截断：本轮只取前 {max_items} 条未翻译词条，项目里还有剩余；"
                f"写回后这些词条不再是 stage=0，再跑一轮会接着处理"
            )
        return out

    # ------------------------------------------------------------------ 翻译
    def _build_messages(
        self, batch: list[StringItem], *, terms: dict[str, dict]
    ) -> list[dict]:
        refs: list[dict] = []
        seen: set[str] = set()
        for s in batch:
            for hit in self.store.tm_search(
                s.original,
                top_k=self.cfg.translate.tm_top_k,
                min_score=self.cfg.translate.tm_min_score,
                # 只在「本项目 + 通用池」里找：跨项目的机翻串味会直接写进平台
                project=self.client.project_id,
            ):
                if hit.source in seen:
                    continue
                seen.add(hit.source)
                refs.append({"source": hit.source, "target": hit.target, "score": round(hit.score, 2)})
        payload = {
            "style": self.cfg.project.style,
            "source_lang": self.cfg.project.source_lang,
            "target_lang": self.cfg.project.target_lang,
            "glossary": self._glossary_for([s.original for s in batch], terms=terms),
            "references": refs[:60],
            "items": [
                {
                    "id": s.id,
                    "original": s.original,
                    **({"context": s.context} if s.context else {}),
                }
                for s in batch
            ],
        }
        return build_messages(self.system_prompt, payload)

    def _parse(self, raw: str) -> dict[int, str]:
        from ..llm import extract_json

        data = extract_json(raw)
        items = data.get("items") if isinstance(data, dict) else data
        out: dict[int, str] = {}
        for it in items or []:
            if not isinstance(it, dict):
                continue
            try:
                sid = int(it["id"])
            except (KeyError, TypeError, ValueError):
                continue
            tr = it.get("translation")
            if isinstance(tr, str):
                out[sid] = tr
        return out

    def _translate_batch(
        self, batch: list[StringItem], *, terms: dict[str, dict], on_item=None,
        budget_check=None,
    ) -> tuple[list[dict], list[dict], int, str]:
        """单批翻译。返回 (待写入, 被拦样例, 失败条数, 失败原因)。

        刻意不在这里改 stats —— 线程池并发时共享累加会丢计数，
        统计由主线程按返回值合并；失败原因同样按返回值回到主线程，
        这样 `except` 里吞掉的信息还能被报表拿到。

        `budget_check` 在**每批模型调用前**执行：以前预算只在任务开始时查一次，
        一次翻几千条的长任务中途超支没人管，跑完才在报表里看到花了多少。
        """
        if on_item is not None:
            on_item(
                [
                    {"id": s.id, "original": s.original, "translation": "", "status": "running"}
                    for s in batch
                ]
            )
        if budget_check is not None:
            try:
                budget_check()
            except BudgetExceeded as exc:
                return [], [], len(batch), f"BudgetExceeded: {exc}"
        try:
            raw = self.llm.chat(
                self._build_messages(batch, terms=terms),
                json_mode=True,
                json_schema=TRANSLATE_SCHEMA,
            )
            mapping = self._parse(raw)
        except LLMNotConfigured:
            raise  # 配置错误必须冒泡，不能当成"这批失败了"
        except LLMError as exc:
            # 整批失败也要给这批每一条一个终态：否则界面会永远停在"处理中"，
            # 任务已经 failed 了，人还看着一堆在飞的行 —— 这也是假象。
            if on_item is not None:
                on_item(
                    [
                        {
                            "id": s.id,
                            "original": s.original,
                            "translation": "",
                            "status": "failed",
                            "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
                        }
                        for s in batch
                    ]
                )
            return [], [], len(batch), f"{type(exc).__name__}: {exc}"

        writes: list[dict] = []
        blocked: list[dict] = []
        failed = 0
        # 逐条上报给界面用的事件（ok / blocked / failed）。写回是否成功在
        # `_write_chunks` 里另有一轮 status 更新，这里只说"模型产出了什么"。
        final: list[dict] = []
        for s in batch:
            tr = mapping.get(s.id)
            if tr is None:
                failed += 1
                final.append(
                    {"id": s.id, "original": s.original, "translation": "",
                     "status": "failed", "reason": "模型没有返回这一条"}
                )
                continue
            # 硬拦：占位符/控制符不一致，或空译文 —— 不写回
            hard = check_placeholders(s.original, tr)
            if hard or not tr.strip():
                reason = "; ".join(i.message for i in hard) or "空译文"
                if len(blocked) < 20:
                    blocked.append(
                        {
                            "id": s.id,
                            "original": s.original,
                            "translation": tr,
                            "reason": reason,
                        }
                    )
                else:
                    blocked.append({"id": s.id, "reason": "…"})
                final.append(
                    {"id": s.id, "original": s.original, "translation": tr,
                     "status": "blocked", "reason": reason}
                )
                continue
            writes.append({"id": s.id, "translation": tr, "stage": int(Stage.TRANSLATED)})
            final.append(
                {"id": s.id, "original": s.original, "translation": tr, "status": "ok"}
            )
        if on_item is not None:
            on_item(final)
        return writes, blocked, failed, ""

    # ------------------------------------------------------------------ 写回
    def _files_for(self, writes: list[dict], by_id: dict[int, StringItem]) -> list[int] | None:
        """复查要扫哪些文件；有任何一个 id 的文件未知就退化成全项目扫。"""
        files: set[int] = set()
        for w in writes:
            item = by_id.get(w["id"])
            fid = item.file_id if item is not None else None
            if fid is None:
                return None
            files.add(int(fid))
        return sorted(files) or None

    def _protect_manual(
        self,
        writes: list[dict],
        by_id: dict[int, StringItem],
        stats: TranslateStats,
    ) -> list[dict]:
        """写回前复查：只保留"现在仍是未翻译且无译文"的条目。

        平台**没有条件更新**（无法"仅当 stage=0 时写入"），所以保护人工译文
        只能在写回前做一次读侧复查。客户端 `list_strings` 不支持按 id 过滤，
        因此按文件 + stage=0 整批扫一遍（分页 500），而不是逐条 `get_string`
        —— N 条就是 N 次请求，代价不可接受。

        复查只取一次快照，`stage=0 且 translation 为空` 的 id 才允许写：
        期间有人置了 stage 或填了译文，该条就不在快照里，直接放过并计数。
        `stage` 这里自己做二次判定，不单靠服务端过滤（平台若忽略 stage 参数，
        条件就不成立）。

        复查本身失败时**保守跳过全部写入**（`manual_risk` 记账并在 warnings 里
        留言）：force_overwrite=false 的语义是"别覆盖人工译文"，确认不了就不动手；
        写回可重放，下一轮重试即可。
        """
        try:
            file_ids = self._files_for(writes, by_id)
            live = {
                s.id
                for s in self.client.iter_strings(file=file_ids, stage=Stage.UNTRANSLATED)
                if s.stage == int(Stage.UNTRANSLATED) and not s.translation.strip()
            }
        except Exception as exc:  # noqa: BLE001 - 复查失败不能把整轮翻译带崩
            stats.manual_risk = len(writes)
            stats.warnings.append(
                f"人工译文复查不可用（{type(exc).__name__}: {exc}），"
                f"按 force_overwrite=false 保守跳过 {len(writes)} 条写回"
            )
            return []
        kept = [w for w in writes if w["id"] in live]
        skipped = len(writes) - len(kept)
        if skipped:
            stats.skipped_manual += skipped
            stats.warnings.append(f"{skipped} 条已被他人翻译/改过 stage，按 force_overwrite=false 放过")
        return kept

    def _write_chunks(
        self,
        writes: list[dict],
        by_id: dict[int, StringItem],
        stats: TranslateStats,
    ) -> None:
        """按 500 分块写回，**每块成功立刻回填 TM + 账本并累加 written**。

        不这么做的话：第 2 块抛错时第 1 块已经写进平台，而 TM / 账本 / 统计
        全都停在异常之前 —— 调用方只拿到一个异常，等于"平台写了一部分，
        本地一无所知"，下一轮要么重复劳动要么覆盖。

        某块失败时中止后面的块（同一次平台故障下继续写只会连着失败），
        但**已完成部分的记账保留**，并把部分成功的 stats 挂到异常上抛出。
        """
        for start in range(0, len(writes), WRITE_CHUNK):
            part = writes[start : start + WRITE_CHUNK]
            try:
                done = self.client.batch_edit_strings(part, chunk=WRITE_CHUNK)
            except Exception as exc:  # noqa: BLE001 - 中途失败必须先把已写入的账保住
                unwritten = len(writes) - start
                stats.failed += unwritten
                stats.write_chunks_failed += 1
                stats.warnings.append(
                    f"写回中断（{type(exc).__name__}: {exc}）：已写入 {stats.written} 条并已记账，"
                    f"{unwritten} 条未写入"
                )
                raise WriteInterrupted(
                    f"写回中断：已写入 {stats.written} 条（已回填 TM 与账本），"
                    f"{unwritten} 条未写入（{type(exc).__name__}: {exc}）",
                    stats,
                ) from exc

            # 平台回的 id 明细见 client.BatchWrite：id 数组、`{"update": N}`
            # 与 2xx 空 body 都能落到确定的 id 上（整块都成功时按提交的 id 补全）。
            # 补不全说明平台报告的条数和本块提交数对不上 —— 哪些落库无从判断，
            # 整块按未写入对待（下一轮幂等重试），绝不能把没写的记成写了。
            ids = list(done)
            if not ids:
                stats.failed += len(part)
                stats.write_chunks_failed += 1
                stats.warnings.append(
                    f"写回结果无法确认：平台报告更新 {done.reported} 条，本块提交 {len(part)} 条，"
                    f"整块按未写入处理（下一轮重试）"
                )
                continue
            if len(ids) < len(part):
                # 平台只确认了一部分：差额既不在 written 也不在 failed 里就会
                # 「凭空消失」（提交 40、写入 37、失败 0）。按未写入记账并留痕。
                missing_n = len(part) - len(ids)
                stats.failed += missing_n
                stats.warnings.append(
                    f"写回结果不完整：本块提交 {len(part)} 条，平台只确认 {len(ids)} 条，"
                    f"{missing_n} 条按未写入处理（下一轮重试）"
                )
            targets_of = {w["id"]: w["translation"] for w in part}
            self.store.tm_add_many(
                [
                    (by_id[i].original, targets_of[i])
                    for i in ids
                    if i in by_id and i in targets_of
                ],
                project=self.client.project_id,
                origin="mt",
            )
            for i in ids:
                self.store.ledger_mark(
                    self.name, str(i), "write", "mt", project=self.client.project_id
                )
            self.store.ledger_commit()
            stats.written += len(ids)

    def run(
        self,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        sync_terms: bool = True,
        max_items: int = DEFAULT_MAX_ITEMS,
        on_items: Callable[[list[dict]], None] | None = None,
        budget_check: Callable[[], None] | None = None,
        ids: Sequence[int] | None = None,
    ) -> TranslateStats:
        """跑一轮翻译。

        `on_items` 是逐条回调（WebUI 的"正在处理的词条"面板靠它）：
        每批开始时推一次 `status="running"`，出结果后推一次终态
        （ok / blocked / failed），写回后再推一次写成 `written`。
        它只做展示，不参与任何判定 —— 失败与否看 stats。
        """
        budget_check = budget_check or self.budget_check
        stats = TranslateStats(dry_run=dry_run)
        # 逐条事件要带上"这是演练"：演练模式下模型确实产出了译文，但**平台没变**，
        # 界面上如果把预览当结果写进列表，用户会以为已经落库。
        if on_items is not None and dry_run:
            _sink = on_items
            on_items = lambda rows: _sink([{**r, "dry_run": True} for r in rows])  # noqa: E731
        if sync_terms:
            # 术语同步失败不该让翻译整轮不动：结果记进 stats，谁看报表谁发现。
            stats.terms_sync = self.sync_terms_from_platform(raise_on_error=False)

        targets = self._collect(limit=limit, max_items=max_items, stats=stats, ids=ids)
        stats.fetched = len(targets)
        if ids:
            # 点名的条目是"我就是要重翻"，不是"帮我找没翻的"：
            # 人工译文保护（force_overwrite=false）在这里拦下来等于违抗指令，
            # 所以跳过它，但把"覆盖了 N 条已有译文"写进报表。
            replaced = sum(1 for s in targets if (s.translation or "").strip())
            if replaced:
                stats.warnings.append(
                    f"按点名重翻：其中 {replaced} 条平台上已有译文，本轮会覆盖它们"
                )
        if not targets:
            return stats

        # 术语表只取一次：QA 与每批 prompt 的 glossary 共用同一份快照，
        # 不再每批 term_all() 重读全表（9087 条 ÷ 40 条/批 = 227 次）
        terms = self.store.term_all()
        todo: list[StringItem] = []
        writes: list[dict] = []
        tm_items: list[dict] = []

        # 1) TM 精确命中直出（省一次模型调用）
        #    **点名重翻时禁用**：用户指了这几条要重翻，TM 里存的正是上一轮的机翻，
        #    直出等于"0 次模型调用把旧译文原样写回"，功能看起来跑了、其实没翻
        #    （审计实测：ids 重翻时 tm_hit=2、模型调用=0）。
        for s in targets:
            exact = None if ids else self.store.tm_exact(
                s.original, project=self.client.project_id
            )
            if exact:
                report = run_qa(s.original, exact, terms=terms, block_on=Severity.ERROR)
                if report.ok:
                    writes.append({"id": s.id, "translation": exact, "stage": int(Stage.TRANSLATED)})
                    stats.tm_hit += 1
                    tm_items.append(
                        {"id": s.id, "original": s.original, "translation": exact,
                         "status": "tm", "reason": "本地记忆命中"}
                    )
                    continue
            todo.append(s)
        if on_items is not None and tm_items:
            for i in range(0, len(tm_items), ITEM_EVENT_CHUNK):
                on_items(tm_items[i : i + ITEM_EVENT_CHUNK])

        # 2) 其余走模型
        size = max(1, self.cfg.translate.batch_size)
        batches = [todo[i : i + size] for i in range(0, len(todo), size)]
        stats.llm_batches = len(batches)
        results = self.llm.map_concurrent(
            batches,
            partial(self._translate_batch, terms=terms, on_item=on_items,
                    budget_check=budget_check),
            concurrency=self.cfg.llm.concurrency,
        )
        for batch, res in zip(batches, results, strict=True):
            # map_concurrent 把异常装在结果里，这里必须显式处理，
            # 否则「未配置 API」会被当成"0 条成功"静默通过
            if isinstance(res, LLMNotConfigured):
                raise res
            if isinstance(res, Exception):
                stats.failed += len(batch)
                self._note_error(stats, f"{type(res).__name__}: {res}")
                if on_items is not None:
                    on_items(
                        [
                            {"id": s.id, "original": s.original, "translation": "",
                             "status": "failed", "reason": f"{type(res).__name__}: {str(res)[:120]}"}
                            for s in batch
                        ]
                    )
                continue
            if isinstance(res, tuple):
                w, blocked, failed, err = res
                if err.startswith("BudgetExceeded"):
                    # 预算拦下的批次不算"模型失败"：单独记，并只提示一次
                    stats.budget_stopped = True
                    if err not in stats.errors and len(stats.errors) < 5:
                        stats.errors.append(err)
                    continue
                writes.extend(w)
                stats.blocked += len(blocked)
                stats.blocked_samples.extend(b for b in blocked if "original" in b)
                stats.failed += failed
                if err:
                    self._note_error(stats, err)
        # writes 里前段是 TM 命中，后段才是 LLM 产出；直接取 len 会双计
        stats.llm_ok = max(0, len(writes) - stats.tm_hit)

        # 网关协商结果（如"不支持 json_object 已改用 …"）必须进报表
        for note in getattr(self.llm, "json_warnings", []) or []:
            if note not in stats.warnings:
                stats.warnings.append(note)

        # 3) 写回（人工译文保护 + 分块提交，块成功即记账）
        if dry_run:
            # 演练：`written` 恒为 0（没落平台就是没写），预估值单独给 `would_write`。
            # 共用一个字段会让 Agent/脚本把"将写入 200 条"当成"已经写进去 200 条"。
            stats.would_write = len(writes)
            if writes:
                stats.warnings.append(
                    "演练模式：would_write 是预估值（未做写前人工译文复查），written 恒为 0"
                )
        elif writes:
            by_id = {s.id: s for s in targets}
            # force_overwrite 以前是死配置：默认 false 却从不读取，MT 译文会直接
            # 盖掉取材之后别人译好的条目。默认走复查，显式 true 才不做。
            # 点名重翻（ids）等同"用户已明确要覆盖"，也不再走人工译文复查。
            if not (bool(getattr(self.cfg.translate, "force_overwrite", False)) or ids):
                writes = self._protect_manual(writes, by_id, stats)
            if writes:
                self._write_chunks(writes, by_id, stats)
                if on_items is not None:
                    # 分片推送：一次塞进几千条会让 SSE 事件体积失控，
                    # 界面一次也只显示一屏（分片后能边收边刷）
                    rows = [
                        {"id": w["id"], "translation": w["translation"], "status": "written"}
                        for w in writes
                        if w["id"] in by_id
                    ]
                    for i in range(0, len(rows), ITEM_EVENT_CHUNK):
                        on_items(rows[i : i + ITEM_EVENT_CHUNK])

        return stats

    @staticmethod
    def _note_error(stats: TranslateStats, message: str) -> None:
        """记下"为什么这批失败了"，去重且最多 5 条（同一网关故障会重复 N 次）。"""
        if message and message not in stats.errors and len(stats.errors) < 5:
            stats.errors.append(message)
