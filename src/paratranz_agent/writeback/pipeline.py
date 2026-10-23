"""回写流水线：平台确认译文 → 按 key 定位 → 校验原文 → 写回游戏文件。

**译文的来源**：平台项目里**经过 translate → review 流程确认过**的词条
（默认门槛 `min_stage=3`，即「已检查」；也可要求 5「已审核」），
用项目的 API Token 拉回本地，绝不使用未确认的机翻。

链路：

    extraction job（.ptz-state 里的 entries.jsonl + game_dir + 提取器参数）
            │  取 key / original / options
            ▼
    平台 GET /strings（按 stage 门槛过滤）──▶ {key: translation}
            │
            ▼
    按 `<rel>::<locator>` 分组 → 逐文件用对应写回器「手术式」替换
            │
            ▼
    输出目录（默认 <game_dir>_cn，只放被改动的文件）  或  原地写 + 自动备份

三条安全线（同一个理由：**宁可少写，不可错写**）：
1. **原文校验**：文件里当前那段文本必须等于平台记录的原文，否则跳过并报 mismatched
   —— 游戏本体更新过之后，key 的行号/路径可能整体错位，硬写会毁数据；
2. **默认不原地改**：只把被改动的文件按相对路径输出到 `<game_dir>_cn`；
3. **原地改要显式授权 + 自动备份**：备份落在 **state 目录**（不能放进游戏目录，
   否则下次提取的 `**/*.json` 会把备份再扫一遍），并写 manifest 供还原。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..filematch import matches_any
from ..server import extraction
from .apply import apply_items, family_of_key
from .base import WriteItem, split_key

MIN_STAGE_ZH = {1: "已翻译", 2: "有疑问", 3: "已检查", 5: "已审核", 9: "已锁定"}

# 回写产出的两种形态
#   mirror：目标目录是**一份完整可跑的游戏**——不影响的文件原样复制过去，
#           被影响的文件直接写改写后的版本（不先复制再覆盖，省一遍 IO）
#   patch ：只放被影响的那几个文件（体积最小，适合先比对再合入）
WB_MODES = {"mirror": "整包镜像（完整可跑）", "patch": "仅改动文件"}

# 问题明细最多留多少条，避免几万条把报告撑爆
MAX_DETAILS = 50

# 每复制多少个文件报一次进度（整包镜像可能是几万个文件）
COPY_LOG_EVERY = 500

# 每个 job 只保留最近几份备份。一次原地回写就是一份完整还原点，只增不减会把
# state 目录一路涨到几个 G（用户根本不会去清那个目录）。
BACKUP_KEEP = 5

# key 里的文件路径越界时的原因文案
BAD_REL_REASON = "key 里的文件路径不合法（绝对路径或含 ..）"


class WritebackError(RuntimeError):
    """回写前置条件不满足（此时不写任何文件）。"""


@dataclass
class WritebackReport:
    """一次回写的结果。所有数字都来自实际动作，没有推算。"""

    job_id: str = ""
    extractor_id: str = ""
    game_dir: str = ""
    out_dir: str = ""
    mode: str = "mirror"
    in_place: bool = False
    dry_run: bool = True
    min_stage: int = 3
    entries: int = 0                 # 提取记录里的词条总数
    pulled: int = 0                  # 平台上达到门槛且有译文的条数
    skipped_stage: int = 0           # 平台上有但没到门槛
    skipped_empty: int = 0           # 到了门槛但译文为空
    skipped_no_original: int = 0     # 平台没记原文，无法校验位置（一律不写）
    skipped_untracked: int = 0       # 提取记录有、平台上没这个 key
    files_unmatched: int = 0         # entries 里有、但被 only_files 全部挡掉的相对路径数（去重）
    files: int = 0                   # 实际产生改动的文件数
    copied: int = 0                  # 镜像模式下原样复制过去的文件数
    written: int = 0
    mismatched: int = 0
    missing: int = 0
    unsupported: int = 0
    failed_files: int = 0
    backup_dir: str = ""
    details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "extractor_id": self.extractor_id,
            "game_dir": self.game_dir,
            "out_dir": self.out_dir,
            "mode": self.mode,
            "in_place": self.in_place,
            "dry_run": self.dry_run,
            "min_stage": self.min_stage,
            "entries": self.entries,
            "pulled": self.pulled,
            "skipped_stage": self.skipped_stage,
            "skipped_empty": self.skipped_empty,
            "skipped_no_original": self.skipped_no_original,
            "skipped_untracked": self.skipped_untracked,
            "files_unmatched": self.files_unmatched,
            "files": self.files,
            "copied": self.copied,
            "written": self.written,
            "mismatched": self.mismatched,
            "missing": self.missing,
            "unsupported": self.unsupported,
            "failed_files": self.failed_files,
            "backup_dir": self.backup_dir,
            "details": self.details,
            "errors": self.errors,
        }

    def add_detail(self, kind: str, key: str, reason: str) -> None:
        if len(self.details) < MAX_DETAILS:
            self.details.append({"kind": kind, "key": key, "reason": reason})

    def add_error(self, msg: str) -> None:
        # 错误列表也要设上限：几万条跨行命中时，无上限的列表会把报告和日志撑爆
        if len(self.errors) < MAX_DETAILS:
            self.errors.append(msg)
        elif len(self.errors) == MAX_DETAILS:
            self.errors.append(f"…错误过多，仅保留前 {MAX_DETAILS} 条")


# --------------------------------------------------------------------- 拉译文
def pull_translations(
    client: Any,
    *,
    min_stage: int = 3,
    file_id: int | None = None,
    max_items: int = 200_000,
) -> tuple[dict[str, WriteItem], dict[str, int]]:
    """从平台拉「确认过的译文」，返回 `({key: WriteItem}, 统计)`。

    `file_id` 为空表示扫整个项目：key 全局唯一，按 key 匹配即可。
    锁定态（stage=9）视为已确认——它比任何门槛都更终局。

    **原文为空的一律不取**：整条回写的安全模型建立在「文件里当前这段必须等于
    平台记录的原文」上；原文缺失就没法校验，写进去等于盲写位置。宁可少写。
    """
    out: dict[str, WriteItem] = {}
    stats = {"scanned": 0, "skipped_stage": 0, "skipped_empty": 0, "skipped_no_original": 0}
    files = [file_id] if file_id else None
    for s in client.iter_strings(file=files, max_items=max_items):
        stats["scanned"] += 1
        stage = int(s.stage)
        translation = (s.translation or "").strip()
        if stage < min_stage and stage != 9:
            stats["skipped_stage"] += 1
            continue
        if not translation:
            stats["skipped_empty"] += 1
            continue
        if not (s.original or "").strip():
            stats["skipped_no_original"] += 1
            continue
        out[s.key] = WriteItem(
            key=s.key, translation=translation, original=s.original or "", stage=stage
        )
    return out, stats


# --------------------------------------------------------------------- 主流程
def run_writeback(
    *,
    client: Any,
    state_dir: Path,
    job_id: str,
    min_stage: int = 3,
    out_dir: str | Path = "",
    mode: str = "mirror",
    in_place: bool = False,
    dry_run: bool = True,
    platform_file: int | None = None,
    only_files: Sequence[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> WritebackReport:
    """执行一次回写。前置条件不满足就抛 `WritebackError`，此时一个文件都没碰。

    `mode`：`mirror`=目标目录是完整可跑的游戏（未改动文件直接复制）；
    `patch`=只放被改动的文件。
    """
    say = log or (lambda _m: None)
    meta = extraction.get_job(state_dir, job_id)
    entries = extraction.load_entries(state_dir, job_id)
    if not entries:
        raise WritebackError("该提取记录没有词条（entries.jsonl 为空）")

    gdir = Path(meta.get("game_dir") or "")
    if not gdir.is_dir():
        raise WritebackError(f"提取记录里的游戏目录已不存在：{gdir}")

    rep = WritebackReport(
        job_id=job_id,
        extractor_id=str(meta.get("extractor_id") or ""),
        game_dir=str(gdir),
        dry_run=dry_run,
        in_place=in_place,
        min_stage=min_stage,
        entries=len(entries),
    )
    mode = (mode or "mirror").strip().lower()
    if mode not in WB_MODES:
        raise WritebackError(f"未知的回写形式：{mode}（可选 {' / '.join(WB_MODES)}）")
    rep.mode = mode
    opts: Mapping[str, str] = {k: str(v) for k, v in (meta.get("options") or {}).items()}
    # 提取器支持的 encoding（如 generic-regex 的 `--opt encoding=gbk`）回写必须沿用：
    # 硬写 utf-8 会把非 UTF-8 文件写成「原行还是 GBK、新译文是 UTF-8」的混合编码，
    # 游戏读它就是乱码或直接崩（Ren'Py 遇到非法序列会抛异常）。
    file_encoding = (opts.get("encoding") or "utf-8").strip() or "utf-8"
    say(f"提取记录 {job_id}：{len(entries)} 条词条，提取器 {rep.extractor_id or '?'}"
        f"（文件编码 {file_encoding}）")

    say(f"从平台拉取达到「{MIN_STAGE_ZH.get(min_stage, min_stage)}」的译文…")
    pulled, pstats = pull_translations(client, min_stage=min_stage, file_id=platform_file)
    rep.pulled = len(pulled)
    rep.skipped_stage = pstats["skipped_stage"]
    rep.skipped_empty = pstats["skipped_empty"]
    rep.skipped_no_original = pstats.get("skipped_no_original", 0)
    say(
        f"平台扫描 {pstats['scanned']} 条：可用译文 {len(pulled)}、"
        f"未达门槛 {pstats['skipped_stage']}、译文为空 {pstats['skipped_empty']}、"
        f"平台缺原文 {rep.skipped_no_original}"
    )

    by_file: dict[str, list[WriteItem]] = {}
    # 白名单先归一化：反斜杠统一成斜杠、去掉首尾斜杠，否则 `--only data\` 与
    # `--only data/` 会判成两回事，而用户在 Windows 上两种都会写
    wanted: list[str] = []
    for raw in only_files or []:
        a = raw.replace("\\", "/").strip().strip("/")
        if a and a not in wanted:
            wanted.append(a)
    blocked: set[str] = set()
    for e in entries:
        key = str(e.get("key") or "")
        if not key:
            continue
        rel, _locator = split_key(key)
        if not rel:
            rep.unsupported += 1
            rep.add_detail("unsupported", key, "key 里没有文件相对路径")
            continue
        if not _safe_rel(rel):
            # 畸形 key（`../outside.json::k`、`C:/x.json::k`）绝不能拿去拼路径：
            # patch 模式下 `target_root / rel` 会直接写到输出目录之外
            rep.unsupported += 1
            rep.add_detail("unsupported", key, BAD_REL_REASON)
            continue
        if wanted and not matches_any(rel, wanted):
            blocked.add(rel)
            continue
        item = pulled.get(key)
        if item is None:
            rep.skipped_untracked += 1
            continue
        by_file.setdefault(rel, []).append(item)
    rep.files_unmatched = len(blocked)

    if not by_file:
        if wanted:
            # 白名单配了却一条都没写：必须说清楚是哪一步挡的，否则「筛错了文件」
            # 和「平台没译文」在日志里长得一模一样，用户会以为回写已经生效
            say(
                f"only_files 没有匹配任何文件（筛选条件 {wanted} 挡掉了 "
                f"{rep.files_unmatched} 个相对路径）"
                if rep.files_unmatched
                else f"only_files 命中的文件都没有可回写的译文（筛选条件 {wanted}）"
            )
        say("没有可回写的词条：平台上找不到这些 key 的确认译文")
        return rep

    target_root = _resolve_out(gdir, out_dir, in_place)
    rep.out_dir = str(target_root)
    backup_dir: Path | None = None
    if in_place:
        # 一次运行只算一次备份目录：按文件调用会散落到不同时间戳目录，
        # manifest 就找不到自己那批备份了（曾在这里踩过）
        if dry_run:
            # 预览不落盘，也就不会创建备份目录 —— 这时把 `backup_dir` 填上
            # 会让报告/CLI 声称"已备份到某处"，而那个目录根本不存在。
            # 报告里的数字与路径必须是事实，所以预览时留空并说明。
            say("⚠ 原地写入（预览）：不会落盘，也不会创建备份目录")
        else:
            backup_dir = _backup_root(state_dir, job_id)
            rep.backup_dir = str(backup_dir)
            say(f"⚠ 原地写入：改动前会备份到 {backup_dir}")
    else:
        _check_out_dir(gdir, target_root)
        if mode == "mirror":
            say(f"输出目录：{target_root}（整包镜像：未改动的文件直接复制，改动文件写改写版）")
        else:
            say(f"输出目录：{target_root}（只放被改动的文件）")

    manifest: dict[str, dict[str, str]] = {}
    # **真正被改写的文件**（不是「有待写词条的文件」）：镜像模式靠它决定跳过谁。
    # 用 by_file 当跳过集会漏掉「该文件的词条全部失败」的情况——那个文件既不复制
    # 也不写出，输出目录里凭空少一个 data json，而报告一片绿。
    changed: set[str] = set()
    say(f"待处理文件 {len(by_file)} 个")

    for rel, items in sorted(by_file.items()):
        src = gdir / rel
        if not src.is_file():
            rep.failed_files += 1
            msg = f"源文件不存在：{src}"
            rep.add_error(msg)
            for it in items:
                rep.add_detail("error", it.key, msg)
            continue
        try:
            original_text = src.read_text(encoding=file_encoding, errors="surrogateescape")
        except OSError as exc:
            rep.failed_files += 1
            rep.add_error(f"读不了 {rel}：{exc}")
            continue

        content = original_text
        file_written = 0
        res = apply_items(content, items, opts=opts)
        content = res.content
        file_written += len(res.written)
        rep.written += len(res.written)
        for key, why in res.mismatched:
            rep.mismatched += 1
            rep.add_detail("mismatched", key, why)
        for key in res.missing:
            rep.missing += 1
            rep.add_detail("missing", key, "定位符在文件里找不到")
        for key, why in res.errors:
            rep.add_detail("error", key, why)
            rep.add_error(f"{rel}: {key}: {why}")
        for key in res.unsupported:
            rep.unsupported += 1
            rep.add_detail("unsupported", key, "没有写回器认这个定位符")

        if not file_written:
            continue
        rep.files += 1
        if dry_run:
            changed.add(rel)
            continue

        if in_place:
            digest = hashlib.sha256(
                original_text.encode(file_encoding, "surrogateescape")
            ).hexdigest()
            assert backup_dir is not None
            bak = _write_backup(backup_dir, rel, original_text, file_encoding)
            manifest[rel] = {"backup": str(bak), "sha256": digest}
        dst = src if in_place else target_root / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding=file_encoding, errors="surrogateescape")
        except OSError as exc:
            # 单个文件写不进去（只读、被占用、路径过长）不能中断整轮：
            # 否则后面所有文件都不写，而 manifest 与报告会一起丢
            rep.failed_files += 1
            rep.add_error(f"写不进 {rel}：{exc}")
            continue
        changed.add(rel)

    if manifest:
        mf = Path(rep.backup_dir) / "manifest.json"
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        say(f"备份清单：{mf}")

    # 镜像模式：把没被改动的文件原样复制过去，让目标目录成为一份完整可跑的游戏。
    # 放在改写字之后执行，这样"被改动的文件"直接是改写版，不用先复制再覆盖。
    if not in_place and mode == "mirror" and not dry_run:
        rep.copied = _mirror_rest(gdir, target_root, changed, say)
        say(f"镜像完成：复制未改动文件 {rep.copied} 个 → {target_root}")

    say(
        f"回写{'预览' if dry_run else '完成'}：写入 {rep.written} 条 / {rep.files} 个文件；"
        f"原文不一致 {rep.mismatched}、定位不到 {rep.missing}、"
        f"不支持 {rep.unsupported}、失败文件 {rep.failed_files}"
    )
    return rep


# --------------------------------------------------------------------- 内部
def _mirror_rest(gdir: Path, target_root: Path, skip: set[str], say: Callable[[str], None]) -> int:
    """把 game_dir 里**未被改写**的文件按相对路径复制到 target_root。

    被改动的文件不在这里（已由主流程直接写入），所以同一批 IO 不会做两遍。
    二进制文件按字节复制，不做任何解码——游戏目录里必然有图片/音频/字体。

    **重复运行必须能同步源文件的更新**：早期版本用「目标已存在就跳过」，
    结果第二轮回写时源文件改过也不生效，镜像里永远是最旧的副本。
    现在改为总是覆盖，仅在「大小与 mtime 都一致」时才跳过（copy2 保留 mtime，
    所以未改动文件会命中这个快速路径，不重复写盘）。
    """
    # 用归一化 key 比对：Windows 上路径分隔符/大小写都可能与提取 key 不一致
    skip_keys = {_norm_rel(r) for r in skip}
    copied = 0
    skipped = 0
    unchanged = 0
    for p in sorted(gdir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(gdir).as_posix()
        if _norm_rel(rel) in skip_keys:
            skipped += 1
            continue
        dst = target_root / rel
        try:
            if dst.exists():
                s, d = p.stat(), dst.stat()
                if s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime):
                    unchanged += 1
                    continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
        except OSError as exc:
            say(f"复制失败 {rel}：{exc}")
            continue
        copied += 1
        if copied % COPY_LOG_EVERY == 0:
            say(f"已复制 {copied} 个文件…")
    if skipped:
        say(f"跳过 {skipped} 个（是本次改写的文件）")
    if unchanged:
        say(f"未变化 {unchanged} 个（大小与时间戳一致，不重复写盘）")
    return copied


def _norm_rel(rel: str) -> str:
    """相对路径归一：统一斜杠 + 小写（Windows 大小写不敏感）。"""
    return rel.replace("\\", "/").strip("/").casefold()


def _safe_rel(rel: str) -> bool:
    """key 里的文件相对路径能不能安全地拼到目录下面（game_dir / 输出目录 / 备份目录）。

    拒绝四类写法：
    - 空串 —— 没有相对路径就无从定位；
    - 绝对路径（`/x.json`、`\\\\host\\share\\x.json`）—— 拼上去会丢掉前缀；
    - 任何 `..` 路径段 —— `target_root / "../outside.json"` 直接跳出目录树，
      patch 模式下就是「key 指向哪儿就往哪儿写」，能读写游戏目录之外的任意文件；
    - 含 `:` 的路径 —— Windows 盘符（`C:/x.json`，`C:x.json` 是相对盘符的写法，
      同样会跑出目录）与 NTFS 数据流（`a.json:ads`，写它等于往文件里插一段隐藏流）。

    只认路径段等于 `..`，`data/..x.json` 这种正常文件名照常通过。
    """
    if not rel or ":" in rel:
        return False
    if rel[0] in ("/", "\\"):
        return False
    return all(part != ".." for part in rel.replace("\\", "/").split("/"))


def _resolve_out(gdir: Path, out_dir: str | Path, in_place: bool) -> Path:
    if in_place:
        return gdir
    if out_dir:
        return Path(out_dir).expanduser().resolve()
    return gdir.parent / f"{gdir.name}_cn"


def _check_out_dir(gdir: Path, out_dir: Path) -> None:
    """输出目录与游戏目录不能互相包含，且不能是盘符根/系统目录。

    镜像模式会把游戏目录整份复制过去——如果输出目录在游戏目录**里面**，
    复制会把自己也当成素材，轻则目录翻倍、重则无限递归把磁盘写满。
    另外 `--out-dir C:\\` 这种写法会把上千个游戏文件平铺到盘根，同样要拦。
    """
    g = gdir.resolve()
    o = out_dir.resolve()
    if g == o:
        raise WritebackError("输出目录不能就是游戏目录本身（想原地改请用 in_place）")
    if g in o.parents:
        raise WritebackError(f"输出目录不能放在游戏目录里面：{o} 在 {g} 之下")
    if o in g.parents:
        raise WritebackError(f"输出目录不能是游戏目录的上级目录：{o} 包含 {g}")
    if o.parent == o:
        raise WritebackError(f"输出目录不能是盘符根目录：{o}")
    lowered = {p.name.lower() for p in list(o.parents)[:3]}
    if lowered & {"windows", "program files", "program files (x86)", "programdata", "system volume information"}:
        raise WritebackError(f"输出目录不能是系统目录：{o}")


def _safe(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return out[:80] or "unknown"


def _backup_root(state_dir: Path, job_id: str) -> Path:
    """本次回写要用的备份目录（只算路径，不建目录）。

    必须落在 state 目录：放进游戏目录会被下次提取的 glob 再扫一遍。
    """
    base = state_dir / "writeback" / _safe(job_id)
    # 秒级时间戳不够：同一秒里跑两轮回写（脚本连着跑、或前端连点两次）会撞到
    # 同一个目录名，第二轮的备份写进第一轮的目录，还原点就退化成「第一次 run
    # 之后的状态」了。加短随机后缀消歧，时间戳前缀留着方便人眼看是哪一轮。
    name = f"backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    _prune_backups(base, current=name)
    return base / name


def _prune_backups(base: Path, *, current: str) -> None:
    """只保留最近 `BACKUP_KEEP` 份备份（含本次这一份）。

    目录名以时间戳开头，按名字倒序排就是时间倒序。清理失败一律忽略：
    多留一份备份只是占点空间，让整轮回写因为清理失败而失败不可接受。
    """
    try:
        names = {p.name for p in base.iterdir() if p.is_dir() and p.name.startswith("backup-")}
    except OSError:
        return  # 目录还不存在：这是第一次回写
    names.add(current)
    for name in sorted(names, reverse=True)[BACKUP_KEEP:]:
        try:
            shutil.rmtree(base / name)
        except OSError:
            continue


def _write_backup(backup_dir: Path, rel: str, text: str, encoding: str = "utf-8") -> Path:
    bak = backup_dir / rel
    bak.parent.mkdir(parents=True, exist_ok=True)
    bak.write_text(text, encoding=encoding, errors="surrogateescape")
    return bak


def locator_family_of(key: str) -> str:
    """诊断用：这个 key 归哪一族，认不出返回 unknown。"""
    return family_of_key(key)
