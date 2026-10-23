"""回写报告的落盘与读取。

WebUI 里「最近一次回写结果」要能在刷新页面后还看得到，所以报告单独存一份，
不跟 job 日志混（job 只在内存里，进程重启就没了）。

位置：`<state_dir>/writeback/<提取记录 id>/last.json`
与备份目录同根，但**不在游戏目录里**——放进游戏目录会被下次提取的
`**/*.json` 通配再扫一遍，污染词条。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _safe(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name))
    return out[:80] or "unknown"


def _run_root(state_dir: Path, extraction_id: str) -> Path:
    p = Path(state_dir) / "writeback" / _safe(extraction_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_last(state_dir: Path, extraction_id: str, report: dict[str, Any]) -> Path:
    """保存最近一次回写报告（附带落盘时间，便于界面显示"多久之前"）。"""
    payload = {"saved_at": time.time(), "report": report}
    path = _run_root(state_dir, extraction_id) / "last.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_last(state_dir: Path, extraction_id: str) -> dict[str, Any] | None:
    """读最近一次报告；没有或文件坏了都返回 None（界面按"还没回写过"处理）。"""
    path = Path(state_dir) / "writeback" / _safe(extraction_id) / "last.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "report" not in data:
        return None
    return data
