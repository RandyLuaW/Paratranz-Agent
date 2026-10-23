"""Agent 生成/改进提取器的测试。

用假 LLM 回放固定 JSON，验证：落盘、source 标记、试跑、安全护栏、语法预检、备份。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.llm import BaseLLM  # noqa: E402
from paratranz_agent.server import extractor_agent as ea  # noqa: E402
from paratranz_agent.server.extraction import (  # noqa: E402
    ExtractionError,
    list_extractors,
)

# 一个符合协议的最小提取器（直接用真实源码字符串，不经过 heredoc）
GOOD_CODE = (
    "import argparse, json, os, sys\n"
    "from pathlib import Path\n"
    "\n"
    "def main():\n"
    "    ap = argparse.ArgumentParser()\n"
    "    ap.add_argument('--input', required=True)\n"
    "    ap.add_argument('--output', required=True)\n"
    "    ap.add_argument('--opt', action='append', default=[])\n"
    "    ns = ap.parse_args()\n"
    "    opts = dict(o.split('=', 1) for o in ns.opt if '=' in o)\n"
    "    root = Path(ns.input)\n"
    "    out = []\n"
    "    for p in sorted(root.glob(opts.get('glob', '**/*.json'))):\n"
    "        if not p.is_file():\n"
    "            continue\n"
    "        rel = str(p.relative_to(root)).replace(os.sep, '/')\n"
    "        try:\n"
    "            data = json.loads(p.read_text(encoding='utf-8', errors='replace'))\n"
    "        except Exception:\n"
    "            continue\n"
    "        if isinstance(data, dict) and isinstance(data.get('name'), str):\n"
    "            out.append({'key': rel + '::name', 'original': data['name'],\n"
    "                        'context': rel})\n"
    "    with open(ns.output, 'w', encoding='utf-8') as f:\n"
    "        for e in out:\n"
    "            f.write(json.dumps(e, ensure_ascii=False) + chr(10))\n"
    "    print('done ' + str(len(out)), file=sys.stderr)\n"
    "\n"
    "main()\n"
)


class FakeGenLLM(BaseLLM):
    name = "fake-gen"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


@pytest.fixture()
def game(tmp_path: Path) -> Path:
    g = tmp_path / "game"
    g.mkdir()
    (g / "a.json").write_text(json.dumps({"name": "Harold"}), encoding="utf-8")
    (g / "b.json").write_text(json.dumps({"name": "Therese"}), encoding="utf-8")
    return g


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _payload(**over):
    base = {
        "id": "mygame-json",
        "name": "MyGame JSON",
        "engine": "generic",
        "desc": "抽 name 字段",
        "input_hint": "游戏根目录",
        "args": [{"name": "glob", "label": "文件匹配", "default": "**/*.json"}],
        "code": GOOD_CODE,
    }
    base.update(over)
    return base


def test_generate_writes_and_trials(state: Path, game: Path) -> None:
    logs: list[str] = []
    r = ea.generate(FakeGenLLM(_payload()), state, str(game), on_log=logs.append)

    assert r["extractor"]["id"] == "mygame-json"
    assert r["extractor"]["source"] == "agent", "应标记为 Agent 生成"
    assert r["trial"]["ok"] is True, r["trial"]
    assert r["trial"]["entries"] == 2
    assert r["trial"]["validate"]["ok"] is True
    assert logs, "应输出进度日志"

    # 落盘目录含 .agent 标记
    meta = next(m for m in list_extractors(state) if m.id == "mygame-json")
    assert (Path(meta.path) / ".agent").exists()
    assert meta.source == "agent"


def test_generate_rejects_dangerous_code(state: Path, game: Path) -> None:
    bad = GOOD_CODE.replace(
        "import argparse, json, os, sys", "import argparse, json, os, sys, subprocess"
    )
    with pytest.raises(ExtractionError) as ei:
        ea.generate(FakeGenLLM(_payload(code=bad)), state, str(game))
    assert "能力边界" in str(ei.value)
    # 没有落盘
    assert not any(m.id == "mygame-json" for m in list_extractors(state))


@pytest.mark.parametrize("snippet,label", [
    ("import socket", "socket"),
    ("import requests", "网络请求"),
    ("import ctypes", "ctypes"),
    ("os.system('x')", "os.system"),
    ("shutil.rmtree('/')", "删除文件"),
    ("eval('1')", "eval/exec"),
])
def test_safety_patterns(state: Path, game: Path, snippet: str, label: str) -> None:
    bad = GOOD_CODE + "\n" + snippet + "\n"
    with pytest.raises(ExtractionError) as ei:
        ea.generate(FakeGenLLM(_payload(code=bad)), state, str(game))
    assert "能力边界" in str(ei.value), ei.value


def test_generate_rejects_syntax_error(state: Path, game: Path) -> None:
    with pytest.raises(ExtractionError) as ei:
        ea.generate(FakeGenLLM(_payload(code="def f(:\n  pass")), state, str(game))
    assert "语法错误" in str(ei.value)


def test_generate_rejects_missing_output(state: Path, game: Path) -> None:
    with pytest.raises(ExtractionError) as ei:
        ea.generate(
            FakeGenLLM(_payload(code="print('hello')\n")), state, str(game)
        )
    assert "--output" in str(ei.value)


def test_improve_keeps_id_and_backs_up(state: Path, game: Path) -> None:
    ea.generate(FakeGenLLM(_payload()), state, str(game))

    # 第二版：改一下 desc，模拟改进
    r = ea.improve(FakeGenLLM(_payload(desc="改进后的说明")), state, "mygame-json",
                   str(game), feedback="也要抽 description 字段")
    assert r["extractor"]["id"] == "mygame-json", "改进后 id 应保持不变"
    assert r["extractor"]["desc"] == "改进后的说明"
    assert r["trial"]["ok"] is True

    meta = next(m for m in list_extractors(state) if m.id == "mygame-json")
    backups = list((Path(meta.path) / ".backup").glob("run-*.py"))
    assert backups, "改进前应备份旧脚本"


def test_improve_forces_original_id(state: Path, game: Path) -> None:
    """模型想改名也不能改——否则路由/历史记录会断。"""
    ea.generate(FakeGenLLM(_payload()), state, str(game))
    r = ea.improve(FakeGenLLM(_payload(id="renamed-by-model")), state,
                   "mygame-json", str(game))
    assert r["extractor"]["id"] == "mygame-json"


def test_imported_vs_agent_source(state: Path, tmp_path: Path, game: Path) -> None:
    """导入的脚本 source=imported，Agent 生成的 source=agent。"""
    ea.generate(FakeGenLLM(_payload()), state, str(game))

    src = tmp_path / "user-ext"
    src.mkdir()
    (src / "extractor.yaml").write_text(
        "id: user-ext\nname: 用户的\nentry: run.py\n", encoding="utf-8"
    )
    (src / "run.py").write_text(GOOD_CODE, encoding="utf-8")
    from paratranz_agent.server.extraction import install_extractor

    install_extractor(state, src)

    by_id = {m.id: m for m in list_extractors(state)}
    assert by_id["mygame-json"].source == "agent"
    assert by_id["user-ext"].source == "imported"
    assert by_id["rpgmaker-mv"].source == "builtin"


def test_trial_reports_extraction_error(state: Path, tmp_path: Path) -> None:
    """试跑失败也要把原因带回去，而不是抛异常。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    bad = GOOD_CODE.replace("root.glob(opts.get('glob', '**/*.json'))",
                            "root.glob('**/*.nonexistent')")
    r = ea.generate(FakeGenLLM(_payload(code=bad, args=[])), state, str(empty))
    assert r["trial"]["ok"] is True or "error" in r["trial"]
    # 没有匹配文件时 entries 应为 0，validate 通过
    if r["trial"]["ok"]:
        assert r["trial"]["entries"] == 0
