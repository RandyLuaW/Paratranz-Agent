"""运行期配置：WebUI 里填的东西，存 JSON，热生效。

模型是**三层结构**（提供商源 → 模型 → 任务路由）：

    Provider（源：base_url + key + 超时/代理/请求头）
      └── Model（模型：id + 能力 + 参数覆盖 + 上下文窗口 + 启用开关）

一个源可以聚合多个模型（中转站、聚合网关都这样），
任务路由指向 `provider_id::model_id`，而不是只指到源。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import AgentConfig

log = logging.getLogger("ptz.settings")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

MODEL_TASKS: tuple[str, ...] = ("agent", "translate", "review", "glossary", "styleguide", "extractor")
TASK_LABELS = {
    "agent": "Agent 意图解析",
    "translate": "翻译",
    "review": "校对",
    "glossary": "术语抽取",
    "styleguide": "规范生成",
    "extractor": "提取脚本生成",
}

CAPABILITIES = ("text", "image", "audio", "tool")
CAP_LABELS = {"text": "文本", "image": "图像", "audio": "音频", "tool": "工具使用"}

SEP = "::"  # provider::model 的分隔符


def join_ref(pid: str, mid: str) -> str:
    return f"{pid}{SEP}{mid}" if pid and mid else ""


def split_ref(ref: str) -> tuple[str, str]:
    if not ref:
        return "", ""
    if SEP in ref:
        pid, mid = ref.split(SEP, 1)
        return pid, mid
    return ref, ""


class ModelCfg(BaseModel):
    """一个具体模型。参数留空表示继承 provider 的默认值。"""

    id: str
    name: str = ""
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=lambda: ["text"])
    context_window: int = 0          # 0 = 未知
    temperature: float | None = None
    max_tokens: int | None = None
    extra_params: dict[str, Any] = Field(default_factory=dict)  # 附加请求体参数
    note: str = ""

    @property
    def label(self) -> str:
        return self.name or self.id

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ProviderCfg(BaseModel):
    """一个模型源。下面挂 models 列表。"""

    id: str
    name: str = ""
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    timeout: float = 120.0
    max_retries: int = 3
    concurrency: int = 4
    proxy: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    note: str = ""
    models: list[ModelCfg] = Field(default_factory=list)
    # 兼容旧配置：单模型字段。迁移后置空，不再由界面写入。
    model: str = ""

    def model_by_id(self, mid: str) -> ModelCfg | None:
        for m in self.models:
            if m.id == mid:
                return m
        return None

    @property
    def usable_models(self) -> list[ModelCfg]:
        return [m for m in self.models if m.enabled]

    def masked(self) -> dict[str, Any]:
        from .redact import mask_tail

        d = self.model_dump(exclude={"api_key", "models"})
        d["masked_key"] = mask_tail(self.api_key)
        d["has_key"] = bool(self.api_key)
        # 自定义请求头里经常直接放鉴权（`Authorization: Bearer xxx`），
        # 原样回显等于把凭据吐给前端与日志；只回键名 + 掩码值。
        d["headers"] = {k: mask_tail(str(v)) if v else "" for k, v in (self.headers or {}).items()}
        d["models"] = [m.as_dict() for m in self.models]
        return d


class RoutingCfg(BaseModel):
    """任务 → `provider::model` 的映射。"""

    default: str = ""
    fallback: list[str] = Field(default_factory=list)
    by_tool: dict[str, str] = Field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "default": self.default,
            "fallback": list(self.fallback),
            "by_tool": dict(self.by_tool),
        }


class PromptCfg(BaseModel):
    """提示词配置。

    - `prefix` 是**总前置提示词**，注入所有环节的 system prompt 最前面；
    - 各环节留空表示使用 pipeline 里的内置默认值（可随时恢复）。
    """

    prefix: str = ""
    translate: str = ""
    review: str = ""
    glossary: str = ""
    styleguide: str = ""
    agent: str = ""
    extractor: str = ""


PROMPT_TASKS: tuple[str, ...] = (
    "translate", "review", "glossary", "styleguide", "agent", "extractor"
)
PROMPT_LABELS = {
    "translate": "翻译",
    "review": "校对",
    "glossary": "术语抽取",
    "styleguide": "规范生成",
    "agent": "Agent 意图解析",
    "extractor": "提取脚本生成",
}


class RuntimeSettings(BaseModel):
    # ---- 平台 ----
    project_id: int = 0
    token: str = ""
    base_url: str = "https://paratranz.cn/api"
    rate_limit_per_min: int = 420

    # ---- 模型：源 + 模型 + 路由 ----
    providers: list[ProviderCfg] = Field(default_factory=list)
    routing: RoutingCfg = Field(default_factory=RoutingCfg)

    # ---- 提示词 ----
    prompts: PromptCfg = Field(default_factory=PromptCfg)

    # ---- 旧字段（仅用于迁移，界面不再写） ----
    llm_base_url: str = DEFAULT_BASE_URL
    llm_model: str = DEFAULT_MODEL
    llm_key: str = ""
    llm_concurrency: int = 4
    llm_temperature: float = 0.2
    llm_max_tokens: int = 0
    llm_timeout: float = 120.0
    llm_max_retries: int = 3
    llm_fallback_models: str = ""
    llm_context_max_turns: int = -1
    llm_keep_recent_ratio: float = 0.15

    # ---- Agent 行为 ----
    agent_max_steps: int = 8
    agent_tool_timeout: int = 120
    system_prompt_prefix: str = ""

    # ---- 成本 ----
    budget: dict[str, Any] = Field(default_factory=dict)
    pricing_overrides: dict[str, Any] = Field(default_factory=dict)

    # ---- 翻译 / 校对 ----
    style: str = "简体中文，口语自然，保留角色语气与口癖；专有名词严格遵循术语表"
    batch_size: int = 40
    review_pass_stage: int = 3
    review_block_on: str = "error"

    # ---- 任务 ----
    max_strings_per_task: int = 800
    task_prefix: str = "[自动分派]"

    # ---- 回写（提取的反向链路）----
    # 输出目录留空 = 用 `<游戏目录>_cn`；填了就固定往这里产出汉化版。
    writeback_out_dir: str = ""
    # mirror = 目标目录是一份完整可跑的游戏（不影响的文件直接复制）
    # patch  = 只放被改动的文件（体积最小，适合先比对）
    writeback_mode: str = "mirror"
    # 只回写达到该 stage 的译文：1 已翻译 / 3 已检查 / 5 已审核
    writeback_min_stage: int = 3

    # ---- 行为 ----
    dry_run: bool = True
    per_run_limit: int = 200

    # ------------------------------------------------------------------ 查询
    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    def prompt_for(self, task: str) -> str:
        """取某环节的 system prompt（含总前置）。返回空串表示用 pipeline 内置默认。"""
        base = (getattr(self.prompts, task, "") or "").strip()
        prefix = (self.prompts.prefix or "").strip()
        if prefix and base:
            return prefix + '\n\n' + base
        return prefix or base

    def provider(self, pid: str) -> ProviderCfg | None:
        for p in self.providers:
            if p.id == pid:
                return p
        return None

    def resolve(self, ref: str) -> tuple[ProviderCfg, ModelCfg] | None:
        """`pid::mid` → (provider, model)。model 可省略（取该源第一个启用的）。"""
        pid, mid = split_ref(ref)
        p = self.provider(pid)
        if p is None:
            return None
        if mid:
            m = p.model_by_id(mid)
            return (p, m) if m is not None else None
        ms = p.usable_models
        return (p, ms[0]) if ms else None

    def _first_usable(self) -> tuple[ProviderCfg, ModelCfg] | None:
        for p in self.providers:
            if p.enabled and p.api_key:
                ms = p.usable_models
                if ms:
                    return p, ms[0]
        return None

    def resolve_for(self, task: str = "") -> tuple[ProviderCfg, ModelCfg] | None:
        """按任务取 (provider, model)。三级回退：任务映射 → 默认 → 第一个可用的。"""
        ref = self.routing.by_tool.get(task) or self.routing.default
        got = self.resolve(ref) if ref else None
        if got and got[0].enabled and got[0].api_key and got[1].enabled:
            return got
        return self._first_usable()

    def chain_for(self, task: str = "") -> list[tuple[ProviderCfg, ModelCfg]]:
        """主 + 回退链（只含启用、有 key、模型启用的）。"""
        out: list[tuple[ProviderCfg, ModelCfg]] = []
        seen: set[str] = set()
        primary = self.resolve_for(task)
        if primary:
            out.append(primary)
            seen.add(join_ref(primary[0].id, primary[1].id))
        for ref in self.routing.fallback:
            got = self.resolve(ref)
            if not got:
                continue
            p, m = got
            key = join_ref(p.id, m.id)
            if p.enabled and p.api_key and m.enabled and key not in seen:
                out.append(got)
                seen.add(key)
        return out

    def masked(self) -> dict[str, Any]:
        """给前端的版本：凭据只回显尾号。"""
        from .redact import mask_tail

        d = self.model_dump(exclude={"providers"})
        for k in ("token", "llm_key"):
            v = d.get(k) or ""
            d[k] = mask_tail(v) if v else ""
        d["has_token"] = bool(self.token)
        d["providers"] = [p.masked() for p in self.providers]
        d["routing"] = self.routing.as_dict()
        d["tasks"] = [{"id": t, "label": TASK_LABELS[t]} for t in MODEL_TASKS]
        d["prompts"] = self.prompts.model_dump()
        d["effective"] = {}
        for t in MODEL_TASKS:
            got = self.resolve_for(t)
            d["effective"][t] = join_ref(got[0].id, got[1].id) if got else ""
        return d

    def to_agent_config(
        self, base_dir: Path, resolved: tuple[ProviderCfg, ModelCfg] | None = None
    ) -> AgentConfig:
        got = resolved or self.resolve_for("")
        p, m = got if got else (None, None)
        cfg = AgentConfig.model_validate(
            {
                "project": {"id": self.project_id, "name": "", "style": self.style},
                "api": {
                    "token": self.token,
                    "base_url": self.base_url,
                    "rate_limit_per_min": self.rate_limit_per_min,
                },
                "llm": {
                    "base_url": p.base_url if p else self.llm_base_url,
                    "model": m.id if m else self.llm_model,
                    "api_key": p.api_key if p else self.llm_key,
                    "concurrency": p.concurrency if p else self.llm_concurrency,
                    "batch_size": self.batch_size,
                    "temperature": (
                        m.temperature if (m and m.temperature is not None)
                        else self.llm_temperature
                    ),
                    "max_tokens": (
                        m.max_tokens if (m and m.max_tokens is not None)
                        else self.llm_max_tokens
                    ),
                    "timeout": p.timeout if p else self.llm_timeout,
                    "max_retries": p.max_retries if p else self.llm_max_retries,
                },
                "translate": {"batch_size": self.batch_size},
                "review": {
                    "pass_stage": self.review_pass_stage,
                    "block_on": self.review_block_on,
                },
                "tasks": {
                    "max_strings_per_task": self.max_strings_per_task,
                    "title_prefix": self.task_prefix,
                },
                "state_dir": str(base_dir),
            }
        )
        return cfg.resolve()


class SettingsStore:
    """JSON 落盘 + 凭据文件权限收紧。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: RuntimeSettings | None = None
        # 写操作都是 read-modify-write，FastAPI 的 sync 端点跑在线程池，
        # 不加锁并发保存会互相覆盖（丢配置）
        self._lock = threading.RLock()

    def load(self) -> RuntimeSettings:
        with self._lock:
            if self._cache is not None:
                return self._cache
            data: dict[str, Any] = {}
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    # **绝不静默回默认值**：配置里是 Token 与 API Key，
                    # 以前这里 `data = {}` 会让「文件坏掉」表现为「凭据全没了」，
                    # 用户只会以为是自己填错。先试 .bak 回滚，再不行就明确报错。
                    data = self._recover(exc)
            if not data.get("token"):
                data["token"] = os.getenv("PTZ_API_TOKEN") or os.getenv("PARATRANZ_TOKEN", "")
            if not data.get("llm_key"):
                data["llm_key"] = os.getenv("PTZ_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
            if not data.get("project_id"):
                raw = os.getenv("PTZ_PROJECT_ID", "")
                if raw.isdigit():
                    data["project_id"] = int(raw)

            data["providers"] = self._migrate_providers(data)
            self._cache = RuntimeSettings.model_validate(data)
            return self._cache

    @staticmethod
    def _migrate_providers(data: dict[str, Any]) -> list[dict[str, Any]]:
        """两代迁移：单模型配置 → provider；provider.model → provider.models[]。"""
        provs = data.get("providers")
        if not provs:
            provs = [
                {
                    "id": "default",
                    "name": "默认",
                    "base_url": data.get("llm_base_url") or DEFAULT_BASE_URL,
                    "api_key": data.get("llm_key") or "",
                    "timeout": data.get("llm_timeout", 120.0),
                    "max_retries": data.get("llm_max_retries", 3),
                    "concurrency": data.get("llm_concurrency", 4),
                    "enabled": True,
                    "note": "由单模型配置自动迁移",
                    "models": [
                        {
                            "id": data.get("llm_model") or DEFAULT_MODEL,
                            "enabled": True,
                            "temperature": data.get("llm_temperature", 0.2),
                            "max_tokens": data.get("llm_max_tokens", 0),
                        }
                    ],
                }
            ]
            data["routing"] = {"default": "default", "fallback": [], "by_tool": {}}
            return provs

        # 第二代：provider 只有 model 字段，补成 models[]
        for p in provs:
            if isinstance(p, dict) and not p.get("models"):
                mid = p.get("model") or DEFAULT_MODEL
                p["models"] = [
                    {"id": mid, "enabled": True, "temperature": p.get("temperature", 0.2)}
                ]
            if isinstance(p, dict):
                p.pop("model", None)
                p.pop("temperature", None)
                p.pop("max_tokens", None)
        # 路由从裸 provider id 升级成 pid::mid
        routing = data.get("routing") or {}
        by_id = {p["id"]: p for p in provs if isinstance(p, dict)}

        def upgrade(ref: str) -> str:
            if not ref or SEP in ref:
                return ref
            p = by_id.get(ref)
            if not p:
                return ref
            ms = p.get("models") or []
            return join_ref(ref, ms[0]["id"]) if ms else ref

        data["routing"] = {
            "default": upgrade(routing.get("default", "")),
            "fallback": [upgrade(x) for x in (routing.get("fallback") or [])],
            "by_tool": {k: upgrade(v) for k, v in (routing.get("by_tool") or {}).items()},
        }
        return provs

    def save(self, patch: dict[str, Any]) -> RuntimeSettings:
        with self._lock:
            cur = self.load().model_dump()
            for k, v in patch.items():
                if k not in RuntimeSettings.model_fields:
                    continue
                if k in ("token", "llm_key") and isinstance(v, str) and "*" in v:
                    continue
                cur[k] = v
            settings = RuntimeSettings.model_validate(cur)
            return self._persist(settings)

        # ------------------------------------------------------------- providers
    def upsert_provider(self, payload: dict[str, Any]) -> RuntimeSettings:
        with self._lock:
            """新增或更新一个源。api_key 传空/脱敏值则保留原值。"""
            s = self.load()
            pid = str(payload.get("id") or "").strip()
            if not pid:
                raise ValueError("provider id 不能为空")
            existing = s.provider(pid)
            data = existing.model_dump() if existing else {"id": pid}
            for k, v in payload.items():
                if k in ("masked_key", "has_key"):
                    continue
                if k == "api_key" and isinstance(v, str) and (not v or "*" in v):
                    continue
                if k == "models":
                    continue  # 模型走独立接口
                if k in ProviderCfg.model_fields and v is not None:
                    data[k] = v
            ProviderCfg.model_validate(data)
            if existing:
                s.providers = [
                    ProviderCfg.model_validate(data) if p.id == pid else p for p in s.providers
                ]
            else:
                s.providers = [*s.providers, ProviderCfg.model_validate(data)]
            if not s.routing.default:
                p = s.provider(pid)
                if p and p.usable_models:
                    s.routing.default = join_ref(pid, p.usable_models[0].id)
            return self._persist(s)

    def delete_provider(self, pid: str) -> RuntimeSettings:
        with self._lock:
            s = self.load()
            s.providers = [p for p in s.providers if p.id != pid]
            s.routing.by_tool = {
                k: v for k, v in s.routing.by_tool.items() if split_ref(v)[0] != pid
            }
            s.routing.fallback = [x for x in s.routing.fallback if split_ref(x)[0] != pid]
            if split_ref(s.routing.default)[0] == pid:
                first = next(
                    (
                        join_ref(p.id, p.usable_models[0].id)
                        for p in s.providers
                        if p.enabled and p.usable_models
                    ),
                    "",
                )
                s.routing.default = first
            return self._persist(s)

        # ---------------------------------------------------------------- models
    def upsert_model(self, pid: str, payload: dict[str, Any]) -> RuntimeSettings:
        with self._lock:
            s = self.load()
            p = s.provider(pid)
            if p is None:
                raise ValueError("provider 不存在")
            mid = str(payload.get("id") or "").strip()
            if not mid:
                raise ValueError("model id 不能为空")
            existing = p.model_by_id(mid)
            data = existing.model_dump() if existing else {"id": mid}
            for k, v in payload.items():
                if k in ModelCfg.model_fields and v is not None:
                    data[k] = v
            ModelCfg.model_validate(data)
            new_models = [
                ModelCfg.model_validate(data) if m.id == mid else m for m in p.models
            ]
            if existing is None:
                new_models.append(ModelCfg.model_validate(data))
            p.models = new_models
            if not s.routing.default:
                s.routing.default = join_ref(pid, mid)
            return self._persist(s)

    def delete_model(self, pid: str, mid: str) -> RuntimeSettings:
        with self._lock:
            s = self.load()
            p = s.provider(pid)
            if p is None:
                raise ValueError("provider 不存在")
            p.models = [m for m in p.models if m.id != mid]
            ref = join_ref(pid, mid)
            s.routing.by_tool = {k: v for k, v in s.routing.by_tool.items() if v != ref}
            s.routing.fallback = [x for x in s.routing.fallback if x != ref]
            if s.routing.default == ref:
                s.routing.default = ""
            return self._persist(s)

    def save_routing(self, payload: dict[str, Any]) -> RuntimeSettings:
        with self._lock:
            s = self.load()
            s.routing = RoutingCfg.model_validate(
                {
                    "default": payload.get("default", s.routing.default),
                    "fallback": [x for x in (payload.get("fallback") or []) if isinstance(x, str)],
                    "by_tool": {
                        k: v
                        for k, v in (payload.get("by_tool") or {}).items()
                        if k in MODEL_TASKS and isinstance(v, str) and v
                    },
                }
            )
            return self._persist(s)

    def _recover(self, exc: Exception) -> dict[str, Any]:
        """配置解析失败时的补救：优先从 `.bak` 回滚，否则明确报错。

        返回 dict 表示已成功回滚；抛 `RuntimeError` 表示无法补救——
        宁可让界面弹一条「配置文件损坏，请从 xxx.bak 恢复」，也不能装作没事
        然后用空配置继续跑（那会让用户以为凭据丢了）。
        """
        bak = self.path.with_suffix(self.path.suffix + ".bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                shutil.copy2(bak, self.path)
                log.warning("settings.json 解析失败（%s），已从 %s 回滚", exc, bak.name)
                return data
            except (json.JSONDecodeError, OSError) as exc2:
                raise RuntimeError(
                    f"配置文件损坏且备份也无法解析：{self.path}（备份 {bak}）：{exc2}"
                ) from exc2
        raise RuntimeError(
            f"配置文件损坏且没有备份：{self.path}（{exc}）。"
            f"请删除该文件后重新填写凭据，或用你之前的备份覆盖它。"
        ) from exc

    def _persist(self, s: RuntimeSettings) -> RuntimeSettings:
        with self._lock:
            data = json.dumps(s.model_dump(), ensure_ascii=False, indent=2)
            # **原子写**：先写临时文件再 os.replace。直接 write_text 时若进程在
            # 写窗口内被杀（web.bat/loop.bat 那种崩了自动重启的用法很常见），
            # 文件会只剩半截 JSON —— 而配置里放的是 Token 与 API Key，
            # 半截文件等于「所有凭据静默消失」。
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, encoding="utf-8")
            if self.path.exists():
                try:  # 留一份上一版，供 load() 在解析失败时回滚
                    shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
                except OSError:
                    pass
            os.replace(tmp, self.path)
            self._tighten()
            self._cache = s
            return s

    def _tighten(self) -> None:
        """把凭据文件权限收到只有当前用户可读写。"""
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        if os.name == "nt":
            try:
                import subprocess

                user = os.getenv("USERNAME") or os.getenv("USER") or ""
                if user:
                    subprocess.run(
                        ["icacls", str(self.path), "/inheritance:r", "/grant:r", f"{user}:F"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
            except (OSError, subprocess.SubprocessError):
                pass

    def invalidate(self) -> None:
        self._cache = None
