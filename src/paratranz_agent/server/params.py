"""请求参数的统一处理：数字转换（畸形入参变 400）与分页边界。

**为什么要数字转换**：FastAPI 只对**函数签名里声明了类型**的参数做校验
（路径/查询参数），而我们这些端点收的是 `body: dict`（前端历史上传的就是裸 JSON），
于是 `int(body.get("min_stage"))` 遇到 `"abc"` 会抛 `ValueError`，被全局异常
处理器接成 **500**。对前端来说 500 意味着「服务端坏了」，真实原因却是「你传了
非法值」—— 状态码骗人，排查方向也就跟着错。凡是把 body 里的值转成数字，都走这里。

**为什么要分页约束**：`limit` 如果是裸 `int`，三个实现的负数语义互不相同 ——
`list(self._order)[-limit:]` 在 `limit=0` 时返回全部、`limit=-1` 时把最新一条切掉；
SQLite 的 `LIMIT -1` 却是「不限制」。上层脚本拿到空列表会以为「没有任务」，
实际是参数写错了。把范围声明在签名上，FastAPI 直接返 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query


def as_int(value: Any, *, name: str, default: int = 0) -> int:
    """转 int。`None` / 空串取默认值；其余非法值一律 400。"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{name} 必须是整数，收到的是 {value!r}") from exc


def as_float(value: Any, *, name: str, default: float = 0.0) -> float:
    """转 float。`None` / 空串取默认值；其余非法值一律 400。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{name} 必须是数字，收到的是 {value!r}") from exc


# 一次列 500 条已远超界面显示能力，再大只会把响应体撑大；
# 上限写在这里，免得每个端点各写一个数字。
MAX_PAGE = 500

LIST_LIMIT = Query(50, ge=1, le=MAX_PAGE, description="返回条数（1~500）")
PAGE_LIMIT = Query(100, ge=1, le=MAX_PAGE, description="返回条数（1~500）")
LOG_LIMIT = Query(300, ge=1, le=MAX_PAGE * 4, description="返回条数（1~2000）")
