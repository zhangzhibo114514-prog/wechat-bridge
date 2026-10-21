"""群聊状态机：决定一条群消息**该不该唤醒模型**。

## 这个模块解决什么

群聊里最贵的动作只有一个——**把消息送进大模型**。收消息、写本地缓冲全是磁盘操作，
不花钱。所以"省 token"的正确做法不是少读消息，而是**在该醒的时候才醒**。

状态机每个群一份，只有两个状态：

    IDLE    休眠：消息只进环形缓冲，**永不调用模型**
    ACTIVE  激活：消息进上下文；满足"回应判据"时才调用模型

## 判据（为什么这么定）

**激活**：被 @ 我（或 @所有人）。这是唯一的入口——群里没人叫你，你就不该开口。

**续期**：只有"跟我有关"的消息才续命（@我 / 我发完话后的追问）。
**故意不把"群里其他人闲聊"算作活动**，两个理由：
  1. 热闹的群里如果"有消息就续期"，AI 会永远醒着，token 白烧；
  2. 更要紧的是——别人正聊得起劲时它突然插话，是最像机器人、最招人烦的行为。

**休眠**：`ACTIVE` 且距最后一次"与我有关"超过 `idle_timeout_sec`。

**激活 ≠ 每条都回**：即使醒着，也只有
  - 被 @ 我，或
  - 我说完话后 `followup_window_sec` 内**刚才那个人**接话
才触发模型调用。其余消息**只记进缓冲**（免费），不回。

**为什么追问要认人**（`followup_same_sender_only`）：判据如果只看时间不看人，
那么甲 @ 我一次之后，90 秒内乙丙丁之间的**相互闲聊**也会被当成"在跟我说话"，
我会一句句接上去——正是"在不该插嘴的地方插话"。所以只接"刚才跟我说话的那个人"，
且 `sender_id` 解不出来时按**不接**处理（宁可少接一句，也不在分不清对象时插嘴）。

**防自我循环**：连续 `max_chain_turns` 轮"没有 @ 的接话"之后就不再接，
必须等下一次 @。否则出现"AI 说一句 → 群里有人随口应一声 → AI 又说一句"的无限循环。

## 用法

    sched = GroupScheduler(GroupPolicy(idle_timeout_sec=300))
    action = sched.observe(chat_id, sender_id, text, at_me=result.matched)
    if action == RESPOND:
        reply = llm.complete(sched.build_prompt(chat_id, ...))
        sched.note_bot_spoke(chat_id)
    ...
    for chat_id in sched.tick():          # 该休眠的群
        log(f"{chat_id} 已休眠")
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional

IDLE = "IDLE"
ACTIVE = "ACTIVE"

RESPOND = "respond"
IGNORE = "ignore"


@dataclass
class GroupPolicy:
    """群聊行为的全部可调参数。默认值偏保守。"""

    # 距最后一次"与我有关"多久之后回到休眠（秒）
    idle_timeout_sec: float = 300.0
    # 我发完话之后，多久之内的接话算"追问"
    followup_window_sec: float = 90.0
    # 每个群保留多少条上下文（环形缓冲，休眠期也在写）
    ring_size: int = 30
    # 是否允许主动插话（**默认关**：群里主动搭话最容易被举报）
    proactive: bool = False
    # 连续多少轮"没有 @ 的接话"之后就不再接（防自我循环）
    max_chain_turns: int = 10
    # **追问只接"刚才跟我说话的那个人"**（默认开）。
    #
    # 为什么必须开：追问窗口原来的判据**只看时间、不看人**——我发完话 90 秒内，
    # 群里**任何人**说话都算"接话"，于是甲 @ 我一次之后，乙丙丁的相互闲聊
    # 我也会一句句接上去。在一个活跃群里，那就是"回一句之后开始对每个人点头"，
    # 是最典型的"在不该插嘴的地方插嘴"。
    #
    # 关掉（False）就退回旧行为：窗口内任何人的话都接——只有在"群里只有你和我
    # 在说话"这种小群里才合适。
    followup_same_sender_only: bool = True
    # 新群的观察期：前 N 条消息只观察不回（0 = 关闭）
    # 用来避免刚进一个工作群/客户群就开始说话
    observe_first_msgs: int = 0
    # 一次响应最多发几段（拟人节奏）
    max_segments: int = 3
    # 段与段之间的随机间隔（秒）
    reply_delay_sec: tuple[float, float] = (1.5, 4.0)
    # 被 @ 时回复要不要 @ 回去
    at_back: bool = True
    # 最多同时跟踪多少个群（防止长期运行内存无限增长）
    max_chats: int = 200


@dataclass
class _ChatState:
    state: str = IDLE
    ring: Deque[dict] = field(default_factory=lambda: deque(maxlen=30))
    last_ping_ts: float = 0.0        # 最后一次 @我
    last_bot_ts: float = 0.0         # 我最后一次发言
    last_followup_ts: float = 0.0    # 最后一次"追问"类接话
    chain: int = 0                   # 连续"没有 @"的接话轮数
    last_speaker: str = ""           # 最近一次"跟我说话的人"的 sender_id（追问只认他）
    seen: int = 0                    # 观察到的消息总数
    activated_at: float = 0.0
    last_active_ts: float = 0.0      # 最后一次任何活动（仅用于报告）
    # 这条消息被忽略的**真实原因**（给日志用）。原先日志只会说"不是在叫我"，
    # 于是链长用尽、追问的人不是他、@ 了别人……全都被显示成"AI 不认人"，
    # 排查时被误导了整整一轮。见 `skip_reason()`。
    skip_reason: str = ""


class GroupScheduler:
    """每个群一份状态，纯逻辑、无 IO、无时间依赖（时钟可注入，便于测试）。"""

    def __init__(self, policy: Optional[GroupPolicy] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.policy = policy or GroupPolicy()
        self._clock = clock
        self._chats: "OrderedDict[str, _ChatState]" = OrderedDict()

    # ---------------- 对外 ----------------

    def observe(self, chat_id: str, sender_id: str, text: str,
                at_me: bool = False, is_self: bool = False,
                at_others: bool = False,
                ts: Optional[float] = None) -> str:
        """喂一条群消息，返回该做什么：`RESPOND` 或 `IGNORE`。

        `at_others=True` 表示这条消息里的 @ 是**冲着别人**去的（不是我、也不是 @所有人）
        —— 那是在跟别人说话，见下面追问分支里的说明。

        **所有消息都会进环形缓冲**（包括我自己的和别人的闲聊）——
        它们不触发模型调用，但被 @ 时要靠它们回溯上下文，AI 才不会一脸茫然。
        """
        now = self._now(ts)
        st = self._chat(chat_id)
        st.seen += 1
        st.last_active_ts = now
        st.ring.append({
            "ts": now,
            "sender": sender_id,
            "text": text,
            "self": bool(is_self),
        })

        if is_self:
            # 自己发的：进缓冲、续命，但绝不触发回应（防自问自答死循环）
            st.last_bot_ts = now
            st.skip_reason = "self"
            return IGNORE

        # 观察期：新群里前 N 条一律不回（**连 @ 也不回**——这正是观察期的意义）
        if st.seen <= self.policy.observe_first_msgs:
            st.skip_reason = "observe"
            return IGNORE

        if at_me:
            st.state = ACTIVE
            st.last_ping_ts = now
            st.chain = 0                      # 被点名 = 重新开始，链条清零
            st.last_speaker = sender_id       # 现在"跟我说话的人"换成了他
            st.activated_at = st.activated_at or now
            st.skip_reason = ""
            return RESPOND

        if st.state != ACTIVE:
            st.skip_reason = "idle"
            return IGNORE

        # **在叫别人 ⇒ 不接**（连"主动插话"也不接）。
        # 实测踩到过：甲（正在跟我对话的人）说了句 `@张三 这事跟你有什么关系`，
        # 我把它当成"甲在追问"接了一句"你俩的事你俩聊哈"——话说得得体，但**我本不该开口**。
        # 判定在调用方做（`at_detect.addresses_other`），这里只负责"别人被点名时闭嘴"。
        if at_others:
            st.skip_reason = "at_others"
            return IGNORE

        if self.policy.proactive:
            st.last_followup_ts = now
            st.skip_reason = ""
            return RESPOND

        # 追问窗口：我刚说完话，紧接着**刚才那个人**接话。
        # 判据必须同时满足"时间在窗口内"和"是同一个人在跟我说话"——
        # 只看时间的话，群里其他人之间的闲聊也会被当成在跟我说话（见 policy 里的说明）。
        if st.last_bot_ts and (now - st.last_bot_ts) <= self.policy.followup_window_sec:
            if self.policy.followup_same_sender_only:
                # sender_id 解不出来时**按不接处理**：宁可少接一句，也不要在
                # 分不清是谁在说话的情况下插嘴。
                if not sender_id or sender_id != st.last_speaker:
                    st.skip_reason = "followup_other_sender"
                    return IGNORE
            # 防自我循环：连续太多轮"没有 @ 的接话"之后就不再接，等下一次 @。
            # **默认 10 轮、设 0 表示不限制**：真人对话本来就是"你一句我一句"，
            # 3 轮太短——实测用户聊到第三句机器人就装死了（"多说几句就不理人"）。
            # 真正防自循环靠的是 `followup_same_sender_only` + 窗口时间，
            # 这条只是极端情况下的保险丝。
            if self.policy.max_chain_turns > 0 and st.chain >= self.policy.max_chain_turns:
                st.skip_reason = "chain_limit"
                return IGNORE                 # 防自我循环：等下一次 @
            st.chain += 1
            st.last_followup_ts = now
            st.skip_reason = ""
            return RESPOND

        st.skip_reason = "window_expired"
        return IGNORE

    def last_speaker(self, chat_id: str) -> str:
        """最近一次"跟我说话的人"（被 @ 的那个人）。

        给说话判定层用：群醒着的时候，**别人**说话不该接——判据就是"这条的发送者
        是不是刚才跟我对话的人"。空字符串表示"还没有人跟我说话"。
        """
        st = self._chats.get(str(chat_id))
        return st.last_speaker if st else ""

    def skip_reason(self, chat_id: str) -> str:
        """最近这条被忽略的原因码：self / observe / idle / at_others /
        followup_other_sender / chain_limit / window_expired（空 = 不是忽略）。"""
        st = self._chats.get(str(chat_id))
        return st.skip_reason if st else ""

    def note_bot_spoke(self, chat_id: str, ts: Optional[float] = None) -> None:
        """我发出了一条消息。**必须在真正发出之后调用**，否则追问窗口的判据是错的。"""
        now = self._now(ts)
        st = self._chat(chat_id)
        st.last_bot_ts = now
        st.last_active_ts = now
        st.state = ACTIVE                         # 我说过话 = 这个群是醒的
        st.activated_at = st.activated_at or now

    def tick(self, ts: Optional[float] = None) -> list[str]:
        """推进时间：把静默超时的群退回休眠。返回**本次刚休眠**的群 id。"""
        now = self._now(ts)
        went_idle: list[str] = []
        for chat_id, st in self._chats.items():
            if st.state != ACTIVE:
                continue
            last_relevant = max(st.last_ping_ts, st.last_bot_ts, st.last_followup_ts)
            if now - last_relevant > self.policy.idle_timeout_sec:
                st.state = IDLE
                st.chain = 0
                st.activated_at = 0.0
                went_idle.append(chat_id)
        return went_idle

    def context(self, chat_id: str, limit: Optional[int] = None) -> list[dict]:
        """这个群最近的上下文（环形缓冲），供被 @ 时回溯。"""
        st = self._chats.get(chat_id)
        if not st:
            return []
        items = list(st.ring)
        return items[-limit:] if limit else items

    def build_prompt_lines(self, chat_id: str, limit: Optional[int] = None,
                           name_of: Optional[Callable[[str], str]] = None) -> list[str]:
        """把上下文渲染成提示词里的几行（`昵称：内容`）。"""
        lines = []
        for item in self.context(chat_id, limit):
            who = "我" if item.get("self") else (
                name_of(item.get("sender", "")) if name_of else item.get("sender", "")
            )
            lines.append(f"{who}：{item.get('text', '')}")
        return lines

    def state_of(self, chat_id: str) -> str:
        st = self._chats.get(chat_id)
        return st.state if st else IDLE

    def active_chats(self) -> list[str]:
        return [cid for cid, st in self._chats.items() if st.state == ACTIVE]

    def reset(self, chat_id: Optional[str] = None) -> None:
        if chat_id is None:
            self._chats.clear()
        else:
            self._chats.pop(chat_id, None)

    def snapshot(self) -> dict[str, Any]:
        """给 `status --json` / 日报用的只读快照。"""
        now = self._now(None)
        return {
            "policy": {
                "idle_timeout_sec": self.policy.idle_timeout_sec,
                "followup_window_sec": self.policy.followup_window_sec,
                "ring_size": self.policy.ring_size,
                "proactive": self.policy.proactive,
                "max_chain_turns": self.policy.max_chain_turns,
            },
            "chats": {
                cid: {
                    "state": st.state,
                    "seen": st.seen,
                    "ring": len(st.ring),
                    "chain": st.chain,
                    "idle_for_sec": round(now - max(st.last_ping_ts, st.last_bot_ts,
                                                    st.last_followup_ts), 1)
                    if st.state == ACTIVE else None,
                }
                for cid, st in self._chats.items()
            },
        }

    # ---------------- 内部 ----------------

    def _now(self, ts: Optional[float]) -> float:
        return self._clock() if ts is None else float(ts)

    def _chat(self, chat_id: str) -> _ChatState:
        st = self._chats.get(chat_id)
        if st is None:
            st = _ChatState()
            st.ring = deque(maxlen=max(1, int(self.policy.ring_size)))
            self._chats[chat_id] = st
        self._chats.move_to_end(chat_id)          # LRU：常用的排后面
        while len(self._chats) > max(1, int(self.policy.max_chats)):
            self._chats.popitem(last=False)       # 淘汰最久没动的群
        return st
