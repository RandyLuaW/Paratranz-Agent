"""真实验证：提取 → 导入平台 → 平台确认译文 → 回写游戏文件（全链路真跑）。

跑法（需要 .ptz-state/settings.json 里有 Token）：
    .\\.venv\\Scripts\\python.exe tools\\verify_writeback_real.py

自清理：测试用的平台文件是临时新建的，结束时 `delete_file` 连词条一起删，
本地临时游戏目录也一并删掉。项目统计应回到跑之前的样子。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from paratranz_agent.client import ParaTranzClient  # noqa: E402
from paratranz_agent.models import Stage  # noqa: E402
from paratranz_agent.server import extraction  # noqa: E402
from paratranz_agent.writeback import run_writeback  # noqa: E402

TEST_FILE = "__wb_verify__.json"

ACTORS = [
    None,
    {"id": 1, "name": "Alice", "nickname": "Ali", "profile": "A cheerful farmer."},
    {"id": 2, "name": "Bob", "nickname": "Bobby", "profile": "A quiet blacksmith."},
]
MAP = {
    "displayName": "Old Farm",
    "events": [
        None,
        {
            "id": 1,
            "name": "EV001",
            "pages": [
                {
                    "list": [
                        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
                        {"code": 401, "indent": 0, "parameters": ["Good morning, farmer.", 0]},
                        {"code": 401, "indent": 0, "parameters": ["The crops are ready.", 0]},
                        {"code": 0, "indent": 0, "parameters": []},
                    ]
                }
            ],
        },
    ],
}
SYSTEM = {"gameTitle": "Farm Story", "terms": {"basic": ["Level", "HP", "MP"], "messages": {"alwaysDash": "Always Dash"}}}


def build_game(root: pathlib.Path) -> None:
    d = root / "www" / "data"
    d.mkdir(parents=True)
    (d / "Actors.json").write_text(json.dumps(ACTORS, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "Map001.json").write_text(json.dumps(MAP, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "System.json").write_text(json.dumps(SYSTEM, ensure_ascii=False, indent=2), encoding="utf-8")
    # 未被提取/改写的文件：整包镜像必须把它们原样复制过去，否则目标目录跑不起来
    js = root / "www" / "js"
    js.mkdir(parents=True)
    (js / "rpg_core.js").write_text("// untouched runtime\n", encoding="utf-8")
    img = root / "www" / "img" / "pictures"
    img.mkdir(parents=True)
    (img / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary-not-text")


TRANSLATIONS = {
    "name": "爱丽丝",
    "Alice": "爱丽丝",
    "Bob": "鲍勃",
    "Ali": "小爱",
    "Bobby": "小鲍",
    "A cheerful farmer.": "开朗的农夫。",
    "A quiet blacksmith.": "沉默的铁匠。",
    "Old Farm": "老农场",
    "Good morning, farmer.": "早上好，农夫。",
    "The crops are ready.": "庄稼可以收了。",
    "Farm Story": "农场物语",
    "Level": "等级",
    "Always Dash": "始终奔跑",
}


def main() -> int:
    st = json.loads((ROOT / ".ptz-state/settings.json").read_text(encoding="utf-8"))
    # 项目号不写死：从 settings 或环境变量取，避免把个人项目号带进仓库
    pid = int(st.get("project_id") or 0) or int(os.environ.get("PTZ_PROJECT_ID") or 0)
    if not pid:
        print("未配置项目号：先在 WebUI 里填，或设置 PTZ_PROJECT_ID")
        return 2
    client = ParaTranzClient(token=st["token"], project_id=pid)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ptz-wb-"))
    game = tmp / "game"
    state_dir = tmp / "state"
    state_dir.mkdir()
    file_id = None
    ok = False
    try:
        build_game(game)
        print(f"=== 1. 造一个 RPG Maker MV 目录：{game} ===")

        res = extraction.run_extractor(state_dir, "rpgmaker-mv", game)
        job_id = res.job_id
        print(f"=== 2. 真跑提取器：{res.status} / {res.entries} 条 / {res.files_scanned} 文件 ===")
        entries = extraction.load_entries(state_dir, job_id)
        print(f"   job_id={job_id}  options={res.options}")
        for e in entries[:8]:
            print(f"   {e['key']:34s} {e['original']!r}")
        assert res.entries >= 10, "提取条数异常"

        print("\n=== 3. 平台侧建临时文件并导入这些 key ===")
        fi = client.create_file(TEST_FILE, b'{"placeholder": "keep"}')
        file_id = fi.id
        print(f"   临时文件 id={file_id}")
        created = client.create_strings(entries, file_id)
        print(f"   新建词条 {len(created['created'])} 条，失败 {len(created['failed'])} 条")
        assert not created["failed"], f"创建失败：{created['failed'][:2]}"

        # 模拟「translate + review 流程确认过」：写成译文并把 stage 推到 3（已检查）
        patches = []
        for sid, e in zip(created["created"], entries, strict=False):
            tr = TRANSLATIONS.get(e["original"])
            if tr:
                patches.append({"id": sid, "translation": tr, "stage": int(Stage.CHECKED)})
        client.batch_edit_strings(patches)
        print(f"   平台确认译文 {len(patches)} 条（stage=3 已检查）")

        pulled_file = client.get_file_strings(file_id)
        confirmed = [s for s in pulled_file if int(s.stage) == 3 and (s.translation or "").strip()]
        print(f"   回读确认：{len(confirmed)} 条达到 stage=3 且有译文")

        print("\n=== 4. 回写（默认整包镜像：输出到 <游戏目录>_cn，是一份完整可跑的游戏）===")
        rep = run_writeback(
            client=client, state_dir=state_dir, job_id=job_id,
            min_stage=3, mode="mirror", dry_run=False,
            log=lambda m: print("   " + m),
        )
        out = pathlib.Path(rep.out_dir)
        print(f"   报告：mode={rep.mode} written={rep.written} files={rep.files} "
              f"copied={rep.copied} mismatched={rep.mismatched} "
              f"missing={rep.missing} unsupported={rep.unsupported}")
        assert rep.written > 0, "一条都没写进去"
        assert rep.mismatched == 0 and rep.missing == 0, f"出现了问题明细：{rep.details[:3]}"
        assert rep.copied >= 2, f"未改动文件应被复制，实际 copied={rep.copied}"
        assert (out / "www/js/rpg_core.js").read_text(encoding="utf-8") == "// untouched runtime\n"
        assert (out / "www/img/pictures/logo.png").read_bytes() == b"\x89PNG\r\n\x1a\n\x00binary-not-text", \
            "二进制文件必须按字节复制"

        print("\n=== 5. 校验写回结果 ===")
        orig_actors = (game / "www/data/Actors.json").read_text(encoding="utf-8")
        new_actors = (out / "www/data/Actors.json").read_text(encoding="utf-8")
        assert orig_actors == json.dumps(ACTORS, ensure_ascii=False, indent=2), "原文件被改动了"
        data = json.loads(new_actors)
        print(f"   Actors[1].name: {data[1]['name']!r} / profile: {data[1]['profile']!r}")
        assert data[1]["name"] == "爱丽丝" and data[1]["profile"] == "开朗的农夫。"
        assert data[2]["nickname"] == "小鲍"

        new_map = json.loads((out / "www/data/Map001.json").read_text(encoding="utf-8"))
        dialogs = [c["parameters"][0] for c in new_map["events"][1]["pages"][0]["list"] if c["code"] == 401]
        print(f"   地图对话：{dialogs}")
        assert dialogs == ["早上好，农夫。", "庄稼可以收了。"]

        new_sys = json.loads((out / "www/data/System.json").read_text(encoding="utf-8"))
        print(f"   System.gameTitle: {new_sys['gameTitle']!r} / terms.basic: {new_sys['terms']['basic']}")
        assert new_sys["gameTitle"] == "农场物语"
        assert new_sys["terms"]["basic"][0] == "等级"

        # 格式必须原样：行数、每行缩进、键序都不许变（只允许字符串内容变）
        def shape(text: str) -> list[int]:
            return [len(ln) - len(ln.lstrip()) for ln in text.splitlines()]

        orig_map = (game / "www/data/Map001.json").read_text(encoding="utf-8")
        new_map_raw = (out / "www/data/Map001.json").read_text(encoding="utf-8")
        assert shape(new_actors) == shape(orig_actors), "Actors 缩进或行数被改了"
        assert shape(new_map_raw) == shape(orig_map), "Map 缩进或行数被改了"
        assert new_map_raw.index('"displayName"') < new_map_raw.index('"events"'), "键序被改了"
        assert new_actors.count("\n") == orig_actors.count("\n"), "换行数变了"

        print("\n=== 6. 幂等：再跑一次，应全部算已写入且文件不变 ===")
        rep2 = run_writeback(client=client, state_dir=state_dir, job_id=job_id, min_stage=3, dry_run=False)
        again = (out / "www/data/Actors.json").read_text(encoding="utf-8")
        print(f"   第二次：written={rep2.written} mismatched={rep2.mismatched}")
        assert again == new_actors, "重复回写改动了文件内容"

        print("\n=== 7. 门槛校验：stage=1 的译文不该被写回 ===")
        client.set_stage([created["created"][0]], Stage.TRANSLATED)
        rep3 = run_writeback(
            client=client, state_dir=state_dir, job_id=job_id,
            min_stage=3, dry_run=True, only_files=["Actors.json"],
        )
        print(f"   预览（门槛3，Actors 少了一条达标）：pulled={rep3.pulled} written={rep3.written}")
        assert rep3.written < rep.written, "门槛没生效"

        ok = True
    finally:
        print("\n=== 清理 ===")
        if file_id:
            try:
                client.delete_file(file_id)
                print(f"   已删除平台临时文件 {file_id}（词条一并消失）")
            except Exception as exc:  # noqa: BLE001
                print(f"   ✗ 删除平台文件失败：{exc}")
        rest = [(f.id, f.name, f.total) for f in client.list_files()]
        print(f"   平台剩余文件：{rest}")
        stats = client.get_project().stats
        print(f"   项目统计：total={stats['total']} checked={stats['checked']} "
              f"disputed={stats['disputed']} translated={stats['translated']}")
        client.close()
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"   已删除本地临时目录 {tmp}")

    print("\n结果:", "通过 ✅" if ok else "未通过 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
