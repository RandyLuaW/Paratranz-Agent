"""pipelines 包：Agent 的五条业务流水线。

顺序即生产顺序：
    plan → translate → review → glossary → styleguide

每条 pipeline 都遵循同一契约：
- 只通过 `ParaTranzClient` 与平台交互（不直连网页）；
- 所有写操作先在 `Store.ledger` 记账，重跑幂等；
- 支持 `dry_run`，打印将要发生的写操作而不落盘。
"""
from .plan import PlanPipeline, PlannedTask
from .translate import TranslatePipeline, TranslateStats
from .review import ReviewPipeline, ReviewStats
from .glossary import GlossaryPipeline, TermCandidate
from .styleguide import StyleGuidePipeline

__all__ = [
    "PlanPipeline",
    "PlannedTask",
    "TranslatePipeline",
    "TranslateStats",
    "ReviewPipeline",
    "ReviewStats",
    "GlossaryPipeline",
    "TermCandidate",
    "StyleGuidePipeline",
]
