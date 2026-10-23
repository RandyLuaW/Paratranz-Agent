"""提取链路的单测：提取器加载 → 跑 → 归并 → 校验 → 导出。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.server.extraction import (  # noqa: E402
    ExtractionError,
    analyze_game_dir,
    export_paratranz,
    install_extractor,
    list_extractors,
    load_entries,
    normalize,
    remove_extractor,
    run_extractor,
    validate,
)


@pytest.fixture()
def game(tmp_path: Path) -> Path:
    """造一个最小 RPG Maker MV 目录。"""
    data = tmp_path / "game" / "www" / "data"
    data.mkdir(parents=True)
    (data / "Actors.json").write_text(
        json.dumps(
            [
                {"id": 1, "name": "Harold", "nickname": "Hero",
                 "profile": "A brave warrior from the north."},
                {"id": 2, "name": "Therese", "nickname": "Mage",
                 "profile": "A scholar of the arcane arts."},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data / "Items.json").write_text(
        json.dumps([{"id": 1, "name": "Potion", "description": "Restores 50 HP."}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    (data / "Map001.json").write_text(
        json.dumps(
            {"events": [None, {"pages": [{"list": [
                {"code": 401, "parameters": ["Hello there, traveler!"]},
                {"code": 401, "parameters": ["The forest is dangerous at night."]},
            ]}]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data / "System.json").write_text(
        json.dumps(
            {"gameTitle": "Sample Game",
             "terms": {"basic": ["Level", "HP", "MP"]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path / "game"


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_builtin_extractors_present(state: Path) -> None:
    ids = {m.id for m in list_extractors(state)}
    assert {"generic-json", "generic-regex", "rpgmaker-mv"} <= ids
    for m in list_extractors(state):
        assert m.builtin is True
        assert (Path(m.path) / m.entry).exists()


def test_run_rpgmaker_extracts_all_kinds(state: Path, game: Path) -> None:
    res = run_extractor(state, "rpgmaker-mv", game)
    assert res.status == "done", res.error
    assert res.entries == 14
    # 2 actors×3 字段 + 1 物品×(name+desc) + 2 地图对话 + 1 标题 + 3 术语 = 14

    entries = load_entries(state, res.job_id)
    keys = {e["key"] for e in entries}
    assert "www/data/Actors.json::[0].name" in keys
    assert any("ev1p0" in k for k in keys), "地图对话没抽到"
    assert "www/data/System.json::gameTitle" in keys
    assert all(e["original"].strip() for e in entries)


def test_run_missing_dir_raises(state: Path, tmp_path: Path) -> None:
    with pytest.raises(ExtractionError):
        run_extractor(state, "rpgmaker-mv", tmp_path / "nope")


def test_run_bad_game_dir_returns_failed(state: Path, tmp_path: Path) -> None:
    """有目录但没有 data —— 提取器自己报错，结果标记 failed 而不是抛异常。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    res = run_extractor(state, "rpgmaker-mv", empty)
    assert res.status == "failed"
    assert "data" in res.error


def test_relative_path_resolved(state: Path, game: Path, monkeypatch) -> None:
    """相对路径必须按调用者 cwd 解析，不能按提取器目录（曾踩过）。"""
    monkeypatch.chdir(game.parent)
    res = run_extractor(state, "rpgmaker-mv", "game")
    assert res.status == "done", res.error
    assert res.entries > 0


def test_generic_json_paths_filter(state: Path, game: Path) -> None:
    res = run_extractor(
        state, "generic-json", game,
        {"glob": "www/data/*.json", "paths": "*.name", "min_len": "3"},
    )
    assert res.status == "done", res.error
    entries = load_entries(state, res.job_id)
    assert entries
    assert all(e["key"].endswith(".name") for e in entries)


def test_validate_catches_dup_and_empty() -> None:
    bad = [
        {"key": "a::1", "original": "hello", "context": "a"},
        {"key": "a::1", "original": "world", "context": "a"},   # 重复 key
        {"key": "a::2", "original": "   ", "context": "a"},     # 空原文
        {"key": "a::3", "original": "x" * 6000, "context": "a"},  # 超长
    ]
    report = validate(bad)
    assert report["ok"] is False
    rules = {e["rule"] for e in report["errors"]}
    assert "dup_key" in rules and "empty_original" in rules
    assert report["stats"]["too_long"] == 1


def test_validate_ok_for_good_entries(state: Path, game: Path) -> None:
    res = run_extractor(state, "rpgmaker-mv", game)
    report = validate(load_entries(state, res.job_id))
    assert report["ok"] is True
    assert report["stats"]["error_count"] == 0


def test_normalize_dedup_and_trim() -> None:
    raw = [
        {"key": "k1", "original": "  hello  ", "context": "a"},
        {"key": "k1", "original": "hello", "context": "a-longer"},
        {"key": "k2", "original": "hello", "context": "b"},
        {"key": "k3", "original": "", "context": "c"},
    ]
    out, stats = normalize(raw)
    assert len(out) == 2
    assert stats["dup_keys"] == 1
    assert stats["trimmed"] == 1
    assert all(e["original"] == "hello" for e in out)


def test_export_paratranz_shape(state: Path, game: Path) -> None:
    res = run_extractor(state, "rpgmaker-mv", game)
    items = export_paratranz(load_entries(state, res.job_id))
    assert items
    for it in items:
        assert set(it) == {"key", "original", "translation", "context"}
        assert it["translation"] == ""


def test_analyze_guesses_engine(game: Path) -> None:
    r = analyze_game_dir(game)
    assert r["engine_guess"] == "rpgmaker-mv/mz"
    assert r["files"] >= 4
    assert ".json" in r["by_ext"]


def test_install_and_remove_custom_extractor(state: Path, tmp_path: Path) -> None:
    src = tmp_path / "myext"
    src.mkdir()
    (src / "extractor.yaml").write_text(
        "id: myext\nname: 我的提取器\nengine: custom\nentry: run.py\n", encoding="utf-8"
    )
    (src / "run.py").write_text(
        "import json,sys\n"
        "args=dict(a.split('=',1) for a in sys.argv if '=' in a)\n"
        "out=args.get('--output') or sys.argv[sys.argv.index('--output')+1]\n"
        "open(out,'w',encoding='utf-8').write(json.dumps("
        "{'key':'x::1','original':'hi','context':'c'})+'\\n')\n",
        encoding="utf-8",
    )
    meta = install_extractor(state, src)
    assert meta.id == "myext" and meta.builtin is False
    assert any(m.id == "myext" for m in list_extractors(state))

    res = run_extractor(state, "myext", tmp_path)
    assert res.status == "done", res.error
    assert res.entries == 1

    remove_extractor(state, "myext")
    assert not any(m.id == "myext" for m in list_extractors(state))


def test_cannot_remove_builtin(state: Path) -> None:
    with pytest.raises(ExtractionError):
        remove_extractor(state, "rpgmaker-mv")
