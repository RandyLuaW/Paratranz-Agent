"""日志：内存环形缓冲 + 按天落盘 + 可查询/下载。

为什么要自己收而不是只靠 print：
- WebUI 要能按级别、关键字、任务 ID 过滤，还要能下载全文给用户排查；
- 守护进程跑一周后日志几十 MB，必须有上限（按天切分 + 单文件上限）；
- 任务日志和系统日志要能互相关联（同一条 log 带 job_id）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from typing import Any

LEVELS = ("debug", "info", "warn", "error")


@dataclass
class LogEntry:
    ts: float
    level: str
    msg: str
    job_id: str = ""
    source: str = "system"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "date": time.strftime("%Y-%m-%d", time.localtime(self.ts)),
            "level": self.level,
            "msg": self.msg,
            "job_id": self.job_id,
            "source": self.source,
        }

    def line(self) -> str:
        d = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))
        jid = f" [{self.job_id}]" if self.job_id else ""
        return f"{d} {self.level.upper():5} {self.source}{jid} {self.msg}"


class LogStore:
    def __init__(
        self,
        log_dir: str | Path,
        *,
        buffer: int = 3000,
        max_mb_per_file: int = 8,
        redactor: "Callable[[str], str] | None" = None,
    ):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_mb_per_file * 1024 * 1024
        self._buf: deque[LogEntry] = deque(maxlen=buffer)
        self._lock = threading.RLock()
        self._seq = 0
        # 落盘前脱敏：日志里绝不允许出现明文密钥
        self._redact = redactor or (lambda x: x)

    # ------------------------------------------------------------------ 写入
    def add(self, msg: str, *, level: str = "info", job_id: str = "", source: str = "system") -> LogEntry:
        entry = LogEntry(ts=time.time(), level=level if level in LEVELS else "info",
                         msg=self._redact(str(msg))[:4000], job_id=job_id, source=source)
        with self._lock:
            self._buf.append(entry)
            self._seq += 1
            self._append_file(entry)
        return entry

    def _append_file(self, entry: LogEntry) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(entry.ts))
        path = self.dir / f"agent-{day}.log"
        try:
            if path.exists() and path.stat().st_size > self.max_bytes:
                # 超过上限就滚动：agent-2026-01-01.log → .1.log
                rolled = path.with_suffix(f".{int(time.time())}.log")
                path.rename(rolled)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(entry.line() + "\n")
        except OSError:
            pass  # 落盘失败不影响主流程

    # ------------------------------------------------------------------ 查询
    def query(
        self,
        *,
        level: str | None = None,
        keyword: str = "",
        job_id: str = "",
        limit: int = 300,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._buf)
        min_sev = LEVELS.index(level) if level in LEVELS else -1
        out = []
        for e in reversed(items):
            if min_sev >= 0 and LEVELS.index(e.level) < min_sev:
                continue
            if job_id and e.job_id != job_id:
                continue
            if since and e.ts < since:
                continue
            if keyword and keyword.lower() not in e.msg.lower():
                continue
            out.append(e.as_dict())
            if len(out) >= limit:
                break
        return out

    def files(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob("agent-*.log"), reverse=True)[:60]:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def read_file(self, name: str, *, tail: int = 800) -> str:
        """只允许读本目录下的 agent-*.log，防路径穿越。"""
        safe = Path(name).name
        if not safe.startswith("agent-") or not safe.endswith(".log"):
            raise ValueError("非法日志文件名")
        path = self.dir / safe
        if not path.exists():
            raise FileNotFoundError(safe)
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            lines = fp.readlines()
        # 老文件可能是脱敏前写的，读出来再过一遍
        return self._redact("".join(lines[-tail:]))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._buf)
        counts = {lv: 0 for lv in LEVELS}
        for e in items:
            counts[e.level] = counts.get(e.level, 0) + 1
        files = self.files()
        return {
            "buffer": len(items),
            "levels": counts,
            "files": len(files),
            "disk_bytes": sum(f["size"] for f in files),
            "dir": str(self.dir),
        }

    def clear_buffer(self) -> None:
        with self._lock:
            self._buf.clear()
