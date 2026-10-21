"""wxhook（4.x 分支框架）探测与适配脚手架。

背景：本机微信是 4.1.15.13，而 wxhook 公开适配表最高到 4.1.13.56 —— 版本对不上是**首查项**。
本脚本在装上 wxhook 之后运行，一次性回答三个问题：
    1. 这个包到底装进来了吗？它自报支持哪些微信版本？
    2. 当前客户端版本和它匹配吗？
    3. 它的核心方法叫什么（收消息要不要开线程、发消息的入口是哪个）？

用法：
    python wxhook_probe.py                 # 只探测，不连接
    python wxhook_probe.py --connect       # 额外尝试建立连接（会真的挂上微信）
    python wxhook_probe.py --emit-shim shim.py   # 生成适配骨架，把 wxhook 的 API 接到本项目的 HTTP 协议

为什么优先走 HTTP 协议而不是直接读 wxhook 的 API：4.x 框架的接口变来变去，
桥接层只需要「拉消息 / 发消息」两个动作，用一层 shim 隔开，框架升级不会打到主逻辑。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import sys
from pathlib import Path

WXHOOK_SUPPORTED = [
    "3.6.0.18", "3.9.2.23", "3.9.5.81", "3.9.10.19", "3.9.10.27", "3.9.11.17", "3.9.12.15",
    "3.9.12.45", "3.9.12.51", "4.0.1.21", "4.0.3.22", "4.1.0.34", "4.1.2.17", "4.1.5.16",
    "4.1.6.14", "4.1.6.46", "4.1.7.30", "4.1.8.27", "4.1.8.67", "4.1.11.24", "4.1.12.26",
    "4.1.12.55", "4.1.13.12", "4.1.13.56",
]

SHIM_TEMPLATE = '''"""wxhook -> 本桥接 HTTP 协议的 shim（由 wxhook_probe.py 生成）。
把下面的 TODO 换成 wxhook 的真实调用，然后跑：
    python shim.py          # 它会以 /messages /send /health 三个接口暴露给 bridge
    python bridge.py --use-http-hook --hook-url http://127.0.0.1:5555
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUEUE: list[dict] = []
LOCK = threading.Lock()

# TODO 1：按 wxhook 文档创建客户端实例（示例形态，方法名以其文档为准）
# import wxhook
# client = wxhook.WxHook()          # 或 WxClient(...)，看它导出了什么
# client.start()                    # 有的框架需要 start/run 才会收消息


def on_message(raw):
    """TODO 2：把 wxhook 回调里的字段映射到本项目的统一结构。"""
    item = {
        "chat_id": str(getattr(raw, "room_id", "") or getattr(raw, "wxid", "")),
        "sender_id": str(getattr(raw, "sender", "") or getattr(raw, "wxid", "")),
        "sender_name": str(getattr(raw, "sender_name", "") or ""),
        "text": str(getattr(raw, "content", "") or getattr(raw, "msg", "")),
        "is_group": bool(getattr(raw, "room_id", "")),
        "msg_id": str(getattr(raw, "msg_id", "") or getattr(raw, "id", "")),
        "ts": time.time(),
        "is_self": bool(getattr(raw, "from_self", False)),
        "mentions_me": False,      # TODO 3：群里 @ 自己的判断（拿到自己 wxid 后补）
    }
    with LOCK:
        QUEUE.append(item)


def send_text(chat_id: str, text: str) -> bool:
    """TODO 4：换成 wxhook 的发消息调用。"""
    # return client.send_text(chat_id, text) is not None
    raise NotImplementedError("请填入 wxhook 的发送方法")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        return None

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json({"ok": True, "queued": len(QUEUE), "role": "wxhook-shim"})
        elif self.path.startswith("/messages"):
            with LOCK:
                batch, QUEUE[:] = list(QUEUE), []
            self._json({"messages": batch})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
        if self.path.startswith("/send"):
            ok = send_text(str(payload.get("chat_id")), str(payload.get("text")))
            self._json({"ok": bool(ok)})
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    # TODO 5：注册回调，例如 client.on_message = on_message（或 client.add_listener(on_message)）
    print("shim 启动：http://127.0.0.1:5555（记得先完成上面的 TODO）")
    ThreadingHTTPServer(("127.0.0.1", 5555), Handler).serve_forever()
'''


def current_wechat_version() -> str:
    import subprocess
    exe = Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe")
    if not exe.exists():
        return "未找到客户端"
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"(Get-Item '{exe}').VersionInfo.FileVersion"],
        capture_output=True, text=True, errors="ignore",
    ).stdout.strip()
    return out or "未知"


def main() -> int:
    ap = argparse.ArgumentParser(description="wxhook 探测与适配脚手架")
    ap.add_argument("--connect", action="store_true", help="尝试真的建立连接（会挂上微信）")
    ap.add_argument("--emit-shim", default=None, help="生成 shim 文件路径")
    args = ap.parse_args()

    ver = current_wechat_version()
    print(f"当前微信版本：{ver}")
    print(f"wxhook 公开适配表最高：{max(WXHOOK_SUPPORTED, key=lambda v: tuple(int(x) for x in v.split('.')))}")
    if ver not in WXHOOK_SUPPORTED:
        print("[注意] 版本不在适配表内。先做这一步：拿框架自带 demo 试连，连不上就别继续往下做。")

    if args.emit_shim:
        out = Path(args.emit_shim)
        out.write_text(SHIM_TEMPLATE, encoding="utf-8")
        print(f"已生成 shim 骨架：{out}")
        return 0

    print("\n== 包探测 ==")
    try:
        spec = importlib.util.find_spec("wxhook")
    except Exception:
        spec = None
    if spec is None:
        print("[缺] 没找到 wxhook 包。装法以框架文档为准（多数是私有分发，pip 官方源里可能没有）")
        return 1
    print(f"[OK] wxhook 已安装：{spec.origin}")

    mod = importlib.import_module("wxhook")
    names = [n for n in dir(mod) if not n.startswith("_")]
    print(f"顶层导出（前 40 个）：{', '.join(names[:40])}")

    print("\n== 候选入口方法 ==")
    for n in names:
        obj = getattr(mod, n)
        if inspect.isclass(obj):
            methods = [m for m in dir(obj) if not m.startswith("_")]
            hits = [m for m in methods if any(k in m.lower() for k in ("send", "msg", "listen", "recv", "start", "run", "friend", "room"))]
            if hits:
                print(f"  类 {n}: {', '.join(hits)}")
        elif callable(obj) and any(k in n.lower() for k in ("send", "listen", "recv", "start", "run")):
            print(f"  函数 {n}{inspect.signature(obj) if _safe_sig(obj) else ''}")

    if args.connect:
        print("\n== 尝试连接 ==")
        print("请按框架文档补上实例化与连接代码（不同版本差异大，脚本不替你猜）；")
        print("连接成功后，用 --emit-shim 生成 shim，把 API 接到本项目的 HTTP 协议上。")
    return 0


def _safe_sig(obj) -> bool:
    try:
        inspect.signature(obj)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
