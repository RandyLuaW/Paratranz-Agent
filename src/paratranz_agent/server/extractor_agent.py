"""让 Agent 写/改提取器脚本。

## 流程

1. `analyze_game_dir` 给目录画像（扩展名分布、引擎猜测、命中特征）
2. `sample_files` 抓几个代表性文件的内容片段
3. 把画像 + 样例 + 用户提示喂给模型，要求输出一个 JSON：
   `{id, name, engine, desc, input_hint, args[], code}`
4. 语法预检 + 安全护栏（禁止超出「读文件 + 写 JSONL」范围的能力）
5. 落盘到 `state/extractors/<id>/`，打 `.agent` 标记
6. 自动试跑一次，返回提取条数 + 校验报告

「改进」走同一套，但额外给模型看现有 yaml、现有代码和上次运行日志，并备份旧脚本。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import yaml

from ..llm import BaseLLM, build_messages, extract_json
from .extraction import (
    ExtractionError,
    ExtractorMeta,
    _extractor_root,
    _load_meta,
    analyze_game_dir,
    check_game_dir,
    check_id,
    get_extractor,
    job_log,
    list_jobs,
    load_entries,
    normalize,
    run_extractor,
    sample_files,
    validate,
)

# 提取器只需要「读文件 + 写 JSONL」。这些能力超出范围，生成时直接拒绝。
# 注意要覆盖逗号导入（`import os, subprocess`），只匹配 `import X` 会漏。
_BANNED_MODULES = (
    "subprocess", "socket", "ctypes", "requests", "httpx", "urllib",
    "aiohttp", "ftplib", "telnetlib", "pty", "multiprocessing",
)
_BANNED_CALLS = (
    (r"\bos\.system\b", "os.system（执行命令）"),
    (r"\bos\.popen\b", "os.popen（执行命令）"),
    (r"\bos\.exec\w*\b", "os.exec*（替换进程）"),
    (r"\bshutil\.rmtree\b", "shutil.rmtree（删除目录）"),
    (r"\bos\.remove\b|\bos\.unlink\b|\bos\.rmdir\b", "删除文件的调用"),
    (r"\beval\s*\(", "eval（动态执行）"),
    (r"\bexec\s*\(", "exec（动态执行）"),
    (r"\b__import__\s*\(", "__import__"),
)

SYSTEM_PROMPT = """你是游戏本地化工程师。任务：读一个游戏目录的结构与文件内容，写出一个「词条提取器」Python 脚本。

## 提取器协议（必须严格遵守）

脚本从命令行接收：
  --input <游戏目录>   --output <输出.jsonl>   --opt key=value（可多次）
也可从环境变量读：PTZ_INPUT / PTZ_OUTPUT / PTZ_OPTIONS（JSON 字符串）

脚本要做的事：遍历游戏目录，把**需要翻译的文本**抽出来，每行往输出文件写一个 JSON：
  {"key": "相对路径::块内定位", "original": "原文", "context": "给人看的定位信息"}

硬性要求：
- key 全局唯一（同一文件里重复出现要加 #2、#3 后缀）
- original 必须是非空字符串，去掉首尾空白
- context 用于在平台里定位，写上文件名 + 行号/路径
- **只用 Python 标准库**（json / os / pathlib / re / sys / argparse）
- 输出必须是 UTF-8，一行一个 JSON，不要写数组
- 扫描统计打到 stderr（print(..., file=sys.stderr)）
- 跳过明显不需要翻译的内容：纯数字、纯符号、URL、变量名、单个字母
- 不要提取已被翻译过的字段（例如目标是英文游戏，跳过含中文的值）

## 能力边界（超范围会被拒绝写入）

禁止：subprocess / os.system / socket / requests / ctypes / eval / exec / 删除文件的调用。
你只需要读文件、解析、写输出文件。

## 输出格式

只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块：
{
  "id": "小写英文短横线 id，如 mygame-json",
  "name": "显示名，如 MyGame JSON",
  "engine": "引擎或格式名，如 rpgmaker / unity / generic",
  "desc": "一句话说明它提取什么",
  "input_hint": "该指向哪个目录",
  "args": [{"name": "glob", "label": "文件匹配", "default": "**/*.json", "hint": "..."}],
  "code": "完整的 run.py 内容"
}
"""


def _safety_check(code: str) -> list[str]:
    problems: list[str] = []
    for mod in _BANNED_MODULES:
        # 覆盖 `import a, b, mod` 与 `from mod import x`
        if re.search(r"\bimport\b[^\n]*\b" + mod + r"\b", code) or re.search(
            r"\bfrom\s+" + mod + r"\b", code
        ):
            problems.append(mod + "（超出提取器能力边界）")
    for pattern, label in _BANNED_CALLS:
        if re.search(pattern, code):
            problems.append(label)
    return problems


def _profile_text(game_dir: str) -> str:
    prof = analyze_game_dir(game_dir)
    lines = [
        "目录：" + prof["game_dir"],
        "文件数：" + str(prof["files"]) + ("（已截断）" if prof.get("truncated") else ""),
        "引擎猜测：" + prof["engine_guess"],
        "扩展名分布：" + ", ".join(k + "×" + str(v) for k, v in prof["by_ext"].items()),
        "命中特征：" + (", ".join(prof["markers"]) or "无"),
        "",
        "文件样例（相对路径）：",
    ]
    lines += ["  " + p for p in prof["sample"][:25]]
    return "\n".join(lines)


def _samples_text(game_dir: str, max_files: int = 5, max_chars: int = 1200) -> str:
    samples = sample_files(game_dir, max_files=max_files, max_chars=max_chars)
    if not samples:
        return "（无法读取文件内容样例）"
    out = []
    for s in samples:
        out.append(
            "--- " + s["path"] + "（" + str(s["size"]) + " 字节）---\n" + s["preview"]
        )
    return "\n\n".join(out)


def _strip_fence(code: str) -> str:
    code = re.sub(r"^```[a-zA-Z]*\n", "", code.strip())
    code = re.sub(r"\n```\s*$", "", code)
    return code.strip()


def _parse_generated(raw: str, *, require_id: str = "") -> dict[str, Any]:
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ExtractionError("模型没有返回 JSON 对象")

    code = _strip_fence(str(data.get("code") or ""))
    if not code:
        raise ExtractionError("模型没有返回脚本代码（code 为空）")

    problems = _safety_check(code)
    if problems:
        raise ExtractionError(
            "生成的脚本超出提取器能力边界：" + "、".join(problems)
            + "。已拒绝写入，请重试或手动编写。"
        )

    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as exc:
        raise ExtractionError("生成的脚本有语法错误：" + str(exc)) from exc

    if "--output" not in code:
        raise ExtractionError("生成的脚本没有接收 --output 参数")

    eid = str(data.get("id") or require_id or "").strip().lower()
    eid = re.sub(r"[^a-z0-9_.-]", "-", eid).strip("-")[:40] or "agent-extractor"
    eid = check_id(eid, "提取器 id")

    args = data.get("args")
    clean_args: list[dict[str, str]] = []
    if isinstance(args, list):
        for a in args[:10]:
            if isinstance(a, dict) and a.get("name"):
                clean_args.append({
                    "name": str(a["name"])[:30],
                    "label": str(a.get("label") or a["name"])[:40],
                    "default": str(a.get("default") or ""),
                    "hint": str(a.get("hint") or "")[:80],
                })

    return {
        "id": eid,
        "name": str(data.get("name") or eid)[:60],
        "engine": str(data.get("engine") or "custom")[:30],
        "desc": str(data.get("desc") or "")[:200],
        "input_hint": str(data.get("input_hint") or "")[:200],
        "args": clean_args,
        "code": code,
    }


def _write_extractor(state_dir, data: dict[str, Any]) -> ExtractorMeta:
    eid = check_id(data["id"], "提取器 id")
    d = _extractor_root(state_dir) / eid
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": eid,
        "name": data["name"],
        "engine": data["engine"],
        "version": "0.1.0",
        "desc": data["desc"],
        "input_hint": data["input_hint"],
        "entry": "run.py",
        "args": data["args"],
    }
    (d / "extractor.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (d / "run.py").write_text(data["code"], encoding="utf-8")
    (d / ".agent").write_text("generated by agent\n", encoding="utf-8")
    return _load_meta(d)


def _defaults(meta: ExtractorMeta) -> dict[str, str]:
    return {a["name"]: a.get("default", "") for a in meta.args if a.get("name")}


def _trial(state_dir, meta: ExtractorMeta, game_dir: str) -> dict[str, Any]:
    """试跑一次并校验。失败也把原因带回去，不抛异常。"""
    try:
        res = run_extractor(state_dir, meta.id, game_dir, _defaults(meta))
    except ExtractionError as exc:
        return {"ok": False, "error": str(exc)}
    if res.status != "done":
        return {"ok": False, "error": res.error, "job_id": res.job_id}
    entries = load_entries(state_dir, res.job_id)
    report = validate(entries)
    merged, stats = normalize(entries)
    return {
        "ok": True,
        "job_id": res.job_id,
        "entries": res.entries,
        "duration_ms": res.duration_ms,
        "merge": stats,
        "validate": report,
        "sample": merged[:8],
    }


def generate(
    llm: BaseLLM, state_dir, game_dir: str, hint: str = "", on_log=None
) -> dict[str, Any]:
    """让模型从零写一个提取器，落盘后自动试跑。"""
    gdir = check_game_dir(game_dir)
    if on_log:
        on_log("分析目录结构…")
    user = (
        "## 目录画像\n\n" + _profile_text(str(gdir))
        + "\n\n## 文件内容样例\n\n" + _samples_text(str(gdir))
    )
    if hint.strip():
        user += "\n\n## 额外要求\n\n" + hint.strip()

    if on_log:
        on_log("让模型生成脚本…")
    raw = llm.chat(
        build_messages(SYSTEM_PROMPT, user),
        json_mode=True,
    )
    data = _parse_generated(raw)

    if on_log:
        on_log("写入提取器 " + data["id"] + " 并试跑…")
    meta = _write_extractor(state_dir, data)
    trial = _trial(state_dir, meta, str(gdir))
    return {"extractor": meta.as_dict(), "trial": trial, "game_dir": str(gdir)}


def improve(
    llm: BaseLLM,
    state_dir,
    eid: str,
    game_dir: str,
    feedback: str = "",
    on_log=None,
) -> dict[str, Any]:
    """在现有提取器上继续改进：给模型看现有脚本 + 上次日志 + 用户反馈。"""
    gdir = check_game_dir(game_dir)
    meta = get_extractor(state_dir, eid)
    old_code = (Path(meta.path) / meta.entry).read_text(encoding="utf-8", errors="replace")
    old_yaml = (Path(meta.path) / "extractor.yaml").read_text(
        encoding="utf-8", errors="replace"
    )

    last_log = ""
    for row in list_jobs(state_dir, limit=30):
        if row.get("extractor_id") == eid:
            last_log = job_log(state_dir, row["job_id"], tail=60)
            break

    user = (
        "## 目录画像\n\n" + _profile_text(str(gdir))
        + "\n\n## 文件内容样例\n\n" + _samples_text(str(gdir))
        + "\n\n## 现有 extractor.yaml\n\n```yaml\n" + old_yaml + "\n```"
        + "\n\n## 现有 run.py\n\n```python\n" + old_code + "\n```"
    )
    if last_log:
        user += "\n\n## 上次运行日志（末尾）\n\n```\n" + last_log + "\n```"
    if feedback.strip():
        user += "\n\n## 用户要求的改进\n\n" + feedback.strip()
    else:
        user += (
            "\n\n## 任务\n\n"
            "检查这个提取器是否漏掉了可翻译内容，或提取了不该提取的内容，改进它。"
        )
    user += "\n\n请保持相同的 id（" + eid + "），输出改进后的完整脚本。只输出那个 JSON 对象。"

    if on_log:
        on_log("让模型改进 " + eid + "…")
    raw = llm.chat(
        build_messages(SYSTEM_PROMPT, user),
        json_mode=True,
    )
    data = _parse_generated(raw, require_id=eid)
    data["id"] = eid  # 强制沿用原 id，否则路由和历史记录会断

    # 备份旧版，便于回溯
    backup_dir = _extractor_root(state_dir) / eid / ".backup"
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (backup_dir / ("run-" + stamp + ".py")).write_text(old_code, encoding="utf-8")

    if on_log:
        on_log("覆写并试跑…")
    new_meta = _write_extractor(state_dir, data)
    trial = _trial(state_dir, new_meta, str(gdir))
    return {"extractor": new_meta.as_dict(), "trial": trial, "game_dir": str(gdir)}
