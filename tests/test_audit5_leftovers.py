"""审计报告5 遗留项的回归测试（7 条，每条对应一个真实缺陷）。

共同点：这些缺陷的坏法都是**静默**的 —— 不报错、只是结果不对或悄悄重复，
所以必须靠断言钉住，不能靠"看着跑通了"。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.writeback.base import WriteItem  # noqa: E402
from paratranz_agent.writeback.json_path import (  # noqa: E402
    JsonScanError,
    apply_json,
    scan_strings,
)


# ---------------------------------------------------------------- 1. BOM / NaN
def test_bom_prefixed_json_is_writable():
    """带 UTF-8 BOM 的 JSON 必须照常写回（原先整份判成非法、一条都写不进）。"""
    src = '\ufeff{"a": "Hello there", "b": "keep"}'
    res = apply_json(src, [WriteItem(key="f.json::a", translation="你好", original="Hello there")])
    assert res.errors == []
    assert res.written == ["f.json::a"]
    # BOM 必须原样留在文件开头（游戏读文件时认它）
    assert res.content.startswith("\ufeff")
    assert '"你好"' in res.content
    assert json.loads(res.content.lstrip("\ufeff"))["b"] == "keep"


def test_bom_offsets_stay_correct():
    """BOM 跳过只影响解析起点，不改动其它字面量的区间。"""
    src = '\ufeff{"k": "abc", "v": "def"}'
    spans = scan_strings(src)
    assert spans[("k",)][2] == "abc"
    assert spans[("v",)][2] == "def"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_numbers_do_not_break_scan(literal):
    """`NaN` / `Infinity` 是 JS 侧真实存在的写法，不能把整份文件判死。"""
    src = '{"score": ' + literal + ', "name": "Alice", "note": "keep"}'
    res = apply_json(src, [WriteItem(key="f.json::name", translation="爱丽丝", original="Alice")])
    assert res.errors == [], res.errors
    assert res.written == ["f.json::name"]
    assert literal in res.content          # 非字符串值一个字节都不动
    assert '"keep"' in res.content


def test_real_syntax_error_still_raises():
    """放宽 NaN/Infinity 之后，真正的语法错误仍要报出来。"""
    src = '{"a": 1, "c": "x"}'
    assert scan_strings(src)[("c",)][2] == "x"
    with pytest.raises(JsonScanError):
        scan_strings('{"a": 1, }')


# ---------------------------------------------------------------- 2. SQLite 并发
def test_sqlite_allows_concurrent_writer(tmp_path):
    """第二个连接（模拟命令行进程）不能立刻拿到 `database is locked`。

    SQLite 默认等待只有 5 秒且没有 busy handler 保证；WebUI 常驻 + CLI 同时跑
    是日常操作，所以这里显式开 WAL 并放宽等待。
    """
    from paratranz_agent.store import Store, open_sqlite

    db_path = tmp_path / "agent.db"
    a = Store(db_path)
    try:
        a.tm_add("hello", "你好", origin="t")
        b = open_sqlite(db_path)
        try:
            b.execute("CREATE TABLE IF NOT EXISTS probe(x INTEGER)")
            b.execute("INSERT INTO probe VALUES (1)")
            b.commit()
        finally:
            b.close()
        mode = a.db.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal", f"journal_mode={mode}"
        busy = a.db.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(busy) >= 20000, f"busy_timeout={busy}"
    finally:
        a.close()


# ---------------------------------------------------------------- 3. 畸形入参 400
def test_bad_body_params_return_400_not_500():
    """畸形数字入参必须是 400（客户端错），不能是 500（服务端错）。"""
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    with TestClient(create_app(mock=True), base_url="http://127.0.0.1") as c:
        r = c.put("/api/usage/pricing", json={"gpt-4o-mini": {"input": "not-a-number"}})
        assert r.status_code == 400, r.text
        assert "input" in r.text

        r2 = c.post("/api/extractions/none/import", json={"file_id": "abc", "dry_run": True})
        assert r2.status_code == 400, r2.text


# ---------------------------------------------------------------- 4. 取消保留结果
def test_cancelled_job_keeps_partial_result():
    """取消发生在「写完平台之后」时，已产出的统计必须留在 result 里。"""
    from paratranz_agent.server.jobs import EventBus, JobRunner

    def slow_tool(job, runner, **kw):
        # 模拟 pipeline 在检查点发现取消，但本轮已经写了一些
        runner._cancel.add(job.id)  # noqa: SLF001 - 测试直接置取消标记
        return {"written": 37, "skipped": 3}

    runner = JobRunner(EventBus(), {"slow": slow_tool})
    job = runner.submit("slow", {})
    for _ in range(300):
        j = runner.get(job.id)
        if j and j.status in ("done", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert j.status == "cancelled"
    assert j.result == {"written": 37, "skipped": 3}, "取消时不能丢掉已经写进去的结果"


# ---------------------------------------------------------------- 5. 源级请求头
def test_provider_headers_reach_chat_client():
    """`provider.headers` 必须真的进 HTTP 头（原先只在探测模型列表时生效）。"""
    from paratranz_agent.config import LLMCfg
    from paratranz_agent.llm import OpenAICompatLLM

    llm = OpenAICompatLLM(LLMCfg(base_url="https://example.invalid/v1", api_key="k-1",
                                 headers={"X-Title": "ptz", "HTTP-Referer": "https://x"}))
    try:
        headers = {k.lower(): v for k, v in llm._client.headers.items()}  # noqa: SLF001
        assert headers["x-title"] == "ptz"
        assert headers["http-referer"] == "https://x"
        # 自己的密钥不能被自定义头顶掉
        assert headers["authorization"] == "Bearer k-1"
    finally:
        llm.close()


def test_provider_headers_in_llm_cache_fingerprint(tmp_path):
    """改了请求头必须重建 LLM 实例，否则界面改了也不生效。"""
    from paratranz_agent.server.settings import SettingsStore
    from paratranz_agent.server.tools import AppContext

    store = SettingsStore(tmp_path / "settings.json")
    store.save({"providers": [{
        "id": "p1", "base_url": "https://example.invalid/v1", "api_key": "k-1",
        "headers": {"X-Title": "a"},
        "models": [{"id": "m1", "enabled": True}],
    }], "routing": {"default": "p1::m1"}})
    ctx = AppContext(store, tmp_path, mock=True)
    try:
        first = ctx.llm_for("translate")
        assert first is ctx.llm_for("translate"), "配置没变却重建了调用客户端"
        store.save({"providers": [{
            "id": "p1", "base_url": "https://example.invalid/v1", "api_key": "k-1",
            "headers": {"X-Title": "b"},
            "models": [{"id": "m1", "enabled": True}],
        }]})
        changed = ctx.llm_for("translate")
        assert changed is not first, "改了请求头却没重建客户端"
    finally:
        ctx.close()


# ---------------------------------------------------------------- 6. 公告不重复
class _NoStore:
    def ledger_mark(self, *a, **k):
        pass

    def ledger_commit(self):
        pass


class _NoLLM:
    def available(self):
        return False


def _pipe(client, name: str = "Demo"):
    from paratranz_agent.config import AgentConfig
    from paratranz_agent.pipelines.styleguide import StyleGuidePipeline

    cfg = AgentConfig.model_validate({"project": {"id": 1, "name": name}})
    return StyleGuidePipeline(client, cfg, _NoStore(), _NoLLM())


def test_styleguide_refuses_to_publish_when_listing_fails():
    """列不出公告时必须中止 —— 静默当成「没有」会重复发一条同名公告。"""

    class Boom:
        def list_announcements(self, **kw):
            raise RuntimeError("平台 500")

    with pytest.raises(RuntimeError, match="重复"):
        _pipe(Boom()).publish("# 规范正文", with_llm=False, dry_run=True)


def test_styleguide_refuses_when_project_name_unreadable():
    """拿不到项目名时标题不可信（会拼成「本项目 汉化规范」），必须中止。"""

    class NoName:
        def list_announcements(self, **kw):
            return []

        def get_project(self):
            raise RuntimeError("网络断了")

    with pytest.raises(RuntimeError, match="项目名"):
        _pipe(NoName(), name="").publish("# 规范正文", with_llm=False, dry_run=True)
