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

# 传给提取器子进程的环境变量**白名单**。
#
# 为什么从黑名单改成白名单：黑名单漏一个变量就漏一个凭据，而提取器是
# 本机执行的任意代码 —— 它 `print(os.environ)` 就能把凭据写进 run.log，
# 而 run.log 会通过 `/api/extractions/{jid}` 明文回显给前端，整条路绕开日志脱敏。
# 白名单只留"没有它 Python 起不来/PATH 找不到命令"的那些。
_ENV_ALLOW = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
        "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONPATH", "PYTHONHOME",
        # 这三个是我们自己注入给提取器的参数（不含凭据），放在白名单里
        # 才能让"按名字判断是否凭据"的旧调用返回一致结果。
        "PTZ_INPUT", "PTZ_OUTPUT", "PTZ_OPTIONS",
    }
)

# 输出体积上限：提取器写错循环就可能把磁盘写满。超限按失败处理并删掉该文件。
MAX_OUTPUT_MB = 256


def _safe_env(extra: dict[str, str]) -> dict[str, str]:
    """只剩白名单里的环境变量（`LC_*` 这类本地化变量一并放行）。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in _ENV_ALLOW or k.upper().startswith("LC_")
    }
    env.update(extra)
    return env


def _is_secret_env(name: str) -> bool:
    """兼容旧调用：现在靠白名单兜底，这里只用来判断"这是不是个凭据变量名"。"""
    upper = name.upper()
    return not (upper in _ENV_ALLOW or upper.startswith("LC_"))


# 日志文件上限：提取器狂刷日志时截到尾部，避免把磁盘写满
MAX_LOG_MB = 64


def _tail(path: Path, max_chars: int = 4000) -> str:
    """读日志尾部若干字符（超大文件也不整个读进内存）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_chars * 4:
                f.seek(-max_chars * 4, os.SEEK_END)
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")[-max_chars:]


def _cap_log(path: Path) -> None:
    """把超过 `MAX_LOG_MB` 的日志截成"一行提示 + 末尾 1MB"。"""
    try:
        if path.stat().st_size <= MAX_LOG_MB * 1024 * 1024:
            return
        tail = _tail(path, max_chars=1024 * 1024)
        path.write_text(
            f"[日志超过 {MAX_LOG_MB} MB，已截断，只保留末尾部分]\n" + tail, encoding="utf-8"
        )
    except OSError:
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时后连子进程一起杀：只 kill 直接子进程会留下孙进程继续写文件。"""
    try:
        if os.name == "nt":
            subprocess.run(  # noqa: S603 - 参数固定，pid 来自本进程创建的子进程
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:  # noqa: BLE001 - 杀不掉就退回单进程 kill，不因此中断主流程
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


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

    env = _safe_env(
        {
            "PYTHONIOENCODING": "utf-8",
            "PTZ_INPUT": str(gdir),
            "PTZ_OUTPUT": str(out_file),
            "PTZ_OPTIONS": json.dumps(options or {}, ensure_ascii=False),
        }
    )

    t0 = time.monotonic()
    res = ExtractionResult(
        job_id=job_id,
        extractor_id=eid,
        game_dir=str(gdir),
        status="failed",
        options={k: v for k, v in (options or {}).items() if v is not None and v != ""},
    )
    # 子进程放进**独立进程组**：超时要连它拉起的孙进程一起杀，
    # 否则 `proc.kill()` 之后孙进程还在继续写文件。
    popen_kw: dict[str, Any] = {}
    if os.name == "nt":
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kw["start_new_session"] = True

    # 输出**直接落盘**而不是 PIPE：提取器如果往 stdout/stderr 狂刷（死循环 print、
    # 把整个文件 dump 出来），communicate() 会把它们全读进内存 —— 几秒钟就能把
    # 主进程 OOM 掉，而磁盘只是多一个日志文件。日志最终仍会截到 MAX_LOG_MB。
    try:
        with log_file.open("wb") as logfh:
            try:
                proc = subprocess.Popen(  # noqa: S603 - cmd 由本模块构造，参数来自提取器元信息
                    cmd,
                    cwd=str(Path(meta.path)),
                    stdout=logfh,
                    stderr=subprocess.STDOUT,
                    env=env,
                    **popen_kw,
                )
            except OSError as exc:
                res.error = f"无法启动提取器：{exc}"
                _write_meta(jdir, meta, res)
                return res

            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - 杀不掉
                    pass
    finally:
        _cap_log(log_file)

    res.duration_ms = int((time.monotonic() - t0) * 1000)
    log_text = _tail(log_file)

    if timed_out:
        res.error = f"提取超时（{timeout}s），已强制结束整个进程树"
        try:
            with log_file.open("a", encoding="utf-8") as f:
                f.write("\n" + res.error + "\n")
        except OSError:
            pass
        _write_meta(jdir, meta, res)
        return res

    if proc.returncode != 0:
        tail = "\n".join(log_text.strip().splitlines()[-8:])
        res.error = f"提取器退出码 {proc.returncode}：\n" + tail
        _write_meta(jdir, meta, res)
        return res

    if not out_file.exists():
        res.error = "提取器没有产出输出文件（检查 --output 参数是否被忽略）"
        _write_meta(jdir, meta, res)
        return res

    # 输出体积上限：写错循环会把磁盘写满，而且卡在这里时用户只看到"没反应"。
    size = out_file.stat().st_size
    if size > MAX_OUTPUT_MB * 1024 * 1024:
        out_file.unlink(missing_ok=True)
        res.error = (
            f"提取器输出 {size / 1024 / 1024:.0f} MB，超过上限 {MAX_OUTPUT_MB} MB，"
            "已丢弃（多半是匹配规则写成了死循环或把整个文件当一条词条）"
        )
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
