# -*- coding: utf-8 -*-
"""说话决策层自测（`wechat_bridge/speak_gate.py`）。

要锁住的事：
  1. **该说的照样说**：@我 / 引用我的消息走快路，连判定调用都不发生（省一次模型钱）；
  2. **由模型决定**：非 @ 的消息把「聊天记录 + 现象提示」交给模型，模型说接就接、说不接就不接；
  3. **现象提示要准**：@别人 / 引用别人的话 / 只是应了一声 / 换了个人说话 / 我上一句在问他；
  4. **失败一律闭嘴**：判定报错、返回看不懂、超出每小时预算或每分钟上限 ⇒ 不说；
  5. `mode=rules` 仍保留确定性口径（零成本回退路径）；
  6. 桥接层真的被这一层拦住，且**静音的消息仍然进上下文**、仍然安排"冷场再开口"。

跑法：python speak_gate_selftest.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "state" / "_speak_gate_tests"
GROUP = "12345@chatroom"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


def main() -> int:
    from wechat_bridge.speak_gate import GateContext, SpeakGate, SpeakGateConfig

    def g(**kw) -> GateContext:
        kw.setdefault("is_group", True)
        return GateContext(**kw)

    def gate(cfg: SpeakGateConfig | None = None, same_sender_only: bool = True,
             clock=None) -> SpeakGate:
        return SpeakGate(cfg or SpeakGateConfig(), same_sender_only=same_sender_only,
                         clock=clock or time.time)

    calls: list = []

    def say_yes(_ctx, _hints):
        calls.append("yes")
        return True

    def say_no(_ctx, _hints):
        calls.append("no")
        return False

    def say_boom(_ctx, _hints):
        calls.append("boom")
        raise RuntimeError("model down")

    def say_giberish(_ctx, _hints):
        calls.append("gib")
        return None

    ok = True
    print("-- 纯逻辑：结构性过滤 + 被点名的快路 --")

    v = gate(SpeakGateConfig(enabled=False)).judge(g(text="嗯", mentions_me=True))
    ok &= check("1 开关关掉 ⇒ 完全回到旧行为（说）", v.speak and v.code == "disabled",
                f"{v.speak}/{v.code}")

    v = gate().judge(g(text="嗯", is_group=False, mentions_me=True))
    ok &= check("2 私聊不判（谁发来都得回）", v.speak and v.code == "disabled", f"{v.speak}/{v.code}")

    v = gate().judge(g(text="我自己发的", is_self=True))
    ok &= check("3 自己发的 ⇒ 不说（结构性，永不交给模型）",
                (not v.speak) and v.code == "self", f"{v.speak}/{v.code}")

    calls.clear()
    v = gate().judge(g(text="@我 你怎么看", mentions_me=True), judge_model=say_no)
    ok &= check("4 @我 ⇒ 直接说，且**不花判定调用**",
                v.speak and v.code == "at_me" and not calls, f"{v.speak}/{v.code} 判定调用={calls}")

    print("-- 纯逻辑：交给模型判断（默认 mode=model） --")

    calls.clear()
    v = gate().judge(g(text="嗯", mentions_me=True), judge_model=say_yes)
    ok &= check("5 @我 但只应了一声 ⇒ 交给模型；模型说接就接",
                v.speak and v.code == "judge_speak" and calls == ["yes"], f"{v.speak}/{v.code}")

    v = gate().judge(g(text="嗯", mentions_me=True), judge_model=say_no)
    ok &= check("6 同一句，模型说不接 ⇒ 不说", (not v.speak) and v.code == "judge_silent",
                f"{v.speak}/{v.code}")

    q = {"quoted_from": "wxid_b", "quoted_name": "阿乙", "quoted_text": "那事你别管"}
    calls.clear()
    v = gate().judge(g(text="[引用 阿乙：那事你别管] 你算什么东西", quote=q), judge_model=say_no)
    ok &= check("7 引用别人的话 ⇒ 由模型判（这里判不接）",
                (not v.speak) and v.code == "judge_silent", f"{v.speak}/{v.code}")

    hints = gate().hints(g(text="[引用 阿乙：那事你别管] 你算什么东西", quote=q,
                           sender_id="wxid_c", last_speaker="wxid_a",
                           bot_asked_question=True))
    ok &= check("8 现象提示齐全（引用的是谁 / 换了人 / 我上一句在问他）",
                any("阿乙" in h for h in hints)
                and any("不是刚才跟你对话" in h for h in hints)
                and any("上一句是在问他" in h for h in hints), " | ".join(hints))
    hints2 = gate().hints(g(text="@张三 你来", at_others=True))
    ok &= check("8 「@的是别人」也要提示给模型", any("@ 的是别人" in h for h in hints2),
                " | ".join(hints2))
    hints3 = gate().hints(g(text="嗯"))
    ok &= check("8 「只是应了一声」也要提示给模型",
                any("应一声" in h for h in hints3), " | ".join(hints3))

    print("-- 纯逻辑：mode=rules（零成本回退口径） --")

    R = gate(SpeakGateConfig(mode="rules"))

    v = R.judge(g(text="@张三 你来一下", at_others=True))
    ok &= check("9 rules：@别人 ⇒ 不说", (not v.speak) and v.code == "at_others", f"{v.speak}/{v.code}")

    v = R.judge(g(text="[引用 阿乙：那事你别管] 你算什么东西", quote=q))
    ok &= check("9 rules：引用别人的话 ⇒ 不说", (not v.speak) and v.code == "quote_others",
                f"{v.speak}/{v.code}")

    v = R.judge(g(text="嗯", mentions_me=True))
    ok &= check("9 rules：只是应了一声 ⇒ 不说（哪怕 @ 了我）",
                (not v.speak) and v.code == "ack_only", f"{v.speak}/{v.code}")

    v = R.judge(g(text="好", mentions_me=False, bot_asked_question=True))
    ok &= check("10 rules：我上一句在问他时，「好」= 答应 ⇒ 说",
                v.speak and v.code == "ack_answers_question", f"{v.speak}/{v.code}")

    v = R.judge(g(text="随便聊聊", sender_id="wxid_b", last_speaker="wxid_a"))
    ok &= check("9 rules：不是刚才跟我对话的人 ⇒ 不说",
                (not v.speak) and v.code == "other_sender", f"{v.speak}/{v.code}")

    v = R.judge(g(text="那你说说", sender_id="wxid_a", last_speaker="wxid_a"))
    ok &= check("11 rules：没命中任何规则 ⇒ 说（保持旧行为）", v.speak, f"{v.speak}/{v.code}")

    print("-- 纯逻辑：兜底（一律闭嘴） --")

    calls.clear()
    m0 = gate(SpeakGateConfig(mode="model", model_budget_per_hour=0))
    v = m0.judge(g(text="随便说一句"), judge_model=say_yes)
    ok &= check("12 每小时预算用完 ⇒ 不说且**根本没调用**模型",
                (not v.speak) and v.code == "judge_budget" and not calls, f"{v.speak}/{v.code}")

    clock = {"t": 1000.0}
    mr = gate(SpeakGateConfig(mode="model", model_max_per_min=1), clock=lambda: clock["t"])
    mr.judge(g(text="第一句"), judge_model=say_yes)
    calls.clear()
    v = mr.judge(g(text="第二句"), judge_model=say_yes)
    ok &= check("13 每分钟上限 ⇒ 不说且不调用模型（延迟保险丝）",
                (not v.speak) and v.code == "judge_rate" and not calls, f"{v.speak}/{v.code}")

    v = gate().judge(g(text="随便说一句"), judge_model=say_boom)
    ok &= check("14 判定调用炸了 ⇒ 按不说处理（宁少说不乱说）",
                (not v.speak) and v.code == "judge_unknown", f"{v.speak}/{v.code}")

    v = gate().judge(g(text="随便说一句"), judge_model=say_giberish)
    ok &= check("15 模型回了看不懂的东西 ⇒ 按不说处理",
                (not v.speak) and v.code == "judge_unknown", f"{v.speak}/{v.code}")

    # ---------------- 桥接层：模型真的在决定 ----------------
    print("-- 桥接层：由模型决定接不接 --")

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
        """同一个模型既当"判定器"又当"回复器"——按提示词区分，贴合真机（只有一个 llm 客户端）。"""

        def __init__(self, replies, decide=None) -> None:
            self.replies = list(replies)
            self.decide = decide or (lambda prompt: "不接")
            self.judge_calls = 0
            self.prompts: list[str] = []

        def complete(self, messages):
            text = "\n".join(str(m.get("content", "")) for m in messages)
            if "只回一个词：接 / 不接" in text:
                self.judge_calls += 1
                self.prompts.append(text)
                return str(self.decide(text))
            return self.replies.pop(0) if self.replies else "<SILENT>"

    def build() -> tuple:
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
        cfg.group.proactive = True               # 旧语义：激活态任何消息都回
        cfg.group.proactive_silence_sec = 0.0
        cfg.group.proactive_min_history = 1
        cfg.group.observe_first_msgs = 0
        cfg.group.at_back = True
        cfg.segments.enabled = False
        cfg.memory.enabled = False
        cfg.storage.audit_path = "state/_speak_gate_tests/audit.jsonl"
        cfg.storage.state_path = "state/_speak_gate_tests/state.json"
        cfg.storage.draft_dir = "state/_speak_gate_tests/drafts"
        _br = B.Bridge(cfg, allow_real_llm=False)
        _hook = Hook()
        _br.hook = _hook
        return _br, _hook

    def decide(prompt: str) -> str:
        # 只看"现在这一条"那一段：聊天记录里还留着更早的句子，用全文匹配会一直判"接"。
        tail = prompt.rsplit("现在这一条", 1)[-1]
        return "接" if "那你倒是说啊" in tail else "不接"

    def msg(text: str, sender: str = "wxid_a", mid: str = "m1", mention: bool = False) -> Message:
        return Message(chat_id=GROUP, sender_id=sender, sender_name=f"人{sender[-1]}",
                       text=text, is_group=True, msg_id=f"{GROUP}:{mid}", ts=time.time(),
                       mentions_me=mention)

    def audit() -> list[dict]:
        path = TMP / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]

    br, hook = build()
    br.llm = LLM(["我看看哈", "那我们从头捋一遍", "好，就按这个来"], decide=decide)

    br.route(msg("@我 这事你怎么看", "wxid_a", "m1", mention=True))
    ok &= check("16 被 @ 时照常回", len(hook.sent) == 1, str(hook.sent))
    ok &= check("16 且**没有**发生判定调用（被点名走快路，省一次模型钱）",
                br.llm.judge_calls == 0, f"judge_calls={br.llm.judge_calls}")

    br.route(msg("我觉得还行", "wxid_b", "m3"))
    ok &= check("17 别人说话、模型判「不接」⇒ 不发",
                len(hook.sent) == 1 and br.llm.judge_calls == 1,
                f"sent={hook.sent} judge={br.llm.judge_calls}")
    ok &= check("17 留痕：审计里有 speak_silent / judge_silent",
                any(r.get("event") == "speak_silent" and r.get("code") == "judge_silent"
                    for r in audit()))

    br.route(msg("嗯", "wxid_a", "m4"))
    ok &= check("18 对方只回了个「嗯」，模型判不接 ⇒ 不发",
                len(hook.sent) == 1, str(hook.sent))

    br.route(msg("那你倒是说啊", "wxid_a", "m5"))
    ok &= check("19 刚才那个人追问、模型判「接」⇒ 发",
                len(hook.sent) == 2, str(hook.sent))

    ring = " ".join(str(x.get("text") or "") for x in br.group_scheduler.context(GROUP))
    ok &= check("20 被静音的消息**照样进了上下文**（不是没看见）",
                "我觉得还行" in ring and "嗯" in ring, ring[:80])
    ok &= check("20 静音后仍然安排了「冷场再决定要不要开口」",
                GROUP in br._proactive_due, str(br._proactive_due))        # noqa: SLF001

    br.route(Message(chat_id=GROUP, sender_id="wxid_b", sender_name="人b", is_group=True,
                     text="[引用 阿丙：那事你别管] 你算什么东西",
                     msg_id=f"{GROUP}:m6", ts=time.time(),
                     quote={"quoted_from": "wxid_c", "quoted_name": "阿丙",
                            "quoted_text": "那事你别管"}))
    q_prompt = br.llm.prompts[-1] if br.llm.prompts else ""
    ok &= check("21 引用别人的话：提示里要写明「引用的是阿丙的话」",
                "引用的是阿丙的话" in q_prompt, q_prompt[-160:].replace("\n", " | "))
    ok &= check("21 模型据此判不接 ⇒ 不发", len(hook.sent) == 2, str(hook.sent))

    br.route(msg("@我 那你倒是说啊", "wxid_a", "m7", mention=True))
    ok &= check("22 再被 @ 时立刻恢复正常（不会一直被静音）",
                len(hook.sent) == 3, str(hook.sent))

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n结论：{'说话决策层自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
