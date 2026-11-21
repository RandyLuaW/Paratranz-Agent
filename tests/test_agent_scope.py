"""Agent 指令里的「范围」必须落到工具参数上（真实事故的回归测试）。

事故（2026-11-22，v0.3.3）：用户在概览页输入

    把项目目录 overworld-town/loc-adultshop/ 下所有文件的待翻译词条执行翻译

结果软件取了 200 条、写入 192 条，**全部来自别的目录**（overworld-town 86、
base-combat 52、base-system 33、overworld-underground 15、base-debug 6），
目标目录一条都没有。两个原因：

1. `server/agent.py` 的规则解析只抽 `limit` / `dry_run`，**从不抽路径**，
   而 `translate` 当时也没有「只处理这些文件」这个参数 —— 指令里的范围被整个丢掉；
2. 于是工具按默认上限（`per_run_limit=200`）从全项目取材并真实写回。

这里锁住三件事：路径能被抽出来、它必须进 `files`、抽不出来时要停手而不是全项目跑。
"""
from __future__ import annotations

import pytest

from paratranz_agent.pipelines.translate import TranslatePipeline
from paratranz_agent.server.agent import _extract_args, _path_tokens, plan_actions

TOOLS = [
    {"name": "translate", "description": "翻译", "params": {"limit": "条数", "files": "范围"}},
    {"name": "review", "description": "校对", "params": {"limit": "条数", "files": "范围"}},
    {"name": "run", "description": "全流程", "params": {}},
    {"name": "status", "description": "状态", "params": {}},
]


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("把项目目录overworld-town/loc-adultshop/下所有文件的待翻译词条执行翻译",
         ["overworld-town/loc-adultshop/"]),
        ("只翻 overworld-town/loc-adultshop 下的", ["overworld-town/loc-adultshop"]),
        (r"译 data\Actors.json", ["data/Actors.json"]),
        ("翻 100 条", []),
        ("全流程跑一遍", []),
        ("把 0.5.11.9 版本更新日志翻了", []),      # 版本号不是路径
    ],
)
def test_path_tokens(text, expect):
    assert _path_tokens(text) == expect


def test_extract_args_puts_scope_into_files():
    args = _extract_args("把项目目录overworld-town/loc-adultshop/下所有文件的待翻译词条执行翻译",
                         "translate")
    assert args["files"] == ["overworld-town/loc-adultshop/"]
    # 目录类指令没有数字时不该凭空造 limit
    assert "limit" not in args


def test_plan_shows_scope_in_reply():
    got = plan_actions("把 overworld-town/loc-adultshop/ 下的都翻了", TOOLS)
    assert got["source"] == "rule"
    assert "overworld-town/loc-adultshop" in got["reply"]
    assert got["steps"][0]["args"]["files"] == ["overworld-town/loc-adultshop/"]


def test_plan_blocks_when_scope_cannot_be_mapped(monkeypatch):
    """安全网：话里有范围、但参数里没有 → 停手，绝不退化成全项目。"""
    import paratranz_agent.server.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_extract_args", lambda text, tool: {})
    got = agent_mod.plan_actions("把 overworld-town/loc-adultshop/ 下的都翻了", TOOLS)
    assert got["source"] == "guard"
    assert got["steps"] == []
    assert "范围" in got["reply"]


# --------------------------------------------------------------- 工具层范围
def test_translate_only_touches_requested_directory(client, cfg, store, llm):
    """`files` 生效：只取该目录下的条目（假平台里 www/quest 有 6 条未翻译）。"""
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=True, files=["www/quest"])
    assert stats.files_unmatched == 0
    assert stats.fetched == 6
    seen: list[dict] = []
    pipe.run(dry_run=True, files=["www/quest"], on_items=lambda rows: seen.extend(rows))
    # 假平台里 Map001 13 条、Items 8 条，quest 的 q1..q6 落在 1021..1026
    assert {r["id"] for r in seen} == {1000 + 21 + i for i in range(6)}


def test_translate_unmatched_scope_returns_nothing(client, cfg, store, llm):
    """范围写错时不能"悄悄退化成全项目"，只能什么都不取并置 files_unmatched。"""
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=True, files=["no/such/dir"])
    assert stats.fetched == 0
    assert stats.files_unmatched == 1


def test_scope_survives_single_file_match(client, cfg, store, llm):
    """单文件精确匹配也要走同一条路（大小写、反斜杠由 matches_any 归一）。"""
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=True, files=[r"www\data\Items.json"])
    assert stats.files_unmatched == 0
    assert stats.fetched == 8
