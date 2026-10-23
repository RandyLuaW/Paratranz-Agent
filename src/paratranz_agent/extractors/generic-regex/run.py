"""通用正则提取器。

扫文本文件，按正则抓取。适合 Ren'Py 的 .rpy 对话、自定义 .txt/.csv/.ini。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def parse_args() -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--opt", action="append", default=[])
    ns = ap.parse_args()
    opts = {}
    for raw in ns.opt:
        if "=" in raw:
            k, v = raw.split("=", 1)
            opts[k.strip()] = v.strip()
    return {"input": ns.input, "output": ns.output, "opts": opts}


def main() -> int:
    a = parse_args()
    root = Path(a["input"]).resolve()
    out_path = Path(a["output"])
    opts = a["opts"]

    glob = opts.get("glob") or "**/*.txt"
    # 默认正则**必须排除换行**：`"([^"]{2,})"` 会让引号跨行配对，
    # 提出来的词条 original 里带换行、而 key 记的是起始行号 →
    # 这类词条任何行号写回器都还原不了（回写时只能拒绝）。见 writeback/line.py。
    pattern = opts.get("pattern") or r'"([^"\n]{2,})"'
    group = int(opts.get("group") or 1)
    min_len = int(opts.get("min_len") or 2)
    max_len = int(opts.get("max_len") or 5000)
    encoding = opts.get("encoding") or "utf-8"

    try:
        rx = re.compile(pattern)
    except re.error as exc:
        print(f"正则无效：{exc}", file=sys.stderr)
        return 2

    files = 0
    entries: list[dict] = []
    for p in sorted(root.glob(glob)):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding=encoding, errors="replace")
        except OSError as exc:
            print(f"[skip] {p}: {exc}", file=sys.stderr)
            continue
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        files += 1
        seen_in_file: dict[str, int] = {}
        for m in rx.finditer(text):
            try:
                s = m.group(group)
            except Exception:  # noqa: BLE001 - 分组不存在就跳过
                continue
            if not isinstance(s, str):
                continue
            s = s.strip()
            if not (min_len <= len(s) <= max_len):
                continue
            if not any(ch.isalpha() for ch in s):
                continue
            line = text.count("\n", 0, m.start()) + 1
            base = f"{rel}::L{line}"
            n = seen_in_file.get(base, 0)
            seen_in_file[base] = n + 1
            key = base if n == 0 else f"{base}#{n}"
            entries.append({"key": key, "original": s, "context": f"{rel}:{line}"})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"扫描 {files} 个文件，提取 {len(entries)} 条", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
