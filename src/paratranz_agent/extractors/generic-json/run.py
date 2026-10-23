"""通用 JSON 词条提取器。

递归遍历 JSON，把「路径命中 patterns 且是字符串」的叶子节点提取出来。
适合 RPG Maker 的 data/*.json、各类 locale/*.json、i18n 文件。

用法：
    python run.py --input <目录> --output <out.jsonl> \
        --opt glob="**/*.json" --opt paths="*.name,*.description" --opt min_len=2
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path

DEFAULT_SKIP_KEYS = {
    "id", "type", "iconIndex", "animationId", "battlerName", "faceName",
    "characterName", "characterIndex", "note", "meta", "uuid", "version",
}


def parse_args() -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--opt", action="append", default=[])
    ns = ap.parse_args()
    opts: dict[str, str] = {}
    for raw in ns.opt:
        if "=" in raw:
            k, v = raw.split("=", 1)
            opts[k.strip()] = v.strip()
    return {"input": ns.input, "output": ns.output, "opts": opts}


def match_path(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    for p in patterns:
        if fnmatch.fnmatch(path, p):
            return True
        # 允许只写末段，如 "name" 匹配 "0.name"
        if fnmatch.fnmatch(path.rsplit(".", 1)[-1], p):
            return True
    return False


def walk(node, path: str, out: list, *, patterns, min_len, max_len, skip_keys, rel: str):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in skip_keys:
                continue
            walk(v, f"{path}.{k}" if path else k, out, patterns=patterns,
                 min_len=min_len, max_len=max_len, skip_keys=skip_keys, rel=rel)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]", out, patterns=patterns,
                 min_len=min_len, max_len=max_len, skip_keys=skip_keys, rel=rel)
    elif isinstance(node, str):
        s = node.strip()
        if not (min_len <= len(s) <= max_len):
            return
        if not match_path(path, patterns):
            return
        # 纯数字/纯符号跳过
        if not any(ch.isalpha() for ch in s):
            return
        out.append({
            "key": f"{rel}::{path}",
            "original": s,
            "context": rel,
        })


def main() -> int:
    a = parse_args()
    root = Path(a["input"]).resolve()
    out_path = Path(a["output"])
    opts = a["opts"]

    glob = opts.get("glob") or "**/*.json"
    patterns = [p.strip() for p in (opts.get("paths") or "").split(",") if p.strip()]
    min_len = int(opts.get("min_len") or 2)
    max_len = int(opts.get("max_len") or 5000)
    skip = {k.strip() for k in (opts.get("skip_keys") or "").split(",") if k.strip()}
    skip_keys = DEFAULT_SKIP_KEYS | skip

    files = 0
    entries: list[dict] = []
    for p in sorted(root.glob(glob)):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[skip] {p}: {exc}", file=sys.stderr)
            continue
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        walk(data, "", entries, patterns=patterns, min_len=min_len,
             max_len=max_len, skip_keys=skip_keys, rel=rel)
        files += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"扫描 {files} 个文件，提取 {len(entries)} 条", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
