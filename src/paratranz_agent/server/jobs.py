"""任务队列 + 事件总线。

为什么单 worker 串行：
- 平台限流按用户算，两个 pipeline 并发跑会互相抢额度；
- 翻译和校对都改词条 stage，并发会出现「刚校对完又被重译」的竞态；
- 用户从 WebUI 连点两个按钮时，串行执行的结果才可预期。

事件流用 SSE（不是 WebSocket）：单向推送足够，浏览器原生支持，
不需要额外依赖，断线自动重连。
"""
from __future__ import annotations

import contextvars
import queue
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

# 单个任务最多保留多少条日志、整个 runner 最多记住多少个任务。
# `_order` 原本就有 maxlen，但 `jobs` 字典没有 —— deque 从左边溢出时没人删字典里的
# 对应条目，于是「最近 200 个任务」的列表和「全部任务」的字典越走越远。
MAX_JOB_LOGS = 500
MAX_JOB_ITEMS = 300
# 最多记住多少个**已结束**的任务。这个上限只管历史，不管排队 ——
# 排队中的任务是"活的"，删掉它就永远不执行、GET 404、cancel 返回 False、
# 也没有任何 SSE 终态（审计实测：205 次提交后第一个任务就这样消失了）。
MAX_JOBS = 200

# 当前正在执行的任务（job_id, tool）。用量统计靠它把成本归到具体任务上。
current_job: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "ptz_current_job", default=("", "")
)


class ToolFailed(RuntimeError):
    """工具跑完了，但结果不能算成功（典型：取材 200 条、模型全失败、一条没写）。

    带着 `result` 一起抛：任务状态必须是 failed（否则界面只显示「完成」），
    但报表也要留下来，否则用户仍然看不到"到底做了什么、卡在哪一步"。
    """

    def __init__(self, message: str, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result


@dataclass
class Job:
    id: str
    tool: str
    args: dict[str, Any]
    status: str = "queued"  # queued | running | done | failed | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    # 日志只用来看最近发生了什么，给个上限：长跑任务（全量校对 9000 条）
    # 每个 job 挂几千条 dict，字典又没有淘汰机制，内存只会一直涨。
    logs: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_JOB_LOGS))
    # 逐条进度（词条级）。界面用它显示"AI 此刻在处理哪些词条"，
    # 刷新页面后也能靠它恢复最近一段现场；只留最近 300 条，不参与判定。
    items: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_JOB_ITEMS))
    result: dict[str, Any] | None = None
    error: str | None = None
    origin: str = "webui"  # webui | agent | cli

    def as_dict(self, *, with_logs: bool = True) -> dict[str, Any]:
        d = {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "origin": self.origin,
            "duration": (
                round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at
                else None
            ),
        }
        if with_logs:
            d["logs"] = list(self.logs)[-200:]
            d["items"] = list(self.items)[-100:]
        return d


class EventBus:
    """每个订阅者一个 Queue；发布时不阻塞（满了丢最旧）。"""

    def __init__(self, history: int = 300):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._history: deque[dict] = deque(maxlen=history)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        # 补发最近事件，避免刷新页面后日志空白
        for ev in list(self._history)[-50:]:
            try:
                q.put_nowait(ev)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        event = {"ts": time.time(), **event}
        self._history.append(event)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


class JobRunner:
    """单 worker 线程池（就一个线程）+ 可取消标记。"""

    def __init__(
        self,
        bus: EventBus,
        registry: dict[str, Callable[..., dict]],
        sink: Any = None,
    ):
        self.bus = bus
        self.registry = registry
        self.sink = sink  # LogStore：任务日志同时进日志页与磁盘
        self.jobs: dict[str, Job] = {}
        # 普通 list：用 `deque(maxlen)` 时溢出会**静默丢掉最老的 id**，
        # 那个任务就变成没人认领的孤儿（状态 queued、GET 404、永不被执行）。
        self._order: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._cancel: set[str] = set()
        self._worker = threading.Thread(target=self._loop, name="ptz-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ 提交
    def _prune_locked(self) -> None:
        """淘汰多余的**已结束**任务（调用方必须已持有 `self._lock`）。

        deque 从左边溢出的条目要同步从字典里删掉，否则内存无上限增长。
        但**只驱逐已结束的**：正在跑的那个被删掉后，`GET /api/jobs/{id}` 会 404、
        cancel 返回 False，而 pipeline 还在对它写日志与逐条进度 —— 结果无处可查。
        不变量：`jobs` 里最多 MAX_JOBS 个"已结束"任务，**排队与运行中的一个都不删**。
        以前 `_order` 是 deque(maxlen)，溢出时它自己丢掉最老的 id，字典里那条就成孤儿：
        状态还是 queued、`get()` 拿不到、`cancel()` 返回 False、永远不会执行，
        也没有任何 SSE 终态（审计实测 205 次提交后第一个任务就这样消失了）。
        """
        while len(self.jobs) >= MAX_JOBS:
            dropped = next(
                (
                    i
                    for i in self._order
                    if i in self.jobs and self.jobs[i].status not in ("queued", "running")
                ),
                None,
            )
            if dropped is None:
                return      # 全在排队/运行：宁可暂时超标，也不删活任务
            self.jobs.pop(dropped, None)
            self._cancel.discard(dropped)
            try:
                self._order.remove(dropped)
            except ValueError:  # pragma: no cover - 并发下已被移走
                return

    def submit(self, tool: str, args: dict[str, Any] | None = None, *, origin: str = "webui") -> Job:
        if tool not in self.registry:
            raise KeyError(f"未知工具: {tool}")
        job = Job(id=uuid.uuid4().hex[:12], tool=tool, args=args or {}, origin=origin)
        with self._lock:
            self._prune_locked()
            self.jobs[job.id] = job
            self._order.append(job.id)
        self._queue.put(job.id)
        self.bus.publish({"type": "job", "job": job.as_dict(with_logs=False)})
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = time.time()
                self.bus.publish({"type": "job", "job": job.as_dict(with_logs=False)})
                return True
            if job.status == "running":
                self._cancel.add(job_id)  # 由 pipeline 在检查点自行退出
                self.log(job, "收到取消请求，将在当前批次结束后停止", level="warn")
                return True
        return False

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancel

    def log(self, job: Job, msg: str, *, level: str = "info") -> None:
        entry = {"ts": time.time(), "level": level, "msg": msg}
        job.logs.append(entry)
        self.bus.publish({"type": "log", "job_id": job.id, "entry": entry})
        if self.sink is not None:
            try:
                self.sink.add(msg, level=level, job_id=job.id, source=job.tool)
            except Exception:  # noqa: BLE001 - 日志写失败不影响任务
                pass

    def item(self, job: Job, items: Any) -> None:
        """逐条进度：`[{id, original?, translation?, status, reason?}]`。

        只做展示（界面上的"正在处理的词条"面板与词条列表联动），
        **不参与任何判定** —— 成功与否一律看任务结果里的计数。
        所以这里不做去重也不抛错，坏数据直接跳过即可。
        """
        rows = [x for x in items or [] if isinstance(x, dict) and x.get("id") is not None]
        if not rows:
            return
        for r in rows:
            job.items.append(r)
        self.bus.publish({"type": "item", "job_id": job.id, "items": rows})

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            ids = list(self._order)[-limit:]
            return [self.jobs[i].as_dict(with_logs=False) for i in reversed(ids) if i in self.jobs]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self.jobs.get(job_id)

    # ------------------------------------------------------------------ 执行
    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                # 真丢了也要给终态：静默 continue 会让界面永远停在 queued
                self.bus.publish(
                    {
                        "type": "job",
                        "job": {
                            "id": job_id, "tool": "", "args": {}, "status": "failed",
                            "error": "任务记录已丢失（可能被历史淘汰），未执行", "origin": "webui",
                        },
                    }
                )
                continue
            if job.status != "queued":
                continue
            fn = self.registry.get(job.tool)
            if fn is None:
                job.status, job.error, job.finished_at = "failed", "工具不存在", time.time()
                continue

            job.status, job.started_at = "running", time.time()
            self.bus.publish({"type": "job", "job": job.as_dict(with_logs=False)})
            self.log(job, f"开始执行 {job.tool} {job.args or ''}")
            ctx_token = current_job.set((job.id, job.tool))
            try:
                result = fn(job=job, runner=self, **job.args)
                # 结果先落地再说取消：pipeline 是在「检查点」退出的，
                # 它返回的那份统计里就写着"已经写进平台多少条"。原先取消时
                # 直接把结果丢掉，用户看到的是一个 cancelled 的空任务，
                # 完全不知道平台已经被改了多少（审计报告5 服务端）。
                job.result = result if isinstance(result, dict) else {"value": result}
                if self.is_cancelled(job.id):
                    job.status = "cancelled"
                    self.log(job, "已取消（已写入的部分结果保留在 result 里）", level="warn")
                else:
                    job.status = "done"
                    self.log(job, "完成")
            except ToolFailed as exc:
                # 跑完了但结果不能算成功：状态必须 failed，同时把报表留下
                job.status = "failed"
                job.error = str(exc)
                job.result = exc.result
                self.log(job, f"未完成：{exc}", level="error")
            except Exception as exc:  # noqa: BLE001 - 单任务失败不影响队列
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                # 带部分报表的异常（WriteInterrupted / StageWriteInterrupted）：
                # 里面写着"平台已经改了多少条"，丢掉它等于让用户自己去平台里数。
                partial = getattr(exc, "stats", None)
                if partial is not None and hasattr(partial, "as_dict"):
                    try:
                        job.result = partial.as_dict()
                    except Exception:  # noqa: BLE001 - 报表序列化失败不影响报错
                        job.result = None
                self.log(job, f"失败：{job.error}", level="error")
                self.log(job, traceback.format_exc(limit=3), level="debug")
            finally:
                current_job.reset(ctx_token)
                job.finished_at = time.time()
                self._cancel.discard(job.id)
                self.bus.publish({"type": "job", "job": job.as_dict(with_logs=False)})
                # 结束时再清一次：提交时它们可能都还在排队（无处可淘汰），
                # 不在这里收尾，字典会一直比上限多
                with self._lock:
                    self._prune_locked()
