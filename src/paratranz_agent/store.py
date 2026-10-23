"""本地状态：翻译记忆（TM）+ 术语缓存 + 运行账本（幂等）。

为什么本地要有一份 TM：
- 平台 suggestions 接口串行且慢，日常翻译用本地 TM 命中更快；
- 自动流程必须可重放：同一词条本轮处理过就不再重复提交（账本）；
- 术语提炼需要"已定稿译文"的统计视图，不能每次全量拉平台。

线程安全：翻译流水线用线程池并发调用 LLM，而每个线程都要查 TM / 写账本。
SQLite 连接默认禁止跨线程使用，所以这里 `check_same_thread=False` 并用
可重入锁串行化所有数据库访问——单次查询是毫秒级，不会成为瓶颈。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS tm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    project INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'mt',      -- mt | human | platform
    quality REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    UNIQUE(src_hash, target, origin)
);
CREATE INDEX IF NOT EXISTS idx_tm_hash ON tm(src_hash);

CREATE TABLE IF NOT EXISTS term (
    term TEXT PRIMARY KEY,
    translation TEXT NOT NULL,
    pos TEXT,
    note TEXT,
    variants TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'platform',
    status TEXT NOT NULL DEFAULT 'active',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    UNIQUE(pipeline, item_id, action)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def src_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def _bigrams(text: str) -> set[str]:
    t = "".join(ch for ch in text.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """bigram Dice 系数，够用且无需第三方依赖。"""
    if not a or not b:
        return 0.0
    if a.strip() == b.strip():
        return 1.0
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


@dataclass
class TMHit:
    source: str
    target: str
    score: float
    origin: str


def open_sqlite(path: str | Path) -> sqlite3.Connection:
    """打开本地 SQLite 连接：**跨进程同时可用**是硬要求。

    WebUI 常驻时会一直握着连接，与此同时在命令行跑 `ptz translate` 是日常操作。
    默认配置下第二个写者会立刻拿到 `database is locked`（sqlite3 的默认等待只有
    5 秒，而且没有 busy handler 的语义保证），整条流水线就断了 —— 而用户看到的
    只是一个莫名其妙的失败。

    这里统一做三件事：
    - `timeout=30`：给写锁 30 秒等待窗口；
    - `PRAGMA busy_timeout`：读写路径都遵守同一个等待时间；
    - `journal_mode=WAL`：允许「一个写者 + 多读者」并存，读不再被写阻塞。

    WAL 在只读介质/网络盘上可能不生效，此时 PRAGMA 会**返回原模式而不是报错**，
    连接照样可用（等待时间仍然放宽了），所以不需要额外兜底分支。
    """
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = open_sqlite(self.path)
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- TM
    def tm_add(
        self,
        source: str,
        target: str,
        *,
        project: int = 0,
        origin: str = "mt",
        quality: float = 0.5,
    ) -> None:
        if not source.strip() or not target.strip():
            return
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO tm(src_hash, source, target, project, origin, quality, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (src_hash(source), source, target, project, origin, quality, time.time()),
            )
            self.db.commit()

    def tm_add_many(
        self,
        rows: Iterable[tuple[str, str]],
        *,
        project: int = 0,
        origin: str = "mt",
        quality: float = 0.6,
    ) -> int:
        now = time.time()
        payload = [
            (src_hash(s), s, t, project, origin, quality, now)
            for s, t in rows
            if s and s.strip() and t and t.strip()
        ]
        if not payload:
            return 0
        with self._lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO tm(src_hash, source, target, project, origin, quality, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                payload,
            )
            self.db.commit()
        return len(payload)

    def tm_exact(self, source: str) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT target FROM tm WHERE src_hash=? ORDER BY quality DESC, created_at DESC LIMIT 1",
                (src_hash(source),),
            ).fetchone()
        return row["target"] if row else None

    def tm_search(self, source: str, *, top_k: int = 5, min_score: float = 0.45) -> list[TMHit]:
        exact = self.tm_exact(source)
        if exact:
            return [TMHit(source, exact, 1.0, "exact")]
        # 粗筛：同首两字符或包含原文前 6 字符，避免每次全表算 bigram
        prefix = source.strip()[:2]
        with self._lock:
            rows = self.db.execute(
                "SELECT source, target, origin FROM tm WHERE substr(source,1,2)=? OR source LIKE ? LIMIT 400",
                (prefix, f"%{source.strip()[:6]}%"),
            ).fetchall()
            if not rows:
                rows = self.db.execute(
                    "SELECT source, target, origin FROM tm ORDER BY id DESC LIMIT 400"
                ).fetchall()
        hits: list[TMHit] = []
        for r in rows:
            sc = similarity(source, r["source"])
            if sc >= min_score:
                hits.append(TMHit(r["source"], r["target"], sc, r["origin"]))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def tm_size(self) -> int:
        with self._lock:
            row = self.db.execute("SELECT COUNT(*) c FROM tm").fetchone()
        return int(row["c"])

    # -------------------------------------------------------------- Terms
    def term_upsert(
        self,
        term: str,
        translation: str,
        *,
        pos: str | None = None,
        note: str | None = None,
        variants: Sequence[str] = (),
        source: str = "platform",
        status: str = "active",
    ) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO term(term, translation, pos, note, variants, source, status, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(term) DO UPDATE SET translation=excluded.translation,"
                " pos=excluded.pos, note=excluded.note, variants=excluded.variants,"
                " source=excluded.source, status=excluded.status, updated_at=excluded.updated_at",
                (term, translation, pos, note, json.dumps(list(variants), ensure_ascii=False),
                 source, status, time.time()),
            )

    def term_all(self, *, status: str = "active") -> dict[str, dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM term WHERE status=?", (status,)).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            d["variants"] = json.loads(d["variants"] or "[]")
            out[d["term"]] = d
        return out

    def glossary_for(
        self,
        texts: Sequence[str],
        *,
        terms: dict[str, dict] | None = None,
        limit: int = 40,
        with_pos: bool = False,
    ) -> list[dict]:
        """筛出「出现在这批原文里」的术语，供 prompt 使用。

        两处调用（翻译、校对）原先各自实现了一遍同样的过滤逻辑，合并到这里；
        `with_pos` 只有翻译侧需要。

        **`terms` 参数是为了避免长任务里的重复全表读**：`term_all()` 每次都要
        `SELECT * FROM term` 并重建字典，而 prompt 是按批构造的——
        9087 条 ÷ 40 条/批 = 227 次调用。调用方应在 run() 开头取一次再传进来。
        """
        pool = self.term_all() if terms is None else terms
        if not pool:
            return []
        joined = "\n".join(texts)
        hits: list[dict] = []
        for term, meta in pool.items():
            variants = [term, *(meta.get("variants") or [])]
            if not any(v and v in joined for v in variants):
                continue
            row = {"term": term, "translation": meta["translation"]}
            if with_pos:
                row["pos"] = meta.get("pos") or ""
            hits.append(row)
            if len(hits) >= limit:
                break
        return hits

    def term_commit(self) -> None:
        with self._lock:
            self.db.commit()

    # ------------------------------------------------------------- Ledger
    def ledger_done(self, pipeline: str, item_id: str, action: str) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM ledger WHERE pipeline=? AND item_id=? AND action=?",
                (pipeline, item_id, action),
            ).fetchone()
        return row is not None

    def ledger_mark(self, pipeline: str, item_id: str, action: str, detail: str = "") -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO ledger(pipeline, item_id, action, detail, ts) VALUES(?,?,?,?,?)",
                (pipeline, str(item_id), action, detail[:500], time.time()),
            )

    def ledger_commit(self) -> None:
        with self._lock:
            self.db.commit()

    def ledger_stats(self, pipeline: str) -> dict[str, int]:
        with self._lock:
            rows = self.db.execute(
                "SELECT action, COUNT(*) c FROM ledger WHERE pipeline=? GROUP BY action", (pipeline,)
            ).fetchall()
        return {r["action"]: int(r["c"]) for r in rows}

    # --------------------------------------------------------------- Meta
    def meta_set(self, k: str, v: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
            self.db.commit()

    def meta_get(self, k: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None
