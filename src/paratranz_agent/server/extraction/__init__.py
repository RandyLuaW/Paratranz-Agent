"""提取类内容管理：导入提取脚本 → 跑游戏目录 → 归并校验 → 导入平台。

## 为什么单独一层

ParaTranz 只能管「已经进平台的词条」。而词条怎么从游戏文件里出来，
是每个引擎都不一样的手工活：RPG Maker 在 data/*.json，Ren'Py 在 .rpy 对话，
Unity 要拆 AssetBundle。这一层把这些差异收进「提取器」这个可插拔单元。

## 提取器协议

一个提取器 = 一个目录，含 `extractor.yaml` + 入口脚本：

    extractor.yaml
      id: rpgmaker-mv
      name: RPG Maker MV/MZ
      entry: run.py
      args: [...]

    运行时：python run.py --input <游戏目录> --output <输出.jsonl> [--opt k=v ...]
    输出协议（JSONL）：{"key": "...", "original": "...", "context": "..."}

## 模块划分

    errors    错误类型
    paths     状态目录布局 + id 守卫
    jsonl     JSONL 读取（支持分页切片）
    guard     游戏目录安全检查
    registry  提取器注册表（内置 / 导入 / Agent 生成）
    runner    起子进程跑提取器
    records   提取记录读取（列表 / 详情 / 词条 / 日志）
    transform 纯函数：归并 → 校验 → 导出
    analyze   目录画像与采样

**本文件只是门面**：所有公开名字照旧从 `paratranz_agent.server.extraction` 导入，
调用方不需要知道内部怎么分的。
"""

from .analyze import analyze_game_dir, sample_files
from .errors import ExtractionError
from .guard import check_game_dir
from .jsonl import MAX_LINES, read_jsonl
from .paths import (
    ENTRY_FILE,
    EXTRACTOR_DIR,
    JOB_DIR,
    _extractor_root,
    _job_root,
    check_id,
)
from .records import get_job, job_log, list_jobs, load_entries
from .registry import (
    ExtractorMeta,
    _load_meta,
    get_extractor,
    install_extractor,
    list_extractors,
    remove_extractor,
)
from .runner import ExtractionResult, run_extractor
from .transform import (
    MAX_ENTRIES_WARN,
    MAX_ORIGINAL_LEN,
    PLACEHOLDER_RE,
    export_paratranz,
    export_upload_json,
    normalize,
    validate,
)

# 历史名字：内部调用已改叫 read_jsonl，但测试与外部脚本可能还在用旧名
_read_jsonl = read_jsonl

__all__ = [
    "ENTRY_FILE",
    "EXTRACTOR_DIR",
    "JOB_DIR",
    "MAX_ENTRIES_WARN",
    "MAX_LINES",
    "MAX_ORIGINAL_LEN",
    "PLACEHOLDER_RE",
    "ExtractionError",
    "ExtractionResult",
    "ExtractorMeta",
    # 私有路径/加载助手：extractor_agent 与测试直接用它们定位状态目录与读 meta
    "_extractor_root",
    "_job_root",
    "_load_meta",
    "analyze_game_dir",
    "check_game_dir",
    "check_id",
    "export_paratranz",
    "export_upload_json",
    "get_extractor",
    "get_job",
    "install_extractor",
    "job_log",
    "list_extractors",
    "list_jobs",
    "load_entries",
    "normalize",
    "read_jsonl",
    "remove_extractor",
    "run_extractor",
    "sample_files",
    "validate",
]
