"""通用 HTTP/本地服务适配器：把“任何 hook 后端”接进来，不写死某一家的 SDK。

为什么要有它：4.x 的 hook 框架五花八门（有的给 Python SDK、有的给本地 HTTP 服务、有的给 C# 程序），
每次都改桥接代码不划算。约定一个极简协议，让后端来适配我们：

后端需要提供两个 HTTP 接口（默认 http://127.0.0.1:5555）：
    GET  /messages          返回 {"messages": [ {chat_id, sender_id, sender_name, text,
                              is_group, msg_id, ts, is_self, mentions_me}, ... ]}
                            调用后即视为“已投递”，后端应清空自己的队列（拉模式）。
    POST /send              请求体 {"chat_id": "...", "text": "..."}，返回 {"ok": true}
    GET  /health            可选，返回 {"ok": true} 供 status() 使用

配置示例（bridge_config.json）：
    "hook": {
      "adapter": "wechat_bridge.adapters.hook_http:HttpHookAdapter",
      "options": {"base_url": "http://127.0.0.1:5555", "timeout_sec": 10}
    }

自测：本目录的 hook_fake_server.py 就是这个协议的最小实现，可用来在真 hook 到位前
把「消息进来 → 决策 → 回复出去」整条链路跑通（另开一个终端跑 fake server 即可）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .base import HookAdapter, HookStatus, Message


class HttpHookAdapter(HookAdapter):
    def __init__(self, base_url: str = "http://127.0.0.1:5555", timeout_sec: float = 10.0, poll_sec: float = 0.3, **_: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.poll_sec = poll_sec
        self._buffer: list[Message] = []
        self._last_poll = 0.0
        self._lock = threading.Lock()
        self._consecutive_errors = 0

    # ---------- 生命周期 ----------

    def connect(self) -> None:
        health = self._request("GET", "/health", required=False)
        if health is None:
            print(f"[http-hook] {self.base_url}/health 无响应，仍继续（后端可能没实现该接口）")

    def close(self) -> None:
        return None

    # ---------- 收 ----------

    def recv(self) -> Optional[Message]:
        with self._lock:
            if self._buffer:
                return self._buffer.pop(0)
            now = time.time()
            if now - self._last_poll < self.poll_sec:
                return None
            self._last_poll = now
        data = self._request("GET", "/messages")
        if data is None:
            return None
        raw_items = data.get("messages") or []
        with self._lock:
            self._buffer.extend(self._to_message(item) for item in raw_items)
            return self._buffer.pop(0) if self._buffer else None

    @staticmethod
    def _to_message(item: dict) -> Message:
        return Message(
            chat_id=str(item.get("chat_id", "")),
            sender_id=str(item.get("sender_id", "")),
            sender_name=str(item.get("sender_name") or item.get("sender_id") or ""),
            text=str(item.get("text", "")),
            is_group=bool(item.get("is_group", False)),
            msg_id=str(item.get("msg_id", "")),
            ts=float(item.get("ts") or time.time()),
            is_self=bool(item.get("is_self", False)),
            mentions_me=bool(item.get("mentions_me", False)),
            raw=item,
        )

    # ---------- 发 ----------

    def send(self, chat_id: str, text: str, at: str = "") -> bool:
        body = {"chat_id": chat_id, "text": text}
        if at:
            body["at"] = at                     # 后端认不认这个字段由它自己决定（协议里是可选）
        resp = self._request("POST", "/send", body)
        return bool(resp and resp.get("ok", True))

    def status(self) -> HookStatus:
        data = self._request("GET", "/health", required=False)
        if data is None:
            return HookStatus(ok=False, detail=f"{self.base_url} 无响应（连续失败 {self._consecutive_errors} 次）")
        return HookStatus(ok=True, detail=f"{self.base_url} 正常", extra=data)

    # ---------- 内部 ----------

    def _request(self, method: str, path: str, body: dict | None = None, required: bool = True) -> Optional[dict]:
        url = self.base_url + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                payload = resp.read().decode("utf-8", "replace")
            self._consecutive_errors = 0
            return json.loads(payload) if payload.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            self._consecutive_errors += 1
            print(f"[http-hook] {method} {path} HTTP {exc.code}: {detail}")
        except Exception as exc:
            self._consecutive_errors += 1
            if required:
                print(f"[http-hook] {method} {path} 失败（第 {self._consecutive_errors} 次）: {exc!r}")
        return None
