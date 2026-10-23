"""游戏目录安全检查。

提取器会在本机执行代码，输入目录必须挡掉盘符根、系统目录与用户主目录根——
否则一次手滑就能把 `C:\Windows` 当游戏目录扫。
"""
import os
from pathlib import Path

from .errors import ExtractionError

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
