# -*- coding: utf-8 -*-
"""情景交融自测：**看见全场 → 冷场后自己决定要不要插一句**。

要锁住的事：
  1. 没被 @ 的群消息也会被"看见"（进环形缓冲），并安排一次"到时候看看要不要开口"；
  2. 到点时如果又有人说话 ⇒ 这次不插（不抢话）；
  3. 模型说 `<SILENT>` ⇒ 一个字都不发；
  4. 模型给了话 ⇒ 发出去，且**不 @ 人**（主动插话 @ 人很突兀）；
  5. 每小时预算、说完之后的冷却期都真实生效；
  6. 开关关掉时完全不安排。

跑法：python proactive_selftest.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "state" / "_proactive_tests"
GROUP = "12345@chatroom"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


def main() -> int:
    import bridge as B
    from wechat_bridge.adapters.base import Message
    from wechat_bridge.adapters.hook_mock import MockHookAdapter
    from wechat_bridge.config import BridgeConfig

    class Hook(MockHookAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[tuple[str, str, str]] = []

        def recv(self):
            return None

        def send(self, chat_id: str, text: str, at: str = "") -> bool:
            self.sent.append((chat_id, text, at))
            return True

    class LLM:
        def __init__(self, replies) -> None:
            self.replies = list(replies)
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            return self.replies.pop(0) if self.replies else "<SILENT>"

    def build(proactive=True, budget=2, cooldown=0.0, silence=0.0, min_history=1):
        shutil.rmtree(TMP, ignore_errors=True)
        cfg = BridgeConfig()
        cfg.policy.mode = "auto"
        cfg.policy.allow_groups = True
        cfg.policy.allow_any_group = True
        cfg.policy.contact_rules = {GROUP: "allow"}
        cfg.policy.max_reply_age_min = 0
        cfg.policy.quiet_hours = ()
        cfg.policy.reply_delay_sec = (0.0, 0.0)
        cfg.policy.reply_debounce_sec = 0
        cfg.group.proactive = proactive
        cfg.group.proactive_budget_per_hour = budget
        cfg.group.proactive_cooldown_sec = cooldown
        cfg.group.proactive_silence_sec = silence
        cfg.group.proactive_min_history = min_history
        cfg.group.observe_first_msgs = 0
        cfg.group.at_back = True
        cfg.segments.enabled = False
        cfg.memory.enabled = False
        cfg.storage.audit_path = "state/_proactive_tests/audit.jsonl"
        cfg.storage.state_path = "state/_proactive_tests/state.json"
        cfg.storage.draft_dir = "state/_proactive_tests/drafts"
        br = B.Bridge(cfg, allow_real_llm=False)
        hook = Hook()
        br.hook = hook
        return br, hook

    def msg(text: str, sender: str = "wxid_a", mid: str = "m1") -> Message:
        return Message(chat_id=GROUP, sender_id=sender, sender_name=f"人{sender[-1]}",
                       text=text, is_group=True, msg_id=f"{GROUP}:{mid}", ts=time.time())

    def audit() -> list[dict]:
        path = TMP / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]

    ok = True

    # 1) 看见全场 + 到点插一句（且不 @ 人）
    br, hook = build()
    br.llm = LLM(["这话题我有印象，上次也聊过"])
    for i, text in enumerate(["今天真冷", "是啊，出门都得穿厚的", "听说周末要下雪"], 1):
        d = br.route(msg(text, "wxid_a", f"m{i}"))
        ok &= check(f"第 {i} 条没被 @ 的群消息被忽略但记进了队列",
                    d.action == "ignore" and GROUP in br._proactive_due,  # noqa: SLF001
                    f"{d.action} due={list(br._proactive_due)}")          # noqa: SLF001
    br._group_last_ts[GROUP] = 0                                          # noqa: SLF001 模拟"真的冷场了"
    br._maybe_proactive_speak()                                           # noqa: SLF001
    ok &= check("到点后插了一句", len(hook.sent) == 1, str(hook.sent))
    ok &= check("插话时**不 @ 人**", hook.sent and hook.sent[0][2] == "", str(hook.sent))
    ok &= check("审计里记了 proactive_speak",
                any(r.get("event") == "proactive_speak" for r in audit()))

    # 2) 冷场条件不满足（还有人刚说话）⇒ 不插
    br, hook = build()
    br.llm = LLM(["这句不该发出去"])
    br.route(msg("随便说一句", "wxid_a", "m1"))
    br._group_last_ts[GROUP] = time.time()                                # noqa: SLF001 刚有人说话
    br.cfg.group.proactive_silence_sec = 60.0                             # noqa: SLF001
    br._maybe_proactive_speak()                                           # noqa: SLF001
    ok &= check("没冷场就不插话", hook.sent == [], str(hook.sent))

    # 3) 模型说 <SILENT> ⇒ 一个字都不发
    br, hook = build()
    br.llm = LLM(["<SILENT>"])
    br.route(msg("他们在聊别人的事", "wxid_a", "m1"))
    br._group_last_ts[GROUP] = 0                                          # noqa: SLF001
    br._maybe_proactive_speak()                                           # noqa: SLF001
    ok &= check("模型选择闭嘴时真的没发", hook.sent == [], str(hook.sent))
    ok &= check("闭嘴也留痕（proactive_skip/decided_silent）",
                any(r.get("event") == "proactive_skip" and r.get("reason") == "decided_silent"
                    for r in audit()))

    # 4) 预算：每小时只允许 1 次
    br, hook = build(budget=1, cooldown=0.0)
    br.llm = LLM(["第一次", "第二次不该发"])
    br.route(msg("聊点啥", "wxid_a", "m1"))
    br._group_last_ts[GROUP] = 0                                          # noqa: SLF001
    br._maybe_proactive_speak()                                           # noqa: SLF001
    br._proactive_due[GROUP] = 0                                          # noqa: SLF001 再排一次
    br._group_last_ts[GROUP] = 0                                          # noqa: SLF001
    br._maybe_proactive_speak()                                           # noqa: SLF001
    ok &= check("预算用完后不再开口", len(hook.sent) == 1, str(hook.sent))
    ok &= check("预算拦截有留痕（reason=budget）",
                any(r.get("event") == "proactive_skip" and r.get("reason") == "budget"
                    for r in audit()))

    # 5) 冷却：自己刚说完，短时间内不再主动开口
    br, hook = build(budget=5, cooldown=999.0)
    br.llm = LLM(["我先说一句"])
    br.route(msg("聊点啥", "wxid_a", "m1"))
    br._group_last_ts[GROUP] = 0                                          # noqa: SLF001
    br._maybe_proactive_speak()                                           # noqa: SLF001
    br._proactive_due[GROUP] = 0                                          # noqa: SLF001
    br._group_last_ts[GROUP] = 0                                          # noqa: SLF001
    br._maybe_proactive_speak()                                           # noqa: SLF001
    ok &= check("冷却期内不再开口", len(hook.sent) == 1, str(hook.sent))
    ok &= check("冷却拦截有留痕（reason=cooldown）",
                any(r.get("event") == "proactive_skip" and r.get("reason") == "cooldown"
                    for r in audit()))

    # 6) 开关关掉 ⇒ 完全不安排
    br, hook = build(proactive=False)
    br.llm = LLM(["不该出现"])
    br.route(msg("随便聊聊", "wxid_a", "m1"))
    ok &= check("关闭时不会安排插话", br._proactive_due == {}, str(br._proactive_due))  # noqa: SLF001

    # 7) @ 别人的消息不安排（不该插嘴）
    br, hook = build()
    br.llm = LLM(["不该出现"])
    m = msg("@张三 你来一下", "wxid_a", "m1")
    m.mentions_me = False
    m.at_others = True
    br.route(m)
    ok &= check("「@别人」的消息不安排插话", br._proactive_due == {}, str(br._proactive_due))  # noqa: SLF001

    # 8) **看见全场**：两个不同的人说的话都要进提示词（这是"情景交融"的前提）
    br, hook = build()
    br.llm = LLM(["嗯"])
    br.route(msg("老王你昨天说的那个事", "wxid_a", "m1"))
    br.route(msg("什么来着？", "wxid_b", "m2"))
    blob = str(br._build_proactive_prompt(GROUP))                         # noqa: SLF001
    ok &= check("两个人的话都进了提示词（不只看 @ 我的）",
                "wxid_a" in blob and "wxid_b" in blob, blob[:120].replace("\n", " | "))
    ok &= check("提示词明确允许不说话（<SILENT>）", "<SILENT>" in blob)

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n结论：{'情景交融自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
