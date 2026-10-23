"""跑提取器：起子进程、收输出、落 meta。

子进程的 cwd 会切到提取器目录，所以游戏目录必须 resolve 成绝对路径——
相对路径在那里会解析到别处。
"""
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guard import check_game_dir
from .jsonl import read_jsonl
from .paths import _job_root
from .registry import ExtractorMeta, get_extractor

@dataclass
class ExtractionResult:
    job_id: str
    extractor_id: str
    game_dir: str
    status: str          # done / failed
    entries: int = 0
    files_scanned: int = 0
    duration_ms: int = 0
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    # 本次实际传给提取器的参数。回写要靠它还原定位语义
    # （行号型 key 只有配上当时的 pattern/group 才知道该行哪一段是译文），
    # 所以必须落盘；老记录里没有这个字段时按空字典处理。
    options: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "extractor_id": self.extractor_id,
            "game_dir": self.game_dir,
            "status": self.status,
            "entries": self.entries,
            "files_scanned": self.files_scanned,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "warnings": self.warnings,
            "options": self.options,
        }


def run_extractor(
    state_dir: Path,
    eid: str,
    game_dir: str | Path,
    options: dict[str, Any] | None = None,
    *,
    timeout: int = 600,
) -> ExtractionResult:
    """子进程跑提取器。输出走文件，日志走 stderr。"""
    meta = get_extractor(state_dir, eid)
    # 必须 resolve：子进程的 cwd 会切到提取器目录，相对路径在那里会解析错
    gdir = check_game_dir(game_dir)

    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    jdir = (_job_root(state_dir) / job_id).resolve()
    jdir.mkdir(parents=True, exist_ok=True)
    out_file = jdir / "entries.jsonl"
    log_file = jdir / "run.log"

    entry_path = Path(meta.path) / meta.entry
    cmd: list[str] = [sys.executable, str(entry_path), "--input", str(gdir), "--output", str(out_file)]
    for k, v in (options or {}).items():
        if v is None or v == "":
            continue
        cmd.extend(["--opt", f"{k}={v}"])

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PTZ_INPUT": str(gdir),
        "PTZ_OUTPUT": str(out_file),
        "PTZ_OPTIONS": json.dumps(options or {}, ensure_ascii=False),
    }

    t0 = time.monotonic()
    res = ExtractionResult(
        job_id=job_id,
        extractor_id=eid,
        game_dir=str(gdir),
        status="failed",
        options={k: v for k, v in (options or {}).items() if v is not None and v != ""},
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path(meta.path)),
            capture_output=True,
            timeout=timeout,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        res.error = f"提取超时（{timeout}s）"
        log_file.write_text(res.error, encoding="utf-8")
        _write_meta(jdir, meta, res)
        return res
    except OSError as exc:
        res.error = f"无法启动提取器：{exc}"
        log_file.write_text(res.error, encoding="utf-8")
        _write_meta(jdir, meta, res)
        return res

    res.duration_ms = int((time.monotonic() - t0) * 1000)
    log = (proc.stdout or "") + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
    log_file.write_text(log, encoding="utf-8")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        res.error = f"提取器退出码 {proc.returncode}：\n" + "\n".join(tail)
        _write_meta(jdir, meta, res)
        return res

    if not out_file.exists():
        res.error = "提取器没有产出输出文件（检查 --output 参数是否被忽略）"
        _write_meta(jdir, meta, res)
        return res

    entries, bad = read_jsonl(out_file)
    res.entries = len(entries)
    res.status = "done"
    if bad:
        res.warnings.append(f"跳过 {bad} 行无法解析的输出")
    if not entries:
        res.warnings.append("没有提取到任何词条")
    _write_meta(jdir, meta, res)
    return res


def _write_meta(jdir: Path, meta: ExtractorMeta, res: ExtractionResult) -> None:
    (jdir / "meta.json").write_text(
        json.dumps(
            {"extractor": meta.as_dict(), **res.as_dict()}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
