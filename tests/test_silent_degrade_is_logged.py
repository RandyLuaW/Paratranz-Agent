"""第六轮审计：把「静默降级」改成「降级但留痕」的回归测试。

降级本身有时是对的（拉不到样例不该让整份规范生成失败），但**必须留下痕迹**，
否则事后无法区分「平台确实没有」和「我们没读到」。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ------------------------------------------------ status：0 与「没读出来」要能区分
def test_status_reports_read_errors_instead_of_silent_zero(monkeypatch, tmp_path):
    """`list_tasks` / `iter_terms` 失败时，报表里必须能看出是「读失败」而不是「0」。"""
    from paratranz_agent.server.settings import SettingsStore
    from paratranz_agent.server.tools import AppContext, tool_status

    store = SettingsStore(tmp_path / "settings.json")
    ctx = AppContext(store, tmp_path, mock=True)
    try:
        c = ctx.client

        class Proj:
            id = 1
            name = "demo"
            source = "en"
            dest = "zh-CN"
            review_mode = 1
            extra: dict = {}

        class F:
            total = 1
            translated = 1
            checked = 0
            reviewed = 0
            disputed = 0
            words = 1
            pending = 0
            dir = ""
            name = "a.txt"
            id = 1

        monkeypatch.setattr(c, "get_project", lambda: Proj())
        monkeypatch.setattr(c, "list_files", lambda: [F()])

        def boom(*a, **k):
            raise RuntimeError("平台 500")

        monkeypatch.setattr(c, "list_tasks", boom)
        monkeypatch.setattr(c, "iter_terms", boom)

        got = tool_status(ctx)
        assert got["tasks"] == [] and got["terms"] == 0
        assert "平台 500" in got["tasks_error"], got["tasks_error"]
        assert "平台 500" in got["terms_error"], got["terms_error"]
    finally:
        ctx.close()


def test_status_errors_are_empty_when_read_succeeds(monkeypatch, tmp_path):
    from paratranz_agent.server.settings import SettingsStore
    from paratranz_agent.server.tools import AppContext, tool_status

    store = SettingsStore(tmp_path / "settings.json")
    ctx = AppContext(store, tmp_path, mock=True)
    try:
        got = tool_status(ctx)  # 假平台：两张表都能读
        assert got["tasks_error"] == "" and got["terms_error"] == ""
    finally:
        ctx.close()


# ------------------------------------------------ styleguide：样例读不到要留痕
def test_styleguide_records_sample_read_failure():
    """拉样例失败不该让整份规范生成失败，但返回值里要能看出样例为什么是空的。"""

    class Boom:
        def get_project(self):
            class P:
                name = "Demo"

            return P()

        def list_files(self):
            class F:
                total = 1
                translated = 1
                checked = 0
                reviewed = 0
                words = 10
                name = "a.json"
                dir = ""

            return [F()]

        def list_terms(self, **kw):
            return []

        def iter_strings(self, **kw):
            raise RuntimeError("网络断了")

    from paratranz_agent.config import AgentConfig
    from paratranz_agent.pipelines.styleguide import StyleGuidePipeline

    class NoStore:
        def ledger_mark(self, *a, **k):
            pass

        def ledger_commit(self):
            pass

        def term_all(self):
            return {}

    pipe = StyleGuidePipeline(Boom(), AgentConfig.model_validate({"project": {"id": 1}}),
                              NoStore(), None)
    md = pipe.build(with_llm=False)          # 不抛异常
    assert md.startswith("# ")               # 规范照样生成
    assert "网络断了" in pipe._samples_error  # 但原因被记下来了


# ------------------------------------------------ review：台账帖查不到要留痕
def test_review_marks_issue_lookup_failure():
    """查不到台账帖会新建一条（可能重复），至少要把它写进 stats 而不是静默。"""
    from paratranz_agent.pipelines.review import ReviewStats

    class Boom:
        def list_issues(self, **kw):
            raise RuntimeError("讨论区不可用")

    from paratranz_agent.config import AgentConfig
    from paratranz_agent.pipelines.review import ReviewPipeline

    class NoStore:
        def ledger_mark(self, *a, **k):
            pass

        def ledger_commit(self):
            pass

    pipe = ReviewPipeline(Boom(), AgentConfig.model_validate({"project": {"id": 1}}), NoStore(), None)
    stats = ReviewStats()
    assert pipe._find_digest_thread(stats) is None
    assert "重复" in stats.issue_error and "讨论区不可用" in stats.issue_error
