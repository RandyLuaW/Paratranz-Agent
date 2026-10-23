"""测试用的假 OpenAI 兼容端点（真实 HTTP 服务）。

为什么需要它：产品里已经没有「无 key 也能跑」的降级，所以任何
走 LLM 的冒烟测试都必须提供一个真实的 HTTP 端点。这里用标准库起，
不引入额外依赖，回复内容交给 `tests/fake_llm.py` 生成（确定性）。

另外识别「让 Agent 写提取器」的请求（system prompt 含「词条提取器」），
回一个真实可执行的脚本，这样生成链路能端到端跑通。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from fake_llm import FakeLLM

_FAKE = FakeLLM()
_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# 「Agent 写提取器」用的固定返回：抽 JSON 里的 name 字段。
# code 从真实文件读，避免在字符串里手写换行转义。
GENERATED_EXTRACTOR = {
    "id": "agent-demo",
    "name": "Agent 演示提取器",
    "engine": "generic",
    "desc": "抽 JSON 里的 name 字段",
    "input_hint": "游戏根目录",
    "args": [{"name": "glob", "label": "文件匹配", "default": "**/*.json"}],
    "code": (_FIXTURES / "agent_demo_run.py").read_text(encoding="utf-8"),
}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            req = {}
        msgs = req.get("messages") or []
        sys_text = " ".join(
            str(m.get("content") or "") for m in msgs if m.get("role") == "system"
        )
        try:
            if "词条提取器" in sys_text:
                content = json.dumps(GENERATED_EXTRACTOR, ensure_ascii=False)
            else:
                content = _FAKE.chat(msgs)
        except Exception as exc:  # noqa: BLE001 - 端点内部错误也要回 200 让测试继续
            content = json.dumps({"error": repr(exc)})
        payload = {
            "id": "fake-cmpl",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 60},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        """支持 `GET /models`，用于验证「获取模型列表」探测功能。"""
        if not self.path.rstrip("/").endswith("/models"):
            self.send_error(404)
            return
        payload = {
            "object": "list",
            "data": [
                {"id": "gpt-4o", "object": "model"},
                {"id": "gpt-4o-mini", "object": "model"},
                {"id": "glm-4-flash", "object": "model"},
                {"id": "deepseek-chat", "object": "model"},
                {"id": "qwen-plus", "object": "model"},
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def start_fake_llm(port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"  [..] 假 LLM 端点在 127.0.0.1:{port}")
    return srv
