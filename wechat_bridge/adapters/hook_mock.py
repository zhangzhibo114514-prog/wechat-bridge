"""演示/自测用的假 hook：从 jsonl 文件逐行读消息，send() 只打印。

用途：在没装任何 hook SDK、也没接通微信时，验证桥接的决策链路（放行、限速、熔断、
留档、草稿）是否按预期工作。`bridge.py --demo` 就是用它。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .base import HookAdapter, HookStatus, Message


class MockHookAdapter(HookAdapter):
    def __init__(self, demo_file: str = "demo_chat.jsonl", replay_speed: float = 0.4, **_: Any) -> None:
        self.demo_file = Path(demo_file)
        self.replay_speed = replay_speed
        self._lines: list[dict[str, Any]] = []
        self._idx = 0
        self._lock = threading.Lock()
        self.sent: list[tuple[str, str]] = []

    def connect(self) -> None:
        if not self.demo_file.exists():
            raise FileNotFoundError(f"演示数据不存在: {self.demo_file}")
        for line in self.demo_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                self._lines.append(json.loads(line))

    def close(self) -> None:
        return None

    def recv(self) -> Optional[Message]:
        with self._lock:
            if self._idx >= len(self._lines):
                return None
            item = self._lines[self._idx]
            self._idx += 1
        time.sleep(self.replay_speed)  # 模拟消息之间的间隔
        return Message(
            chat_id=item.get("chat_id", ""),
            sender_id=item.get("sender_id", ""),
            sender_name=item.get("sender_name", ""),
            text=item.get("text", ""),
            is_group=bool(item.get("is_group", False)),
            msg_id=item.get("msg_id", f"demo-{self._idx}"),
            ts=time.time(),
            is_self=bool(item.get("is_self", False)),
            mentions_me=bool(item.get("mentions_me", False)),
            raw=item,
        )

    def send(self, chat_id: str, text: str, at: str = "") -> bool:
        self.sent.append((chat_id, text))
        print(f"[MOCK SEND] -> {chat_id}: " + (f"@{at} " if at else "") + text)
        return True

    def status(self) -> HookStatus:
        return HookStatus(ok=True, detail="mock", extra={"sent": len(self.sent)})
