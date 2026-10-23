"""扩展：插件 / SKILL / MCP 服务器的注册与管理。

这是**位置预留**：目录约定、元数据格式、启停状态、导入与移除都已就位，
WebUI 能看能管；插件代码的实际执行引擎留待后续接入（`runtime` 字段标出）。

目录约定（都在 `.ptz-state/` 下，可被 `.gitignore` 排除）：

    .ptz-state/plugins/<plugin_id>/metadata.yaml   插件（metadata 声明元信息）
    .ptz-state/skills/<skill_id>/SKILL.md          SKILL（frontmatter: name/description）
    .ptz-state/mcp.json                            MCP 服务器配置

插件 metadata.yaml 字段：
    name / desc / short_desc / version / author / repo / display_name / pages[]
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # PyYAML 已是依赖，这里只是防御
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _parse_yaml(text: str) -> dict[str, Any]:
    if yaml is None:
        return {}
    try:
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """SKILL.md 的 `--- ... ---` 头。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    return _parse_yaml(m.group(1)), m.group(2)


@dataclass
class Extension:
    kind: str            # plugin | skill | mcp
    id: str
    name: str = ""
    desc: str = ""
    version: str = ""
    author: str = ""
    enabled: bool = True
    path: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    runtime: str = "pending"   # pending（预留）| ready
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name or self.id,
            "desc": self.desc,
            "version": self.version,
            "author": self.author,
            "enabled": self.enabled,
            "path": self.path,
            "meta": self.meta,
            "runtime": self.runtime,
            "note": self.note,
        }


class ExtensionStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.plugins_dir = self.root / "plugins"
        self.skills_dir = self.root / "skills"
        self.mcp_file = self.root / "mcp.json"
        for d in (self.plugins_dir, self.skills_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.mcp_file.exists():
            self._write_mcp({"servers": {}})

    # ------------------------------------------------------------------ MCP
    def _read_mcp(self) -> dict[str, Any]:
        try:
            data = json.loads(self.mcp_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"servers": {}}
        except (json.JSONDecodeError, OSError):
            return {"servers": {}}

    def _write_mcp(self, data: dict[str, Any]) -> None:
        self.mcp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def mcp_servers(self) -> list[Extension]:
        out = []
        for name, cfg in (self._read_mcp().get("servers") or {}).items():
            if not isinstance(cfg, dict):
                continue
            out.append(
                Extension(
                    kind="mcp",
                    id=name,
                    name=name,
                    desc=cfg.get("description", "") or f"{cfg.get('command', '')} {' '.join(cfg.get('args') or [])}".strip(),
                    enabled=bool(cfg.get("enabled", True)),
                    meta=cfg,
                    runtime="pending" if not cfg.get("enabled") else "ready",
                    note="" if cfg.get("enabled") else "已停用",
                )
            )
        return out

    def upsert_mcp(self, name: str, cfg: dict[str, Any]) -> None:
        data = self._read_mcp()
        servers = data.setdefault("servers", {})
        servers[name] = {**servers.get(name, {}), **cfg}
        self._write_mcp(data)

    def remove_mcp(self, name: str) -> bool:
        data = self._read_mcp()
        servers = data.get("servers") or {}
        if name not in servers:
            return False
        servers.pop(name)
        self._write_mcp(data)
        return True

    # -------------------------------------------------------------- 插件
    def plugins(self) -> list[Extension]:
        out = []
        for d in sorted(self.plugins_dir.iterdir()) if self.plugins_dir.exists() else []:
            if not d.is_dir():
                continue
            meta_file = d / "metadata.yaml"
            meta: dict[str, Any] = {}
            if meta_file.exists():
                meta = _parse_yaml(meta_file.read_text(encoding="utf-8", errors="replace"))
            disabled = (d / ".disabled").exists()
            out.append(
                Extension(
                    kind="plugin",
                    id=d.name,
                    name=str(meta.get("display_name") or meta.get("name") or d.name),
                    desc=str(meta.get("desc") or meta.get("short_desc") or ""),
                    version=str(meta.get("version") or ""),
                    author=str(meta.get("author") or ""),
                    enabled=not disabled,
                    path=str(d),
                    meta=meta,
                    runtime="pending",
                    note="插件运行引擎待接入：当前仅登记与启停状态",
                )
            )
        return out

    def install_plugin(self, src: str | Path) -> Extension:
        """从本地目录导入插件（要求有 metadata.yaml 或 main.py）。"""
        src = Path(src)
        if not src.is_dir():
            raise ValueError(f"不是目录：{src}")
        if not (src / "metadata.yaml").exists() and not (src / "main.py").exists():
            raise ValueError("目录里既没有 metadata.yaml 也没有 main.py，不像是插件目录")
        dest = self.plugins_dir / src.name
        if dest.exists():
            raise ValueError(f"插件已存在：{src.name}")
        shutil.copytree(src, dest)
        return next(
            (p for p in self.plugins() if p.id == src.name), None
        ) or Extension(kind="plugin", id=src.name)

    def remove_plugin(self, plugin_id: str) -> bool:
        d = self.plugins_dir / Path(plugin_id).name
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        return True

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> bool:
        d = self.plugins_dir / Path(plugin_id).name
        if not d.is_dir():
            return False
        flag = d / ".disabled"
        if enabled and flag.exists():
            flag.unlink()
        elif not enabled and not flag.exists():
            flag.write_text(str(time.time()), encoding="utf-8")
        return True

    # -------------------------------------------------------------- SKILL
    def skills(self) -> list[Extension]:
        out = []
        for d in sorted(self.skills_dir.iterdir()) if self.skills_dir.exists() else []:
            if not d.is_dir():
                continue
            md = d / "SKILL.md"
            if not md.exists():
                continue
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            out.append(
                Extension(
                    kind="skill",
                    id=d.name,
                    name=str(meta.get("name") or d.name),
                    desc=str(meta.get("description")
                             or (body.strip().splitlines()[0] if body.strip() else "")),
                    version=str(meta.get("version") or ""),
                    author=str(meta.get("author") or ""),
                    enabled=not (d / ".disabled").exists(),
                    path=str(d),
                    meta=meta,
                    runtime="pending",
                    note="SKILL 已登记；供 Agent 提示词装载，装载器待接入",
                )
            )
        return out

    def install_skill(self, src: str | Path) -> Extension:
        src = Path(src)
        if not (src / "SKILL.md").exists():
            raise ValueError("目录里没有 SKILL.md")
        dest = self.skills_dir / src.name
        if dest.exists():
            raise ValueError(f"SKILL 已存在：{src.name}")
        shutil.copytree(src, dest)
        return next((s for s in self.skills() if s.id == src.name),
                    Extension(kind="skill", id=src.name))

    def remove_skill(self, skill_id: str) -> bool:
        d = self.skills_dir / Path(skill_id).name
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        return True

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> bool:
        d = self.skills_dir / Path(skill_id).name
        if not d.is_dir():
            return False
        flag = d / ".disabled"
        if enabled and flag.exists():
            flag.unlink()
        elif not enabled and not flag.exists():
            flag.write_text(str(time.time()), encoding="utf-8")
        return True

    # ------------------------------------------------------------------ 汇总
    def all(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "plugins": [p.as_dict() for p in self.plugins()],
            "skills": [s.as_dict() for s in self.skills()],
            "mcp": [m.as_dict() for m in self.mcp_servers()],
        }
