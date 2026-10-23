"""回写核心的回归测试：定位符解析、JSON 手术式替换、端到端管线。

全部离线：假平台（FakeParaTranz）+ 临时目录里伪造一条提取记录。
重点挡的是「错位写入」——这是唯一会毁掉用户数据的一类错误。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.writeback import (  # noqa: E402
    JsonPathWriter,
    LocatorError,
    WriteItem,
    json_escape,
    parse_locator,
    pull_translations,
    run_writeback,
    scan_strings,
)
from paratranz_agent.writeback.pipeline import WritebackError  # noqa: E402


# --------------------------------------------------------------- 定位符解析
@pytest.mark.parametrize(
    "locator,family,steps",
    [
        ("[12].name", "json", (12, "name")),
        ("0.name", "json", ("0", "name")),
        ("a.b[0].c", "json", ("a", "b", 0, "c")),
        ("gameTitle", "json", ("gameTitle",)),
        ("terms.basic[0]", "json", ("terms", "basic", 0)),
        ("events[3].pages[0].list[2].parameters[0]", "json",
         ("events", 3, "pages", 0, "list", 2, "parameters", 0)),
        # rpgmaker-mv 的紧凑标签要展开成真实路径
        ("ev3p0[45]", "json", ("events", 3, "pages", 0, "list", 45, "parameters", 0)),
        ("ce[12]", "json", ("list", 12, "parameters", 0)),
        ("tp2[7]", "json", ("pages", 2, "list", 7, "parameters", 0)),
    ],
)
def test_parse_json_locators(locator, family, steps):
    p = parse_locator(locator)
    assert p.family == family
    assert p.steps == steps


@pytest.mark.parametrize("locator,line,index", [("L123", 123, 0), ("L7#3", 7, 3), ("L1#0", 1, 0)])
def test_parse_line_locators(locator, line, index):
    p = parse_locator(locator)
    assert p.family == "line"
    assert (p.line, p.index) == (line, index)


@pytest.mark.parametrize("bad", ["", "   ", "L0", "[abc]", "a.", "[12]x"])
def test_parse_rejects_garbage(bad):
    with pytest.raises(LocatorError):
        parse_locator(bad)


def test_line_family_does_not_swallow_code_locator():
    """`L12.code401` 属于 JSON 家族的历史写法，不能被行号族误认。"""
    assert parse_locator("L12.code401").family == "json"


def test_line_writer_and_locator_agree():
    """行号写回器自带的识别正则必须与 `parse_locator` 判断一致。

    两处各写一份正则（写回器刻意不依赖兄弟模块），所以要有测试盯着它们别漂移。
    """
    from paratranz_agent.writeback.line import LineWriter

    writer = LineWriter()
    for locator in ("L1", "L123", "L7#0", "L7#3", "L12.code401", "[3].name", "gameTitle", "#3", "L"):
        expect = False
        try:
            expect = parse_locator(locator).family == "line"
        except LocatorError:
            expect = False
        assert writer.can_handle(locator) is expect, f"{locator} 两边判断不一致"


# --------------------------------------------------------------- 后端工具闸门
def test_tool_writeback_requires_accept_risk_for_in_place(client, fake, game, tmp_path):
    """防回归：原地改游戏文件的后端硬拦。

    前端弹窗 + 5 秒倒计时只是提醒，改 DOM 就能绕过；
    真正的闸门是这里的 `accept_risk`，缺了必须抛错而不是「照写不误」。
    """
    from types import SimpleNamespace

    from paratranz_agent.server.tools import tool_writeback

    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    ctx = SimpleNamespace(client=client, state_dir=tmp_path)

    with pytest.raises(PermissionError, match="accept_risk"):
        tool_writeback(ctx, extraction_id=job, in_place=True, accept_risk=False, dry_run=False)
    # 原文件必须没被动过
    assert json.loads((game / "data" / "x.json").read_text(encoding="utf-8"))["0"]["name"] == "Alice"

    ok = tool_writeback(ctx, extraction_id=job, in_place=True, accept_risk=True, dry_run=False)
    assert ok["written"] == 1
    assert ok["backup_dir"], "原地写必须给出备份目录"
    # 报告要落盘，界面刷新后还能看到
    last = json.loads((tmp_path / "writeback" / job / "last.json").read_text(encoding="utf-8"))
    assert last["report"]["written"] == 1 and last["report"]["in_place"] is True


def test_tool_writeback_dry_run_does_not_need_risk(client, fake, game, tmp_path):
    """仅预览不落盘，不需要风险确认。"""
    from types import SimpleNamespace

    from paratranz_agent.server.tools import tool_writeback

    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    ctx = SimpleNamespace(client=client, state_dir=tmp_path)
    rep = tool_writeback(ctx, extraction_id=job, in_place=True, accept_risk=False, dry_run=True)
    assert rep["written"] == 1 and rep["dry_run"] is True
    assert json.loads((game / "data" / "x.json").read_text(encoding="utf-8"))["0"]["name"] == "Alice"


# --------------------------------------------------------------- JSON 扫描
def test_scan_records_spans_and_decoded_values():
    src = '{"a":{"b":[{"name":"Hello"}]},"n":1,"t":true,"z":null}'
    spans = scan_strings(src)
    assert spans[("a", "b", 0, "name")][2] == "Hello"
    # 键本身不当作可写值
    assert ("a",) not in spans


def test_scan_handles_escapes():
    src = '{"k":"a\\"b\\\\c\\nd\\u4e2d"}'
    spans = scan_strings(src)
    assert spans[("k",)][2] == 'a"b\\c\nd中'


# --------------------------------------------------------------- JSON 写回
def _write(src, items):
    return JsonPathWriter().apply(src, items)


def test_write_preserves_formatting_exactly():
    """只替换字面量内容：缩进、键序、其它字段一个字节都不许动。"""
    src = '{\n  "a": "Hello there",\n  "b": "keep me",\n  "c": 42\n}\n'
    res = _write(src, [WriteItem(key="f.json::a", translation="你好", original="Hello there")])
    assert res.written == ["f.json::a"]
    assert res.content == src.replace('"Hello there"', '"你好"')
    assert res.content.endswith("}\n")


def test_write_skips_when_original_mismatched():
    """文件里的原文和平台记录不一致 → 不写，报 mismatched（错位写入的唯一防线）。"""
    src = '{"a": "改了的内容"}'
    res = _write(src, [WriteItem(key="f.json::a", translation="你好", original="Hello there")])
    assert res.written == []
    assert res.mismatched and "原文不一致" in res.mismatched[0][1]
    assert res.content == src


def test_write_is_idempotent():
    """重复回写：文件里已经是目标译文时算成功，不再改也不报错。"""
    src = '{"a": "你好"}'
    res = _write(src, [WriteItem(key="f.json::a", translation="你好", original="Hello there")])
    assert res.written == ["f.json::a"]
    assert res.mismatched == []
    assert res.content == src


def test_write_missing_path():
    src = '{"a": "x"}'
    res = _write(src, [WriteItem(key="f.json::nope.deeper", translation="y", original="x")])
    assert res.missing == ["f.json::nope.deeper"]
    assert res.content == src


def test_write_escapes_translation():
    src = '{"a": "old"}'
    res = _write(src, [WriteItem(key="f.json::a", translation='有"引号"与\\反斜杠\n换行', original="old")])
    assert res.content == '{"a": "有\\"引号\\"与\\\\反斜杠\\n换行"}'
    # 转义后仍是合法 JSON，且解回来等于原文
    assert json.loads(res.content)["a"] == '有"引号"与\\反斜杠\n换行'


def test_json_escape_control_chars():
    assert json_escape("\x01") == "\\u0001"
    assert json_escape("a\tb") == "a\\tb"


def test_write_marks_bad_json_as_error_not_partial():
    """非法 JSON：整份文件一律不写，全部报错误（绝不部分写入）。"""
    res = _write('{"a": "old"', [WriteItem(key="f.json::a", translation="新", original="old")])
    assert res.written == []
    assert res.errors and "不是合法 JSON" in res.errors[0][1]


def test_write_multiple_keys_one_pass():
    src = '{"a":"one","b":"two"}'
    res = _write(
        src,
        [
            WriteItem(key="f.json::a", translation="壹", original="one"),
            WriteItem(key="f.json::b", translation="贰", original="two"),
        ],
    )
    assert res.written == ["f.json::a", "f.json::b"]
    assert json.loads(res.content) == {"a": "壹", "b": "贰"}


# --------------------------------------------------------------- 端到端管线
GAME_JSON = {
    "0": {"name": "Alice", "id": 1},
    "1": {"name": "Bob", "id": 2},
}


def _make_job(state_dir: Path, game_dir: Path, keys: list[tuple[str, str]], *, options=None) -> str:
    """按真实落盘结构伪造一条提取记录（meta.json + entries.jsonl）。"""
    job_id = "20260101-000000-abcdef"
    jdir = state_dir / "extractions" / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / "meta.json").write_text(
        json.dumps(
            {
                "extractor": {"id": "generic-json", "name": "通用 JSON", "args": []},
                "job_id": job_id,
                "extractor_id": "generic-json",
                "game_dir": str(game_dir),
                "status": "done",
                "entries": len(keys),
                "files_scanned": 1,
                "duration_ms": 5,
                "error": "",
                "warnings": [],
                "options": options or {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with (jdir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for key, original in keys:
            f.write(json.dumps({"key": key, "original": original, "context": "data/x.json"},
                               ensure_ascii=False) + "\n")
    return job_id


def _seed_platform(fake, rows: list[dict]) -> None:
    """把词条塞进假平台（stage=3 已检查）。"""
    fake.state["strings"] = [
        {
            "id": 900000 + i,
            "key": r["key"],
            "original": r["original"],
            "translation": r.get("translation", ""),
            "stage": r.get("stage", 3),
            "fileId": 101,
            "context": None,
        }
        for i, r in enumerate(rows)
    ]


@pytest.fixture
def game(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    (root / "data").mkdir(parents=True)
    (root / "data" / "x.json").write_text(
        json.dumps(GAME_JSON, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def test_pipeline_writes_to_sibling_dir_by_default(client, fake, game, tmp_path):
    """默认整包镜像：目标目录是一份完整可跑的游戏。

    未改动的文件原样复制过去，改动的文件直接写改写版（不先复制再覆盖）。
    """
    (game / "js").mkdir()
    (game / "js" / "game.js").write_text("// untouched\n", encoding="utf-8")
    (game / "img").mkdir()
    (game / "img" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary")

    job = _make_job(
        tmp_path,
        game,
        [("data/x.json::0.name", "Alice"), ("data/x.json::1.name", "Bob")],
    )
    _seed_platform(
        fake,
        [
            {"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"},
            {"key": "data/x.json::1.name", "original": "Bob", "translation": "鲍勃"},
        ],
    )
    before = (game / "data" / "x.json").read_text(encoding="utf-8")

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)

    assert rep.mode == "mirror"
    assert rep.written == 2
    assert rep.files == 1
    assert rep.out_dir.endswith("game_cn")
    out = Path(rep.out_dir)
    assert json.loads((out / "data" / "x.json").read_text(encoding="utf-8"))["0"]["name"] == "爱丽丝"
    # 未改动的文件也要在目标目录里（整包镜像的意义）
    assert (out / "js" / "game.js").read_text(encoding="utf-8") == "// untouched\n"
    assert (out / "img" / "logo.png").read_bytes() == b"\x89PNG\r\n\x1a\n binary"
    assert rep.copied == 2, "两个未改动文件应被复制"
    # 原游戏文件必须一个字节都没变
    assert (game / "data" / "x.json").read_text(encoding="utf-8") == before


def test_pipeline_patch_mode_only_outputs_changed_files(client, fake, game, tmp_path):
    """patch 模式：只放被改动的文件，体积最小。"""
    (game / "js").mkdir()
    (game / "js" / "game.js").write_text("// untouched\n", encoding="utf-8")
    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False, mode="patch")

    out = Path(rep.out_dir)
    assert rep.copied == 0
    assert (out / "data" / "x.json").is_file()
    assert not (out / "js" / "game.js").exists(), "patch 模式不该复制未改动文件"


def test_mirror_second_run_syncs_source_updates(client, fake, game, tmp_path):
    """防回归：重复镜像必须把源文件的更新同步过去。

    早期实现「目标已存在就跳过」，结果第二轮回写时镜像里永远是最旧的副本
    —— 而多轮回写是常态（每翻完一批就回写一次）。
    """
    import os

    js = game / "js"
    js.mkdir()
    src = js / "game.js"
    src.write_text("// v1\n", encoding="utf-8")

    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])

    rep1 = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)
    out = Path(rep1.out_dir)
    assert (out / "js" / "game.js").read_text(encoding="utf-8") == "// v1\n"
    assert rep1.copied == 1

    # 源文件更新（内容与 mtime 都变）
    src.write_text("// v2 changed\n", encoding="utf-8")
    stamp = src.stat().st_mtime + 5
    os.utime(src, (stamp, stamp))

    rep2 = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)
    assert (out / "js" / "game.js").read_text(encoding="utf-8") == "// v2 changed\n", (
        "第二次镜像没把源文件更新同步过去"
    )
    assert rep2.copied == 1


def test_mirror_skips_unchanged_files_on_repeat(client, fake, game, tmp_path):
    """未变化的文件走「大小+mtime 一致」快速路径，不重复写盘。"""
    js = game / "js"
    js.mkdir()
    (js / "big.js").write_text("// stable\n", encoding="utf-8")

    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)

    lines: list[str] = []
    rep2 = run_writeback(
        client=client, state_dir=tmp_path, job_id=job, dry_run=False, log=lines.append
    )
    assert rep2.copied == 0, "第二次不该重写未变化文件"
    assert any("未变化" in ln for ln in lines), f"应有未变化计数，实际日志：{lines}"


def test_pipeline_rejects_out_dir_inside_game(client, fake, game, tmp_path):
    """输出目录不能在游戏目录里——镜像会把自己当素材，目录翻倍甚至递归写满磁盘。"""
    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    with pytest.raises(WritebackError, match="里面"):
        run_writeback(
            client=client, state_dir=tmp_path, job_id=job,
            out_dir=game / "cn", dry_run=False,
        )
    with pytest.raises(WritebackError, match="本身"):
        run_writeback(
            client=client, state_dir=tmp_path, job_id=job,
            out_dir=game, dry_run=False,
        )
    assert not (game / "cn").exists()


def test_pipeline_dry_run_touches_nothing(client, fake, game, tmp_path):
    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=True)
    assert rep.written == 1
    assert not Path(rep.out_dir).exists(), "预览模式不得创建输出目录"
    assert (game / "data" / "x.json").read_text(encoding="utf-8") == json.dumps(
        GAME_JSON, ensure_ascii=False, indent=2
    )


def test_pipeline_in_place_backs_up_first(client, fake, game, tmp_path):
    """原地写必须先把原文件备份到 state 目录，并留下 manifest。"""
    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    original = (game / "data" / "x.json").read_text(encoding="utf-8")

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, in_place=True, dry_run=False)

    assert json.loads((game / "data" / "x.json").read_text(encoding="utf-8"))["0"]["name"] == "爱丽丝"
    bak = Path(rep.backup_dir) / "data" / "x.json"
    assert bak.read_text(encoding="utf-8") == original, "备份必须是改动前的原文"
    manifest = json.loads((Path(rep.backup_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert "data/x.json" in manifest and manifest["data/x.json"]["sha256"]


def test_pipeline_respects_min_stage(client, fake, game, tmp_path):
    """门槛 3：只有 stage=1 的译文不能被写回。"""
    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(
        fake,
        [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝", "stage": 1}],
    )
    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, min_stage=3, dry_run=False)
    assert rep.pulled == 0
    assert rep.written == 0
    assert rep.skipped_stage == 1

    rep1 = run_writeback(client=client, state_dir=tmp_path, job_id=job, min_stage=1, dry_run=False)
    assert rep1.written == 1


def test_pipeline_reports_mismatch_and_keeps_file(client, fake, game, tmp_path):
    """游戏本体更新过：文件里的原文已经不是提取时那句 → 不写、报 mismatched。

    这是防错位写入的主防线：提取时记录 Alice，平台也记 Alice，
    但玩家/作者已经把文件改成了中文旧版，此时按路径硬写会覆盖掉别人的成果。
    """
    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    changed = json.dumps({"0": {"name": "旧的中文名", "id": 1}, "1": {"name": "Bob", "id": 2}},
                         ensure_ascii=False, indent=2)
    (game / "data" / "x.json").write_text(changed, encoding="utf-8")

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)

    assert rep.mismatched == 1
    assert rep.written == 0
    assert rep.details and rep.details[0]["kind"] == "mismatched"
    assert (game / "data" / "x.json").read_text(encoding="utf-8") == changed, "不能改动文件"


def test_pipeline_errors_when_game_dir_gone(client, fake, game, tmp_path):
    import shutil

    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    shutil.rmtree(game)
    with pytest.raises(WritebackError, match="游戏目录已不存在"):
        run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=True)


def test_pipeline_counts_untracked_keys(client, fake, game, tmp_path):
    """提取记录里有、平台上没有的 key：计入 skipped_untracked，不算失败。"""
    job = _make_job(
        tmp_path, game,
        [("data/x.json::0.name", "Alice"), ("data/x.json::1.name", "Bob")],
    )
    _seed_platform(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])
    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)
    assert rep.written == 1
    assert rep.skipped_untracked == 1


def test_pull_translations_keeps_locked_stage(client, fake):
    """stage=9（已锁定）比任何门槛都终局，应视为可回写。"""
    _seed_platform(
        fake,
        [
            {"key": "k_locked", "original": "A", "translation": "甲", "stage": 9},
            {"key": "k_low", "original": "B", "translation": "乙", "stage": 1},
        ],
    )
    got, stats = pull_translations(client, min_stage=5)
    assert "k_locked" in got
    assert "k_low" not in got
    assert stats["skipped_stage"] == 1


def test_pipeline_routes_line_family_to_line_writer(client, fake, tmp_path):
    """行号型 key（generic-regex 提取器产出）要路由到行号写回器。

    行号写回器必须拿到提取器当时的 `pattern`/`group` 才能还原该行哪一段是译文，
    所以 `ExtractionResult.options` 必须落盘并在回写时传下去。
    """
    game = tmp_path / "game"
    (game / "cfg").mkdir(parents=True)
    text = 'title = "Farm Story"\nname = "Alice"\n'
    (game / "cfg" / "ui.txt").write_text(text, encoding="utf-8")

    job = _make_job(
        tmp_path,
        game,
        [("cfg/ui.txt::L1", "Farm Story"), ("cfg/ui.txt::L2", "Alice")],
        options={"pattern": '"([^"]{2,})"', "group": "1"},
    )
    _seed_platform(
        fake,
        [
            {"key": "cfg/ui.txt::L1", "original": "Farm Story", "translation": "农场物语"},
            {"key": "cfg/ui.txt::L2", "original": "Alice", "translation": "爱丽丝"},
        ],
    )

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)

    assert rep.unsupported == 0, "行号型 key 必须有写回器认"
    assert rep.written == 2
    out = Path(rep.out_dir) / "cfg" / "ui.txt"
    assert out.read_text(encoding="utf-8") == 'title = "农场物语"\nname = "爱丽丝"\n'
