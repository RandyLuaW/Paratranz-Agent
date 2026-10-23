"""全命令冒烟：每条 CLI 命令在 --mock 下跑一遍，校验退出码与关键输出。

设计要点：
- 用持久化假平台（.ptz-smoke/fake_platform.json），所以命令之间状态连续，
  能验证「plan 建任务 → status 看得见 → translate 推进 stage」这条链；
- 每条命令都断言退出码 + 至少一个关键字符串，避免"没崩但什么也没做"；
- 最后检查平台侧最终状态（无 stage=0、有任务、有公告、有术语）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _interpreter() -> Path:
    """优先用项目 venv；没有就退回「当前正在跑这个脚本的解释器」。

    以前这里硬编码 `.venv/Scripts/python.exe`（Windows 目录布局），
    在有 venv 的本机能跑，但 CI 的 Linux runner 上必然报「venv 不存在」。
    脚本自己就是被某个 python 启动的，用 `sys.executable` 一定可用。
    """
    for cand in (
        ROOT / ".venv" / "Scripts" / "python.exe",   # Windows venv
        ROOT / ".venv" / "bin" / "python",           # POSIX venv
    ):
        if cand.exists():
            return cand
    return Path(sys.executable)


PY = _interpreter()
sys.path.insert(0, str(ROOT / "tests"))
from fake_openai import start_fake_llm  # noqa: E402

FAKE_LLM_PORT = 8898
SMOKE_STATE = ROOT / ".ptz-smoke"
ENV = {
    **os.environ,
    "PYTHONIOENCODING": "utf-8",
    "PYTHONPATH": str(ROOT / "src"),
    # 产品没有假 LLM 降级，CLI 冒烟也必须有可用的模型端点
    "PTZ_LLM_BASE": f"http://127.0.0.1:{FAKE_LLM_PORT}/v1",
    "PTZ_LLM_API_KEY": "sk-test-0123456789abcdefghij",
}
for _k in ("OPENAI_API_KEY", "PTZ_API_TOKEN", "PARATRANZ_TOKEN"):
    ENV.pop(_k, None)

results: list[tuple[str, bool, str]] = []


def run(name: str, args: list[str], *, expect: list[str], code: int = 0, timeout: int = 180) -> str:
    cmd = [str(PY), "-m", "paratranz_agent.cli", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, cwd=str(ROOT), timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    problems = []
    if proc.returncode != code:
        problems.append(f"exit={proc.returncode} 期望 {code}")
    for token in expect:
        if token not in out:
            problems.append(f"缺少输出 {token!r}")
    ok = not problems
    results.append((name, ok, "; ".join(problems) if problems else "ok"))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + ("" if ok else f"  ← {'; '.join(problems)}"))
    if not ok:
        print("       --- 输出尾部 ---")
        for line in out.strip().splitlines()[-8:]:
            print("       " + line)
    return out


def main() -> int:
    if not PY.exists():
        print(f"venv 不存在: {PY}")
        return 2
    if SMOKE_STATE.exists():
        shutil.rmtree(SMOKE_STATE)
    fake_srv = start_fake_llm(FAKE_LLM_PORT)

    # 冒烟用独立配置 + 独立 state，不碰真实 .ptz-state
    smoke_cfg = SMOKE_STATE / "agent.yaml"
    smoke_cfg.parent.mkdir(parents=True, exist_ok=True)
    smoke_cfg.write_text(
        (ROOT / "config" / "agent.example.yaml")
        .read_text(encoding="utf-8")
        .replace("state_dir: ../.ptz-state", "state_dir: ."),
        encoding="utf-8",
    )
    CFG = smoke_cfg.relative_to(ROOT).as_posix()

    def base(sub: str, *opts: str) -> list[str]:
        """typer 的子命令选项必须写在子命令之后。"""
        return [sub, "--config", CFG, "--mock", *opts]

    print("\n== 基础命令 ==")
    run("version", ["version"], expect=["paratranz-agent"])
    run("--help", ["--help"], expect=["plan", "translate", "review", "glossary", "styleguide", "watch"])
    run("init", ["init", "--path", ".ptz-smoke/gen.yaml"], expect=["已写入"])
    run("init 重复不覆盖", ["init", "--path", ".ptz-smoke/gen.yaml"], expect=["已存在"], code=1)

    print("\n== doctor / status ==")
    run("doctor --mock", ["doctor", "--mock"], expect=["ptz doctor", "OK"])
    run("doctor --json", ["doctor", "--mock", "--json"], expect=['"checks"'])
    run("status --mock", ["status", "--mock"], expect=["项目状态", "未翻译"])
    run("status --json", ["status", "--mock", "--json"], expect=['"progress"', '"local_tm"'])

    print("\n== plan ==")
    run("plan 预览", base("plan"), expect=["分派计划", "dry-run"])
    run("plan --yes", base("plan", "--yes"), expect=["已创建"])
    run("status 看到任务", base("status"), expect=["任务 2"])  # 按目录分片：www/data/ + www/quest/
    run("plan 幂等（重跑不新建）", base("plan", "--yes"), expect=["已创建 0 个"])

    print("\n== translate ==")
    run("translate 预览", base("translate"), expect=["翻译结果", "dry-run"])
    run("translate --yes", base("translate", "--yes"), expect=["翻译结果", "written"])
    run("translate --limit", base("translate", "--limit", "5", "--yes"), expect=["翻译结果"])
    run("translate --json", base("translate", "--json"), expect=['"fetched"'])

    print("\n== review ==")
    run("review 预览", base("review"), expect=["校对结果", "dry-run"])
    run("review --yes", base("review", "--yes"), expect=["校对结果", "disputed"])
    run("review --include-disputed", base("review", "--include-disputed", "--yes"), expect=["校对结果"])
    run("review --json", base("review", "--json"), expect=['"qa_blocked"'])

    print("\n== glossary ==")
    run("glossary 预览", base("glossary"), expect=["术语候选", "dry-run"])
    run("glossary --out", base("glossary", "--out", ".ptz-smoke/terms.md"), expect=["已写入"])
    run("glossary --yes", base("glossary", "--yes"), expect=["提交"])

    print("\n== styleguide ==")
    run("styleguide 预览", base("styleguide"), expect=["汉化规范", "dry-run"])
    run("styleguide --out", base("styleguide", "--no-llm", "--out", ".ptz-smoke/style.md"),
        expect=["已写入"])
    run("styleguide --yes", base("styleguide", "--yes"), expect=["公告"])
    run("styleguide 幂等（更新非新建）", base("styleguide", "--yes"), expect=["公告 update"])

    print("\n== run 全流程 ==")
    run("run 预览", base("run"), expect=["1/5 分派", "5/5 规范", "dry-run"])
    run("run --yes", base("run", "--yes"), expect=["1/5 分派", "5/5 规范"])
    run("run --skip", base("run", "--skip", "plan,glossary", "--yes"), expect=["2/5 翻译"])
    run("run --json", base("run", "--json"), expect=['"stages"', '"llm_usage"'])

    print("\n== watch 守护 ==")
    run("watch 2 轮", base("watch", "--yes", "--interval", "2", "--max-rounds", "2"),
        expect=["第 1 轮", "累计"])
    run("watch --json", base("watch", "--json", "--max-rounds", "1"), expect=['"round"'])

    print("\n== 错误路径 ==")
    run("无 token 时 doctor 报警", ["doctor", "--config", CFG],
        expect=["token", "FAIL"], code=1)
    run("缺配置文件", ["status", "--config", "nope.yaml"], expect=["配置文件不存在"], code=2)

    # ---- 平台最终状态 ----
    print("\n== 最终平台状态 ==")
    plat = json.loads((SMOKE_STATE / "fake_platform.json").read_text(encoding="utf-8"))
    strings = plat["strings"]
    stage0 = [s for s in strings if s["stage"] == 0]
    checked = [s for s in strings if s["stage"] == 3]
    disputed = [s for s in strings if s["stage"] == 2]
    tasks = plat["tasks"]
    anns = plat["announcements"]
    terms = plat["terms"]

    checks = [
        ("无残留未翻译", len(stage0) == 0, f"{len(stage0)} 条"),
        ("有已检查条目", len(checked) > 0, f"{len(checked)} 条"),
        ("有打回条目", len(disputed) > 0, f"{len(disputed)} 条"),
        ("任务已建", len(tasks) > 0, f"{len(tasks)} 个"),
        ("任务都带文件", all(t["taskFiles"] for t in tasks), ""),
        ("公告已发", len(anns) > 0, f"{len(anns)} 条"),
        ("术语表可用", len(terms) >= 1, f"{len(terms)} 条"),
        ("占位符未丢", all(
            "\\N[1]" in s["translation"] for s in strings
            if "\\N[1]" in s["original"] and s["stage"] >= 1
        ), ""),
    ]
    for name, ok, detail in checks:
        results.append((name, ok, detail))
        print(f"  [{'OK' if ok else 'FAIL'}] {name} {detail}")

    try:
        fake_srv.shutdown()
    except Exception:  # noqa: BLE001
        pass

    failed = [r for r in results if not r[1]]
    print(f"\n{'='*60}\n冒烟结果：{len(results)-len(failed)}/{len(results)} 通过")
    if failed:
        print("失败项：")
        for name, _, why in failed:
            print(f"  - {name}: {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
