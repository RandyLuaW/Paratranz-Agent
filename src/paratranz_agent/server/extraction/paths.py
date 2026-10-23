"""状态目录布局与路径守卫。

`check_id` 是安全边界：id 会拼进文件路径（`state/extractors/<id>`、
`state/extractions/<id>`），不校验的话 `../../x` 能读任意 meta.json、能 rmtree 任意目录。
"""
import re
from pathlib import Path

from .errors import ExtractionError

EXTRACTOR_DIR = "extractors"
JOB_DIR = "extractions"
ENTRY_FILE = "extractor.yaml"


# id 会拼进文件路径（state/extractors/<id>、state/extractions/<id>），
# 必须白名单校验：否则 `../../x` 能读任意 meta.json、能 rmtree 任意目录。
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def check_id(value: str, what: str = "id") -> str:
    v = (value or "").strip()
    if not v or ".." in v or not _SAFE_ID.match(v):
        raise ExtractionError(
            f"非法{what}：{value!r}（只允许字母数字和 _ . -，且不能含 ..）"
        )
    return v


def _extractor_root(state_dir: Path) -> Path:
    p = state_dir / EXTRACTOR_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _builtin_root() -> Path:
    """包内自带提取器（随 pip 安装分发，只读）。

    `parents[2]` = `paratranz_agent/`：本文件是
    `paratranz_agent/server/extraction/paths.py`，比原先的
    `paratranz_agent/server/extraction.py` 深了一层——拆包时这里最容易漏改。
    """
    return Path(__file__).resolve().parents[2] / "extractors"


def _job_root(state_dir: Path) -> Path:
    p = state_dir / JOB_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p
