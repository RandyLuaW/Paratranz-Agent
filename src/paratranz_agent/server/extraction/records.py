"""提取记录的读取：列表、详情、词条分页、运行日志。

记录本身是磁盘上的一个目录（`state/extractions/<job_id>/`），没有数据库——
用户可以直接打开看、直接删。
"""
import heapq
import json
from pathlib import Path

from .errors import ExtractionError
from .jsonl import read_jsonl
from .paths import _job_root, check_id

def list_jobs(state_dir: Path, limit: int = 50) -> list[dict]:
    """最近的提取记录（新→旧）。

    原先 `sorted(root.iterdir())` 会把**所有** job 目录先排一遍再截断；
    目录名就是时间戳，取最大的 limit 个即可（多取一倍以容忍缺 meta.json 的目录）。
    """
    root = _job_root(state_dir)
    if not root.exists():
        return []
    candidates = heapq.nlargest(max(limit * 2, limit), (d for d in root.iterdir() if d.is_dir()))
    rows: list[dict] = []
    for d in candidates:
        mf = d / "meta.json"
        if not mf.exists():
            continue
        try:
            rows.append(json.loads(mf.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
        if len(rows) >= limit:
            break
    return rows


def get_job(state_dir: Path, job_id: str) -> dict:
    mf = _job_root(state_dir) / check_id(job_id, "提取记录 id") / "meta.json"
    if not mf.exists():
        raise ExtractionError(f"提取记录不存在：{job_id}")
    return json.loads(mf.read_text(encoding="utf-8"))


def load_entries(
    state_dir: Path,
    job_id: str,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """读某次提取的词条。分页时给 offset/limit，避免整份解析（见 `_read_jsonl`）。"""
    f = _job_root(state_dir) / check_id(job_id, "提取记录 id") / "entries.jsonl"
    if not f.exists():
        raise ExtractionError(f"该记录没有词条文件：{job_id}")
    entries, _ = read_jsonl(f, offset=offset, limit=limit)
    return entries


def job_log(state_dir: Path, job_id: str, tail: int = 200) -> str:
    f = _job_root(state_dir) / check_id(job_id, "提取记录 id") / "run.log"
    if not f.exists():
        return ""
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])
