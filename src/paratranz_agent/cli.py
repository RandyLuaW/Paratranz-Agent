"""ptz 命令行。

约定：
- 所有会写平台的命令都默认 `--dry-run`，必须显式加 `--yes` 才真正执行；
- 输出用 rich 表格，同时可 `--json` 输出机器可读结果（供上层 Agent 编排）；
- Windows 控制台是 GBK，启动时把 stdout/stderr 重设为 UTF-8 且 errors=replace，
  避免中文日志 mojibake 直接把进程搞崩。
"""
from __future__ import annotations

import json as _json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .client import ParaTranzClient, ParaTranzError
from .config import AgentConfig, load_config
from .llm import build_llm
from .models import STAGE_ZH
from .pipelines import (
    GlossaryPipeline,
    PlanPipeline,
    ReviewPipeline,
    StyleGuidePipeline,
    TranslatePipeline,
)
from .store import Store

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="ParaTranz 汉化项目全自动 Agent：分派 / 翻译 / 校对 / 术语 / 规范",
)
console = Console()
# 进度/诊断走 stderr：`--json` 的 stdout 必须保持是纯 JSON，方便上层编排解析
err_console = Console(stderr=True)

DEFAULT_CONFIG = "config/agent.yaml"

CONFIG_TEMPLATE = """\
# paratranz-agent 配置。复制为 config/agent.yaml 后修改。
# 密钥不要写在这里：用环境变量 PTZ_API_TOKEN / PTZ_LLM_API_KEY（或 .env）。

project:
  id: 0                      # ParaTranz 项目 ID，例如 6896
  name: ""                   # 项目名，用于任务标题与规范文档
  source_lang: en
  target_lang: zh-CN
  style: "简体中文，口语自然，保留角色语气；专有名词遵循术语表"

api:
  base_url: https://paratranz.cn/api
  rate_limit_per_min: 420    # 平台硬限 480/min，留余量
  timeout: 60

llm:
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  api_key_env: OPENAI_API_KEY
  temperature: 0.2
  concurrency: 4
  batch_size: 40

translate:
  batch_size: 40
  tm_top_k: 5
  tm_min_score: 0.45
  enforce_terms: true
  use_platform_suggestions: false
  force_overwrite: false
  files: []                  # 只处理匹配这些子串的文件，空=全部
  min_length: 1

review:
  block_on: error            # error / warn / info
  llm_second_pass: true
  pass_stage: 3              # 3 已检查 / 5 已审核
  fail_stage: 2              # 2 有疑问
  length_ratio_limit: 3.5
  ascii_ratio_limit: 0.55
  batch_size: 20

tasks:
  max_strings_per_task: 800
  max_files_per_task: 6
  goal:
    translate: 1.0
    check: 0.0
    review: 0.0
  pin: true
  deadline_days: null
  title_prefix: "[自动分派]"

glossary:
  min_occurrences: 3
  max_terms: 200
  llm_propose: true

styleguide:
  internal: true
  category: announcement
  bumped: true

state_dir: .ptz-state
"""


# --------------------------------------------------------------------- 上下文
class Ctx:
    def __init__(self, config_path: str, force_mock: bool = False):
        self.mock = force_mock
        try:
            self.cfg: AgentConfig = load_config(config_path)
        except FileNotFoundError:
            if not force_mock:
                raise
            # mock 模式允许没有配置文件，用默认值演示
            self.cfg = AgentConfig.model_validate({}).resolve()
        self._client: ParaTranzClient | None = None
        self._store: Store | None = None
        self.llm = build_llm(self.cfg.llm)

    @property
    def client(self) -> ParaTranzClient:
        if self._client is None:
            if self.mock:
                from .fake import FakeParaTranz

                self._client = FakeParaTranz(
                    persist_path=str(self.cfg.state_path / "fake_platform.json")
                ).client()
                return self._client
            if not self.cfg.api.token:
                console.print(
                    Panel(
                        "缺少 API Token。\n\n"
                        "1. 打开 https://paratranz.cn/users/my 的「设置」标签页生成 Token\n"
                        "2. 设环境变量 [bold]PTZ_API_TOKEN[/bold]，或写进 .env\n\n"
                        "离线自测可用 [bold]--mock[/bold]（不发真实请求）。",
                        title="[red]未配置 Token",
                        border_style="red",
                    )
                )
                raise typer.Exit(2)
            if not self.cfg.project.id:
                console.print("[red]缺少 project.id[/red]：在 config/agent.yaml 里填项目 ID")
                raise typer.Exit(2)
            self._client = ParaTranzClient(
                token=self.cfg.api.token,
                project_id=self.cfg.project.id,
                base_url=self.cfg.api.base_url,
                rate_limit_per_min=self.cfg.api.rate_limit_per_min,
                timeout=self.cfg.api.timeout,
            )
        return self._client

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.cfg.state_path / "agent.db")
        return self._store

    def close(self) -> None:
        if self._client:
            self._client.close()
        if self._store:
            self._store.close()


def _ctx(config: str, mock: bool) -> Ctx:
    try:
        return Ctx(config, force_mock=mock)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]\n先运行 [bold]ptz init[/bold] 生成配置模板")
        raise typer.Exit(2) from exc


def _emit(data: dict, as_json: bool) -> None:
    if as_json:
        console.print_json(_json.dumps(data, ensure_ascii=False, default=str))


# ----------------------------------------------------------------------- 命令
@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"paratranz-agent {__version__}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8787, "--port", help="端口"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir", help="状态目录，默认 .ptz-state"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
    mock: bool = typer.Option(False, "--mock", help="演示模式：用内存假平台，不需要 Token"),
) -> None:
    """启动 WebUI 控制台（浏览器里填 Token、下发任务）。"""
    from .server import serve as _serve

    console.print("[bold]paratranz-agent WebUI[/bold] 启动中…")
    try:
        _serve(
            host=host,
            port=port,
            state_dir=state_dir or str(ctx_default_state_dir()),
            open_browser=not no_browser,
            mock=mock,
        )
    except ImportError as exc:  # 缺 fastapi/uvicorn
        console.print(f"[red]缺少 Web 依赖：{exc}[/red]")
        console.print('装一下：uv pip install fastapi "uvicorn[standard]"')
        raise typer.Exit(2) from exc


def ctx_default_state_dir() -> Path:
    """serve 用的默认状态目录：优先环境变量，其次 cwd/.ptz-state。"""
    import os as _os

    return Path(_os.getenv("PTZ_STATE_DIR", ".ptz-state"))


@app.command()
def init(
    path: str = typer.Option(DEFAULT_CONFIG, "--path", help="配置输出路径"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的配置"),
) -> None:
    """生成 config/agent.yaml 模板。"""
    p = Path(path)
    if p.exists() and not force:
        console.print(f"[yellow]{p} 已存在[/yellow]，加 --force 覆盖")
        raise typer.Exit(1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    console.print(f"[green]已写入[/green] {p}")
    console.print("下一步：填 project.id，然后设置环境变量 PTZ_API_TOKEN")


@app.command()
def doctor(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    mock: bool = typer.Option(False, "--mock", help="离线模式，不发真实请求"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """体检：Token、项目、权限、平台能力开关。"""
    ctx = _ctx(config, mock)
    report: dict = {"ok": True, "checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["ok"] = False

    check("config", True, f"project.id={ctx.cfg.project.id}")
    if mock:
        check("token", True, "mock 模式跳过")
    else:
        check("token", bool(ctx.cfg.api.token), "已配置" if ctx.cfg.api.token else "缺失")
    check("llm", ctx.llm.available(), f"{ctx.llm.name} / {ctx.cfg.llm.model}")

    if not mock and ctx.cfg.api.token and ctx.cfg.project.id:
        try:
            info = ctx.client.get_project()
            report["project"] = info.model_dump()
            check("api", True, f"{info.name}（{info.source}→{info.dest}）")
            check("task_feature", bool(info.extra.get("task")),
                  "已开启，可自动分派任务" if info.extra.get("task") else "未开启（extra.task=false），plan 会失败")
            check("api_edit", not bool(info.extra.get("disableApiEdit")),
                  "允许 API 写入" if not info.extra.get("disableApiEdit") else "项目禁止非管理员 API 写入")
            check("review_mode", True, f"校对等级 {info.review_mode}")
            try:
                members = ctx.client.list_members()
                report["members"] = len(members)
                check("members", True, f"{len(members)} 名成员")
            except Exception as exc:  # noqa: BLE001
                check("members", False, str(exc)[:120])
        except ParaTranzError as exc:
            check("api", False, str(exc))
    elif mock:
        check("api", True, "mock 模式跳过")

    check("state", True, str(ctx.cfg.state_path))

    if as_json:
        _emit(report, True)
    else:
        t = Table(title="ptz doctor")
        t.add_column("检查项")
        t.add_column("结果")
        t.add_column("说明")
        for c in report["checks"]:
            t.add_row(c["name"], "[green]OK[/green]" if c["ok"] else "[red]FAIL[/red]", c["detail"])
        console.print(t)
    ctx.close()
    if not report["ok"]:
        raise typer.Exit(1)


@app.command()
def status(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """项目进度概览：文件、状态分布、任务、术语、本地 TM。"""
    ctx = _ctx(config, mock)
    info = ctx.client.get_project()
    files = ctx.client.list_files()

    total = sum(f.total for f in files)
    translated = sum(f.translated for f in files)
    checked = sum(f.checked for f in files)
    reviewed = sum(f.reviewed for f in files)
    words = sum(f.words for f in files)

    stage_counts: dict[int, int] = {}
    for st in (0, 1, 2, 3, 5):
        try:
            n = sum(1 for _ in ctx.client.iter_strings(stage=st, page_size=500))
            stage_counts[st] = n
        except ParaTranzError:
            stage_counts[st] = -1

    tasks: list = []
    try:
        tasks = ctx.client.list_tasks(page=1, page_size=100)
    except ParaTranzError:
        pass

    terms = 0
    try:
        terms = sum(1 for _ in ctx.client.iter_terms())
    except ParaTranzError:
        pass

    data = {
        "project": info.name,
        "files": len(files),
        "total": total,
        "translated": translated,
        "checked": checked,
        "reviewed": reviewed,
        "words": words,
        "progress": round(translated / total * 100, 1) if total else 0.0,
        "stages": {STAGE_ZH.get(k, str(k)): v for k, v in stage_counts.items()},
        "tasks": {"total": len(tasks), "open": sum(1 for t in tasks if t.status == 0)},
        "terms": terms,
        "local_tm": ctx.store.tm_size(),
        "ledger": {
            p: ctx.store.ledger_stats(p)
            for p in ("plan", "translate", "review", "glossary", "styleguide")
        },
    }

    if as_json:
        _emit(data, True)
    else:
        console.print(Panel(
            f"[bold]{info.name}[/bold]（{info.source} → {info.dest}）\n"
            f"文件 {len(files)} · 词条 {total} · 词数 {words}\n"
            f"已翻译 {translated}（{data['progress']}%）· 已检查 {checked} · 已审核 {reviewed}\n"
            f"任务 {len(tasks)}（进行中 {data['tasks']['open']}）· 术语 {terms} 条 · 本地 TM {ctx.store.tm_size()} 条",
            title="项目状态",
        ))
        t = Table(title="词条状态分布")
        t.add_column("状态")
        t.add_column("数量", justify="right")
        for st, n in stage_counts.items():
            t.add_row(STAGE_ZH.get(st, str(st)), "—" if n < 0 else str(n))
        console.print(t)
    ctx.close()


@app.command()
def plan(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes", help="默认只预览；--yes 才真正建任务"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """自动编写并分派翻译任务。"""
    ctx = _ctx(config, mock)
    pipe = PlanPipeline(ctx.client, ctx.cfg, ctx.store)
    planned = pipe.build_plan()

    if as_json:
        _emit({"planned": [p.summary() for p in planned], "created": 0, "dry_run": dry_run}, True)
    else:
        if not planned:
            console.print("[green]没有待分派的内容[/green]（所有文件都已翻译）")
        else:
            t = Table(title=f"分派计划（{len(planned)} 个任务）")
            t.add_column("#", justify="right")
            t.add_column("标题")
            t.add_column("词条", justify="right")
            t.add_column("模式")
            for i, p in enumerate(planned, 1):
                t.add_row(str(i), p.title, str(p.strings), "文件模式" if p.mode == 1 else "词条模式")
            console.print(t)
            console.print(Panel(planned[0].content, title="首个任务说明预览", border_style="dim"))

    try:
        created = pipe.execute(planned, dry_run=dry_run)
    except RuntimeError as exc:  # 项目未开启任务功能
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if not as_json:
        if dry_run:
            console.print("[yellow]dry-run：未创建任务[/yellow]，加 --yes 执行")
        else:
            console.print(f"[green]已创建 {len(created)} 个任务[/green]")
    ctx.close()


@app.command()
def translate(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    limit: Optional[int] = typer.Option(None, "--limit", help="本次最多处理多少条"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """内容汉化：TM 命中直出 + LLM 翻译 + QA 硬拦 + 写回 stage=1。"""
    ctx = _ctx(config, mock)
    pipe = TranslatePipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm)
    stats = pipe.run(limit=limit, dry_run=dry_run)

    if as_json:
        _emit(stats.as_dict(), True)
    else:
        t = Table(title="翻译结果")
        t.add_column("指标")
        t.add_column("值", justify="right")
        for k, v in stats.as_dict().items():
            t.add_row(k, str(v))
        console.print(t)
        if stats.blocked_samples:
            console.print(Panel(
                "\n".join(
                    f"#{s['id']} {s['original'][:60]!r} → {s['translation'][:60]!r}\n    原因: {s['reason']}"
                    for s in stats.blocked_samples[:8]
                ),
                title=f"QA 拦截样例（共 {stats.blocked}）",
                border_style="yellow",
            ))
        console.print(f"LLM 用量: {ctx.llm.usage.as_dict()}")
        if dry_run:
            console.print("[yellow]dry-run：未写入平台[/yellow]，加 --yes 执行")
    ctx.close()


@app.command()
def review(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    task: Optional[int] = typer.Option(None, "--task", help="只校对某个任务关联的词条"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes"),
    include_disputed: bool = typer.Option(
        False, "--include-disputed", help="同时复核「有疑问」的条目（默认跳过，避免死循环）"
    ),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """校对：确定性 QA + LLM 二审，通过→已检查，打回→有疑问。"""
    ctx = _ctx(config, mock)

    def _progress(msg: str) -> None:
        # 打到 stderr：--json 的 stdout 必须保持是纯 JSON，上层还要解析
        err_console.print(f"[dim]· {msg}[/dim]")

    pipe = ReviewPipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm, log=_progress)
    stats = pipe.run(task=task, limit=limit, dry_run=dry_run, include_disputed=include_disputed)

    if as_json:
        _emit(stats.as_dict(), True)
    else:
        t = Table(title="校对结果")
        t.add_column("指标")
        t.add_column("值", justify="right")
        for k, v in stats.as_dict().items():
            t.add_row(k, str(v))
        console.print(t)
        if stats.samples:
            console.print(Panel(
                "\n".join(
                    f"#{s['id']} [{s['stage']}] {s['original'][:50]!r} → {s['translation'][:50]!r}"
                    + (f"\n    {s.get('reason') or ''}" if s.get("reason") else "")
                    + (f"\n    {[i['message'] for i in s.get('issues', [])][:2]}" if s.get("issues") else "")
                    for s in stats.samples[:10]
                ),
                title="打回样例",
                border_style="yellow",
            ))
        if dry_run:
            console.print("[yellow]dry-run：未修改 stage[/yellow]，加 --yes 执行")
    ctx.close()


@app.command()
def glossary(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖译法不同的既有术语"),
    out: Optional[str] = typer.Option(None, "--out", help="候选表导出为 markdown"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """整理并发布术语表。"""
    ctx = _ctx(config, mock)
    pipe = GlossaryPipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm)
    candidates = pipe.build()
    md = pipe.to_markdown(candidates)

    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(md, encoding="utf-8")
        console.print(f"[green]候选表已写入[/green] {out}")

    result = pipe.publish(candidates, dry_run=dry_run, overwrite=overwrite)
    if as_json:
        _emit({"candidates": [c.as_import() for c in candidates], **result}, True)
    else:
        console.print(Panel(md or "（无候选）", title=f"术语候选 {len(candidates)} 条", border_style="dim"))
        console.print(
            f"提交 {result['submitted']} 条，跳过 {result['skipped']} 条"
            + ("（dry-run，未写入）" if result["dry_run"] else "")
        )
    ctx.close()


@app.command()
def styleguide(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes"),
    out: Optional[str] = typer.Option(None, "--out", help="规范导出为 markdown 文件"),
    no_llm: bool = typer.Option(False, "--no-llm", help="不调用模型，只用模板"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """生成汉化规范并发布为项目公告。"""
    ctx = _ctx(config, mock)
    pipe = StyleGuidePipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm)
    md = pipe.build(with_llm=not no_llm)

    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(md, encoding="utf-8")
        console.print(f"[green]规范已写入[/green] {out}")

    result = pipe.publish(md, dry_run=dry_run)
    if as_json:
        _emit({**result, "markdown": md}, True)
    else:
        console.print(Panel(md[:2500] + ("\n…（已截断）" if len(md) > 2500 else ""),
                            title="汉化规范预览", border_style="dim"))
        console.print(
            f"公告 {result['action']}：{result['title']}"
            + ("（dry-run，未发布）" if result["dry_run"] else f"（id={result.get('id')}）")
        )
    ctx.close()


@app.command()
def writeback(
    extraction_id: str = typer.Argument(..., help="提取记录 id（ptz status 或 WebUI 提取页可见）"),
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    min_stage: int = typer.Option(3, "--min-stage", help="译文门槛：1 已翻译 / 3 已检查 / 5 已审核"),
    out_dir: str = typer.Option("", "--out-dir", help="输出目录，空=游戏目录旁的 <游戏目录>_cn"),
    mode: str = typer.Option(
        "mirror", "--mode", help="mirror=整包镜像（完整可跑）/ patch=只放改动文件"
    ),
    in_place: bool = typer.Option(False, "--in-place", help="原地修改游戏文件（危险，需 --yes）"),
    platform_file: int = typer.Option(0, "--platform-file", help="只取某个平台文件的词条（默认全项目）"),
    only: str = typer.Option("", "--only", help="只处理路径包含这些子串的文件（逗号分隔）"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes", help="默认预览，不落盘"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """把平台上确认过的译文回写到游戏文件（提取的反向链路）。"""
    from paratranz_agent.writeback import WritebackError, run_writeback

    ctx = _ctx(config, mock)

    def _progress(msg: str) -> None:
        err_console.print(f"[dim]· {msg}[/dim]")

    only_files = [s.strip() for s in only.split(",") if s.strip()]
    try:
        rep = run_writeback(
            client=ctx.client,
            state_dir=ctx.state_dir,
            job_id=extraction_id,
            min_stage=min_stage,
            out_dir=out_dir,
            mode=mode,
            in_place=in_place,
            dry_run=dry_run,
            platform_file=platform_file or None,
            only_files=only_files,
            log=_progress,
        )
    except WritebackError as exc:
        console.print(f"[red]{exc}[/red]")
        ctx.close()
        raise typer.Exit(2) from exc

    data = rep.as_dict()
    if as_json:
        _emit(data, True)
    else:
        t = Table(title="回写结果")
        t.add_column("指标")
        t.add_column("值", justify="right")
        for k in (
            "min_stage", "mode", "entries", "pulled", "skipped_stage", "skipped_empty",
            "skipped_no_original", "skipped_untracked", "files", "copied", "written",
            "mismatched", "missing", "unsupported", "failed_files",
        ):
            t.add_row(k, str(data[k]))
        console.print(t)
        console.print(f"输出目录：{data['out_dir']}" + ("（预览，未落盘）" if dry_run else ""))
        if data["mode"] == "mirror" and data["copied"]:
            console.print(f"[dim]整包镜像：{data['copied']} 个未改动文件已原样复制[/dim]")
        if data["backup_dir"]:
            console.print(f"备份目录：{data['backup_dir']}")
        if data["details"]:
            console.print(Panel(
                "\n".join(f"[{d['kind']}] {d['key']}\n    {d['reason']}" for d in data["details"][:12]),
                title="问题明细（最多显示 12 条）",
                border_style="yellow",
            ))
        if dry_run:
            console.print("[yellow]dry-run：未写入任何文件[/yellow]，加 --yes 执行")
    ctx.close()


@app.command()
def run(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes"),
    limit: Optional[int] = typer.Option(None, "--limit", help="每轮翻译条数上限"),
    skip: str = typer.Option("", "--skip", help="跳过的阶段，逗号分隔：plan,translate,review,glossary,styleguide"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """全流程：分派 → 翻译 → 校对 → 术语 → 规范。"""
    ctx = _ctx(config, mock)
    skipped = {s.strip() for s in skip.split(",") if s.strip()}
    report: dict = {"dry_run": dry_run, "stages": {}}

    # 先把平台术语同步到本地，后面 translate 的 prompt 和 review 的术语校验才有依据
    if "glossary" not in skipped or "translate" not in skipped:
        try:
            TranslatePipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm).sync_terms_from_platform()
        except Exception:  # noqa: BLE001 - 术语表可能对非成员不可见
            pass

    if "plan" not in skipped:
        pipe = PlanPipeline(ctx.client, ctx.cfg, ctx.store)
        planned = pipe.build_plan()
        try:
            created = pipe.execute(planned, dry_run=dry_run)
        except RuntimeError as exc:  # 任务功能未开启：不阻断后续步骤
            created = []
            report.setdefault("warnings", []).append(str(exc))
            if not as_json:
                console.print(f"[yellow]{exc}[/yellow]")
        report["stages"]["plan"] = {"planned": len(planned), "created": len(created)}
        if not as_json:
            console.rule("[bold]1/5 分派")
            console.print(f"计划 {len(planned)} 个任务，创建 {len(created)} 个")

    if "translate" not in skipped:
        stats = TranslatePipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm).run(
            limit=limit, dry_run=dry_run, sync_terms=False
        )
        report["stages"]["translate"] = stats.as_dict()
        if not as_json:
            console.rule("[bold]2/5 翻译")
            console.print(stats.as_dict())

    if "review" not in skipped:
        stats = ReviewPipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm).run(dry_run=dry_run)
        report["stages"]["review"] = stats.as_dict()
        if not as_json:
            console.rule("[bold]3/5 校对")
            console.print(stats.as_dict())

    if "glossary" not in skipped:
        pipe = GlossaryPipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm)
        cands = pipe.build()
        res = pipe.publish(cands, dry_run=dry_run)
        report["stages"]["glossary"] = {**res, "candidates": len(cands)}
        if not as_json:
            console.rule("[bold]4/5 术语")
            console.print(report["stages"]["glossary"])

    if "styleguide" not in skipped:
        pipe = StyleGuidePipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm)
        res = pipe.publish(dry_run=dry_run)
        report["stages"]["styleguide"] = res
        if not as_json:
            console.rule("[bold]5/5 规范")
            console.print(res)

    report["llm_usage"] = ctx.llm.usage.as_dict()
    if as_json:
        _emit(report, True)
    elif dry_run:
        console.print("\n[yellow]全程 dry-run，未写入平台。确认无误后加 --yes[/yellow]")
    ctx.close()


@app.command()
def watch(
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
    interval: int = typer.Option(600, "--interval", help="每轮间隔秒数（默认 10 分钟）"),
    max_rounds: Optional[int] = typer.Option(None, "--max-rounds", help="最多跑几轮；默认无限"),
    limit: Optional[int] = typer.Option(60, "--limit", help="每轮翻译/校对条数上限，防止跑飞"),
    with_plan: bool = typer.Option(False, "--with-plan", help="每轮顺带补建任务"),
    dry_run: bool = typer.Option(True, "--dry-run/--yes"),
    mock: bool = typer.Option(False, "--mock"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """常驻守护：定时轮询项目，有待办就干，没待办就等。Ctrl+C 退出。

    与 `run` 的区别：`run` 是一次性全流程，`watch` 是循环——
    适合挂后台（任务计划程序 / 双击 loop.bat / 开机自启）。
    每轮只做"当前该做的事"，所以不会重复翻译已完成的词条。
    """
    import time as _time

    ctx = _ctx(config, mock)
    round_no = 0
    total = {"translate": 0, "review": 0, "glossary": 0, "styleguide": 0, "plan": 0}

    def emit(msg: str) -> None:
        if not as_json:
            console.print(f"[dim]{_time.strftime('%H:%M:%S')}[/dim] {msg}")

    try:
        while True:
            round_no += 1
            if max_rounds and round_no > max_rounds:
                break
            round_report: dict = {"round": round_no, "actions": {}}
            try:
                files = ctx.client.list_files()
                pending = sum(f.pending for f in files)
                # 排除已打回(有疑问)的：它们等人处理，不算待校对
                unchecked = sum(max(0, f.translated - f.checked - f.disputed) for f in files)
                emit(f"第 {round_no} 轮：待翻译 {pending} 条，待校对 {unchecked} 条")

                if with_plan and pending > 0:
                    pipe = PlanPipeline(ctx.client, ctx.cfg, ctx.store)
                    created = pipe.execute(pipe.build_plan(), dry_run=dry_run)
                    if created:
                        total["plan"] += len(created)
                        round_report["actions"]["plan"] = len(created)
                        emit(f"  建任务 {len(created)} 个")

                if pending > 0:
                    st = TranslatePipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm).run(
                        limit=limit, dry_run=dry_run, sync_terms=(round_no == 1)
                    )
                    total["translate"] += st.written
                    round_report["actions"]["translate"] = st.as_dict()
                    emit(f"  翻译写入 {st.written} 条（拦截 {st.blocked}）")

                if unchecked > 0:
                    st = ReviewPipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm).run(
                        limit=limit, dry_run=dry_run
                    )
                    total["review"] += st.written
                    round_report["actions"]["review"] = st.as_dict()
                    emit(f"  校对处理 {st.written} 条（通过 {st.passed} / 打回 {st.disputed}）")

                # 首轮和每 6 轮做一次治理动作，避免频繁调模型
                if round_no == 1 or round_no % 6 == 0:
                    gpipe = GlossaryPipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm)
                    gres = gpipe.publish(gpipe.build(), dry_run=dry_run)
                    total["glossary"] += gres["submitted"]
                    round_report["actions"]["glossary"] = gres
                    if gres["submitted"]:
                        emit(f"  术语提交 {gres['submitted']} 条")

                    sres = StyleGuidePipeline(ctx.client, ctx.cfg, ctx.store, ctx.llm).publish(
                        dry_run=dry_run
                    )
                    total["styleguide"] += 1
                    round_report["actions"]["styleguide"] = sres
                    emit(f"  规范 {sres['action']}")

                if not round_report["actions"]:
                    emit("  无事可做，等待下一轮")

            except ParaTranzError as exc:
                emit(f"[red]  本轮失败（平台错误）：{exc}[/red]")
                round_report["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 - 守护进程不能因单轮异常退出
                emit(f"[red]  本轮失败：{exc}[/red]")
                round_report["error"] = repr(exc)

            if as_json:
                _emit({"round": round_report, "totals": total}, True)

            if max_rounds and round_no >= max_rounds:
                break
            _time.sleep(max(5, interval))

    except KeyboardInterrupt:
        if not as_json:
            console.print("\n[yellow]收到中断，已停止守护[/yellow]")

    if not as_json:
        console.print(
            f"累计：建任务 {total['plan']} · 翻译 {total['translate']} · 校对 {total['review']} "
            f"· 术语 {total['glossary']} · 规范 {total['styleguide']}"
            + ("（dry-run，未写入平台）" if dry_run else "")
        )
    ctx.close()


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
