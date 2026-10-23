"""plan / styleguide 四个静默失效缺陷的回归测试。

这四条的共同点是**不报错**：截断碰撞让某个目录的工作被跳过、只读第一页
让去重失效、标题带日期让公告每天多一条、扩展名误判让规范里统计出假的
文件类型。全部离线跑（`paratranz_agent.fake.FakeParaTranz`，不联网）：
- 截断碰撞：两个共享前 118 字符的 120 字符目录名，去重键必须不同；
- 翻页去重：>100 个既有任务时，第 2 页上的同名任务也要能认出来；
- 公告标题：固定标题（日期进正文），旧版带日期的公告被接管更新而不是新建；
- 文件类型：`www/data/v1.2/Map001` 不能算成扩展名 `2/map001`。
"""
from __future__ import annotations

import datetime as _dt
import re

import pytest

from paratranz_agent.fake import FakeParaTranz
from paratranz_agent.models import FileItem, Task
from paratranz_agent.pipelines import PlanPipeline, StyleGuidePipeline
from paratranz_agent.pipelines.plan import KEY_LIMIT, TITLE_LIMIT, _bounded_key


# --------------------------------------------------------------------- 造数据
def _make_fake(file_specs: list[tuple[str, int]]) -> FakeParaTranz:
    """按 `(文件名, 未翻译条数)` 造一个内存假平台。"""
    files: list[dict] = []
    strings: list[dict] = []
    fid = 1000
    sid = 1
    for name, pending in file_specs:
        fid += 1
        files.append({"id": fid, "name": name, "hash": "d" * 32})
        for i in range(pending):
            strings.append(
                {
                    "id": sid,
                    "key": f"k{fid}_{i}",
                    "original": f"sentence {i} of file {fid}",
                    "translation": "",
                    "stage": 0,
                    "fileId": fid,
                    "context": None,
                }
            )
            sid += 1
    return FakeParaTranz(
        state={
            "project": {
                "id": 6896,
                "name": "Test Project",
                "source": "en",
                "dest": "zh-CN",
                "extra": {"task": True, "reviewMode": 1, "disableApiEdit": False, "termsMode": 1},
            },
            "files": files,
            "strings": strings,
            "terms": [],
            "members": [],
            "tasks": [],
            "announcements": [],
            "calls": [],
        }
    )


def _task(stub_id: int, title: str) -> dict:
    return {
        "id": stub_id,
        "title": title,
        "content": "",
        "status": 0,
        "goal": {"translate": 1.0, "check": 0.0, "review": 0.0},
        "deadline": None,
        "bumpedAt": None,
        "taskFiles": [],
    }


# ------------------------------------------------- 1. 长目录名截断不再碰撞
@pytest.mark.parametrize("name_len", [115, 130])
def test_dedup_key_survives_title_truncation(cfg, store, name_len):
    """防回归：长目录名共享前缀时，硬截断会让去重失效。

    后果不是报错，而是 `execute()` 里 `key in existing` 不命中 / 命中错人，
    前者重复建任务，后者把某个目录的工作静默跳过。
    实测两种坏法（对旧代码复现过）：
    - 115 字符：`前缀 + 目录名` = 122，键本身不撞，但 title 122+13=135 被
      截成 128，计数后缀被切掉一半 → `_COUNT_SUFFIX` 剥不干净 →
      平台上的标题还原不出 dedup_key → 每轮重建；
    - 130 字符：`前缀 + 目录名` = 137 > 128，两个共享前 128 字符的目录名
      在旧代码里拿到同一个 dedup_key，其中一个目录直接没有任务。
    两种都要求：键不同、长度合规、标题剥掉计数后缀后能还原 dedup_key。
    """
    shared = "d" * (name_len - 2)
    dir_a = shared + "aa"
    dir_b = shared + "bb"  # 前 name_len-2 个字符与 A 完全相同
    assert len(dir_a) == len(dir_b) == name_len and dir_a != dir_b

    fake = _make_fake([(f"{dir_a}/Map001.json", 3), (f"{dir_b}/Map001.json", 3)])
    client = fake.client()
    try:
        plan = PlanPipeline(client, cfg, store).build_plan()
    finally:
        client.close()

    assert len(plan) == 2, "两个目录应各出一个任务"
    keys = {p.dedup_key for p in plan}
    assert len(keys) == 2, f"去重键仍然相撞：{sorted(keys)}"

    for p in plan:
        assert len(p.dedup_key) <= KEY_LIMIT < TITLE_LIMIT
        assert len(p.title) <= TITLE_LIMIT, f"title 超 128：{len(p.title)}"
        # 平台上的标题剥掉计数后缀后必须还原 dedup_key，否则重跑还是会重复建
        stripped = re.sub(r"\s*·\s*\d+\s*文件\s*/\s*\d+\s*条\s*$", "", p.title).strip()
        assert stripped == p.dedup_key

    if len(dir_a) > 128:
        # 哈希进了键尾，长目录名尾部仍有区分度
        assert all("~" in p.dedup_key for p in plan)

    # 同一目录拆成多个 part 时，`#2` 不能被截断吃掉
    assert _bounded_key("x" * 200 + " #2") != _bounded_key("x" * 200)
    assert len(_bounded_key("x" * 500)) == KEY_LIMIT


# --------------------------------------------- 2. 既有任务要翻页取全量
def test_existing_titles_paginate_beyond_first_page(cfg, store):
    """防回归：`_existing_titles()` 只读第 1 页 100 条时，第 101 个之后的
    任务看不见，`execute()` 会给它们再建一遍同名任务（任务列表越滚越长）。
    这里把目标任务的标题放在第 131 位，验证它仍然被认出去重。
    """
    fake = _make_fake([("www/data/Map001.json", 3)])
    client = fake.client()
    try:
        pipe = PlanPipeline(client, cfg, store)
        plan = pipe.build_plan()
        assert len(plan) == 1

        # 130 条无关任务占满第一页，目标任务的标题落在第 2 页
        fake.state["tasks"] = [_task(2000 + i, f"历史任务 {i}") for i in range(130)]
        fake.state["tasks"].append(_task(3000, plan[0].title))
        assert len(fake.state["tasks"]) == 131

        titles = pipe._existing_titles()
        assert plan[0].dedup_key in titles, "第 2 页上的任务没被读到"
        assert len(titles) == 131

        created = pipe.execute(plan, dry_run=False)
    finally:
        client.close()

    assert created == [], "已存在的任务被重复创建"
    assert len(fake.state["tasks"]) == 131, "平台任务数不应该增长"


def test_existing_titles_stops_when_page_param_ignored(cfg, store):
    """防回归：平台忽略 page 参数（永远返回同一页）时，翻页必须有硬上限，
    不能死循环。这里用一个永远回同一批任务的假 client 验证会退出。
    """
    page_calls: list[int] = []
    stuck = Task.model_validate(_task(1, "[自动分派] a/ · 1 文件 / 1 条"))

    class _StuckClient:
        def list_tasks(self, page: int = 1, page_size: int = 50):
            page_calls.append(page)
            return [stuck]

    titles = PlanPipeline(_StuckClient(), cfg, store)._existing_titles()  # type: ignore[arg-type]
    assert titles == {"[自动分派] a/"}
    assert len(page_calls) <= 2, f"没有及时刹车，翻了 {len(page_calls)} 页"


# -------------------------------------------------- 3. 公告标题固定不再每天新建
def test_styleguide_title_has_no_date_and_reuses_announcement(cfg, store, llm):
    """防回归：标题里带 `v{今天}` 时，每天标题都不同 → 每天新建一条公告。
    固定标题 + 日期进正文，第二次发布必须走 update，公告数保持 1。
    """
    fake = _make_fake([("www/data/Map001.json", 3)])
    client = fake.client()
    try:
        pipe = StyleGuidePipeline(client, cfg, store, llm)
        first = pipe.publish(dry_run=False, with_llm=False)
        second = pipe.publish(dry_run=False, with_llm=False)
    finally:
        client.close()

    assert first["action"] == "create"
    assert second["action"] == "update"
    assert len(fake.state["announcements"]) == 1

    ann = fake.state["announcements"][0]
    assert not re.search(r"\d{4}-\d{2}-\d{2}", ann["title"]), f"标题仍带日期：{ann['title']}"
    # 日期必须在正文里，否则信息丢失
    assert _dt.date.today().isoformat() in ann["content"]


def test_styleguide_adopts_legacy_dated_announcement(cfg, store, llm):
    """防回归：升级前已经发过带日期的公告，切到固定标题后不能再多发一条，
    而应接管旧公告并把它改名归位。
    """
    fake = _make_fake([("www/data/Map001.json", 3)])
    fake.state["announcements"] = [
        {
            "id": 777,
            "title": "Test Project 汉化规范 v2020-01-01",
            "content": "旧版内容",
            "category": "announcement",
            "internal": True,
            "bumpedAt": None,
        }
    ]
    client = fake.client()
    try:
        res = StyleGuidePipeline(client, cfg, store, llm).publish(dry_run=False, with_llm=False)
    finally:
        client.close()

    assert res["action"] == "update" and res["id"] == 777
    assert len(fake.state["announcements"]) == 1
    assert fake.state["announcements"][0]["title"] == "Test Project 汉化规范"
    assert "旧版内容" not in fake.state["announcements"][0]["content"]


# ------------------------------------------------- 4. 文件类型统计不再误判
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("www/data/Map001.json", "json"),
        ("www/quest/quest1.TXT", "txt"),
        ("www/data/v1.2/Map001", "unknown"),  # 目录里的点不是扩展名
        ("www/data/v1.2/map.001", "001"),  # 点后 1-5 位字母数字 → 算扩展名
        ("localisation/strings.csv", "csv"),
        ("www/noext", "unknown"),
        ("www/pack.tar.gz", "gz"),
        ("www/data/verylongext", "unknown"),
        ("www/data/name.abcdefg", "unknown"),  # 点后超过 5 位 → unknown
        ("www/data/trailing.", "unknown"),
    ],
)
def test_kind_of_uses_basename(name, expected):
    """防回归：`name.rsplit(".", 1)` 会把 `www/data/v1.2/Map001` 算成
    `2/map001`（把目录名里的点当扩展名），规范里凭空多出不存在的文件类型。
    """
    assert StyleGuidePipeline._kind_of(name) == expected


def test_styleguide_build_stats_do_not_leak_path_as_kind(cfg, store, llm):
    """防回归（整条链路）：噪音文件不能把 `2/map001` 这类伪类型带进规范正文。"""
    fake = _make_fake(
        [
            ("www/data/Map001.json", 3),
            ("www/data/v1.2/Map001", 2),
            ("www/quest/quest1.txt", 2),
        ]
    )
    client = fake.client()
    try:
        md = StyleGuidePipeline(client, cfg, store, llm).build(with_llm=False)
    finally:
        client.close()

    assert "2/map001" not in md.lower()
    assert "`unknown`" in md
    assert "`json`" in md and "`txt`" in md


# ----------------------------------------------------------- 辅助断言（FileItem）
def test_file_item_dir_matches_long_dirname():
    """只是钉住 `FileItem.dir` 的形状：长目录名按最后一个 `/` 切，带尾斜杠。"""
    name = "d" * 118 + "aa/Map001.json"
    f = FileItem(id=1, name=name)
    assert f.dir == "d" * 118 + "aa/"
