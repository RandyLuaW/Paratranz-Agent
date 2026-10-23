"""防回归：采样内容会被发给第三方模型，凭据不能跟着走。

场景：用户把「让 Agent 写提取器」指向某个工作目录（经过 accept_risk 确认，
他理解的风险是「在本机执行代码」），目录里恰好有 `.env`、`settings.json`
或凭据库——这些文件一旦被采样，就等于把密钥发给了模型供应商。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.server.extraction.analyze import sample_files  # noqa: E402


def _mk(tmp_path: Path, name: str, text: str) -> None:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_sensitive_files_are_never_sampled(tmp_path):
    """凭据/运行数据类文件一个都不许出现在采样里。"""
    _mk(tmp_path, "data/Actors.json", '{"1":{"name":"Alice"}}')
    _mk(tmp_path, ".env", "PTZ_API_TOKEN=super-secret-token-value")
    _mk(tmp_path, "settings.json", '{"token":"abc","llm_key":"sk-xxxxxxxxxxxx"}')
    _mk(tmp_path, "agent.db", "SQLite format 3")
    _mk(tmp_path, "run.log", "2026-10-23 token=abc")

    samples = sample_files(tmp_path, max_files=10, max_chars=2000)
    paths = {s["path"] for s in samples}
    assert "data/Actors.json" in paths
    for bad in (".env", "settings.json", "agent.db", "run.log"):
        assert bad not in paths, f"{bad} 被采样了"

    blob = "".join(s["preview"] for s in samples)
    assert "super-secret-token-value" not in blob
    assert "sk-xxxxxxxxxxxx" not in blob


def test_preview_secrets_are_scrubbed(tmp_path):
    """即使文件名看着无害，正文里的密钥形态串也要抹掉。"""
    _mk(tmp_path, "data/Config.json",
        '{"api_key": "sk-live-abcdefghijklmnop", "note": "与密钥无关的普通文本"}')

    samples = sample_files(tmp_path, max_files=5, max_chars=2000)
    assert samples, "应该采到 data/Config.json"
    blob = "".join(s["preview"] for s in samples)
    assert "sk-live-abcdefghijklmnop" not in blob
    assert "***REDACTED***" in blob
    # 普通文本不能被误抹
    assert "与密钥无关的普通文本" in blob
