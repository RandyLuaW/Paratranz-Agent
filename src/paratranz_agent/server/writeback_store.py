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

# 纯点号是**目录穿越**的标准写法：`_safe("..")` 如果原样返回，`... / ".." / "last.json"`
# 就落到了 state 根目录。写侧目前进不来（`run_writeback` 会先过 `check_id`），
# 但读侧是对着 URL 的，早晚会有人拿它当入口——在函数里一次性堵死更省事。
_DOTS = {"", ".", ".."}


def _safe(name: str) -> str:
    """把任意字符串压成一个安全的单段目录名。

    只保留字母数字与 `-_`；点号虽然对普通文件名无害，但 `.`/`..` 两段是
    路径语义，所以纯点号一律换成 `unknown`。
    """
    raw = str(name)
    if raw.strip() in _DOTS:
        return "unknown"
    out = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)
    return out[:80] or "unknown"


def _check_id(extraction_id: str) -> str:
    """与 `extraction.check_id` 同一套规则：拒空、拒 `..`、拒路径分隔符。"""
    from .extraction import check_id

    return check_id(str(extraction_id), "提取记录 id")


def _run_root(state_dir: Path, extraction_id: str) -> Path:
    p = Path(state_dir) / "writeback" / _safe(extraction_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_last(state_dir: Path, extraction_id: str, report: dict[str, Any]) -> Path:
    """保存最近一次回写报告（附带落盘时间，便于界面显示"多久之前"）。"""
    _check_id(extraction_id)
    payload = {"saved_at": time.time(), "report": report}
    path = _run_root(state_dir, extraction_id) / "last.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_last(state_dir: Path, extraction_id: str) -> dict[str, Any] | None:
    """读最近一次报告；没有或文件坏了都返回 None（界面按"还没回写过"处理）。

    非法 id 直接返回 None 而不是抛异常：这是只读查询，界面刷新时不该因为
    一个畸形 URL 就 500（但也不会去读任何 state 目录之外的文件）。
    """
    try:
        _check_id(extraction_id)
    except Exception:  # noqa: BLE001 - 非法 id 等价于"没有这条记录"
        return None
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
