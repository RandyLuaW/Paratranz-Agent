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
from typing import TYPE_CHECKING, Sequence

from ..config import AgentConfig
from ..llm import BaseLLM, LLMError, LLMNotConfigured, build_messages
from ..models import Stage, StringItem
from ..qa import Severity, check_placeholders, run_qa

if TYPE_CHECKING:  # pragma: no cover
    from ..client import ParaTranzClient
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


@dataclass
class TranslateStats:
    fetched: int = 0
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
    # 平台术语同步结果 {"ok":bool,"reason":str,"count":int,"retired":int}；空 = 未同步
    terms_sync: dict = field(default_factory=dict)
    # 需要运维看见的告警（复查不可用、写回中断等），不静默
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    blocked_samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "fetched": self.fetched,
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
    ):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.llm = llm
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
        max_items: int = 20000,
        stats: "TranslateStats | None" = None,
    ) -> list[StringItem]:
        files = self.cfg.translate.files
        file_ids: list[int] | None = None
        if files:
            file_ids = [f.id for f in self.client.list_files() if any(a in f.name for a in files)]
            if not file_ids:
                # 白名单配了但一个都没匹配到 —— 绝不能把空列表传给 iter_strings，
                # 那里 `for fid in file or []` 会退化成"不传 file 参数"= 全项目翻译。
                if stats is not None:
                    stats.files_unmatched = 1
                return []

        out: list[StringItem] = []
        for s in self.client.iter_strings(
            file=file_ids, stage=Stage.UNTRANSLATED, max_items=max_items
        ):
            if len(s.original.strip()) < self.cfg.translate.min_length:
                if stats is not None:
                    stats.skipped_short += 1
                continue
            if s.translation.strip():
                continue
            out.append(s)
            if limit and len(out) >= limit:
                break
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
        self, batch: list[StringItem], *, terms: dict[str, dict]
    ) -> tuple[list[dict], list[dict], int]:
        """单批翻译。返回 (待写入, 被拦样例, 失败条数)。

        刻意不在这里改 stats —— 线程池并发时共享累加会丢计数，
        统计由主线程按返回值合并。
        """
        try:
            raw = self.llm.chat(self._build_messages(batch, terms=terms), json_mode=True)
            mapping = self._parse(raw)
        except LLMNotConfigured:
            raise  # 配置错误必须冒泡，不能当成"这批失败了"
        except LLMError:
            return [], [], len(batch)

        writes: list[dict] = []
        blocked: list[dict] = []
        failed = 0
        for s in batch:
            tr = mapping.get(s.id)
            if tr is None:
                failed += 1
                continue
            # 硬拦：占位符/控制符不一致，或空译文 —— 不写回
            hard = check_placeholders(s.original, tr)
            if hard or not tr.strip():
                if len(blocked) < 20:
                    blocked.append(
                        {
                            "id": s.id,
                            "original": s.original,
                            "translation": tr,
                            "reason": "; ".join(i.message for i in hard) or "空译文",
                        }
                    )
                else:
                    blocked.append({"id": s.id, "reason": "…"})
                continue
            writes.append({"id": s.id, "translation": tr, "stage": int(Stage.TRANSLATED)})
        return writes, blocked, failed

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

            # 平台正常返回写入成功的 id 列表；非列表响应（2xx 但无 body）时
            # batch_edit_strings 返回空列表 —— 请求已被接受，按整块记账。
            ids = list(done) if done else [w["id"] for w in part]
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
                self.store.ledger_mark(self.name, str(i), "write", "mt")
            self.store.ledger_commit()
            stats.written += len(ids)

    def run(
        self,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        sync_terms: bool = True,
    ) -> TranslateStats:
        stats = TranslateStats(dry_run=dry_run)
        if sync_terms:
            # 术语同步失败不该让翻译整轮不动：结果记进 stats，谁看报表谁发现。
            stats.terms_sync = self.sync_terms_from_platform(raise_on_error=False)

        targets = self._collect(limit=limit, stats=stats)
        stats.fetched = len(targets)
        if not targets:
            return stats

        # 术语表只取一次：QA 与每批 prompt 的 glossary 共用同一份快照，
        # 不再每批 term_all() 重读全表（9087 条 ÷ 40 条/批 = 227 次）
        terms = self.store.term_all()
        todo: list[StringItem] = []
        writes: list[dict] = []

        # 1) TM 精确命中直出（省一次模型调用）
        for s in targets:
            exact = self.store.tm_exact(s.original)
            if exact:
                report = run_qa(s.original, exact, terms=terms, block_on=Severity.ERROR)
                if report.ok:
                    writes.append({"id": s.id, "translation": exact, "stage": int(Stage.TRANSLATED)})
                    stats.tm_hit += 1
                    continue
            todo.append(s)

        # 2) 其余走模型
        size = max(1, self.cfg.translate.batch_size)
        batches = [todo[i : i + size] for i in range(0, len(todo), size)]
        stats.llm_batches = len(batches)
        results = self.llm.map_concurrent(
            batches,
            partial(self._translate_batch, terms=terms),
            concurrency=self.cfg.llm.concurrency,
        )
        for batch, res in zip(batches, results, strict=True):
            # map_concurrent 把异常装在结果里，这里必须显式处理，
            # 否则「未配置 API」会被当成"0 条成功"静默通过
            if isinstance(res, LLMNotConfigured):
                raise res
            if isinstance(res, Exception):
                stats.failed += len(batch)
                continue
            if isinstance(res, tuple):
                w, blocked, failed = res
                writes.extend(w)
                stats.blocked += len(blocked)
                stats.blocked_samples.extend(b for b in blocked if "original" in b)
                stats.failed += failed
        # writes 里前段是 TM 命中，后段才是 LLM 产出；直接取 len 会双计
        stats.llm_ok = max(0, len(writes) - stats.tm_hit)

        # 3) 写回（人工译文保护 + 分块提交，块成功即记账）
        if dry_run or not writes:
            stats.written = len(writes)  # dry-run 时报"将写入多少"
        else:
            by_id = {s.id: s for s in targets}
            # force_overwrite 以前是死配置：默认 false 却从不读取，MT 译文会直接
            # 盖掉取材之后别人译好的条目。默认走复查，显式 true 才不做。
            if not bool(getattr(self.cfg.translate, "force_overwrite", False)):
                writes = self._protect_manual(writes, by_id, stats)
            if writes:
                self._write_chunks(writes, by_id, stats)

        return stats
