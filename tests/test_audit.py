"""审计发现的协议/边界问题，全部固化为回归测试。

每条都对应一个真实缺陷，不是"为了覆盖率"写的：
- 非幂等 POST 重试会造成重复任务；
- 平台忽略 page 参数会让分页迭代器死循环；
- 令牌桶容量=每分钟速率会让启动瞬间打爆 429；
- 项目未开任务功能时给出裸 403；
- state_dir 相对 cwd 解析，从别处调用会写错目录。
"""
from __future__ import annotations

import time

import httpx
import pytest

from paratranz_agent.client import ParaTranzClient, ParaTranzError, RateLimiter
from paratranz_agent.config import load_config
from paratranz_agent.pipelines import PlanPipeline
from paratranz_agent.qa import Severity, run_qa


def _client(handler, token: str = "test-token") -> ParaTranzClient:
    return ParaTranzClient(
        token=token,
        project_id=6896,
        base_url="https://paratranz.cn/api",
        transport=httpx.MockTransport(handler),
        rate_limit_per_min=100000,
    )


# ------------------------------------------------------- 非幂等请求的重试语义
def test_non_idempotent_post_not_retried_on_5xx():
    """create_task 遇 500 不能自动重试——服务端可能已经建了任务。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"message": "internal"})

    c = _client(handler)
    with pytest.raises(ParaTranzError):
        c.create_task("t", "x")
    assert calls["n"] == 1, f"非幂等 POST 被重试了 {calls['n']} 次"
    c.close()


def test_idempotent_get_is_retried_on_5xx():
    """幂等 GET 仍应重试，保证瞬时抖动不影响流程。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"message": "busy"})
        return httpx.Response(200, json={"id": 6896, "name": "p", "extra": {}})

    c = _client(handler)
    assert c.get_project().id == 6896
    assert calls["n"] == 3
    c.close()


def test_429_is_retried_even_for_non_idempotent():
    """429 表示请求被拒绝未处理，非幂等也应重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "too many"})
        return httpx.Response(
            200,
            json={"id": 1, "title": "t", "content": "", "status": 0, "goal": {}, "taskFiles": []},
        )

    c = _client(handler)
    assert c.create_task("t", "x").id == 1
    assert calls["n"] == 2
    c.close()


# ------------------------------------------------------------ 分页迭代器兜底
def test_iter_strings_stops_when_platform_ignores_page():
    """平台不翻页（永远返回同一页）时必须终止，不能无限循环。"""
    page_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page_calls["n"] += 1
        return httpx.Response(
            200,
            json=[{"id": i, "key": f"k{i}", "original": "x", "translation": "", "stage": 0}
                  for i in range(5)],
        )

    c = _client(handler)
    got = list(c.iter_strings(page_size=5))
    assert len(got) == 5, "重复页应该只取一次"
    assert page_calls["n"] <= 2, f"发起了 {page_calls['n']} 次请求，说明没及时终止"
    c.close()


def test_iter_strings_respects_max_items():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        size = int(request.url.params.get("pageSize", 100))
        base = (page - 1) * size
        return httpx.Response(
            200,
            json=[{"id": base + i, "key": "k", "original": "x", "translation": "", "stage": 0}
                  for i in range(size)],
        )

    c = _client(handler)
    assert len(list(c.iter_strings(page_size=10, max_items=25))) == 25
    c.close()


# ---------------------------------------------------------------- 令牌桶
def test_rate_limiter_allows_burst_then_throttles():
    """burst 内零等待，超出后按平均速率补充。"""
    rl = RateLimiter(per_minute=600, burst=10)  # 10/s

    t0 = time.time()
    for _ in range(10):
        rl.acquire()
    assert time.time() - t0 < 0.2, "burst 内不该等待"

    t0 = time.time()
    for _ in range(10):
        rl.acquire()
    elapsed = time.time() - t0
    assert elapsed >= 0.8, f"超出 burst 后未限速（{elapsed:.2f}s）"


def test_rate_limiter_burst_default_is_far_below_per_minute():
    """默认 burst 必须远小于每分钟额度，否则启动瞬间会打爆平台限流。"""
    rl = RateLimiter(per_minute=420)
    assert rl.burst <= 42, f"burst={rl.burst} 太大，启动会瞬间发出过多请求"


# -------------------------------------------------- 任务功能未开启时的行为
def test_plan_execute_rejects_when_task_feature_disabled(client, cfg, store, fake):
    fake.state["project"]["extra"]["task"] = False
    pipe = PlanPipeline(client, cfg, store)
    planned = pipe.build_plan()
    with pytest.raises(RuntimeError, match="任务"):
        pipe.execute(planned, dry_run=False)
    assert fake.state["tasks"] == []


def test_plan_dry_run_allows_when_task_feature_disabled(client, cfg, store, fake):
    """只预览方案时不写平台，不该被任务功能开关拦住。"""
    fake.state["project"]["extra"]["task"] = False
    pipe = PlanPipeline(client, cfg, store)
    assert pipe.execute(pipe.build_plan(), dry_run=True) == []


# --------------------------------------------------------- state_dir 解析
def test_state_dir_resolves_relative_to_config_file(tmp_path, monkeypatch):
    """从别的目录调用 ptz 时，状态目录应跟着配置文件走，而不是 cwd。"""
    conf_dir = tmp_path / "proj"
    conf_dir.mkdir()
    conf = conf_dir / "agent.yaml"
    conf.write_text(
        "project:\n  id: 1\napi:\n  token: x\nstate_dir: .ptz-state\n", encoding="utf-8"
    )

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    cfg = load_config(conf)
    # 相对 state_dir 按配置文件所在目录解析（不是 cwd，也不是项目根）
    assert cfg.state_path == (conf_dir / ".ptz-state").resolve()


# ------------------------------------------------------------- QA 边界输入
@pytest.mark.parametrize(
    "src,dst",
    [
        ("", ""),
        ("   ", "   "),
        ("a", "啊"),
        ("\\", "\\"),
        ("{" * 50, "{" * 50),
        ("%" * 30, "%" * 30),
        ("Hello " * 100, "你好 " * 100),
    ],
)
def test_qa_never_raises_on_extreme_input(src, dst):
    rep = run_qa(src, dst, block_on=Severity.ERROR)
    assert isinstance(rep.ok, bool)


# ------------------------------------------------------ token 不出现在异常里
def test_error_message_does_not_leak_token():
    secret = "SUPER_SECRET_TOKEN_123"
    c = _client(lambda r: httpx.Response(400, json={"message": "bad request"}), token=secret)
    with pytest.raises(ParaTranzError) as exc_info:
        c.get_project()
    assert secret not in str(exc_info.value)
    c.close()
