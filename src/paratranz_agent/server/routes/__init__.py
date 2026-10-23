"""路由分包：按前缀分组，`app.py` 只负责组装。"""

from __future__ import annotations

from fastapi import FastAPI

from ..deps import Deps
from . import (
    agent,
    extensions,
    extract,
    health,
    jobs,
    models,
    project,
    settings,
    usage,
    writeback,
)


def register_all(app: FastAPI, d: Deps) -> None:
    """按固定顺序注册全部路由。"""
    health.register(app, d)
    settings.register(app, d)
    project.register(app, d)
    jobs.register(app, d)
    agent.register(app, d)
    usage.register(app, d)
    extensions.register(app, d)
    models.register(app, d)
    extract.register(app, d)
    writeback.register(app, d)
