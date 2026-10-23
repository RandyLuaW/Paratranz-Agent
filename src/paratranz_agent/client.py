"""ParaTranz OpenAPI (v0.5.7) 全量客户端。

设计要点
- 覆盖平台 60 个端点中的全部写操作能力，重点是 Tasks / Terms / Announcements /
  Suggestions / batchOperateStrings —— 官方 MCP 生态（fmwizard/paratranz-mcp-server）
  缺少的正是这几块，而它们恰好是"自动分派 + 自动校对 + 发布术语/规范"的支点。
- 全局限流按用户 480 次/分钟；这里默认 420 留余量，令牌桶平滑。
- Suggestions（翻译建议）接口在 ES 上执行相似度检索，服务端明确要求串行，
  所以单独走 `suggest_serial`，内部加锁。
- 423（文件正在被其他用户更新/回滚）与 429 走退避重试。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterable, Iterator, Sequence

import httpx

from .models import (
    Announcement,
    FileItem,
    Member,
    ProjectInfo,
    Stage,
    StringItem,
    Task,
    Term,
)

DEFAULT_BASE_URL = "https://paratranz.cn/api"
DEFAULT_UA = "paratranz-agent/0.1 (+https://paratranz.cn)"

# 平台限制：批量词条操作单次上限
BATCH_LIMIT = 500
TASK_STRING_LIMIT = 1000


class ParaTranzError(RuntimeError):
    def __init__(self, status: int, message: str, path: str, request_id: str | None = None):
        self.status = status
        self.message = message
        self.path = path
        self.request_id = request_id
        super().__init__(f"[{status}] {path}: {message}" + (f" (id={request_id})" if request_id else ""))


class RateLimiter:
    """令牌桶。

    两个参数分开，是因为「每分钟总量」和「瞬时突发」不是一回事：
    - `per_minute` 是平台允许的**平均速率**（480/min 按用户计）；
    - `burst` 是**瞬时**最多能连发多少。如果把桶容量直接设成 per_minute，
      启动瞬间会把 420 个请求一口气打出去，必然触发 429。
    默认 burst = per_minute / 20（420 → 21），之后按平均速率补充。
    """

    def __init__(self, per_minute: int = 420, burst: int | None = None):
        self.rate = max(0.001, float(per_minute) / 60.0)  # 每秒补充
        self.burst = float(burst if burst is not None else max(1, per_minute // 20))
        self.tokens = self.burst
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> None:
        n = min(n, self.burst)  # 单次需求不能超过桶容量，否则永远等不到
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            # 直接睡够所需时间，不做 1 秒粒度的忙等
            time.sleep(min(wait, 5.0))


def unwrap_page(data: Any, *, path: str = "") -> list[Any]:
    """平台分页接口返回 `{results, page, pageSize, rowCount, pageCount}`。

    `/files` 等少数接口直接返回裸数组。这里统一成 list，
    否则 `for x in data` 会遍历到字典的 key（真实踩过：
    status 崩在 `StringItem.model_validate('results')`）。

    **认不出来的形状要报错，不能返回空列表**：空列表在下游等价于「这个项目
    一条数据都没有」，于是平台一改 envelope 字段名，界面就显示「0 条词条」
    而不是「客户端不认这个响应」。空字典/None 才是真的没有数据。
    """
    if isinstance(data, list):
        return data
    if data is None or (isinstance(data, dict) and not data):
        return []
    if isinstance(data, dict):
        for key in ("results", "items", "data", "rows"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        raise ParaTranzError(
            0,
            "无法识别的列表响应（键：" + "、".join(sorted(map(str, data)))[:120] + "）",
            path or "(unknown)",
        )
    raise ParaTranzError(
        0, f"无法识别的响应类型：{type(data).__name__}", path or "(unknown)"
    )


def _unwrap_file(data: Any) -> dict:
    """上传类接口把文件包在 `{"file": {...}}` 里（实测），其余情况直接是文件对象。"""
    if isinstance(data, dict):
        inner = data.get("file")
        if isinstance(inner, dict):
            return inner
        if "id" in data:
            return data
    raise ValueError(f"上传接口返回体无法识别：{str(data)[:200]}")


class BatchWrite(list):
    """批量写回的返回值：**仍然是 `list[int]`**（现有调用方照旧遍历、取长度），
    额外带一个 `reported` —— 平台确认改动的条数。

    两者必须分开：`PUT /strings` 的响应形状不止一种，除了 id 数组，还有只给
    条数的 `{"update": N}`。只回条数时 `len(res)` 是 0，上层据此报「写回 0 条」
    就是误导（stage 其实已经改完）；而凭空造 id 又会让 TM/账本记到错的词条上。
    所以：`len(res)` = 确知其 id 的条数，`reported` = 平台报告的改动条数 ——
    「改了 N 条但不知道是哪几条」和「一条都没改」由此可以区分。
    """

    def __init__(self, ids: Iterable[int] = (), *, reported: int):
        super().__init__(ids)
        self.reported = int(reported)


# `PUT /strings` 只回条数时的字段名（平台不同版本的实测形状）
_EDIT_COUNT_KEYS = ("update", "updated", "count")


def _edit_result(res: Any, part: Sequence[dict]) -> tuple[list[int], int]:
    """解析批量写回响应 → (确认的 id 列表, 确认改动的条数)。

    - id 数组：最常见，条数 = len(ids)；
    - `{"update": N}`：只有条数。N 等于本次提交条数时 id 可以确定（整块都成功），
      直接补全；对不上就**不猜**，id 留空、只回条数；
    - 2xx 空 body：请求已被接受，按整块记账（与 id 数组语义一致）；
    - 其它形状（字符串/数字/认不出的 dict）直接报错：静默返回空列表会让调用方
      以为一条都没改。
    """
    if isinstance(res, list):
        ids = [int(x) for x in res]
        return ids, len(ids)
    if res is None:
        ids = [int(it["id"]) for it in part]
        return ids, len(ids)
    if isinstance(res, dict):
        for key in _EDIT_COUNT_KEYS:
            count = res.get(key)
            if isinstance(count, int):
                ids = [int(it["id"]) for it in part] if count == len(part) else []
                return ids, count
        raise ParaTranzError(0, f"批量写回响应缺少更新条数：{str(res)[:200]}", "/strings")
    raise ParaTranzError(0, f"批量写回响应形状无法识别：{str(res)[:200]}", "/strings")


class ParaTranzClient:
    def __init__(
        self,
        token: str,
        project_id: int,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit_per_min: int = 420,
        timeout: float = 60.0,
        user_agent: str = DEFAULT_UA,
        transport: httpx.BaseTransport | None = None,
    ):
        self.token = token
        self.project_id = int(project_id)
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(rate_limit_per_min)
        self._suggest_lock = threading.Lock()
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Authorization": token,
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
            transport=transport,
        )

    # ------------------------------------------------------------------ 底层
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ParaTranzClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return path.replace("{projectId}", str(self.project_id))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Any = None,
        files: Any = None,
        data: Any = None,
        retries: int = 3,
        expect_json: bool = True,
        idempotent: bool = True,
    ) -> Any:
        """发请求。

        `idempotent=False` 用于**创建类** POST（建任务、建公告、建术语、加任务文件）：
        这类请求一旦被服务端执行，重试就会产生重复数据。所以只对
        「确定没被执行」的状态重试 —— 连接层错误（未送达）、429（被限流拒绝）、
        423（资源锁定拒绝）；对 5xx **不重试**，因为服务端可能已经写入。
        """
        url = self._url(path)
        # 这些状态码表示请求**未被处理**，重试对任何方法都安全
        safe_retry = {423, 429}
        # 这些状态码表示服务端可能已经处理，只有幂等请求才敢重试
        risky_retry = {500, 502, 503, 504}
        last: Exception | None = None

        for attempt in range(retries + 1):
            self.limiter.acquire()
            try:
                resp = self._client.request(
                    method, url, json=json_body, params=params, files=files, data=data
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last = e
                time.sleep(min(2 ** attempt, 10))
                continue

            retryable = resp.status_code in safe_retry or (
                idempotent and resp.status_code in risky_retry
            )
            if retryable and attempt < retries:
                last = ParaTranzError(resp.status_code, resp.text[:300], path)
                time.sleep(min(2 ** attempt, 10) + (1 if resp.status_code == 429 else 0))
                continue

            if resp.status_code >= 400:
                payload: dict[str, Any] = {}
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"message": resp.text[:300]}
                raise ParaTranzError(
                    resp.status_code,
                    str(payload.get("message", "")),
                    path,
                    payload.get("id"),
                )

            if not expect_json:
                return resp.content
            if not resp.content:
                return None
            try:
                return resp.json()
            except json.JSONDecodeError:
                return resp.text

        raise ParaTranzError(0, f"重试耗尽: {last}", path)

    # ------------------------------------------------------------- Projects
    def get_project(self) -> ProjectInfo:
        return ProjectInfo.model_validate(self.request("GET", "/projects/{projectId}"))

    def list_projects(self, page: int = 1, page_size: int = 20) -> list[dict]:
        return unwrap_page(
            self.request("GET", "/projects", params={"page": page, "pageSize": page_size})
        )

    def update_project(self, payload: dict) -> ProjectInfo:
        return ProjectInfo.model_validate(self.request("PUT", "/projects/{projectId}", json_body=payload))

    # ---------------------------------------------------------------- Files
    def list_files(self) -> list[FileItem]:
        return [FileItem.model_validate(x) for x in self.request("GET", "/projects/{projectId}/files")]

    def get_file(self, file_id: int) -> FileItem:
        return FileItem.model_validate(self.request("GET", f"/projects/{{projectId}}/files/{file_id}"))

    def get_file_strings(self, file_id: int) -> list[StringItem]:
        """单文件全部词条（不分页），含原文+译文+stage。"""
        data = self.request("GET", f"/projects/{{projectId}}/files/{file_id}/translation")
        if isinstance(data, str):
            data = json.loads(data)
        return [StringItem.model_validate(x) for x in data]

    def update_file_translation(
        self,
        file_id: int,
        items: Sequence[dict],
        filename: str,
        *,
        force: bool = False,
        skip_hash: bool = False,
    ) -> dict:
        """直接提交词条数组（application/json 形态），比传文件更省事。

        注意：只会更新数据库中已存在的词条；force=False 时会跳过
        空译文 / 译文=原文 / 与现有译文相同 / 已人工翻译过的词条。
        """
        body = {
            "items": [{"key": i["key"], **{k: v for k, v in i.items() if k != "key"}} for i in items],
            "filename": filename,
            "force": force,
        }
        if skip_hash:
            body["skip"] = True
        return self.request("POST", f"/projects/{{projectId}}/files/{file_id}/translation", json_body=body)

    def get_file_content(self, file_id: int, *, rev: int | None = None, tid: int | None = None) -> dict:
        params = {k: v for k, v in {"rev": rev, "tid": tid}.items() if v is not None}
        return self.request("GET", f"/projects/{{projectId}}/files/{file_id}/content", params=params)

    def create_file(self, filename: str, content: bytes, *, path: str = "") -> FileItem:
        """上传并创建文件（multipart：`file` + `path`）。

        平台按项目格式解析上传的文件并**自动建出词条** —— 这是「导入全新文件」
        的正确入口。`path` 是目标目录（如 `"locale/"`，省略即项目根目录）；
        文件名不能与目标目录里已有文件重名，否则平台拒绝。

        返回体是 `{"file": {...}}` 包了一层（实测 2026-10-23），这里拆开。
        """
        files = {"file": (filename, content, "application/octet-stream")}
        data = {"path": path} if path else {}
        res = self.request(
            "POST",
            "/projects/{projectId}/files",
            files=files,
            data=data,
            idempotent=False,
        )
        return FileItem.model_validate(_unwrap_file(res))

    def update_file(self, file_id: int, filename: str, content: bytes) -> FileItem:
        """用新内容替换文件（multipart 上传同一个 `file` 字段）。"""
        files = {"file": (filename, content, "application/octet-stream")}
        res = self.request(
            "POST",
            f"/projects/{{projectId}}/files/{file_id}",
            files=files,
            idempotent=False,
        )
        return FileItem.model_validate(_unwrap_file(res))

    def delete_file(self, file_id: int) -> None:
        """删除文件（连同它下面的词条）。不可逆，调用方必须先确认。"""
        self.request("DELETE", f"/projects/{{projectId}}/files/{file_id}")

    # -------------------------------------------------------------- Strings
    def list_strings(
        self,
        *,
        page: int = 1,
        page_size: int = 500,
        file: Sequence[int] | None = None,
        task: int | None = None,
        stage: Stage | int | None = None,
        detailed: bool = False,
    ) -> list[StringItem]:
        params: list[tuple[str, Any]] = [("page", page), ("pageSize", page_size)]
        for fid in file or []:
            params.append(("file", fid))
        if task is not None:
            params.append(("task", task))
        if stage is not None:
            params.append(("stage", int(stage)))
        if detailed:
            params.append(("detailed", 1))
        data = self.request("GET", "/projects/{projectId}/strings", params=params)
        return [StringItem.model_validate(x) for x in unwrap_page(data)]

    def iter_strings(
        self,
        *,
        page_size: int = 500,
        file: Sequence[int] | None = None,
        task: int | None = None,
        stage: Stage | int | None = None,
        max_pages: int | None = None,
        max_items: int | None = None,
    ) -> Iterator[StringItem]:
        """分页迭代词条。

        两道兜底，防止平台行为异常时把调用方拖死：
        - `max_pages`（默认 2000 页 ≈ 100 万条）硬上限；
        - **重复 id 检测**：若平台忽略 `page` 参数一直返回同一页，第二次
          见到重复 id 就终止，而不是无限循环。
        """
        page = 1
        hard_cap = max_pages or 2000
        seen: set[int] = set()
        yielded = 0
        while page <= hard_cap:
            batch = self.list_strings(
                page=page, page_size=page_size, file=file, task=task, stage=stage
            )
            if not batch:
                return
            fresh = [s for s in batch if s.id not in seen]
            if not fresh:
                # 全是见过的 id → 平台没有真的翻页，停止而不是空转
                return
            for s in fresh:
                seen.add(s.id)
                yield s
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            if len(batch) < page_size:
                return
            page += 1

    def get_string(self, string_id: int) -> StringItem:
        data = self.request("GET", f"/projects/{{projectId}}/strings/{string_id}")
        if not isinstance(data, dict):
            # 已删除的词条真实平台返回 null（不是 404），这里给个可读错误
            raise ParaTranzError(404, "词条不存在（可能已删除）", f"/strings/{string_id}")
        return StringItem.model_validate(data)

    def batch_edit_strings(self, items: Sequence[dict], *, chunk: int = BATCH_LIMIT) -> BatchWrite:
        """批量写译文 / 改 stage。items 每项至少含 id，可含 translation/stage/context。

        返回值见 `BatchWrite`：id 明细与「平台报告的改动条数」分开给，
        报表要报「实际改了多少」时用后者，别拿 `len()` 当条数。
        """
        ids: list[int] = []
        reported = 0
        items = list(items)
        for i in range(0, len(items), chunk):
            part = items[i : i + chunk]
            res = self.request(
                "PUT", "/projects/{projectId}/strings", json_body={"op": "edit", "items": part}
            )
            part_ids, part_reported = _edit_result(res, part)
            ids.extend(part_ids)
            reported += part_reported
        return BatchWrite(ids, reported=reported)

    def create_strings(
        self, items: Sequence[dict], file_id: int, *, on_progress=None
    ) -> dict:
        """在指定文件下创建词条。

        平台**没有批量创建接口**（`POST /strings` 一次只能建一条），
        所以这里是逐条 POST，受全局令牌桶限流保护。

        注意：`POST /files/{fileId}/translation` 只更新**已存在**的词条，
        新 key 会被静默忽略——要新增词条必须走这个接口（需管理员权限）。

        items 每项需含 key / original，可选 context。
        返回 {"created": [id...], "failed": [{"key","error"}...]}
        """
        created: list[int] = []
        failed: list[dict] = []
        items = list(items)
        for idx, it in enumerate(items, 1):
            body = {
                "key": str(it["key"]),
                "original": str(it.get("original") or ""),
                # translation 必须显式给（哪怕空串）：真实平台缺这个字段直接 500，
                # spec 里没标它必填，但实测必须带 —— 否则导入会全部失败
                "translation": str(it.get("translation") or ""),
                "context": str(it.get("context") or ""),
                "fileId": int(file_id),
            }
            try:
                res = self.request(
                    "POST", "/projects/{projectId}/strings", json_body=body
                )
            except ParaTranzError as exc:
                # 单条失败不中断整批，记下来让调用方展示
                failed.append({"key": body["key"], "error": str(exc)})
            else:
                sid = (res or {}).get("id") if isinstance(res, dict) else None
                if sid:
                    created.append(int(sid))
                else:
                    # 2xx 却拿不到 id：这条既不能算成功（不知道建成什么）也不能
                    # 悄悄丢掉 —— 否则「提交 N 条、created+failed 少于 N」，
                    # 没人看得出少的那几条去哪了。
                    failed.append({
                        "key": body["key"],
                        "error": "平台返回 2xx 但响应体里没有 id，无法确认是否已创建"
                                 f"（{type(res).__name__}）",
                    })
            if on_progress and (idx % 20 == 0 or idx == len(items)):
                on_progress(idx, len(items))
        return {"created": created, "failed": failed}

    def existing_keys(self, file_id: int) -> set[str]:
        """该文件下已存在的所有 key（用于导入前算差集）。"""
        return {x.key for x in self.get_file_strings(file_id)}

    def set_stage(self, string_ids: Iterable[int], stage: Stage) -> BatchWrite:
        return self.batch_edit_strings([{"id": int(i), "stage": int(stage)} for i in string_ids])

    def get_suggestions(self, string_id: int, *, scope: str = "self", rate: int = 70) -> list[dict]:
        """翻译建议（TM/关联项目相似句）。服务端要求串行，这里加进程内互斥。"""
        with self._suggest_lock:
            data = self.request(
                "GET",
                f"/projects/{{projectId}}/strings/{string_id}/suggestions",
                params={"scope": scope, "rate": rate},
                retries=1,
            )
        return unwrap_page(
            data, path=f"/projects/*/strings/{string_id}/suggestions"
        )

    # ---------------------------------------------------------------- Terms
    def list_terms(self, page: int = 1, page_size: int = 500) -> list[Term]:
        data = self.request("GET", "/projects/{projectId}/terms", params={"page": page, "pageSize": page_size})
        return [Term.model_validate(x) for x in unwrap_page(data)]

    def iter_terms(self, page_size: int = 500, max_pages: int = 400) -> Iterator[Term]:
        """分页迭代术语。

        与 `iter_strings` 同一套兜底：硬页数上限 + 重复 id 检测。
        没有这两条时，平台一旦忽略 `page` 参数就**无限循环**（实测：一直请求
        第 N 页），`sync_terms_from_platform` 会挂死，而用户只看到"卡住了"。
        """
        page = 1
        seen: set[int] = set()
        while page <= max_pages:
            batch = self.list_terms(page=page, page_size=page_size)
            if not batch:
                return
            fresh = [t for t in batch if t.id not in seen]
            if not fresh:
                # 全是见过的 id → 平台没有真的翻页，停止而不是空转
                return
            for t in fresh:
                seen.add(t.id)
            yield from fresh
            if len(batch) < page_size:
                return
            page += 1

    def create_term(
        self,
        term: str,
        translation: str,
        *,
        pos: str | None = None,
        note: str | None = None,
        variants: Sequence[str] | None = None,
        case_sensitive: bool | None = None,
    ) -> Term:
        body: dict[str, Any] = {"term": term, "translation": translation}
        if pos:
            body["pos"] = pos
        if note:
            body["note"] = note
        if variants:
            body["variants"] = list(variants)
        if case_sensitive is not None:
            body["caseSensitive"] = case_sensitive
        return Term.model_validate(
            self.request("POST", "/projects/{projectId}/terms", json_body=body, idempotent=False)
        )

    def update_term(self, term_id: int, payload: dict) -> Term:
        return Term.model_validate(
            self.request("PUT", f"/projects/{{projectId}}/terms/{term_id}", json_body=payload)
        )

    def import_terms(self, terms: Sequence[dict]) -> dict:
        """批量导入/覆盖术语。服务端为 multipart JSON 文件。"""
        payload = json.dumps(list(terms), ensure_ascii=False).encode("utf-8")
        files = {"file": ("terms.json", payload, "application/json")}
        return self.request("PUT", "/projects/{projectId}/terms", files=files)

    # ---------------------------------------------------------------- Tasks
    def list_tasks(self, page: int = 1, page_size: int = 50) -> list[Task]:
        data = self.request("GET", "/projects/{projectId}/tasks", params={"page": page, "pageSize": page_size})
        return [Task.model_validate(x) for x in unwrap_page(data)]

    def create_task(
        self,
        title: str,
        content: str = "",
        *,
        goal: dict | None = None,
        deadline: str | None = None,
        bumped: bool = False,
    ) -> Task:
        body: dict[str, Any] = {
            "title": title[:128],
            "content": content,
            "goal": goal or {"translate": 1.0, "check": 0.0, "review": 0.0},
            "deadline": deadline,
            "bumped": bumped,
        }
        return Task.model_validate(
            self.request("POST", "/projects/{projectId}/tasks", json_body=body, idempotent=False)
        )

    def get_task(self, task_id: int) -> Task:
        return Task.model_validate(self.request("GET", f"/projects/{{projectId}}/tasks/{task_id}"))

    def save_task(self, task_id: int, **fields: Any) -> Task:
        return Task.model_validate(
            self.request("PUT", f"/projects/{{projectId}}/tasks/{task_id}", json_body=fields)
        )

    def operate_task(self, task_id: int, op: str) -> Task:
        """op: pin | unpin | finish"""
        return Task.model_validate(
            self.request("POST", f"/projects/{{projectId}}/tasks/{task_id}", json_body={"op": op})
        )

    def delete_task(self, task_id: int) -> None:
        """删任务（需管理员）。用于回滚建到一半的失败任务。"""
        try:
            self.request("DELETE", f"/projects/{{projectId}}/tasks/{task_id}")
        except ParaTranzError:
            pass  # 回滚失败不掩盖原始错误

    def add_task_file(self, task_id: int, file_id: int, mode: int = 0) -> dict:
        """mode 0 = 词条模式（只追踪显式加入的词条），1 = 文件模式（追踪整文件）。"""
        return self.request(
            "POST", f"/projects/{{projectId}}/tasks/{task_id}/files",
            json_body={"fileId": file_id, "mode": mode},
            idempotent=False,
        )

    def operate_task_strings(self, task_id: int, op: str, string_ids: Sequence[int]) -> dict:
        """op: add | remove。单次上限 1000。"""
        ids = list(string_ids)
        total = {"added": 0, "removed": 0, "skipped": 0}
        for i in range(0, len(ids), TASK_STRING_LIMIT):
            res = self.request(
                "PUT", f"/projects/{{projectId}}/tasks/{task_id}/strings",
                json_body={"op": op, "items": ids[i : i + TASK_STRING_LIMIT]},
            ) or {}
            if not isinstance(res, dict) or not (set(total) & set(res)):
                # 空 dict / 形状不认识时按 0 累计，就等于「平台说加了 0 条」——
                # 而实际情况是"我们没看懂它的回应"。这两件事必须区分。
                raise ParaTranzError(
                    0,
                    "任务词条操作返回了无法识别的结果（键："
                    + "、".join(sorted(map(str, res)))[:120] + "）",
                    f"/projects/*/tasks/{task_id}/strings",
                )
            for k in total:
                total[k] += int(res.get(k, 0) or 0)
        return total

    # --------------------------------------------------------- Announcements
    def list_announcements(self, page: int = 1, page_size: int = 50) -> list[Announcement]:
        data = self.request(
            "GET", "/projects/{projectId}/announcements", params={"page": page, "pageSize": page_size}
        )
        # 真实平台这里返回分页包（实测 2026-10-23：{page,pageCount,pageSize,rowCount,results}）。
        # 漏掉 unwrap_page 会让 styleguide 的「已存在则更新」永远匹配不到 →
        # 每跑一次多一条重复公告，而且异常被 except 吞掉，表面上一切正常。
        return [Announcement.model_validate(x) for x in unwrap_page(data)]

    def create_announcement(
        self,
        title: str,
        content: str,
        *,
        category: str = "announcement",
        internal: bool = False,
        bumped: bool = False,
    ) -> Announcement:
        body = {"title": title, "content": content, "category": category, "internal": internal, "bumped": bumped}
        return Announcement.model_validate(
            self.request("POST", "/projects/{projectId}/announcements", json_body=body, idempotent=False)
        )

    def update_announcement(self, ann_id: int, payload: dict) -> Announcement:
        return Announcement.model_validate(
            self.request("PUT", f"/projects/{{projectId}}/announcements/{ann_id}", json_body=payload)
        )

    # --------------------------------------------------------------- Issues
    def create_issue(self, title: str, content: str, *, project_id: int | None = None) -> dict:
        """发起项目级讨论（`POST /projects/{projectId}/issues`）。

        实测（真实项目，9087 条词条）：

        - **`title` 是必填**。`{"content": ...}` 和 `{"content": ..., "stringId": ...}`
          都返回 **400**，只有带 `title` 才 201。
        - **`stringId` 被静默忽略**：带不带它创建出来的讨论一模一样，返回体里
          只有 `project/title/content/uid/status/parent`，**没有任何指向词条的字段**。
        - spec 里确实有 `Comment` schema（`type: text|announcement|term` + `tid`），
          但**没有公开端点**：`/comments`、`/strings/{id}/comments`、`/strings/{id}/comment`
          全部 404。

        结论：**平台侧不存在「词条级评论」的写接口**。打回理由只能落成项目级讨论帖，
        所以 review 把一批打回项汇总成一条帖子（而不是逐条评论）。

        创建类 POST → `idempotent=False`：5xx 重试会在平台上留下重复帖子。
        """
        body: dict[str, Any] = {"title": str(title), "content": str(content)}
        path = (
            f"/projects/{int(project_id)}/issues"
            if project_id is not None
            else "/projects/{projectId}/issues"
        )
        res = self.request("POST", path, json_body=body, idempotent=False)
        # 平台可能返回 None / 非 dict；统一成 dict，调用方不必再做类型判断
        return res if isinstance(res, dict) else {"result": res}

    def reply_issue(self, issue_id: int, content: str) -> dict:
        """在讨论里追加一条回复（`POST /projects/{projectId}/issues/{issueId}`，`op=reply`）。

        实测：正文为空返回 400 `content required`；markdown 表格会被渲染成
        `<table>`，所以打回明细用表格排。
        """
        res = self.request(
            "POST",
            f"/projects/{{projectId}}/issues/{int(issue_id)}",
            json_body={"op": "reply", "content": str(content)},
            idempotent=False,
        )
        return res if isinstance(res, dict) else {"result": res}

    def get_issue(self, issue_id: int) -> dict:
        """单条讨论 + 它的回复（回复在 `activities` 里）。"""
        res = self.request("GET", f"/projects/{{projectId}}/issues/{int(issue_id)}")
        return res if isinstance(res, dict) else {"result": res}

    def list_issues(self, status: int | None = 0, page: int = 1, page_size: int = 50) -> list[dict]:
        """讨论列表。`status`：0=讨论中，1=已关闭，None=全部。

        真实返回是分页包（另带 `openCount` / `closedCount`），这里只取 results。
        """
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if status is not None:
            params["status"] = status
        data = self.request("GET", "/projects/{projectId}/issues", params=params)
        return [x for x in unwrap_page(data) if isinstance(x, dict)]

    # -------------------------------------------------- Members / history ...
    def list_members(self) -> list[Member]:
        return [Member.model_validate(x) for x in self.request("GET", "/projects/{projectId}/members")]

    def get_history(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        type: str | None = None,
        uid: int | None = None,
        tid: int | None = None,
        begin: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        params = {
            k: v
            for k, v in {"page": page, "pageSize": page_size, "type": type, "uid": uid,
                         "tid": tid, "begin": begin, "end": end}.items()
            if v is not None
        }
        return unwrap_page(self.request("GET", "/projects/{projectId}/history", params=params))

    def get_scores(self, page: int = 1, page_size: int = 100) -> list[dict]:
        return unwrap_page(
            self.request(
                "GET", "/projects/{projectId}/scores", params={"page": page, "pageSize": page_size}
            )
        )

    # ------------------------------------------------------------ Artifacts
    def generate_artifact(self) -> dict:
        return self.request("POST", "/projects/{projectId}/artifacts")

    def get_latest_artifact(self) -> dict:
        return self.request("GET", "/projects/{projectId}/artifacts/latest")

    def download_artifact(self) -> bytes:
        return self.request("GET", "/projects/{projectId}/artifacts/download", expect_json=False)
