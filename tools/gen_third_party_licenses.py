"""第三方许可清单生成器：按已安装依赖的 METADATA 汇总，写进 THIRD-PARTY-LICENSES.txt。

为什么要这个文件：免安装包里**分发的是这些库的二进制**，MIT/BSD 类许可要求随分发
保留其版权与许可声明。不写这一份，属于合规缺口。
"""
from __future__ import annotations

import pathlib
import sys
from importlib.metadata import distributions

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "THIRD-PARTY-LICENSES.txt"

# 本项目自身不列；标准库不列
SKIP = {"paratranz-agent", "pip", "setuptools", "wheel"}


def main() -> int:
    rows: list[tuple[str, str, str, str]] = []
    for dist in distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name or name.lower() in SKIP:
            continue
        ver = dist.version or ""
        lic = (dist.metadata.get("License-Expression")
               or dist.metadata.get("License")
               or "")
        lic = " ".join(str(lic).split())
        if not lic or len(lic) > 90:  # 有些包把整篇许可塞进 License 字段
            cls = [c for c in dist.metadata.get_all("Classifier") or []
                   if c.startswith("License ::")]
            lic = cls[0].split("::")[-1].strip() if cls else "见包内许可文件"
        home = (dist.metadata.get("Home-page")
                or next((p.split(", ")[-1] for p in dist.metadata.get_all("Project-URL") or []
                         if "home" in p.lower()), ""))
        rows.append((name, ver, lic, home))

    rows.sort(key=lambda r: r[0].lower())
    lines = [
        "Paratranz Agent —— 第三方组件许可 / Third-Party Components",
        "=========================================================",
        "",
        "本软件分发（含免安装包）中包含下列第三方库的二进制或源码，它们各自遵循其许可。",
        "本项目自身的许可见 LICENSE；本文件仅用于满足第三方许可的声明保留要求。",
        "This distribution bundles the libraries below; each remains under its own",
        "license. See LICENSE for the license of this project itself.",
        "",
        f"{'组件 / Package':<32}{'版本':<14}{'许可 / License'}",
        "-" * 96,
    ]
    for name, ver, lic, home in rows:
        lines.append(f"{name:<32}{ver:<14}{lic}" + (f"   ({home})" if home else ""))
    lines += [
        "",
        "各库的完整许可正文位于其发行包内（site-packages/<包名>-<版本>.dist-info/ 下的",
        "LICENSE 或 METADATA 文件），免安装包中同样保留这些文件。",
        "",
        "若您认为此处遗漏了应当声明的组件，请提交 issue。",
    ]
    OUT.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    print(f"写入 {OUT.name}：{len(rows)} 个组件")
    for name, ver, lic, _ in rows[:6]:
        print(f"  {name} {ver} — {lic}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
