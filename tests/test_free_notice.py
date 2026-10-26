"""「本软件完全免费」声明的回归测试。

这件事的特殊性：它挡的是**别人拿这个软件收钱**。所以三处都要在，且不能只做一半 ——
① 仓库里有文本文件（可以脱离软件被转发）；
② 首次启动在 WebUI 弹一次，中英**同时**显示（不能因为界面是英文就只看到英文）；
③ 设置页「关于」里常驻一条（错过弹窗也能查到）。
"""
from __future__ import annotations


import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

REPO = "https://github.com/RandyLuaW/Paratranz-Agent"


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    with TestClient(create_app(tmp_path, mock=True), base_url="http://127.0.0.1") as c:
        yield c


# ------------------------------------------------------------------ ① 文本文件
def test_notice_file_is_bilingual_and_points_to_repo():
    """仓库根目录的 NOTICE.txt：中英双语、写明"立即退款"、给出官方仓库。"""
    path = ROOT / "NOTICE.txt"
    assert path.is_file(), "缺 NOTICE.txt"
    text = path.read_text(encoding="utf-8")
    assert "完全免费" in text
    assert "退款" in text
    assert "completely free" in text.lower()
    assert "refund" in text.lower()
    assert REPO in text


def test_notice_file_is_shipped_in_portable_package_rule():
    """打包脚本必须把声明带进包里（文件名给中文，记事本双击不乱码）。"""
    src = (ROOT / "tools" / "build_portable.py").read_text(encoding="utf-8")
    assert "NOTICE.txt" in src, "打包脚本没有复制免费声明"
    assert "免费声明.txt" in src, "包内文件名应便于普通用户识别"
    assert "utf-8-sig" in src, "带 BOM 才能让 Windows 记事本正确识别中文"


# ------------------------------------------------------------------ ② 首次启动弹窗
def test_first_launch_is_pending_then_acknowledged(client):
    first = client.get("/api/notice").json()
    assert first["pending"] is True
    assert first["repo"] == REPO
    assert first["notice_file"] == "NOTICE.txt"
    # 中英双语都要给，且是两段不同文本
    assert "完全免费" in first["text_zh"]
    assert "completely free" in first["text_en"].lower()

    ack = client.post("/api/notice/ack")
    assert ack.status_code == 200 and ack.json()["pending"] is False
    assert client.get("/api/notice").json()["pending"] is False


def test_acknowledge_is_idempotent(client):
    client.post("/api/notice/ack")
    assert client.post("/api/notice/ack").json()["pending"] is False


def test_acknowledged_state_survives_restart(tmp_path):
    """按「这台机器这次安装」算：换进程、换浏览器都不再弹，但清掉状态目录会重新弹。"""
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    with TestClient(create_app(tmp_path, mock=True), base_url="http://127.0.0.1") as c:
        c.post("/api/notice/ack")
    with TestClient(create_app(tmp_path, mock=True), base_url="http://127.0.0.1") as c2:
        assert c2.get("/api/notice").json()["pending"] is False
    # 状态文件坏掉 → 当作没确认过（宁可再弹一次，也不要静默漏掉声明）
    (tmp_path / "notice-free-ack.json").write_text("{broken", encoding="utf-8")
    with TestClient(create_app(tmp_path, mock=True), base_url="http://127.0.0.1") as c3:
        assert c3.get("/api/notice").json()["pending"] is True


def test_notice_state_is_not_mixed_into_settings(client):
    """声明状态是"这台机器看过没有"，不该进用户配置（settings.json）。"""
    client.post("/api/notice/ack")
    s = client.get("/api/settings").json()["settings"]
    assert not any("notice" in k for k in s), s.keys()


# ------------------------------------------------------------------ ③ 界面接入
def test_ui_wires_modal_and_about_row():
    app_js = (ROOT / "src/paratranz_agent/server/webui/app.js").read_text(encoding="utf-8")
    assert "function freeNoticeDialog" in app_js
    assert "/api/notice" in app_js and "/api/notice/ack" in app_js
    # 中英两段同屏：模板里必须同时出现 zh 与 en 字段
    assert "info.text_zh" in app_js and "info.text_en" in app_js
    # 关于页常驻入口，错过弹窗也能查
    assert "免费声明" in app_js


# ------------------------------------------------- 许可条款不许被静默删掉
def test_license_keeps_key_clauses():
    """许可里每一条都是"删掉就少一层保护"，用断言盯住。

    历史：这几个条款是分两次加上的——先是禁止付费网络服务与署名要求，
    后补专利授权、30 日补救期与商业授权渠道。没有断言时，任何一次文档
    重写都可能把它们悄悄读掉。
    """
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    musts = {
        "免费分发授权": "free of charge",
        "署名要求": "ATTRIBUTION",
        "官方仓库链接要求": "https://github.com/RandyLuaW/Paratranz-Agent",
        "禁止付费网络服务": "NO PAID NETWORK SERVICE",
        "保留权利": "not expressly granted",
        "专利授权": "PATENT LICENSE",
        "违约补救期": "30 days after receiving written notice",
        "商业授权渠道": "COMMERCIAL AND ALTERNATIVE LICENSING",
        "中文对照存在": "商业与替代授权",
        "版本号": "版本 1.1",
    }
    missing = [label for label, kw in musts.items() if kw not in text]
    assert not missing, f"LICENSE 缺少条款：{missing}"


def test_readmes_mention_commercial_license():
    """两个 README 都要给出"想收费就来谈"的出口，否则读者只会看到禁止。"""
    zh = (ROOT / "README.md").read_text(encoding="utf-8")
    en = (ROOT / "README.en.md").read_text(encoding="utf-8")
    assert "商业授权" in zh and "书面授权" in zh
    assert "commercial or alternative license" in en.lower()
