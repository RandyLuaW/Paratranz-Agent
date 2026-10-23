"""回写：把平台上确认过的译文写回游戏文件（「提取 → 导入」的反向链路）。

    from paratranz_agent.writeback import run_writeback, pull_translations

    rep = run_writeback(client=client, state_dir=state_dir, job_id="20261023-...",
                        min_stage=3, dry_run=True)
    print(rep.as_dict())

分层：
- `base`     —— 公共契约（`WriteItem` / `ApplyResult`）
- `locator`  —— 把 key 里的定位符解析成结构化路径（JSON 路径族 / 行号族）
- `json_path`—— JSON 手术式替换（保住原文件格式，逐字面量校验原文）
- `line`     —— 行号族替换（依赖提取器当时的 pattern/group）
- `apply`    —— 按定位符族分派到上面两个函数（唯一的写回入口）
- `pipeline` —— 拉平台译文、分组、校验、输出/原地写+备份、出报告
"""
from __future__ import annotations

from .apply import apply_items, family_of_key
from .base import ApplyResult, WriteItem, split_key
from .json_path import apply_json, json_escape, scan_strings
from .line import apply_line
from .locator import LocatorError, ParsedLocator, parse_locator, steps_to_locator
from .pipeline import (
    MIN_STAGE_ZH,
    WritebackError,
    WritebackReport,
    pull_translations,
    run_writeback,
)

__all__ = [
    "ApplyResult",
    "LocatorError",
    "MIN_STAGE_ZH",
    "ParsedLocator",
    "WriteItem",
    "WritebackError",
    "WritebackReport",
    "apply_items",
    "apply_json",
    "apply_line",
    "family_of_key",
    "json_escape",
    "parse_locator",
    "pull_translations",
    "run_writeback",
    "scan_strings",
    "split_key",
    "steps_to_locator",
]
