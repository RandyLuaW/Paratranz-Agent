"""请求体里的数字参数转换：畸形入参统一变 400，而不是 500。

FastAPI 只对**函数签名里声明了类型**的参数做校验（路径/查询参数），
而我们这些端点收的是 `body: dict`（前端历史上传的就是裸 JSON），
于是 `int(body.get("min_stage"))` 遇到 `"abc"` 会抛 `ValueError`，
被全局异常处理器接成 **500**。对前端来说 500 意味着「服务端坏了」，
而真实原因是「你传了非法值」—— 状态码骗人，排查方向也就跟着错。

所以凡是把 body 里的值转成数字，都走这里：非法就给 400 + 说清哪个字段。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


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
