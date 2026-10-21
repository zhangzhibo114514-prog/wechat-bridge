# -*- coding: utf-8 -*-
"""KI-2 自测：同一个人连发多条 → **只回一次**。

背景（真机踩到的）：一人连发 3 条
    `@de 帮我看看` / `就是这个链接` / `在吗`
桥接会触发 1 次 @ 回复 + 2 次追问回复 = **回 3 条**，一次意思被拆成三轮。

这里要锁住四件事：
  1. 同一个人的连发**合并成一次模型调用**；
  2. 合并后的正文里**两句都在**（不能丢内容）；
  3. **别人**在窗口期发的消息**不丢**，进待处理队列，之后照常处理；
  4. 关掉防抖（`reply_debounce_sec=0`）时行为回到原样（一条一回）。

跑法：python debounce_selftest.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


def main() -> int:
    import bridge as B
    from wechat_bridge.adapters.base import Message
    from wechat_bridge.adapters.hook_mock import MockHookAdapter
    from wechat_bridge.config import BridgeConfig

    class FakeLLM:
        def __init__(self) -> None:
            self.prompts: list[list[dict]] = []

        def complete(self, messages):
            self.prompts.append(messages)
            return "收到"

    class QueueHook(MockHookAdapter):
        """`recv()` 按顺序吐出预置消息；吐完返回 None（模拟"暂时没有新消息"）。"""

        def __init__(self) -> None:
            super().__init__()
            self.inbox: list[Message] = []
            self.sent: list[str] = []

        def recv(self):
            return self.inbox.pop(0) if self.inbox else None

        def send(self, chat_id: str, text: str, at: str = "") -> bool:     # noqa: ARG002
            self.sent.append(text)
            return True

    def build(debounce: float, hook_factory=None):
        products = ROOT / "state" / "_debtests"
        shutil.rmtree(products, ignore_errors=True)
        cfg = BridgeConfig()
        cfg.policy.mode = "auto"
        cfg.policy.contact_rules = {"filehelper": "allow"}
        cfg.policy.max_reply_age_min = 0
        cfg.policy.quiet_hours = ()                     # 否则 23:00 之后跑这条自测会被静默时段拦
        cfg.policy.reply_delay_sec = (0.0, 0.0)
        cfg.policy.reply_debounce_sec = debounce
        cfg.segments.gap_sec = (0.0, 0.0)
        cfg.segments.enabled = False                    # 这条自测只看"合并"，不看分段
        cfg.storage.audit_path = "state/_debtests/audit.jsonl"
        cfg.storage.state_path = "state/_debtests/state.json"
        cfg.storage.draft_dir = "state/_debtests/drafts"
        bridge = B.Bridge(cfg, allow_real_llm=False)
        llm = FakeLLM()
        bridge.llm = llm
        hook = (hook_factory or QueueHook)()
        bridge.hook = hook
        return bridge, llm, hook

    def msg(text: str, sender: str = "wxid_a", mid: str = "m1") -> Message:
        return Message(chat_id="filehelper", sender_id=sender, sender_name=sender,
                       text=text, is_group=False, msg_id=f"filehelper:{mid}", ts=time.time())

    ok = True

    # 1) 防抖开启：同一人连发 3 条 → 只调用一次模型、只发一条
    bridge, llm, hook = build(debounce=0.7)
    bridge.hook.inbox = [msg("就是这个链接", "wxid_a", "m2"), msg("在吗", "wxid_a", "m3")]
    t0 = time.time()
    decision = bridge.route(msg("帮我看看", "wxid_a", "m1"))
    cost = time.time() - t0
    ok &= check("只调用一次模型", len(llm.prompts) == 1, f"调用 {len(llm.prompts)} 次")
    ok &= check("只发出一条回复", hook.sent == ["收到"], str(hook.sent))
    ok &= check("决策是 reply", decision.action == "reply", f"{decision.action}/{decision.reason}")
    merged = str(llm.prompts[0]) if llm.prompts else ""
    ok &= check("合并后三句都在（内容不丢）",
                all(s in merged for s in ("帮我看看", "就是这个链接", "在吗")), merged[:160])
    ok &= check("确实等了防抖窗口（≥0.6s）", cost >= 0.6, f"{cost:.2f}s")
    ok &= check("等待期间没有多余的消息积压", len(bridge._pending) == 0,  # noqa: SLF001
                str(list(bridge._pending)))                              # noqa: SLF001

    # 1b) 窗口是**滑动**的：他在窗口内又说了一句，就重新计时。
    #     用户反馈"我发两条它就回两条，我打字慢了"——期望就是等他把话说完。
    class SlowHook(QueueHook):
        """`recv()` 先睡一会儿再吐消息，模拟"人正在一个字一个字打"。"""

        def __init__(self) -> None:
            super().__init__()
            self.delays: list[float] = []

        def recv(self):
            if self.inbox and self.delays:
                time.sleep(self.delays.pop(0))
                return self.inbox.pop(0)
            return None

    bridge, llm, hook = build(debounce=0.5, hook_factory=SlowHook)
    hook.delays = [0.3]                                  # 第二句在 0.3s 后才到
    bridge.hook.inbox = [msg("就是这个链接", "wxid_a", "m2")]
    t0 = time.time()
    bridge.route(msg("帮我看看", "wxid_a", "m1"))
    cost = time.time() - t0
    ok &= check("窗口内又来一句 → 重新计时（不是到点就走）", cost >= 0.7, f"{cost:.2f}s")
    ok &= check("滑动窗口也把第二句并了进去",
                "就是这个链接" in str(llm.prompts[0] if llm.prompts else ""))

    # 1c) 硬上限：一直有人连发，也不能无限期拖下去。
    #     `reply_debounce_max_sec=0` 才是不设上限；这里是 0.8s 到点就回。
    bridge, llm, hook = build(debounce=0.5, hook_factory=SlowHook)
    bridge.cfg.policy.reply_debounce_max_sec = 0.8       # noqa: SLF001
    hook.delays = [0.3, 0.3, 0.3]                        # 三句一句比一句晚
    bridge.hook.inbox = [msg("第二句", "wxid_a", "m2"),
                         msg("第三句", "wxid_a", "m3"),
                         msg("第四句", "wxid_a", "m4")]
    t0 = time.time()
    bridge.route(msg("第一句", "wxid_a", "m1"))
    cost = time.time() - t0
    ok &= check("到硬上限就发，不无限等", cost < 1.4, f"{cost:.2f}s")
    ok &= check("硬上限之前到达的消息都并进来了",
                "第二句" in str(llm.prompts[0] if llm.prompts else ""))

    # 2) 窗口期别人插话 → 不并进来、也不丢（进待处理队列）
    bridge, llm, hook = build(debounce=0.7)
    bridge.hook.inbox = [msg("我先说一句", "wxid_b", "m9")]
    bridge.route(msg("你好", "wxid_a", "m1"))
    merged = str(llm.prompts[0]) if llm.prompts else ""
    ok &= check("别人的话没有被并进这次回复", "我先说一句" not in merged, merged[:160])
    ok &= check("别人的消息没丢（进了待处理队列）",
                len(bridge._pending) == 1                                   # noqa: SLF001
                and bridge._pending[0].sender_id == "wxid_b",                # noqa: SLF001
                str([m.sender_id for m in bridge._pending]))                 # noqa: SLF001

    # 3) 关掉防抖 → 回到"一条一回"
    bridge, llm, hook = build(debounce=0)
    bridge.hook.inbox = [msg("第二句", "wxid_a", "m2")]
    bridge.route(msg("第一句", "wxid_a", "m1"))
    ok &= check("防抖=0 时只处理当前这一条", len(llm.prompts) == 1, f"调用 {len(llm.prompts)} 次")
    ok &= check("防抖=0 时不吃掉后面的消息",
                len(bridge._pending) == 0                                   # noqa: SLF001
                and len(bridge.hook.inbox) == 1,                            # noqa: SLF001
                str(list(bridge._pending)))                                  # noqa: SLF001

    # 4) 群聊提示词必须说清"这是群、最后一条是要回的那条、别人的插话算语境"。
    #    真机踩到过：第三个人插话、用户回应之后，模型仍当一对一，只接最后一句。
    bridge, llm, hook = build(debounce=0)
    bridge.group_scheduler.observe("g1@chatroom", "wxid_a", "@我 在吗", at_me=True)
    bridge.group_scheduler.observe("g1@chatroom", "wxid_c", "我也插一句", at_me=False)
    gmsg = Message(chat_id="g1@chatroom", sender_id="wxid_a", sender_name="甲",
                   text="你看他说的", is_group=True, msg_id="g1:2", ts=time.time())
    blob = str(bridge._build_prompt(gmsg))                          # noqa: SLF001
    ok &= check("群提示词标明「最后一条是要回应的那条」",
                "最后一条就是现在要你回应的那条" in blob)
    ok &= check("群提示词提醒「群里不止你们两个人」", "不止你们两个人" in blob)
    ok &= check("别人的插话确实进了语境", "我也插一句" in blob)

    shutil.rmtree(ROOT / "state" / "_debtests", ignore_errors=True)
    print(f"\n结论：{'KI-2 防抖合并自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
