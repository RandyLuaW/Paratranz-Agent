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
    project INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pipeline, item_id, action, project)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def src_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


# --------------------------------------------------------------- schema 迁移
# 每张表「后来加的列」。迁移只补**缺的那些**：
# `CREATE TABLE IF NOT EXISTS` 对已存在的表一个字都不改，所以给老库加列只能靠
# `ALTER TABLE ADD COLUMN`；而重复 ALTER 会直接抛 duplicate column name，
# 必须先读 `PRAGMA table_info` 比对。这里的声明要与 SCHEMA 里的列定义同口径。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "term": {
        "pos": "TEXT",
        "note": "TEXT",
        "variants": "TEXT NOT NULL DEFAULT '[]'",
        "source": "TEXT NOT NULL DEFAULT 'platform'",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
}


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """v1：给术语表补齐可选列。

    老库（v0）只有 `term` / `translation` 两列，而 `term_upsert` 会显式写 pos /
    variants / status 等列 —— 列不存在时整条流水线以
    `table term has no column named pos` 失败，用户升级后一次都跑不起来。
    """
    for table, columns in _ADDED_COLUMNS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v2：账本加 `project` 列。

    `ledger` 原先没有项目维度，`status` 里的「已处理 N 条」是**跨项目累计**：
    换一个项目看到的数字里混着上一个项目的记录，而词条 id 虽然全局唯一，
    "这个项目处理了多少"却是统计上必须回答的问题。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
    if "project" not in cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN project INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_project ON ledger(project, pipeline)")


def _ledger_unique_columns(conn: sqlite3.Connection) -> list[list[str]]:
    """账本上所有唯一索引的列名（用于判断唯一键是否已包含 project）。"""
    out: list[list[str]] = []
    for idx in conn.execute("PRAGMA index_list(ledger)").fetchall():
        if not idx[2]:        # idx[2] = unique 标志
            continue
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()]
        out.append(list(cols))
    return out


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v3：账本唯一键补上 `project`。

    公告 id、任务 id 这类对象都是**每个项目各自编号**的，
    `UNIQUE(pipeline, item_id, action)` 不含项目时，项目 B 的记录会被
    `INSERT OR IGNORE` 静默丢弃；`replace=True` 更糟——直接把项目 A 的记录抹掉。
    """
    merged = ["pipeline", "item_id", "action", "project"]
    if merged in _ledger_unique_columns(conn):
        return
    conn.execute("ALTER TABLE ledger RENAME TO ledger_old")
    conn.execute(
        """
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    project INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pipeline, item_id, action, project)
);
"""
    )
    conn.execute(
        "INSERT INTO ledger(id, pipeline, item_id, action, detail, ts, project) "
        "SELECT id, pipeline, item_id, action, detail, ts, project FROM ledger_old"
    )
    conn.execute("DROP TABLE ledger_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_project ON ledger(project, pipeline)")


# 迁移步骤：下标 + 1 就是它升到的版本号，严格按顺序执行，只能往后追加。
MIGRATIONS = (_migrate_to_v1, _migrate_to_v2, _migrate_to_v3)

# 当前 schema 版本，记在库文件的 `PRAGMA user_version` 里（不用 meta 表：版本号
# 必须在建表之前就读得到）。由迁移表长度推出，避免常数和步骤数各写一遍而走样。
SCHEMA_VERSION = len(MIGRATIONS)


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
    # `PRAGMA journal_mode = WAL` 在**并发首次建库**时会立刻抛
    # `database is locked`（实测 0.001s，`busy_timeout` 对它不生效）——
    # WebUI 与 CLI 同时首次启动就有一个直接失败。所以：先读，已是 wal 就不动；
    # 需要切换时短暂重试几次，仍失败就保持原模式（连接照样可用，只是没有 WAL）。
    try:
        mode = (conn.execute("PRAGMA journal_mode").fetchone() or [""])[0]
        if str(mode).lower() != "wal":
            for attempt in range(5):
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError:
                    if attempt == 4:
                        break
                    time.sleep(0.05 * (attempt + 1))
    except sqlite3.OperationalError:
        pass
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
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """按 `PRAGMA user_version` 把库逐级升到 `len(MIGRATIONS)`。

        没有这一层时 `CREATE TABLE IF NOT EXISTS` 会把老库原样放过：后来给 term
        加的列永远不会补上，升级后的第一轮就是 `table term has no column named pos`。
        """
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version > len(MIGRATIONS):
            # 旧程序打开新库：硬报错，别让它在缺表/缺列的库上继续写
            raise RuntimeError(
                f"本地状态库版本 {version} 高于本程序支持的 {len(MIGRATIONS)}（{self.path}），"
                f"请升级 paratranz-agent 后重试"
            )
        for target in range(version, len(MIGRATIONS)):
            # 迁移必须**整体成功或整体回退**：`BEGIN IMMEDIATE` 先拿写锁，
            # 多进程同时启动时后来者等锁（busy_timeout 30s）而不是并行改表；
            # `ALTER TABLE` 是 DDL，在 sqlite3 里会隐式开事务，显式 BEGIN 前
            # 先确保没有悬挂事务，否则直接抛 "cannot start a transaction within a transaction"。
            if self.db.in_transaction:  # pragma: no cover - 正常路径不会进
                self.db.rollback()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[target](self.db)
                # PRAGMA 不支持参数绑定；这里的值来自 range，不是外部输入
                self.db.execute(f"PRAGMA user_version = {target + 1}")
            except Exception:
                self.db.rollback()
                raise
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

    def tm_exact(self, source: str, *, project: int = 0) -> str | None:
        """精确命中一条记忆译文。

        `project` 必须传：`agent.db` 是**按 state 目录共用**的，A 项目写进去的
        机翻如果在 B 项目命中，translate 会「只过 QA 就直接写进平台」，还报
        「TM 命中」（看着是好事）。`project=0` 表示通用池（人工确认过的译法）。
        """
        with self._lock:
            row = self.db.execute(
                "SELECT target FROM tm WHERE src_hash=? AND project IN (0, ?)"
                " ORDER BY quality DESC, created_at DESC LIMIT 1",
                (src_hash(source), int(project)),
            ).fetchone()
        return row["target"] if row else None

    def tm_search(
        self, source: str, *, top_k: int = 5, min_score: float = 0.45, project: int = 0
    ) -> list[TMHit]:
        exact = self.tm_exact(source, project=project)
        if exact:
            return [TMHit(source, exact, 1.0, "exact")]
        # 粗筛：同首两字符或包含原文前 6 字符，避免每次全表算 bigram
        prefix = source.strip()[:2]
        with self._lock:
            rows = self.db.execute(
                "SELECT source, target, origin FROM tm WHERE project IN (0, ?)"
                " AND (substr(source,1,2)=? OR source LIKE ?) LIMIT 400",
                (int(project), prefix, f"%{source.strip()[:6]}%"),
            ).fetchall()
            if not rows:
                rows = self.db.execute(
                    "SELECT source, target, origin FROM tm WHERE project IN (0, ?)"
                    " ORDER BY id DESC LIMIT 400",
                    (int(project),),
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
            # 自己提交：以前靠调用方 `term_commit()`，忘了提交就在 close()
            # 时被回滚（无返回、无日志 = 术语静默丢失），而且未提交的写事务
            # 会一直握着写锁，WebUI 与 CLI 同时跑时另一个进程 30 秒后拿到
            # `database is locked`。
            self.db.commit()

    def term_all(self, *, status: str = "active") -> dict[str, dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM term WHERE status=?", (status,)).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            try:
                d["variants"] = json.loads(d["variants"] or "[]")
            except (TypeError, ValueError) as exc:
                # 认不出就当空 = 这个术语的同义变体永远不参与命中，且没人知道。
                raise RuntimeError(
                    f"术语 {d.get('term')!r} 的 variants 列不是合法 JSON：{d['variants']!r}"
                ) from exc
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
        # 与确定性校验（`qa.check_terms`）共用同一套命中口径：ASCII 整词、中文子串。
        # 口径不一致会让原文里压根没有的术语被塞进批次上下文 —— 白占 token，
        # 还会诱导模型把无关术语硬套进去。
        from .qa import term_hit

        hits: list[dict] = []
        for term, meta in pool.items():
            variants = [term, *(meta.get("variants") or [])]
            if not any(term_hit(joined, v) for v in variants if v):
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
    def ledger_done(
        self, pipeline: str, item_id: str, action: str, *, project: int | None = None
    ) -> bool:
        """这条记录是否已存在（幂等判断/测试断言用）。`project` 给了就限定项目。"""
        sql = "SELECT 1 FROM ledger WHERE pipeline=? AND item_id=? AND action=?"
        params: list[object] = [pipeline, str(item_id), action]
        if project is not None:
            sql += " AND project=?"
            params.append(int(project))
        with self._lock:
            row = self.db.execute(sql, params).fetchone()
        return row is not None

    def ledger_mark(
        self,
        pipeline: str,
        item_id: str,
        action: str,
        detail: str = "",
        *,
        replace: bool = False,
        project: int = 0,
    ) -> None:
        """记一笔。`replace=True` 时同一条目的同一动作会被**覆盖**（ts 也跟着更新）。

        默认 `INSERT OR IGNORE` 是为了幂等（同一轮重跑不重复记账）；但人工编辑
        这类"同一动作会发生多次"的记录必须覆盖，否则界面上永远只显示第一次的
        时间，看起来像"这条上周改过一次、之后没人动"。

        `project` 必须有：账本原先没有项目维度，`status` 的「已处理 N 条」是跨项目
        累计，换个项目看到的数字里混着上一个项目的记录。
        """
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        with self._lock:
            self.db.execute(
                f"{verb} INTO ledger(pipeline, item_id, action, detail, ts, project) "
                "VALUES(?,?,?,?,?,?)",
                (pipeline, str(item_id), action, detail[:500], time.time(), int(project)),
            )
            # 同 term_upsert：自己提交，别把「写事务没提交」留给调用方决定
            self.db.commit()

    def ledger_commit(self) -> None:
        with self._lock:
            self.db.commit()

    def ledger_stats(self, pipeline: str, *, project: int | None = None) -> dict[str, int]:
        """按动作统计条数。

        `project` 给了就只数该项目，**但包含 project=0 的老记录**：
        project 列是 v2 才加的，之前所有记录都是 0（无从判断属于哪个项目）。
        排除它们会让升级后的 `ptz status` 从 0 开始，看起来像"数据没了"。
        """
        sql = "SELECT action, COUNT(*) c FROM ledger WHERE pipeline=?"
        params: list[object] = [pipeline]
        if project is not None:
            sql += " AND project IN (0, ?)"
            params.append(int(project))
        sql += " GROUP BY action"
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        return {r["action"]: int(r["c"]) for r in rows}

    def ledger_for_item(
        self, item_id: str | int, *, limit: int = 50, project: int | None = None
    ) -> list[dict]:
        """某条词条的本地处理记录（谁/哪个环节/什么动作/时间）。

        为什么需要它：平台的 `history` **只记网页端操作**，用批量接口写回的改动
        根本不进历史（实测：改完译文再读 `/strings/{id}`，history 仍是 0 条）。
        所以"这条被我们的流水线动过什么"只能靠本地账本，两边合起来才是完整记录。
        """
        sql = "SELECT pipeline, action, detail, ts, project FROM ledger WHERE item_id=?"
        params: list[object] = [str(item_id)]
        if project is not None:
            # 同上：project=0 是"列存在之前"的老记录，不能因为过滤就看不见
            sql += " AND project IN (0, ?)"
            params.append(int(project))
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        return [
            {
                "pipeline": r["pipeline"],
                "action": r["action"],
                "detail": r["detail"],
                "ts": r["ts"],
                "project": int(r["project"]),
            }
            for r in rows
        ]

    # --------------------------------------------------------------- Meta
    def meta_set(self, k: str, v: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
            self.db.commit()

    def meta_get(self, k: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None
