"""「上传为新文件」的回归测试。

这个能力此前只在 client 里有个 `create_file`，没有任何调用方 —— 界面上也
没有入口，等于不存在。这里钉住三件事：产物形状、强制预览、以及「格式假设」
必须被写在返回里而不是让用户以为一定成功。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

JID = "20261024-000000-up001"
ENTRIES = [
    {"key": "data/A.json::[0].name", "original": "Alice", "context": "name"},
    {"key": "data/A.json::[1].name", "original": "Bob", "context": "name"},
]


@pytest.fixture()
def app_dir(tmp_path):
    """造一条提取记录（真实目录结构，不 mock 存储层）。"""
    from paratranz_agent.server.extraction import JOB_DIR

    root = tmp_path / JOB_DIR / JID
    root.mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({
        "job_id": JID, "extractor_id": "generic-json", "game_dir": str(tmp_path),
        "status": "done", "entries": len(ENTRIES), "options": {},
    }), encoding="utf-8")
    (root / "entries.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ENTRIES), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client(app_dir):
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    with TestClient(create_app(app_dir, mock=True), base_url="http://127.0.0.1") as c:
        yield c


# ------------------------------------------------------------------ 产物形状
def test_upload_payload_is_key_to_original_json():
    """产出 `{key: original}` 的 JSON 对象：key 原样保留（回写要靠它定位）。"""
    from paratranz_agent.server.extraction import export_upload_json

    blob = export_upload_json(ENTRIES)
    data = json.loads(blob.decode("utf-8"))
    assert data == {"data/A.json::[0].name": "Alice", "data/A.json::[1].name": "Bob"}
    # 同样输入必须产生同样字节（便于比对与去重）
    assert export_upload_json(ENTRIES) == blob


def test_upload_payload_is_sorted_and_utf8():
    from paratranz_agent.server.extraction import export_upload_json

    blob = export_upload_json(list(reversed(ENTRIES)))
    text = blob.decode("utf-8")
    assert text.index("::[0]") < text.index("::[1]")
    # 不转义成 \uXXXX：平台按 UTF-8 读
    assert "Alice" in text


# ------------------------------------------------------------------ 必须先预览
def test_preview_returns_content_and_format_warning(client):
    r = client.post(f"/api/extractions/{JID}/upload",
                    json={"filename": "new.json", "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["bytes"] > 0
    assert any("Alice" in line for line in body["preview"])
    # 「格式假设」必须写在返回里，不能只成功不给警告
    assert "JSON" in body["note"] and "格式" in body["note"]


def test_upload_rejects_non_json_filename(client):
    for name in ("extract.txt", "extract", "x.JSONX"):
        r = client.post(f"/api/extractions/{JID}/upload",
                        json={"filename": name, "dry_run": True})
        assert r.status_code == 400, name


def test_upload_submits_job_and_creates_file(client):
    """正式上传走 job（与导入一致），平台侧真的建出文件与词条。"""
    r = client.post(f"/api/extractions/{JID}/upload", json={"filename": "brand-new.json"})
    assert r.status_code == 200
    jid = r.json()["job"]["id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{jid}").json()["job"]
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    assert job["result"]["uploaded"] == 2
    assert job["result"]["file"]["name"] == "brand-new.json"
    # 文件真的出现在平台的 files 列表里（走 fake 平台的 POST /files）
    files = client.get("/api/files").json()["files"]
    assert any(f["name"] == "brand-new.json" for f in files), files


def test_upload_tool_is_registered_and_writes():
    from paratranz_agent.server.tools import TOOL_SPECS

    spec = next((t for t in TOOL_SPECS if t["name"] == "extraction_upload"), None)
    assert spec is not None, "upload 工具没进 TOOL_SPECS"
    assert spec["writes"] is True, "它会写平台，必须标成写操作（前端要二次确认）"
    assert spec["label_en"] and spec["description_en"], "英文界面要能显示"
