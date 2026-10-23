"""本轮（审计低危 + 上传新文件）顺带修掉的一致性问题的回归测试。

这些问题都不是「功能不见了」，而是**两处实现口径不一致**或**报告说了没做的事**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ------------------------------------------------- 路径白名单：一处实现，两处调用
def test_matches_any_is_exact_or_prefix_not_substring():
    """子串匹配下 `--only a` 会命中 `data/*.json` 里的一切（"a" 是 "data" 的一半）。"""
    from paratranz_agent.filematch import matches_any

    assert matches_any("data/Actors.json", ["data/Actors.json"])
    assert matches_any("data/Actors.json", ["data"])
    assert matches_any("data/Actors.json", ["data/"])
    assert not matches_any("data/Actors.json", ["a"])
    assert not matches_any("data/Actors.json", ["Actors"])  # 不是路径段
    # 空白名单 = 不筛
    assert matches_any("data/Actors.json", [])


def test_matches_any_normalizes_windows_paths():
    """同一个文件可能写成 `Data\\A.json` / `./data/a.json`，不能因写法漏掉。"""
    from paratranz_agent.filematch import matches_any

    assert matches_any("Data\\Actors.json", ["data/actors.json"])
    assert matches_any("./data/Actors.json", ["data"])
    assert matches_any("/data/Actors.json", ["data"])


def test_translate_files_filter_uses_the_shared_predicate():
    """翻译侧 `translate.files` 必须与回写 `--only` 同一条语义。"""
    import inspect

    from paratranz_agent.pipelines import translate

    src = inspect.getsource(translate.TranslatePipeline._collect)
    assert "matches_any(" in src, "翻译侧没走共用谓词"
    assert "in f.name for a in files" not in src, "还在用子串匹配"


# ------------------------------------------------- 报告说的是事实：预览不报备份目录
def _wb_env(tmp_path: Path):
    """一个最小的回写环境：游戏目录 + 一条提取记录。"""
    from paratranz_agent.server.extraction import JOB_DIR

    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "A.json").write_text('{"a": "Hello"}', encoding="utf-8")
    jid = "20261024-010101-wb001"
    root = tmp_path / "state" / JOB_DIR / jid
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({
        "job_id": jid, "extractor_id": "generic-json", "game_dir": str(game),
        "status": "done", "entries": 1, "options": {},
    }), encoding="utf-8")
    (root / "entries.jsonl").write_text(json.dumps(
        {"key": "data/A.json::a", "original": "Hello", "context": ""}), encoding="utf-8")
    return game, tmp_path / "state", jid


class _FakeClient:
    """只回答回写需要的两个问题。"""

    def iter_strings(self, **_kw):
        class S:
            key = "data/A.json::a"
            translation = "你好"
            original = "Hello"
            stage = 5

        yield S()


def test_in_place_preview_does_not_claim_a_backup_dir(tmp_path):
    """预览（dry_run）不落盘，也就不该报 `backup_dir` —— 那个目录根本没建。"""
    from paratranz_agent.writeback import run_writeback

    game, state, jid = _wb_env(tmp_path)
    rep = run_writeback(
        client=_FakeClient(), state_dir=state, job_id=jid, min_stage=3,
        in_place=True, dry_run=True,
    )
    assert rep.written == 1, "预览也要算出会写多少条"
    assert rep.backup_dir == "", "预览时 backup_dir 必须是空的（没备份就别声称备份了）"
    assert not (state / "writeback" / jid).exists() or not any(
        p.name.startswith("backup-") for p in (state / "writeback" / jid).iterdir())


def test_in_place_real_run_reports_existing_backup_dir(tmp_path):
    """真正原地写时，报告里的 `backup_dir` 必须真的存在（这才是可运维的前提）。"""
    from paratranz_agent.writeback import run_writeback

    game, state, jid = _wb_env(tmp_path)
    rep = run_writeback(
        client=_FakeClient(), state_dir=state, job_id=jid, min_stage=3,
        in_place=True, dry_run=False,
    )
    assert rep.written == 1
    assert rep.backup_dir and Path(rep.backup_dir).is_dir(), rep.backup_dir
    assert (Path(rep.backup_dir) / "data" / "A.json").is_file()


# ------------------------------------------------- 术语命中口径：校验与筛选一致
def test_term_hit_whole_word_for_ascii_and_substring_for_cjk():
    from paratranz_agent.qa import term_hit

    assert not term_hit("Start the game", "art")
    assert not term_hit("The heart is red", "he")
    assert term_hit("The heart is red", "heart")
    assert term_hit("用he说话", "he")        # 与中文相邻仍算命中
    assert term_hit("术语表里有龙", "龙")     # 中文按子串


def test_glossary_for_uses_same_predicate_as_qa(tmp_path):
    """筛选说命中、校验说没命中，会让无关术语塞进 prompt（白占 token）。"""
    from paratranz_agent.store import Store

    st = Store(tmp_path / "agent.db")
    try:
        st.term_upsert("art", "美术", variants=[], pos="noun")
        st.term_upsert("dragon", "龙", variants=[], pos="noun")
        st.term_commit()
        got = st.glossary_for(["Start the game"])
        assert [h["term"] for h in got] == [], f"art 不该命中 Start：{got}"
        got2 = st.glossary_for(["The dragon flies"])
        assert [h["term"] for h in got2] == ["dragon"]
    finally:
        st.close()
