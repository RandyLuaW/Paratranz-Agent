"""纯函数层：归并去重 → 校验 → 导出成平台 items。

**顺序不能反**：`normalize` 的职责就是去重，先归并再校验，`dup_key` 规则就永远
触发不了（这个 bug 历史上真出过）。所以对外暴露的是三个独立函数，调用方自己
决定顺序：`validate(原始) → normalize → export`。
"""
import re
from typing import Iterable, Sequence

# 校验阈值
MAX_ORIGINAL_LEN = 5000
MAX_ENTRIES_WARN = 200_000


PLACEHOLDER_RE = re.compile(r"\\[A-Za-z]+\[[^\]]*\]|\{[^}]*\}|%[sd]|<\w+>|\$\[[^\]]*\]")


def normalize(entries: Iterable[dict]) -> tuple[list[dict], dict]:
    """归并去重 + 规范化。返回 (结果, 统计)。"""
    entries = list(entries)  # 迭代器只能遍历一次，先固化
    seen: dict[str, dict] = {}
    dup_keys = 0
    dup_text = 0
    trimmed = 0
    by_original: dict[str, str] = {}

    for e in entries:
        key = str(e.get("key") or "").strip()
        original = e.get("original")
        if not isinstance(original, str):
            continue
        o2 = original.replace("\r\n", "\n").strip()
        if o2 != original:
            trimmed += 1
        if not o2:
            continue
        if key in seen:
            dup_keys += 1
            # 同 key 后到的覆盖（保留 context 更长的）
            if len(str(e.get("context") or "")) <= len(seen[key].get("context") or ""):
                continue
        # 同原文不同 key：保留第一个，其余记录（平台侧仍可各自存在）
        if o2 in by_original and by_original[o2] != key:
            dup_text += 1
        else:
            by_original[o2] = key
        seen[key] = {
            "key": key,
            "original": o2,
            "context": str(e.get("context") or "").strip(),
        }

    out = list(seen.values())
    stats = {
        "input": len(entries),
        "output": len(out),
        "dup_keys": dup_keys,
        "dup_text": dup_text,
        "trimmed": trimmed,
    }
    return out, stats


def validate(entries: Sequence[dict]) -> dict:
    """校验提取结果。返回 {ok, errors[], warnings[], stats}。"""
    errors: list[dict] = []
    warnings: list[dict] = []
    seen: dict[str, int] = {}
    empty = 0
    too_long = 0
    no_context = 0
    placeholder_only = 0
    key_bad = 0

    for i, e in enumerate(entries):
        key = str(e.get("key") or "")
        original = e.get("original")
        if not key:
            errors.append({"row": i, "rule": "empty_key", "msg": "key 为空"})
            continue
        if not isinstance(original, str):
            errors.append({"row": i, "rule": "not_string", "msg": "original 不是字符串", "key": key})
            continue
        if not original.strip():
            empty += 1
            errors.append({"row": i, "rule": "empty_original", "msg": "原文为空", "key": key})
            continue
        if len(original) > MAX_ORIGINAL_LEN:
            too_long += 1
            warnings.append(
                {"row": i, "rule": "too_long", "msg": f"原文超过 {MAX_ORIGINAL_LEN} 字符", "key": key}
            )
        if key in seen:
            errors.append(
                {"row": i, "rule": "dup_key", "msg": f"key 重复（首见第 {seen[key]} 行）", "key": key}
            )
        else:
            seen[key] = i
        if not e.get("context"):
            no_context += 1
        # 纯占位符/纯符号：没有可翻译内容
        stripped = PLACEHOLDER_RE.sub("", original).strip()
        if not stripped:
            placeholder_only += 1
            warnings.append(
                {"row": i, "rule": "placeholder_only", "msg": "去掉占位符后没有可译内容", "key": key}
            )
        if "::" not in key and "/" not in key:
            key_bad += 1

    if key_bad:
        warnings.append(
            {
                "row": -1,
                "rule": "key_format",
                "msg": f"{key_bad} 个 key 既不含 '::' 也不含 '/'，建议用 `文件::路径` 形式便于回溯",
            }
        )
    if no_context:
        warnings.append(
            {"row": -1, "rule": "no_context", "msg": f"{no_context} 条没有 context，平台里不好定位"}
        )
    if len(entries) > MAX_ENTRIES_WARN:
        warnings.append(
            {"row": -1, "rule": "too_many", "msg": f"词条数 {len(entries)} 超过 {MAX_ENTRIES_WARN}，建议分文件"}
        )

    return {
        "ok": not errors,
        "errors": errors[:200],
        "warnings": warnings[:200],
        "stats": {
            "total": len(entries),
            "unique_keys": len(seen),
            "empty_original": empty,
            "too_long": too_long,
            "no_context": no_context,
            "placeholder_only": placeholder_only,
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
    }


def export_paratranz(entries: Sequence[dict], *, translation: str = "") -> list[dict]:
    """转成 ParaTranz 的 items 格式（update_file_translation 直接吃）。"""
    return [
        {
            "key": e["key"],
            "original": e["original"],
            "translation": translation,
            "context": e.get("context", ""),
        }
        for e in entries
    ]


def export_upload_json(entries: Sequence[dict]) -> bytes:
    """把词条打成一个 JSON 对象文件，供 `POST /files` 上传建**新文件**。

    为什么是「key → 原文」的扁平对象：平台按**项目格式**解析上传内容，
    `json` 格式项目的映射就是 key→value；而我们的 key 本身就是定位符
    （`data/Actors.json::[12].name`），原样做键，回写时能直接对上。
    字段顺序按 key 排序，同样的输入每次产出同样的字节（便于比对与去重）。

    局限要讲清楚：项目格式不是 `json`（例如 twee、gettext）时，平台拿这份
    JSON 解析不出词条。所以上传前一定要先看预览，别盲传。
    """
    import json

    payload = {e["key"]: e["original"] for e in entries if e.get("key")}
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    return text.encode("utf-8")
