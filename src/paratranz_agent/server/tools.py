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
from ..config import AgentConfig, LLMCfg
from ..errors import BudgetExceeded
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
from .jobs import ToolFailed, current_job
from .logs import LogStore
from .params import as_bool, as_int
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

# 配置类错误：客户端改一下设置就能解决，不该按 500 报（见 app.py 的处理器）
class NotConfigured(RuntimeError):
    """缺少 Token / 项目编号这类「去设置页填一下就好」的状态。"""


def require_risk_ack(tool: str, accept_risk: object) -> None:
    """危险工具缺少显式确认时抛 `PermissionError`（任务终态 failed，拦在动手之前）。"""
    if tool in DANGEROUS_TOOLS and not as_bool(accept_risk, name="accept_risk"):
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
        # 写平台数据的互斥锁：批量/单条人工写与 worker 之外的入口共用，
        # 避免两次并发批量互相覆盖 stage（审计实测：后写覆盖前写、账本只剩一条）
        self.write_lock = threading.RLock()
        self._client: ParaTranzClient | None = None
        self._store: Store | None = None
        self._retired: list[ParaTranzClient] = []
        self._llms: dict[str, BaseLLM] = {}
        self._llm_fps: dict[str, tuple] = {}
        self._cfg_cache: tuple[RuntimeSettings, str, AgentConfig] | None = None
        self._fingerprint: tuple | None = None

    @property
    def settings(self) -> RuntimeSettings:
        return self.settings_store.load()

    def _fp(self) -> tuple:
        s = self.settings
        # `rate_limit_per_min` 必须进指纹：它被固化进 RateLimiter，
        # 不进指纹就等于「设置页把 420 改成 60、保存成功、实际还按 420 打」。
        return (
            s.project_id, s.token, s.base_url, s.rate_limit_per_min,
            s.llm_base_url, s.llm_model, s.llm_key,
        )

    def client_for(
        self,
        *,
        token: str,
        project_id: int,
        base_url: str,
        rate_limit_per_min: int,
    ) -> ParaTranzClient:
        """按**显式参数**建一个客户端（唯一构造点）。

        「测试连接」要用界面上正在填的值试，而不是已保存的那份 —— 否则
        「改了编号 → 点测试 → 报的还是上个编号的错」，用户永远对不上。
        这类临时客户端不进缓存、不落盘，调用方负责 close()。
        """
        if self.mock:
            # 演示/自测：内存假平台，状态落盘，命令之间连续
            from ..fake import FakeParaTranz

            return FakeParaTranz(
                persist_path=str(self.state_dir / "fake_platform.json")
            ).client()
        return ParaTranzClient(
            token=token,
            project_id=project_id or 1,
            base_url=base_url,
            rate_limit_per_min=rate_limit_per_min,
        )

    def _ensure(self) -> None:
        with self._lock:
            fp = self._fp()
            if self._fingerprint == fp and self._client is not None:
                return
            if self._client is not None:
                # **不立刻 close**：任务可能正拿着它发请求，
                # 中途保存设置会让正在跑的任务报 "client has been closed"。
                # 统一在 close() 时回收。
                self._retired.append(self._client)
            s = self.settings
            self._client = self.client_for(
                token=s.token,
                project_id=s.project_id or 1,
                base_url=s.base_url,
                rate_limit_per_min=s.rate_limit_per_min,
            )
            self._llms.clear()
            self._llm_fps.clear()
            if self._store is None:
                self._store = Store(self.state_dir / "agent.db")
                # 让账本知道"这笔记录属于哪个任务"：worker 线程里 current_job 有值，
                # 于是任务报表可以下钻到具体词条（谁写的、写了哪些）。
                self._store.set_job_provider(lambda: current_job.get()[0])
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

    def check_budget(self, *, job_id: str = "", task: str = "") -> dict[str, Any]:
        """预算检查。`model` 必须取**真正会用到的模型**。

        以前传的是 `settings.llm_model`（旧的单模型字段，默认 gpt-4o-mini），
        而 `usage.model` 列记的是路由解析出来的真实模型 ——
        `WHERE model=?` 永远匹配 0 行，「单模型每日预算」结构性永不触发。
        """
        got = self.settings.resolve_for(task)
        model = got[1].id if got else ""
        return self.usage.check_budget(self.budget_cfg, job_id=job_id, model=model)

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
        # 并发调用（多个 worker 同时首批）时，`_llms` 的读写必须加锁：
        # 无锁时可能建出两份实例，用量记账与 json 协商结果会分裂
        with self._lock:
            return self._build_llm_locked(p, m)

    def _build_llm_locked(self, p, m) -> BaseLLM:
        ref = join_ref(p.id, m.id)
        fp = (
            p.base_url, p.api_key, m.id, m.temperature, m.max_tokens,
            p.timeout, p.max_retries, p.concurrency, p.proxy,
            tuple(sorted((m.extra_params or {}).items())),
            tuple(sorted((p.headers or {}).items())),
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
                headers=dict(p.headers or {}),
                proxy=p.proxy or "",
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

    def cfg_for(self, task: str = "") -> AgentConfig:
        """按任务取 pipeline 用的配置对象。

        `AgentConfig` 由 `RuntimeSettings` 现算（见 `to_agent_config`），
        缓存键用**设置对象本身**：`SettingsStore.load()` 每次保存都换新对象，
        所以对象身份变了就等于配置变了，不必再手写一份字段指纹
        （那种指纹漏一个字段就会读到旧配置，属于自己给自己埋坑）。
        """
        s = self.settings
        got = s.resolve_for(task)
        ref = join_ref(got[0].id, got[1].id) if got else ""
        hit = self._cfg_cache
        if hit is not None and hit[0] is s and hit[1] == ref:
            return hit[2]
        cfg = s.to_agent_config(self.state_dir, got)
        self._cfg_cache = (s, ref, cfg)
        return cfg

    @property
    def cfg(self) -> AgentConfig:
        """默认模型下的配置（`to_agent_config(..., None)` 等价于 `cfg_for("")`）。"""
        return self.cfg_for("")

    def guard(self) -> None:
        """写操作前的统一检查（凭据 / 演练模式 / 预算）。"""
        _guard(self)

    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            for old in self._retired:      # 延迟回收的旧客户端（见 _ensure）
                try:
                    old.close()
                except Exception:  # noqa: BLE001 - 关闭失败不该挡住退出
                    pass
            self._retired.clear()
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


def agent_needs_confirm(tool: str) -> bool:
    """`/api/agent`（自然语言入口）是否必须拦住这个工具。

    判定依据是 **TOOL_SPECS 里的 `agent` 标记**（唯一来源，不再手维护第二份名单）：
    `allow` = 可以直接执行，`confirm` = 只给建议、由用户到页面手动执行。
    **缺标记、或工具名不认识，一律按「要确认」处理** —— 新增工具忘了标记时被拦住，
    而不是默默放行。这一条是拿真实绕过换来的：黑名单当初只写了 extract 系，
    于是 `writeback --in-place`（覆盖游戏原文件）和 `extraction_import`（不可撤回地
    往平台建词条）都能由模型一句话直接跑。
    """
    for spec in TOOL_SPECS:
        if spec["name"] == tool:
            return str(spec.get("agent", "confirm")) != "allow"
    return True


# --------------------------------------------------------------------- 工具实现
def _budget_guard(ctx: AppContext, job, runner) -> None:
    """写操作前的预算护栏。stop 直接拦，warn 只记，downgrade 提示切备用模型。

    粒度说明（别当成已解决）：这是**步骤前**检查。`run`（全流程）每一步都会过
    一次，所以第 2 步起就能看到前面步骤的花费；但单跑一次 translate 这类长任务
    是在批循环内部花钱的，中途没有检查点，只能事后看报表。
    """
    if job is None:
        return
    verdict = ctx.check_budget(job_id=job.id, task=job.tool)
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
        raise NotConfigured("尚未配置 ParaTranz Token，请到「设置」页填写")
    if not s.project_id:
        raise NotConfigured("尚未配置项目编号，请到「设置」页填写")


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
    tasks_error = ""
    try:
        tasks = ctx.client.list_tasks(page=1, page_size=100)
    except Exception as exc:  # noqa: BLE001 - 任务功能可能未开启
        # 项目没开 task 功能时这里是正常的空；但其它失败（网络/权限）也会走到
        # 这里。把它吞掉就等于报表里写「平台任务 0 个」—— 用户会以为真的没有。
        tasks_error = f"{type(exc).__name__}: {exc}"
    terms = 0
    terms_error = ""
    try:
        terms = sum(1 for _ in ctx.client.iter_terms())
    except Exception as exc:  # noqa: BLE001
        terms_error = f"{type(exc).__name__}: {exc}"

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
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "files": len(t.taskFiles),
                # 任务里"还剩多少条没翻"：平台把任务一直挂在 status=0（未结项），
                # 只看 status 会把"早已译完的空任务"也算成进行中
                # （实测 8 个 status=0 的任务里，有 1 个的待翻译是 0）。
                "pending": sum(
                    max(0, (tf.total or 0) - (tf.translated or 0)) for tf in (t.taskFiles or [])
                ),
            }
            for t in tasks
        ],
        "task_summary": _task_summary(tasks),
        "terms": terms,
        "local_tm": ctx.store.tm_size(),
        # 读不到就说读不到：0 和「没读出来」必须能区分
        "tasks_error": tasks_error,
        "terms_error": terms_error,
    }


def _task_summary(tasks: list) -> dict:
    """任务口径：未结项 / 其中还有待翻译 / 待翻译总条数。

    为什么不能只报 status=0 的个数：用户看到「8 个进行中」会以为有 8 个任务正在被处理。
    真实情况往往是历史任务一直没结项，其中一部分已经没有任何待翻译词条。
    """
    open_tasks = [t for t in tasks if t.status == 0]
    pending = {
        t.id: sum(max(0, (tf.total or 0) - (tf.translated or 0)) for tf in (t.taskFiles or []))
        for t in open_tasks
    }
    return {
        "open": len(open_tasks),
        "with_pending": sum(1 for v in pending.values() if v > 0),
        "pending_strings": sum(pending.values()),
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


# ------------------------------------------------- 五个流水线的公共编排
# 原先每个工具都重写一遍「闸门 + 预算 + 参数默认 + 构造流水线」。
# 前两件是所有写操作都必须做的，后两件只是取值，集中到这里；
# 各环节真正的差异（取材、执行、回收结果）留在各自的工具函数里。
_PIPELINES: dict[str, tuple[type, bool]] = {
    "plan": (PlanPipeline, False),
    "translate": (TranslatePipeline, True),
    "review": (ReviewPipeline, True),
    "glossary": (GlossaryPipeline, True),
    "styleguide": (StyleGuidePipeline, True),
}


def _preflight(ctx: AppContext, job, runner) -> None:
    """写操作前的统一检查：凭据与演练模式（`_guard`）+ 预算（`_budget_guard`）。"""
    _guard(ctx)
    _budget_guard(ctx, job, runner)


def _pipeline(ctx: AppContext, kind: str, *, job=None, runner=None):
    """按环节构造流水线。

    这三项必须**成套**出现：给哪个任务选模型（`llm_for`）、用哪套配置（`cfg_for`）、
    注入哪段提示词（`prompt_for`）。原先在四个工具里各写一遍——漏改一处，
    那个环节就会悄悄退回默认模型而没人发现。集中到这里，想漏也漏不了。

    `log=` 也必须在这里传：ReviewPipeline 的「二审进度 / 未判定 N / 失败批 M」
    全靠它，只在 CLI 传等于 WebUI 里把这些诊断直接丢掉。
    """
    cls, needs_llm = _PIPELINES[kind]
    if not needs_llm:
        return cls(ctx.client, ctx.cfg, ctx.store)
    llm = ctx.llm_for(kind)
    # 上一轮任务留下的降档说明属于上一轮：不清掉的话每份报表都会重复贴历史警告，
    # 用户分不清"这次又降档了"还是"早就降过"。
    notes = getattr(llm, "json_warnings", None)
    if isinstance(notes, list):
        notes.clear()
    kwargs: dict[str, Any] = {"system_prompt": ctx.settings.prompt_for(kind)}
    if cls is ReviewPipeline and job is not None and runner is not None:
        kwargs["log"] = lambda msg: runner.log(job, msg)
    if job is not None and runner is not None and cls in (TranslatePipeline, ReviewPipeline):
        # **任务内**预算检查点：翻译/校对是在批循环里花钱的，
        # 只在任务开始时查一次，中途超支到跑完才发现（第七轮遗留）。
        def _budget_gate() -> None:
            verdict = ctx.check_budget(job_id=job.id, task=job.tool)
            if verdict.get("action") == "stop":
                raise BudgetExceeded(verdict.get("reason") or "预算超限，已停止本任务")

        kwargs["budget_check"] = _budget_gate
    return cls(ctx.client, ctx.cfg_for(kind), ctx.store, llm, **kwargs)


def _limit_of(ctx: AppContext, kw: dict) -> int:
    """单次运行条数上限：显式传参 > 设置页默认值。

    不能写成 `kw.get("limit") or 默认值`：`{"limit": 0}` 会被当成"没给"，于是按
    默认 200 条**真写平台**；`{"limit": "50"}` 会把字符串带进 `len(...) >= "50"`
    抛 TypeError。这里显式判 None 并转成整数。
    """
    v = kw.get("limit")
    if v is None or v == "":
        return ctx.settings.per_run_limit
    return as_int(v, name="limit")


def _dry_of(ctx: AppContext, kw: dict) -> bool:
    """是否演练：显式传参 > 设置页默认值。字符串 `"false"` 必须解析成 False。"""
    v = kw.get("dry_run")
    if v is None:
        return ctx.settings.dry_run
    return as_bool(v, name="dry_run")


def tool_plan(ctx: AppContext, *, job=None, runner=None, dry_run: Any = None, **_: Any) -> dict:
    _preflight(ctx, job, runner)
    dry = ctx.settings.dry_run if dry_run is None else as_bool(dry_run, name="dry_run")
    pipe = _pipeline(ctx, "plan", job=job, runner=runner)
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


def _is_hard_failure(stats: Any) -> bool:
    """「取材非空但一条没写」里，哪些属于真故障。

    故障证据：模型报错 / 全被 QA 拦 / 复查不可用 / 分块写回失败 /
    **模型回了但一条都没给**（`failed` 计数）。
    不属于故障：全被别人抢先翻译（force_overwrite=false 放过）——
    那是保护人工译文，报 failed 会让人以为软件坏了。
    """
    return bool(
        stats.errors or stats.blocked or stats.manual_risk
        or stats.write_chunks_failed or stats.failed
    )


def _why_nothing_written(stats: Any) -> str:
    """「取材非空但一条都没写」的确切原因。

    这句话存在的理由：真实事故里任务日志只有「处理 0 条」然后「完成」，
    用户完全无法判断是网关 400、是全被 QA 拦了、还是白名单没匹配上。
    """
    if stats.errors:
        return f"模型调用全部失败：{stats.errors[0]}"
    if stats.files_unmatched:
        return "translate.files 白名单没有匹配到任何文件，本轮没有取材"
    if stats.skipped_manual:
        return (
            f"{stats.skipped_manual} 条在取材之后已被他人翻译/改过 stage，"
            "按 force_overwrite=false 全部放过（这是保护人工译文，不是故障）"
        )
    if stats.manual_risk:
        return "写回前的人工译文复查不可用（网络/限流），为防覆盖人工译文，本轮整轮未写入"
    if stats.blocked:
        first = (stats.blocked_samples or [{}])[0]
        return (
            f"{stats.blocked} 条全部被 QA 拦下（占位符/控制符不一致或空译文），一条未写入；"
            f"例：{str(first.get('reason') or '')[:80]}"
        )
    if stats.failed:
        return f"模型返回了，但 {stats.failed} 条都没给译文（id 对不上或 items 为空）"
    return f"取材 {stats.fetched} 条，但没有任何一条写入成功"


def _files_of(kw: dict) -> list[str] | None:
    """单次任务的文件/目录范围（`files` 参数）。

    语义与配置里的 `translate.files` 完全一致（精确相等或以 `项/` 开头），
    但只在这次任务里生效 —— Agent 说「只翻 overworld-town/loc-adultshop/」时
    走这里，而不是去改全局配置。
    """
    raw = kw.get("files")
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("files 必须是字符串数组（文件名或目录前缀）")
    out: list[str] = []
    for x in raw[:50]:
        v = str(x or "").strip()
        if v:
            out.append(v)
    return out or None


def _ids_of(kw: dict) -> list[int] | None:
    """点名要处理的词条 id（WebUI 批量选择用）。最多 500 条。"""
    raw = kw.get("ids")
    if not raw:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError("ids 必须是数组（点名要重翻的词条 id 列表）")
    out: list[int] = []
    for x in raw[:500]:
        out.append(as_int(x, name="ids[i]"))
    return out or None


def tool_translate(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _preflight(ctx, job, runner)
    limit, dry = _limit_of(ctx, kw), _dry_of(ctx, kw)
    ids = _ids_of(kw)
    files = _files_of(kw)
    if ids:
        # 点名要的就是"这些"，再按 per_run_limit 截断会静默少翻几条
        # （审计实测：ids=3 而 per_run_limit=2 → 只翻 2 条，日志还写"3 条"）。
        limit = len(ids)
    pipe = _pipeline(ctx, "translate", job=job, runner=runner)
    on_items = (lambda rows: runner.item(job, rows)) if (job and runner) else None
    if job and runner:
        scope = f"，范围 {'、'.join(files)}" if files else ""
        runner.log(
            job,
            f"点名重翻 {len(ids)} 条（只处理这些，不再按阶段取材）{scope}" if ids
            else f"拉取未翻译词条，上限 {limit} 条{scope}",
        )
    stats = pipe.run(limit=limit, dry_run=dry, on_items=on_items, ids=ids, files=files)
    out = stats.as_dict()
    out["samples"] = stats.blocked_samples[:20]
    # 演练模式下 written 恒为 0，"有没有产出"要看 would_write
    produced = stats.would_write if dry else stats.written
    if job and runner:
        tag = "（演练模式：仅预览，未写入平台）" if dry else ""
        written_txt = f"将写入 {stats.would_write} 条" if dry else f"写入 {stats.written} 条"
        # 一行里必须能看出「取材多少 / 写进去多少 / 失败多少」：
        # 只报 written 的老写法让"全程 0 条"和"翻完了"长得一模一样。
        runner.log(
            job,
            f"取材 {stats.fetched} 条 → {written_txt}{tag}；"
            f"TM 命中 {stats.tm_hit}，被拦 {stats.blocked}，失败 {stats.failed}",
        )
        for note in stats.warnings:
            runner.log(job, note, level="warn")
        for err in stats.errors:
            runner.log(job, f"模型调用失败：{err}", level="error")
    if stats.fetched and not produced:
        # 取材了却一条没产出：分两种，别混着报。
        # ①有明确故障证据（模型报错/一条没回/全被 QA 拦/复查不可用/分块失败）→ 必须 failed；
        # ②只是"没活可干"（全被别人抢先翻译、>force_overwrite=false 放过）→ 不算失败，
        #    但也绝不能用一句「完成」糊过去，得把原因写进日志与结果。
        hard = _is_hard_failure(stats)
        if hard:
            raise ToolFailed(_why_nothing_written(stats), out)
        out["reason"] = _why_nothing_written(stats)
        if job and runner:
            runner.log(job, f"本轮没有写入任何条目：{out['reason']}", level="warn")
    if files and stats.files_unmatched:
        # 用户点名了范围却一个文件都没对上：这是范围写错/不存在，必须报错而不是
        # 悄悄退化成"全项目"或"没有待翻译"（真实事故：指定目录 6 条，实际翻了 200 条）
        raise ToolFailed(
            f"指定的范围没有匹配到任何文件：{'、'.join(files)}。"
            "请检查路径（应为平台里的文件全名或目录前缀，例如 overworld-town/loc-adultshop）",
            out,
        )
    if not stats.fetched:
        # 一条都没取到：不是失败，但也绝不能只显示「完成」——
        # 真实场景里这既可能是"项目已全部翻译"，也可能是阶段/文件/白名单条件不匹配，
        # 还可能是 min_length 把所有短句都跳过了（这一种以前根本没提，用户会以为平台空了）。
        if stats.files_unmatched:
            why = "translate.files 白名单没有匹配到任何文件"
        elif stats.skipped_short:
            why = (
                f"取到的 {stats.skipped_short} 条原文都短于 translate.min_length，被整批跳过"
            )
        elif stats.manual_risk:
            why = "写前人工译文复查不可用，本轮未取材"
        else:
            why = "当前条件下没有未翻译词条（项目可能已全部翻译，或阶段已不是未翻译）"
        out["reason"] = why
        if job and runner:
            runner.log(job, f"没有取到可翻译的词条：{why}", level="warn")
    return out


def tool_review(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _preflight(ctx, job, runner)
    limit, dry = _limit_of(ctx, kw), _dry_of(ctx, kw)
    pipe = _pipeline(ctx, "review", job=job, runner=runner)
    on_items = (lambda rows: runner.item(job, rows)) if (job and runner) else None
    stats = pipe.run(
        limit=limit, dry_run=dry, include_disputed=as_bool(kw.get("include_disputed"), name="include_disputed"),
        on_items=on_items,
    )
    if job and runner:
        tag = "（演练模式：未改 stage）" if dry else ""
        changed = f"将改 {stats.would_write} 条" if dry else f"改 stage {stats.written} 条"
        runner.log(
            job,
            f"取材 {stats.fetched} 条 → 通过 {stats.passed}，打回 {stats.disputed}，{changed}{tag}；"
            f"QA 拦 {stats.qa_blocked}，模型打回 {stats.llm_blocked}，"
            f"未判定 {stats.skipped_no_verdict}，失败批 {stats.failed_batches}",
        )
        for note in stats.warnings:
            runner.log(job, note, level="warn")
        for err in stats.errors:
            runner.log(job, f"模型二审失败：{err}", level="error")
    out = stats.as_dict()
    out["samples"] = stats.samples[:20]
    # 一条 stage 都没改、却有二审失败/未判定 → 不能报"完成"
    if stats.fetched and not (stats.would_write if dry else stats.written) and (
        stats.errors or stats.failed_batches
    ):
        raise ToolFailed(
            f"{stats.failed_batches} 批模型二审失败（{stats.errors[0] if stats.errors else '原因未知'}），"
            f"本轮未改任何词条的状态",
            out,
        )
    return out


def tool_glossary(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _preflight(ctx, job, runner)
    dry = _dry_of(ctx, kw)
    pipe = _pipeline(ctx, "glossary", job=job, runner=runner)
    cands = pipe.build()
    if job and runner:
        runner.log(job, f"抽取到 {len(cands)} 个候选术语")
    res = pipe.publish(
        cands, dry_run=dry, overwrite=as_bool(kw.get("overwrite"), name="overwrite")
    )
    if job and runner and dry:
        runner.log(job, "演练模式：未写入术语表")
    return {**res, "candidates": [c.as_import() for c in cands][:100]}


def tool_styleguide(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    _preflight(ctx, job, runner)
    dry = _dry_of(ctx, kw)
    pipe = _pipeline(ctx, "styleguide", job=job, runner=runner)
    md = pipe.build(with_llm=as_bool(kw.get("with_llm") if kw.get("with_llm") is not None else True,
                                    name="with_llm"))
    res = pipe.publish(md, dry_run=dry)
    if job and runner and dry:
        runner.log(job, "演练模式：未发布公告")
    return {**res, "markdown": md}


def tool_run(ctx: AppContext, *, job=None, runner=None, **kw: Any) -> dict:
    """全流程：分派 → 翻译 → 校对 → 术语 → 规范。

    这里**不再**做闸门与预算检查：下面每一步调用的工具自己会做（`_preflight`）。
    再多挡一遍只会让预算事件被重复记 6 次，日志里看不出真实原因。
    """
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
            if name in ("glossary", "styleguide") and isinstance(out[name], dict):
                # 全流程走的是未包装本体，注册表那层的 llm_notes 补不到这里
                notes = _llm_notes(ctx, name)
                if notes:
                    merged = list(out[name].get("llm_notes") or [])
                    out[name]["llm_notes"] = merged + [n for n in notes if n not in merged]
        except ToolFailed as exc:
            # 这一步"跑了但不算成功"：报表要留下（取材/失败/原因），
            # 否则全流程的结果里只剩一句 error，跟单跑该工具看到的东西对不上。
            out[name] = {**(exc.result or {}), "error": str(exc)}
            if job and runner:
                runner.log(job, f"{name} 未完成：{exc}", level="error")
        except Exception as exc:  # noqa: BLE001 - 单步失败不阻断后续
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
            if job and runner:
                runner.log(job, f"{name} 失败：{exc}", level="error")
    # 有任何环节没跑成就不能报 done：全流程最容易掩盖单步失败，
    # 界面只显示"完成"时，用户不会知道翻译其实一条没写成。
    bad = [k for k, v in out.items() if isinstance(v, dict) and v.get("error")]
    if bad:
        raise ToolFailed(f"全流程有 {len(bad)} 个环节未完成：{'、'.join(bad)}", out)
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
    dry_run: bool | None = None,
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

    # 闸门必须在本层：同一个工具还有 `POST /api/jobs {tool,args}` 与
    # `/api/agent` 两个入口，只在路由校验等于没校验（审计高危 1 的同一类问题）。
    _guard(ctx)
    dry = _dry_of(ctx, {"dry_run": dry_run})
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
        return {"created": 0, "skipped": skipped, "failed": [], "total": len(merged),
                "dry_run": dry}

    if dry:
        # 演练模式：该拉该算的都做了，就是不写。返回的数字仍是事实（"会新增多少"）。
        log("演练模式：不写入平台（将新增 " + str(len(to_create)) + " 条）", level="warn")
        return {"created": 0, "would_create": len(to_create), "skipped": skipped,
                "failed": [], "total": len(merged), "dry_run": True}

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


def tool_extraction_upload(
    ctx: AppContext,
    *,
    extraction_id: str = "",
    filename: str = "",
    path: str = "",
    force: bool = False,
    dry_run: bool | None = None,
    job=None,
    runner=None,
    **_: Any,
) -> dict:
    """把提取结果**上传成一个新文件**（`POST /files`）。

    与 `extraction_import` 的分工：
    - `extraction_import` 往**已有文件**里补 key（逐条 `POST /strings`，几千条要跑很久）；
    - 本工具新建一个文件，平台自己按项目格式解析并建词条（一次请求，快得多）。

    内容形状是 `{key: original}` 的 JSON 对象。实测（2026-10-24，twee 项目 15382）
    平台照扩展名解析、把词条原样建了出来；但不同项目格式没逐一验证，
    所以路由层强制先「预览」，把内容给用户看一眼再决定传不传。
    """
    from .extraction import export_upload_json, load_entries, normalize, validate

    # 与 import 同理：闸门放本层，三个入口共用
    _guard(ctx)
    dry = _dry_of(ctx, {"dry_run": dry_run})
    if not extraction_id:
        raise ValueError("缺少 extraction_id")
    name = (filename or "").strip() or "extract.json"
    if not name.lower().endswith(".json"):
        # 平台按扩展名挑解析器；名字对不上就会出现"传上去了但没词条"
        raise ValueError("上传文件名必须以 .json 结尾（平台按扩展名选解析器）")

    entries = load_entries(ctx.state_dir, extraction_id)
    report = validate(entries)
    merged, _ = normalize(entries)
    if report["errors"] and not force:
        raise RuntimeError(
            "校验有 " + str(report["stats"]["error_count"]) + " 个错误，先修或勾选强制上传"
        )

    def log(msg: str, level: str = "info") -> None:
        if job and runner:
            runner.log(job, msg, level=level)

    content = export_upload_json(merged)
    log(
        "准备上传 " + name + "：" + str(len(merged)) + " 条词条，"
        + str(len(content)) + " 字节" + ("，目录 " + path if path else "")
    )
    if dry:
        log("演练模式：不调用上传接口（内容 " + str(len(content)) + " 字节已就绪）",
            level="warn")
        return {"file": None, "uploaded": 0, "would_upload": len(merged),
                "bytes": len(content), "dry_run": True}

    item = ctx.client.create_file(name, content, path=path or "")
    log(f"平台已建文件 id={item.id} name={item.name}", level="info")
    return {
        "file": {"id": item.id, "name": item.name, "path": path or ""},
        "uploaded": len(merged),
        "bytes": len(content),
        "dry_run": False,
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
    min_stage: int | None = None,
    out_dir: str | None = None,
    mode: str | None = None,
    in_place: Any = False,
    accept_risk: Any = False,
    dry_run: Any = None,
    platform_file: int = 0,
    job=None,
    runner=None,
    **_: Any,
) -> dict:
    """把平台上确认过的译文回写到游戏文件（提取的反向链路）。

    危险点在于 `in_place=True`：会直接覆盖游戏文件。所以要 `accept_risk`
    显式确认（与提取、导入一致的两道闸：前端弹窗 + 后端硬拦），
    并且原地写之前一定先备份到 state 目录。

    三项默认值（门槛 / 形式 / 输出目录）跟随设置页：以前这里是硬编码默认值，
    只有前端弹窗会预填，于是走 `POST /api/jobs {tool:"writeback"}` 时设置页
    填的三项被完全忽略 —— 「保存成功但不生效」。
    """
    from ..writeback import WritebackError, run_writeback

    _guard(ctx)
    s = ctx.settings
    # 演练模式是全局开关：显式传参优先，没传就跟随设置页，
    # 这样 `POST /api/jobs` 直投也不会绕过它。
    dry = _dry_of(ctx, {"dry_run": dry_run})
    in_place_flag = as_bool(in_place, name="in_place")
    if not extraction_id:
        raise ValueError("缺少 extraction_id")
    if in_place_flag and not dry and not as_bool(accept_risk, name="accept_risk"):
        raise PermissionError("原地修改游戏文件必须显式确认（accept_risk）")
    stage = int(min_stage if min_stage is not None else s.writeback_min_stage)
    out = (out_dir if out_dir is not None else s.writeback_out_dir) or ""
    wb_mode = (mode or s.writeback_mode or "mirror").strip()

    def log(msg: str, level: str = "info") -> None:
        if job and runner:
            runner.log(job, msg, level=level)

    try:
        rep = run_writeback(
            client=ctx.client,
            state_dir=ctx.state_dir,
            job_id=extraction_id,
            min_stage=stage,
            out_dir=out,
            mode=wb_mode,
            in_place=in_place_flag,
            dry_run=dry,
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
def _llm_notes(ctx: AppContext, kind: str) -> list[str]:
    """该环节解析出的模型实例上报的"传输层异常"（如网关不支持 json_object）。

    集中在这里读一次：各 pipeline 里的 llm 实例是同一个（`llm_for` 按环节缓存），
    所以不必每个工具自己 `getattr` 一遍。
    """
    try:
        llm = ctx.llm_for(kind)
    except Exception:  # noqa: BLE001 - 读不到备注不能影响业务结果
        return []
    return list(getattr(llm, "json_warnings", []) or [])


def build_registry(ctx: AppContext) -> dict[str, Callable[..., dict]]:
    """把工具函数绑定到 ctx，并在**唯一一处**补上"模型传输层备注"。

    为什么放在这里而不是每个工具里：每个环节手抄一遍"把 llm.json_warnings 塞进
    结果"迟早漏一处，而漏的那处就重新变成静默。

    只管 `glossary` / `styleguide`：`translate` / `review` 有自己的 `warnings`
    字段并且已经写进去了，这里再补一次就是同一句话出现两遍。
    """
    _NOTED = {"glossary", "styleguide"}

    def bind(name, fn):
        def call(**kw):
            out = fn(ctx, **kw)
            if name in _NOTED:
                notes = _llm_notes(ctx, name)
                if notes and isinstance(out, dict):
                    merged = list(out.get("llm_notes") or [])
                    out["llm_notes"] = merged + [n for n in notes if n not in merged]
            return out

        return call

    return {
        "status": bind("status", tool_status),
        "files": bind("files", tool_files),
        "plan": bind("plan", tool_plan),
        "translate": bind("translate", tool_translate),
        "review": bind("review", tool_review),
        "glossary": bind("glossary", tool_glossary),
        "styleguide": bind("styleguide", tool_styleguide),
        "run": bind("run", tool_run),
        "qa_check": bind("qa_check", tool_qa_check),
        "extract": bind("extract", tool_extract),
        "extractor_generate": bind("extractor_generate", tool_extractor_generate),
        "extractor_improve": bind("extractor_improve", tool_extractor_improve),
        "extraction_import": bind("extraction_import", tool_extraction_import),
        "extraction_upload": bind("extraction_upload", tool_extraction_upload),
        "writeback": bind("writeback", tool_writeback),
    }


# 工具清单。`*_en` 是给英文界面用的第二份文案：界面按当前语言挑，
# 缺 `*_en` 就回落中文。**端点本身是 ASCII 时不写 `endpoint_en`**——
# 两份一模一样的东西不叫双语，只叫重复。
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "status",
        "label": "项目概览",
        "label_en": "Project overview",
        "description": "读取项目进度、文件数、任务、术语数",
        "description_en": "Read project progress, file count, tasks and term count",
        "params": {},
        "writes": False,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "GET /projects/{id}",
    },
    {
        "name": "files",
        "label": "文件清单",
        "label_en": "File list",
        "description": "列出所有文件及其翻译进度，按待翻译量降序",
        "description_en": "List all files with translation progress, busiest first",
        "params": {},
        "writes": False,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "GET /projects/{id}/files",
    },
    {
        "name": "plan",
        "label": "分派任务",
        "label_en": "Assign tasks",
        "description": "按未翻译量装箱，生成带验收标准的任务并创建",
        "description_en": "Pack untranslated volume into tasks with acceptance criteria and create them",
        "params": {"dry_run": "只预览不创建"},
        "writes": True,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "POST /projects/{id}/tasks",
    },
    {
        "name": "translate",
        "label": "翻译",
        "label_en": "Translate",
        "description": "TM 命中直出 + LLM 翻译 + 占位符硬拦 + 写回 stage=1",
        "description_en": "TM hits pass straight through + LLM translation + placeholder guard + write stage=1",
        "params": {
            "limit": "本次最多处理条数",
            "files": "只处理这些文件或目录（前缀匹配，如 overworld-town/loc-adultshop）",
            "ids": "只重翻这些词条 id",
        },
        "writes": True,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "PUT /projects/{id}/strings",
    },
    {
        "name": "review",
        "label": "校对",
        "label_en": "Review",
        "description": "确定性 QA + LLM 二审，通过→已检查，打回→有疑问",
        "description_en": "Deterministic QA + LLM second pass: pass → checked, fail → disputed",
        "params": {"limit": "本次最多处理条数", "include_disputed": "同时复核有疑问条目"},
        "writes": True,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "PUT /projects/{id}/strings (stage)",
    },
    {
        "name": "glossary",
        "label": "术语",
        "label_en": "Glossary",
        "description": "从已定稿译文抽术语并批量导入",
        "description_en": "Extract terms from finalized translations and import them in bulk",
        "params": {"overwrite": "覆盖译法不同的既有术语"},
        "writes": True,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "PUT /projects/{id}/terms",
    },
    {
        "name": "styleguide",
        "label": "汉化规范",
        "label_en": "Style guide",
        "description": "按项目实况生成规范并发布为公告",
        "description_en": "Generate a style guide from the project's actual state and publish it as an announcement",
        "params": {"with_llm": "用模型润色风格章节"},
        "writes": True,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "POST /projects/{id}/announcements",
    },
    {
        "name": "run",
        "label": "全流程",
        "label_en": "Full pipeline",
        "description": "分派 → 翻译 → 校对 → 术语 → 规范，一次跑完",
        "description_en": "Assign → translate → review → glossary → style guide, all in one run",
        "params": {},
        "writes": True,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "多个",
        "endpoint_en": "Multiple",
    },
    {
        "name": "extract",
        "label": "提取词条",
        "label_en": "Extract strings",
        "description": "跑提取器把游戏文件里的词条抽成 JSONL，不碰平台",
        "description_en": "Run an extractor to pull strings out of game files into JSONL; does not touch the platform",
        "params": {"extractor_id": "提取器 id", "game_dir": "游戏目录", "options": "提取参数",
                   "accept_risk": "必填：确认执行本机代码的风险"},
        "writes": False,
        "agent": "confirm",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "本地文件系统",
        "endpoint_en": "Local filesystem",
    },
    {
        "name": "extractor_generate",
        "label": "Agent 写提取器",
        "label_en": "Agent writes extractor",
        "description": "扫描游戏目录，让模型生成提取脚本并自动试跑",
        "description_en": "Scan the game directory and let the model generate an extractor script, then trial-run it",
        "params": {"game_dir": "游戏目录", "hint": "额外要求（可选）",
                   "accept_risk": "必填：确认执行本机代码的风险"},
        "writes": False,
        "agent": "confirm",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "本地文件系统 + LLM",
        "endpoint_en": "Local filesystem + LLM",
    },
    {
        "name": "extractor_improve",
        "label": "Agent 改提取器",
        "label_en": "Agent improves extractor",
        "description": "看着现有脚本和上次日志，让模型改进并用新版本试跑",
        "description_en": "Show the model the current script and last log, then let it improve and trial-run the new version",
        "params": {"extractor_id": "提取器 id", "game_dir": "游戏目录",
                   "feedback": "改进要求（可选）",
                   "accept_risk": "必填：确认执行本机代码的风险"},
        "writes": False,
        "agent": "confirm",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "本地文件系统 + LLM",
        "endpoint_en": "Local filesystem + LLM",
    },
    {
        "name": "extraction_import",
        "label": "导入提取结果",
        "label_en": "Import extraction",
        "description": "把提取的词条逐条创建到平台指定文件（只新增缺失的 key）",
        "description_en": "Create extracted strings one by one in a platform file (only adds missing keys)",
        "params": {"extraction_id": "提取记录 id", "file_id": "目标文件 id",
                   "force": "跳过校验错误"},
        "writes": True,
        "agent": "confirm",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "POST /projects/{id}/strings",
    },
    {
        "name": "extraction_upload",
        "label": "上传为新文件",
        "label_en": "Upload as new file",
        "description": "把提取结果打成一个 JSON 文件上传，平台按项目格式解析并自动建词条（只适用于 json 格式项目）",
        "description_en": "Package the extraction as a JSON file and upload it; the platform "
                          "parses it per the project format and creates strings automatically "
                          "(json-format projects only)",
        "params": {"extraction_id": "提取记录 id", "filename": "上传文件名（须 .json 结尾）",
                   "path": "目标目录（可选，如 locale/）", "force": "跳过校验错误"},
        "writes": True,
        "agent": "confirm",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "POST /projects/{id}/files",
    },
    {
        "name": "writeback",
        "label": "回写译文到游戏文件",
        "label_en": "Write back to game files",
        "description": "把平台上达到门槛的确认译文，按提取记录的 key 写回游戏文件（默认只输出到旁路目录）",
        "description_en": "Write confirmed translations above the stage threshold back into game files by extraction key (outputs to a side directory by default)",
        "params": {"extraction_id": "提取记录 id", "min_stage": "译文门槛 1/3/5",
                   "out_dir": "输出目录（空=游戏目录旁的 <游戏目录>_cn）",
                   "mode": "mirror 整包镜像 / patch 仅改动文件",
                   "in_place": "原地修改游戏文件（危险，需 accept_risk）",
                   "dry_run": "仅预览，不落盘"},
        "writes": True,
        "agent": "confirm",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "本地文件（读平台 GET /strings）",
        "endpoint_en": "Local files (reads platform GET /strings)",
    },
    {
        "name": "qa_check",
        "label": "单条自查",
        "label_en": "Single-string check",
        "description": "对一条原文/译文跑确定性 QA，译者在网页里自查",
        "description_en": "Run deterministic QA on one original/translation pair, right in the web UI",
        "params": {"original": "原文", "translation": "译文"},
        "writes": False,
        "agent": "allow",   # /api/agent 能否直接执行（缺标记=拦）
        "endpoint": "本地规则",
        "endpoint_en": "Local rules",
    },
]
