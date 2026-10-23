"""glossary.py 四条缺陷的回归测试。

全部走内存假平台（`paratranz_agent.fake.FakeParaTranz`）+ 测试专用 `FakeLLM`，
不联网、不花钱、结果确定。需要精确控制句对/平台术语表的用例用 `_reset()` 把假平台
状态换成最小数据集，避免被种子数据里的词条干扰。每个测试的 docstring 写清
"防的是什么回归"。
"""
from __future__ import annotations

import json
from typing import Any

from fake_llm import FakeLLM

from paratranz_agent.models import Stage, StringItem
from paratranz_agent.pipelines import GlossaryPipeline
from paratranz_agent.pipelines.glossary import RESOLVE_BATCH, TermCandidate


# --------------------------------------------------------------------- 工具
def _reset(fake, rows: list[tuple[str, str, int]], *, terms: list[dict] | None = None) -> None:
    """把假平台换成最小数据集：rows = (原文, 译文, stage)。"""
    fake.state = {
        "project": {"id": 6896, "name": "T", "source": "en", "dest": "zh-CN", "extra": {}},
        "files": [{"id": 101, "name": "www/data/Items.json", "hash": "a" * 32}],
        "strings": [
            {
                "id": 100 + i,
                "key": f"k{i}",
                "original": orig,
                "translation": tr,
                "stage": int(st),
                "fileId": 101,
                "context": None,
            }
            for i, (orig, tr, st) in enumerate(rows)
        ],
        "terms": list(terms or []),
        "members": [],
        "tasks": [],
        "announcements": [],
        "calls": [],
    }


def _terms_payload(items: list[dict[str, Any]]) -> str:
    return json.dumps({"terms": items}, ensure_ascii=False)


# ------------------------------------- 1) 定译不再只处理前 80 个 / 丢弃要有计数
def test_resolve_covers_all_candidates_beyond_batch_limit(client, cfg, store):
    """防回归：`_resolve` 用 `need[:80]` 一刀截断 —— 第 81 条以后的候选拿不到译法，
    随后在 build 里被静默丢弃（实测 84→80，且统计里一点痕迹都没有）。
    期望：按 RESOLVE_BATCH 分批把候选全部发完，每条都拿到译法。"""
    calls: list[int] = []

    def responder(messages):
        payload = json.loads(messages[-1]["content"])
        assert payload.get("task") == "term-resolve"
        calls.append(len(payload["items"]))
        return _terms_payload(
            [
                {"term": it["term"], "translation": f"译-{it['term']}", "pos": "noun"}
                for it in payload["items"]
            ]
        )

    pipe = GlossaryPipeline(client, cfg, store, FakeLLM(responder=responder))
    total = RESOLVE_BATCH + 4  # 84 条，正好复现"84→80"
    cands = [
        TermCandidate(term=f"Term Number {i:03d}", samples=[("Term Number", "术语")])
        for i in range(total)
    ]

    out = pipe._resolve(cands)

    assert len(out) == total
    missing = [c.term for c in out if not c.translation]
    assert missing == [], f"超出 {RESOLVE_BATCH} 条的候选被静默丢掉了：{missing}"
    # 不是一次塞 84 条，而是分批；两批都要发到
    assert calls == [RESOLVE_BATCH, total - RESOLVE_BATCH]
    assert pipe.stats["resolved"] == total


def test_build_counts_unresolved_instead_of_dropping_silently(client, cfg, store, fake):
    """防回归：没拿到译法的候选在 build 里 `continue` 掉，谁也看不见 ——
    "定译请求截断/模型漏答"这类故障在日志和统计里完全不可观测。
    期望：`stats["unresolved"]` 如实计数，并随 publish 结果一起返回。"""
    _reset(
        fake,
        [
            ("Ancient Relic of Dawn", "远古遗物", Stage.REVIEWED),
            ("Crystal Shard Fragment", "水晶碎片", Stage.REVIEWED),
            ("Silver Bell Tower", "银钟塔", Stage.CHECKED),
        ],
    )

    def responder(messages):  # 定译一律留空：模拟模型漏答
        payload = json.loads(messages[-1]["content"])
        if payload.get("task") == "term-resolve":
            return _terms_payload(
                [{"term": it["term"], "translation": ""} for it in payload["items"]]
            )
        return _terms_payload([])

    pipe = GlossaryPipeline(client, cfg, store, FakeLLM(responder=responder))
    cands = pipe.build()

    unique = pipe.stats["frequency"] - pipe.stats["deduped"]
    assert unique >= 3, f"前提：统计法应产出若干候选，实际 {unique}"
    assert cands == [], "没有译法的候选不该进结果"
    assert pipe.stats["unresolved"] == unique, "被丢弃的候选没有计数"
    assert pipe.stats["resolved"] == 0
    assert pipe.publish(cands, dry_run=True)["unresolved"] == unique


# ------------------------------------------- 2) 模型返回重复 term 必须归并
def test_build_dedupes_repeated_term_and_keeps_model_alignment(client, cfg, store, fake):
    """防回归：模型返回重复 term 就进两条候选，publish 都提交 —— 译法冲突时后者
    覆盖前者，谁胜出取决于模型返回顺序。期望：按 term（忽略大小写）归并成一条，
    首次出现的顺序和拼写优先，同 term 不同译法时保留来源为"模型对齐"的那条，
    并给出 `deduped` 计数。"""
    _reset(fake, [("Health Potion", "生命药水", Stage.REVIEWED)])

    def responder(messages):
        payload = json.loads(messages[-1]["content"])
        if payload.get("task") == "term-resolve":
            return _terms_payload(
                [{"term": it["term"], "translation": "统计译法"} for it in payload["items"]]
            )
        return _terms_payload(
            [
                # 同一 term 出现两次、译法不同 → 首次优先
                {"term": "Health Potion", "translation": "生命药水", "pos": "noun"},
                {"term": "Health Potion", "translation": "治疗药水", "pos": "noun"},
                # 大小写与词频候选撞车 → 归并后须保留模型（source=llm）的译法
                {"term": "health potion", "translation": "生命药水", "pos": "noun"},
            ]
        )

    pipe = GlossaryPipeline(client, cfg, store, FakeLLM(responder=responder))
    cands = pipe.build()

    same = [c for c in cands if c.term.casefold() == "health potion"]
    assert len(same) == 1, f"同一 term 出了多条候选：{[c.term for c in same]}"
    only = same[0]
    assert only.term == "Health Potion", "首次出现的拼写应保留"
    assert only.translation == "生命药水", f"译法被后来的覆盖了：{only.translation}"
    assert only.source == "llm"
    assert pipe.stats["deduped"] == 2

    # 归并后提交条数 == 候选条数，且没有重复 term（否则后者会盖掉前者的译法）
    res = pipe.publish(cands, dry_run=True)
    assert res["submitted"] == len(cands)
    keys = [c.term.casefold() for c in cands]
    assert len(keys) == len(set(keys))


# ------------------------------- 3) stage=1（未校对机翻）不再当已定稿取材
def test_pairs_exclude_machine_translation_by_default(client, cfg, store, fake):
    """防回归：取材把 stage=1（未校对机翻）和 5/3 一起当已定稿，错译会被固化成
    强制术语。期望：默认只收 5（已审核）/3（已检查）；机翻句对里的独有词条不能
    进候选，`pairs_mt` 为 0。"""
    _reset(
        fake,
        [
            ("Ancient Relic", "远古遗物", Stage.REVIEWED),
            ("Machine Only Phrase", "机翻独有", Stage.TRANSLATED),
        ],
    )

    pipe = GlossaryPipeline(client, cfg, store, FakeLLM())
    pairs = pipe._translated_pairs()
    originals = [p.original for p in pairs]

    assert "Ancient Relic" in originals
    assert "Machine Only Phrase" not in originals, "stage=1 机翻句对被当成了已定稿"
    assert pipe.stats["pairs_mt"] == 0
    assert pipe.stats["pairs_final"] == len(pairs)

    cands = pipe.build()
    assert "Machine Only Phrase" not in {c.term for c in cands}

    res = pipe.publish(cands, dry_run=False)
    platform = {t["term"] for t in fake.state["terms"]}
    assert "Machine Only Phrase" not in platform, "机翻句对里的词条被发到了平台"
    assert "Ancient Relic" in platform and res["submitted"] >= 1

    # publish 本身也要挡住 mt 来源：即使调用方塞进来一条 mt 候选，默认不提交
    mt_only = TermCandidate(term="Hand Made Mt", translation="手工机翻", source="mt")
    res_mt = pipe.publish([mt_only], dry_run=False)
    assert res_mt["skipped_mt"] == 1 and res_mt["submitted"] == 0
    assert "Hand Made Mt" not in {t["term"] for t in fake.state["terms"]}


def test_include_mt_opt_in_marks_candidates_and_publishes(client, cfg, store, fake):
    """防回归：补机翻句对时若不标来源，publish 无法区分"人工定稿的术语"和
    "未校对机翻推出来的术语"，错译会被当强制术语提交。
    期望：`glossary.include_mt=True`（5/3 不足 limit_pairs 才补）时仍可取材，
    但候选必须标 `source="mt"`；显式开这个开关时才允许把 mt 候选发出去。"""
    _reset(
        fake,
        [
            ("Ancient Relic", "远古遗物", Stage.REVIEWED),
            ("Machine Only Phrase", "机翻独有", Stage.TRANSLATED),
        ],
    )
    # include_mt 不在 config.py 的 schema 里（按约束不改 config.py），直接塞进实例
    object.__setattr__(cfg.glossary, "include_mt", True)

    pipe = GlossaryPipeline(client, cfg, store, FakeLLM())
    pairs = pipe._translated_pairs()
    assert "Machine Only Phrase" in [p.original for p in pairs], "开关打开时该补机翻句对"
    assert pipe.stats["pairs_mt"] == 1

    cands = pipe.build()
    mt = [c for c in cands if c.term == "Machine Only Phrase"]
    assert mt and mt[0].source == "mt", "机翻来源的候选没标 source=mt"
    assert [c for c in cands if c.term == "Ancient Relic"][0].source != "mt"

    # 开关（显式 opt-in）打开后，mt 候选会被提交 —— 与 skipped_mt 的默认行为互补
    res = pipe.publish(cands, dry_run=False)
    assert res["skipped_mt"] == 0 and res["submitted"] >= 1
    platform = {t["term"] for t in fake.state["terms"]}
    assert "Machine Only Phrase" in platform
    assert "Ancient Relic" in platform


# ----------------------- 4) min_occurrences 只比真实次数，加权分只用来排序
def test_min_occurrences_counts_real_occurrences_not_weighted_score(client, cfg, store):
    """防回归：`counter[p] += 2` 把加权分当出现次数 —— `min_occurrences=2` 时
    只出现 1 次的词组也凭权重 2 过关，真实只出现 1 次的词因此进了术语表。
    期望：occurrences 记真实次数并与阈值比较；权重只影响排序。"""
    cfg.glossary.min_occurrences = 2
    pairs = [
        StringItem(id=1, key="a", original="Ancient Gate stands open", translation="远古之门开着"),
        StringItem(
            id=2,
            key="b",
            original="Mix a potion, then drink the potion",
            translation="调配药水，然后喝下药水",
        ),
    ]

    pipe = GlossaryPipeline(client, cfg, store, FakeLLM())
    picked = pipe._frequency_candidates(pairs)

    assert "Ancient Gate" not in picked, "只出现 1 次的词组靠权重 2 混过了 min_occurrences"
    assert "Ancient" not in picked, "只出现 1 次的单词不该过阈值"
    assert "potion" in picked, "真实出现 2 次的词被误杀"
    assert pipe._freq_counts["Ancient Gate"] == 1
    assert pipe._freq_counts["potion"] == 2

    # 阈值降到 1 时词组仍可收录 —— 说明不是把词组整个砍掉，只是不再虚计次数
    cfg.glossary.min_occurrences = 1
    assert "Ancient Gate" in pipe._frequency_candidates(pairs)
