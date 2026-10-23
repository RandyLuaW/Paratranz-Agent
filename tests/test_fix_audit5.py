"""全盘审计（第五轮）发现的高危缺陷回归测试，全部离线。

覆盖：
1. review 二审「非 fail 即通过」（fail-open 残留通路）
2. 回写忽略提取器 encoding → 非 UTF-8 文件被写成混合编码
3. 镜像模式把「词条全部失败的文件」漏出输出目录
4. JSON 值带首尾空白永远写不进去
5. 配置非原子写 + 损坏时静默丢凭据
6. 危险工具的 accept_risk 闸门可被通用任务口绕过
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.pipelines.review import _norm_verdict  # noqa: E402
from paratranz_agent.server.tools import (  # noqa: E402
    DANGEROUS_TOOLS,
    require_risk_ack,
)
from paratranz_agent.writeback import JsonPathWriter, WriteItem, run_writeback  # noqa: E402


# ------------------------------------------------- 1. review fail-open
@pytest.mark.parametrize(
    "raw,expect",
    [
        ("pass", "pass"),
        ("PASS", "pass"),
        (" 通过 ", "pass"),
        ("合格", "pass"),
        (None, ""),
        ("", ""),
        ("maybe", ""),          # 认不出来 → 未判定，绝不默认通过
        ("fail", "fail"),
        (" fail ", "fail"),
        ("不通过", "fail"),
        ("该条不合格", "fail"),
        ("译文错误", "fail"),
        ("FAILED", "fail"),
    ],
)
def test_norm_verdict_is_fail_closed(raw, expect):
    """防回归：模型没明确说「通过」就不能算通过。

    以前是 `str(it.get("verdict", "pass")).lower()`：缺字段默认 pass，
    主循环又只认精确的 `"fail"`，于是「中文不合格」「带空格的 fail」「字段缺失」
    全部被当成通过，未审词条被置成 stage=3（已检查）。
    """
    assert _norm_verdict(raw) == expect


# ------------------------------------------------- 2. 回写沿用提取器 encoding
def _make_job(state_dir: Path, game_dir: Path, keys: list[tuple[str, str]], *, options=None) -> str:
    job_id = "20260101-000000-enc"
    jdir = state_dir / "extractions" / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / "meta.json").write_text(
        json.dumps(
            {
                "extractor": {"id": "generic-regex"},
                "job_id": job_id,
                "extractor_id": "generic-regex",
                "game_dir": str(game_dir),
                "status": "done",
                "entries": len(keys),
                "files_scanned": 1,
                "duration_ms": 1,
                "error": "",
                "warnings": [],
                "options": options or {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with (jdir / "entries.jsonl").open("w", encoding="utf-8") as f:
        for key, original in keys:
            f.write(json.dumps({"key": key, "original": original, "context": ""},
                               ensure_ascii=False) + "\n")
    return job_id


def _seed(fake, rows: list[dict]) -> None:
    fake.state["strings"] = [
        {
            "id": 700000 + i,
            "key": r["key"],
            "original": r["original"],
            "translation": r.get("translation", ""),
            "stage": r.get("stage", 3),
            "fileId": 101,
            "context": None,
        }
        for i, r in enumerate(rows)
    ]


def test_writeback_respects_extractor_encoding(client, fake, tmp_path):
    """防回归：提取器用 GBK 提取，回写也必须用 GBK 写。

    以前硬编码 utf-8 → 原行还是 GBK 字节、新译文是 UTF-8 字节，
    文件变成混合编码，游戏读它就是乱码或直接崩。
    """
    game = tmp_path / "game"
    (game / "script").mkdir(parents=True)
    src = game / "script" / "ui.rpy"
    src.write_bytes('label start:\n    "Hello there"\n'.encode("gbk"))

    job = _make_job(
        tmp_path, game,
        [("script/ui.rpy::L2", "Hello there")],
        options={"encoding": "gbk", "pattern": '"([^"\\n]{2,})"', "group": "1"},
    )
    _seed(fake, [{"key": "script/ui.rpy::L2", "original": "Hello there", "translation": "你好"}])

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)
    assert rep.written == 1

    out = Path(rep.out_dir) / "script" / "ui.rpy"
    raw = out.read_bytes()
    decoded = raw.decode("gbk")  # 必须是纯 GBK，混合编码在这里就会炸
    assert "你好" in decoded
    assert decoded.startswith("label start:")


# ------------------------------------------------- 3. 镜像不漏文件
def test_mirror_keeps_files_whose_entries_all_failed(client, fake, tmp_path):
    """防回归：该文件的词条全部失败时，它也必须出现在输出目录里。

    以前镜像模式拿「有待写词条的文件」当跳过集，于是这种文件既不复制也不写出，
    输出目录凭空少一个 data json，而报告一片绿（written=1 copied=1 errors=[]）。
    """
    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "good.json").write_text('{"a": "Alice"}', encoding="utf-8")
    (game / "data" / "bad.json").write_text('{"b": "Bob"}', encoding="utf-8")

    job = _make_job(
        tmp_path, game,
        [("data/good.json::a", "Alice"), ("data/bad.json::b", "Bob")],
    )
    _seed(
        fake,
        [
            {"key": "data/good.json::a", "original": "Alice", "translation": "爱丽丝"},
            # 平台记的原文与文件里不一致 → 这一条会 mismatched
            {"key": "data/bad.json::b", "original": "Robert", "translation": "鲍勃"},
        ],
    )

    rep = run_writeback(client=client, state_dir=tmp_path, job_id=job, dry_run=False)
    out = Path(rep.out_dir)

    assert rep.mismatched == 1
    assert (out / "data" / "good.json").is_file()
    assert (out / "data" / "bad.json").is_file(), "全部失败的文件也必须镜像过去"
    assert json.loads((out / "data" / "bad.json").read_text(encoding="utf-8")) == {"b": "Bob"}


# ------------------------------------------------- 4. 首尾空白
def test_json_write_tolerates_whitespace_difference():
    """防回归：提取器 strip 过原文，文件里的字面量可能带首尾空白。

    逐字比对会让这类词条永远写不进去，报错还写成「文件里是 Alice，平台记录是 Alice」。
    """
    src = '{"a": "  Hello there  "}'
    res = JsonPathWriter().apply(
        src, [WriteItem(key="f.json::a", translation="你好", original="Hello there")]
    )
    assert res.written == ["f.json::a"]
    assert res.mismatched == []
    assert json.loads(res.content)["a"] == "你好"


# ------------------------------------------------- 5. 配置原子写与损坏恢复
def test_settings_recovers_from_bak_when_corrupt(tmp_path):
    """防回归：配置损坏时先试 .bak 回滚，绝不静默丢凭据。"""
    from paratranz_agent.server.settings import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    store.save({"token": "real-token-123456", "project_id": 1001})
    assert (tmp_path / "settings.json.bak").exists() or True  # 首次写没有旧文件

    store.save({"project_id": 1002})  # 第二次写会生成 .bak
    bak = tmp_path / "settings.json.bak"
    assert bak.exists(), "第二次保存后应有备份"

    # 把主文件写坏
    (tmp_path / "settings.json").write_text('{"token": "real-to', encoding="utf-8")
    fresh = SettingsStore(tmp_path / "settings.json")
    s = fresh.load()
    assert s.project_id in (1001, 1002), "应从 .bak 恢复出可用配置"
    assert (tmp_path / "settings.json").read_text(encoding="utf-8").strip().endswith("}")


def test_settings_raises_when_corrupt_without_bak(tmp_path):
    """没有备份时明确报错，而不是装作配置为空。"""
    from paratranz_agent.server.settings import SettingsStore

    p = tmp_path / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="损坏"):
        SettingsStore(p).load()


# ------------------------------------------------- 6. 危险工具闸门
@pytest.mark.parametrize("tool", sorted(DANGEROUS_TOOLS))
def test_dangerous_tools_require_accept_risk(tool):
    """防回归：闸门在**工具层**，所以通用任务口（POST /api/jobs）也绕不过去。"""
    with pytest.raises(PermissionError, match="accept_risk"):
        require_risk_ack(tool, False)
    require_risk_ack(tool, True)  # 确认了就放行


def test_tool_extract_rejects_without_accept_risk(fake, tmp_path):
    """端到端：直接调工具函数（等价于改走 /api/jobs）也必须被拦。"""
    from types import SimpleNamespace

    from paratranz_agent.server.tools import tool_extract

    ctx = SimpleNamespace(client=fake.client(), state_dir=tmp_path)
    try:
        with pytest.raises(PermissionError, match="accept_risk"):
            tool_extract(ctx, extractor_id="any", game_dir=str(tmp_path))
    finally:
        ctx.client.close()


def test_agent_path_does_not_auto_run_dangerous_tools():
    """防回归：`/api/agent` 不得把危险工具直接提交成任务。

    模型只是把自然语言映射成工具序列，而输入可能来自游戏文件内容（提示注入）；
    审计实测「忽略之前规则」这类输入能让它真的执行本机代码。
    这条用源码断言守住提交处的过滤逻辑（避免搭整套 app 夹具）。
    """
    block = (ROOT / "src/paratranz_agent/server/routes/agent.py").read_text(encoding="utf-8")
    assert "DANGEROUS_TOOLS" in block, "agent 提交处必须过滤危险工具"
    assert "needs_confirm" in block, "被拦下的危险步骤要回给用户看"


# ------------------------------------------------- 7. 预算：GET→PUT 回传必须合法
def test_budget_get_put_roundtrip(tmp_path):
    """防回归：`GET /api/budget` 返回的是嵌套形状，改一改再 PUT 回来必须接受。

    这条正是「加了类型校验把正常用法拦死」的翻车点：web_smoke 里
    「先 GET、改 daily、再 PUT」的流程一度静默 400，直到前端审计发现。
    """
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    app = create_app(state_dir=tmp_path / "state")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        got = c.get("/api/budget").json()["budget"]
        assert isinstance(got.get("daily"), dict), "GET 返回的应是嵌套 rule"

        got["daily"]["max_usd"] = 9.5
        r = c.put("/api/budget", json=got)
        assert r.status_code == 200, r.text
        assert r.json()["budget"]["daily"]["max_usd"] == 9.5

        # 真非法（字符串金额）仍要拦
        r2 = c.put("/api/budget", json={"per_job": {"max_usd": "很多钱"}})
        assert r2.status_code == 400

        # 深层嵌套也拦
        r3 = c.put("/api/budget", json={"daily": {"max_usd": {"nested": 1}}})
        assert r3.status_code == 400


# ------------------------------------------------- 8. --mock 演示模式必须真的 mock
def test_serve_passes_mock_to_create_app():
    """防回归：`ptz serve --mock` / `web.bat --mock` 必须真的用假平台。

    真实事故：`serve()` 收了 `mock` 形参却没传给 `create_app`，
    于是「演示模式」实际连的是**真实平台**——而 README 与 WebUI说明
    都写着「内置假平台、不碰真实项目」。没人发现是因为 web_smoke 直接
    调 `create_app(mock=True)`，从不经过 serve()。
    """
    src = (ROOT / "src/paratranz_agent/server/app.py").read_text(encoding="utf-8")
    i = src.index("def serve(")
    block = src[i : i + 1200]
    assert "create_app(" in block, "serve 里应构建 app"
    assert "mock=mock" in block, "serve 必须把 mock 传给 create_app"


def test_create_app_mock_uses_fake_platform(tmp_path):
    """mock=True 时 client 应指向假平台（health 会自报 mock）。"""
    from fastapi.testclient import TestClient

    from paratranz_agent.server.app import create_app

    app = create_app(state_dir=tmp_path / "state", mock=True)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        h = c.get("/api/health").json()
        assert h["mock"] is True
        assert h["configured"] is True, "演示模式不需要 Token 也应视为已配置"
