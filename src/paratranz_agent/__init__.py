"""paratranz-agent：让 Agent 接管 ParaTranz 汉化项目的管理、翻译、校对、术语与规范。

模块边界（从下到上）：
    models     平台数据模型（对齐 OpenAPI v0.5.7）
    client     唯一的平台出口，负责限流/重试/分页
    store      本地状态：翻译记忆 TM / 术语缓存 / 幂等账本
    qa         确定性校对规则（模型二审之前的硬拦）
    llm        OpenAI 兼容通道；无 key 时 UnavailableLLM（调用即报错）
    pipelines  plan / translate / review / glossary / styleguide
    cli        ptz 命令组
"""
from __future__ import annotations

__version__ = "0.3.2"

from .config import AgentConfig, load_config
from .models import Stage, StringItem, Task, Term

__all__ = ["AgentConfig", "load_config", "Stage", "StringItem", "Task", "Term", "__version__"]
