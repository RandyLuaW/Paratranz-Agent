"""游戏目录画像与文件采样：给「让 Agent 写提取器」提供上下文。
"""
import os
import re
from pathlib import Path
from typing import Any

from .guard import check_game_dir

# 采样内容会被拼进 prompt 发给第三方模型，所以这些文件一律不采：
# 它们要么是凭据、要么是运行数据，和「游戏文本长什么样」毫无关系，
# 但一旦被采样就等于把密钥/账号信息发给模型供应商。
# 用户点 accept_risk 时理解的风险是「在本机执行代码」，不是「把凭据外发」。
_SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", "settings.json", "credentials.json",
    "secrets.json", "id_rsa", "id_ed25519", ".npmrc", ".netrc", ".pgpass",
}
_SENSITIVE_SUFFIX = {
    ".db", ".sqlite", ".sqlite3", ".db3", ".log", ".key", ".pem", ".p12", ".pfx",
    ".kdbx", ".crt", ".jks", ".keystore",
}

# 采样前缀里明显是密钥的串（与 redact 的格式规则同源，这里只做行内抹除，
# 因为采样是「原始文本片段」，不走日志通道）
_SECRET_INLINE = re.compile(
    r"\b(?:sk|rv|pk|ak)[-_][A-Za-z0-9_\-]{12,}"          # sk-/rv_ 等前缀
    r"|(?i:\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\"?)[A-Za-z0-9._\-]{8,}"
    r"|(?i:\bbearer\s+)[A-Za-z0-9._\-]{12,}",
)


def _is_sensitive(p: Path) -> bool:
    name = p.name.lower()
    if name in _SENSITIVE_NAMES or name.startswith(".env"):
        return True
    return p.suffix.lower() in _SENSITIVE_SUFFIX


def _scrub(text: str) -> str:
    """把预览里的密钥形态串抹掉，再交给模型。"""
    return _SECRET_INLINE.sub("***REDACTED***", text)


def sample_files(
    game_dir: str | Path,
    *,
    max_files: int = 6,
    max_chars: int = 1200,
    max_scan: int = 3000,
) -> list[dict[str, Any]]:
    """挑几个代表性文件的内容片段，供模型理解数据格式。

    按扩展名分组，每组取第一个，文件数多的扩展名优先。
    **凭据与运行数据类文件不采**（见 `_SENSITIVE_*`），预览内容统一过 `_scrub`。
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
        if _is_sensitive(p):
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
            text = _scrub(f.read_text(encoding="utf-8", errors="replace")[:max_chars])
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
