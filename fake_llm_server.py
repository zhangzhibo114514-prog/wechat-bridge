"""假模型服务：本机起一个 OpenAI 兼容端点，用来在没有真 key 的情况下端到端验证桥接的模型链路。

为什么需要：`--probe-llm` 只能证明"打得通 DeepSeek"（假 key 会拿到 401）。
但**响应解析**（choices[0].message.content）、超时、HTTP 错误映射这几段从没被真实响应覆盖过——
真发现解析写错，就是用户填完 key 才炸。这个假服务补上这一段。

它同时提供一个"坏端点"，用来验证桥接在模型返回异常时**不会假装成功**：
    /v1/chat/completions      正常返回一条 OpenAI 格式的回复
    /bad/no-choices           返回 200 但没有 choices 字段（解析必须报错）
    /bad/malformed            返回不是 JSON 的内容
    /bad/500                  返回 500

用法（两个终端）：
    终端 A： python fake_llm_server.py
    终端 B： python llm_endpoint_selftest.py      # 自动起服务、跑完四种情况、再关掉

也可以手动拿来当真模型用：把 bridge_config.json 的 base_url 改成 http://127.0.0.1:8899、
path 改成 /v1/chat/completions、api_key 随便填，就能在没有 key 的情况下跑通整条"桥接→模型→回复"链路。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY_TEXT = "收到，我回头确认下。"          # 假模型固定回这句，便于断言


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):     # 静音默认访问日志
        return None

    def _send(self, code: int, payload, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"

        if self.path.startswith("/bad/no-choices"):
            self._send(200, {"id": "x", "object": "chat.completion"})       # 缺 choices
            return
        if self.path.startswith("/bad/malformed"):
            self._send(200, b"this is not json", "text/plain")
            return
        if self.path.startswith("/bad/500"):
            self._send(500, {"error": {"message": "boom"}})
            return

        # 正常路径：回一条标准 OpenAI 格式
        try:
            payload = json.loads(raw)
            model = payload.get("model", "fake-model")
        except Exception:
            model = "fake-model"
        self._send(200, {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": REPLY_TEXT},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        })

    def do_GET(self) -> None:
        if self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
            return
        self._send(404, {"error": "not found"})


def main() -> int:
    ap = argparse.ArgumentParser(description="假 OpenAI 兼容模型服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    print(f"假模型服务：http://{args.host}:{args.port}/v1/chat/completions")
    print("固定回复：", REPLY_TEXT)
    print("坏端点：/bad/no-choices  /bad/malformed  /bad/500")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
