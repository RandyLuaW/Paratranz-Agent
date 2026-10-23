"""真实平台只读体检工具（唯一会写平台的子命令是 reset，且必须显式 --yes）。

为什么要有这个文件：`smoke.py` / `web_smoke.py` 都跑在假平台上，它们证明不了
「平台真的这么返回」。本工具直接打真实 ParaTranz API，用来做三件事：

  probe   看每个列表端点的真实返回形态（裸数组 vs 分页包）—— 假平台最容易在这里说谎
  census  对全项目跑确定性 QA（不调模型、不花钱），统计各规则命中分布
  sample  抽样看被打回的条目到底该不该打回（判断误报）
  reset   把所有非「已翻译」的词条拨回 stage=1（测试后还原用）

用法：
    python tools/realcheck.py probe
    python tools/realcheck.py census
    python tools/realcheck.py sample --n 12
    python tools/realcheck.py reset --yes

凭据从 `.ptz-state/settings.json` 的 token 读，项目号用 `--project` 覆盖。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.client import ParaTranzClient  # noqa: E402
from paratranz_agent.models import STAGE_ZH, Stage  # noqa: E402
from paratranz_agent.qa import Severity, run_qa  # noqa: E402
from paratranz_agent.store import Store  # noqa: E402

LIST_ENDPOINTS = [
    ("GET /projects", "/projects", {"page": 1, "pageSize": 5}),
    ("GET /files", "/projects/{projectId}/files", {}),
    ("GET /files/{id}/translation", None, {}),  # 运行时填真实 file id
    ("GET /strings", "/projects/{projectId}/strings", {"page": 1, "pageSize": 3}),
    ("GET /tasks", "/projects/{projectId}/tasks", {"page": 1, "pageSize": 5}),
    ("GET /terms", "/projects/{projectId}/terms", {"page": 1, "pageSize": 5}),
    ("GET /announcements", "/projects/{projectId}/announcements", {"page": 1, "pageSize": 5}),
    ("GET /members", "/projects/{projectId}/members", {}),
    ("GET /issues", "/projects/{projectId}/issues", {"status": 0, "page": 1, "pageSize": 5}),
    ("GET /history", "/projects/{projectId}/history", {"page": 1, "pageSize": 3}),
    ("GET /scores", "/projects/{projectId}/scores", {"page": 1, "pageSize": 3}),
]


def _client(args) -> ParaTranzClient:
    st = json.loads((ROOT / ".ptz-state/settings.json").read_text(encoding="utf-8"))
    return ParaTranzClient(token=st["token"], project_id=args.project)


def _shape(v) -> str:
    if isinstance(v, list):
        return f"list[{len(v)}] of {type(v[0]).__name__ if v else '-'}"
    if isinstance(v, dict):
        return f"dict keys={sorted(v)}"
    return type(v).__name__


def cmd_probe(args) -> int:
    c = _client(args)
    print("=== 列表端点真实返回形态 ===")
    for label, path, params in LIST_ENDPOINTS:
        if path is None:
            files = c.list_files()
            if not files:
                print(f"{label:30s} 跳过（项目没有文件）")
                continue
            path = f"/projects/{{projectId}}/files/{files[0].id}/translation"
        try:
            raw = c.request("GET", path, params=params or None)
        except Exception as e:  # noqa: BLE001
            print(f"{label:30s} ERR {type(e).__name__}: {str(e)[:110]}")
            continue
        print(f"{label:30s} {_shape(raw)}")
        if isinstance(raw, dict):
            for k in ("rowCount", "pageCount", "openCount", "closedCount"):
                if k in raw:
                    print(f"{'':30s}   {k}={raw[k]}")
    p = c.get_project()
    print("\n=== 项目统计 ===")
    print(json.dumps(p.stats, ensure_ascii=False))
    print("\n=== stage 分布 ===")
    for name, stage in (("已翻译(1)", 1), ("有疑问(2)", 2), ("已检查(3)", 3), ("已审核(5)", 5)):
        n = sum(1 for _ in c.iter_strings(stage=Stage(stage), max_items=20000))
        print(f"  {name}: {n}")
    c.close()
    return 0


def cmd_census(args) -> int:
    c = _client(args)
    store = Store(ROOT / ".ptz-state/agent.db")
    terms = store.term_all()
    rules: dict[str, int] = {}
    total = blocked = 0
    for s in c.iter_strings(max_items=20000):
        if int(s.stage) in (0, int(Stage.HIDDEN)):
            continue
        total += 1
        rep = run_qa(
            s.original, s.translation or "", terms=terms,
            block_on=Severity.ERROR, length_ratio_limit=3.5, ascii_ratio_limit=0.55,
        )
        blocked += 0 if rep.ok else 1
        for i in rep.issues:
            rules[i.rule] = rules.get(i.rule, 0) + 1
    print(f"=== 确定性 QA：{total} 条，硬拦 {blocked} 条（{blocked / max(total, 1):.1%}）===")
    for k, v in sorted(rules.items(), key=lambda kv: -kv[1]):
        print(f"  {k:32s} {v}")
    c.close()
    return 0


def cmd_sample(args) -> int:
    c = _client(args)
    store = Store(ROOT / ".ptz-state/agent.db")
    terms = store.term_all()
    n = 0
    for s in c.iter_strings(stage=Stage.DISPUTED, max_items=args.n):
        n += 1
        rep = run_qa(
            s.original, s.translation or "", terms=terms,
            block_on=Severity.ERROR, length_ratio_limit=3.5, ascii_ratio_limit=0.55,
        )
        why = "确定性QA" if not rep.ok else "仅模型二审"
        print(f"\n[{n}] id={s.id} ({why}) key={s.key[:40]!r}")
        print(f"    原: {s.original[:110]!r}")
        print(f"    译: {(s.translation or '')[:110]!r}")
        for i in rep.issues[:2]:
            print(f"    QA: {i.message[:100]}")
    print(f"\n共抽样 {n} 条")
    c.close()
    return 0


def cmd_reset(args) -> int:
    if not args.yes:
        print("reset 会改写真实平台的 stage，必须加 --yes 确认")
        return 2
    c = _client(args)
    moved = 0
    for stage in (Stage.DISPUTED, Stage.CHECKED, Stage.REVIEWED):
        ids = [s.id for s in c.iter_strings(stage=stage, max_items=20000)]
        print(f"stage={int(stage)}({STAGE_ZH.get(int(stage))}): {len(ids)} 条")
        if ids:
            c.set_stage(ids, Stage.TRANSLATED)
            moved += len(ids)
    print(f"已拨回 {moved} 条到 stage=1")
    print("提示：平台 stats 有 6~8 秒延迟，稍等再读才是新值")
    c.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="真实 ParaTranz 平台体检")
    ap.add_argument("command", choices=["probe", "census", "sample", "reset"])
    ap.add_argument("--project", type=int, required=True, help="你的 ParaTranz 项目编号")
    ap.add_argument("--n", type=int, default=12, help="sample 抽样条数")
    ap.add_argument("--yes", action="store_true", help="reset 的确认开关")
    args = ap.parse_args()
    return {"probe": cmd_probe, "census": cmd_census, "sample": cmd_sample, "reset": cmd_reset}[
        args.command
    ](args)


if __name__ == "__main__":
    raise SystemExit(main())
