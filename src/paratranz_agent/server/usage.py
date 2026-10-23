"""用量采集、成本计算与预算护栏。

按汉化场景设计：逐次调用记账 + 四维预算（单次 / 单任务 / 每日 / 每月），

- **计费**：`per_token` 四档（input / input_cached / output / cache_creation），
  单位 USD/百万 token。模型名匹配走「精确 > 前缀 > 子串，最长优先」，
  所以 `gpt-4o-mini-2024-07-18` 能自动命中 `gpt-4o-mini` 的价。
- **预算**：四个维度（单次任务 / 单日 / 单模型 / 全项目），动作三档
  （`warn` 只告警、`stop` 硬拦、`downgrade` 换便宜模型）。
  汉化场景不需要"单用户"，但需要"单次任务"——一次 translate 跑飞最烧钱。
- **存储**：SQLite 单表 `usage`，按 job 关联，便于回答"这次任务花了多少"。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..store import open_sqlite

# 内置定价（USD / 百万 token）。字段含义：
#   input          —— 非缓存输入
#   input_cached   —— 缓存命中输入
#   output         —— 输出
#   cache_creation —— 缓存写入（非 Anthropic 系模型按 input 价占位，保守不低估）
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.5, "input_cached": 1.25, "output": 10.0, "cache_creation": 2.5},
    "gpt-4o-mini": {"input": 0.15, "input_cached": 0.075, "output": 0.6, "cache_creation": 0.15},
    "gpt-4.1": {"input": 2.0, "input_cached": 0.5, "output": 8.0, "cache_creation": 2.0},
    "gpt-4.1-mini": {"input": 0.4, "input_cached": 0.1, "output": 1.6, "cache_creation": 0.4},
    "gpt-4.1-nano": {"input": 0.1, "input_cached": 0.025, "output": 0.4, "cache_creation": 0.1},
    "o3-mini": {"input": 1.1, "input_cached": 0.55, "output": 4.4, "cache_creation": 1.1},
    "claude-3-5-haiku": {"input": 0.8, "input_cached": 0.08, "output": 4.0, "cache_creation": 1.0},
    "claude-3-5-sonnet": {"input": 3.0, "input_cached": 0.3, "output": 15.0, "cache_creation": 3.75},
    "claude-sonnet-4": {"input": 3.0, "input_cached": 0.3, "output": 15.0, "cache_creation": 3.75},
    "claude-opus-4": {"input": 15.0, "input_cached": 1.5, "output": 75.0, "cache_creation": 18.75},
    "deepseek-chat": {"input": 0.27, "input_cached": 0.07, "output": 1.1, "cache_creation": 0.27},
    "deepseek-reasoner": {"input": 0.55, "input_cached": 0.14, "output": 2.19, "cache_creation": 0.55},
    "qwen-plus": {"input": 0.4, "input_cached": 0.1, "output": 1.2, "cache_creation": 0.4},
    "qwen-turbo": {"input": 0.05, "input_cached": 0.012, "output": 0.2, "cache_creation": 0.05},
    "glm-4-flash": {"input": 0.02, "input_cached": 0.005, "output": 0.02, "cache_creation": 0.02},
    "glm-4-plus": {"input": 0.7, "input_cached": 0.18, "output": 0.7, "cache_creation": 0.7},
}

# 兜底价：没命中任何定价时用它，避免成本显示为 0 造成错觉
FALLBACK_PRICE = {"input": 1.0, "input_cached": 0.25, "output": 3.0, "cache_creation": 1.0}

BUDGET_ACTIONS = ("warn", "stop", "downgrade")


def match_pricing(model: str, table: dict[str, dict[str, float]]) -> tuple[str | None, dict[str, float]]:
    """模型名 → 定价。精确 > 前缀 > 子串，最长优先。"""
    if not model:
        return None, dict(FALLBACK_PRICE)
    m = model.lower()
    if m in table:
        return m, table[m]
    # 去厂商前缀：openai/gpt-4o → gpt-4o
    tail = m.rsplit("/", 1)[-1]
    if tail in table:
        return tail, table[tail]

    best: tuple[str, dict[str, float]] | None = None
    for key, price in table.items():
        if tail.startswith(key) or m.startswith(key):
            if best is None or len(key) > len(best[0]):
                best = (key, price)
    if best:
        return best
    for key, price in table.items():
        if key in tail or key in m:
            if best is None or len(key) > len(best[0]):
                best = (key, price)
    return best if best else (None, dict(FALLBACK_PRICE))


def compute_cost(
    price: dict[str, float],
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD。cached 部分从 input 里扣出来按缓存价算。"""
    uncached = max(0, prompt_tokens - cached_tokens)
    return (
        uncached / 1e6 * price.get("input", 0.0)
        + cached_tokens / 1e6 * price.get("input_cached", price.get("input", 0.0))
        + completion_tokens / 1e6 * price.get("output", 0.0)
        + cache_write_tokens / 1e6 * price.get("cache_creation", price.get("input", 0.0))
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    job_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    day TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_job ON usage(job_id);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day);

CREATE TABLE IF NOT EXISTS budget_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    spent_usd REAL NOT NULL DEFAULT 0,
    limit_usd REAL NOT NULL DEFAULT 0,
    action TEXT NOT NULL DEFAULT 'warn'
);
"""


@dataclass
class BudgetRule:
    """一个维度的预算：token 与美元双上限，任一触发即执行动作。"""

    name: str
    enabled: bool = True
    max_tokens: int = 0        # 0 = 不限制
    max_usd: float = 0.0       # 0 = 不限制
    action: str = "warn"       # warn | stop | downgrade
    fallback_model: str = ""   # action=downgrade 时切到哪个模型


@dataclass
class BudgetConfig:
    per_job: BudgetRule = field(default_factory=lambda: BudgetRule("单次任务", max_usd=0.50, action="stop"))
    daily: BudgetRule = field(default_factory=lambda: BudgetRule("每日", max_usd=5.0, action="stop"))
    per_model: BudgetRule = field(default_factory=lambda: BudgetRule("单模型每日", max_usd=3.0, action="warn"))
    monthly: BudgetRule = field(default_factory=lambda: BudgetRule("每月", max_usd=100.0, action="warn"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "per_job": self.per_job.__dict__,
            "daily": self.daily.__dict__,
            "per_model": self.per_model.__dict__,
            "monthly": self.monthly.__dict__,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetConfig":
        out = cls()
        for key in ("per_job", "daily", "per_model", "monthly"):
            raw = data.get(key)
            if isinstance(raw, dict):
                rule = getattr(out, key)
                for k, v in raw.items():
                    if hasattr(rule, k) and v is not None:
                        # max_usd 必须是数字：传成字符串会让预算护栏在比较时
                        # TypeError 崩掉，进而把所有写操作任务一起打挂
                        if k == "max_usd":
                            try:
                                v = float(v)
                            except (TypeError, ValueError) as exc:
                                raise ValueError(f"{key}.max_usd 不是数字：{v!r}") from exc
                            if v < 0:
                                raise ValueError(f"{key}.max_usd 不能为负：{v}")
                        setattr(rule, k, v)
                if rule.action not in BUDGET_ACTIONS:
                    rule.action = "warn"
        return out


class UsageStore:
    """用量与预算的持久化。线程安全（翻译流水线是并发的）。"""

    def __init__(self, path: str | Path, pricing_overrides: dict[str, dict[str, float]] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 与 agent.db 共用同一个打开方式：WebUI 常驻 + 命令行同时跑时，
        # 默认 5 秒等待会让第二个写者直接 `database is locked`。
        self.db = open_sqlite(self.path)
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
        self.pricing: dict[str, dict[str, float]] = {**DEFAULT_PRICING, **(pricing_overrides or {})}

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------ 写入
    def record(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        job_id: str = "",
        tool: str = "",
        latency_ms: int = 0,
        ok: bool = True,
    ) -> dict[str, Any]:
        key, price = match_pricing(model, self.pricing)
        cost = compute_cost(
            price,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        month = day[:7]
        with self._lock:
            self.db.execute(
                "INSERT INTO usage(ts, job_id, tool, model, prompt_tokens, completion_tokens,"
                " cached_tokens, cost_usd, latency_ms, ok, day)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now, job_id, tool, model, prompt_tokens, completion_tokens,
                 cached_tokens, cost, latency_ms, 1 if ok else 0, day),
            )
            self.db.commit()
        return {
            "model": model,
            "pricing_key": key,
            "cost_usd": cost,
            "day": day,
            "month": month,
            "tokens": prompt_tokens + completion_tokens,
        }

    # ------------------------------------------------------------------ 查询
    # where 子句全部来自这张白名单，绝不接受外部输入拼接
    _SUM_WHERE = {
        "job": "job_id=?",
        "today": "day=?",
        "month": "substr(day,1,7)=?",
        "model_today": "day=? AND model=?",
        "total": "1=1",
    }

    def _sum(self, where: str, params: Iterable[Any] = ()) -> dict[str, Any]:
        with self._lock:
            # where 只来自 _SUM_WHERE 白名单（结构固定），值一律走 ? 占位符
            sql = "SELECT COUNT(*) calls, COALESCE(SUM(prompt_tokens),0) pin,"                   " COALESCE(SUM(completion_tokens),0) pout,"                   " COALESCE(SUM(cached_tokens),0) pcache,"                   " COALESCE(SUM(cost_usd),0) cost FROM usage WHERE " + where  # noqa: S608
            row = self.db.execute(sql, tuple(params)).fetchone()
        return {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["pin"]),
            "completion_tokens": int(row["pout"]),
            "cached_tokens": int(row["pcache"]),
            "tokens": int(row["pin"]) + int(row["pout"]),
            "cost_usd": round(float(row["cost"]), 6),
        }

    def spent(self, scope: str, *, job_id: str = "", model: str = "") -> dict[str, Any]:
        """按维度取已花费。scope 只接受白名单值，未知值退化为全量。"""
        today = time.strftime("%Y-%m-%d")
        month = today[:7]
        where = self._SUM_WHERE.get(scope, self._SUM_WHERE["total"])
        params: tuple[Any, ...] = {
            "job": (job_id,),
            "today": (today,),
            "month": (month,),
            "model_today": (today, model),
        }.get(scope, ())
        return self._sum(where, params)

    def summary(self, *, days: int = 7) -> dict[str, Any]:
        today = time.strftime("%Y-%m-%d")
        month = today[:7]
        with self._lock:
            rows = self.db.execute(
                "SELECT day, COUNT(*) calls, SUM(prompt_tokens+completion_tokens) tokens,"
                " SUM(cost_usd) cost FROM usage GROUP BY day ORDER BY day DESC LIMIT ?",
                (days,),
            ).fetchall()
            by_model = self.db.execute(
                "SELECT model, COUNT(*) calls, SUM(prompt_tokens+completion_tokens) tokens,"
                " SUM(cost_usd) cost FROM usage GROUP BY model ORDER BY cost DESC LIMIT 20",
            ).fetchall()
            by_tool = self.db.execute(
                "SELECT tool, COUNT(*) calls, SUM(prompt_tokens+completion_tokens) tokens,"
                " SUM(cost_usd) cost FROM usage WHERE tool<>'' GROUP BY tool ORDER BY cost DESC",
            ).fetchall()
            by_job = self.db.execute(
                "SELECT job_id, tool, COUNT(*) calls, SUM(prompt_tokens+completion_tokens) tokens,"
                " SUM(cost_usd) cost, MAX(ts) ts FROM usage WHERE job_id<>''"
                " GROUP BY job_id ORDER BY ts DESC LIMIT 30",
            ).fetchall()
        return {
            "today": self._sum("day=?", (today,)),
            "month": self._sum("substr(day,1,7)=?", (month,)),
            "total": self._sum("1=1"),
            "daily": [
                {"day": r["day"], "calls": r["calls"], "tokens": r["tokens"] or 0,
                 "cost_usd": round(r["cost"] or 0.0, 6)}
                for r in rows
            ],
            "by_model": [
                {"model": r["model"], "calls": r["calls"], "tokens": r["tokens"] or 0,
                 "cost_usd": round(r["cost"] or 0.0, 6)}
                for r in by_model
            ],
            "by_tool": [
                {"tool": r["tool"], "calls": r["calls"], "tokens": r["tokens"] or 0,
                 "cost_usd": round(r["cost"] or 0.0, 6)}
                for r in by_tool
            ],
            "by_job": [
                {"job_id": r["job_id"], "tool": r["tool"], "calls": r["calls"],
                 "tokens": r["tokens"] or 0, "cost_usd": round(r["cost"] or 0.0, 6),
                 "ts": r["ts"]}
                for r in by_job
            ],
            "pricing": self.pricing,
        }

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def budget_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM budget_event ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def log_budget_event(self, scope: str, detail: str, spent: float, limit: float, action: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO budget_event(ts, scope, detail, spent_usd, limit_usd, action)"
                " VALUES(?,?,?,?,?,?)",
                (time.time(), scope, detail, spent, limit, action),
            )
            self.db.commit()

    # ------------------------------------------------------------------ 预算
    def check_budget(
        self, cfg: BudgetConfig, *, job_id: str = "", model: str = ""
    ) -> dict[str, Any]:
        """返回 {action, scope, reason, spent_usd, limit_usd}。

        action: ok | warn | stop | downgrade
        多个维度同时超限时取**最严重**的（stop > downgrade > warn）。
        """
        checks: list[tuple[BudgetRule, str, dict[str, Any]]] = [
            (cfg.per_job, "job", self.spent("job", job_id=job_id)),
            (cfg.daily, "today", self.spent("today")),
            (cfg.per_model, "model_today", self.spent("model_today", model=model)),
            (cfg.monthly, "month", self.spent("month")),
        ]
        severity = {"ok": 0, "warn": 1, "downgrade": 2, "stop": 3}
        worst = {"action": "ok", "scope": "", "reason": "", "spent_usd": 0.0, "limit_usd": 0.0}
        for rule, scope, spent in checks:
            if not rule.enabled:
                continue
            over_usd = rule.max_usd > 0 and spent["cost_usd"] >= rule.max_usd
            over_tok = rule.max_tokens > 0 and spent["tokens"] >= rule.max_tokens
            if not (over_usd or over_tok):
                continue
            action = rule.action
            reason = (
                f"{rule.name}预算超限：已花 ${spent['cost_usd']:.4f}"
                f"（上限 ${rule.max_usd:.2f}），{spent['tokens']} tokens"
                if over_usd
                else f"{rule.name}预算超限：已用 {spent['tokens']} tokens（上限 {rule.max_tokens}）"
            )
            if severity[action] > severity[worst["action"]]:
                worst = {
                    "action": action,
                    "scope": scope,
                    "reason": reason,
                    "spent_usd": spent["cost_usd"],
                    "limit_usd": rule.max_usd,
                    "fallback_model": rule.fallback_model,
                }
        return worst
