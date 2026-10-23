"""全盘审计（对照官方 spec + 真实平台数据）发现的缺陷回归测试，全部离线。

目前覆盖：
1. `Term.variants` 为 null 时不得崩（真实平台会把「没有变体」存成 null）
2. 假平台也要能产出 null 变体，保证这条分支永远有测试盯着
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.models import Term  # noqa: E402


def test_term_variants_null_becomes_empty_list():
    """防回归：平台对没有变体的术语返回 `variants: null`。

    真实事故：`POST /terms` 不带 variants → 平台存 null →
    我们的 `Term` 要求 list → `create_term` 解析响应就崩，
    更严重的是此后**任何** `iter_terms()` 都崩，而 glossary 第一步就是它。
    离线假平台永远填列表，所以这条只能靠这个用例守着。
    """
    t = Term.model_validate(
        {"id": 1, "term": "farm", "translation": "农场", "pos": "noun", "variants": None}
    )
    assert t.variants == []
    assert t.term == "farm"


def test_term_variants_missing_and_present():
    """缺字段 / 有值两条路径都要正常。"""
    assert Term.model_validate({"term": "a", "translation": "甲"}).variants == []
    assert Term.model_validate(
        {"term": "b", "translation": "乙", "variants": ["bs"]}
    ).variants == ["bs"]


def test_iter_terms_survives_null_variants(client, fake):
    """端到端：平台上存在 variants=null 的术语时，iter_terms 仍然可用。"""
    fake.state["terms"] = [
        {"id": 1, "term": "farm", "translation": "农场", "pos": "noun", "variants": None},
        {"id": 2, "term": "mill", "translation": "磨坊", "pos": "noun", "variants": ["mills"]},
    ]
    got = {t.term: t for t in client.iter_terms()}
    assert set(got) == {"farm", "mill"}
    assert got["farm"].variants == []
    assert got["mill"].variants == ["mills"]
