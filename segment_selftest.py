"""多段输出（拟人节奏）的离线自测。跑法：python segment_selftest.py

**为什么要有它**：多段切分的失败方式**都很安静**——
切多了像刷屏、切少了等于没做（功能看起来"没生效"）、切坏了（把词砍成两半）
在群里看起来像程序出错。而"越界时把内容丢了"这种错误，用户在群里只看到
"它话说了一半"，根本不会想到是切分逻辑干的。

所以这里有两条主断言：
① **绝不丢内容**——越界（段数超限 / 段落超长）时，原文的每个字都要还在；
② **越界必须留痕**——notes 非空，让它进审计、看得见。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wechat_bridge.segments import split_segments                            # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return ok


def test_basic() -> bool:
    ok = True
    print("-- 基本切分 --")

    r = split_segments("在的，怎么了")
    ok &= check("没有分隔符 → 原样一段", r.segments == ["在的，怎么了"], str(r.segments))
    ok &= check("没越界时 notes 为空", r.notes == [], str(r.notes))

    r = split_segments("这是什么？|||算了，我看看就知道了")
    ok &= check("`|||` 切成两段", r.segments == ["这是什么？", "算了，我看看就知道了"], str(r.segments))

    r = split_segments("第一句\n\n第二句")
    ok &= check("空行也能分段（模型常这么干）", r.segments == ["第一句", "第二句"], str(r.segments))

    r = split_segments("这是什么？|||算了|||我看看就知道了")
    ok &= check("三段且顺序不变",
                r.segments == ["这是什么？", "算了", "我看看就知道了"], str(r.segments))

    r = split_segments("  前面有空格 ||| 后面也有  ")
    ok &= check("段首段尾空白被清掉", r.segments == ["前面有空格", "后面也有"], str(r.segments))

    print("\n-- 边界输入不能炸 --")
    ok &= check("空串 → 空列表", split_segments("").segments == [])
    ok &= check("None → 空列表（不抛异常）", split_segments(None).segments == [])       # type: ignore[arg-type]
    ok &= check("只有分隔符 → 空列表", split_segments("|||").segments == [])
    ok &= check("全是空白 → 空列表", split_segments("   \n\n  ").segments == [])
    ok &= check("max_segments=1 也不会返回空", split_segments("a|||b", max_segments=1).segments != [])
    return ok


def test_never_lose_content() -> bool:
    """本文件的核心：**越界时的行为必须是"全发出去 + 留痕"，不是"悄悄丢掉一部分"**。"""
    ok = True
    print("\n-- 越界（段数超限）：内容一个都不能少 --")

    pieces = ["第一句", "第二句", "第三句", "第四句", "第五句"]
    r = split_segments("|||".join(pieces), max_segments=3)
    ok &= check("段数被压到上限 3", len(r.segments) == 3, str(r.segments))
    ok &= check("**五段文字全在**（多出的并进最后一段，没有丢弃）",
                all(p in " ".join(r.segments) for p in pieces), " ".join(r.segments))
    ok &= check("越界被记进 notes（可进审计）", r.notes and "超过上限" in r.notes[0], str(r.notes))

    print("\n-- 越界（单段超长）：在句末标点断，不硬砍 --")
    long_text = "这是第一句话。这是第二句话。这是第三句话。这是第四句话。"
    r = split_segments(long_text, max_segments=3, max_chars=10)
    ok &= check("长段落被自动断成多条", len(r.segments) > 1, str(r.segments))
    ok &= check("**每个字都还在**（拼接后与原文一致）",
                "".join(r.segments) == long_text, "".join(r.segments))
    ok &= check("断点都在句末标点上（不是从词中间砍）",
                all(s.endswith(("。", "！", "？", "…", "；", ";")) for s in r.segments[:-1]),
                str(r.segments))
    ok &= check("自动断句也记进 notes", any("自动断成" in n for n in r.notes), str(r.notes))

    print("\n-- 超长且无处可断：宁可原样发，也不切坏 --")
    no_stop = "一" * 50                       # 50 个没有标点的字
    r = split_segments(no_stop, max_segments=3, max_chars=10)
    ok &= check("没有句末标点时原样一段", r.segments == [no_stop], str(r.segments))
    ok &= check("且如实说明（不是悄悄放过）", any("没有可断的句末标点" in n for n in r.notes), str(r.notes))
    ok &= check("内容仍然完整", "".join(r.segments) == no_stop)

    print("\n-- 真实样例（DESIGN.md 2.5 里用户举的那个）--")
    real = "这是什么？|||算了，我看看就知道了"
    r = split_segments(real, max_segments=3, max_chars=80)
    ok &= check("用户示例切成两段、按序、无越界",
                r.segments == ["这是什么？", "算了，我看看就知道了"] and not r.notes,
                str(r.segments))
    return ok


def test_send_loop() -> bool:
    """**走真实发送循环**的端到端断言（这是唯一能证明"真的分次发了"的测法）。

    为什么不能只测 `split_segments()`：切分正确 ≠ 桥接真的分次发了，
    更证明不了"第 2 条失败时没有谎报成功"。而这两件事恰恰是群里唯一看得见的部分
    ——对方收到的是三条，还是半句话。
    """
    ok = True
    import json
    import shutil
    import time

    import bridge as B
    from wechat_bridge.adapters.base import Message
    from wechat_bridge.adapters.hook_mock import MockHookAdapter
    from wechat_bridge.config import BridgeConfig

    print("\n-- 端到端：一次响应 → 逐条发出 --")
    ack = "这是什么？|||算了，我看看就知道了"

    class FakeLLM:
        def complete(self, messages):                       # noqa: ARG002
            return ack

    class Recorder(MockHookAdapter):
        """记录每次 send 的正文与 @ 对象；fail_at=N 时第 N 次返回失败。"""

        def __init__(self, fail_at: "int | None" = None) -> None:
            super().__init__()
            self.sent: list[str] = []
            self.ats: list[str] = []
            self.fail_at = fail_at

        def send(self, chat_id: str, text: str, at: str = "") -> bool:    # noqa: ARG002
            self.sent.append(text)
            self.ats.append(at)
            return not (self.fail_at is not None and len(self.sent) == self.fail_at)

    def run(fail_at=None, reply=None, background=None):
        products = ROOT / "state" / "_segtests"
        shutil.rmtree(products, ignore_errors=True)         # 每次跑都从干净状态开始
        cfg = BridgeConfig()
        cfg.policy.mode = "auto"
        cfg.policy.contact_rules = {"filehelper": "allow"}
        cfg.policy.max_reply_age_min = 0                    # 合成消息的时间戳可能是旧的
        cfg.policy.quiet_hours = ()                         # 否则过了 23:00 这条自测会"被静默时段拦"
        cfg.policy.reply_delay_sec = (0.0, 0.0)
        cfg.segments.gap_sec = (0.0, 0.0)                   # 自测不该真的等 1.5–4 秒
        cfg.storage.audit_path = "state/_segtests/audit.jsonl"
        cfg.storage.state_path = "state/_segtests/state.json"
        cfg.storage.draft_dir = "state/_segtests/drafts"

        class FakeLLM:
            def complete(self, messages):                   # noqa: ARG002
                return reply if reply is not None else ack

        # allow_real_llm=False：不解析真实模型适配器（离线自测不该碰网络/密钥）。
        # 下面紧接着就把 bridge.llm 换成控制回复内容的假模型。
        bridge = B.Bridge(cfg, allow_real_llm=False)
        bridge.llm = FakeLLM()
        hook = Recorder(fail_at=fail_at)
        # background=None ⇒ 这个假 hook **没有** can_send_background 方法，
        # 等价于 demo/干跑那种"根本没有后台概念"的适配器（保持老行为）。
        if background is not None:
            hook.can_send_background = lambda chat_id: background   # noqa: ARG005
        bridge.hook = hook
        # 直接构造统一 Message（不经某个适配器的行转换）——这条自测关心的是**发送侧**。
        msg = Message(chat_id="filehelper", sender_id="wxid_somebody", sender_name="某人",
                      text="在吗？", is_group=False, msg_id="filehelper:1", ts=time.time())
        decision = bridge.route(msg)
        events = [json.loads(line) for line in
                  (ROOT / cfg.storage.audit_path).read_text(encoding="utf-8").splitlines() if line.strip()]
        return hook, decision, events

    hook, decision, events = run()
    ok &= check("一次响应被拆成 2 条发出",
                hook.sent == ["这是什么？", "算了，我看看就知道了"], str(hook.sent))
    ok &= check("私聊不加 @（只有群聊才需要 @ 回去）",
                hook.ats == ["", ""], str(hook.ats))
    ok &= check("决策是 reply（成功）", decision.action == "reply", f"{decision.action}/{decision.reason}")
    sent_events = [e for e in events if e.get("event") == "sent"]
    ok &= check("审计里是**一条** sent（带 segments 明细），不是两条",
                len(sent_events) == 1 and sent_events[0].get("segments") == hook.sent,
                str([(e.get("event"), e.get("segments")) for e in events]))

    print("\n-- 端到端：第 2 条发送失败 → 必须记 send_partial，**绝不许记成 sent** --")
    hook2, decision2, events2 = run(fail_at=2)
    # 注意：这里断言的是**尝试过两条**。真实成功条数以审计的 sent_segments 为准
    # （下面那条），别拿"调用过几次"冒充"发出去了几条"——那是两回事。
    ok &= check("两条都尝试发了（第 2 条是发送失败，不是压根没发）",
                hook2.sent == ["这是什么？", "算了，我看看就知道了"], str(hook2.sent))
    ok &= check("审计里**没有** sent（不能谎报成功）",
                not [e for e in events2 if e.get("event") == "sent"],
                str([e.get("event") for e in events2]))
    partial = [e for e in events2 if e.get("event") == "send_partial"]
    ok &= check("记的是 send_partial，且如实写明只发出 1/2",
                len(partial) == 1 and partial[0].get("sent_segments") == 1
                and partial[0].get("total_segments") == 2, str(partial))
    ok &= check("决策是 error（对上层也不谎称成功）", decision2.action == "error", decision2.action)

    print("\n-- 端到端：**模型没给分隔符**也要能拆（真机 112 条里 0 条用了 |||）--")
    long_reply = "这图我看懂了。左边两个小脑在喊 AMD 和 Intel，右边是黄仁勋顶着大脑袋。你从哪弄的？"
    hook3, decision3, events3 = run(reply=long_reply)
    ok &= check("长回复被自动拆成多条发出", len(hook3.sent) >= 2, str(hook3.sent))
    ok &= check("拆出来的字一个不少（拼回去等于原文）",
                "".join(hook3.sent) == long_reply, "".join(hook3.sent))
    ok &= check("审计里记了「模型没给分隔符」这件事",
                any("没给分隔符" in str(n) for e in events3
                    for n in (e.get("segment_notes") or [])),
                str([e.get("segment_notes") for e in events3 if e.get("segment_notes")]))
    ok &= check("决策仍是 reply", decision3.action == "reply", f"{decision3.action}/{decision3.reason}")

    hook4, _, _ = run(reply="在的，怎么了。")
    ok &= check("短回复不拆（一句话本来就够）", hook4.sent == ["在的，怎么了。"], str(hook4.sent))

    # 真机 21:29：用户让它艾特某人，它回「好，这就艾特 @阿乙」——
    # 正文里的 @ 在微信里只是纯文本，对方收不到提醒，群里却以为它艾特了。
    hook8, _, events8 = run(reply="好，这就艾特 @阿乙")
    ok &= check("正文里的假 @ 被去掉（只留名字）",
                hook8.sent == ["好，这就艾特 阿乙"], str(hook8.sent))
    ok &= check("审计里留下 mention_stripped 痕迹",
                any(e.get("event") == "mention_stripped" for e in events8),
                str([e.get("event") for e in events8]))

    hook9, _, _ = run(reply="我@不了，你自己艾特吧")
    ok &= check("「我@不了」是动词用法，不能被误伤（也不能被拆坏）",
                hook9.sent == ["我@不了，你自己艾特吧"], str(hook9.sent))

    # 真机 20:46 的实际回复：**一个句号都没有**，第一版只认句末标点 ⇒ 永远不拆。
    # 微信聊天本来就不爱打句号，所以冒号也算断点。
    real = "行，那我讲一个：番茄和土豆打架，西红柿去劝架，结果被做成阿甲了"
    hook5, _, events5 = run(reply=real)
    ok &= check("没有句号也能拆（按冒号断）", len(hook5.sent) == 2, str(hook5.sent))
    ok &= check("断在冒号后面，像真人分两条说",
                hook5.sent and hook5.sent[0].endswith(("：", ":")), str(hook5.sent))
    ok &= check("拆完一字不差（真机原文）", "".join(hook5.sent) == real, "".join(hook5.sent))

    hook6, _, _ = run(reply="就……你被炖了，还挺入味")
    ok &= check("一口气说完的短句不拆（12 字，拆了像挤牙膏）",
                hook6.sent == ["就……你被炖了，还挺入味"], str(hook6.sent))

    # 颜文字里的 ASCII 冒号不能当断点：`_(:з」∠)_` 会被拦腰砍成 `_(:` + `з」∠)_`。
    kaomoji = "我没事_(:з」∠)_就是有点想笑，你别管我。"
    hook7, _, _ = run(reply=kaomoji)
    ok &= check("颜文字不被冒号砍断", all("_" not in s[:3] or s.endswith("_") for s in hook7.sent)
                and "".join(hook7.sent) == kaomoji, str(hook7.sent))

    # 真机现象（用户 2026-10-18 22:2x 报的）：**多段有时候还走桌面控制**。
    # 根因不是分段逻辑，而是"目标会话没开着"：后台发送要求会话已有窗口，
    # 没有就只能走 GUI——打开会话、抢焦点。分三段就是弹三次窗口。
    # 所以发送侧要会降级：不能后台发 ⇒ 压成一条，宁可少一点节奏，也只打扰一次。
    print("\n-- 会话没开着时：多段必须压成一条（别连弹三次窗口）--")
    hook_bg_off, _, events_bg_off = run(background=False)
    ok &= check("后台不可用 ⇒ 只发 1 条", len(hook_bg_off.sent) == 1, str(hook_bg_off.sent))
    ok &= check("压起来的那条一个字都没丢",
                hook_bg_off.sent == ["这是什么？算了，我看看就知道了"], str(hook_bg_off.sent))
    sent_bg_off = [e for e in events_bg_off if e.get("event") == "sent"]
    ok &= check("审计写明「为什么这次只有一条」",
                bool(sent_bg_off) and any("no_background" in str(n) for n in
                                          (sent_bg_off[0].get("segment_notes") or [])),
                str([e.get("segment_notes") for e in sent_bg_off]))
    hook_bg_on, _, _ = run(background=True)
    ok &= check("后台可用 ⇒ 照旧分两条（节奏没被误伤）",
                hook_bg_on.sent == ["这是什么？", "算了，我看看就知道了"], str(hook_bg_on.sent))

    shutil.rmtree(ROOT / "state" / "_segtests", ignore_errors=True)
    return ok


def main() -> int:
    print("=" * 64)
    print("多段输出自测")
    print("=" * 64)
    ok = test_basic()
    ok &= test_never_lose_content()
    ok &= test_send_loop()
    print("\n" + "=" * 64)
    print("结论：" + ("多段输出自测通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
