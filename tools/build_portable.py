"""构建 Windows 免安装包（解压即用，自带 Python 运行时）。

用法：
    python tools/build_portable.py                # 用 pyproject 里的版本号
    python tools/build_portable.py --python 3.13.7

产物（都在 dist/ 下）：
    paratranz-agent-<ver>-win64.zip
    paratranz-agent-<ver>-win64.zip.sha256

做法与踩过的坑：
  · 用 python.org 的 **embeddable** 包当运行时——`python -m venv` 出来的环境
    `pyvenv.cfg` 指回本机 Python，换机器就废，不能当"解压即用"。
  · embeddable 默认 `python313._pth` 关掉了 site 且不含 Lib\\site-packages，
    必须改写它，否则第三方包 import 不到。
  · `pip install -e .` 会在 site-packages 留 `*editable*.pth` 指回源码目录，
    本机能跑、别人机器直接崩——必须删掉，改成复制真实包目录。
  · 启动器 .bat **全文只能 ASCII**：cmd 会按当前代码页截断中文，
    连注释尾巴都可能被当命令执行。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SITE = ROOT / ".venv" / "Lib" / "site-packages"

# 打包进 zip 的顶层文件（python/ 目录另外处理）
LAUNCHERS = {
    "ptz.bat": (
        "@echo off\n"
        "setlocal\n"
        'set "PYTHONIOENCODING=utf-8"\n'
        'set "PYTHONUTF8=1"\n'
        '"%~dp0python\\python.exe" -m paratranz_agent.cli %*\n'
    ),
    "web.bat": (
        "@echo off\n"
        "setlocal\n"
        'set "PYTHONIOENCODING=utf-8"\n'
        'set "PYTHONUTF8=1"\n'
        '"%~dp0python\\python.exe" -m paratranz_agent.cli serve %*\n'
    ),
}


def force_rmtree(path: Path) -> None:
    """删目录，遇到只读文件先去掉只读位再删。

    Windows 上 `shutil.rmtree` 碰到 ReadOnly 文件直接 WinError 5——
    而从附件/压缩包复制进来的图常常带着只读属性（本项目 LOGO 就是）。
    """

    def on_error(func, p, _exc):
        try:
            Path(p).chmod(0o666)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:  # pragma: no cover - 兼容旧解释器
        shutil.rmtree(path, onerror=lambda f, p, e: on_error(f, p, e))


def clear_readonly(root: Path) -> None:
    """把树里所有文件的可写位加回来，避免后续 rmtree/覆盖失败。"""
    for p in root.rglob("*"):
        if p.is_file():
            try:
                p.chmod(0o666)
            except OSError:
                pass


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.M)
    if not m:
        raise SystemExit("pyproject.toml 里找不到 version")
    return m.group(1)


def fetch_embed(py_ver: str) -> Path:
    """下载 embeddable 包（有缓存就不重复下）。"""
    cache = DIST / f"python-{py_ver}-embed-amd64.zip"
    if cache.exists() and cache.stat().st_size > 1_000_000:
        print(f"  复用缓存 {cache.name}")
        return cache
    url = f"https://www.python.org/ftp/python/{py_ver}/python-{py_ver}-embed-amd64.zip"
    print(f"  下载 {url}")
    DIST.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=180) as r, cache.open("wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f)
    return cache


def patch_pth(py_dir: Path, py_ver: str) -> None:
    """embeddable 默认禁用 site，且不含 site-packages。"""
    tag = py_ver.split(".")[0] + py_ver.split(".")[1]  # 3.13.7 -> 313
    pth = py_dir / f"python{tag}._pth"
    if not pth.exists():
        raise SystemExit(f"找不到 {pth.name}，embeddable 结构可能变了")
    pth.write_text(
        f"python{tag}.zip\n.\nLib\\site-packages\n\nimport site\n", encoding="utf-8"
    )


def copy_deps(py_dir: Path) -> None:
    """复制依赖；剔除 editable 残留（它指回本机源码，换机器必崩）。"""
    dst = py_dir / "Lib" / "site-packages"
    if dst.exists():
        force_rmtree(dst)
    shutil.copytree(
        SITE, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.dist-info")
    )
    # 注意去重：`_editable_impl_paratranz_agent.pth` 同时命中 *editable* 和 *paratranz*
    junk = {p for pat in ("*editable*", "*paratranz*") for p in dst.glob(pat)}
    for p in sorted(junk):
        if not p.exists():
            continue
        if p.is_dir():
            force_rmtree(p)
        else:
            p.chmod(0o666)
            p.unlink()
    # 真实包（不用 editable 安装的软链）
    shutil.copytree(
        ROOT / "src" / "paratranz_agent",
        dst / "paratranz_agent",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def write_extras(stage: Path) -> None:
    for name, body in LAUNCHERS.items():
        (stage / name).write_bytes(body.encode("ascii"))  # 只能 ASCII
    lic = ROOT / "LICENSE"
    if lic.exists():
        shutil.copy2(lic, stage / "LICENSE.txt")
    # 免费声明必须跟着包走：这段文字就是给「从付费渠道拿到包的人」看的。
    # 换行统一 CRLF + BOM，Windows 记事本双击打开不会乱码。
    notice = ROOT / "NOTICE.txt"
    if notice.exists():
        text = notice.read_text(encoding="utf-8").replace("\r\n", "\n")
        (stage / "免费声明.txt").write_text(text.replace("\n", "\r\n"), encoding="utf-8-sig")
    guide = ROOT / "docs" / "portable-quickstart.txt"
    if guide.exists():
        # 带 BOM：Windows 记事本才不会把中文认成乱码
        text = guide.read_text(encoding="utf-8")
        (stage / "快速上手.txt").write_text(text, encoding="utf-8-sig")


def smoke(stage: Path) -> None:
    """打完包先自检：CLI 能跑、依赖能 import。"""
    py = stage / "python" / "python.exe"
    for args, label in (
        (["-c", "import paratranz_agent, fastapi, uvicorn, httpx, pydantic, typer, rich, yaml"],
         "import 依赖"),
        (["-m", "paratranz_agent.cli", "version"], "CLI version"),
    ):
        r = subprocess.run([str(py), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(stage))
        if r.returncode != 0:
            raise SystemExit(f"自检失败（{label}）：\n{r.stdout}\n{r.stderr}")
        print(f"  ✓ {label}: {(r.stdout or '').strip().splitlines()[-1] if r.stdout.strip() else 'ok'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default="3.13.7", help="embeddable 版本（默认 3.13.7）")
    args = ap.parse_args()

    ver = project_version()
    name = f"paratranz-agent-{ver}-win64"
    stage = DIST / name
    print(f"构建 {name}（Python {args.python}）")

    if not SITE.is_dir():
        raise SystemExit(f"找不到依赖目录 {SITE}，先在项目根建好 venv 并 pip install -e .")

    if stage.exists():
        force_rmtree(stage)
    (stage / "python").mkdir(parents=True)

    embed = fetch_embed(args.python)
    print("  解压运行时 …")
    with zipfile.ZipFile(embed) as z:
        z.extractall(stage / "python")

    patch_pth(stage / "python", args.python)
    print("  复制依赖与包 …")
    copy_deps(stage / "python")
    write_extras(stage)
    clear_readonly(stage)          # 从附件/压缩包复制来的图可能带只读位
    print("  自检 …")
    smoke(stage)

    out = DIST / f"{name}.zip"
    out.unlink(missing_ok=True)
    print("  压缩 …")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(stage.rglob("*")):
            z.write(p, p.relative_to(DIST))

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (DIST / f"{name}.zip.sha256").write_text(
        f"{digest}  {name}.zip\n", encoding="ascii"
    )
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"\n完成：{out}（{size_mb:.1f} MB）")
    print(f"sha256：{digest}")
    print(f"\n发版：gh release create v{ver} \"{out}\" \"{out}.sha256\" --notes-file dist/release-notes.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
