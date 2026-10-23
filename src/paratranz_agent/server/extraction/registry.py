"""提取器注册表：内置的三个 + 导入的 + Agent 生成的，统一按目录约定挂载。

一个提取器 = 一个目录（`extractor.yaml` + 入口脚本），见包级文档。
"""
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ExtractionError
from .paths import (
    ENTRY_FILE,
    _builtin_root,
    _extractor_root,
    check_id,
)

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
