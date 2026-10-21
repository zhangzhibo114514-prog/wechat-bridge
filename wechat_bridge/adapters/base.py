"""接口定义：hook 适配器与 LLM 客户端。

设计约定（改动前先读）：
- hook 侧收消息用 `recv()` **轮询**，不依赖回调，这样不同 hook 库（wcferry 的 socket 回调、
  UI 自动化轮询、协议库长连接）都能包成同一形状；`worker()` 是留给支持回调的实现的钩子。
- `send()` 只负责“把文本发出去”，限速、熔断、留档都在 bridge 里做，适配器保持薄。
- 每个收发的 Message 都带 `raw`，便于出问题时回查原始结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

MessageHandler = Callable[["Message"], None]


@dataclass
class Message:
    """一条收到的消息。"""

    chat_id: str                 # 会话 id：私聊=对方 wxid，群聊=群 id
    sender_id: str               # 发送者 id：私聊=本人 wxid，群聊=群成员 wxid
    sender_name: str             # 发送者昵称（用于放行名单匹配）
    text: str                    # 文本内容
    is_group: bool = False
    kind: str = "text"           # text | image —— 图片消息留到"决定要回"之后才去取图（省得白取）
    msg_id: str = ""
    # 消息在本机数据库里的行号（取图/取语音/取文件都要它）。
    # 单独一个字段而不是让适配器从 `raw` 里摸：两种 raw 形状（库的 Message 对象 /
    # 监听回调的原始行 dict）里有一路不一定带它，取不到就静默失败——真机踩过。
    local_id: Any = None
    ts: float = 0.0
    is_self: bool = False        # 自己发的（必须过滤，否则自问自答死循环）
    mentions_me: bool = False    # 群里是否 @ 了我
    at_others: bool = False      # 群里这条 @ 的是**别人**（不是我、也不是 @所有人）
    at_note: str = ""            # @ 判定的依据（人话，进审计；排查"它为什么没理我"全靠它）
    # 「引用」的解析结果（见 `hook_wechatauto.parse_quote`）：
    # {text, quoted_from, quoted_name, quoted_text}；不是引用就是 None。
    # 为什么不能只把 `[引用 谁：原话]` 拼进正文：判定层要区分
    # "引用了**我**"（＝在跟我说话，必须回）和 "引用了**别人**"（＝别人之间的对话，
    # 不该插嘴）——这两件事在纯文本里分不清，只能留一份结构化的。
    quote: Optional[dict] = None
    raw: Any = None


@dataclass
class Decision:
    """一次收到的消息该怎么处理。"""

    action: str                  # reply | draft | ignore | error
    reason: str
    reply_text: str = ""
    run_id: str = ""


@dataclass
class HookStatus:
    ok: bool = True
    detail: str = ""
    frozen: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class HookAdapter:
    """hook 适配器必须实现的接口。"""

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def recv(self) -> Optional[Message]:
        """非阻塞取一条消息；没有则返回 None。"""
        raise NotImplementedError

    def send(self, chat_id: str, text: str, at: str = "") -> bool:
        """发一条消息。``at`` 非空时在群里 @ 对方（不是所有后端都支持，默认忽略）。"""
        raise NotImplementedError

    def set_extra_nicknames(self, names: Optional[list[str]]) -> None:
        """补一批"我在群里可能被叫到的名字"（群昵称、别名）。

        默认**什么都不做**——只有真正做 @ 检测的适配器才需要它。
        为什么要有这个入口：@ 检测认的是**昵称**（这个客户端不吐 `<atuserlist>`），
        而账号昵称 ≠ 群昵称。群里 @ 人时微信插的是**群昵称**，
        所以只认账号昵称会漏判——`group.my_nicknames` 就是给这种情况留的。
        """
        return None

    def status(self) -> HookStatus:
        return HookStatus()

    def identity(self) -> tuple[str, list[str]]:
        """本账号身份：``(wxid, 我在群里可能被叫到的名字)``。

        桥接层用它做群聊 @ 检测的兜底——**适配器最清楚自己是谁**，
        配置里再写一遍容易漏、也容易过期。
        """
        return "", []

    def worker(self, on_message: MessageHandler) -> None:
        """可选：实现推送式收消息。默认置空，交给主循环轮询 recv()。"""
        return None


class LLMClient:
    """LLM 客户端接口：输入消息列表，输出一条回复文本。"""

    def complete(self, messages: list[dict[str, str]]) -> str:
        raise NotImplementedError
