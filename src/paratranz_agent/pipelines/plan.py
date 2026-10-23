"""任务规划与分派。

平台侧对应接口：
    POST /projects/{id}/tasks                 createTask
    POST /projects/{id}/tasks/{tid}/files     createTaskFile      (mode 0 词条 / 1 文件)
    PUT  /projects/{id}/tasks/{tid}/strings   operateTaskStrings  (op add/remove)
    POST /projects/{id}/tasks/{tid}           operateTask         (op pin/unpin/finish)

分派策略（确定性，可解释）：
1. 只挑还有未翻译词条的文件，按未翻译量降序；
2. 贪心装箱：单任务不超过 `max_strings_per_task` 条 / `max_files_per_task` 个文件；
3. 整文件未翻译 → 文件模式（mode=1，后续新增词条自动纳入追踪）；
   部分残留 → 词条模式（mode=0）+ 显式 add 未翻译词条 id，避免把已翻译的也算进进度；
4. 标题带前缀，用于去重（重跑不会重复建任务）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..config import AgentConfig
from ..models import FileItem, Stage, Task

if TYPE_CHECKING:  # pragma: no cover
    from ..client import ParaTranzClient
    from ..store import Store

TASK_TEMPLATE = """## 任务范围

**目录**：`{dirname}`（ParaTranz 文件页按目录分组，本任务只覆盖这个目录）

{file_table}

合计 **{strings}** 条未翻译词条、约 **{words}** 词。

## 翻译要求

1. **术语**：必须使用项目术语表既定译法，不得自创。遇到术语表未覆盖的重要名词，先在讨论区提出。
2. **占位符与控制符**：`\\N[1]`、`\\V[1]`、`\\C[2]`、`\\I[3]`、`%s`、`{{0}}` 等必须**原样保留**，数量与顺序一致；不得翻译、不得增删。
3. **标点**：中文语境用全角标点，省略号用「……」，不用英文 `...`。
4. **风格**：{style}
5. **换行与空白**：原文含换行/首尾空格的，译文保持一致，避免 UI 溢出。
6. **数字与变量**：数值一律保留原值，不做单位换算。

## 验收标准

- 本任务词条 stage 达到 **已翻译(1)**；
- 自动 QA 无 `error` 级问题（占位符缺失、译文与原文相同、数字丢失）；
- 抽查 10 条，语气与上下文连贯。

## 参考

- 项目公告区的《汉化规范》为最终依据；
- 有疑问的词条请置为「有疑问(2)」并在评论说明，不要留空。
"""


# 平台侧 title 上限 128 字符（client.create_task 也会再截一次）。
TITLE_LIMIT = 128
# 标题尾部「· N 文件 / M 条」的最大长度：如 " · 12 文件 / 1234567 条" = 20。
# 给计数后缀预留这段空间，title 就永远不需要被截断 —— 否则计数被切掉后
# `_COUNT_SUFFIX` 剥不干净，去重键对不上，重跑就会重复建任务。
_COUNT_RESERVE = 20
# 去重键自身可用长度。key + 计数后缀必须 ≤ 128。
KEY_LIMIT = TITLE_LIMIT - _COUNT_RESERVE
# 截断后追加的短哈希长度（sha1 前 8 位十六进制）
_HASH_HEX = 8


def _bounded_key(text: str, limit: int = KEY_LIMIT) -> str:
    """超长时保留头部并追加**原文短哈希**，而不是硬截断。

    硬截断会让共享前 N 个字符的两个长目录名拿到同一个去重键，
    结果是跨轮时其中一个目录的工作被静默跳过（看起来"没任务"）。
    哈希取自完整原文（含 part 后缀），所以 `.../aaa` 与 `.../aab`
    即使前 99 个字符相同也不会撞。
    """
    if len(text) <= limit:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:_HASH_HEX]
    head = text[: limit - _HASH_HEX - 1]
    return f"{head}~{digest}"


@dataclass
class PlannedTask:
    title: str
    content: str
    files: list[FileItem] = field(default_factory=list)
    string_ids: list[int] = field(default_factory=list)
    mode: int = 1  # 1 文件模式 / 0 词条模式
    goal: dict[str, float] = field(default_factory=dict)
    strings: int = 0
    words: int = 0
    dir: str = "(根目录)"
    # 稳定去重键：只含「前缀 + 目录 + part」，不含会变动的计数；
    # 超过 108 字符时尾部换成原文短哈希（见 `_bounded_key`）
    dedup_key: str = ""

    def summary(self) -> str:
        names = ", ".join(f.name.rsplit("/", 1)[-1] for f in self.files[:4])
        more = f" 等 {len(self.files)} 个文件" if len(self.files) > 4 else ""
        return f"{self.title} | {self.strings} 条 / {self.words} 词 | {names}{more}"


class PlanPipeline:
    name = "plan"

    def __init__(self, client: "ParaTranzClient", cfg: AgentConfig, store: "Store"):
        self.client = client
        self.cfg = cfg
        self.store = store

    # ------------------------------------------------------------------ 规划
    def _target_files(self) -> list[FileItem]:
        files = self.client.list_files()
        allow = set(self.cfg.translate.files or [])
        if allow:
            files = [f for f in files if any(a in f.name for a in allow)]
        return [f for f in files if f.pending > 0]

    def build_plan(self) -> list[PlannedTask]:
        """按**目录**分片装箱。

        为什么按目录而不是纯按量贪心：
        ParaTranz 文件页本身就是按目录分组的（`www/data/`、`localisation/`），
        每个目录是一个功能模块。跨目录混装出来的任务，成员在平台上对不上号，
        也说不清"这个任务是干什么的"。所以：
        - 同一目录的文件优先装进同一个任务；
        - 单目录总量超过上限才拆成多个任务（仍属于同一目录）；
        - 只有单文件就超限时才会独占一个任务。
        """
        cfg = self.cfg.tasks
        files = self._target_files()
        if not files:
            return []

        groups: dict[str, list[FileItem]] = {}
        for f in files:
            groups.setdefault(f.dir, []).append(f)

        # 目录按待翻译总量降序：先啃大模块
        ordered = sorted(
            groups.items(), key=lambda kv: sum(x.pending for x in kv[1]), reverse=True
        )

        planned: list[PlannedTask] = []
        idx = 0
        for dirname, flist in ordered:
            flist = sorted(flist, key=lambda f: f.pending, reverse=True)
            box: list[FileItem] = []
            cur = 0
            part = 0
            for f in flist:
                over_strings = cur + f.pending > cfg.max_strings_per_task
                over_files = len(box) >= cfg.max_files_per_task
                if box and (over_strings or over_files):
                    idx += 1
                    part += 1
                    planned.append(self._make_task(idx, dirname, box, part))
                    box, cur = [], 0
                box.append(f)
                cur += f.pending
            if box:
                idx += 1
                part += 1
                planned.append(self._make_task(idx, dirname, box, part))
        return planned

    def _make_task(
        self, idx: int, dirname: str, box: list[FileItem], part: int
    ) -> PlannedTask:
        cfg = self.cfg.tasks
        total = sum(f.pending for f in box)
        words = sum(
            max(1, round(f.words * (f.pending / f.total))) if f.total else 0 for f in box
        )
        whole = all(f.translated == 0 for f in box)
        mode = 1 if whole else 0
        suffix = f" #{part}" if part > 1 else ""
        # 计数会随进度变化，不能进去重键，否则重跑会当成新任务。
        # 长目录名走 `head~哈希` 的稳定形态：哈希覆盖 dirname + part，
        # 截断再也不会把 `#2` 切掉，也不会让两个长目录名撞成同一个键。
        dedup_key = _bounded_key(f"{cfg.title_prefix} {dirname}{suffix}")
        count = f" · {len(box)} 文件 / {total} 条"
        # title = 去重键 + 计数后缀，总长 ≤ KEY_LIMIT + _COUNT_RESERVE = 128，
        # 平台不会二次截断，`_existing_titles()` 剥掉后缀后正好还原 dedup_key。
        title = f"{dedup_key}{count}"
        content = TASK_TEMPLATE.format(
            dirname=dirname,
            file_table=self._file_table(box),
            strings=total,
            words=words,
            style=self.cfg.project.style,
        )
        return PlannedTask(
            title=title,
            dedup_key=dedup_key,
            content=content,
            files=box,
            mode=mode,
            goal=dict(cfg.goal),
            strings=total,
            words=words,
            dir=dirname,
        )

    @staticmethod
    def _file_table(files: list[FileItem]) -> str:
        rows = ["| 文件 | 未翻译 | 总词条 | 总词数 |", "| --- | ---: | ---: | ---: |"]
        for f in files:
            rows.append(f"| `{f.name}` | {f.pending} | {f.total} | {f.words} |")
        return "\n".join(rows)

    # ------------------------------------------------------------------ 执行
    # 「· N 文件 / M 条」里的数字会随进度变，去掉它才能当稳定去重键
    _COUNT_SUFFIX = re.compile(r"\s*·\s*\d+\s*文件\s*/\s*\d+\s*条\s*$")

    # 任务列表最多翻多少页。平台若忽略 page 参数（永远返回同一页），
    # 没有硬上限就会死循环；20 页 × 100 条 = 2000 个任务，足够真实项目用。
    MAX_TASK_PAGES = 20
    TASK_PAGE_SIZE = 100

    def _existing_titles(self) -> set[str]:
        """翻页拉全量已有任务的标题，剥掉计数后缀后作为去重集合。

        只读第 1 页（100 条）时，项目任务数一过 100，去重就失效：
        后面的任务看不见 → 每轮都给它们再建一个同名任务。
        所以这里翻页取全量，并用三重条件保证一定会停：
        1. 硬上限 `MAX_TASK_PAGES` 页；
        2. 拿到空页视为到底（**不**用"不足一页"判断：服务端可能把 pageSize
           压到 50 以下，用条数判断会提前收工，又回到只读一部分的老问题）；
        3. 连续两页拿到同一批 id → 平台没认 page 参数，立即停。
        """
        titles: set[str] = set()
        prev_ids: set[int] = set()
        page = 1
        while page <= self.MAX_TASK_PAGES:
            try:
                batch = self.client.list_tasks(page=page, page_size=self.TASK_PAGE_SIZE)
            except Exception:  # noqa: BLE001 - 任务功能可能未开启，退回已拿到的部分
                break
            if not batch:
                break
            ids = {t.id for t in batch}
            if ids == prev_ids:  # page 被忽略，再翻也是同一页
                break
            titles.update(t.title for t in batch)
            prev_ids = ids
            page += 1
        return {self._COUNT_SUFFIX.sub("", t).strip()[:TITLE_LIMIT] for t in titles}

    def _pending_string_ids(self, file_id: int) -> list[int]:
        """只分页拉未翻译词条，避免把整文件（含已翻译）读进内存。"""
        return [
            s.id
            for s in self.client.iter_strings(file=[file_id], stage=Stage.UNTRANSLATED, page_size=500)
            if not s.translation.strip()
        ]

    def _require_task_feature(self) -> None:
        """提前检查项目是否开启任务功能，给出可执行的提示而不是裸 403。"""
        try:
            info = self.client.get_project()
        except Exception:  # noqa: BLE001 - 读不到项目信息就交给后续调用报错
            return
        if not info.extra.get("task"):
            raise RuntimeError(
                "项目未开启「任务」功能（extra.task=false），无法自动分派。"
                "请项目管理员在 ParaTranz 项目设置 → 功能 中启用任务，或改用 "
                "`ptz translate --yes` 直接翻译。"
            )

    def execute(self, plan: list[PlannedTask] | None = None, *, dry_run: bool = False) -> list[Task]:
        plan = plan if plan is not None else self.build_plan()
        if not plan:
            return []
        if not dry_run:  # 预览不写平台，任务功能未开启也允许看方案
            self._require_task_feature()
        existing = self._existing_titles()
        created: list[Task] = []

        for item in plan:
            key = item.dedup_key or item.title
            if key in existing:
                continue
            if dry_run:
                continue

            task = self.client.create_task(
                title=item.title,
                content=item.content,
                goal=item.goal,
                deadline=self._deadline(),
                bumped=self.cfg.tasks.pin,
            )
            # 同轮里如果出现同一 dedup_key（长目录名截断碰撞等），不要重复建
            existing.add(key)

            try:
                for f in item.files:
                    self.client.add_task_file(task.id, f.id, mode=item.mode)
                    if item.mode == 0:
                        ids = self._pending_string_ids(f.id)
                        for i in range(0, len(ids), 1000):
                            self.client.operate_task_strings(task.id, "add", ids[i : i + 1000])
            except Exception:
                # 建任务中途失败（文件已在别的任务里等）→ 删掉这个孤儿任务再抛
                self.client.delete_task(task.id)
                existing.discard(key)
                raise

            if self.cfg.tasks.pin:
                try:
                    self.client.operate_task(task.id, "pin")
                except Exception:  # noqa: BLE001 - 置顶失败不影响任务本体
                    pass

            self.store.ledger_mark(self.name, str(task.id), "create", item.title)
            self.store.ledger_commit()
            created.append(task)

        return created

    def _deadline(self) -> str | None:
        days = self.cfg.tasks.deadline_days
        if not days:
            return None
        import datetime as _dt

        return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days)).isoformat()
