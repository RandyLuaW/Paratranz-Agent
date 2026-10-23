"""提取类内容管理：导入提取脚本 → 跑游戏目录 → 归并校验 → 导入平台。

## 为什么单独一层

ParaTranz 只能管「已经进平台的词条」。而词条怎么从游戏文件里出来，
是每个引擎都不一样的手工活：RPG Maker 在 data/*.json，Ren'Py 在 .rpy 对话，
Unity 要拆 AssetBundle。这一层把这些差异收进「提取器」这个可插拔单元。

## 提取器协议

一个提取器 = 一个目录，含 `extractor.yaml` + 入口脚本：

    extractor.yaml
      id: rpgmaker-mv
      name: RPG Maker MV/MZ
      engine: rpgmaker
      entry: run.py
      args:
        - name: glob
          label: 文件匹配
          default: "data/*.json"

    运行时：
      python run.py --input <游戏目录> --output <输出.jsonl> [--opt k=v ...]
      环境变量 PTZ_INPUT / PTZ_OUTPUT / PTZ_OPTIONS(JSON) 同时可用

    输出协议（JSONL，每行一个词条）：
      {"key": "data/Actors.json::0.name", "original": "Harold", "context": "Actors.json"}
      key 必须全局唯一；original 必须非空字符串。

这样任何语言都能写提取器，只要按协议往输出文件里写 JSONL。
"""
from __future__ import annotations

import heapq
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

EXTRACTOR_DIR = "extractors"
JOB_DIR = "extractions"
ENTRY_FILE = "extractor.yaml"

# 校验阈值
MAX_ORIGINAL_LEN = 5000
MAX_ENTRIES_WARN = 200_000


class ExtractionError(Exception):
    """提取流程里的可读错误（直接展示给用户）。"""


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


# ------------------------------------------------------------------ 数据结构
@dataclass
class ExtractorMeta:
    id: str
    name: str
    engine: str = "custom"
    version: str = "0.0.0"
    desc: str = ""
    entry: str = "run.py"
    input_hint: str = ""
    args: list[dict[str, Any]] = field(default_factory=list)
    builtin: bool = False
    source: str = "imported"   # builtin / imported / agent
    path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "engine": self.engine,
            "version": self.version,
            "desc": self.desc,
            "entry": self.entry,
            "input_hint": self.input_hint,
            "args": self.args,
            "builtin": self.builtin,
            "source": self.source,
            "path": self.path,
        }


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


# ------------------------------------------------------------------ 提取器管理
def _extractor_root(state_dir: Path) -> Path:
    p = state_dir / EXTRACTOR_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_meta(path: Path) -> ExtractorMeta:
    yml = path / ENTRY_FILE
    if not yml.exists():
        raise ExtractionError(f"缺少 {ENTRY_FILE}：{path}")
    try:
        data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExtractionError(f"{ENTRY_FILE} 解析失败：{exc}") from exc
    if not isinstance(data, dict) or not data.get("id"):
        raise ExtractionError(f"{ENTRY_FILE} 必须包含 id 字段")
    entry = str(data.get("entry") or "run.py")
    if not (path / entry).exists():
        raise ExtractionError(f"入口脚本不存在：{entry}")
    return ExtractorMeta(
        id=check_id(str(data["id"]), "提取器 id"),
        name=str(data.get("name") or data["id"]),
        engine=str(data.get("engine") or "custom"),
        version=str(data.get("version") or "0.0.0"),
        desc=str(data.get("desc") or ""),
        entry=entry,
        input_hint=str(data.get("input_hint") or ""),
        args=list(data.get("args") or []),
        builtin=bool(data.get("builtin")),
        # .agent 标记文件区分「Agent 生成」与「用户导入」
        source=(
            "builtin" if data.get("builtin")
            else ("agent" if (path / ".agent").exists() else "imported")
        ),
        # 必须绝对：run_extractor 会用这个路径当 cwd 并拼入口脚本，
        # 相对路径会被子进程按自己的 cwd 再解析一次（拼成双份）
        path=str(path.resolve()),
    )


def _builtin_root() -> Path:
    """包内自带提取器（随 pip 安装分发，只读）。"""
    return Path(__file__).resolve().parent.parent / "extractors"


def list_extractors(state_dir: Path) -> list[ExtractorMeta]:
    """用户导入的优先，同 id 覆盖内置。"""
    out: list[ExtractorMeta] = []
    seen: set[str] = set()
    root = _extractor_root(state_dir)
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            m = _load_meta(d)
        except ExtractionError:
            continue  # 坏掉的提取器不阻塞列表
        out.append(m)
        seen.add(m.id)
    builtin = _builtin_root()
    if builtin.is_dir():
        for d in sorted(builtin.iterdir()):
            if not d.is_dir():
                continue
            try:
                m = _load_meta(d)
            except ExtractionError:
                continue
            if m.id not in seen:
                out.append(m)
    return out


def get_extractor(state_dir: Path, eid: str) -> ExtractorMeta:
    eid = check_id(eid, "提取器 id")
    for m in list_extractors(state_dir):
        if m.id == eid:
            return m
    raise ExtractionError(f"提取器不存在：{eid}")


def install_extractor(state_dir: Path, src: str | Path) -> ExtractorMeta:
    src = Path(src)
    if not src.is_dir():
        raise ExtractionError(f"目录不存在：{src}")
    meta = _load_meta(src)  # 先校验，再复制
    root = _extractor_root(state_dir)
    dst = root / check_id(meta.id, "提取器 id")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return _load_meta(dst)


def remove_extractor(state_dir: Path, eid: str) -> None:
    eid = check_id(eid, "提取器 id")
    d = _extractor_root(state_dir) / eid
    if not d.exists():
        if (_builtin_root() / eid).exists():
            raise ExtractionError(f"{eid} 是内置提取器，不能删除（可导入同 id 覆盖）")
        raise ExtractionError(f"提取器不存在：{eid}")
    shutil.rmtree(d)


# ------------------------------------------------------------------ 运行
# 拒绝把系统关键目录当成"游戏目录"来扫（提取器会递归遍历并读文件）
_DENY_ROOTS_WIN = (
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
)
_DENY_ROOTS_POSIX = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev", "/var")


def check_game_dir(path: str | Path) -> Path:
    """校验游戏目录：存在、是目录、不是盘符根/系统关键目录。"""
    raw = str(path or "").strip()
    if not raw:
        raise ExtractionError("游戏目录不能为空")
    p = Path(raw).expanduser()
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExtractionError(f"游戏目录不存在或无法访问：{raw}") from exc
    if not p.is_dir():
        raise ExtractionError(f"不是目录：{p}")
    if p.parent == p:  # 盘符根，如 C:\
        raise ExtractionError(f"拒绝扫描盘符根目录：{p}")

    if os.name == "nt":
        parts = [x.lower() for x in p.parts]
        if len(parts) >= 2 and parts[1] in _DENY_ROOTS_WIN:
            raise ExtractionError(f"拒绝扫描系统目录：{p}")
        home = Path.home().resolve()
        if p == home:
            raise ExtractionError(f"拒绝扫描用户主目录根：{p}")
    else:
        for d in _DENY_ROOTS_POSIX:
            if p == Path(d) or Path(d) in p.parents:
                raise ExtractionError(f"拒绝扫描系统目录：{p}")
    return p


def _job_root(state_dir: Path) -> Path:
    p = state_dir / JOB_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


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

    entries, bad = _read_jsonl(out_file)
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


MAX_LINES = 1_000_000  # 单次提取的词条上限，防内存爆


def _read_jsonl(
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


# ------------------------------------------------------------------ 历史
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
    entries, _ = _read_jsonl(f, offset=offset, limit=limit)
    return entries


def job_log(state_dir: Path, job_id: str, tail: int = 200) -> str:
    f = _job_root(state_dir) / check_id(job_id, "提取记录 id") / "run.log"
    if not f.exists():
        return ""
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


# ------------------------------------------------------------------ 归并 / 校验
PLACEHOLDER_RE = re.compile(r"\\[A-Za-z]+\[[^\]]*\]|\{[^}]*\}|%[sd]|<\w+>|\$\[[^\]]*\]")


def normalize(entries: Iterable[dict]) -> tuple[list[dict], dict]:
    """归并去重 + 规范化。返回 (结果, 统计)。"""
    entries = list(entries)  # 迭代器只能遍历一次，先固化
    seen: dict[str, dict] = {}
    dup_keys = 0
    dup_text = 0
    trimmed = 0
    by_original: dict[str, str] = {}

    for e in entries:
        key = str(e.get("key") or "").strip()
        original = e.get("original")
        if not isinstance(original, str):
            continue
        o2 = original.replace("\r\n", "\n").strip()
        if o2 != original:
            trimmed += 1
        if not o2:
            continue
        if key in seen:
            dup_keys += 1
            # 同 key 后到的覆盖（保留 context 更长的）
            if len(str(e.get("context") or "")) <= len(seen[key].get("context") or ""):
                continue
        # 同原文不同 key：保留第一个，其余记录（平台侧仍可各自存在）
        if o2 in by_original and by_original[o2] != key:
            dup_text += 1
        else:
            by_original[o2] = key
        seen[key] = {
            "key": key,
            "original": o2,
            "context": str(e.get("context") or "").strip(),
        }

    out = list(seen.values())
    stats = {
        "input": len(entries),
        "output": len(out),
        "dup_keys": dup_keys,
        "dup_text": dup_text,
        "trimmed": trimmed,
    }
    return out, stats


def validate(entries: Sequence[dict]) -> dict:
    """校验提取结果。返回 {ok, errors[], warnings[], stats}。"""
    errors: list[dict] = []
    warnings: list[dict] = []
    seen: dict[str, int] = {}
    empty = 0
    too_long = 0
    no_context = 0
    placeholder_only = 0
    key_bad = 0

    for i, e in enumerate(entries):
        key = str(e.get("key") or "")
        original = e.get("original")
        if not key:
            errors.append({"row": i, "rule": "empty_key", "msg": "key 为空"})
            continue
        if not isinstance(original, str):
            errors.append({"row": i, "rule": "not_string", "msg": "original 不是字符串", "key": key})
            continue
        if not original.strip():
            empty += 1
            errors.append({"row": i, "rule": "empty_original", "msg": "原文为空", "key": key})
            continue
        if len(original) > MAX_ORIGINAL_LEN:
            too_long += 1
            warnings.append(
                {"row": i, "rule": "too_long", "msg": f"原文超过 {MAX_ORIGINAL_LEN} 字符", "key": key}
            )
        if key in seen:
            errors.append(
                {"row": i, "rule": "dup_key", "msg": f"key 重复（首见第 {seen[key]} 行）", "key": key}
            )
        else:
            seen[key] = i
        if not e.get("context"):
            no_context += 1
        # 纯占位符/纯符号：没有可翻译内容
        stripped = PLACEHOLDER_RE.sub("", original).strip()
        if not stripped:
            placeholder_only += 1
            warnings.append(
                {"row": i, "rule": "placeholder_only", "msg": "去掉占位符后没有可译内容", "key": key}
            )
        if "::" not in key and "/" not in key:
            key_bad += 1

    if key_bad:
        warnings.append(
            {
                "row": -1,
                "rule": "key_format",
                "msg": f"{key_bad} 个 key 既不含 '::' 也不含 '/'，建议用 `文件::路径` 形式便于回溯",
            }
        )
    if no_context:
        warnings.append(
            {"row": -1, "rule": "no_context", "msg": f"{no_context} 条没有 context，平台里不好定位"}
        )
    if len(entries) > MAX_ENTRIES_WARN:
        warnings.append(
            {"row": -1, "rule": "too_many", "msg": f"词条数 {len(entries)} 超过 {MAX_ENTRIES_WARN}，建议分文件"}
        )

    return {
        "ok": not errors,
        "errors": errors[:200],
        "warnings": warnings[:200],
        "stats": {
            "total": len(entries),
            "unique_keys": len(seen),
            "empty_original": empty,
            "too_long": too_long,
            "no_context": no_context,
            "placeholder_only": placeholder_only,
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
    }


def export_paratranz(entries: Sequence[dict], *, translation: str = "") -> list[dict]:
    """转成 ParaTranz 的 items 格式（update_file_translation 直接吃）。"""
    return [
        {
            "key": e["key"],
            "original": e["original"],
            "translation": translation,
            "context": e.get("context", ""),
        }
        for e in entries
    ]


def sample_files(
    game_dir: str | Path,
    *,
    max_files: int = 6,
    max_chars: int = 1200,
    max_scan: int = 3000,
) -> list[dict[str, Any]]:
    """挑几个代表性文件的内容片段，供模型理解数据格式。

    按扩展名分组，每组取第一个，文件数多的扩展名优先。
    """
    g = check_game_dir(game_dir)
    by_ext: dict[str, list[Path]] = {}
    scanned = 0
    for p in g.rglob("*"):
        scanned += 1
        if scanned > max_scan:
            break
        if not p.is_file():
            continue
        if any(part in (".git", "node_modules", "__pycache__", ".venv", ".idea")
               for part in p.parts):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0 or size > 8_000_000:
            continue
        by_ext.setdefault(p.suffix.lower() or "(noext)", []).append(p)

    picked: list[dict[str, Any]] = []
    for ext, files in sorted(by_ext.items(), key=lambda kv: -len(kv[1]))[:max_files]:
        f = files[0]
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:max_chars]
            size = f.stat().st_size
        except OSError:
            continue
        picked.append({
            "ext": ext,
            "path": str(f.relative_to(g)).replace(os.sep, "/"),
            "size": size,
            "preview": text,
        })
    return picked


def analyze_game_dir(game_dir: str | Path, *, max_files: int = 4000) -> dict:
    """粗扫游戏目录，给出引擎猜测与文件画像（供 Agent 写提取脚本参考）。"""
    g = check_game_dir(game_dir)
    ext_count: dict[str, int] = {}
    markers: list[str] = []
    total = 0
    sample: list[str] = []
    for root, dirs, files in os.walk(g):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for f in files:
            total += 1
            if total > max_files:
                break
            p = Path(root) / f
            ext = p.suffix.lower() or "(noext)"
            ext_count[ext] = ext_count.get(ext, 0) + 1
            rel = str(p.relative_to(g)).replace("\\", "/")
            if len(sample) < 30:
                sample.append(rel)
            low = rel.lower()
            for m in ("www/data/", "game.rpy", "renpy/", "assets/bin/data", "data/", "pak", "locale/"):
                if m in low and m not in markers:
                    markers.append(m)
        if total > max_files:
            break

    guess = "unknown"
    if "www/data/" in markers or any(k.startswith("data/") for k in sample):
        guess = "rpgmaker-mv/mz"
    if "game.rpy" in markers or "renpy/" in markers:
        guess = "renpy"
    if "assets/bin/data" in markers:
        guess = "unity"
    if "pak" in markers:
        guess = "unreal/other-pak"

    return {
        "game_dir": str(g),
        "files": total,
        "truncated": total > max_files,
        "by_ext": dict(sorted(ext_count.items(), key=lambda kv: -kv[1])[:15]),
        "markers": markers,
        "engine_guess": guess,
        "sample": sample,
    }
