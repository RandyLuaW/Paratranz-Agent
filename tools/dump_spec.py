"""把官方 OpenAPI spec 导出成可读摘要，作为全盘审计的基准文档。

`https://paratranz.cn/docs` 是个 SPA 空壳（渲染后正文为空），
真正的官方说明都在 swagger spec 的 description 字段里，所以这里把
路径 / 方法 / 参数 / 请求体 / 响应 / 说明全部摊平成 markdown。
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / ".audit"
SPEC_URL = "https://paratranz.cn/api-docs/?format=json"


def fetch() -> dict:
    cache = OUT / "openapi.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    with urllib.request.urlopen(SPEC_URL, timeout=60) as r:  # noqa: S310 - 固定官方 URL
        data = json.loads(r.read().decode("utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def resolve(spec: dict, node: dict, depth: int = 0) -> str:
    """把 schema 展开成人类可读的类型描述（最多 3 层，避免递归爆）。"""
    if not isinstance(node, dict) or depth > 3:
        return "…"
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        target = spec.get("components", {}).get("schemas", {}).get(name, {})
        if target:
            return f"{name}" + resolve(spec, target, depth + 1)
        return name
    if "allOf" in node:
        return " & ".join(resolve(spec, x, depth + 1) for x in node["allOf"])
    t = node.get("type")
    if t == "array":
        return f"array<{resolve(spec, node.get('items') or {}, depth + 1)}>"
    if node.get("properties"):
        props = ", ".join(
            f"{k}: {resolve(spec, v, depth + 1)}" for k, v in list(node["properties"].items())[:20]
        )
        return "{" + props + "}"
    if node.get("enum"):
        return f"{t or 'any'}[{'|'.join(map(str, node['enum']))}]"
    return str(t or "any")


def schema_fields(spec: dict, schema_name: str) -> str:
    sch = spec.get("components", {}).get("schemas", {}).get(schema_name)
    if not sch:
        return f"（spec 里没有 {schema_name}）"
    lines = []
    req = set(sch.get("required") or [])
    for k, v in (sch.get("properties") or {}).items():
        mark = "**必填**" if k in req else ""
        desc = (v.get("description") or "").replace("\n", " ")
        readonly = "只读" if v.get("readOnly") else ""
        lines.append(
            f"  - `{k}` {resolve(spec, v)} {mark} {readonly} {desc[:120]}".rstrip()
        )
    return "\n".join(lines)


def main() -> int:
    spec = fetch()
    OUT.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    info = spec.get("info", {})
    out.append(f"# ParaTranz OpenAPI {info.get('version', '?')} — 审计基准摘要\n")
    out.append(f"> 来源：{SPEC_URL}\n")
    out.append(f"说明：{info.get('description', '')}\n")
    out.append(f"\n共 {len(spec.get('paths', {}))} 个路径。\n")

    for path in sorted(spec.get("paths", {})):
        node = spec["paths"][path]
        out.append(f"\n## `{path}`\n")
        # 路径级参数
        shared = node.get("parameters")
        if isinstance(shared, list):
            for p in shared:
                if "$ref" in p:
                    name = p["$ref"].rsplit("/", 1)[-1]
                    p = spec.get("components", {}).get("parameters", {}).get(name, {})
                out.append(
                    f"- 路径参数 `{p.get('name')}` ({p.get('in')}): "
                    f"{resolve(spec, p.get('schema') or {})} — {(p.get('description') or '')[:100]}"
                )
        for method, op in node.items():
            if not isinstance(op, dict):
                continue
            out.append(f"\n### {method.upper()} — {op.get('summary', '')}\n")
            desc = (op.get("description") or "").replace("\r", "")
            if desc:
                out.append(desc.strip() + "\n")
            for p in op.get("parameters") or []:
                if "$ref" in p:
                    name = p["$ref"].rsplit("/", 1)[-1]
                    p = spec.get("components", {}).get("parameters", {}).get(name, {})
                out.append(
                    f"- 参数 `{p.get('name')}` ({p.get('in')}) "
                    f"{resolve(spec, p.get('schema') or {})} — {(p.get('description') or '')[:100]}"
                )
            body = op.get("requestBody") or {}
            for ctype, content in (body.get("content") or {}).items():
                sch = content.get("schema") or {}
                out.append(f"- 请求体 ({ctype}): {resolve(spec, sch)}")
                ref = sch.get("$ref")
                if ref:
                    out.append(schema_fields(spec, ref.rsplit("/", 1)[-1]))
                elif sch.get("allOf"):
                    for x in sch["allOf"]:
                        if x.get("$ref"):
                            out.append(schema_fields(spec, x["$ref"].rsplit("/", 1)[-1]))
            for code, resp in (op.get("responses") or {}).items():
                if not isinstance(resp, dict):
                    continue
                note = resp.get("description") or ""
                sch = ((resp.get("content") or {}).get("application/json") or {}).get("schema") or {}
                ref = sch.get("$ref")
                extra = ""
                if ref:
                    extra = f" → {ref.rsplit('/', 1)[-1]}"
                elif sch.get("properties"):
                    extra = " → " + resolve(spec, sch)
                out.append(f"- 响应 {code}: {note}{extra}")

    # 关键 schema 全文
    out.append("\n\n# 附：关键 Schema 字段\n")
    for name in (
        "String", "File", "Project", "Task", "Term", "Announcement", "Issue", "Comment",
        "Member", "User", "Stage", "FileRevision",
    ):
        if name in spec.get("components", {}).get("schemas", {}):
            out.append(f"\n## {name}\n")
            out.append(schema_fields(spec, name))

    # 所有 schema 名一览
    out.append("\n\n# 附：全部 schema 名\n")
    out.append(", ".join(sorted(spec.get("components", {}).get("schemas", {}))))

    md = OUT / "spec-digest.md"
    md.write_text("\n".join(out), encoding="utf-8")
    print(f"已写出 {md}（{md.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
