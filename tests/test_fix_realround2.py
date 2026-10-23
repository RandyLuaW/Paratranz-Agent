"""第五轮（真实平台实测）发现的缺陷的回归测试，全部离线。

覆盖：
1. `create_file` / `delete_file` 的文件生命周期（上传建文件、平台解析出词条、删除）
2. 上传接口返回体是 `{"file": {...}}` 包一层
3. 同名文件不能重复创建
4. `list_announcements` 容忍分页包（已在 test_fix_review 里，这里补 styleguide 视角）
5. `map_concurrent` 的进度回调
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.client import ParaTranzError, _unwrap_file  # noqa: E402


def test_create_file_builds_strings_and_returns_file(client, fake):
    """防回归：上传建文件后，平台会按项目格式解析出词条。

    真实实测：上传一个 2 键的 JSON，
    平台自动建出 2 条 stage=0 的词条 —— 这是「导入全新文件」的正确入口，
    以前误以为「平台不能建文件」，只能往已有文件补 key。
    """
    payload = json.dumps({"hello": "Hello there.", "bye": "Goodbye."}).encode("utf-8")
    fi = client.create_file("__t__.json", payload)
    assert fi.name == "__t__.json"
    assert fi.total == 2

    strs = client.get_file_strings(fi.id)
    assert {s.key for s in strs} == {"hello", "bye"}
    assert all(int(s.stage) == 0 for s in strs), "新词条应为未翻译"


def test_create_file_response_is_wrapped(client, fake):
    """防回归：真实平台返回 `{"file": {...}}`，直接 model_validate 会炸。

    实测报错：`ValidationError: id Field required, input_value={'file': {...}}`。
    """
    assert _unwrap_file({"file": {"id": 7, "name": "a.json"}})["id"] == 7
    assert _unwrap_file({"id": 8, "name": "b.json"})["id"] == 8
    with pytest.raises(ValueError):
        _unwrap_file({"nonsense": 1})

    payload = json.dumps({"k": "v"}).encode("utf-8")
    fi = client.create_file("__wrapped__.json", payload)
    assert fi.id > 0


def test_create_file_rejects_duplicate_name(client, fake):
    """防回归：文件名与目录内已有文件冲突时平台 400，不能静默当成新建。"""
    payload = json.dumps({"a": "A"}).encode("utf-8")
    client.create_file("__dup__.json", payload)
    with pytest.raises(ParaTranzError) as ei:
        client.create_file("__dup__.json", payload)
    assert ei.value.status == 400


def test_delete_file_removes_strings(client, fake):
    """防回归：DELETE 文件必须连同它的词条一起消失（不能留孤儿词条）。"""
    payload = json.dumps({"x": "X marks the spot."}).encode("utf-8")
    fi = client.create_file("__del__.json", payload)
    before = len(fake.state["strings"])
    client.delete_file(fi.id)
    assert all(f.id != fi.id for f in client.list_files())
    assert len(fake.state["strings"]) < before
    assert not [s for s in fake.state["strings"] if s.get("fileId") == fi.id]


def test_styleguide_second_run_updates_same_announcement(client, cfg, store, fake, llm):
    """防回归（真实平台踩到）：公告列表是分页包，漏 unwrap 时第二次跑会新建重复公告。

    真实事故：`list_announcements` 抛 ValidationError → styleguide 的
    `except Exception: pass` 把异常吞了 → existing=None → 每天新建一条公告。
    """
    from paratranz_agent.pipelines.styleguide import StyleGuidePipeline

    pipe = StyleGuidePipeline(client, cfg, store, llm)
    first = pipe.publish()
    second = pipe.publish()
    assert first["action"] == "create"
    assert second["action"] == "update", "同标题公告存在时必须更新而不是新建"
    assert len(fake.state["announcements"]) == 1
    assert second["id"] == first["id"]


def test_map_concurrent_reports_progress():
    """防回归：批量任务必须有进度回调。

    真实事故：全量校对 9087 条静默跑了 20 分钟、零输出，被当成卡死中断。
    """
    from paratranz_agent.llm import BaseLLM

    class _Llm(BaseLLM):
        name = "t"

        def chat(self, messages, **kw):  # noqa: ANN001, ANN003
            return "ok"

    calls: list[tuple[int, int]] = []
    out = _Llm().map_concurrent([1, 2, 3], lambda x: x * 2, concurrency=2, on_done=lambda d, t: calls.append((d, t)))
    assert out == [2, 4, 6]
    assert calls == [(1, 3), (2, 3), (3, 3)]
