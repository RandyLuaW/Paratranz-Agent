"""「Agent 写提取器」演示脚本（被 fake_openai 引用，真实存在的文件，避免转义地狱）。"""
import argparse
import json
import os
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--opt", action="append", default=[])
    ns = ap.parse_args()
    opts = dict(o.split("=", 1) for o in ns.opt if "=" in o)

    root = Path(ns.input)
    out = []
    for p in sorted(root.glob(opts.get("glob", "**/*.json"))):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        rows = data if isinstance(data, list) else [data]
        for i, item in enumerate(rows):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                key = rel + ("[" + str(i) + "].name" if isinstance(data, list) else "::name")
                out.append({"key": key, "original": item["name"], "context": rel})

    with open(ns.output, "w", encoding="utf-8") as f:
        for e in out:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print("done " + str(len(out)), file=sys.stderr)


main()
