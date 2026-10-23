"""RPG Maker MV/MZ 词条提取器。

覆盖：
- data/Actors.json         name / nickname / profile
- data/Items|Weapons|Armors|Skills|States|Enemies.json  name / description / message*
- data/System.json         gameTitle / terms.*
- data/Map*.json           code 401/405 的对话文本
- data/CommonEvents.json   同上
- data/Troops.json         code 401 的对话

key 形如 `data/Actors.json::[12].name` 或 `data/Map001.json::L45.code401`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TEXT_FIELDS = ("name", "nickname", "profile", "description", "message1", "message2",
               "message3", "message4")
DIALOG_CODES = {401, 405}   # 401 = 对话行，405 = 滚动文本


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


def find_data_dir(root: Path) -> Path | None:
    """MV 是 www/data，MZ 是 data。"""
    for cand in (root / "www" / "data", root / "data", root):
        if cand.is_dir() and any(cand.glob("*.json")):
            return cand
    return None


def collect_from_events(events, rel: str, out: list, *, min_len: int, max_len: int, tag: str):
    """扫事件指令列表里的对话。"""
    if not isinstance(events, list):
        return
    for idx, cmd in enumerate(events):
        if not isinstance(cmd, dict):
            continue
        if cmd.get("code") not in DIALOG_CODES:
            continue
        params = cmd.get("parameters") or []
        if not params or not isinstance(params[0], str):
            continue
        s = params[0].strip()
        if not (min_len <= len(s) <= max_len):
            continue
        if not any(ch.isalpha() for ch in s):
            continue
        out.append({
            "key": f"{rel}::{tag}[{idx}]",
            "original": s,
            "context": rel,
        })


def main() -> int:
    a = parse_args()
    root = Path(a["input"]).resolve()
    out_path = Path(a["output"])
    opts = a["opts"]
    min_len = int(opts.get("min_len") or 2)
    max_len = int(opts.get("max_len") or 5000)
    want_dialog = (opts.get("dialog") or "1") not in ("0", "false", "no")
    want_fields = (opts.get("fields") or "1") not in ("0", "false", "no")

    data_dir = find_data_dir(root)
    if data_dir is None:
        print(f"未找到 data 目录（试过 www/data、data、根目录）：{root}", file=sys.stderr)
        return 2

    entries: list[dict] = []
    files = 0

    for p in sorted(data_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[skip] {p.name}: {exc}", file=sys.stderr)
            continue
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        files += 1
        name = p.name

        # 1) 数组型数据库（Actors/Items/...）
        if isinstance(data, list) and want_fields:
            for i, row in enumerate(data):
                if not isinstance(row, dict):
                    continue
                for f in TEXT_FIELDS:
                    v = row.get(f)
                    if isinstance(v, str) and v.strip():
                        s = v.strip()
                        if min_len <= len(s) <= max_len:
                            entries.append({
                                "key": f"{rel}::[{i}].{f}",
                                "original": s,
                                "context": f"{name} id={row.get('id', i)}",
                            })

        # 2) Map / CommonEvents / Troops：事件指令
        if want_dialog and isinstance(data, dict):
            if isinstance(data.get("events"), list):          # Map
                for ei, ev in enumerate(data["events"]):
                    if not isinstance(ev, dict):
                        continue
                    for pi, page in enumerate(ev.get("pages") or []):
                        if isinstance(page, dict):
                            collect_from_events(
                                page.get("list"), rel, entries,
                                min_len=min_len, max_len=max_len,
                                tag=f"ev{ei}p{pi}",
                            )
            if isinstance(data.get("list"), list):            # CommonEvents 单条
                collect_from_events(data["list"], rel, entries,
                                    min_len=min_len, max_len=max_len, tag="ce")
            if isinstance(data.get("pages"), list):           # Troops
                for pi, page in enumerate(data["pages"]):
                    if isinstance(page, dict):
                        collect_from_events(page.get("list"), rel, entries,
                                            min_len=min_len, max_len=max_len,
                                            tag=f"tp{pi}")

        # 3) System.json：标题与术语
        if name == "System.json" and isinstance(data, dict) and want_fields:
            title = data.get("gameTitle")
            if isinstance(title, str) and title.strip():
                entries.append({"key": f"{rel}::gameTitle",
                                "original": title.strip(), "context": name})
            terms = data.get("terms")
            if isinstance(terms, dict):
                for group, vals in terms.items():
                    if isinstance(vals, list):
                        for i, v in enumerate(vals):
                            if isinstance(v, str) and v.strip():
                                entries.append({
                                    "key": f"{rel}::terms.{group}[{i}]",
                                    "original": v.strip(),
                                    "context": f"{name} terms.{group}",
                                })
                    elif isinstance(vals, str) and vals.strip():
                        entries.append({
                            "key": f"{rel}::terms.{group}",
                            "original": vals.strip(),
                            "context": f"{name} terms",
                        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"扫描 {files} 个文件，提取 {len(entries)} 条（data={data_dir}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
