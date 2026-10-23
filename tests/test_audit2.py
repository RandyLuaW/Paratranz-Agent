"""第二轮审计发现的缺陷的回归测试。

覆盖：
1. 提取模块的 id 路径穿越（读 / rmtree）
2. 恶意 extractor.yaml 的 id
3. normalize 对生成器输入的统计
4. SettingsStore 并发写不丢配置
5. 导入端点端到端（含假平台 translation 端点）
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.server.extraction import (  # noqa: E402
    ExtractionError,
    _job_root,
    get_job,
    install_extractor,
    job_log,
    load_entries,
    normalize,
    remove_extractor,
)
from paratranz_agent.server.settings import SettingsStore  # noqa: E402


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    st = tmp_path / "state"
    (st / "extractions").mkdir(parents=True)
    (st / "extractors").mkdir(parents=True)
    return st


@pytest.fixture()
def victim(state: Path) -> Path:
    """state 外面一个"受害者"目录，用于验证穿越被挡住。"""
    v = state.parent / "victim"
    v.mkdir()
    (v / "meta.json").write_text('{"job_id":"PWNED"}', encoding="utf-8")
    (v / "entries.jsonl").write_text('{"key":"x","original":"s"}\n', encoding="utf-8")
    (v / "run.log").write_text("secret", encoding="utf-8")
    (v / "extractor.yaml").write_text("id: pwned\n", encoding="utf-8")
    return v


@pytest.mark.parametrize("bad", ["../victim", "../../victim", "..\\victim", "/etc", "a/../../b"])
def test_job_id_traversal_blocked(state: Path, victim: Path, bad: str) -> None:
    for fn in (
        lambda: get_job(state, bad),
        lambda: load_entries(state, bad),
        lambda: job_log(state, bad),
    ):
        with pytest.raises(ExtractionError):
            fn()
    assert victim.exists()


@pytest.mark.parametrize("bad", ["../victim", "../../victim"])
def test_remove_extractor_traversal_blocked(state: Path, victim: Path, bad: str) -> None:
    """这条最危险：以前会 shutil.rmtree 掉 state 外的目录。"""
    with pytest.raises(ExtractionError):
        remove_extractor(state, bad)
    assert victim.exists(), "victim 被删了 —— 穿越防护失效"


def test_malicious_extractor_yaml_id_rejected(state: Path, tmp_path: Path) -> None:
    src = tmp_path / "evil"
    src.mkdir()
    (src / "extractor.yaml").write_text(
        "id: ../../pwned\nname: evil\nentry: run.py\n", encoding="utf-8"
    )
    (src / "run.py").write_text("print('x')\n", encoding="utf-8")
    with pytest.raises(ExtractionError):
        install_extractor(state, src)
    assert not (state.parent / "pwned").exists()


def test_normalize_handles_generator() -> None:
    gen = (e for e in [
        {"key": "a", "original": "x"},
        {"key": "b", "original": "y"},
        {"key": "a", "original": "x"},
    ])
    out, stats = normalize(gen)
    assert stats["input"] == 3
    assert len(out) == 2
    assert stats["dup_keys"] == 1


def test_settings_concurrent_writes_not_lost(tmp_path: Path) -> None:
    st = SettingsStore(tmp_path / "s.json")
    st.save({"project_id": 1})
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            st.upsert_provider({"id": f"c{i}", "api_key": "sk-x"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    ids = {p.id for p in st.load().providers}
    assert {f"c{i}" for i in range(12)} <= ids, f"并发写入丢失：{ids}"


def _wait(client, job_id: str, *, tries: int = 100) -> dict:
    import time

    job = {}
    for _ in range(tries):
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.1)
    return job


def test_import_endpoint_end_to_end(tmp_path: Path) -> None:
    """导入走 job：按平台真实语义，新增词条必须用 POST /strings。

    历史 bug：曾用 `POST /files/{id}/translation` 导入，而该接口
    **只更新已存在的词条**，新 key 会被静默忽略，用户看到 sent=N 但平台没变。
    """
    from fastapi.testclient import TestClient

    from paratranz_agent.server import create_app

    st = tmp_path / "state"
    app = create_app(state_dir=st, mock=True)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.put("/api/settings", json={"dry_run": False})

    jd = _job_root(st) / "job-x1"
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "entries.jsonl").write_text(
        "\n".join(
            json.dumps({"key": f"a.json::k{i}", "original": f"Text {i}", "context": "a"},
                       ensure_ascii=False)
            for i in range(3)
        ) + "\n",
        encoding="utf-8",
    )
    (jd / "meta.json").write_text('{"job_id":"job-x1","status":"done"}', encoding="utf-8")

    r = c.post("/api/extractions/job-x1/import", json={"file_id": 1, "filename": "x.json"})
    assert r.status_code == 200, r.text
    job = _wait(c, r.json()["job"]["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["created"] == 3, res
    assert res["failed_count"] == 0, res

    # 二次导入：全部跳过，不重复创建（说明 key 已在平台上）
    r2 = c.post("/api/extractions/job-x1/import", json={"file_id": 1, "filename": "x.json"})
    job2 = _wait(c, r2.json()["job"]["id"])
    assert job2["status"] == "done"
    assert job2["result"]["created"] == 0
    assert job2["result"]["skipped"] == 3, job2["result"]


def test_translation_endpoint_does_not_create_strings() -> None:
    """假平台要与真实一致：translation 接口不创建新 key，只更新已存在的。"""
    import httpx

    from paratranz_agent.fake import FakeParaTranz

    fake = FakeParaTranz()
    before = len(fake.state["strings"])

    # 1. 全新 key → 不创建
    req = httpx.Request(
        "POST",
        "https://paratranz.cn/api/projects/6896/files/1/translation",
        headers={"Authorization": "test-token"},
        json={"items": [{"key": "brand-new-key", "translation": "x"}], "filename": "x.json"},
    )
    res = fake.handler(req)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] == 0, body
    assert len(fake.state["strings"]) == before, "translation 接口不该创建词条"

    # 2. 已存在的 key → 更新译文
    existing = fake.state["strings"][0]
    req2 = httpx.Request(
        "POST",
        "https://paratranz.cn/api/projects/6896/files/%d/translation" % existing["fileId"],
        headers={"Authorization": "test-token"},
        json={"items": [{"key": existing["key"], "translation": "新译文"}],
              "filename": "x.json", "force": True},
    )
    res2 = fake.handler(req2)
    assert res2.status_code == 200
    assert res2.json()["updated"] == 1, res2.json()
    assert existing["translation"] == "新译文"
