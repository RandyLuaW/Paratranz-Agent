"""第六轮审计：`client.py` 五处「形状不认识就静默降级」的回归测试。

共同点：平台换个 envelope 字段 / 忽略 page 参数 / 2xx 但响应体变了，
这些情况在代码里原本都会退化成「0 条」「无限循环」「条数凭空少一条」——
而调用方看到的是一个正常数字，于是没人会去查。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.client import (  # noqa: E402
    ParaTranzClient,
    ParaTranzError,
    unwrap_page,
)
from paratranz_agent.models import Term  # noqa: E402


def _client() -> ParaTranzClient:
    return ParaTranzClient(token="t", project_id=1, base_url="http://127.0.0.1:1/api")


# --------------------------------------------------- ① 认不出的列表形状要报错
def test_unwrap_page_accepts_known_shapes():
    assert unwrap_page([1, 2]) == [1, 2]
    assert unwrap_page({"results": [1, 2]}) == [1, 2]
    assert unwrap_page({"rows": []}) == []
    # 空字典 / None 才是真的没有数据
    assert unwrap_page({}) == []
    assert unwrap_page(None) == []


@pytest.mark.parametrize("bad", [{"payload": [1]}, {"total": 3}, {"items": {"a": 1}}])
def test_unwrap_page_raises_on_unknown_shape(bad):
    """返回空列表等于告诉下游「这个项目一条数据都没有」，必须报错。"""
    with pytest.raises(ParaTranzError) as exc:
        unwrap_page(bad)
    assert "无法识别" in str(exc.value)


def test_unwrap_page_raises_on_scalar():
    with pytest.raises(ParaTranzError):
        unwrap_page("oops")


# --------------------------------------------------- ② 术语分页必须能停下来
def test_iter_terms_stops_when_platform_ignores_page(monkeypatch):
    """平台忽略 `page` 参数时，原实现会无限翻页（挂死），现在靠重复 id 检测停下。"""
    c = _client()
    calls = {"n": 0}

    def fake_list(page: int = 1, page_size: int = 500):
        calls["n"] += 1
        assert calls["n"] < 6, "重复 id 没有被识别，仍在翻页"
        return [Term(id=1, term="same", translation="x")] * page_size

    monkeypatch.setattr(c, "list_terms", fake_list)
    got = list(c.iter_terms(page_size=500))
    assert len(got) == 500, "只应产出第一页（后面全是重复 id）"
    assert calls["n"] == 2, "第二次发现全是重复就该停"
    c.close()


def test_iter_terms_has_hard_page_cap(monkeypatch):
    """即使每页都是新 id，也要有硬上限，不能靠"平台会结束"这种假设。"""
    c = _client()
    counter = {"i": 0}

    def fake_list(page: int = 1, page_size: int = 500):
        # 每页都塞满、且 id 全新 —— 模拟"平台永远有下一页"的异常情况
        counter["i"] += 1
        base = counter["i"] * 1000
        return [Term(id=base + k, term=f"t{base + k}", translation="x")
                for k in range(page_size)]

    monkeypatch.setattr(c, "list_terms", fake_list)
    got = list(c.iter_terms(page_size=500, max_pages=3))
    assert len(got) == 3 * 500, len(got)
    assert counter["i"] == 3, "超过 max_pages 还在请求"
    c.close()


# --------------------------------------------------- ③ 2xx 但没 id 的创建
def test_create_strings_counts_unusable_response_as_failed(monkeypatch):
    """提交 N 条，`created + failed` 必须等于 N —— 少一条就是"凭空消失"。"""
    c = _client()
    monkeypatch.setattr(c, "request", lambda *a, **k: ["no-id"])
    res = c.create_strings([{"key": "k1", "original": "a"},
                            {"key": "k2", "original": "b"}], file_id=1)
    assert len(res["created"]) + len(res["failed"]) == 2
    assert all("没有 id" in f["error"] for f in res["failed"])
    c.close()


def test_create_strings_reads_id_from_normal_response(monkeypatch):
    c = _client()
    seq = iter([{"id": 101}, {"id": 102}])
    monkeypatch.setattr(c, "request", lambda *a, **k: next(seq))
    res = c.create_strings([{"key": "k1", "original": "a"},
                            {"key": "k2", "original": "b"}], file_id=1)
    assert res["created"] == [101, 102] and res["failed"] == []
    c.close()


# --------------------------------------------------- ④ 任务词条操作不能报假 0
def test_operate_task_strings_raises_on_unknown_shape(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "request", lambda *a, **k: {})
    with pytest.raises(ParaTranzError, match="无法识别"):
        c.operate_task_strings(1, "add", [1, 2, 3])
    c.close()


def test_operate_task_strings_accumulates_known_counts(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "request", lambda *a, **k: {"added": 2, "skipped": 1})
    got = c.operate_task_strings(1, "add", [1, 2, 3])
    assert got == {"added": 2, "removed": 0, "skipped": 1}
    c.close()


# --------------------------------------------------- ⑤ 建议接口不能谎报类型
def test_get_suggestions_unwraps_paged_response(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "request", lambda *a, **k: {"results": [{"text": "x"}]})
    got = c.get_suggestions(1)
    assert isinstance(got, list) and got == [{"text": "x"}]
    c.close()


def test_get_suggestions_keeps_bare_list(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "request", lambda *a, **k: [{"text": "y"}])
    assert c.get_suggestions(1) == [{"text": "y"}]
    c.close()
