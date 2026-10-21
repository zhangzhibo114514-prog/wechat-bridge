# -*- coding: utf-8 -*-
"""长记忆自测：跨天还记得住这个人。

要锁住的事：
  1. 存进去 / 按相关性取出来（中文二元组要能命中："美式咖啡" 用 "咖啡" 也能找到）；
  2. 重复的不重复记、太短的不记；
  3. 会话之间互不串味（A 群记得的事不能出现在跟 B 的私聊里）；
  4. 重启不丢（换一个实例照样读得出来）；
  5. 能删（"忘掉 咖啡" 只删命中的那条）；
  6. 写满后按**价值**淘汰，不是无脑按时间丢；
  7. 显式指令"记住…/忘掉…"**不调用模型**也生效；
  8. 模型摘要能解析（含 ```json 包裹、脏输出），坏输出不崩；
  9. 提示词里真的带上了记忆（这条最容易"写了但没接线"）。

跑法：python long_memory_selftest.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

TMP = ROOT / "state" / "_memtests"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


def main() -> int:
    from wechat_bridge.config import MemoryConfig
    from wechat_bridge.long_memory import LongMemory, tokens

    shutil.rmtree(TMP, ignore_errors=True)
    ok = True

    def mem(**kw) -> LongMemory:
        cfg = MemoryConfig(dir="state/_memtests", **kw)
        return LongMemory(cfg, ROOT)

    # 1) 分词：中文二元组 + 英文词
    t = tokens("我喜欢喝美式咖啡 latte")
    ok &= check("中文按二元组切（'咖啡' 在）", "咖啡" in t, str(sorted(t))[:120])
    ok &= check("英文词也切出来了", "latte" in t)

    # 2) 存 / 取
    m = mem()
    ok &= check("存一条返回 True", m.add("c1", "他喜欢喝美式咖啡", kind="pref") is True)
    m.add("c1", "他周五要去上海出差", kind="event")
    m.add("c1", "他养了一只叫豆豆的猫")
    hit = m.recall("c1", "咖啡")
    ok &= check("按关键词 recalled 到正确那条",
                bool(hit) and "美式咖啡" in hit[0].text, hit[0].text if hit else "<空>")
    ok &= check("问到的东西一条没命中时也有兜底", len(m.recall("c1", "完全不相关的火星语")) > 0)

    # 3) 去重 / 太短
    before = len(m.load("c1"))
    ok &= check("同一句记第二遍返回 False", m.add("c1", "他喜欢喝美式咖啡") is False)
    ok &= check("重复的没有真的加进去", len(m.load("c1")) == before)
    ok &= check("太短的不记（'嗯'）", m.add("c1", "嗯") is False)

    # 4) 会话隔离
    ok &= check("别的会话看不到这条", all("咖啡" not in it.text for it in m.load("c2")))

    # 5) 持久化：换实例照样读得到
    m2 = mem()
    ok &= check("重启后记忆还在", any("咖啡" in it.text for it in m2.load("c1")))

    # 6) 能删（只删命中的）
    removed = m2.forget("c1", "咖啡")
    left = [it.text for it in m2.load("c1")]
    ok &= check("忘掉只删命中的那条", removed == 1 and all("咖啡" not in t for t in left), str(left))
    ok &= check("空关键词不会清空全部", m2.forget("c1", "") == 0)

    # 7) 上限淘汰按价值：偏好比闲聊耐留
    m3 = mem(max_items=3)
    m3.add("c3", "他随口说过一句今天风挺大")
    m3.add("c3", "他随口说过一句路上堵")
    m3.add("c3", "他对坚果过敏", kind="pref")
    m3.add("c3", "他喜欢喝美式咖啡", kind="pref")
    texts = [it.text for it in m3.load("c3")]
    ok &= check("超过上限就淘汰（只留 3 条）", len(texts) == 3, str(texts))
    ok &= check("偏好类活下来了",
                any("过敏" in t for t in texts) and any("咖啡" in t for t in texts), str(texts))

    # 8) 渲染
    block = m3.render("c3", "过敏")
    ok &= check("渲染成提示词能读的一段话", "长期记忆" in block and "过敏" in block, block[:80])
    ok &= check("没有记忆时渲染为空串", mem().render("c_empty", "随便") == "")

    # 9) 显式指令解析
    p = mem()
    ok &= check("解析「记住 我周五要去上海」",
                p.parse_remember("记住 我周五要去上海") == "我周五要去上海")
    ok &= check("解析「记住：我叫小王」", p.parse_remember("记住：我叫小王") == "我叫小王")
    ok &= check("解析「记一下我今天生日」", p.parse_remember("记一下我今天生日") == "我今天生日")
    # 2026-10-18：真机上「记着」「备忘」这类同义说法也该认（此前只认 5 个词）
    ok &= check("解析「帮我记着 我下午要开会」", p.parse_remember("帮我记着 我下午要开会") == "我下午要开会")
    ok &= check("解析「备忘 周五交报告」", p.parse_remember("备忘 周五交报告") == "周五交报告")
    ok &= check("「我记住你了」不会被当成指令", p.parse_remember("我记住你了") is None)
    ok &= check("普通聊天不会被误判成指令", p.parse_remember("你记住了吗") is None)
    ok &= check("解析「忘掉 咖啡」", p.parse_forget("忘掉 咖啡") == "咖啡")

    # 10) 模型摘要解析
    good = '[{"kind":"pref","text":"他喜欢喝美式"},{"kind":"event","text":"他周五去上海"}]'
    ok &= check("解析标准 JSON 数组", len(LongMemory.parse_extract(good)) == 2)
    fenced = '好的：\n```json\n' + good + '\n```\n以上。'
    ok &= check("夹在 ```json 里的也能解析", len(LongMemory.parse_extract(fenced)) == 2)
    ok &= check("坏输出不崩、返回空", LongMemory.parse_extract("模型说了些废话没有 JSON") == [])

    # 11) 关闭时什么都不做
    off = mem(enabled=False)
    ok &= check("关闭时 add 返回 False", off.add("c9", "随便记一条试试") is False)
    ok &= check("关闭时 render 为空", off.render("c9", "随便") == "")

    # ---- 接线：桥接里真的用上了（这一类"写了没接"最容易出）----
    import bridge as B
    from wechat_bridge.adapters.base import Message
    from wechat_bridge.adapters.hook_mock import MockHookAdapter
    from wechat_bridge.config import BridgeConfig

    class CountingLLM:
        def __init__(self, reply: str = "收到") -> None:
            self.calls: list[list[dict]] = []
            self.reply = reply

        def complete(self, messages):
            self.calls.append(messages)
            return self.reply

    class Hook(MockHookAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[str] = []

        def recv(self):
            return None

        def send(self, chat_id: str, text: str, at: str = "") -> bool:     # noqa: ARG002
            self.sent.append(text)
            return True

    def build_mem_bridge() -> tuple:
        shutil.rmtree(TMP, ignore_errors=True)
        cfg = BridgeConfig()
        cfg.policy.mode = "auto"
        cfg.policy.contact_rules = {"filehelper": "allow"}
        cfg.policy.max_reply_age_min = 0
        cfg.policy.quiet_hours = ()
        cfg.policy.reply_delay_sec = (0.0, 0.0)
        cfg.policy.reply_debounce_sec = 0
        cfg.segments.enabled = False
        cfg.segments.gap_sec = (0.0, 0.0)
        cfg.memory.dir = "state/_memtests"
        cfg.storage.audit_path = "state/_memtests/audit.jsonl"
        cfg.storage.state_path = "state/_memtests/state.json"
        cfg.storage.draft_dir = "state/_memtests/drafts"
        br = B.Bridge(cfg, allow_real_llm=False)
        llm = CountingLLM()
        br.llm = llm
        br.hook = Hook()
        return br, llm

    def note(text: str, mid: str) -> Message:
        return Message(chat_id="filehelper", sender_id="wxid_a", sender_name="Alice",
                       text=text, is_group=False, msg_id=f"filehelper:{mid}", ts=time.time())

    br, llm = build_mem_bridge()
    br.memory.add("filehelper", "他对坚果过敏", kind="pref")
    blob = str(br._build_prompt(note("今晚吃啥", "p1")))            # noqa: SLF001
    ok &= check("提示词里带上了长期记忆", "坚果" in blob and "长期记忆" in blob)

    # 显式"记住…"：不该花模型的钱
    br, llm = build_mem_bridge()
    decision = br.route(note("记住 我周五要去上海出差", "m1"))
    ok &= check("「记住…」不调用模型", len(llm.calls) == 0, f"调用了 {len(llm.calls)} 次")
    ok &= check("「记住…」落盘成功",
                any("上海" in it.text for it in br.memory.load("filehelper")))
    ok &= check("「记住…」回了一句确认", decision.action == "reply" and bool(br.hook.sent),
                str(br.hook.sent))

    # 忘掉
    br.route(note("忘掉 上海", "m2"))
    ok &= check("「忘掉…」真的删掉了",
                all("上海" not in it.text for it in br.memory.load("filehelper")))

    # 群里最自然的说法：「@我 记住 xxx」也要认出来（不能只认正文开头的"记住"）
    br.route(note("@我 记住 我下周一要交报告", "m2b"))
    ok &= check("「@我 记住 …」也能落盘",
                any("交报告" in it.text for it in br.memory.load("filehelper")),
                str([it.text for it in br.memory.load("filehelper")]))

    # 模型摘要：extract_every=1 时，回一次就该学到东西
    br, llm = build_mem_bridge()
    br.cfg.memory.extract_every = 1                                # noqa: SLF001
    llm.reply = '[{"kind":"pref","text":"他喜欢喝美式"},{"kind":"event","text":"他周五去上海"}]'
    br.route(note("随便聊两句", "m3"))
    learned = [it.text for it in br.memory.load("filehelper")]
    ok &= check("模型摘要写进了长记忆", ("他喜欢喝美式" in learned) and ("他周五去上海" in learned),
                str(learned))

    # 12) 分层压缩：太老的记忆 → 先摘要成 digest，原文进 archive/
    old_ts = time.time() - 10 * 86400
    m4 = mem(archive_after_days=7, archive_batch=8, digest_max_items=3)
    m4.add("c4", "他三周前提过在学吉他", ts=old_ts)
    m4.add("c4", "他当时说想换工作", ts=old_ts)
    m4.add("c4", "他抱怨过通勤太久", ts=old_ts)
    m4.add("c4", "他昨天说周五要去上海")
    digest_reply = '[{"kind":"digest","text":"他在学吉他，想换工作，通勤很远"}]'
    res = m4.compact("c4", summarizer=lambda _msgs: digest_reply)
    active = [it.text for it in m4.load("c4")]
    ok &= check("压缩后旧条目被归档（3 条）", res["archived"] == 3, str(res))
    ok &= check("压缩生成了一条摘要（没有 fallback）",
                res["digests"] == 1 and res["fallback"] is False, str(res))
    ok &= check("新的那条还留着（只压旧的）", any("周五要去上海" in t for t in active), str(active))
    ok &= check("摘要条目进了活跃记忆",
                any("学吉他" in t for t in active), str(active))
    ok &= check("旧原文已经不在活跃记忆里",
                not any("通勤太久" in t for t in active), str(active))
    ok &= check("原文进了 archive/*.jsonl（可回溯、不丢）",
                m4.archived_count("c4") == 3, f"{m4.archived_count('c4')} 条")
    block = m4.render("c4", "他最近在做什么")
    ok &= check("摘要以「摘要」出现在提示词里", "[摘要]" in block, block[:120])

    # 幂等：没有新的旧条目时再压一次什么都不做
    res2 = m4.compact("c4", summarizer=lambda _msgs: digest_reply)
    ok &= check("没有旧条目时再压一次是空操作",
                res2["archived"] == 0 and res2["digests"] == 0, str(res2))

    # 摘要器挂了：仍然归档原文（不能因为模型不可用就无限膨胀）
    m5 = mem(archive_after_days=7)
    m5.add("c5", "他很早以前说过一句废话", ts=old_ts)

    def boom(_msgs):
        raise RuntimeError("模型挂了")

    res3 = m5.compact("c5", summarizer=boom)
    ok &= check("摘要失败时 fallback=True 且原文照样归档",
                res3["fallback"] is True and res3["archived"] == 1, str(res3))
    ok &= check("摘要失败不会崩，活跃记忆变空", m5.load("c5") == [])

    # archive_after_days=0：不按时间压缩
    m6 = mem(archive_after_days=0)
    m6.add("c6", "很久以前的事", ts=old_ts)
    ok &= check("archive_after_days=0 时不做时间压缩", m6.stale_items("c6") == [])

    # 超上限的那部分也要归档，不能凭空消失
    m7 = mem(max_items=2)
    for i in range(4):
        m7.add("c7", f"第{i}条闲聊记录内容", ts=old_ts + i)
    ok &= check("超出上限被淘汰的条目进了归档", m7.archived_count("c7") >= 2,
                f"{m7.archived_count('c7')} 条")

    # 13) 桥接里真的会触发压缩（按会话限频，不能每次回复都扫）
    br, llm = build_mem_bridge()
    br.cfg.memory.archive_after_days = 7                              # noqa: SLF001
    br.memory.add("filehelper", "他很久以前说过想学吉他", ts=time.time() - 10 * 86400)
    llm.reply = '[{"kind":"digest","text":"他想学吉他"}]'
    br._maybe_compact("filehelper")                                   # noqa: SLF001
    ok &= check("桥接会触发压缩并生成摘要",
                any(it.kind == "digest" for it in br.memory.load("filehelper")),
                str([(it.kind, it.text) for it in br.memory.load("filehelper")]))
    ok &= check("桥接压缩后原文进了归档",
                br.memory.archived_count("filehelper") >= 1)
    before = len(llm.calls)
    br.memory.add("filehelper", "另一条很久以前的话", ts=time.time() - 10 * 86400)
    br._maybe_compact("filehelper")                                   # noqa: SLF001
    ok &= check("同一会话内的压缩检查有限频（刚查过就不再调模型）",
                len(llm.calls) == before, f"多调了 {len(llm.calls) - before} 次")

    # 14) 「记住啊」这种**没说记什么**的口语：要记上一句，不能记成"啊"也不能当没说。
    #     真机原话：`@我 我现在说的` + `你可一定要记住啊`（防抖合并成一条）。
    br, llm = build_mem_bridge()
    br.route(note("我周五要去上海开会", "p10"))                       # 普通聊天，先让它进上下文
    d = br.route(note("你可一定要记住啊", "p11"))
    stored = [it.text for it in br.memory.load("filehelper")]
    ok &= check("「记住啊」会记下**上一句**，而不是记成'啊'",
                any("上海" in t for t in stored), str(stored))
    ok &= check("「记住啊」不调用模型（它是指令）", len(llm.calls) == 1, f"调用 {len(llm.calls)} 次")
    ok &= check("「记住啊」给出的是确认，不是'已经记着了'",
                "记着" not in str(getattr(d, "reply_text", "")),
                str(getattr(d, "reply_text", "")))
    ok &= check("「记住了吗」这种问句不会被当成指令",
                br._memory_instruction(note("你记住了吗", "p12"))[0] is None)  # noqa: SLF001

    # 15) 防抖合并：指令落在第二行也要认（`@我 我现在说的` + `记住 我下周三交报告`）
    br, llm = build_mem_bridge()
    br.route(note("@我 我现在说的\n记住 我下周三交报告", "p13"))
    ok &= check("多行合并后指令在第二行也能被认出来",
                any("交报告" in it.text for it in br.memory.load("filehelper")),
                str([it.text for it in br.memory.load("filehelper")]))

    # 16) add_verbose：三种"没加成"必须分得开（否则会把失败说成"已经记着了"）
    m8 = mem()
    ok &= check("add_verbose 新增 = added",
                m8.add_verbose("c8", "他喜欢喝美式咖啡") == "added")
    ok &= check("add_verbose 重复 = duplicate",
                m8.add_verbose("c8", "他喜欢喝美式咖啡") == "duplicate")
    ok &= check("add_verbose 太短 = too_short", m8.add_verbose("c8", "啊") == "too_short")
    off2 = mem(enabled=False)
    ok &= check("add_verbose 关闭 = disabled", off2.add_verbose("c8", "随便一条内容") == "disabled")

    # 17) 审计必须留下**喂给模型的原话**（P0-2：没有它，记忆没生效时无从判断）
    br, llm = build_mem_bridge()
    br.memory.add("filehelper", "他对坚果过敏", kind="pref")
    br.route(note("今晚吃啥", "p14"))
    rows = [json.loads(line) for line in
            (TMP / "audit.jsonl").open(encoding="utf-8") if line.strip()]
    sent_rows = [r for r in rows if r.get("event") == "sent"]
    ok &= check("审计里存了 prompt", bool(sent_rows) and bool(sent_rows[-1].get("prompt")),
                f"{len(sent_rows)} 条 sent")
    blob = str(sent_rows[-1].get("prompt") if sent_rows else "")
    ok &= check("存下来的 prompt 里能看到长期记忆块", "坚果" in blob, blob[:80])

    # 18) 空闲兜底摘要：不靠"回复满 N 次"也能把记忆写进去
    #     （真机零触发的根因之一：只回了 6 次，阈值 10）
    br, llm = build_mem_bridge()
    br.cfg.memory.extract_every = 0                                   # noqa: SLF001
    br.cfg.memory.extract_idle_sec = 0.001                            # noqa: SLF001（0=关闭，所以要给个极小的正数）
    llm.reply = '[{"kind":"pref","text":"他喜欢喝美式"}]'
    br.route(note("随便聊两句", "p15"))
    ok &= check("还没到阈值时确实没摘要", br.memory.load("filehelper") == [])
    br._last_recv_ts = 0                                              # noqa: SLF001 模拟"早就空闲了"
    br._maybe_extract_idle()                                          # noqa: SLF001
    ok &= check("空闲时补跑了摘要，记忆写进去了",
                any("美式" in it.text for it in br.memory.load("filehelper")),
                str([it.text for it in br.memory.load("filehelper")]))

    # 19) 会话第一句就是「记住啊」：没有上一句可记时必须**反问**，
    #     不能让模型接一句"记住了记住了"（2026-10-18 真机踩到：什么都没记还给了保证）。
    br, llm = build_mem_bridge()
    d = br.route(note("你可一定要记住啊", "p16"))
    ok &= check("没有上一句可记时反问，不调用模型",
                len(llm.calls) == 0 and "记啥" in str(getattr(d, "reply_text", "")),
                f"calls={len(llm.calls)} reply={getattr(d, 'reply_text', '')!r}")
    ok &= check("反问时审计里留下 memory_skipped", 
                any(r.get("event") == "memory_skipped" for r in
                    (json.loads(line) for line in (TMP / "audit.jsonl").open(encoding="utf-8")
                     if line.strip())))

    # 20) 两遍抽取：第一遍模型交白卷时，用"玩笑也要记"的提示词再试一次。
    #     真机背景：同一段对话，普通提示词给 []，加这句就抽出 2 条（2026-10-18 实测）。
    class SeqLLM:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = []

        def complete(self, messages):
            self.calls.append(messages)
            return self.replies.pop(0) if self.replies else "[]"

    br, _llm = build_mem_bridge()
    seq = SeqLLM(["收到", "收到", "[]", '[{"kind":"fact","text":"他在做笔记本生意"}]'])
    br.llm = seq
    br.cfg.memory.extract_every = 0                                   # noqa: SLF001
    br.cfg.memory.extract_idle_sec = 0.001                            # noqa: SLF001
    br.cfg.memory.extract_context_turns = 3                           # noqa: SLF001
    br.route(note("我在客户要买的笔记本里加了花生酱", "p20"))
    br.route(note("结果那人他过敏", "p21"))
    br._last_recv_ts = 0                                              # noqa: SLF001
    br._maybe_extract_idle()                                          # noqa: SLF001
    ok &= check("第一遍空、第二遍抽到 → 仍然写进记忆",
                any("笔记本生意" in it.text for it in br.memory.load("filehelper")),
                str([it.text for it in br.memory.load("filehelper")]))
    ok &= check("重试命中会留痕（memory_extract_retry_hit）",
                any(r.get("event") == "memory_extract_retry_hit" for r in
                    (json.loads(line) for line in (TMP / "audit.jsonl").open(encoding="utf-8")
                     if line.strip())))
    ok &= check("确实调了两次抽取（回复2次 + 抽取2次）", len(seq.calls) == 4, f"{len(seq.calls)} 次")

    # 21) 刷屏去重：群里连发同一条，审计别写十几行（实测 16 条 `test` 写了 16 行）
    br, _llm = build_mem_bridge()
    audit = TMP / "audit.jsonl"
    for _ in range(12):
        br._log(event="group_quiet", reason="测试刷屏", chat_id="g@chatroom",
                sender_id="wxid_a", text="test")             # noqa: SLF001
    rows = [json.loads(line) for line in audit.open(encoding="utf-8") if line.strip()]
    ok &= check("同一条刷屏 12 次只留 3 行审计（第1/第2/第10次）", len(rows) == 3, f"{len(rows)} 行")
    ok &= check("留下的行里带 repeat 计数", rows[-1].get("repeat") == 10, str(rows[-1].get("repeat")))
    br._log(event="group_quiet", reason="测试刷屏", chat_id="g@chatroom",    # noqa: SLF001
            sender_id="wxid_a", text="不一样的内容")
    rows2 = [json.loads(line) for line in audit.open(encoding="utf-8") if line.strip()]
    ok &= check("不同内容照常记录", len(rows2) == 4, f"{len(rows2)} 行")

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n结论：{'长记忆自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
