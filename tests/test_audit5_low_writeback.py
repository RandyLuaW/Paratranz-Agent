"""审计报告5 里回写子系统的 5 条缺陷回归测试（第 6 条是前端文案，不在这里）。

这五条的坏法都不是「报错」，而是**结果不对还一片绿**：
报告说写了 2 条实际只落地 1 条、白名单形同虚设、key 能读写游戏目录之外、
一个畸形文件炸掉整轮回写、同一秒的两次回写共用一份还原点。
所以每条都用断言钉住「报告里必须有这个事实」，不能靠「跑通了」判断。
全部离线，走纯函数式调用（`apply_json` / `run_writeback`）+ `tmp_path`。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.writeback import WriteItem, apply_json, run_writeback  # noqa: E402
from paratranz_agent.writeback import pipeline  # noqa: E402


# --------------------------------------------------------------- 公共夹具
def _make_job(state_dir: Path, game_dir: Path, keys: list[tuple[str, str]]) -> str:
    """按真实落盘结构伪造一条提取记录（meta.json + entries.jsonl）。"""
    job_id = "20261024-001530-wbaudit"
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
                "options": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with (jdir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for key, original in keys:
            f.write(json.dumps({"key": key, "original": original, "context": ""},
                               ensure_ascii=False) + "\n")
    return job_id


def _seed(fake, rows: list[dict]) -> None:
    """把词条塞进假平台（默认 stage=3 已检查）。"""
    fake.state["strings"] = [
        {
            "id": 800000 + i,
            "key": r["key"],
            "original": r["original"],
            "translation": r.get("translation", ""),
            "stage": r.get("stage", 3),
            "fileId": 101,
            "context": None,
        }
        for i, r in enumerate(rows)
    ]


# ------------------------------------------------- 1. 同一 key 出现两次
def test_duplicate_key_reports_overlap_instead_of_success():
    """原来的坏法：同一个 key 出现两次，两条定位符指向同一个字面量。

    `written` 在挑选阶段就记了两次成功，`_splice` 遇到区间重叠却只 `continue`
    扔掉后一条 —— 报告说「写入 2 条」，文件里只改了 1 条，还看不出是哪条没写。
    行号族（`line._splice`）一直是记 errors 的，这里必须对齐。
    """
    res = apply_json(
        '{"a":"x"}',
        [
            WriteItem(key="f.json::a", translation="壹", original="x"),
            WriteItem(key="f.json::a", translation="贰", original="x"),
        ],
    )
    assert len(res.written) == 1, res.written
    assert len(res.errors) == 1, res.errors
    key, why = res.errors[0]
    assert key == "f.json::a"
    assert why == "与另一条译文的位置重叠，已跳过"
    # 先到的那条落地，后到的不许悄悄改内容
    assert json.loads(res.content) == {"a": "壹"}


def test_duplicate_key_error_does_not_disturb_other_entries():
    """被重叠挡掉的只有那一条：同批次里其它 key 照常写入，顺序仍按入参回报。"""
    src = '{"a":"x","b":"y"}'
    res = apply_json(
        src,
        [
            WriteItem(key="f.json::a", translation="壹", original="x"),
            WriteItem(key="f.json::b", translation="贰", original="y"),
            WriteItem(key="f.json::a", translation="叁", original="x"),
        ],
    )
    assert res.written == ["f.json::a", "f.json::b"]
    assert [k for k, _ in res.errors] == ["f.json::a"]
    assert json.loads(res.content) == {"a": "壹", "b": "贰"}


# ------------------------------------------------- 2. only_files
def test_only_files_matches_path_segment_not_substring(client, fake, tmp_path):
    """原来的坏法：`any(a in rel for a in wanted)` 是**子串**匹配。

    `only_files=["a"]` 于是命中 `data/x.json`、`abc/y.json` —— 每个相对路径都
    含字母 a。用户以为只改一个文件，实际白名单等于失效，全项目都被写了。
    现在只认「路径段精确相等」或「以 `a/` 为前缀」。
    """
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "abc").mkdir(parents=True)
    (game / "data" / "x.json").write_text('{"a": "Alice"}', encoding="utf-8")
    (game / "abc" / "y.json").write_text('{"a": "Bob"}', encoding="utf-8")
    job = _make_job(
        tmp_path, game,
        [("data/x.json::a", "Alice"), ("abc/y.json::a", "Bob")],
    )
    _seed(
        fake,
        [
            {"key": "data/x.json::a", "original": "Alice", "translation": "爱丽丝"},
            {"key": "abc/y.json::a", "original": "Bob", "translation": "鲍勃"},
        ],
    )

    lines: list[str] = []
    none = run_writeback(
        client=client, state_dir=tmp_path, job_id=job,
        only_files=["a"], dry_run=True, log=lines.append,
    )
    assert none.written == 0, "子串 'a' 不该让所有文件都命中"
    assert none.files_unmatched == 2, "两个相对路径都该被记成「被 only_files 挡掉」"

    # 前缀匹配：目录名照常能选中整棵子树
    by_dir = run_writeback(
        client=client, state_dir=tmp_path, job_id=job, only_files=["data"], dry_run=True
    )
    assert by_dir.written == 1
    assert by_dir.files_unmatched == 1

    # 精确匹配：单独点一个文件
    exact = run_writeback(
        client=client, state_dir=tmp_path, job_id=job,
        only_files=["abc/y.json"], dry_run=True,
    )
    assert exact.written == 1
    assert exact.files_unmatched == 1


def test_only_files_zero_match_is_reported_not_silent(client, fake, tmp_path):
    """原来的坏法：白名单一个都没匹配上时静默 `continue`，最后报「0 写入」。

    「筛错了文件」和「平台没译文」在日志与报告里长得一模一样，用户会以为
    回写已经生效。现在报告里有 `files_unmatched`，日志里有一句明说。
    """
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "x.json").write_text('{"a": "Alice"}', encoding="utf-8")
    job = _make_job(tmp_path, game, [("data/x.json::a", "Alice")])
    _seed(fake, [{"key": "data/x.json::a", "original": "Alice", "translation": "爱丽丝"}])

    lines: list[str] = []
    rep = run_writeback(
        client=client, state_dir=tmp_path, job_id=job,
        only_files=["nope"], dry_run=False, log=lines.append,
    )

    assert rep.written == 0
    assert rep.files_unmatched == 1
    assert rep.as_dict()["files_unmatched"] == 1, "报告结构里必须有这个字段"
    assert any("only_files 没有匹配任何文件" in ln for ln in lines), lines
    assert not (tmp_path / "game_cn").exists(), "一条都没匹配，不该建默认输出目录"


# ------------------------------------------------- 3. rel 越界
@pytest.mark.parametrize(
    "rel",
    ["../outside.json", "..", "a/../../b.json", "/abs/x.json", "\\abs\\x.json",
     "C:/x/y.json", "C:y.json", "data/x.json:ads", ""],
)
def test_safe_rel_rejects_escaping_paths(rel):
    """畸形相对路径一律拒绝：绝对路径、`..` 路径段、盘符、NTFS 数据流。"""
    assert pipeline._safe_rel(rel) is False


@pytest.mark.parametrize(
    "rel", ["data/Actors.json", "x.json", "a/b/c.txt", "data/..x.json", "./x.json"]
)
def test_safe_rel_accepts_normal_keys(rel):
    """正常 key 的路径必须照常通过（`data/..x.json` 只是个拗口的文件名）。"""
    assert pipeline._safe_rel(rel) is True


def test_dotdot_key_cannot_write_outside_output_dir(client, fake, tmp_path):
    """原来的坏法：`rel` 不做任何校验就拼到目录上。

    entries 里放一条 `../outside.json::k`，patch 模式的 `target_root / rel` 直接
    跳到输出目录外面 —— key 指向哪儿就往哪儿写，能覆盖游戏目录之外的任意文件。
    现在这类条目记 unsupported 明细、绝不落盘，同批次的正常文件照常写。
    """
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "x.json").write_text('{"0": {"name": "Alice"}}', encoding="utf-8")
    job = _make_job(
        tmp_path, game,
        [("../outside.json::k", "Alice"), ("data/x.json::0.name", "Alice")],
    )
    _seed(
        fake,
        [
            {"key": "../outside.json::k", "original": "Alice", "translation": "外面"},
            {"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"},
        ],
    )

    rep = run_writeback(
        client=client, state_dir=tmp_path, job_id=job, mode="patch", dry_run=False
    )

    assert rep.unsupported == 1
    assert rep.written == 1, "正经文件不能因为邻居畸形就不写"
    assert any(
        d["kind"] == "unsupported" and d["key"] == "../outside.json::k" for d in rep.details
    ), rep.details
    assert not (tmp_path / "outside.json").exists(), "越界 key 写出了输出目录"
    assert (Path(rep.out_dir) / "data" / "x.json").is_file()


# ------------------------------------------------- 4. 深嵌套 RecursionError
def test_deep_nesting_is_a_file_error_not_a_crash():
    """原来的坏法：`apply_json` 只 `except JsonScanError`。

    递归下降扫描器吃一层嵌套两帧栈，900 层的文件就会抛 `RecursionError`，
    而它穿透 `apply_items` 一路炸到 pipeline，**整轮回写**崩掉 —— 已经写完的
    文件连报告都没有。现在按单文件失败处理：全部条目记 errors，内容原样返回。
    """
    deep = "[" * 5000 + "]" * 5000
    res = apply_json(deep, [WriteItem(key="f.json::a", translation="壹", original="x")])

    assert res.written == []
    assert res.content == deep, "扫不动就必须一个字节都不动"
    assert res.errors and res.errors[0][0] == "f.json::a"
    assert res.errors[0][1] == "文件嵌套过深，扫描器无法处理（建议改用行号族提取器）"


def test_deep_nesting_fails_single_file_and_round_continues(client, fake, tmp_path):
    """端到端：一个坏文件不能让整轮回写崩掉，其它文件照常写并出报告。"""
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    deep = "[" * 5000 + "]" * 5000
    (game / "data" / "deep.json").write_text(deep, encoding="utf-8")
    (game / "data" / "ok.json").write_text('{"a": "Alice"}', encoding="utf-8")
    job = _make_job(
        tmp_path, game,
        [("data/deep.json::a", "x"), ("data/ok.json::a", "Alice")],
    )
    _seed(
        fake,
        [
            {"key": "data/deep.json::a", "original": "x", "translation": "壹"},
            {"key": "data/ok.json::a", "original": "Alice", "translation": "爱丽丝"},
        ],
    )

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=True)

    assert rep.written == 1, "好文件必须照常写"
    assert any("嵌套过深" in msg for msg in rep.errors), rep.errors
    assert not Path(rep.out_dir).exists()


# ------------------------------------------------- 5. 备份目录撞名与保留数
def test_backup_dir_name_carries_unique_suffix(tmp_path):
    """原来的坏法：目录名只有秒级时间戳 `backup-%Y%m%d-%H%M%S`。"""
    root = pipeline._backup_root(tmp_path, "job-1")
    assert re.fullmatch(r"backup-\d{8}-\d{6}-[0-9a-f]{6}", root.name), root.name


def test_two_backup_roots_never_collide_in_one_second(tmp_path):
    """同一秒内的两次回写必须拿到两个不同目录，否则第二份还原点覆盖第一份。"""
    a = pipeline._backup_root(tmp_path, "job-1")
    b = pipeline._backup_root(tmp_path, "job-1")
    assert a != b


def test_in_place_two_runs_keep_two_restore_points(client, fake, tmp_path):
    """端到端：连跑两轮原地回写，两份还原点都在，各自留着改动前的原文。"""
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    original = '{"0": {"name": "Alice"}}'
    src = game / "data" / "x.json"
    src.write_text(original, encoding="utf-8")
    job = _make_job(tmp_path, game, [("data/x.json::0.name", "Alice")])
    _seed(fake, [{"key": "data/x.json::0.name", "original": "Alice", "translation": "爱丽丝"}])

    rep1 = run_writeback(
        client=client, state_dir=tmp_path, job_id=job, in_place=True, dry_run=False
    )
    src.write_text(original, encoding="utf-8")  # 模拟同一秒内的第二轮
    rep2 = run_writeback(
        client=client, state_dir=tmp_path, job_id=job, in_place=True, dry_run=False
    )

    assert rep1.backup_dir != rep2.backup_dir, "两轮回写撞了同一个备份目录"
    for d in (rep1.backup_dir, rep2.backup_dir):
        assert (Path(d) / "data" / "x.json").read_text(encoding="utf-8") == original
        assert (Path(d) / "manifest.json").is_file()


def test_backup_retention_keeps_latest_five(tmp_path):
    """原来的坏法：备份目录只增不减，原地回写几次就涨成几个 G 的还原点。"""
    base = tmp_path / "writeback" / "job-1"
    older = [f"backup-2020010{i}-000000-aaaaaa" for i in range(1, 8)]  # 7 份更早的
    for name in older:
        (base / name).mkdir(parents=True)
        (base / name / "data").mkdir()
        (base / name / "data" / "x.json").write_text("{}", encoding="utf-8")

    current = pipeline._backup_root(tmp_path, "job-1")
    assert not current.exists(), "这里只算路径，不该顺手建目录"
    current.mkdir(parents=True)  # 回写时由 `_write_backup` 真正建出来

    left = sorted(p.name for p in base.iterdir() if p.is_dir())
    assert len(left) == 5, left
    assert current.name in left
    assert older[:3] == [n for n in older if n not in left], "应删掉最旧的 3 份"
    assert older[-1] in left, "最近的一份必须留着"


def test_backup_prune_failure_does_not_break_writeback(tmp_path, monkeypatch):
    """清理旧备份必须**尝试**做，而且失败了只能忍住。

    只增不减治不了旧备份堆积，所以每轮都要清理；但多留一份只是占空间，
    让整轮回写因为清理失败而失败不可接受。
    """
    tried: list[Path] = []

    def boom(path, *_args, **_kwargs):
        tried.append(Path(path))
        raise OSError("拒绝访问")

    base = tmp_path / "writeback" / "job-1"
    for i in range(1, 9):
        (base / f"backup-2020010{i}-000000-aaaaaa").mkdir(parents=True)
    monkeypatch.setattr(pipeline.shutil, "rmtree", boom)

    root = pipeline._backup_root(tmp_path, "job-1")
    assert tried, "旧备份要被清理，不能只增不减"
    assert root.name.startswith("backup-"), "清理失败也得照常给出本次备份目录"
