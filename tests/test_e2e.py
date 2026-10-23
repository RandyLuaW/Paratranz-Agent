"""端到端：真实 client 代码路径 + 内存假平台 + 测试专用 FakeLLM。

覆盖五条流水线的实际写操作，断言的是**假平台上的状态变化**，
而不是"函数返回了个对象"。
"""
from __future__ import annotations

import json

from fake_llm import FakeLLM
from paratranz_agent.models import Stage
from paratranz_agent.pipelines import (
    GlossaryPipeline,
    PlanPipeline,
    ReviewPipeline,
    StyleGuidePipeline,
    TranslatePipeline,
)

# ------------------------------------------------------------------ client
def test_client_reads_project_and_files(client):
    info = client.get_project()
    assert info.id == 6896
    assert info.extra["task"] is True

    files = client.list_files()
    assert len(files) == 3
    assert files[0].pending == 7  # 10 条里 3 条已翻译


def test_client_iter_strings_stage_filter(client):
    pending = list(client.iter_strings(stage=Stage.UNTRANSLATED, page_size=5))
    assert len(pending) == 21  # 24 条 - 3 条已翻译
    assert all(s.stage == 0 for s in pending)


def test_client_batch_edit_writes_translation(client, fake):
    target = fake.state["strings"][3]
    client.batch_edit_strings([{"id": target["id"], "translation": "早上好，农夫。", "stage": 1}])
    assert target["translation"] == "早上好，农夫。"
    assert target["stage"] == 1


# -------------------------------------------------------------------- plan
def test_plan_builds_boxes(client, cfg, store):
    pipe = PlanPipeline(client, cfg, store)
    planned = pipe.build_plan()
    # 3 个文件，每个 pending 7/8/6，max_strings=10 → 三个箱子
    assert len(planned) == 3
    assert planned[0].files[0].name.endswith("Items.json")  # pending 最大优先
    # 部分翻译的文件走词条模式，整文件未翻译的走文件模式
    modes = {p.files[0].name: p.mode for p in planned}
    assert modes["www/data/Map001.json"] == 0
    assert modes["www/data/Items.json"] == 1
    # 任务说明含硬规则
    assert "占位符" in planned[0].content


def test_plan_execute_creates_tasks_with_files(client, cfg, store, fake):
    pipe = PlanPipeline(client, cfg, store)
    created = pipe.execute(dry_run=False)
    assert len(created) == 3
    assert len(fake.state["tasks"]) == 3
    for t in fake.state["tasks"]:
        assert t["taskFiles"], f"任务 {t['title']} 没有关联文件"
        assert t["content"]
    # 幂等：重跑不重复建
    again = pipe.execute(pipe.build_plan(), dry_run=False)
    assert again == []
    assert len(fake.state["tasks"]) == 3


def test_plan_dry_run_writes_nothing(client, cfg, store, fake):
    pipe = PlanPipeline(client, cfg, store)
    pipe.execute(pipe.build_plan(), dry_run=True)
    assert fake.state["tasks"] == []


# --------------------------------------------------------------- translate
def test_translate_fills_untranslated_and_sets_stage(client, cfg, store, llm, fake):
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=False)

    assert stats.fetched == 21
    assert stats.written > 0
    remaining = [s for s in fake.state["strings"] if s["stage"] == 0]
    assert remaining == [], f"仍有未翻译: {[(s['key'], s['original']) for s in remaining]}"
    # 占位符都保住了
    for s in fake.state["strings"]:
        if s["stage"] >= 1 and "\\N[1]" in s["original"]:
            assert "\\N[1]" in s["translation"]
    assert store.tm_size() > 0


def test_translate_tm_exact_hit_avoids_llm(client, cfg, store, fake):
    store.tm_add("Wooden Sword", "木剑", project=6896, origin="human", quality=1.0)
    pipe = TranslatePipeline(client, cfg, store, FakeLLM())
    stats = pipe.run(dry_run=False)
    assert stats.tm_hit >= 1
    wooden = next(s for s in fake.state["strings"] if s["original"] == "Wooden Sword")
    assert wooden["translation"] == "木剑"


def test_translate_blocks_broken_output(client, cfg, store):
    """模型凭空多出占位符时，必须拦住不写回。"""
    broken = FakeLLM(responder=lambda msgs: json.dumps(
        {"items": [{"id": it["id"], "translation": "坏译文 {9}"}
                   for it in json.loads(msgs[-1]["content"])["items"]]},
        ensure_ascii=False,
    ))
    pipe = TranslatePipeline(client, cfg, store, broken)
    stats = pipe.run(dry_run=False)
    assert stats.blocked > 0
    assert stats.blocked_samples
    assert "{9}" in stats.blocked_samples[0]["reason"]


# ------------------------------------------------------------------ review
def test_review_disputes_missing_placeholder(client, cfg, store, llm, fake):
    pipe = ReviewPipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=False)

    bad = next(s for s in fake.state["strings"] if s["original"].startswith("You got"))
    assert bad["stage"] == 2, "丢占位符的条目应被打回为「有疑问」"

    good = next(s for s in fake.state["strings"] if s["original"].startswith("Hello,"))
    assert good["stage"] == 3, "合格条目应推进到「已检查」"
    assert stats.disputed >= 1
    assert stats.passed >= 1


def test_review_dry_run_keeps_stage(client, cfg, store, llm, fake):
    before = [s["stage"] for s in fake.state["strings"]]
    ReviewPipeline(client, cfg, store, llm).run(dry_run=True)
    assert [s["stage"] for s in fake.state["strings"]] == before


def test_review_skips_disputed_on_rerun(client, cfg, store, llm, fake):
    """打回（stage=2）的条目默认不再复核，否则守护进程会无限循环。"""
    pipe = ReviewPipeline(client, cfg, store, llm)
    first = pipe.run(dry_run=False)
    assert first.disputed >= 1

    second = pipe.run(dry_run=False)
    assert second.fetched == 0, "第二轮不应再拉出已打回的条目"

    # 显式要求复核时才处理
    third = pipe.run(dry_run=False, include_disputed=True)
    assert third.fetched >= 1


# ---------------------------------------------------------------- glossary
def _glossary_responder(messages):
    payload = json.loads(messages[-1]["content"])
    if payload.get("task") == "term-resolve":
        return json.dumps(
            {"terms": [
                {"term": it["term"], "translation": f"定译-{it['term']}", "pos": "noun", "note": ""}
                for it in payload["items"]
            ]},
            ensure_ascii=False,
        )
    return json.dumps(
        {"terms": [
            {"term": "Health Potion", "translation": "生命药水", "pos": "noun",
             "note": "恢复道具", "variants": ["health potions"]}
        ]},
        ensure_ascii=False,
    )


def _smart_responder(messages):
    """既能翻译、又能审校、又能提术语的 responder —— 用于全流程测试。"""
    from fake_llm import fake_translate

    payload = json.loads(messages[-1]["content"])
    if payload.get("task") == "review":
        return json.dumps(
            {"items": [{"id": it["id"], "verdict": "pass", "reason": ""} for it in payload["items"]]},
            ensure_ascii=False,
        )
    items = payload.get("items") or []
    if items and isinstance(items[0], dict) and "id" in items[0]:
        return json.dumps(
            {"items": [{"id": it["id"], "translation": fake_translate(it["original"])} for it in items]},
            ensure_ascii=False,
        )
    return _glossary_responder(messages)


def test_glossary_build_and_publish(client, cfg, store, fake):
    llm = FakeLLM(responder=_glossary_responder)
    pipe = GlossaryPipeline(client, cfg, store, llm)
    cands = pipe.build()
    assert any(c.term == "Health Potion" and c.translation == "生命药水" for c in cands)
    # 平台已有术语不重复提
    assert not any(c.term == "farm" for c in cands)

    before = len(fake.state["terms"])
    result = pipe.publish(cands, dry_run=False)
    assert result["submitted"] > 0
    assert len(fake.state["terms"]) > before
    published = {t["term"]: t["translation"] for t in fake.state["terms"]}
    assert published["Health Potion"] == "生命药水"
    # 本地术语库同步
    assert store.term_all()["Health Potion"]["translation"] == "生命药水"


def test_glossary_dry_run(client, cfg, store, fake):
    pipe = GlossaryPipeline(client, cfg, store, FakeLLM(responder=_glossary_responder))
    cands = pipe.build()
    before = len(fake.state["terms"])
    pipe.publish(cands, dry_run=True)
    assert len(fake.state["terms"]) == before


# -------------------------------------------------------------- styleguide
def test_styleguide_build_contains_real_data(client, cfg, store, llm):
    md = StyleGuidePipeline(client, cfg, store, llm).build(with_llm=False)
    assert "汉化规范" in md
    assert "占位符与控制符" in md
    assert "www/data/Map001.json" not in md  # 不逐文件罗列，只给类型统计
    assert "`json`" in md and "`txt`" in md
    assert "自动校对拦什么" in md


def test_styleguide_publish_creates_announcement(client, cfg, store, llm, fake):
    pipe = StyleGuidePipeline(client, cfg, store, llm)
    res = pipe.publish(dry_run=False, with_llm=False)
    assert res["action"] == "create"
    assert len(fake.state["announcements"]) == 1
    ann = fake.state["announcements"][0]
    assert ann["internal"] is True
    assert "汉化规范" in ann["title"]

    # 再发一次应更新而不是新建
    res2 = pipe.publish(dry_run=False, with_llm=False)
    assert res2["action"] == "update"
    assert len(fake.state["announcements"]) == 1


# --------------------------------------------------------------- 全流程
def test_full_pipeline_sequence(client, cfg, store, fake):
    llm = FakeLLM(responder=_smart_responder)

    PlanPipeline(client, cfg, store).execute(dry_run=False)
    t = TranslatePipeline(client, cfg, store, llm).run(dry_run=False)
    r = ReviewPipeline(client, cfg, store, llm).run(dry_run=False)
    g = GlossaryPipeline(client, cfg, store, llm).publish(
        GlossaryPipeline(client, cfg, store, llm).build(), dry_run=False
    )
    s = StyleGuidePipeline(client, cfg, store, llm).publish(dry_run=False, with_llm=False)

    assert len(fake.state["tasks"]) == 3
    assert t.written > 0
    assert r.passed > 0
    assert g["submitted"] > 0
    assert s["action"] == "create"

    # 最终状态：没有 stage=0 的词条
    assert not [x for x in fake.state["strings"] if x["stage"] == 0]
