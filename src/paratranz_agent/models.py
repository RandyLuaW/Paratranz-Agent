"""ParaTranz 数据模型（对齐 OpenAPI v0.5.7）。"""
from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Stage(IntEnum):
    UNTRANSLATED = 0
    TRANSLATED = 1
    DISPUTED = 2
    CHECKED = 3
    REVIEWED = 5
    LOCKED = 9
    HIDDEN = -1


STAGE_ZH = {
    0: "未翻译",
    1: "已翻译",
    2: "有疑问",
    3: "已检查",
    5: "已审核",
    9: "锁定",
    -1: "隐藏",
}


class Permission(IntEnum):
    TRANSLATOR = 1
    PROOFREADER = 2
    ADMIN = 3
    OWNER = 10


PERMISSION_ZH = {1: "翻译", 2: "校对", 3: "管理员", 10: "创建者"}


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ProjectInfo(_Base):
    id: int
    name: str = ""
    desc: str = ""
    source: str = "en"
    dest: str = "zh-CN"
    game: str = ""
    members: int = 0
    privacy: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def review_mode(self) -> int:
        return int(self.extra.get("reviewMode", 1) or 0)

    @property
    def terms_mode(self) -> int:
        return int(self.extra.get("termsMode", 0) or 0)


class FileItem(_Base):
    id: int
    name: str = ""
    # 平台统计字段
    total: int = 0
    translated: int = 0
    checked: int = 0
    reviewed: int = 0
    disputed: int = 0
    hidden: int = 0
    locked: int = 0
    words: int = 0
    hash: str = ""

    @property
    def pending(self) -> int:
        """待翻译条数。

        平台的 `total` **包含 hidden（stage=-1）**，而 `translated` 只统计 stage>=1，
        两个口径不一致。不减掉 `hidden` 就会把隐藏词条算成"待翻译"：任务标题/说明里
        的条数虚高，文件模式下该文件还永远到不了 100%。
        """
        return max(0, self.total - self.translated - self.hidden)

    @property
    def progress(self) -> float:
        return (self.translated / self.total) if self.total else 0.0

    @property
    def dir(self) -> str:
        """平台的分片单位是目录（如 `www/data/`、`localisation/`）。

        ParaTranz 文件页就是按目录分组的，每个目录给出汇总进度。
        任务分派跟随这个划分，成员认知才一致。
        """
        return self.name.rsplit("/", 1)[0] + "/" if "/" in self.name else "(根目录)"


class StringItem(_Base):
    id: int
    key: str = ""
    original: str = ""
    translation: str = ""
    context: str | None = None
    stage: int = 0
    fileId: int | None = Field(default=None, alias="fileId")
    # 真实 API 的 `file` 是对象 {id,name,project}（同时也给 fileId），
    # 旧版/假平台可能直接给 int —— 两种都要能解析
    file: int | dict[str, Any] | None = None

    @property
    def file_id(self) -> int | None:
        if self.fileId is not None:
            return self.fileId
        if isinstance(self.file, int):
            return self.file
        if isinstance(self.file, dict):
            fid = self.file.get("id")
            return int(fid) if isinstance(fid, (int, str)) and str(fid).isdigit() else None
        return None


class TaskGoal(_Base):
    translate: float = 1.0
    check: float = 0.0
    review: float = 0.0


class TaskFile(_Base):
    id: int | None = None
    task: int | None = None
    file: Any = None
    mode: int = 0
    total: int = 0
    translated: int = 0
    checked: int = 0
    reviewed: int = 0
    disputed: int = 0
    hidden: int = 0
    locked: int = 0
    words: int = 0
    progress: dict[str, float] = Field(default_factory=dict)

    @property
    def file_id(self) -> int | None:
        if isinstance(self.file, dict):
            return self.file.get("id")
        if isinstance(self.file, int):
            return self.file
        return None

    @property
    def file_name(self) -> str:
        if isinstance(self.file, dict):
            return str(self.file.get("name", ""))
        return ""


class Task(_Base):
    id: int
    title: str = ""
    content: str = ""
    html: str = ""
    status: int = 0
    deadline: str | None = None
    goal: TaskGoal = Field(default_factory=TaskGoal)
    bumpedAt: str | None = None
    taskFiles: list[TaskFile] = Field(default_factory=list)
    words: dict[str, int] = Field(default_factory=dict)
    uid: int | None = None


class Term(_Base):
    id: int | None = None
    term: str
    translation: str = ""
    pos: str | None = None
    note: str | None = None
    variants: list[str] = Field(default_factory=list)
    caseSensitive: bool = False
    project: int | None = None

    @field_validator("variants", mode="before")
    @classmethod
    def _variants_none_to_empty(cls, v: Any) -> Any:
        """平台对「没有变体」的术语返回 `variants: null`。

        真的踩过：`create_term` 不传 variants → 平台存 null → 之后**任何**
        `iter_terms()` 都在这里抛 ValidationError，而 glossary 的第一步就是
        iter_terms，等于「用户手建过一个无变体术语，整条术语链永久失效」。
        """
        return [] if v is None else v


class Member(_Base):
    id: int | None = None
    uid: int
    user: dict[str, Any] = Field(default_factory=dict)
    permission: int = 1
    totalPoints: float = 0.0
    translated: int = 0
    edited: int = 0
    reviewed: int = 0
    note: str = ""

    @property
    def name(self) -> str:
        return str(self.user.get("username") or self.user.get("nickname") or self.uid)


class Announcement(_Base):
    id: int
    title: str = ""
    content: str = ""
    category: str = "announcement"
    internal: bool = False
    bumpedAt: str | None = None
