"""三位一体回归：review 打回理由、FileItem.pending 口径、空译文脏数据。

三条都是已确认缺陷的**定点**回归，全部离线（FakeParaTranz + FakeLLM），不联网。
每条测试的 docstring 写明它挡的是哪种回归。
"""
from __future__ import annotations

from paratranz_agent.config import AgentConfig, ReviewCfg
from paratranz_agent.fake import FakeParaTranz
from paratranz_agent.models import FileItem
from paratranz_agent.pipelines.review import ReviewPipeline, ReviewStats


class _ReviewCfgOn(ReviewCfg):
    """把 review 的两个可选开关显式打开。

    `comment_on_fail` / `reset_empty` 已在 config.py 的 ReviewCfg 里声明、
    默认都是 False；review.py 仍用 `getattr` 兜底读，防止老配置文件缺字段时报错。
    这里用子类构造等价于「配置项存在且被打开」的环境。
    """

    comment_on_fail: bool = False
    reset_empty: bool = False


def _enable(cfg: AgentConfig, **flags: bool) -> AgentConfig:
    """在测试内把 review 开关打开（不改测试外部状态，每个用例独立）。"""
    data = cfg.review.model_dump()
    data.update(flags)
    cfg.review = _ReviewCfgOn(**data)
    return cfg


class _IssueBrokenFake(FakeParaTranz):
    """评论端点坏掉，用来验证「评论失败不阻塞 stage 写回」。"""

    def _dispatch(self, request):  # noqa: ANN001 - 与父类签名一致
        if request.url.path.endswith("/issues"):
            return self._json({"message": "issues endpoint down"}, 400)
        return super()._dispatch(request)


def _by_original(fake: FakeParaTranz, prefix: str) -> dict:
    return next(s for s in fake.state["strings"] if s["original"].startswith(prefix))


def _inject_empty_translated(fake: FakeParaTranz, sid: int = 99001) -> int:
    """造一条 stage=1 但译文为空的脏数据（平台会把它算进 translated）。"""
    fake.state["strings"].append(
        {
            "id": sid,
            "key": "dirty_empty",
            "original": "Dirty entry with empty translation.",
            "translation": "",
            "stage": 1,
            "fileId": 101,
            "context": None,
        }
    )
    return sid


# ------------------------------------------------- 1. 打回理由落到平台
def test_create_issue_requires_title(client, fake):
    """防回归：`POST /issues` 必须带 `title`，否则真实平台 400。

    真实实测：`{content}` 和 `{content, stringId}`
    都是 400，只有带 `title` 才 201。旧实现只发 content，等于这个功能在真实
    平台上 100% 失败——而假平台当时不校验 title，所以测试全绿。
    """
    res = client.create_issue("[自动校对] 打回明细", "打回原因：占位符丢失")
    assert res["title"] == "[自动校对] 打回明细"
    assert res["content"] == "打回原因：占位符丢失"
    assert int(res["id"]) > 0
    assert "POST /api/projects/6896/issues" in fake.state["calls"], "请求必须真的发出"


def test_create_issue_without_title_is_rejected_by_platform(fake):
    """防回归：调用方必须传 title，平台侧会硬拒（不是可选字段）。"""
    from paratranz_agent.client import ParaTranzClient, ParaTranzError

    client = ParaTranzClient("test-token", 6896, transport=fake.transport())
    try:
        try:
            client.request("POST", "/projects/{projectId}/issues", json_body={"content": "只有正文"})
        except ParaTranzError as e:
            assert e.status == 400
        else:  # pragma: no cover
            raise AssertionError("缺 title 应当被平台拒绝")
    finally:
        client.close()


def test_reply_issue_appends_child(client, fake):
    """防回归：台账帖的追加回复走 `POST /issues/{id}` + `op=reply`。

    实测：正文为空会被平台拒（400 `content required`）；回复落在 `parent` 上。
    """
    issue = client.create_issue("[自动校对] 打回明细", "首帖")
    iid = int(issue["id"])
    reply = client.reply_issue(iid, "| 词条 | 原因 |\n|---|---|\n| 1 | 占位符丢失 |")
    assert int(reply["parent"]) == iid
    assert "占位符丢失" in reply["content"]

    got = client.get_issue(iid)
    assert len(got.get("activities") or []) == 1, "回复要能从详情里读回"


def test_list_issues_tolerates_paged_envelope(client, fake):
    """防回归：讨论列表是分页包，不是裸数组（真实平台实测）。"""
    client.create_issue("[自动校对] 打回明细", "首帖")
    issues = client.list_issues(status=0)
    assert isinstance(issues, list) and len(issues) == 1
    assert issues[0]["title"] == "[自动校对] 打回明细"


def test_list_announcements_tolerates_paged_envelope(client, fake):
    """防回归：公告列表是分页包。

    真实平台返回 `{results,...}`；漏用 unwrap_page 时 pydantic 拿 `'results'`
    字符串去校验 → ValidationError，而 styleguide 用 `except: pass` 兜住，
    于是「已存在则更新」永远匹配不到 → 每跑一次多一条重复公告。
    """
    fake.state["announcements"] = [
        {"id": 500, "title": "旧公告", "content": "x", "category": "announcement", "internal": True}
    ]
    anns = client.list_announcements()
    assert [a.id for a in anns] == [500]


def test_review_comment_on_fail_posts_digest_thread(client, cfg, store, fake, llm):
    """防回归：开 comment_on_fail 后把**整批打回明细**发成一条讨论帖。

    平台没有词条级评论接口（实测 404），人工复查只看得到 stage=2，
    所以帖子里必须有「打回原因 + 原文 + 译文」，否则复查的人无从下手。
    """
    _enable(cfg, comment_on_fail=True)
    stats = ReviewPipeline(client, cfg, store, llm).run(dry_run=False)

    bad = _by_original(fake, "You got")
    assert bad["stage"] == 2, "丢占位符的条目应被打回"
    assert stats.disputed >= 1
    assert stats.issue_action == "create", "首次应建帖"
    assert stats.issue_id > 0
    assert stats.issue_error == ""

    issues = fake.state["issues"]
    assert len(issues) == 1, "整批只发一条帖子，不逐条刷屏"
    body = issues[0]["content"]
    assert issues[0]["title"] == "[自动校对] 打回明细"
    assert "打回" in body
    assert "占位符" in body, "帖子要带 QA 规则的 message"
    assert "You got" in body, "帖子要带原文摘要"
    assert "金币" in body, "帖子要带当前译文摘要"
    assert str(bad["id"]) in body, "帖子要能定位到具体词条 id"


def test_review_comment_second_run_appends_reply(client, cfg, store, fake, llm):
    """防回归：第二次跑出同样的打回时**追加回复**，不新建重复帖子。"""
    _enable(cfg, comment_on_fail=True)
    pipe = ReviewPipeline(client, cfg, store, llm)
    first = pipe.run(dry_run=False)
    assert first.issue_action == "create"

    # 把打回项改回「已翻译」，制造又一次同样的打回
    for s in fake.state["strings"]:
        if s["stage"] == 2:
            s["stage"] = 1
    second = pipe.run(dry_run=False)

    assert second.issue_action == "reply", "已有台账帖时应追加回复"
    assert second.issue_id == first.issue_id
    assert len(fake.state["issues"]) == 1, "不能因为重跑就多出一条帖子"
    assert len(fake.state["issues"][0]["activities"]) == 1


def test_review_comment_disabled_by_default(client, cfg, store, fake, llm):
    """防回归：打回明细帖默认**关闭**。

    默认开启等于每次校对都往真实项目讨论区写东西；必须显式配置才发。
    """
    stats = ReviewPipeline(client, cfg, store, llm).run(dry_run=False)
    assert stats.disputed >= 1, "默认仍然要正常打回"
    assert stats.issue_action == ""
    assert stats.issue_error == ""
    assert fake.state.get("issues", []) == [], "默认不向平台发任何帖子"


def test_comment_failure_does_not_block_stage_writeback(cfg, store, llm):
    """防回归：评论接口报错时，打回仍然要落 stage=2。

    评论是锦上添花。以前如果直接在写回前调用评论且不兜住异常，
    一次接口失败就会让整批词条留在 stage=1（等于没校对）。
    """
    fake = _IssueBrokenFake()
    client = fake.client()
    try:
        _enable(cfg, comment_on_fail=True)
        stats = ReviewPipeline(client, cfg, store, llm).run(dry_run=False)
    finally:
        client.close()

    bad = _by_original(fake, "You got")
    assert bad["stage"] == 2, "发帖失败不能连累 stage 写回"
    assert stats.disputed >= 1
    assert stats.issue_action == "", "失败时不该声称发成功了"
    assert stats.issue_error, "失败必须留痕，不能静默吞掉"
    assert stats.written >= stats.disputed


def test_dry_run_sends_no_comments(client, cfg, store, fake, llm):
    """防回归：dry_run 必须零写入——包括讨论帖。

    预演唯一的作用就是「不碰平台」，往平台发帖同样是写入。
    """
    _enable(cfg, comment_on_fail=True)
    stats = ReviewPipeline(client, cfg, store, llm).run(dry_run=True)
    assert stats.disputed >= 1
    assert stats.issue_action == ""
    assert fake.state.get("issues", []) == []


# --------------------------------------------- 2. FileItem.pending 口径
def test_file_item_pending_excludes_hidden():
    """防回归：hidden(stage=-1) 不算待翻译。

    实测 3 条里含 1 条隐藏：旧算法 `total - translated` 得 3，实际待翻 2 条。
    条数虚高会直接污染任务标题/说明，文件模式下进度也永远到不了 100%。
    """
    assert FileItem(id=1, total=3, translated=0, hidden=1).pending == 2


def test_file_item_pending_parses_hidden_from_platform_payload():
    """防回归：平台响应里的 `hidden` 不能被丢掉。

    字段解析失败（改名/未声明）会让 hidden 恒为 0，pending 就悄悄退回旧算法。
    """
    parsed = FileItem.model_validate({"id": 9, "total": 10, "translated": 4, "hidden": 3})
    assert parsed.hidden == 3
    assert parsed.pending == 3


def test_file_item_pending_never_negative():
    """防回归：total/translated/hidden 三者口径打架时不能算出负数条数。"""
    assert FileItem(id=2, total=5, translated=5, hidden=2).pending == 0
    assert FileItem(id=3, total=0, translated=0, hidden=0).pending == 0


# ------------------------------------- 3. stage=1 + 空译文不再永久卡死
def test_review_empty_translation_is_disputed_not_skipped(client, cfg, store, fake, llm):
    """防回归：stage=1 但译文为空的脏数据不能被静默跳过。

    以前 `_collect` 里一句 `if not s.translation.strip(): continue` 把它吞掉：
    translate 只取 stage=0、review 又不看它，于是永久卡在 stage=1，
    还把平台的 translated 计数抬成假的 100%。
    """
    sid = _inject_empty_translated(fake)
    stats = ReviewPipeline(client, cfg, store, llm).run(dry_run=False)

    dirty = next(s for s in fake.state["strings"] if s["id"] == sid)
    assert dirty["stage"] == 2, "空译文应被打回「有疑问」，而不是留在 stage=1"
    assert stats.empty_translation == 1, "空译文要有独立计数"
    assert stats.qa_blocked >= 1
    assert any(s["id"] == sid and s["stage"] == "empty" for s in stats.samples), (
        "空译文要在 samples 里留证，便于人工定位"
    )


def test_review_reset_empty_moves_to_untranslated(client, cfg, store, fake, llm):
    """防回归：reset_empty 开启时把空译文退回 stage=0，交给翻译流水线重做。

    这是「打回」之外的另一条路：这类条目根本没有译文，人工也没东西可改，
    重置成未翻译才是有效处置。同时必须**不算打回**，否则 disputed 统计失真。
    """
    sid = _inject_empty_translated(fake)
    _enable(cfg, reset_empty=True)
    stats = ReviewPipeline(client, cfg, store, llm).run(dry_run=False)

    dirty = next(s for s in fake.state["strings"] if s["id"] == sid)
    assert dirty["stage"] == 0, "重置后应回到未翻译，翻译流水线才会重取"
    assert stats.empty_translation == 1
    assert stats.disputed == 1, "只有真 QA 失败的那条算打回"
    assert not any(s["id"] == sid for s in stats.samples), "重置不是打回，不进打回样本"
    assert stats.written >= 1


def test_review_stats_as_dict_exposes_new_counters():
    """防回归：新计数器必须出现在 `as_dict()`。

    CLI / daemon / HTTP 都只读 as_dict()，漏字段等于新计数对使用者不可见。
    """
    d = ReviewStats(empty_translation=2, issue_action="create", issue_id=777, issue_error="").as_dict()
    assert d["empty_translation"] == 2
    assert d["issue_action"] == "create"
    assert d["issue_id"] == 777
    assert d["issue_error"] == ""
