"""审计报告5「服务端 13 条低危」的回归测试。

这些问题的坏法都很小，但都属于「看起来正常、实际在骗人」那一类：
错误码用错、上限没有、白名单开了个恒真的口子、死配置让人以为生效了。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    with TestClient(create_app(tmp_path, mock=True), base_url="http://127.0.0.1") as c:
        yield c


# --------------------------------------------------- 低 1：错误体只有一种形状
def test_validation_error_uses_same_shape_as_business_error(client):
    """422 也要是 `{"error", "fields"}`，不能是 FastAPI 默认的 detail 数组。

    默认响应里 `detail` 是**数组**，前端 `data.error || data.detail` 会把它
    塞进 toast，渲染成 `[object Object]`。
    """
    r = client.post("/api/jobs", json={})
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"error", "fields"}
    assert isinstance(body["error"], str) and body["error"]
    assert body["fields"] and "tool" in body["fields"][0]


# --------------------------------------------------- 低 2：任务字典与日志有上限
def test_job_registry_and_logs_are_bounded():
    """`_order` 有 maxlen 而 `jobs` 没有 → 长跑时字典无上限。

    这里把上限临时调到很小，验证「溢出时字典同步淘汰」和「单任务日志有上限」。
    """
    from paratranz_agent.server import jobs as jobs_mod
    from paratranz_agent.server.jobs import EventBus, JobRunner

    def noop(job, runner, **kw):
        return {"ok": True}

    runner = JobRunner(EventBus(), {"noop": noop})
    try:
        assert jobs_mod.MAX_JOBS >= 10
        # 直接灌满到上限 + 5，检查字典不会比 _order 多
        for _ in range(jobs_mod.MAX_JOBS + 5):
            runner.submit("noop", {})
        assert len(runner.jobs) <= jobs_mod.MAX_JOBS, "jobs 字典没有跟着 _order 淘汰"
        # 日志上限：塞满再截
        job = runner.submit("noop", {})
        for i in range(jobs_mod.MAX_JOB_LOGS + 50):
            runner.log(job, f"line {i}")
        assert len(job.logs) == jobs_mod.MAX_JOB_LOGS
        last = runner.get(job.id).as_dict()["logs"][-1]["msg"]
        assert last == f"line {jobs_mod.MAX_JOB_LOGS + 49}", last
    finally:
        pass


# --------------------------------------------------- 低 3：limit 有边界
@pytest.mark.parametrize("bad", ["0", "-1", "9999"])
def test_limit_out_of_range_is_rejected(client, bad):
    """负数是真会出错的值：`[-0:]` 返回全部、`[-1:]` 把最新一条切掉，
    而 SQLite 的 `LIMIT -1` 又是不限制 —— 三种语义各不相同，必须挡在入口。"""
    assert client.get(f"/api/jobs?limit={bad}").status_code == 422
    assert client.get(f"/api/usage/recent?limit={bad}").status_code == 422


def test_limit_in_range_still_works(client):
    assert client.get("/api/jobs?limit=1").status_code == 200
    assert client.get("/api/logs?limit=5").status_code == 200


# --------------------------------------------------- 低 4：空 Host 不再放行
def test_empty_host_is_rejected(client):
    """`LOCAL_HOSTS` 里曾有 `""`，等于给「不写 Host 的裸请求」开恒真口子。"""
    assert client.get("/api/health", headers={"Host": ""}).status_code == 403
    assert client.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 200


# --------------------------------------------------- 低 6：回写报告的路径安全
def test_writeback_store_rejects_dot_paths(tmp_path):
    """`_safe("..")` 曾是原样返回 → `<state>/writeback/../last.json` 落到 state 根。"""
    from paratranz_agent.server.writeback_store import _safe, load_last, save_last

    assert _safe("..") == "unknown"
    assert _safe(".") == "unknown"
    assert _safe("") == "unknown"
    # 读侧对畸形 id 只返回 None（只读查询不该 500），但也不会读 state 之外的文件
    assert load_last(tmp_path, "..") is None
    assert load_last(tmp_path, "a/b") is None
    # 正常 id 照旧
    p = save_last(tmp_path, "20261024-001122-abcd", {"written": 1})
    assert p.is_file()
    assert load_last(tmp_path, "20261024-001122-abcd")["report"]["written"] == 1
    # 写入侧对畸形 id 直接拒绝（ExtractionError，不是任何异常都算）
    from paratranz_agent.server.extraction import ExtractionError

    with pytest.raises(ExtractionError):
        save_last(tmp_path, "..", {"written": 1})


# --------------------------------------------------- 低 7：死配置已删除
def test_system_prompt_prefix_is_gone():
    """它既不进 `prompt_for()` 也不进 `to_agent_config()`，留着只会误导人。"""
    from paratranz_agent.server.settings import RuntimeSettings

    assert "system_prompt_prefix" not in RuntimeSettings.model_fields
    s = RuntimeSettings()
    assert s.prompt_for("translate") == ""  # 真正生效的是 prompts.prefix


# --------------------------------------------------- 低 8：当前模型按路由解析
def test_usage_summary_reports_resolved_model(client):
    """原来读 `llm_model`（旧单模型字段的默认值），与实际调用模型不符。"""
    # providers / routing 各有专用端点，不走 PUT /api/settings（补丁模型排除它们）；
    # 模型也必须单独加 —— `upsert_provider` 不看嵌套的 models 列表。
    assert client.post("/api/providers", json={
        "id": "p1", "base_url": "https://example.invalid/v1", "api_key": "k-1",
    }).status_code == 200
    assert client.post("/api/providers/p1/models",
                       json={"id": "m-9", "enabled": True}).status_code == 200
    assert client.put("/api/routing", json={"default": "p1::m-9"}).status_code == 200
    got = client.get("/api/usage/summary").json()["current_model"]
    assert got == "p1::m-9", got


# --------------------------------------------------- 低 9：提取器拿不到凭据
def test_secret_env_not_passed_to_extractor(monkeypatch):
    """提取器脚本 `print(os.environ)` 曾能把 PTZ_API_TOKEN 写进 run.log，
    而 run.log 会明文回显给前端 —— 绕开了日志脱敏。"""
    from paratranz_agent.server.extraction.runner import _is_secret_env

    for name in ("PTZ_API_TOKEN", "PTZ_LLM_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN",
                 "MY_API_KEY", "DB_PASSWORD", "SOME_SECRET"):
        assert _is_secret_env(name), name
    for name in ("PATH", "PYTHONPATH", "PTZ_INPUT", "PTZ_OUTPUT", "PTZ_OPTIONS", "TEMP"):
        assert not _is_secret_env(name), name


def test_extraction_log_is_redacted(client, tmp_path):
    """日志来自子进程 stdout，是唯一不经过 LogStore 的文本通道，要单独脱敏。"""
    from paratranz_agent.server.routes.extract import _redact_log

    class Ctx:
        class settings:  # noqa: N801 - 只用到 token / llm_key / providers
            token = "real-platform-token-abcdef"
            llm_key = "sk-shouldnotappear123456"
            providers: list = []

    out = _redact_log(Ctx, "token=real-platform-token-abcdef key sk-shouldnotappear123456 ok")
    assert "real-platform-token-abcdef" not in out
    assert "sk-shouldnotappear123456" not in out
    assert "ok" in out


# --------------------------------------------------- 低 11：保留参数名
@pytest.mark.parametrize("bad", ["ctx", "job", "runner"])
def test_reserved_arg_names_rejected(client, bad):
    """`args` 里塞保留名会变成 `TypeError: got multiple values for argument`——
    任务 failed 且把内部函数签名回显给调用方。"""
    r = client.post("/api/jobs", json={"tool": "translate", "args": {bad: 1}})
    assert r.status_code == 400
    assert bad in r.json()["error"]


# --------------------------------------------------- 低 12：设置写入校验值域
@pytest.mark.parametrize("body", [
    {"review_block_on": "garbage"},
    {"project_id": -1},
    {"max_strings_per_task": -5},
    {"writeback_min_stage": 99},
    {"writeback_mode": "weird"},
    {"review_pass_stage": 2},
])
def test_settings_patch_validates_ranges(client, body):
    r = client.put("/api/settings", json=body)
    assert r.status_code == 422, r.text
    assert set(r.json()) == {"error", "fields"}


def test_settings_patch_accepts_legal_values(client):
    r = client.put("/api/settings", json={"review_block_on": "warn",
                                          "writeback_min_stage": 5,
                                          "writeback_mode": "patch",
                                          "project_id": 15382,
                                          "batch_size": 30})
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["review_block_on"] == "warn" and s["writeback_min_stage"] == 5
    assert s["project_id"] == 15382 and s["batch_size"] == 30


# --------------------------------------------------- 低 13：extensions 两处瑕疵
def test_skill_desc_falls_back_to_frontmatter_description(tmp_path):
    """三元优先级写错时，「有 frontmatter description 但正文为空」会让 desc 变空串。"""
    from paratranz_agent.server.extensions import ExtensionStore

    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: Demo\ndescription: 来自 frontmatter\n---\n",
                                     encoding="utf-8")
    store = ExtensionStore(tmp_path)
    got = [e for e in store.skills() if e.id == "demo"]
    assert got and got[0].desc == "来自 frontmatter", got
