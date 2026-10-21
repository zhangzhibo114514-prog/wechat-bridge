"""WeChatFerry（wcferry）适配器骨架。

现状说明（2026-10-15 实测本机）：
- 本机微信为 4.1.15.13（C:\\Program Files\\Tencent\\Weixin\\Weixin.exe），**不是** wcferry
  支持的 3.9.x 客户端，因此本文件目前只是“接口已对齐、等环境就绪即插”的占位实现。
- wcferry 官方 wheel 只到 Python 3.12，本机 Python 为 3.14.7，需另建 3.11/3.12 虚拟环境。

接通前必须满足（缺一不可）：
1. 目标客户端版本在 wcferry 支持表内（3.9.x），且与 SDK 的 sdk.dll 匹配；
2. Python 3.9–3.12 环境，pip install wcferry；
3. 这是**违反腾讯服务协议**的操作，用独立小号而非主力号（见 README 风险段）。

启用方式：bridge_config.json 里把 hook.adapter 改为
"wechat_bridge.adapters.hook_wcferry:WcferryHookAdapter"。
"""

from __future__ import annotations

import queue
from typing import Any, Optional

from .base import HookAdapter, HookStatus, Message


class WcferryHookAdapter(HookAdapter):
    def __init__(self, **_: Any) -> None:
        self._wx = None
        self._queue: "queue.Queue[Message]" = queue.Queue()
        self._bot_wxid = ""
        self._self_wxid = ""

    # ---------- 生命周期 ----------

    def connect(self) -> None:
        from wcferry import Wcf  # 延迟导入：没装 wcferry 时不影响其它适配器使用

        self._wx = Wcf()
        self._wx.enable_receiving(self._on_message)
        try:
            self._self_wxid = self._wx.get_self_wxid()
        except Exception as exc:
            # 拿不到自己的 wxid 只影响“群里是否 @ 我”的判断，不该拖垮整个连接
            print(f"[wcferry] 取自己 wxid 失败（群内 @ 判断将退化为按文本匹配）: {exc!r}")
            self._self_wxid = ""
        self._bot_wxid = self._self_wxid

    def close(self) -> None:
        if self._wx is not None:
            try:
                self._wx.cleanup()
            finally:
                self._wx = None

    # ---------- 收 ----------

    def _on_message(self, msg: Any) -> None:
        """wcferry 的回调：转成统一 Message 塞进队列，由 recv() 取。"""
        try:
            if msg.from_self():
                return
            self._queue.put(
                Message(
                    chat_id=msg.roomid if msg.from_group() else msg.sender,
                    sender_id=msg.sender,
                    sender_name=self._name_of(msg.sender),
                    text=msg.content,
                    is_group=msg.from_group(),
                    msg_id=str(msg.id),
                    ts=float(msg.ts),
                    is_self=False,
                    mentions_me=bool(self._bot_wxid and f"@{self._bot_wxid}" in (msg.content or "")),
                    raw=msg,
                )
            )
        except Exception as exc:  # 回调里抛异常会打死 SDK 线程，必须吞掉并留痕
            print(f"[wcferry] 解析消息失败: {exc!r}")

    def _name_of(self, wxid: str) -> str:
        try:
            detail = self._wx.get_user_info(wxid)  # 不同版本方法名可能不同，失败就退回 wxid
            return (detail.get("name") or wxid) if isinstance(detail, dict) else wxid
        except Exception:
            return wxid

    def recv(self) -> Optional[Message]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    # ---------- 发 ----------

    def send(self, chat_id: str, text: str, at: str = "") -> bool:
        if self._wx is None:
            return False
        if at:
            # WeChatFerry 的 @ 是正文里的占位符形式：先 @ 名字再跟内容。
            # 这条路线本机没在跑（wxhook 适配表最高 4.1.13.56），按文档实现、未真机验证。
            return self._wx.send_text(msg=f"@{at} {text}", receiver=chat_id) == 0
        return self._wx.send_text(msg=text, receiver=chat_id) == 0

    def status(self) -> HookStatus:
        if self._wx is None:
            return HookStatus(ok=False, detail="未连接")
        return HookStatus(ok=True, detail="wcferry 已连接", extra={"self": self._self_wxid})
