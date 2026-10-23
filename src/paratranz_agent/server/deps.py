"""路由层的依赖容器。

`create_app` 里建好的单例（服务容器、配置存储、事件总线、任务队列、
工具注册表）通过这个 dataclass **显式**传给各路由模块，而不是让路由
自己去构造或从全局取——这样每个路由模块的依赖一眼可见，测试也能只
替换其中一项。

形态上刻意保持极简：只有字段，没有方法。它不承担任何业务判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .jobs import EventBus, JobRunner
from .settings import SettingsStore
from .tools import AppContext


@dataclass(slots=True)
class Deps:
    """一次 `create_app` 里的全部长期存活对象。"""

    ctx: AppContext
    store: SettingsStore
    bus: EventBus
    runner: JobRunner
    registry: dict[str, Callable[..., dict]]
    mock: bool
