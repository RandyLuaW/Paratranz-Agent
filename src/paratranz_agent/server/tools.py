"""工具注册表：把五条 pipeline 包装成「可被 WebUI 按钮 / Agent 调用的动作」。

统一契约：
    fn(job, runner, **args) -> dict

`job` / `runner` 用于写日志与响应取消；返回值会被塞进 Job.result，
前端直接渲染。所有工具都从 `AppContext` 取 client/store/llm，
所以 WebUI 改配置后立刻生效（context 会按需重建）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from ..client import ParaTranzClient
from ..config import LLMCfg
from ..llm import BaseLLM, UsageEvent, UnavailableLLM, build_llm
from ..pipelines import (
    GlossaryPipeline,
    PlanPipeline,
    ReviewPipeline,
    StyleGuidePipeline,
    TranslatePipeline,
)
from ..store import Store
from .extensions import ExtensionStore
from .jobs import current_job
from .logs import LogStore
from .redact import make_redactor
from .settings import RuntimeSettings, SettingsStore, join_ref
from .usage import BudgetConfig, UsageStore

# --------------------------------------------------------------- 危险动作闸门
# 这几个工具会**在本机执行代码**或**覆盖用户文件**，必须拿到显式风险确认。
#
# 闸门刻意放在**工具层**而不是路由层：同一个工具还有另外两个入口——
# `POST /api/jobs {tool, args}`（通用任务口）与 `/api/agent`（模型给的步骤），
# 只在路由层校验会被绕过（审计实测：改走通用口，提取器照跑、任意代码照执行）。
# 放在这里之后，三条路共用同一道闸。
DANGEROUS_TOOLS = frozenset({"extract", "extractor_generate", "extractor_improve"})


def require_risk_ack(tool: str, accept_risk: bool) -> None:
    """危险工具缺少显式确认时抛 `PermissionError`（任务终态 failed，拦在动手之前）。"""
    if tool in DANGEROUS_TOOLS and not accept_risk:
        raise PermissionError(
            f"{tool} 等于在本机执行任意代码，需要先勾选风险确认（accept_risk=true）"
        )


class AppContext:
    """服务级上下文：按当前 settings 惰性构建 client / store / llm。"""

    def __init__(self, settings_store: SettingsStore, state_dir, mock: bool = False):
        self.settings_store = settings_store
        self.state_dir = state_dir
        self.mock = mock
        self.usage = UsageStore(state_dir / "usage.db")
        self.logs = LogStore(
            state_dir / "logs",
            redactor=make_redactor(
                lambda: [self.settings.token, self.settings.llm_key]
                # provider 的 api_key 也必须是「已知密钥」：SSE / /api/jobs / 异常处理器
                # 都走这个 redactor，漏了它就会把源里的 Key 原文漏进日志与事件流
                + [p.api_key for p in self.settings.providers if p.api_key]
            ),
        )
        self.extensions = ExtensionStore(state_dir)
        self._lock = threading.Lock()
        self._client: ParaTranzClient | None = None
        self._store: Store | None = None
        self._llms: dict[str, BaseLLM] = {}
        self._llm_fps: dict[str, tuple] = {}
        self._fingerprint: tuple | None = None

    @property
    def settings(self) -> RuntimeSettings:
        return self.settings_store.load()

    def _fp(self) -> tuple:
        s = self.settings
        return (s.project_id, s.token, s.base_url, s.llm_base_url, s.llm_model, s.llm_key)

    def _ensure(self) -> None:
        with self._lock:
            fp = self._fp()
            if self._fingerprint == fp and self._client is not None:
                return
            if self._client is not None:
                self._client.close()
            s = self.settings
            if self.mock:
                # 演示/自测：内存假平台，状态落盘，命令之间连续
                from ..fake import FakeParaTranz

                self._client = FakeParaTranz(
                    persist_path=str(self.state_dir / "fake_platform.json")
                ).client()
            else:
                self._client = ParaTranzClient(
                    token=s.token,
                    project_id=s.project_id or 1,
                    base_url=s.base_url,
                    rate_limit_per_min=s.rate_limit_per_min,
                )
            self._llms.clear()
            self._llm_fps.clear()
            if self._store is None:
                self._store = Store(self.state_dir / "agent.db")
            self._fingerprint = fp

    def _on_usage(self, ev: UsageEvent) -> None:
        """每次模型调用后记账：归属到当前 job，并写一条 debug 日志。"""
        job_id, tool = current_job.get()
        info = self.usage.record(
            model=ev.model,
            prompt_tokens=ev.prompt_tokens,
            completion_tokens=ev.completion_tokens,
            cached_tokens=ev.cached_tokens,
            job_id=job_id,
            tool=tool,
            latency_ms=ev.latency_ms,
            ok=ev.ok,
        )
        self.logs.add(
            f"LLM {ev.model} in={ev.prompt_tokens} out={ev.completion_tokens} "
            f"${info['cost_usd']:.6f} {ev.latency_ms}ms",
            level="debug",
            job_id=job_id,
            source="usage",
        )

    @property
    def budget_cfg(self) -> BudgetConfig:
        return BudgetConfig.from_dict(self.settings.budget or {})

    def check_budget(self, *, job_id: str = "") -> dict[str, Any]:
        return self.usage.check_budget(
            self.budget_cfg, job_id=job_id, model=self.settings.llm_model
        )

    @property
    def client(self) -> ParaTranzClient:
        self._ensure()
        assert self._client is not None
        return self._client

    @property
    def store(self) -> Store:
        self._ensure()
        assert self._store is not None
        return self._store

    def llm_of(self, p, m) -> BaseLLM:
        """按 (provider, model) 取实例（测试连接时用）。"""
        if p is None or m is None or not p.api_key:
            return UnavailableLLM()
        return self._build_llm(p, m)

    def llm_for(self, task: str = "") -> BaseLLM:
        """按任务取模型实例。每个 provider::model 一份实例并缓存。"""
        got = self.settings.resolve_for(task)
        if not got:
            return UnavailableLLM()
        p, m = got
        if not p.api_key:
            return UnavailableLLM()
        return self._build_llm(p, m)

    def _build_llm(self, p, m) -> BaseLLM:
        ref = join_ref(p.id, m.id)
        fp = (
            p.base_url, p.api_key, m.id, m.temperature, m.max_tokens,
            p.timeout, p.max_retries, p.concurrency,
            tuple(sorted((m.extra_params or {}).items())),
        )
        cached = self._llms.get(ref)
        if cached is not None and self._llm_fps.get(ref) == fp:
            return cached
        llm = build_llm(
            LLMCfg(
                base_url=p.base_url,
                model=m.id,
                api_key=p.api_key,
                temperature=m.temperature if m.temperature is not None else 0.2,
                max_tokens=m.max_tokens or 0,
                timeout=p.timeout,
                max_retries=p.max_retries,
                concurrency=p.concurrency,
                extra_params=dict(m.extra_params or {}),
            )
        )
        llm.on_usage = self._on_usage
        self._llms[ref] = llm
        self._llm_fps[ref] = fp
        return llm

    @property
    def llm(self) -> BaseLLM:
        """默认 provider 的实例（兼容旧调用）。"""
        return self.llm_for("")

    def cfg_for(self, task: str = ""):
        return self.settings.to_agent_config(self.state_dir, self.settings.resolve_for(task))

    @property
    def cfg(self):
        return self.settings.to_agent_config(self.state_dir)

    def guard(self) -> None:
        """写操作前的统一检查（凭据 / 演练模式 / 预算）。"""
        _guard(self)

    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            if self._store:
                self._store.close()
                self._store = None
            self.usage.close()


@dataclass
class ToolSpec:
    name: str
    label: str
    description: str
    params: dict[str, str]  # 参数名 → 说明（前端据此渲染表单，Agent 据此理解）
    fn: Callable[..., dict]
    writes: bool  # 是否会写平台（前端据此标红/二次确认）


# --------------------------------------------------------------------- 工具实现
def _budget_guard(ctx: AppContext, job, runner) -> None:
    """写操作前的预算护栏。stop 直接拦，warn 只记，downgrade 提示切备用模型。"""
    if job is None:
        return
    verdict = ctx.check_budget(job_id=job.id)
    action = verdict.get("action", "ok")
    if action == "ok":
        return
    msg = verdict.get("reason") or "预算超限"
    ctx.usage.log_budget_event(
        verdict.get("scope", ""), msg, verdict.get("spent_usd", 0.0),
        verdict.get("limit_usd", 0.0), action,
    )
    ctx.logs.add(f"预算 {action}：{msg}", level="warn" if action != "stop" else "error",
                 job_id=job.id, source="budget")
    if runner:
        runner.log(job, f"预算：{msg}", level="warn" if action != "stop" else "error")
    if action == "stop":
        raise RuntimeError(f"{msg} —— 已按预算策略拦截，本次任务未执行")
    if action == "downgrade":
        fb = verdict.get("fallback_model") or "未配置"
        if runner:
            runner.log(job, f"建议切换到备用模型 {fb}（当前仅告警，不自动切换）", level="warn")


def _guard(ctx: AppContext) -> None:
    if ctx.mock:
        return
    s = ctx.settings
    if not s.token:
        raise RuntimeError("尚未配置 ParaTranz Token，请到「设置」页填写")
    if not s.project_id:
        raise RuntimeError("尚未配置项目编号，请到「设置」页填写")


def _dir_rollup(files: list) -> list[dict]:
    """按目录汇总——与 ParaTranz 文件页的分组方式保持一致。"""
    groups: dict[str, dict] = {}
    for f in files:
        d = groups.setdefault(
            f.dir, {"dir": f.dir, "files": 0, "total": 0, "translated": 0,
                    "checked": 0, "disputed": 0, "words": 0, "pending": 0}
        )
        d["files"] += 1
        d["total"] += f.total
        d["translated"] += f.translated
        d["checked"] += f.checked
        d["disputed"] += f.disputed
        d["words"] += f.words
        d["pending"] += f.pending
    out = list(groups.values())
    for d in out:
        d["percent"] = round(d["translated"] / d["total"] * 100, 1) if d["total"] else 0.0
    out.sort(key=lambda x: x["pending"], reverse=True)
    return out


def tool_status(ctx: AppContext, *, job=None, runner=None, **_: Any) -> dict:
    _guard(ctx)
    info = ctx.client.get_project()
    files = ctx.client.list_files()
    total = sum(f.total for f in files)
    translated = sum(f.translated for f in files)
    checked = sum(f.checked for f in files)
    reviewed = sum(f.reviewed for f in files)
    disputed = sum(f.disputed for f in files)

    tasks: list = []
    try:
        tasks = ctx.client.list_tasks(page=1, page_size=20)
    except Exception:  # noqa: BLE001 - 任务功能可能未开启
        pass
    terms = 0
    try:
        terms = sum(1 for _ in ctx.client.iter_terms())
    except Exception:  # noqa: BLE001
        pass

    return {
        "project": {
            "id": info.id,
            "name": info.name,
            "source": info.source,
            "dest": info.dest,
            "task_enabled": bool(info.extra.get("task")),
            "review_mode": info.review_mode,
        },
        "progress": {
            "files": len(files),
            "total": total,
            "translated": translated,
            "checked": checked,
            "reviewed": reviewed,
            "disputed": disputed,
            "words": sum(f.words for f in files),
            "percent": round(translated / total * 100, 1) if total else 0.0,
        },
        "dirs": _dir_rollup(files),
        "tasks": [
            {"id": t.id, "title": t.title, "status": t.status, "files": len(t.taskFiles)}
            for t in tasks
        ],
        "terms": terms,
        "local_tm": ctx.store.tm_size(),
    }


def tool_files(ctx: AppContext, *, job=None, runner=None, **_: Any) -> dict:
    _guard(ctx)
    files = ctx.client.list_files()
    rows = [
        {
            "id": f.id,
            "name": f.name,
            "dir": f.dir,
            "total": f.total,
            "translated": f.translated,
            "checked": f.checked,
            "disputed": f.disputed,
            "pending": f.pending,
            "percent": round(f.progress * 100, 1),
        }
        for f in sorted(files, key=lambda x: (x.dir, -x.pending))
    ]
    return {"files": rows, "dirs": _dir_rollup(files)}


def tool_plan(ctx: AppContext, *, job=None, runner=None, dry_run: bool | None = None, **_: Any) -> dict:
    _guard(ctx)
    _budget_guard(ctx, job, runner)
    cfg = ctx.cfg
    dry = cfg and (ctx.settings.dry_run if dry_run is None else dry_run)
    pipe = PlanPipeline(ctx.client, cfg, ctx.store)
    planned = pipe.build_plan()
    if job and runner:
        for p in planned:
            runner.log(job, f"计划：{p.summary()}")
    created = pipe.execute(planned, dry_run=dry)
    if job and runner and dry:
        runner.log(job, "演练模式：未创建任务")
    return {
        "planned": [{"title": p.title, "strings": p.strings, "mode": p.mode} for p in planned],
        "created": len(created),
        "dry_run": dry,
    }


def _pipeline_kwargs(ctx: AppContext) -> dict:
    s = ctx.settings
    return {"limit": s.per_run_limit, "dry_run": s.dry_run}


def tool_translate(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _guard(ctx)
    _budget_guard(ctx, job, runner)
    s = ctx.settings
    limit = kw.get("limit") or s.per_run_limit
    dry = s.dry_run if kw.get("dry_run") is None else bool(kw["dry_run"])
    pipe = TranslatePipeline(
        ctx.client, ctx.cfg_for("translate"), ctx.store, ctx.llm_for("translate"),
        system_prompt=ctx.settings.prompt_for("translate"),
    )
    if job and runner:
        runner.log(job, f"拉取未翻译词条，上限 {limit} 条")
    stats = pipe.run(limit=limit, dry_run=dry)
    if job and runner:
        tag = "（演练模式：仅预览，未写入平台）" if dry else ""
        runner.log(job, f"处理 {stats.written} 条{tag}，TM 命中 {stats.tm_hit}，拦截 {stats.blocked}")
    return stats.as_dict()


def tool_review(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _guard(ctx)
    _budget_guard(ctx, job, runner)
    s = ctx.settings
    limit = kw.get("limit") or s.per_run_limit
    dry = s.dry_run if kw.get("dry_run") is None else bool(kw["dry_run"])
    pipe = ReviewPipeline(
        ctx.client, ctx.cfg_for("review"), ctx.store, ctx.llm_for("review"),
        system_prompt=ctx.settings.prompt_for("review"),
    )
    stats = pipe.run(limit=limit, dry_run=dry, include_disputed=bool(kw.get("include_disputed")))
    if job and runner:
        tag = "（演练模式：未改 stage）" if dry else ""
        runner.log(job, f"通过 {stats.passed}，打回 {stats.disputed}{tag}")
    out = stats.as_dict()
    out["samples"] = stats.samples[:20]
    return out


def tool_glossary(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _guard(ctx)
    _budget_guard(ctx, job, runner)
    s = ctx.settings
    dry = s.dry_run if kw.get("dry_run") is None else bool(kw["dry_run"])
    pipe = GlossaryPipeline(
        ctx.client, ctx.cfg_for("glossary"), ctx.store, ctx.llm_for("glossary"),
        system_prompt=ctx.settings.prompt_for("glossary"),
    )
    cands = pipe.build()
    if job and runner:
        runner.log(job, f"抽取到 {len(cands)} 个候选术语")
    res = pipe.publish(cands, dry_run=dry, overwrite=bool(kw.get("overwrite")))
    if job and runner and dry:
        runner.log(job, "演练模式：未写入术语表")
    return {**res, "candidates": [c.as_import() for c in cands][:100]}


def tool_styleguide(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _guard(ctx)
    _budget_guard(ctx, job, runner)
    s = ctx.settings
    dry = s.dry_run if kw.get("dry_run") is None else bool(kw["dry_run"])
    pipe = StyleGuidePipeline(
        ctx.client, ctx.cfg_for("styleguide"), ctx.store, ctx.llm_for("styleguide"),
        system_prompt=ctx.settings.prompt_for("styleguide"),
    )
    md = pipe.build(with_llm=bool(kw.get("with_llm", True)))
    res = pipe.publish(md, dry_run=dry)
    if job and runner and dry:
        runner.log(job, "演练模式：未发布公告")
    return {**res, "markdown": md}


def tool_run(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    """全流程：分派 → 翻译 → 校对 → 术语 → 规范。"""
    _guard(ctx)
    _budget_guard(ctx, job, runner)
    out: dict[str, Any] = {}
    steps = [
        ("plan", tool_plan),
        ("translate", tool_translate),
        ("review", tool_review),
        ("glossary", tool_glossary),
        ("styleguide", tool_styleguide),
    ]
    for name, fn in steps:
        if job and runner:
            if runner.is_cancelled(job.id):
                runner.log(job, "已取消，停止后续步骤", level="warn")
                break
            runner.log(job, f"── {name} ──")
        try:
            out[name] = fn(ctx, job=job, runner=runner, **kw)
        except Exception as exc:  # noqa: BLE001 - 单步失败不阻断后续
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
            if job and runner:
                runner.log(job, f"{name} 失败：{exc}", level="error")
    return out


def tool_extract(
    ctx: AppContext,
    *,
    extractor_id: str = "",
    game_dir: str = "",
    options: dict[str, Any] | None = None,
    accept_risk: bool = False,
    job=None,
    runner=None,
    **_: Any,
) -> dict:
    """跑提取器，把游戏目录里的词条抽成 JSONL（不碰平台）。"""
    from .extraction import run_extractor

    require_risk_ack("extract", accept_risk)

    if not extractor_id:
        raise ValueError("缺少 extractor_id")
    if not game_dir:
        raise ValueError("缺少 game_dir")
    if job and runner:
        runner.log(job, f"用 {extractor_id} 扫描 {game_dir}")
    res = run_extractor(ctx.state_dir, extractor_id, game_dir, options or {})
    if job and runner:
        if res.status == "done":
            runner.log(job, f"提取完成：{res.entries} 条（{res.duration_ms}ms）")
            for w in res.warnings:
                runner.log(job, w, level="warn")
        else:
            runner.log(job, f"提取失败：{res.error}", level="error")
    if res.status != "done":
        raise RuntimeError(res.error or "提取失败")
    return res.as_dict()


def tool_extraction_import(
    ctx: AppContext,
    *,
    extraction_id: str = "",
    file_id: int = 0,
    force: bool = False,
    job=None,
    runner=None,
    **_: Any,
) -> dict:
    """把提取结果导入平台文件。

    平台语义：`POST /files/{fileId}/translation` 只更新**已存在**的词条，
    新增必须走 `POST /projects/{projectId}/strings`（逐条、需管理员）。
    所以这里先算差集，再逐条创建。
    """
    from .extraction import load_entries, normalize, validate

    if not extraction_id:
        raise ValueError("缺少 extraction_id")
    if not file_id:
        raise ValueError("缺少 file_id")

    entries = load_entries(ctx.state_dir, extraction_id)
    report = validate(entries)
    merged, _ = normalize(entries)
    if report["errors"] and not force:
        raise RuntimeError(
            "校验有 " + str(report["stats"]["error_count"]) + " 个错误，先修或勾选强制导入"
        )

    def log(msg: str, level: str = "info") -> None:
        if job and runner:
            runner.log(job, msg, level=level)

    log("读取平台文件 " + str(file_id) + " 的现有词条…")
    existing = ctx.client.existing_keys(int(file_id))
    to_create = [e for e in merged if e["key"] not in existing]
    skipped = len(merged) - len(to_create)
    log(
        "已有 " + str(len(existing)) + " 个 key；需新增 " + str(len(to_create))
        + " 条，跳过已存在 " + str(skipped) + " 条"
    )

    if not to_create:
        log("没有需要新增的词条", level="warn")
        return {"created": 0, "skipped": skipped, "failed": [], "total": len(merged)}

    res = ctx.client.create_strings(
        to_create,
        int(file_id),
        on_progress=lambda i, n: log("已创建 " + str(i) + "/" + str(n)),
    )
    created = res["created"]
    failed = res["failed"]
    log(
        "导入完成：新增 " + str(len(created)) + " 条，失败 " + str(len(failed))
        + " 条，跳过 " + str(skipped) + " 条",
        level="info" if not failed else "warn",
    )
    return {
        "created": len(created),
        "skipped": skipped,
        "failed": failed[:50],
        "failed_count": len(failed),
        "total": len(merged),
    }


def tool_extractor_generate(
    ctx: AppContext,
    *,
    game_dir: str = "",
    hint: str = "",
    accept_risk: bool = False,
    job=None,
    runner=None,
    **_: Any,
) -> dict:
    """让模型扫描游戏目录并写出提取器，落盘后自动试跑。"""
    from .extractor_agent import generate

    require_risk_ack("extractor_generate", accept_risk)

    if not game_dir:
        raise ValueError("缺少 game_dir")
    log = (lambda m: runner.log(job, m)) if (job and runner) else None
    res = generate(ctx.llm_for("extractor"), ctx.state_dir, game_dir, hint, on_log=log)
    if job and runner:
        t = res["trial"]
        runner.log(
            job,
            "生成 " + res["extractor"]["id"] + "，试跑"
            + ("成功：" + str(t.get("entries", 0)) + " 条" if t.get("ok")
               else "失败：" + str(t.get("error", ""))[:120]),
            level="info" if t.get("ok") else "warn",
        )
    return res


def tool_extractor_improve(
    ctx: AppContext,
    *,
    extractor_id: str = "",
    game_dir: str = "",
    feedback: str = "",
    accept_risk: bool = False,
    job=None,
    runner=None,
    **_: Any,
) -> dict:
    """在现有提取器上继续改进（备份旧脚本后覆写并试跑）。"""
    from .extractor_agent import improve

    require_risk_ack("extractor_improve", accept_risk)

    if not extractor_id:
        raise ValueError("缺少 extractor_id")
    if not game_dir:
        raise ValueError("缺少 game_dir")
    log = (lambda m: runner.log(job, m)) if (job and runner) else None
    res = improve(
        ctx.llm_for("extractor"), ctx.state_dir, extractor_id, game_dir, feedback, on_log=log
    )
    if job and runner:
        t = res["trial"]
        runner.log(
            job,
            "改进 " + extractor_id + "，试跑"
            + ("成功：" + str(t.get("entries", 0)) + " 条" if t.get("ok")
               else "失败：" + str(t.get("error", ""))[:120]),
            level="info" if t.get("ok") else "warn",
        )
    return res


def tool_writeback(
    ctx: AppContext,
    *,
    extraction_id: str = "",
    min_stage: int = 3,
    out_dir: str = "",
    mode: str = "mirror",
    in_place: bool = False,
    accept_risk: bool = False,
    dry_run: bool = True,
    platform_file: int = 0,
    job=None,
    runner=None,
    **_: Any,
) -> dict:
    """把平台上确认过的译文回写到游戏文件（提取的反向链路）。

    危险点在于 `in_place=True`：会直接覆盖游戏文件。所以要 `accept_risk`
    显式确认（与提取、导入一致的两道闸：前端弹窗 + 后端硬拦），
    并且原地写之前一定先备份到 state 目录。
    """
    from ..writeback import WritebackError, run_writeback

    if not extraction_id:
        raise ValueError("缺少 extraction_id")
    if in_place and not dry_run and not accept_risk:
        raise PermissionError("原地修改游戏文件必须显式确认（accept_risk）")

    def log(msg: str, level: str = "info") -> None:
        if job and runner:
            runner.log(job, msg, level=level)

    try:
        rep = run_writeback(
            client=ctx.client,
            state_dir=ctx.state_dir,
            job_id=extraction_id,
            min_stage=int(min_stage or 3),
            out_dir=out_dir,
            mode=mode or "mirror",
            in_place=bool(in_place),
            dry_run=bool(dry_run),
            platform_file=int(platform_file) or None,
            log=log,
        )
    except WritebackError as exc:
        log(str(exc), level="error")
        raise RuntimeError(str(exc)) from exc
    report = rep.as_dict()
    # 落一份报告：job 日志在内存里，进程重启就没了，界面要能"事后回看"
    from .writeback_store import save_last

    try:
        save_last(ctx.state_dir, extraction_id, report)
    except OSError as exc:  # 报告存不下来不该让任务失败
        log(f"报告落盘失败：{exc}", level="warn")
    return report


def tool_qa_check(ctx: AppContext, *, job=None, runner=None, original: str = "", translation: str = "", **_: Any) -> dict:
    """对单条文本跑确定性 QA —— 译者在 WebUI 里自查用。"""
    from ..qa import Severity, run_qa

    rep = run_qa(
        original,
        translation,
        terms=ctx.store.term_all(),
        block_on=Severity.ERROR,
    )
    return {
        "ok": rep.ok,
        "max_severity": int(rep.max_severity),
        "issues": [i.as_dict() for i in rep.issues],
    }


# --------------------------------------------------------------------- 注册表
def build_registry(ctx: AppContext) -> dict[str, Callable[..., dict]]:
    def bind(fn):
        return lambda **kw: fn(ctx, **kw)

    return {
        "status": bind(tool_status),
        "files": bind(tool_files),
        "plan": bind(tool_plan),
        "translate": bind(tool_translate),
        "review": bind(tool_review),
        "glossary": bind(tool_glossary),
        "styleguide": bind(tool_styleguide),
        "run": bind(tool_run),
        "qa_check": bind(tool_qa_check),
        "extract": bind(tool_extract),
        "extractor_generate": bind(tool_extractor_generate),
        "extractor_improve": bind(tool_extractor_improve),
        "extraction_import": bind(tool_extraction_import),
        "writeback": bind(tool_writeback),
    }


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "status",
        "label": "项目概览",
        "description": "读取项目进度、文件数、任务、术语数",
        "params": {},
        "writes": False,
        "endpoint": "GET /projects/{id}",
    },
    {
        "name": "files",
        "label": "文件清单",
        "description": "列出所有文件及其翻译进度，按待翻译量降序",
        "params": {},
        "writes": False,
        "endpoint": "GET /projects/{id}/files",
    },
    {
        "name": "plan",
        "label": "分派任务",
        "description": "按未翻译量装箱，生成带验收标准的任务并创建",
        "params": {"dry_run": "只预览不创建"},
        "writes": True,
        "endpoint": "POST /projects/{id}/tasks",
    },
    {
        "name": "translate",
        "label": "翻译",
        "description": "TM 命中直出 + LLM 翻译 + 占位符硬拦 + 写回 stage=1",
        "params": {"limit": "本次最多处理条数"},
        "writes": True,
        "endpoint": "PUT /projects/{id}/strings",
    },
    {
        "name": "review",
        "label": "校对",
        "description": "确定性 QA + LLM 二审，通过→已检查，打回→有疑问",
        "params": {"limit": "本次最多处理条数", "include_disputed": "同时复核有疑问条目"},
        "writes": True,
        "endpoint": "PUT /projects/{id}/strings (stage)",
    },
    {
        "name": "glossary",
        "label": "术语",
        "description": "从已定稿译文抽术语并批量导入",
        "params": {"overwrite": "覆盖译法不同的既有术语"},
        "writes": True,
        "endpoint": "PUT /projects/{id}/terms",
    },
    {
        "name": "styleguide",
        "label": "汉化规范",
        "description": "按项目实况生成规范并发布为公告",
        "params": {"with_llm": "用模型润色风格章节"},
        "writes": True,
        "endpoint": "POST /projects/{id}/announcements",
    },
    {
        "name": "run",
        "label": "全流程",
        "description": "分派 → 翻译 → 校对 → 术语 → 规范，一次跑完",
        "params": {},
        "writes": True,
        "endpoint": "多个",
    },
    {
        "name": "extract",
        "label": "提取词条",
        "description": "跑提取器把游戏文件里的词条抽成 JSONL，不碰平台",
        "params": {"extractor_id": "提取器 id", "game_dir": "游戏目录", "options": "提取参数",
                   "accept_risk": "必填：确认执行本机代码的风险"},
        "writes": False,
        "endpoint": "本地文件系统",
    },
    {
        "name": "extractor_generate",
        "label": "Agent 写提取器",
        "description": "扫描游戏目录，让模型生成提取脚本并自动试跑",
        "params": {"game_dir": "游戏目录", "hint": "额外要求（可选）",
                   "accept_risk": "必填：确认执行本机代码的风险"},
        "writes": False,
        "endpoint": "本地文件系统 + LLM",
    },
    {
        "name": "extractor_improve",
        "label": "Agent 改提取器",
        "description": "看着现有脚本和上次日志，让模型改进并用新版本试跑",
        "params": {"extractor_id": "提取器 id", "game_dir": "游戏目录",
                   "feedback": "改进要求（可选）",
                   "accept_risk": "必填：确认执行本机代码的风险"},
        "writes": False,
        "endpoint": "本地文件系统 + LLM",
    },
    {
        "name": "extraction_import",
        "label": "导入提取结果",
        "description": "把提取的词条逐条创建到平台指定文件（只新增缺失的 key）",
        "params": {"extraction_id": "提取记录 id", "file_id": "目标文件 id",
                   "force": "跳过校验错误"},
        "writes": True,
        "endpoint": "POST /projects/{id}/strings",
    },
    {
        "name": "writeback",
        "label": "回写译文到游戏文件",
        "description": "把平台上达到门槛的确认译文，按提取记录的 key 写回游戏文件（默认只输出到旁路目录）",
        "params": {"extraction_id": "提取记录 id", "min_stage": "译文门槛 1/3/5",
                   "out_dir": "输出目录（空=游戏目录旁的 <游戏目录>_cn）",
                   "mode": "mirror 整包镜像 / patch 仅改动文件",
                   "in_place": "原地修改游戏文件（危险，需 accept_risk）",
                   "dry_run": "仅预览，不落盘"},
        "writes": True,
        "endpoint": "本地文件（读平台 GET /strings）",
    },
    {
        "name": "qa_check",
        "label": "单条自查",
        "description": "对一条原文/译文跑确定性 QA，译者在网页里自查",
        "params": {"original": "原文", "translation": "译文"},
        "writes": False,
        "endpoint": "本地规则",
    },
]
