"""translate.py 三条已确认缺陷的回归测试。

都用内存假平台（FakeParaTranz）+ 测试专用 FakeLLM 驱动真实 client 代码路径，
不联网。每个测试的 docstring 写清"防的是什么回归"。
"""
from __future__ import annotations

import json

import pytest
from fake_llm import FakeLLM, fake_translate

from paratranz_agent.client import BATCH_LIMIT, ParaTranzError
from paratranz_agent.models import Stage
from paratranz_agent.pipelines import TranslatePipeline
from paratranz_agent.pipelines import translate as tr_mod
from paratranz_agent.qa import check_terms


def _fake_responder(messages) -> str:
    """与 FakeLLM 默认行为等价，但保留占位符（用 fake_translate）。"""
    items = json.loads(messages[-1]["content"])["items"]
    return json.dumps(
        {"items": [{"id": it["id"], "translation": fake_translate(it["original"])} for it in items]},
        ensure_ascii=False,
    )


def _pending(fake) -> list[dict]:
    return [s for s in fake.state["strings"] if s["stage"] == 0 and not s["translation"]]


# --------------------------------------------------------- 0) 分块常量不漂移
def test_write_chunk_matches_platform_batch_limit():
    """防回归：写回块大小与 client.BATCH_LIMIT 脱钩，导致单请求超过平台 500 条上限。"""
    assert tr_mod.WRITE_CHUNK == BATCH_LIMIT


# ------------------------------------- 1) force_overwrite 保护人工译文（方案 A）
def test_force_overwrite_false_does_not_clobber_manual_translation(client, cfg, store, fake):
    """防回归：force_overwrite 曾是死配置 —— 取材之后别人译好并置 stage=1 的条目，
    MT 译文会直接覆盖。默认 false 时必须在写回前复查并放过这些条目。"""
    victim = next(s for s in fake.state["strings"] if s["key"] == "i1")
    assert victim["stage"] == 0

    def responder(messages):
        # 模拟"取材完成、写回之前"有人手工翻译并推进 stage
        victim["translation"] = "木剑（人工）"
        victim["stage"] = int(Stage.TRANSLATED)
        return _fake_responder(messages)

    pipe = TranslatePipeline(client, cfg, store, FakeLLM(responder=responder))
    stats = pipe.run(dry_run=False)

    assert victim["translation"] == "木剑（人工）", "人工译文被 MT 覆盖了"
    assert victim["stage"] == int(Stage.TRANSLATED)
    assert stats.skipped_manual == 1
    assert stats.written == 20, f"应只写其余 20 条，实际 {stats.written}"
    # 被放过的条目不该进 TM，否则下一轮 TM 命中会把人工译文盖回去
    assert store.tm_exact("Wooden Sword") is None
    assert store.tm_size() == 20


def test_force_overwrite_true_overwrites_manual_translation(client, cfg, store, fake):
    """防回归：force_overwrite=true 时若仍做复查，运维显式要求的覆盖会失效（死配置反向）。"""
    cfg.translate.force_overwrite = True
    victim = next(s for s in fake.state["strings"] if s["key"] == "i1")

    def responder(messages):
        victim["translation"] = "木剑（人工）"
        victim["stage"] = int(Stage.TRANSLATED)
        return _fake_responder(messages)

    pipe = TranslatePipeline(client, cfg, store, FakeLLM(responder=responder))
    stats = pipe.run(dry_run=False)

    assert victim["translation"] != "木剑（人工）"
    assert stats.skipped_manual == 0
    assert stats.manual_risk == 0
    assert stats.written == 21


def test_manual_recheck_unavailable_skips_writes_conservatively(
    client, cfg, store, fake, monkeypatch
):
    """防回归：复查请求失败时若"照写不误"，正好在确认不了的情况下覆盖人工译文。
    期望：一条都不写、manual_risk 记账、warnings 里留痕（不静默、不崩）。"""
    real_iter = client.iter_strings

    def boom(*args, **kw):
        # _collect 不传 file（全项目），复查一定带 file 列表 —— 以此区分两条调用
        if kw.get("file"):
            raise ParaTranzError(0, "重试耗尽: 模拟超时", "/projects/6896/strings")
        return real_iter(*args, **kw)

    monkeypatch.setattr(client, "iter_strings", boom)
    pipe = TranslatePipeline(client, cfg, store, FakeLLM())
    stats = pipe.run(dry_run=False)

    assert stats.written == 0
    assert stats.manual_risk == 21
    assert any("复查不可用" in w for w in stats.warnings)
    assert len(_pending(fake)) == 21, "复查失败时不该写入任何词条"
    assert store.tm_size() == 0
    assert store.ledger_stats("translate").get("write") is None


def test_protection_does_not_trust_server_side_stage_filter(client, cfg, store, fake, monkeypatch):
    """防回归：复查若只靠服务端 `stage=0` 过滤，平台一旦忽略该参数，
    "被置成有疑问但还没填译文"的条目会被 MT 覆盖。期望：本地二次判定 stage。"""
    other = next(s for s in fake.state["strings"] if s["key"] == "m5")
    real_iter = client.iter_strings

    def ignoring_stage(*args, **kw):
        # 只让"复查"那次调用丢掉 stage 参数（_collect 一定带 max_items，复查不带）
        if kw.get("max_items") is None:
            kw.pop("stage", None)
        return real_iter(*args, **kw)

    def responder(messages):
        # 有人在取材后把这条置为「有疑问」，但没填译文
        other["stage"] = int(Stage.DISPUTED)
        return _fake_responder(messages)

    monkeypatch.setattr(client, "iter_strings", ignoring_stage)
    pipe = TranslatePipeline(client, cfg, store, FakeLLM(responder=responder))
    stats = pipe.run(dry_run=False)

    assert other["translation"] == "", "stage!=0 的条目被 MT 写入了"
    assert other["stage"] == int(Stage.DISPUTED)
    assert stats.skipped_manual == 1
    assert stats.written == 20


# ------------------------------------------- 2) 写回中途失败保留已完成记账
def test_partial_write_failure_keeps_tm_and_ledger(client, cfg, store, fake, monkeypatch):
    """防回归：第 2 块抛错时第 1 块已写进平台，但 TM / 账本 / written 全都没执行，
    调用方只拿到异常。期望：首块立即记账，异常里带"已写入 N 条"，统计可读。"""
    monkeypatch.setattr(tr_mod, "WRITE_CHUNK", 2)
    real = client.batch_edit_strings
    calls = {"n": 0}

    def flaky(items, *, chunk=BATCH_LIMIT):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ParaTranzError(502, "bad gateway", "/projects/6896/strings")
        return real(items, chunk=chunk)

    monkeypatch.setattr(client, "batch_edit_strings", flaky)
    pipe = TranslatePipeline(client, cfg, store, FakeLLM())

    with pytest.raises(tr_mod.WriteInterrupted) as excinfo:
        pipe.run(dry_run=False)

    exc = excinfo.value
    stats = exc.stats
    assert "已写入 2 条" in str(exc)
    assert stats.written == 2, f"首块应已计入，实际 {stats.written}"
    assert stats.failed == 19, f"失败块及其后应计失败，实际 {stats.failed}"
    assert stats.write_chunks_failed == 1
    assert stats.warnings

    # 首块真的落到了平台，且本地账本/TM 与它**逐 id** 一致
    # （不是"平台写了、本地不知情"）
    written = [s for s in fake.state["strings"] if s["stage"] == 1 and s["key"] in ("m4", "m5")]
    assert len(written) == 2
    assert stats.written == len(written)
    for s in written:
        assert store.tm_exact(s["original"]) == s["translation"], f"{s['key']} 未回填 TM"
        assert store.ledger_done("translate", str(s["id"]), "write"), f"{s['key']} 未记账"
    assert store.tm_size() == 2
    assert store.ledger_stats("translate").get("write") == 2
    # 未写入的条目不能提前进 TM / 账本，否则下一轮会误判"已翻过"
    for s in fake.state["strings"]:
        if s["key"].startswith(("m6", "m7", "m8", "m9", "m10")) or s["key"].startswith(("i", "q")):
            assert store.tm_exact(s["original"]) is None


def test_dry_run_writes_nothing_but_reports_plan(client, cfg, store, fake):
    """防回归：dry-run 被分块写回逻辑带跑 —— 只出报表、不落平台、不记账、不复查人工译文。"""
    pending_before = len(_pending(fake))
    pipe = TranslatePipeline(client, cfg, store, FakeLLM())
    stats = pipe.run(dry_run=True)

    assert len(_pending(fake)) == pending_before, "dry-run 不该写入平台"
    assert stats.written == 21, f"dry-run 应报'将写入多少'，实际 {stats.written}"
    assert store.tm_size() == 0 and store.ledger_stats("translate") == {}
    assert stats.skipped_manual == 0 and stats.manual_risk == 0
    assert stats.dry_run is True


# ------------------------------------------------- 3) 平台术语同步不再吞异常
def test_sync_terms_retires_terms_deleted_on_platform(client, cfg, store):
    """防回归：平台已删/改名的术语永久留在本地，check_terms 一直按旧译法报警。
    期望：同步成功后按 source="platform" 差集把本地孤儿置 stale，
    人工来源（source 非 platform）的术语不动，stale 之后不再误报 term_violation。"""
    store.term_upsert("farm", "农场", source="platform")          # 平台仍有 → 保留
    store.term_upsert("Wooden Sword", "木剑", source="platform")   # 平台已删 → retire
    store.term_upsert("HP", "生命值", source="manual")             # 本地人工 → 不动
    store.term_commit()

    before = store.term_all()
    assert check_terms("Wooden Sword", "剑", before), "前提：旧术语此时会误报"

    pipe = TranslatePipeline(client, cfg, store, FakeLLM())
    res = pipe.sync_terms_from_platform()

    assert res["ok"] is True
    assert res["count"] == 1  # 假平台只有 farm
    assert res["retired"] == 1
    after = store.term_all()
    assert "farm" in after
    assert "HP" in after, "人工来源的术语被误清理"
    assert "Wooden Sword" not in after
    assert check_terms("Wooden Sword", "剑", after) == [], "已删术语仍在误报"


def test_sync_terms_empty_platform_does_not_wipe_local(client, cfg, store, fake):
    """防回归：接口/权限异常返回空表时把整张本地术语表清空。
    期望：平台返回 0 条但本地有平台术语 → 不清理，并在返回值里说明。"""
    store.term_upsert("farm", "农场", source="platform")
    store.term_commit()
    fake.state["terms"] = []

    pipe = TranslatePipeline(client, cfg, store, FakeLLM())
    res = pipe.sync_terms_from_platform()

    assert res["ok"] is True
    assert res["count"] == 0
    assert res["retired"] == 0
    assert res.get("note") == "empty_platform"
    assert "farm" in store.term_all()


def test_sync_terms_forbidden_is_reported_not_swallowed(client, cfg, store, monkeypatch):
    """防回归：403（termsMode=1 非成员）被 `except: pass` 吞掉，返回值像成功。
    期望：返回 ok=False / reason="forbidden" 且不抛，也不做任何差集清理。"""
    store.term_upsert("farm", "农场", source="platform")
    store.term_commit()

    def forbidden(*args, **kw):
        raise ParaTranzError(403, "无权访问术语表", "/projects/6896/terms")

    monkeypatch.setattr(client, "list_terms", forbidden)
    pipe = TranslatePipeline(client, cfg, store, FakeLLM())

    res = pipe.sync_terms_from_platform()
    assert res == {"ok": False, "reason": "forbidden", "count": 0, "retired": 0}
    assert "farm" in store.term_all(), "拿不到完整表时不该清理本地术语"

    # run() 内也不再静默：记进 stats，但翻译照常进行
    stats = pipe.run(sync_terms=True, dry_run=False)
    assert stats.terms_sync["ok"] is False
    assert stats.terms_sync["reason"] == "forbidden"
    assert stats.written == 21


def test_sync_terms_hard_failure_raises_by_default(client, cfg, store, monkeypatch):
    """防回归：超时/5xx 这类"真失败"被当成"平台不可见"静默吞掉。
    期望：默认抛（不许当没事发生）；raise_on_error=False 时返回 ok=False/error，
    run() 里记进 stats.terms_sync 但不阻断翻译。"""
    def broken(*args, **kw):
        raise ParaTranzError(500, "internal error", "/projects/6896/terms")

    monkeypatch.setattr(client, "list_terms", broken)
    pipe = TranslatePipeline(client, cfg, store, FakeLLM())

    with pytest.raises(ParaTranzError):
        pipe.sync_terms_from_platform()

    res = pipe.sync_terms_from_platform(raise_on_error=False)
    assert res["ok"] is False
    assert res["reason"] == "error"
    assert "internal error" in res["error"]

    stats = pipe.run(sync_terms=True, dry_run=False)
    assert stats.terms_sync["ok"] is False
    assert stats.terms_sync["reason"] == "error"
    assert stats.written == 21, "术语同步失败不该拖住翻译"
