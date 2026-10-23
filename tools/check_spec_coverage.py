"""把 client.py 的每个调用与官方 OpenAPI spec 对照。

三件事：
1. **结构对照**：每个 (方法, 路径骨架) 是否在 spec 里存在 → 抓「打错端点」。
2. **参数名对照**：query/path 参数名是否与 spec 声明的一致 → 抓「参数被平台静默忽略」。
3. **覆盖缺口**：spec 里有、我们没实现的端点（信息性，不是缺陷）。

参数名这一类最容易出事：写成 `page_size` 而平台只认 `pageSize`，请求照发、过滤无效、
结果全量返回，而且**不报错**。

跑法：python tools/check_spec_coverage.py
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / ".audit" / "openapi.json"
CLIENT = ROOT / "src" / "paratranz_agent" / "client.py"
HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


def shape(path: str) -> str:
    """路径骨架：数字 id 与前缀还原成模板，再抹平所有 {…} 占位符名。"""
    p = path.split("?")[0].strip()
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"/projects/\d+", "/projects/{_}", p)
    p = re.sub(r"/users/\d+", "/users/{_}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    p = re.sub(r"/\d+(?=/|$)", "/{}", p)
    return p.rstrip("/") or "/"


def _lit(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _dict_keys(node: ast.AST | None) -> list[str]:
    if not isinstance(node, ast.Dict):
        return []
    out = []
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append(k.value)
    return out


def client_calls() -> list[dict]:
    src = CLIENT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[dict] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == "request"):
                continue
            if len(node.args) < 2:
                continue
            method, path = _lit(node.args[0]), _lit(node.args[1])
            if not method or not path:
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            out.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "line": node.lineno,
                    "fn": fn.name,
                    "query": _dict_keys(kw.get("params")),
                    "body": _dict_keys(kw.get("json_body")),
                    "files": "files" in kw,
                }
            )
    return out


def spec_index(spec: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for path, node in (spec.get("paths") or {}).items():
        for method, op in node.items():
            if method.upper() in HTTP_METHODS and isinstance(op, dict):
                out[(method.upper(), shape(path))] = {"path": path, "op": op, "spec": spec}
    return out


def spec_params(entry: dict) -> set[str]:
    names: set[str] = set()
    op, spec = entry["op"], entry["spec"]
    for p in (op.get("parameters") or []) + (spec["paths"][entry["path"]].get("parameters") or []):
        if not isinstance(p, dict):
            continue
        if "$ref" in p:
            nm = p["$ref"].rsplit("/", 1)[-1]
            p = spec.get("components", {}).get("parameters", {}).get(nm, {})
        if p.get("name"):
            names.add(p["name"])
    return names


def spec_body_keys(entry: dict) -> set[str]:
    """请求体顶层字段名（含 allOf 引用展开）。"""
    spec, op = entry["spec"], entry["op"]
    keys: set[str] = set()
    body = op.get("requestBody") or {}
    for _ctype, content in (body.get("content") or {}).items():
        sch = (content or {}).get("schema") or {}
        targets = [sch] + (sch.get("allOf") or [])
        for t in targets:
            if "$ref" in t:
                nm = t["$ref"].rsplit("/", 1)[-1]
                t = spec.get("components", {}).get("schemas", {}).get(nm, {})
            props = t.get("properties") or {}
            keys |= set(props)
            # 额外声明的 properties（allOf 外层）
        keys |= set(sch.get("properties") or {})
    return keys


def main() -> int:
    if not SPEC.exists():
        print(f"缺少 {SPEC}，先跑 tools/dump_spec.py")
        return 2
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    index = spec_index(spec)
    calls = client_calls()

    unmatched: list[dict] = []
    param_issues: list[tuple[dict, list[str]]] = []
    body_issues: list[tuple[dict, list[str]]] = []
    used: set[tuple[str, str]] = set()

    for c in calls:
        key = (c["method"], shape(c["path"]))
        entry = index.get(key)
        if entry is None:
            unmatched.append(c)
            continue
        used.add(key)
        declared = spec_params(entry)
        unknown = [k for k in c["query"] if declared and k not in declared]
        if unknown:
            param_issues.append((c, unknown))
        if c["body"] and not c["files"]:
            ok = spec_body_keys(entry)
            extra = [k for k in c["body"] if ok and k not in ok]
            if extra:
                body_issues.append((c, extra))

    print(f"spec 端点 {len(index)} 个 / client 调用 {len(calls)} 处\n")

    def dump(title: str, rows, fmt) -> None:
        print(f"=== {title}：{len(rows)} ===")
        for r in rows:
            print("  " + fmt(r))
        if rows:
            print()

    dump(
        "❌ 结构对不上（方法/路径在 spec 里不存在）",
        unmatched,
        lambda c: f"client.py:{c['line']} {c['fn']}(): {c['method']} {c['path']}",
    )
    dump(
        "⚠ 查询参数名 spec 未声明（可能被平台静默忽略）",
        param_issues,
        lambda t: f"client.py:{t[0]['line']} {t[0]['fn']}(): {t[0]['method']} {t[0]['path']} → {t[1]}",
    )
    dump(
        "⚠ 请求体字段 spec 未声明（可能是平台未文档化字段）",
        body_issues,
        lambda t: f"client.py:{t[0]['line']} {t[0]['fn']}(): {t[0]['method']} {t[0]['path']} → {t[1]}",
    )

    missing = sorted(set(index) - used)
    print(f"=== spec 有、client 未覆盖：{len(missing)} 个（信息性）===")
    for m in missing:
        print(f"  {m[0]:6s} {index[m]['path']}  {index[m]['op'].get('summary') or ''}")

    hard = len(unmatched)
    print(f"\n结论：硬错误 {hard}，参数名待确认 {len(param_issues)}，请求体待确认 {len(body_issues)}")
    return 0 if hard == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
