r"""第七轮（用户反馈的第一个线上 BUG + 三路全盘审计）的回归断言。

每条都写明「原来的坏法」，因为它们都属于同一类缺陷：
**看起来成功了，但事实不是那样**。

覆盖：
1. 「测试连接」拿已保存的配置去测界面上的输入 → 报错与眼前的值对不上账；
2. `ParaTranzError` 里是未替换的 `/projects/{projectId}`，且平台对
   「项目不存在 / 私有未加入 / Token 无效」回同一个 404 → 用户无法自诊；
3. 平台压低 pageSize 时 `len(batch) < page_size` 提前收工 → 只读第一页；
4. 2xx 空 body 被当成「0 条」；
5. TM 跨项目串味；
6. 非幂等 POST 在读超时被重试 → 平台重复写入；
7. 布尔入参 `"false"` 是 truthy → 越过危险动作闸门；
8. QA 数字用成员判断 → 少一个重复数字不报；`block_on=info` 让全部译文打回；
9. `\uXXXX` 代理对不合并 → 所有 emoji 词条永远判「原文不一致」；
10. 回写在写成功**之前**计数；编码失败把源文件截成 0 字节；
11. 限流值改了不生效（指纹漏字段）；
12. CommonEvents.json 是数组，事件指令一条都提不出来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.client import ParaTranzClient, ParaTranzError  # noqa: E402
from paratranz_agent.models import Stage  # noqa: E402
from paratranz_agent.qa import Severity, check_control_balance, check_numbers, run_qa  # noqa: E402
from paratranz_agent.store import Store  # noqa: E402


def _client(handler) -> ParaTranzClient:
    return ParaTranzClient(
        token="t",
        project_id=4780,
        base_url="https://paratranz.cn/api",
        transport=httpx.MockTransport(handler),
        rate_limit_per_min=100000,
    )


# ---------------------------------------------------------------- ① 报错要能自诊
def test_error_path_is_resolved_not_template():
    """原来的坏法：`[404] /projects/{projectId}: Not Found` —— 既不知道哪个项目，
    也不知道实际请求了哪个地址。"""
    c = _client(lambda req: httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(ParaTranzError) as exc:
        c.get_project()
    assert "/projects/4780" in str(exc.value)
    assert "{projectId}" not in str(exc.value)
    assert exc.value.url == "https://paratranz.cn/api/projects/4780"
    c.close()


def test_404_diagnose_says_not_in_public_catalog():
    """平台把三种原因合成一个 404；这里要给出「不在公开目录」这个事实。"""
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/api/projects/4780":       # 带 Token / 匿名都 404
            return httpx.Response(404, json={"message": "Not Found"})
        if req.url.path == "/api/projects":            # 公开目录里没有 4780
            return httpx.Response(
                200,
                json={"results": [{"id": 2, "name": "x"}], "page": 1,
                      "pageSize": 1000, "pageCount": 1, "rowCount": 1},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    c = _client(handler)
    hints = c.diagnose_project_access()
    text = "\n".join(hints)
    assert "不在公开项目目录" in text and "4780" in text
    assert any("/projects/4780" in p for p in calls), "必须真的去探测过"
    c.close()


def test_404_diagnose_flags_invalid_token_for_public_project():
    """不带 Token 却能读到 → 项目是公开的 → 带上 Token 还 404 就是 Token 的问题。"""
    def handler(req: httpx.Request) -> httpx.Response:
        authed = "authorization" in {k.lower() for k in req.headers.keys()}
        if req.url.path == "/api/projects/4780":
            return httpx.Response(404 if authed else 200, json={"id": 4780})
        return httpx.Response(404, json={"message": "Not Found"})

    c = _client(handler)
    text = "\n".join(c.diagnose_project_access())
    assert "Token 无效或已过期" in text
    c.close()


def test_settings_test_uses_unsaved_form_values(tmp_path, monkeypatch):
    """「测试连接」必须测**界面上正在填的值**。

    原来的坏法：端点不接收参数，只读 `settings.json` —— 改了编号/Token 不点保存
    就测，测的是上一次的值，用户看到「编号明明是对的却报 404」。
    """
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    app = create_app(state_dir=tmp_path / "state", mock=True)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        ctx = app.state.ctx
        seen: dict = {}
        real = ctx.client_for

        def spy(**kw):
            seen.update(kw)
            return real(**kw)

        monkeypatch.setattr(ctx, "client_for", spy)
        r = c.post(
            "/api/settings/test",
            json={"project_id": 4780, "token": "abc", "base_url": "https://paratranz.cn/api"},
        )
        assert r.status_code == 200
        assert seen.get("project_id") == 4780, f"没把界面上的编号传下去：{seen}"
        assert seen.get("token") == "abc"
        # 未保存的值绝不能顺手写盘
        assert c.get("/api/settings").json()["settings"]["project_id"] != 4780


# ------------------------------------------------------- ② 分页：平台压低 pageSize
def test_iter_strings_uses_pagecount_not_short_page():
    """原来的坏法：「这一页不足 pageSize 就是最后一页」。

    平台把 500 压成 2 条/页时，第 1 页就被当成最后一页 —— 整个项目只读到 2 条，
    而 translate 报「没有待翻译词条，完成」。
    """
    rows = [
        {"id": i, "key": f"k{i}", "original": f"o{i}", "translation": "",
         "stage": 0, "fileId": 1}
        for i in range(1, 6)
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        chunk = rows[(page - 1) * 2 : page * 2]
        return httpx.Response(
            200,
            json={"results": chunk, "page": page, "pageSize": 2,
                  "rowCount": len(rows), "pageCount": 3},
        )

    c = _client(handler)
    got = list(c.iter_strings(page_size=500))
    assert [s.id for s in got] == [1, 2, 3, 4, 5], "只读了第一页就是漏数据"
    c.close()


def test_empty_body_on_get_is_an_error_not_zero_rows():
    c = _client(lambda req: httpx.Response(200, content=b""))
    with pytest.raises(ParaTranzError, match="响应体为空"):
        c.get_project()
    with pytest.raises(ParaTranzError, match="响应体为空"):
        c.list_strings()
    c.close()


# --------------------------------------------------------------- ③ 非幂等不重试
def test_non_idempotent_post_is_not_retried_on_read_timeout():
    """读超时 ≠ 没送达：服务端可能已经建好了。

    原来的坏法：异常分支不看 `idempotent`，一律重试 —— 平台上出现两条同名任务。
    """
    hits = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        raise httpx.ReadTimeout("slow", request=req)

    c = _client(handler)
    with pytest.raises(ParaTranzError, match="可能已被平台执行"):
        c.create_task("t", "x")
    assert hits["n"] == 1, f"重试了 {hits['n']} 次"
    c.close()


# ------------------------------------------------------------- ④ TM 不能跨项目串味
def test_tm_is_scoped_by_project(tmp_path):
    s = Store(tmp_path / "agent.db")
    s.tm_add("Hello", "你好（A 项目）", project=111)
    assert s.tm_exact("Hello", project=111) == "你好（A 项目）"
    # 另一个项目不能白拿 A 的机翻
    assert s.tm_exact("Hello", project=222) is None
    # 通用池（人工确认）两个项目都能用
    s.tm_add("Hello", "你好（通用）", project=0)
    assert s.tm_exact("Hello", project=222) == "你好（通用）"
    s.close()


# -------------------------------------------------------------- ⑤ 布尔入参闸门
def test_string_false_does_not_pass_risk_gate():
    """`{"accept_risk": "false"}` 以前是 truthy，直接过闸。"""
    from fastapi import HTTPException

    from paratranz_agent.server.tools import require_risk_ack

    with pytest.raises(PermissionError):
        require_risk_ack("extract", "false")
    with pytest.raises(HTTPException):
        require_risk_ack("extract", 42)      # 认不出的值一律 400，不猜
    require_risk_ack("extract", "true")      # 显式 true 才放行
    require_risk_ack("status", "false")      # 非危险工具不受影响


def test_dry_run_string_false_is_really_false():
    from types import SimpleNamespace

    from paratranz_agent.server.tools import _dry_of

    ctx = SimpleNamespace(settings=SimpleNamespace(dry_run=True))
    assert _dry_of(ctx, {"dry_run": "false"}) is False
    assert _dry_of(ctx, {"dry_run": "true"}) is True
    assert _dry_of(ctx, {}) is True          # 没传就跟设置页


# ------------------------------------------------------------------- ⑥ QA 假通过
def test_qa_counts_numbers_as_multiset():
    """原文 `HP 10 / 10` → 译文 `生命 10`：成员判断认为「没缺数字」，假通过。"""
    rules = {i.rule for i in check_numbers("HP 10 / 10", "生命 10")}
    assert "number_mismatch" in rules
    rules2 = {i.rule for i in check_numbers("Gold 10", "金币 10 20")}
    assert "number_extra" in rules2


def test_block_on_info_does_not_fail_everything():
    """`block_on=info` 时 `max_severity(INFO) < INFO` 恒假 → 全项目 100% 打回。"""
    ok = run_qa("Hello", "你好", block_on=Severity.INFO)
    assert ok.ok is True
    bad = run_qa("Hi \\N[1]", "嗨", block_on=Severity.INFO)
    assert bad.ok is False


def test_percent_sign_in_percentage_is_not_stray():
    assert not [i for i in check_control_balance("命中率 50%", "命中率 50%")
                if i.rule == "percent_stray"]


# -------------------------------------------------------- ⑦ JSON 代理对（emoji）
def test_json_writer_merges_surrogate_pairs(tmp_path):
    """文件里 `"\\ud83d\\ude00"` 的解码值必须等于 😀（`json.loads` 也这么算）。

    原来的坏法：两个 `\\uXXXX` 各自 `chr()` 成孤立代理 → 扫描值长 1 个码位 →
    与平台记录的原文永远不相等 → 所有 emoji 词条都判 mismatched，一条都写不进去。
    """
    from paratranz_agent.writeback.apply import apply_items
    from paratranz_agent.writeback.base import WriteItem

    src = '{"0": {"name": "\\ud83d\\ude00 smile"}}'
    items = [WriteItem(key="x.json::0.name", translation="😀 微笑",
                       original=json.loads(src)["0"]["name"])]
    res = apply_items(src, items)
    assert not res.mismatched, res.mismatched
    assert res.written == ["x.json::0.name"]
    assert json.loads(res.content)["0"]["name"] == "😀 微笑"


# ------------------------------------------------------- ⑧ 回写：计数与截断
def test_writeback_counts_only_after_success(tmp_path):
    """原来的坏法：`rep.written += len(res.written)` 在写盘之前，
    写失败时报告仍宣称「写入 N 条 / M 个文件」，镜像模式还把源文件原样复制过去。"""
    from paratranz_agent.server import extraction as ex
    from paratranz_agent.writeback.pipeline import run_writeback

    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    f = game / "data" / "x.json"
    f.write_text('{"0": {"name": "Alice"}}', encoding="utf-8")

    state = tmp_path / "state"
    jid = ex.run_extractor(state, "generic-json", game).job_id

    # 让目标目录不可写：用「已存在同名文件占住父目录」制造 OSError
    out = tmp_path / "out"
    (out / "data").mkdir(parents=True)
    (out / "data" / "x.json").mkdir()               # 目录占位 → write 必失败

    class FakeClient:
        project_id = 1

        def iter_strings(self, **kw):
            from paratranz_agent.models import StringItem

            yield StringItem.model_validate(
                {"id": 1, "key": "data/x.json::0.name", "original": "Alice",
                 "translation": "爱丽丝", "stage": 5, "fileId": 1}
            )

    rep = run_writeback(
        client=FakeClient(), state_dir=state, job_id=jid, min_stage=3,
        out_dir=str(out), mode="patch", in_place=False, dry_run=False,
    )
    assert rep.written == 0, f"写失败了还报写入 {rep.written} 条"
    assert rep.failed_files == 1
    assert f.read_text(encoding="utf-8") == '{"0": {"name": "Alice"}}', "源文件被动了"


def test_unencodable_translation_does_not_truncate_source(tmp_path):
    """GBK 遇上 emoji：`write_text` 打开即截断 → 源文件变 0 字节。"""
    from paratranz_agent.writeback.pipeline import run_writeback
    from paratranz_agent.server import extraction as ex

    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    f = game / "data" / "x.json"
    original = '{"0": {"name": "Alice"}}'
    f.write_text(original, encoding="utf-8")

    state = tmp_path / "state"
    jid = ex.run_extractor(state, "generic-json", game, {"encoding": "gbk"}).job_id

    class FakeClient:
        project_id = 1

        def iter_strings(self, **kw):
            from paratranz_agent.models import StringItem

            yield StringItem.model_validate(
                {"id": 1, "key": "data/x.json::0.name", "original": "Alice",
                 "translation": "😀", "stage": 5, "fileId": 1}
            )

    rep = run_writeback(
        client=FakeClient(), state_dir=state, job_id=jid, min_stage=3,
        out_dir=str(tmp_path / "out"), mode="patch", in_place=False, dry_run=False,
    )
    assert rep.written == 0 and rep.failed_files == 1
    assert f.read_text(encoding="utf-8") == original, "编码失败把源文件写坏了"


# --------------------------------------------------------- ⑨ 改限流必须真的生效
def test_rate_limit_is_in_client_fingerprint(tmp_path):
    """原来的坏法：指纹不含 `rate_limit_per_min`，改完保存、界面回显新值，
    内存里的 limiter 还是旧速率（只有重启才生效）。"""
    from paratranz_agent.server.settings import SettingsStore
    from paratranz_agent.server.tools import AppContext

    store = SettingsStore(tmp_path / "settings.json")
    store.save({"rate_limit_per_min": 420})
    ctx = AppContext(store, tmp_path, mock=True)
    fp1 = ctx._fp()
    store.save({"rate_limit_per_min": 60})
    assert ctx._fp() != fp1


# ------------------------------------------------- ⑩ CommonEvents 是数组，不是 dict
def test_rpgmaker_extracts_common_events_array(tmp_path):
    """原来的坏法：事件指令只在 `isinstance(data, dict)` 分支里找 `list`，
    而 CommonEvents.json / Troops.json 的顶层是数组 → 一条都提不出来。"""
    import subprocess

    data = tmp_path / "www" / "data"
    data.mkdir(parents=True)
    (data / "CommonEvents.json").write_text(
        json.dumps([{"id": 1, "name": "", "list": [
            {"code": 401, "indent": 0, "parameters": ["Hello there, traveler."]},
            {"code": 0, "indent": 0, "parameters": []},
        ]}], ensure_ascii=False),
        encoding="utf-8",
    )
    (data / "Troops.json").write_text(
        json.dumps([{"id": 1, "pages": [{"list": [
            {"code": 401, "indent": 0, "parameters": ["The enemy appears!"]},
        ]}]}], ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "entries.jsonl"
    script = ROOT / "src/paratranz_agent/extractors/rpgmaker-mv/run.py"
    r = subprocess.run(
        [sys.executable, str(script), "--input", str(tmp_path), "--output", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    keys = [json.loads(line)["key"] for line in out.read_text(encoding="utf-8").splitlines()]
    assert any("CommonEvents.json" in k for k in keys), f"CommonEvents 没提出来：{keys}"
    assert any("Troops.json" in k for k in keys), f"Troops 没提出来：{keys}"


# ---------------------------------------------------------------- 附加：真实形状
def test_list_files_tolerates_paged_envelope():
    """`/files` 真实平台给裸数组，但它一旦改成分页包，旧实现会去遍历字典的 key。"""
    c = _client(lambda req: httpx.Response(
        200, json={"results": [{"id": 1, "name": "a.json"}], "pageCount": 1, "page": 1}
    ))
    files = c.list_files()
    assert [f.id for f in files] == [1]
    c.close()


def test_iter_issues_reads_all_pages():
    """review 找自己的打回台账帖必须跨页：只看第 1 页时会被挤出 → 每轮新建重复帖。"""
    pages = {
        1: [{"id": 1, "title": "a"}],
        2: [{"id": 2, "title": "[自动校对] 打回明细"}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        return httpx.Response(200, json={
            "results": pages.get(page, []), "page": page, "pageCount": 2, "pageSize": 1,
        })

    c = _client(handler)
    titles = [x["title"] for x in c.iter_issues(status=0, page_size=1)]
    assert "[自动校对] 打回明细" in titles
    c.close()


def test_stage_enum_untouched_by_writeback_guard():
    """回写门槛只接受 1/3/5（与设置页语义一致），别让 2（有疑问）漏进成品。"""
    assert {int(s) for s in (Stage.TRANSLATED, Stage.CHECKED, Stage.REVIEWED)} == {1, 3, 5}


def test_writeback_skips_disputed_stage():
    """stage=2「有疑问」是校对打回的标记，`--min-stage 1` 也不能把它写进成品。"""
    from paratranz_agent.models import StringItem
    from paratranz_agent.writeback.pipeline import pull_translations

    class FakeClient:
        project_id = 1

        def iter_strings(self, **kw):
            for i, stage in enumerate([1, 2, 3, 5, 9], start=1):
                yield StringItem.model_validate(
                    {"id": i, "key": f"k{i}", "original": f"o{i}",
                     "translation": f"t{i}", "stage": stage, "fileId": 1}
                )

    got, stats = pull_translations(FakeClient(), min_stage=1)
    assert "k2" not in got, "打回条目被写进了成品"
    assert stats["skipped_disputed"] == 1
    assert set(got) == {"k1", "k3", "k4", "k5"}, f"留下来的不对：{sorted(got)}"


def test_translate_counts_short_id_list_as_failed():
    """平台只确认一部分时必须记账：提交 3 → 确认 2，差额不能凭空消失。"""
    from paratranz_agent.client import BatchWrite
    from paratranz_agent.models import StringItem
    from paratranz_agent.pipelines.translate import TranslatePipeline

    class FakeStore:
        def tm_add_many(self, *a, **k):
            return 0

        def ledger_mark(self, *a, **k):
            pass

        def ledger_commit(self):
            pass

        def tm_exact(self, *a, **k):
            return None

    class FakeClient:
        project_id = 1

        def batch_edit_strings(self, items, chunk=0):
            ids = [it["id"] for it in items][:-1]      # 少确认一条
            return BatchWrite(ids, reported=len(ids))

    from paratranz_agent.config import AgentConfig

    cfg = AgentConfig.model_validate({"project": {"id": 1}})
    pipe = TranslatePipeline(FakeClient(), cfg, FakeStore(), None)   # type: ignore[arg-type]
    from paratranz_agent.pipelines.translate import TranslateStats

    st = TranslateStats()
    writes = [{"id": i, "translation": f"t{i}", "stage": 1} for i in (1, 2, 3)]
    by_id = {
        i: StringItem.model_validate(
            {"id": i, "key": f"k{i}", "original": f"o{i}", "translation": "",
             "stage": 0, "fileId": 1}
        )
        for i in (1, 2, 3)
    }
    pipe._write_chunks(writes, by_id, st)
    assert st.written == 2 and st.failed == 1, (st.written, st.failed)
    assert any("只确认" in w for w in st.warnings)
