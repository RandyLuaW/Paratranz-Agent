"""配置：config/agent.yaml + 环境变量覆盖。

环境变量优先级最高，命名规则 PTZ_<SECTION>_<KEY>，例如 PTZ_API_TOKEN。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr


class ProjectCfg(BaseModel):
    id: int = 0
    name: str = ""
    source_lang: str = "en"
    target_lang: str = "zh-CN"
    # 目标风格，注入 prompt
    style: str = "简体中文，口语自然，保留角色语气；专有名词遵循术语表"


class ApiCfg(BaseModel):
    base_url: str = "https://paratranz.cn/api"
    token: str = ""
    rate_limit_per_min: int = 420
    timeout: float = 60.0


class LLMCfg(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.2
    concurrency: int = 4
    timeout: float = 120.0
    max_retries: int = 3
    # 单次请求最多带多少条词条
    batch_size: int = 40
    # 模型级附加请求体参数（top_p / reasoning_effort 等），合并进请求体
    extra_params: dict[str, Any] = Field(default_factory=dict)


class TranslateCfg(BaseModel):
    batch_size: int = 40
    # 每批塞多少条 TM 参考
    tm_top_k: int = 5
    # 低于该相似度的 TM 不作为参考
    tm_min_score: float = 0.45
    # 术语表命中时是否强制替换
    enforce_terms: bool = True
    # 是否调用平台 suggestions 接口补充参考（串行，慢）
    use_platform_suggestions: bool = False
    # 写回时是否强制覆盖人工译文
    force_overwrite: bool = False
    # 只处理这些文件（空=全部）
    files: list[str] = Field(default_factory=list)
    # 跳过短于此长度的词条
    min_length: int = 1


class ReviewCfg(BaseModel):
    # 确定性 QA 严重度达到该级别即打回（error / warn / info）
    block_on: str = "error"
    # 是否用 LLM 做二审
    llm_second_pass: bool = True
    # 二审通过后写入的 stage：3=已检查，5=已审核
    pass_stage: int = 3
    # 打回时写入的 stage：2=有疑问
    fail_stage: int = 2
    # 长度膨胀阈值
    length_ratio_limit: float = 3.5
    # 打回时是否在平台词条下发一条讨论说明原因。
    # 默认关：POST /issues 的 body 结构尚未经真实平台验证。
    comment_on_fail: bool = False
    # stage=1 但译文为空的脏数据：False=打回 stage=2（默认，人能看到），
    # True=重置 stage=0 让翻译流水线重做
    reset_empty: bool = False
    # 允许的残留 ASCII 字母比例
    ascii_ratio_limit: float = 0.55
    batch_size: int = 20


class TaskCfg(BaseModel):
    # 每个任务覆盖的词条上限，超出则拆成多个任务
    max_strings_per_task: int = 800
    # 每个任务覆盖的文件数上限
    max_files_per_task: int = 6
    # 生成任务时的 goal
    goal: dict[str, float] = Field(default_factory=lambda: {"translate": 1.0, "check": 0.0, "review": 0.0})
    # 是否把任务置顶
    pin: bool = True
    # 截止天数（None = 长期有效）
    deadline_days: int | None = None
    # 标题前缀
    title_prefix: str = "[自动分派]"


class GlossaryCfg(BaseModel):
    # 候选术语的最低出现次数（真实出现次数，不是加权分）
    min_occurrences: int = 3
    # 已审核/已检查的定稿译文不足时，是否补 stage=1（未校对机翻）作为取材。
    # 默认关：机翻错译一旦被固化成术语，会经 term_violation 变成全项目强制译法。
    include_mt: bool = False
    # 单次最多提交多少条
    max_terms: int = 200
    # 是否允许 LLM 提议新译法
    llm_propose: bool = True


class StyleGuideCfg(BaseModel):
    # 发布为公告时是否仅成员可见
    internal: bool = True
    # 公告分类
    category: str = "announcement"
    # 是否置顶
    bumped: bool = True


class AgentConfig(BaseModel):
    """流水线看到的配置形态（领域形状）：按环节分节，只放该环节用得到的项。

    两个来源都收敛到它：CLI 从 `config/agent.yaml` + 环境变量读，
    WebUI 从 `RuntimeSettings.to_agent_config()` 换算。因此流水线
    永远只认这一种类型，不需要知道自己是被 CLI 还是被界面驱动的。
    """
    # 配置文件所在目录：相对 state_dir 按它解析（而非 cwd），
    # 这样从任何目录调用 ptz 状态都不会落到意外位置。
    _base_dir: Path | None = PrivateAttr(default=None)

    project: ProjectCfg = Field(default_factory=ProjectCfg)
    api: ApiCfg = Field(default_factory=ApiCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    translate: TranslateCfg = Field(default_factory=TranslateCfg)
    review: ReviewCfg = Field(default_factory=ReviewCfg)
    tasks: TaskCfg = Field(default_factory=TaskCfg)
    glossary: GlossaryCfg = Field(default_factory=GlossaryCfg)
    styleguide: StyleGuideCfg = Field(default_factory=StyleGuideCfg)
    # 本地状态目录
    state_dir: str = ".ptz-state"

    def resolve(self) -> "AgentConfig":
        """环境变量覆盖 + 密钥解析。"""
        if not self.api.token:
            self.api.token = os.getenv("PTZ_API_TOKEN") or os.getenv("PARATRANZ_TOKEN", "")
        if not self.llm.api_key:
            self.llm.api_key = os.getenv(self.llm.api_key_env) or os.getenv("PTZ_LLM_API_KEY", "")
        env_map = {
            "PTZ_PROJECT_ID": ("project", "id", int),
            "PTZ_API_BASE": ("api", "base_url", str),
            "PTZ_API_RATE": ("api", "rate_limit_per_min", int),
            "PTZ_LLM_BASE": ("llm", "base_url", str),
            "PTZ_LLM_MODEL": ("llm", "model", str),
            "PTZ_STATE_DIR": ("state_dir", None, str),
        }
        for env, (sec, key, cast) in env_map.items():
            val = os.getenv(env)
            if val is None or val == "":
                continue
            try:
                cast_val = cast(val)
            except (TypeError, ValueError):
                continue
            if key is None:
                setattr(self, sec, cast_val)
            else:
                setattr(getattr(self, sec), key, cast_val)
        return self

    @property
    def state_path(self) -> Path:
        p = Path(self.state_dir)
        if not p.is_absolute():
            p = (self._base_dir or Path.cwd()) / p
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> AgentConfig:
    data: dict[str, Any] = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if overrides:
        _deep_merge(data, overrides)
    cfg = AgentConfig.model_validate(data)
    if path:
        cfg._base_dir = Path(path).resolve().parent
    return cfg.resolve()


def _deep_merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
