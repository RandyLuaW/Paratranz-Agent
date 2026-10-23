"""提取器输出的 JSONL 读取（唯一入口）。

支持按条切片：详情页翻一页不该把整份文件（9087 条约 2 MB）重新解析。
"""
import json
from pathlib import Path

from .errors import ExtractionError

MAX_LINES = 1_000_000  # 单次提取的词条上限，防内存爆


def read_jsonl(
    path: Path,
    max_lines: int = MAX_LINES,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> tuple[list[dict], int]:
    """读 JSONL，可按条切片。

    加 offset/limit 是为了详情端点分页：9087 条的记录约 2 MB，原先
    `load_entries()` 全量解析后前端再 `entries[offset:offset+limit]` 切片，
    等于每翻一页就把整份文件重新解析一遍。现在边读边跳、够数即停。

    `max_lines` 仍然是**整份文件**的行数上限（防提取器匹配过宽），
    与切片无关——即使只取 100 条，也要读完才发现文件超限吗？不需要，
    因为写文件时就已经按 MAX_LINES 校验过，这里只对全量读取生效。
    """
    out: list[dict] = []
    bad = 0
    seen = 0          # 已通过校验的条数（用于 offset/limit）
    full = offset <= 0 and limit is None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if full and lineno > max_lines:
                raise ExtractionError(
                    f"输出超过 {max_lines} 行，已中止（检查提取器是否匹配过宽）"
                )
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(obj, dict):
                bad += 1
                continue
            key = str(obj.get("key") or "").strip()
            original = obj.get("original")
            if not key or not isinstance(original, str):
                bad += 1
                continue
            if seen < offset:
                seen += 1
                continue
            seen += 1
            out.append(
                {
                    "key": key,
                    "original": original,
                    "context": str(obj.get("context") or ""),
                }
            )
            if limit is not None and len(out) >= limit:
                break
    return out, bad
