"""审计报告5「低级缺陷」回归：QA 术语噪音、批量写回报表、取材截断、schema 迁移。

四条缺陷的共同点：不抛异常、不崩溃，只是**报表/判定在说假话** ——
- 术语子串匹配把 WARN 刷满屏，真问题被埋掉；
- 平台回 `{"update": N}` 时报表写「写回 0 条」，而 stage 早已改完；
- 词条超过 2 万条时静默截断，看起来像「全处理完了」；
- 老库缺列时不迁移，升级后整条流水线以 `no column named pos` 失败。

每条用例的 docstring 都写明「原来的坏法」，改动一旦被回退，断言会立刻红。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from paratranz_agent.client import BatchWrite, ParaTranzError, _edit_result
from paratranz_agent.fake import FakeParaTranz
from paratranz_agent.pipelines import review as review_mod
from paratranz_agent.pipelines import translate as translate_mod
from paratranz_agent.pipelines.review import ReviewPipeline
from paratranz_agent.pipelines.translate import TranslatePipeline, TranslateStats
from paratranz_agent.qa import GENERIC_TOKENS, check_placeholders, check_terms
from paratranz_agent.store import SCHEMA_VERSION, Store


# =============================================================== 1. 术语词边界
def test_short_ascii_terms_do_not_fire_inside_words():
    """原来的坏法：`hit = any(v in src for v in variants)` 是纯子串匹配。

    术语表里只要有 `art` / `he` / `a` 这类短词，"Start the game" 会因为 `art`
    命中 `Start`、"The heart is red" 会因为 `he` 命中 `The` 和 `heart`，
    一条原文刷出好几条 term_violation，真正的错译反而看不见。
    """
    terms = {
        "art": {"translation": "美术"},
        "he": {"translation": "他"},
        "a": {"translation": "一个"},
    }
    for src in ("Start the game", "The heart is red"):
        assert check_terms(src, "随便的译文", terms) == [], f"{src!r} 不该命中任何短术语"

    # 真正的整词出现当然还是要报（否则就是把规则改瞎了）
    hits = check_terms("Start the game", "开始吧", {"game": {"translation": "游戏"}})
    assert [i.rule for i in hits] == ["term_violation"]


def test_ascii_term_hit_is_case_insensitive():
    """原来的坏法：`v in src` 区分大小写，术语表写 `Heart` 时连 `heart is red`
    都命中不了 —— 该报的漏报（与短术语的误报同时存在）。"""
    terms = {"Heart": {"translation": "心脏"}}
    assert [i.rule for i in check_terms("The heart is red", "红色", terms)] == ["term_violation"]
    assert [i.rule for i in check_terms("Heart rate", "心率", terms)] == ["term_violation"]
    assert check_terms("The heart is red", "心脏是红色的", terms) == []
    # 边界只管 ASCII：`he` 不能命中 `heart`，但中文紧邻时仍算命中
    assert check_terms("heartbeat", "心跳", {"he": {"translation": "他"}}) == []
    assert [i.rule for i in check_terms("用he造一句", "有人说话", {"he": {"translation": "他"}})] == [
        "term_violation"
    ]


def test_chinese_term_still_matches_by_substring():
    """中文术语没有词边界，改动后必须继续按子串命中（别为了英文边界把中文弄丢）。"""
    terms = {"生命值": {"translation": "HP"}}
    hits = check_terms("最大生命值提升", "最大值提升", terms)
    assert [i.rule for i in hits] == ["term_violation"]
    assert check_terms("最大生命值提升", "HP 上限提升", terms) == []


# ======================================================= 2. `[tag]` 占位符保护
def test_single_bracket_tag_is_protected():
    """原来的坏法：注释写着「通用占位符包含 `[tag]`」，正则却只保护 `[[…]]`。

    Unity 富文本 / Yarn 的 `[wave]`、`[/wave]` 被译文丢掉时不报任何错，
    线上就是渲染错乱或字面量泄漏。
    """
    assert check_placeholders("[wave]Hello[/wave]", "[wave]你好[/wave]") == []
    lost = check_placeholders("[wave]Hello[/wave]", "你好")
    assert [i.rule for i in lost] == ["placeholder_missing_generic"]
    assert "lost" not in lost[0].message  # 报的是真的丢标签，不是原文里的单词


def test_double_bracket_link_is_one_token():
    """`[[…]]` 不能被单层方括号规则再数一遍：数错会让正确译文报「占位符多出」。"""
    assert GENERIC_TOKENS.findall("[[Go|Passage]]") == ["[[Go|Passage]]"]
    assert check_placeholders("[[Go|Passage]]", "[[去|Passage]]") == []
    # 单层标签和双层链接并存时各算各的
    assert check_placeholders("[[Go|Passage]] [wave]Hi[/wave]", "[[去|Passage]] [wave]嗨[/wave]") == []


# ================================================ 3. batch_edit_strings 响应形状
class _CountOnlyFake(FakeParaTranz):
    """批量写回只回 `{"update": N}` 的假平台。

    真实平台的写入类接口就是这个形状（`update_file_translation` 实测如此），
    客户端不能把非 list 响应当成「一条都没改」。
    """

    def _dispatch(self, request):  # noqa: ANN001 - 与父类签名一致
        resp = super()._dispatch(request)
        if request.method == "PUT" and request.url.path.endswith("/strings") and resp.status_code == 200:
            body = json.loads(request.content or b"{}")
            return self._json({"update": len(body.get("items") or [])})
        return resp


def test_batch_edit_strings_reads_update_count_from_dict():
    """原来的坏法：`if isinstance(res, list)` 之外一律不管，dict 响应静默返回空列表，
    上层只能以为「一条都没改」。"""
    fake = _CountOnlyFake()
    client = fake.client()
    try:
        ids = [1000, 1001, 1002]
        res = client.batch_edit_strings([{"id": i, "stage": 3} for i in ids])
    finally:
        client.close()

    assert isinstance(res, list), "现有调用方按 list 用（遍历/取长度），形状不能变"
    assert isinstance(res, BatchWrite)
    assert res.reported == 3, "平台回的是条数，不能被当成 0"
    assert sorted(res) == ids, "条数与提交条数一致时 id 可以确定，应补全而不是留空"


def test_batch_edit_strings_does_not_invent_ids_on_partial_count():
    """条数与提交数对不上时**不猜 id**：`len(res)` 保持 0（不伪造），
    `reported` 如实给出平台报告的条数 —— 两者合起来才分得清
    「改了 N 条但不知道是哪几条」和「一条都没改」。"""
    assert _edit_result({"update": 1}, [{"id": 1}, {"id": 2}, {"id": 3}]) == ([], 1)
    assert _edit_result({"update": 0}, [{"id": 1}]) == ([], 0)
    # 空 body（2xx，请求已接受）按整块记账，与 id 数组语义一致
    assert _edit_result(None, [{"id": 7}, {"id": 8}]) == ([7, 8], 2)
    # 「改了 0 条」与「改了 3 条但不知道 id」是两个不同的事实
    assert BatchWrite([], reported=0).reported != BatchWrite([], reported=3).reported


def test_batch_edit_strings_rejects_unrecognized_shape():
    """形状异常必须明确报错：以前静默返回空 list，上层只能读到「没做事」。"""
    with pytest.raises(ParaTranzError):
        _edit_result("OK", [{"id": 1}])
    with pytest.raises(ParaTranzError):
        _edit_result({"status": "ok"}, [{"id": 1}])


def test_review_reports_written_from_count_only_platform(cfg, store, llm):
    """原来的坏法：平台回 `{"update": N}` 时 `set_stage` 得到空列表，
    于是 `stats.written = len(written_ids)` = 0 —— stage 明明全改完了，
    报表却写「stage 写回完成：0 条」。"""
    fake = _CountOnlyFake()
    client = fake.client()
    logs: list[str] = []
    try:
        stats = ReviewPipeline(client, cfg, store, llm, log=logs.append).run(dry_run=False)
    finally:
        client.close()

    assert stats.disputed >= 1, "前提：这批里确实有该打回的条目"
    assert stats.written == stats.disputed + stats.passed, "写回条数必须与实际调用一致"
    # 数字是真的：去平台上核对 stage 真的被改了
    stage_of = {s["key"]: s["stage"] for s in fake.state["strings"]}
    assert stage_of["m2"] == cfg.review.fail_stage, "打回项真的落到 fail_stage"
    assert stage_of["m1"] == cfg.review.pass_stage, "通过项真的落到 pass_stage"
    assert stats.written > 0


# ================================================================== 4. 取材截断
def test_translate_marks_truncated_when_cap_is_reached(client, cfg, store, llm):
    """原来的坏法：`iter_strings(max_items=20000)` 到上限就安静 `return`，
    stats 里没有任何痕迹 —— 2 万条以上的项目每轮只翻前 2 万条，用户却以为翻完了。"""
    assert translate_mod.DEFAULT_MAX_ITEMS == 20000
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(max_items=3, sync_terms=False)

    assert stats.truncated is True
    assert stats.max_items == 3
    assert stats.as_dict()["truncated"] is True
    assert stats.as_dict()["max_items"] == 3
    assert any("截断" in w for w in stats.warnings), "必须在告警里写明，不能只躺在字段里"

    # 没顶到上限时不能误报「截断」
    whole = pipe.run(max_items=1000, sync_terms=False, dry_run=True)
    assert whole.truncated is False
    assert whole.max_items == 0


def test_review_marks_truncated_and_logs_it(client, cfg, store, llm):
    """原来的坏法：review 的 `max_items=20000` 同样静默截断，
    报表里看不出「还有剩余」，日志也一句不说。"""
    assert review_mod.DEFAULT_MAX_ITEMS == 20000
    logs: list[str] = []
    stats = ReviewPipeline(client, cfg, store, llm, log=logs.append).run(max_items=2, dry_run=True)

    assert stats.truncated is True
    assert stats.max_items == 2
    assert stats.as_dict()["truncated"] is True
    assert any("截断" in msg for msg in logs), "流水线日志要输出一句（走已有的 _log 通道）"

    quiet: list[str] = []
    under = ReviewPipeline(client, cfg, store, llm, log=quiet.append).run(max_items=1000, dry_run=True)
    assert under.truncated is False
    assert not any("截断" in msg for msg in quiet)


def test_translate_stats_expose_truncation_fields():
    """新字段必须出现在 as_dict()：CLI / WebUI 只读 as_dict()，漏了就看不见。"""
    d = TranslateStats(truncated=True, max_items=20000).as_dict()
    assert d["truncated"] is True
    assert d["max_items"] == 20000


# ================================================================ 5. schema 迁移
def test_old_db_without_pos_column_is_migrated(tmp_path):
    """原来的坏法：只有 `CREATE TABLE IF NOT EXISTS`，老库的表原样保留 ——
    后来加的列永远补不上，升级后第一轮就是
    `table term has no column named pos`，整条流水线失败。"""
    path = tmp_path / "agent.db"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE term (term TEXT PRIMARY KEY, translation TEXT NOT NULL);")
    old.execute("INSERT INTO term(term, translation) VALUES('farm', '农场')")
    old.commit()
    old.close()

    store = Store(path)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # 补列之后 upsert 必须能跑通（缺列时这里会抛 no column named pos）
        store.term_upsert("farm", "农场", pos="noun", note="农田", variants=["farms"])
        store.term_commit()
        row = store.term_all()["farm"]
        assert row["pos"] == "noun"
        assert row["status"] == "active"
        assert row["variants"] == ["farms"]
    finally:
        store.close()


def test_migration_is_idempotent_on_reopen(tmp_path):
    """迁移只 ALTER 缺的列：重复 ALTER 会抛 duplicate column name，
    所以第二个 `Store` 打开同一个库必须是干净的 no-op。"""
    path = tmp_path / "agent.db"
    first = Store(path)
    assert first.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    first.close()

    second = Store(path)
    try:
        second.term_upsert("Sword", "剑", pos="noun")
        assert second.term_all()["Sword"]["pos"] == "noun"
        assert second.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        second.close()


def test_newer_db_version_is_refused(tmp_path):
    """版本号高于本程序支持的库必须明确报错 —— 旧程序在缺表缺列的库上继续写
    只会把数据写坏，而且报错信息会离真相很远。"""
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="版本"):
        Store(path)
