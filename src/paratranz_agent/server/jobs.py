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

# 当前正在执行的任务（job_id, tool）。用量统计靠它把成本归到具体任务上。
current_job: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "ptz_current_job", default=("", "")
)


@dataclass
class Job:
    id: str
    tool: str
    args: dict[str, Any]
    status: str = "queued"  # queued | running | done | failed | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    logs: list[dict[str, Any]] = field(default_factory=list)
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
            d["logs"] = self.logs[-200:]
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
        self._order: deque[str] = deque(maxlen=200)
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._cancel: set[str] = set()
        self._worker = threading.Thread(target=self._loop, name="ptz-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ 提交
    def submit(self, tool: str, args: dict[str, Any] | None = None, *, origin: str = "webui") -> Job:
        if tool not in self.registry:
            raise KeyError(f"未知工具: {tool}")
        job = Job(id=uuid.uuid4().hex[:12], tool=tool, args=args or {}, origin=origin)
        with self._lock:
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
            if job is None or job.status != "queued":
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
                if self.is_cancelled(job.id):
                    job.status = "cancelled"
                    self.log(job, "已取消", level="warn")
                else:
                    job.status = "done"
                    job.result = result if isinstance(result, dict) else {"value": result}
                    self.log(job, "完成")
            except Exception as exc:  # noqa: BLE001 - 单任务失败不影响队列
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                self.log(job, f"失败：{job.error}", level="error")
                self.log(job, traceback.format_exc(limit=3), level="debug")
            finally:
                current_job.reset(ctx_token)
                job.finished_at = time.time()
                self._cancel.discard(job.id)
                self.bus.publish({"type": "job", "job": job.as_dict(with_logs=False)})
