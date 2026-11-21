"""词条工作台 + 模型网关兼容 + 「不许假成功」三组约束的回归测试。

背景（真实事故，2026-11-21）：用户在 WebUI 点了「翻译」，任务日志打出
`拉取未翻译词条，上限 200 条 → 处理 0 条 → 完成`，4 秒结束，界面显示成功，
而平台上一条都没动。两个原因叠在一起：

1. `llm.py` 把 `response_format` 硬写成 `{"type": "json_object"}`，而那家中转站
   只认 `{"type": "json_schema", "json_schema": {"strict": true}}`，
   于是**每一批**都在 1.4 秒内 400 失败；
2. `translate._translate_batch` 里 `except LLMError: return [], [], len(batch)`
   把失败原因吞了，`stats` 只有 `failed` 计数，而任务日志只打印
   `written/tm_hit/blocked` —— `failed` 从头到尾没出现在任何地方。

这里的测试就是为了让"这条链"再断的时候立刻红。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from paratranz_agent.config import LLMCfg
from paratranz_agent.llm import LLMError, OpenAICompatLLM
from paratranz_agent.pipelines.translate import TRANSLATE_SCHEMA, TranslatePipeline
from paratranz_agent.server.jobs import EventBus, JobRunner, ToolFailed


# --------------------------------------------------------------- 模型网关协商
def _gateway(seen: list[dict], *, reject: set[str], ok_body: str = '{"items":[]}'):
    """假网关：`reject` 里列出要拒绝的 response_format.type。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        rf = body.get("response_format") or {}
        seen.append(rf)
        if rf and rf.get("type") in reject:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format.type: Input should be 'json_schema'"}},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": ok_body}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    return handler


def _llm_with(handler, **over) -> OpenAICompatLLM:
    cfg = LLMCfg.model_validate(
        {"api_key": "k", "base_url": "https://gw.example/v1", "max_retries": 0, "timeout": 5, **over}
    )
    llm = OpenAICompatLLM(cfg)
    llm._client = httpx.Client(base_url="https://gw.example/v1", transport=httpx.MockTransport(handler))
    return llm


def test_json_object_rejected_falls_back_to_strict_schema():
    """网关只认 json_schema（且要 strict）时，必须自动降档并把这件事记下来。

    修复前：请求体里永远只有 `{"type": "json_object"}`，网关回 400，
    每一批都失败，而失败原因被吞掉。
    """
    seen: list[dict] = []
    llm = _llm_with(_gateway(seen, reject={"json_object"}))
    out = llm.chat([{"role": "user", "content": "x"}], json_mode=True, json_schema=TRANSLATE_SCHEMA)
    assert out == '{"items":[]}'
    assert [r.get("type") for r in seen] == ["json_object", "json_schema"]
    assert seen[1]["json_schema"]["strict"] is True, "strict 缺失时网关照样 400"
    assert seen[1]["json_schema"]["schema"] == TRANSLATE_SCHEMA
    assert llm.json_kind_schema == "schema"
    assert llm.json_warnings and "json_object" in llm.json_warnings[0]

    # 学到之后不再重撞：第二次调用直接从 schema 开始
    seen.clear()
    llm.chat([{"role": "user", "content": "x"}], json_mode=True, json_schema=TRANSLATE_SCHEMA)
    assert [r.get("type") for r in seen] == ["json_schema"]


def test_json_ladder_without_schema_is_object_then_plain():
    """不给 schema 的调用点（校对/术语/意图路由）没有中间档可退。"""
    seen: list[dict] = []
    llm = _llm_with(_gateway(seen, reject={"json_object"}))
    llm.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert [r.get("type") for r in seen] == ["json_object", None]


def test_json_ladder_reaches_plain_and_says_so():
    """object 与 schema 都被拒时退到不传 response_format，并留下警告。"""
    seen: list[dict] = []
    llm = _llm_with(_gateway(seen, reject={"json_object", "json_schema"}))
    assert llm.chat(
        [{"role": "user", "content": "x"}], json_mode=True, json_schema=TRANSLATE_SCHEMA
    ) == '{"items":[]}'
    assert seen[-1] == {}, "最后一档必须是不传 response_format"
    assert llm.json_kind_schema == "none"
    assert any("不传 response_format" in w for w in llm.json_warnings)


def test_json_ladder_all_rejected_raises_not_pretends():
    """连"不传"也被拒（网关坏）时必须抛错，不能返回空串当成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "response_format.type: bad"}})

    llm = _llm_with(handler)
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "x"}], json_mode=True, json_schema=TRANSLATE_SCHEMA)


def test_non_response_format_400_is_not_retried_as_ladder():
    """鉴权失败这种 400 不该被当成"换个 response_format 就能好"。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()).get("response_format") or {})
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    llm = _llm_with(handler)
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert len(seen) == 1


def test_translate_schema_is_strict_compatible():
    """strict:true 要求每个属性都在 required 里且 additionalProperties:false。"""
    item = TRANSLATE_SCHEMA["properties"]["items"]["items"]
    assert TRANSLATE_SCHEMA["additionalProperties"] is False
    assert set(TRANSLATE_SCHEMA["required"]) == {"items"}
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"id", "translation"}


# ------------------------------------------------------- 失败原因必须能看见
class _BoomLLM:
    """始终抛 LLMError 的替身（模拟网关 400 / 超时）。"""

    name = "boom"
    json_warnings: list[str] = []

    def available(self) -> bool:
        return True

    def chat(self, *a, **kw):
        raise LLMError("HTTP 400: response_format.type: Input should be 'json_schema'")

    def map_concurrent(self, items, fn, *, concurrency=4, on_done=None):
        out = []
        for i, it in enumerate(items):
            try:
                out.append(fn(it))
            except Exception as exc:  # noqa: BLE001 - 与真实实现一致：装进结果
                out.append(exc)
            if on_done:
                on_done(i + 1, len(items))
        return out


def test_translate_records_llm_failure_reason(client, cfg, store):
    """修复前：`stats.errors` 不存在，失败原因只剩一个计数。"""
    pipe = TranslatePipeline(client, cfg, store, _BoomLLM())  # type: ignore[arg-type]
    stats = pipe.run(dry_run=True)
    assert stats.fetched > 0
    assert stats.written == 0
    assert stats.failed > 0
    assert stats.errors, "模型调用失败的原因必须留在报表里"
    assert "LLMError" in stats.errors[0]


def test_translate_emits_per_item_events(client, cfg, store, llm):
    """逐条事件：先 running（界面才知道"正在处理哪些词条"），再终态。"""
    seen: list[dict] = []
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=True, on_items=lambda rows: seen.extend(rows))
    assert stats.fetched > 0
    assert any(r["status"] == "running" for r in seen)
    assert any(r["status"] in ("ok", "tm") for r in seen)
    assert all(r.get("id") is not None for r in seen)


def test_translate_dry_run_events_are_marked(client, cfg, store, llm):
    """演练模式的事件必须带 dry_run：模型产出了但平台没变，界面不能当已写入。"""
    seen: list[dict] = []
    pipe = TranslatePipeline(client, cfg, store, llm)
    pipe.run(dry_run=True, on_items=lambda rows: seen.extend(rows))
    assert seen and all(r.get("dry_run") is True for r in seen)
    assert not any(r["status"] == "written" for r in seen)


def test_review_emits_per_item_events(client, cfg, store, llm):
    """校对也要逐条上报：判定完成即推终态，写回后再推新的 stage。"""
    from paratranz_agent.pipelines.review import ReviewPipeline

    seen: list[dict] = []
    pipe = ReviewPipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=False, on_items=lambda rows: seen.extend(rows))
    assert stats.fetched > 0
    verdicts = {r["status"] for r in seen}
    assert verdicts & {"pass", "dispute", "reset", "skip"}, verdicts
    assert any(r.get("id") is not None for r in seen)
    # 真跑时写过 stage 的条目会带上新阶段号
    written = [r for r in seen if r["status"] == "written"]
    assert written and all(isinstance(r.get("stage"), int) for r in written)


def test_review_dry_run_events_are_marked(client, cfg, store, llm):
    from paratranz_agent.pipelines.review import ReviewPipeline

    seen: list[dict] = []
    pipe = ReviewPipeline(client, cfg, store, llm)
    pipe.run(dry_run=True, on_items=lambda rows: seen.extend(rows))
    assert seen and all(r.get("dry_run") is True for r in seen)
    assert not any(r["status"] == "written" for r in seen)


def test_extract_json_repairs_unescaped_quotes():
    """模型把中文引号打成半角 `"` 时，整批 JSON 不能就这么废掉。

    实测（校对二审）：`"reason":"原文是"递过衣物篮"，译文变成"走到法拉第笼旁""`
    —— 未转义的引号让 json.loads 直接失败，界面上只看到"无法解析"。
    """
    from paratranz_agent.llm import extract_json

    raw = (
        '```json\n{"items":[{"id":1,"verdict":"fail",'
        '"reason":"原文是"递过衣物篮"，译文变成"走到笼旁""},'
        '{"id":2,"verdict":"pass","reason":"正常"}]}\n```'
    )
    data = extract_json(raw)
    assert data["items"][0]["verdict"] == "fail"
    assert data["items"][0]["reason"] == '原文是"递过衣物篮"，译文变成"走到笼旁"'
    assert data["items"][1]["reason"] == "正常"


def test_extract_json_keeps_escaped_quotes_intact():
    """已经转义过的 `\\"` 不能被再转义一次。"""
    from paratranz_agent.llm import extract_json

    raw = '{"items":[{"id":1,"verdict":"pass","reason":"他说\\"好\\""}]}'
    assert extract_json(raw)["items"][0]["reason"] == '他说"好"'


def test_extract_json_repairs_invalid_escapes_for_rpg_controls():
    """RPG Maker 控制符 `\\N[1] \\V[2] \\C[3]` 在 JSON 里是**非法转义**。

    这是本项目的主场景：译文原样带控制符、模型没有转义反斜杠，
    整批就会以 `Invalid \\escape` 失败 —— 而错误信息只指向"模型输出无法解析"。
    """
    from paratranz_agent.llm import extract_json

    bs = chr(92)
    raw = '{"items":[{"id":1,"translation":"' + bs + "C[3]红字" + bs + "N[1] 和 " + bs + "V[2]\"}]}"
    got = extract_json(raw)["items"][0]["translation"]
    assert got == bs + "C[3]红字" + bs + "N[1] 和 " + bs + "V[2]"


def test_extract_json_repairs_literal_newlines_and_tabs():
    """多行译文常被写成真换行；JSON 不允许字符串里有裸控制字符。"""
    from paratranz_agent.llm import extract_json

    raw = '{"items":[{"id":1,"translation":"L1\nL2\tX"}]}'
    assert extract_json(raw)["items"][0]["translation"] == "L1\nL2\tX"


def test_extract_json_leaves_valid_json_untouched():
    """合法输出必须原样解析：修复器不能反过来改坏它。"""
    from paratranz_agent.llm import extract_json

    bs = chr(92)
    raw = '{"items":[{"id":1,"translation":"a' + bs + 'nb ' + bs + '"q' + bs + '" "}]}'
    assert extract_json(raw)["items"][0]["translation"] == 'a\nb "q" '


def test_review_accepts_verdict_alias(client, cfg, store):
    """模型用 `review` 当字段名时也要能判定，并把这件事记进日志。"""
    from fake_llm import FakeLLM

    from paratranz_agent.pipelines.review import ReviewPipeline

    logs: list[str] = []

    def responder(messages):
        import json as _json

        payload = _json.loads(messages[-1]["content"])
        return _json.dumps(
            {"items": [{"id": it["id"], "review": "pass", "reason": "ok"}
                       for it in payload["items"]]},
            ensure_ascii=False,
        )

    pipe = ReviewPipeline(client, cfg, store, FakeLLM(responder), log=logs.append)
    stats = pipe.run(dry_run=True)
    assert stats.skipped_no_verdict == 0
    assert stats.passed > 0
    assert any("别名键" in m for m in logs)


def test_translate_stops_when_budget_gate_trips(client, cfg, store, llm):
    """任务内预算检查点：以前只在任务开始查一次，长任务中途超支没人管。"""
    from paratranz_agent.errors import BudgetExceeded

    calls = {"n": 0}

    def gate() -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise BudgetExceeded("超出本次任务预算 0.50 USD")

    pipe = TranslatePipeline(client, cfg, store, llm, budget_check=gate)
    stats = pipe.run(dry_run=True)
    assert calls["n"] > 1, "预算检查必须每批都跑"
    assert stats.budget_stopped is True
    assert any("BudgetExceeded" in e for e in stats.errors)
    assert stats.written == 0


def test_review_stops_when_budget_gate_trips(client, cfg, store, llm):
    from paratranz_agent.errors import BudgetExceeded
    from paratranz_agent.pipelines.review import ReviewPipeline

    # 一批就拦：假平台里进入二审的条目只有一批，检查点必须在**调模型前**生效
    def gate() -> None:
        raise BudgetExceeded("超出本次任务预算")

    pipe = ReviewPipeline(client, cfg, store, llm, budget_check=gate)
    stats = pipe.run(dry_run=True)
    assert stats.budget_stopped is True
    assert any("BudgetExceeded" in e for e in stats.errors)


def test_extract_json_failure_says_why():
    """解析失败必须能分辨"被截断"与"模型写了坏 JSON"——处置完全不同。

    截断 → 调大 max_tokens / 少塞条目；坏 JSON → 换模型或改提示词。
    以前只报"无法解析 + 原文前 300 字"，两种情况长得一模一样。
    """
    from paratranz_agent.llm import LLMError, extract_json

    with pytest.raises(LLMError) as cut:
        extract_json('{"items":[{"id":1,"translation":"被截断')
    assert "截断" in str(cut.value) and "Unterminated" in str(cut.value)

    with pytest.raises(LLMError) as plain:
        extract_json("这不是 JSON")
    assert "截断" not in str(plain.value)
    assert "Expecting value" in str(plain.value)


# --------------------------------------------- 工具层：不许把失败报成完成
def test_why_nothing_written_prefers_real_reason():
    from paratranz_agent.pipelines.translate import TranslateStats
    from paratranz_agent.server.tools import _why_nothing_written

    s = TranslateStats(fetched=3, failed=3, errors=["LLMError: HTTP 400"])
    assert "HTTP 400" in _why_nothing_written(s)

    s = TranslateStats(fetched=5, blocked=5, blocked_samples=[{"reason": "占位符不一致"}])
    assert "QA" in _why_nothing_written(s) and "占位符" in _why_nothing_written(s)

    s = TranslateStats(fetched=7, skipped_manual=7)
    assert "force_overwrite=false" in _why_nothing_written(s)

    s = TranslateStats(fetched=0, files_unmatched=1)
    assert "白名单" in _why_nothing_written(s)


def test_hard_failure_vs_nothing_to_do():
    """「一条没写」要分清故障与无活可干 —— 后者报 failed 会让人以为软件坏了。"""
    from paratranz_agent.pipelines.translate import TranslateStats
    from paratranz_agent.server.tools import _is_hard_failure

    assert _is_hard_failure(TranslateStats(fetched=3, errors=["LLMError: 400"]))
    assert _is_hard_failure(TranslateStats(fetched=3, blocked=3))
    assert _is_hard_failure(TranslateStats(fetched=3, manual_risk=3))
    assert _is_hard_failure(TranslateStats(fetched=3, write_chunks_failed=1))
    # 全被别人抢先翻译：保护人工译文，不是故障
    assert not _is_hard_failure(TranslateStats(fetched=5, skipped_manual=5))


def test_job_marked_failed_when_tool_reports_no_result():
    """ToolFailed → 任务终态 failed，但报表要留下来。"""

    def boom(**_):
        raise ToolFailed("取材 200 条但一条没写", {"fetched": 200, "written": 0})

    runner = JobRunner(EventBus(), {"translate": boom})
    job = runner.submit("translate")
    for _ in range(200):
        got = runner.get(job.id)
        if got and got.status != "queued" and got.status != "running":
            break
        import time

        time.sleep(0.02)
    got = runner.get(job.id)
    assert got is not None
    assert got.status == "failed"
    assert "一条没写" in (got.error or "")
    assert got.result == {"fetched": 200, "written": 0}


def test_runner_item_events_reach_the_bus_and_the_job():
    runner = JobRunner(EventBus(), {"noop": lambda **_: {}})
    bus = runner.bus
    q = bus.subscribe()
    job = runner.submit("noop")
    runner.item(job, [{"id": 1, "original": "a", "status": "running"}])
    runner.item(job, [{"id": 1, "original": "a", "translation": "甲", "status": "ok"}])

    events = []
    while not q.empty():
        events.append(q.get_nowait())
    items = [e for e in events if e.get("type") == "item"]
    assert len(items) == 2
    assert items[-1]["items"][0]["status"] == "ok"
    assert list(job.items)[-1]["translation"] == "甲"


# --------------------------------------------------------------- 词条工作台 API
@pytest.fixture
def web(tmp_path):
    from paratranz_agent.server.app import create_app

    with TestClient(create_app(tmp_path, mock=True), base_url="http://127.0.0.1") as c:
        yield c


def _first_string(c) -> dict:
    d = c.get("/api/strings", params={"page": 1, "page_size": 5}).json()
    assert d["items"], "假平台应当有词条"
    return d["items"][0]


def test_strings_list_shape(web):
    d = web.get("/api/strings", params={"page": 1, "page_size": 5}).json()
    assert d["total"] > 0 and d["page"] == 1
    row = d["items"][0]
    for key in ("id", "original", "translation", "stage", "stage_zh", "file_name"):
        assert key in row


def test_strings_stage_filter_rejects_bad_value(web):
    assert web.get("/api/strings", params={"stage": "abc"}).status_code == 400


def test_string_detail_returns_history_field(web):
    row = _first_string(web)
    d = web.get(f"/api/strings/{row['id']}").json()
    assert d["item"]["id"] == row["id"]
    assert isinstance(d["history"], list)


def test_put_dry_run_echoes_without_writing(web):
    web.put("/api/settings", json={"dry_run": True})
    row = _first_string(web)
    r = web.put(f"/api/strings/{row['id']}", json={"translation": "演练值"})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert r.json()["would_write"]["translation"] == "演练值"
    # 平台侧没变
    assert web.get(f"/api/strings/{row['id']}").json()["item"]["translation"] == row["translation"]


def test_put_writes_and_reads_back(web):
    web.put("/api/settings", json={"dry_run": False})
    row = _first_string(web)
    r = web.put(f"/api/strings/{row['id']}", json={"translation": "人工改的译文", "stage": 1})
    assert r.status_code == 200
    assert r.json()["item"]["translation"] == "人工改的译文"
    assert r.json()["item"]["stage"] == 1
    # 回读必须是平台上的样子，而不是我们提交的那份
    again = web.get(f"/api/strings/{row['id']}").json()["item"]
    assert again["translation"] == "人工改的译文" and again["stage"] == 1


def test_put_requires_a_field(web):
    row = _first_string(web)
    assert web.put(f"/api/strings/{row['id']}", json={}).status_code == 400


def test_put_rejects_unknown_stage(web):
    row = _first_string(web)
    assert web.put(f"/api/strings/{row['id']}", json={"stage": 77}).status_code == 400


def test_records_endpoint(web):
    d = web.get("/api/records", params={"page_size": 5}).json()
    assert "items" in d


# --------------------------------------------------------------- 批量操作
def test_batch_stage_writes_and_reports_confirmed(web):
    web.put("/api/settings", json={"dry_run": False})
    rows = web.get("/api/strings", params={"page_size": 3}).json()["items"]
    ids = [r["id"] for r in rows[:2]]
    r = web.post("/api/strings/batch", json={"ids": ids, "action": "stage", "stage": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    assert body["count"] == 2 and body["confirmed"] == 2
    for sid in ids:
        assert web.get(f"/api/strings/{sid}").json()["item"]["stage"] == 3


def test_batch_clear_resets_translation_and_stage(web):
    web.put("/api/settings", json={"dry_run": False})
    rows = web.get("/api/strings", params={"page_size": 5}).json()["items"]
    target = next(r for r in rows if r["translation"])
    r = web.post("/api/strings/batch", json={"ids": [target["id"]], "action": "clear"})
    assert r.status_code == 200
    after = web.get(f"/api/strings/{target['id']}").json()["item"]
    assert after["translation"] == "" and after["stage"] == 0


def test_batch_respects_dry_run(web):
    web.put("/api/settings", json={"dry_run": True})
    rows = web.get("/api/strings", params={"page_size": 2}).json()["items"]
    ids = [r["id"] for r in rows]
    r = web.post("/api/strings/batch", json={"ids": ids, "action": "stage", "stage": 3})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    # 平台侧没被改
    assert web.get(f"/api/strings/{ids[0]}").json()["item"]["stage"] != 3


def test_batch_rejects_bad_input(web):
    assert web.post("/api/strings/batch", json={"ids": [], "action": "stage",
                                                "stage": 3}).status_code == 400
    assert web.post("/api/strings/batch", json={"ids": [1], "action": "nope"}).status_code == 400
    assert web.post("/api/strings/batch", json={"ids": ["x"], "action": "stage",
                                                "stage": 3}).status_code == 400
    assert web.post("/api/strings/batch", json={"ids": [1], "action": "stage",
                                                "stage": 77}).status_code == 400


def test_batch_writes_local_ledger(web):
    web.put("/api/settings", json={"dry_run": False})
    row = web.get("/api/strings", params={"page_size": 1}).json()["items"][0]
    web.post("/api/strings/batch", json={"ids": [row["id"]], "action": "stage", "stage": 3})
    d = web.get(f"/api/strings/{row['id']}").json()
    assert any("批量" in h["detail"] for h in d["local_history"])


def test_translate_accepts_explicit_ids(client, cfg, store, llm):
    """点名重翻：只翻给出的这些 id，且不再按阶段/白名单取材。"""
    ids = [1000 + 3, 1000 + 4]          # 假平台里 m4/m5：stage=0
    seen: list[dict] = []
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=True, ids=ids, on_items=lambda rows: seen.extend(rows))
    assert stats.fetched == 2
    assert {r["id"] for r in seen if r["status"] in ("ok", "tm")} <= set(ids)


def test_translate_ids_can_overwrite_existing_translation(client, cfg, store, llm):
    """点名重翻不走人工译文保护：人已经明确指了这几条。"""
    ids = [1000]                         # m1：已有译文、stage=1
    pipe = TranslatePipeline(client, cfg, store, llm)
    stats = pipe.run(dry_run=True, ids=ids)
    assert stats.fetched == 1
    assert stats.manual_risk == 0
    assert any("覆盖" in w for w in stats.warnings)


def test_manual_edit_lands_in_local_ledger(web):
    """平台 history 不记 API 写回，所以人工修改必须记进本地账本，否则记录是空的。"""
    web.put("/api/settings", json={"dry_run": False})
    row = _first_string(web)
    web.put(f"/api/strings/{row['id']}", json={"translation": "记账测试"})
    d = web.get(f"/api/strings/{row['id']}").json()
    assert any(h["action"] == "edit" and "人工" in h["detail"] for h in d["local_history"])


def test_detail_exposes_both_record_sources(web):
    """详情必须同时给平台 history 与本地账本 —— 只给一个都会让人误判成"没人动过"。"""
    row = _first_string(web)
    d = web.get(f"/api/strings/{row['id']}").json()
    assert "history" in d and "local_history" in d


# ------------------------------------------------- 概览数字必须是事实
def test_task_summary_counts_only_tasks_with_pending():
    """「平台任务 N 个进行中」不能说谎。

    实测（2026-11-22，项目 4780）：8 个任务全是 status=0（未结项），但其中一个
    「衣服」已经 0 条待翻译 —— 只按 status 计数会让用户以为有 8 个任务正在被处理。
    """
    from types import SimpleNamespace as NS

    from paratranz_agent.server.tools import _task_summary

    def task(tid, pairs):
        return NS(
            id=tid,
            status=0,
            taskFiles=[NS(total=t, translated=tr) for t, tr in pairs],
        )

    tasks = [
        task(1, [(100, 100)]),          # 已译完却没结项
        task(2, [(200, 190), (50, 50)]),  # 还有 10 条
        task(3, [(30, 0)]),             # 还有 30 条
        NS(id=4, status=1, taskFiles=[NS(total=9, translated=0)]),  # 已完成任务不计入
    ]
    got = _task_summary(tasks)
    assert got == {"open": 3, "with_pending": 2, "pending_strings": 40}


def test_project_payload_exposes_task_summary(web):
    d = web.get("/api/project").json()
    assert "task_summary" in d
    assert set(d["task_summary"]) == {"open", "with_pending", "pending_strings"}
