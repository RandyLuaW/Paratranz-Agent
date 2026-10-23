"""内存版 ParaTranz 假服务，用于离线端到端测试。

用 `httpx.MockTransport` 挂在 `ParaTranzClient` 上，所以测的是**真实**的
client 代码路径（限流、分页、错误处理、multipart 解析），而不是绕过它。

覆盖的端点与真实平台同构：
    GET  /projects/{id}
    GET  /projects/{id}/files
    GET  /projects/{id}/files/{fid}/translation
    GET  /projects/{id}/strings
    PUT  /projects/{id}/strings            batchOperateStrings
    GET  /projects/{id}/terms
    PUT  /projects/{id}/terms              importTerms (multipart)
    GET  /projects/{id}/members
    GET  /projects/{id}/tasks
    POST /projects/{id}/tasks
    POST /projects/{id}/tasks/{tid}/files
    PUT  /projects/{id}/tasks/{tid}/strings
    POST /projects/{id}/tasks/{tid}
    GET  /projects/{id}/announcements
    POST /projects/{id}/announcements
    PUT  /projects/{id}/announcements/{aid}
    POST /projects/{id}/issues             create_issue（打回理由留评论）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx


def _seed() -> dict[str, Any]:
    files = [
        {"id": 101, "name": "www/data/Map001.json", "hash": "a" * 32},
        {"id": 102, "name": "www/data/Items.json", "hash": "b" * 32},
        {"id": 103, "name": "www/quest/quest1.txt", "hash": "c" * 32},
    ]
    strings: list[dict] = []

    def add(fid: int, key: str, original: str, translation: str = "", stage: int = 0, context: str = "") -> None:
        strings.append(
            {
                "id": 1000 + len(strings),
                "key": key,
                "original": original,
                "translation": translation,
                "stage": stage,
                "fileId": fid,
                "context": context or None,
            }
        )

    # Map001：3 条已翻译（其中 1 条占位符正确、1 条丢了控制符、1 条正常）
    add(101, "m1", "Hello, \\N[1]! Welcome to the farm.", "你好，\\N[1]！欢迎来到农场。", 1)
    add(101, "m2", "You got \\V[2] coins.", "你获得了金币。", 1)  # 丢了 \V[2] → 应被打回
    add(101, "m3", "The crops are ready to harvest.", "作物可以收获了。", 1)
    add(101, "m4", "Good morning, farmer.", "")
    add(101, "m5", "The river is deep here.", "")
    add(101, "m6", "Do you want to buy a seed?", "")
    add(101, "m7", "Your health is low.", "")
    add(101, "m8", "Open the door with the key.", "")
    add(101, "m9", "The monster attacked you for 12 damage.", "")
    add(101, "m10", "Thank you, friend.", "")

    # 已审核（stage=5）的定稿译文 —— 术语抽取只应从这里取材，
    # stage=1 是未校对机翻，不能当术语依据
    add(101, "r1", "Buy a Health Potion for 50 coins.", "花 50 金币买一瓶生命药水。", 5)
    add(101, "r2", "The Magic Shield blocks damage.", "魔法盾可以抵挡伤害。", 5)
    add(101, "r3", "Visit the farm at dawn.", "黎明时分去农场。", 5)

    # Items：全未翻译
    add(102, "i1", "Wooden Sword", "")
    add(102, "i2", "Health Potion", "")
    add(102, "i3", "Restores 50 health.", "")
    add(102, "i4", "Magic Shield", "")
    add(102, "i5", "Increases defense by 10.", "")
    add(102, "i6", "Farm Seed", "")
    add(102, "i7", "Grows in 3 days.", "")
    add(102, "i8", "Old Key", "")

    # quest：全未翻译
    add(103, "q1", "Quest: Harvest 5 crops.", "")
    add(103, "q2", "Reward: 100 coins.", "")
    add(103, "q3", "Talk to the village elder.", "")
    add(103, "q4", "The forest is dangerous at night.", "")
    add(103, "q5", "Find the lost key in the cave.", "")
    add(103, "q6", "Return to the farm before sunset.", "")

    return {
        "project": {
            "id": 6896,
            "name": "Sample Game",
            "source": "en",
            "dest": "zh-CN",
            "extra": {"task": True, "reviewMode": 1, "disableApiEdit": False, "termsMode": 1},
        },
        "files": files,
        "strings": strings,
        "terms": [
            {"id": 1, "term": "farm", "translation": "农场", "pos": "noun", "variants": ["farms"]},
        ],
        "members": [{"id": 1, "uid": 42, "user": {"username": "agent"}, "permission": 3}],
        "tasks": [],
        "announcements": [],
        "issues": [],
        "calls": [],
    }


class FakeParaTranz:
    """内存假平台。

    `persist_path` 给了就把状态存成 JSON，下次构造时读回 —— 这样
    `ptz plan --mock --yes` 建的任务，`ptz status --mock` 能看见，
    离线演示才有连贯性。
    """

    def __init__(self, state: dict[str, Any] | None = None, persist_path: str | None = None):
        self.persist_path = Path(persist_path) if persist_path else None
        if self.persist_path and self.persist_path.exists():
            try:
                self.state = json.loads(self.persist_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.state = state or _seed()
        else:
            self.state = state or _seed()
        self._next_id = self.state.get("_next_id") or {"task": 900, "term": 100, "ann": 500, "taskfile": 1, "string": 90000}

    def _save(self) -> None:
        if not self.persist_path:
            return
        self.state["_next_id"] = self._next_id
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ 工具
    def _file_stats(self, fid: int) -> dict:
        rows = [s for s in self.state["strings"] if s["fileId"] == fid]
        return {
            "total": len(rows),
            "translated": sum(1 for s in rows if s["stage"] >= 1),
            "checked": sum(1 for s in rows if s["stage"] >= 3),
            "reviewed": sum(1 for s in rows if s["stage"] >= 5),
            "disputed": sum(1 for s in rows if s["stage"] == 2),
            "hidden": 0,
            "locked": 0,
            "words": sum(len(s["original"].split()) for s in rows),
        }

    def _files_payload(self) -> list[dict]:
        out = []
        for f in self.state["files"]:
            out.append({**f, **self._file_stats(f["id"])})
        return out

    def _page(self, rows: list, page: int, size: int) -> httpx.Response:
        """真实平台的列表接口返回分页对象 {results,page,pageSize,rowCount,pageCount}。

        只有 `/files` 等少数接口直接返回裸数组。
        """
        import math

        start = (page - 1) * size
        part = rows[start : start + size]
        return self._json({
            "results": part,
            "page": page,
            "pageSize": size,
            "rowCount": len(rows),
            "pageCount": max(1, math.ceil(len(rows) / size)) if rows else 0,
        })

    def _json(self, payload: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=payload)

    # --------------------------------------------------------------- handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        resp = self._dispatch(request)
        self._save()
        return resp

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        method = request.method
        self.state["calls"].append(f"{method} {path}")

        if request.headers.get("Authorization") != "test-token":
            return self._json({"message": "unauthorized"}, 401)

        pid = re.escape("6896")

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}", path):
            return self._json(self.state["project"])

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}/files", path):
            return self._json(self._files_payload())

        # 上传建文件（multipart: file + path）。真实平台返回 `{"file": {...}}` 包一层，
        # 且会按项目格式解析上传内容、自动建出词条 —— 这里同样解析 JSON 内容建词条。
        if method == "POST" and re.fullmatch(rf"/api/projects/{pid}/files", path):
            raw = request.content.decode("utf-8", errors="replace")
            fname = ""
            mm = re.search(r'filename="([^"]+)"', raw)
            if mm:
                fname = mm.group(1)
            if not fname:
                return self._json({"message": "缺少 file"}, 400)
            if any(f["name"] == fname for f in self.state["files"]):
                return self._json({"message": "同名文件已存在"}, 400)
            body_start = raw.find("\r\n\r\n")
            payload = {}
            if body_start != -1:
                text = raw[body_start + 4 :]
                text = text.split("\r\n--")[0]
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = {}
            fid = max((f["id"] for f in self.state["files"]), default=100) + 1
            self._next_id.setdefault("string", 90000)
            for k, v in (payload or {}).items():
                self._next_id["string"] += 1
                self.state["strings"].append(
                    {
                        "id": self._next_id["string"],
                        "key": k,
                        "original": str(v),
                        "translation": "",
                        "stage": 0,
                        "fileId": fid,
                        "file": {"id": fid, "name": fname, "project": 6896},
                        "context": None,
                    }
                )
            self.state["files"].append({"id": fid, "name": fname, "project": 6896})
            return self._json({"file": {"id": fid, "name": fname, "project": 6896, "total": len(payload or {})}})

        m = re.fullmatch(rf"/api/projects/{pid}/files/(\d+)", path)
        if m and method == "DELETE":
            fid = int(m.group(1))
            self.state["files"] = [f for f in self.state["files"] if f["id"] != fid]
            self.state["strings"] = [s for s in self.state["strings"] if s["fileId"] != fid]
            return self._json("OK")

        m = re.fullmatch(rf"/api/projects/{pid}/files/(\d+)/translation", path)
        if m and method == "GET":
            fid = int(m.group(1))
            rows = [s for s in self.state["strings"] if s["fileId"] == fid]
            return self._json(rows)

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}/strings", path):
            page = int(url.params.get("page", 1))
            size = int(url.params.get("pageSize", 100))
            stage = url.params.get("stage")
            # httpx 新版是 get_list，老版是 getlist
            _getlist = getattr(url.params, "get_list", None) or url.params.getlist  # noqa: B009
            files = [int(v) for v in _getlist("file")] if "file" in url.params else []
            rows = self.state["strings"]
            if stage is not None and stage != "":
                rows = [s for s in rows if s["stage"] == int(stage)]
            if files:
                rows = [s for s in rows if s["fileId"] in files]
            return self._page(rows, page, size)

        if method == "PUT" and re.fullmatch(rf"/api/projects/{pid}/strings", path):
            body = json.loads(request.content or b"{}")
            items = body.get("items", [])
            updated = []
            by_id = {s["id"]: s for s in self.state["strings"]}
            for it in items:
                sid = int(it["id"])
                s = by_id.get(sid)
                if not s:
                    continue
                if "translation" in it:
                    s["translation"] = it["translation"]
                    if "stage" not in it:
                        s["stage"] = 1 if it["translation"] else 0
                if "stage" in it:
                    s["stage"] = int(it["stage"])
                updated.append(sid)
            return self._json(updated)

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}/terms", path):
            return self._page(
                self.state["terms"],
                int(url.params.get("page", 1)),
                int(url.params.get("pageSize", 500)),
            )

        if method == "PUT" and re.fullmatch(rf"/api/projects/{pid}/terms", path):
            raw = request.content.decode("utf-8", errors="replace")
            start, end = raw.find("["), raw.rfind("]")
            incoming = json.loads(raw[start : end + 1]) if start != -1 else []
            existing = {t["term"]: t for t in self.state["terms"]}
            for t in incoming:
                if t["term"] in existing:
                    existing[t["term"]].update(
                        {k: v for k, v in t.items() if k in ("translation", "pos", "note", "variants")}
                    )
                else:
                    self._next_id["term"] += 1
                    self.state["terms"].append({"id": self._next_id["term"], **t})
            return self._json({"status": "ok", "count": len(incoming)})

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}/members", path):
            return self._json(self.state["members"])

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}/tasks", path):
            page = int(url.params.get("page", 1))
            size = int(url.params.get("pageSize", 20))
            return self._page(self.state["tasks"], page, size)

        if method == "POST" and re.fullmatch(rf"/api/projects/{pid}/tasks", path):
            body = json.loads(request.content or b"{}")
            self._next_id["task"] += 1
            task = {
                "id": self._next_id["task"],
                "title": body.get("title", ""),
                "content": body.get("content", ""),
                "status": 0,
                "goal": body.get("goal", {}),
                "deadline": body.get("deadline"),
                "bumpedAt": None,
                "taskFiles": [],
            }
            self.state["tasks"].append(task)
            return self._json(task)

        m = re.fullmatch(rf"/api/projects/{pid}/tasks/(\d+)/files", path)
        if m and method == "POST":
            tid = int(m.group(1))
            body = json.loads(request.content or b"{}")
            task = next(t for t in self.state["tasks"] if t["id"] == tid)
            fid = int(body["fileId"])
            stats = self._file_stats(fid)
            tf = {
                "id": self._next_id["taskfile"],
                "task": tid,
                "file": {"id": fid, "name": next(f["name"] for f in self.state["files"] if f["id"] == fid)},
                "mode": int(body.get("mode", 0)),
                **stats,
            }
            self._next_id["taskfile"] += 1
            task["taskFiles"].append(tf)
            return self._json(tf)

        m = re.fullmatch(rf"/api/projects/{pid}/tasks/(\d+)/strings", path)
        if m and method == "PUT":
            body = json.loads(request.content or b"{}")
            n = len(body.get("items", []))
            key = "added" if body.get("op") == "add" else "removed"
            return self._json({key: n, "skipped": 0})

        # 创建词条（单条）。真实平台没有批量创建接口。
        if method == "POST" and re.fullmatch(rf"/api/projects/{pid}/strings", path):
            body = json.loads(request.content or b"{}")
            k = body.get("key")
            fid = body.get("fileId") or body.get("file")
            if not k or not fid:
                return self._json({"message": "key 和 fileId 必填"}, 400)
            if any(x["key"] == k and x["fileId"] == int(fid) for x in self.state["strings"]):
                return self._json({"message": "词条已存在"}, 400)
            row = {
                "id": self._next_id["string"],
                "key": k,
                "original": body.get("original") or "",
                "translation": body.get("translation") or "",
                "context": body.get("context") or "",
                "stage": 0,
                "fileId": int(fid),
                "project": 6896,
            }
            self._next_id["string"] += 1
            self.state["strings"].append(row)
            return self._json(row, 201)

        # 导入词条：update_file_translation 打的就是这个端点
        m = re.fullmatch(rf"/api/projects/{pid}/files/(\d+)/translation", path)
        if m and method == "POST":
            fid = int(m.group(1))
            body = json.loads(request.content or b"{}")
            items = body.get("items") or []
            force = bool(body.get("force"))
            by_key = {s["key"]: s for s in self.state["strings"]}
            created = updated = skipped = 0
            for it in items:
                k = it.get("key")
                if not k:
                    continue
                tr = it.get("translation") or ""
                if k in by_key:
                    row = by_key[k]
                    # 已有译文默认不覆盖，与真实平台一致
                    if row.get("translation") and not force:
                        skipped += 1
                        continue
                    row["translation"] = tr
                    row["stage"] = 1 if tr else 0
                    updated += 1
                else:
                    # 真实平台的 translation 接口**不会创建新词条**，
                    # 只更新已存在的。新增必须走 POST /strings。
                    skipped += 1
            return self._json(
                {"message": "ok", "created": created,
                 "updated": updated, "skipped": skipped}
            )

        m = re.fullmatch(rf"/api/projects/{pid}/tasks/(\d+)", path)
        if m and method == "POST":
            tid = int(m.group(1))
            body = json.loads(request.content or b"{}")
            task = next(t for t in self.state["tasks"] if t["id"] == tid)
            if body.get("op") == "finish":
                task["status"] = 1
            return self._json(task)

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}/announcements", path):
            # 真实平台返回分页包（实测 2026-10-23）。这里必须一致，否则
            # client 漏用 unwrap_page 的 bug 在假平台上永远测不出来。
            return self._page(
                self.state["announcements"],
                int(url.params.get("page", 1)),
                int(url.params.get("pageSize", 50)),
            )

        if method == "POST" and re.fullmatch(rf"/api/projects/{pid}/announcements", path):
            body = json.loads(request.content or b"{}")
            self._next_id["ann"] += 1
            ann = {
                "id": self._next_id["ann"],
                "title": body.get("title", ""),
                "content": body.get("content", ""),
                "category": body.get("category", "announcement"),
                "internal": bool(body.get("internal")),
                "bumpedAt": None,
            }
            self.state["announcements"].append(ann)
            return self._json(ann)

        m = re.fullmatch(rf"/api/projects/{pid}/announcements/(\d+)", path)
        if m and method == "PUT":
            aid = int(m.group(1))
            body = json.loads(request.content or b"{}")
            ann = next(a for a in self.state["announcements"] if a["id"] == aid)
            ann.update({k: v for k, v in body.items() if k in ("title", "content", "category", "internal")})
            return self._json(ann)

        # ---------------------------------------------------------- 讨论 Issues
        # 全部按真实平台实测行为实现（字段名与必填项都逐条真跑确认过）：
        #   POST /issues 必须带 title（只带 content → 400），且 stringId 被忽略；
        #   回复走 POST /issues/{id} {"op":"reply","content":...}，空 content → 400；
        #   列表/详情/修改/删除分别对应 GET /issues、GET /issues/{id}、PUT、DELETE。
        # 词条级评论端点（/comments、/strings/{id}/comments）在真实平台是 404，
        # 这里故意不实现，让误用立刻暴露。
        if method == "POST" and re.fullmatch(rf"/api/projects/{pid}/issues", path):
            body = json.loads(request.content or b"{}")
            title = str(body.get("title") or "").strip()
            content = body.get("content")
            if not title:
                return self._json({"message": "title required"}, 400)
            if not content:
                return self._json({"message": "content required"}, 400)
            self.state.setdefault("issues", [])
            self._next_id.setdefault("issue", 700)
            self._next_id["issue"] += 1
            issue = {
                "id": self._next_id["issue"],
                "project": 6896,
                "uid": 42,
                "parent": None,
                "title": title,
                "content": str(content),
                "status": 0,
                "lastEdit": None,
                "activities": [],
            }
            self.state["issues"].append(issue)
            return self._json(issue, 201)

        if method == "GET" and re.fullmatch(rf"/api/projects/{pid}/issues", path):
            rows = self.state.setdefault("issues", [])
            raw_status = url.params.get("status")
            if raw_status is not None:
                rows = [r for r in rows if int(r.get("status", 0)) == int(raw_status)]
            page = int(url.params.get("page", 1))
            size = int(url.params.get("pageSize", 50))
            resp = self._page(rows, page, size)
            payload = json.loads(resp.content)
            payload["openCount"] = len([r for r in rows if int(r.get("status", 0)) == 0])
            payload["closedCount"] = len([r for r in rows if int(r.get("status", 0)) == 1])
            return self._json(payload)

        m = re.fullmatch(rf"/api/projects/{pid}/issues/(\d+)", path)
        if m:
            iid = int(m.group(1))
            issues = self.state.setdefault("issues", [])
            issue = next((i for i in issues if i["id"] == iid), None)
            if issue is None:
                return self._json({"message": "Not Found"}, 404)
            if method == "GET":
                return self._json(issue)
            body = json.loads(request.content or b"{}")
            if method == "POST":
                op = body.get("op")
                if op == "reply":
                    if not body.get("content"):
                        return self._json({"message": "content required"}, 400)
                    self._next_id.setdefault("issue", 700)
                    self._next_id["issue"] += 1
                    reply = {
                        "id": self._next_id["issue"],
                        "project": 6896,
                        "uid": 42,
                        "parent": iid,
                        "title": None,
                        "content": str(body["content"]),
                        "status": 0,
                        "lastEdit": None,
                    }
                    issue.setdefault("activities", []).append(reply)
                    return self._json(reply, 201)
                if op in ("subscribe", "unsubscribe"):
                    return self._json({"id": iid, "tid": iid, "type": "issue", "uid": 42})
                return self._json({"message": f"unknown op {op}"}, 400)
            if method == "PUT":
                issue.update({k: v for k, v in body.items() if k in ("title", "content")})
                issue["lastEdit"] = 42
                return self._json(issue)
            if method == "DELETE":
                self.state["issues"] = [i for i in issues if i["id"] != iid]
                return self._json("OK")

        return self._json({"message": f"unhandled {method} {path}"}, 404)

    # ------------------------------------------------------------- 构造 client
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, **kw: Any):
        """构造一个走 MockTransport 的真实 client（token 仅占位，不出网）。"""
        from .client import ParaTranzClient

        return ParaTranzClient(
            token="test-token",
            project_id=6896,
            base_url="https://paratranz.cn/api",
            transport=self.transport(),
            rate_limit_per_min=100000,
            **kw,
        )
