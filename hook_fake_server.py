"""假 hook 后端：按 hook_http.py 里约定的极简协议提供 /messages /send /health。

用途：在真实 hook 还没接通前，验证「消息进来 → 风控决策 → 回复出去」整条链路，
包括 HTTP 这一层（而不是只在进程内跑 mock）。

跑法（两个终端）：
    终端 A： python hook_fake_server.py                 # 监听 127.0.0.1:5555
    终端 B： python bridge.py --use-http-hook           # 桥接改为通过 HTTP 收消息

想换消息内容就改 demo_chat.jsonl；想看桥接是否真在发送，就看本进程打印的 [SEND] 行。
仅绑定回环地址，不对外网开放。
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
QUEUE: list[dict[str, Any]] = []
SENT: list[dict[str, str]] = []
LOCK = threading.Lock()


def load_demo(path: Path) -> list[dict[str, Any]]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            items.append(json.loads(line))
    return items


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志，避免刷屏
        return None

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            self._json({"ok": True, "queued": len(QUEUE), "sent": len(SENT), "role": "fake-hook"})
        elif self.path.startswith("/messages"):
            with LOCK:
                batch, QUEUE[:] = list(QUEUE), []
            if batch:
                print(f"[POP] 投递 {len(batch)} 条待处理消息")
            self._json({"messages": batch})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        if self.path.startswith("/send"):
            with LOCK:
                SENT.append({"chat_id": str(payload.get("chat_id")), "text": str(payload.get("text"))})
            print(f"[SEND] -> {payload.get('chat_id')}: {payload.get('text')}")
            self._json({"ok": True, "sent_total": len(SENT)})
        else:
            self._json({"ok": False, "error": "not found"}, 404)


def main() -> int:
    ap = argparse.ArgumentParser(description="假 hook 后端（仅回环）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--demo", default=str(ROOT / "demo_chat.jsonl"))
    args = ap.parse_args()

    items = load_demo(Path(args.demo))
    with LOCK:
        QUEUE.extend(items)
    print(f"假 hook 后端启动：http://{args.host}:{args.port}  已装载 {len(items)} 条演示消息")
    print("等待桥接拉取…（Ctrl+C 退出）")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n退出。共发送：")
        for row in SENT:
            print(f"  {row['chat_id']}: {row['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
