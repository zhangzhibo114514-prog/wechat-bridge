"""群聊状态机 + @ 检测的离线自测（不需要微信、不需要网络、不需要凭据）。

跑法：python group_selftest.py

为什么要有它：这两个模块决定"什么时候花钱调用模型"。规则写错了的症状是
**要么该回的不回（用户觉得它死了），要么不该回的乱回（群里刷屏被封号）**——
两种都是上线后很难现场调试的问题，所以必须在离线阶段把判据钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wechat_bridge.at_detect import (                                    # noqa: E402
    addresses_other,
    describe,
    extract_atuserlist,
    is_at_me,
    strip_sender_prefix,
)
from wechat_bridge.group_state import (                                  # noqa: E402
    ACTIVE,
    IDLE,
    IGNORE,
    RESPOND,
    GroupPolicy,
    GroupScheduler,
)
from wechat_bridge.adapters.hook_wechatauto import WechatautoHookAdapter   # noqa: E402

MY_WXID = "wxid_me_0001"
GROUP = "12345678@chatroom"
OTHER_GROUP = "87654321@chatroom"
NICKS = ["DeepSeek", "小深"]


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return ok


class Clock:
    """可控时钟：状态机的所有超时判断都基于它，测试才能确定。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------- @ 检测

def test_at_detect() -> bool:
    ok = True
    print("-- @ 检测：XML 形态（权威判据）--")

    r = is_at_me(f"{MY_WXID}:\n你好<msgsource><atuserlist>{MY_WXID}</atuserlist></msgsource>", MY_WXID, NICKS)
    ok &= check("atuserlist 含我 → 命中", r.matched and r.how == "xml", describe(r))

    r = is_at_me("wxid_a:\n你好<msgsource><atuserlist>wxid_b,wxid_c</atuserlist></msgsource>", MY_WXID, NICKS)
    ok &= check("atuserlist 不含我 → 不命中（且明确说明是 @ 别人）",
                not r.matched and "不含我" in r.evidence, r.evidence)

    r = is_at_me(f"{MY_WXID}:\n<msgsource><atuserlist>announcement</atuserlist></msgsource>", MY_WXID, NICKS)
    ok &= check("@所有人 标记 → 命中且标记为 at_all", r.matched and r.at_all, describe(r))

    r = is_at_me(f"{MY_WXID}:\n<msgsource><atuserlist>{MY_WXID},wxid_other</atuserlist></msgsource>", MY_WXID, NICKS)
    ok &= check("列表里我排在别人前面也能认出", r.matched and not r.at_all and r.at_userlist == [MY_WXID, "wxid_other"],
                str(r.at_userlist))

    r = is_at_me("wxid_a:\n<msgsource><atuserlist>wxid_x\n  ,  wxid_y</atuserlist></msgsource>", MY_WXID, NICKS)
    ok &= check("列表里的换行与空格会被清掉", r.at_userlist == ["wxid_x", "wxid_y"], str(r.at_userlist))

    print("\n-- @ 检测：文本形态（兜底判据）--")
    r = is_at_me("wxid_a:\n@DeepSeek 在不在", MY_WXID, NICKS)
    ok &= check("正文 @ 我的群昵称 → 命中", r.matched and r.how == "text", describe(r))

    r = is_at_me("wxid_a:\n@小深 看看这个", MY_WXID, NICKS)
    ok &= check("别名也能命中", r.matched and r.how == "text", describe(r))

    r = is_at_me("wxid_a:\n@隔壁老王 你看看", MY_WXID, NICKS)
    ok &= check("@别人 → 不命中（不能把别人的对话抢过来）", not r.matched, r.evidence)

    r = is_at_me("wxid_a:\nDeepSeek 是什么", MY_WXID, NICKS)
    ok &= check("只是提到名字、没有 @ → 不命中", not r.matched)

    r = is_at_me("wxid_a:\n@DeepSeek\u200b 在不在", MY_WXID, NICKS)
    ok &= check("名字后面跟零宽字符也能命中", r.matched, describe(r))

    print("\n-- @ 检测：边界 --")
    ok &= check("空内容不炸", not is_at_me("", MY_WXID, NICKS))
    ok &= check("None 不炸", not is_at_me(None, MY_WXID, NICKS))
    ok &= check("没有昵称列表时不误判", not is_at_me("wxid_a:\n@谁 在吗", MY_WXID, None))
    ok &= check("BOM 不影响", is_at_me("\ufeffwxid_a:\n@DeepSeek 在吗", MY_WXID, NICKS).matched)

    ok &= check("剥前缀：正文只剩内容",
                strip_sender_prefix("wxid_abc_123:\n你好啊") == "你好啊",
                repr(strip_sender_prefix("wxid_abc_123:\n你好啊")))
    ok &= check("剥前缀：非群消息原样返回",
                strip_sender_prefix("你好啊") == "你好啊")
    ok &= check("提取 atuserlist", extract_atuserlist("<atuserlist>a,b</atuserlist>") == ["a", "b"])
    ok &= check("没有 atuserlist 时返回空表", extract_atuserlist("普通文本") == [])
    ok &= check("AT_ALL_MARKERS 可被覆盖",
                is_at_me("<atuserlist>ALLHANDS</atuserlist>", MY_WXID, NICKS,
                         at_all_markers=["allhands"]).at_all)

    # ---- 真实数据校准：这些样例**原样**来自 2026-10-16 真机 dump 出来的群消息 ----
    # 实测两点（都是猜不出来的）：
    #   ① 群里 @ 机器人时正文就是纯文本 `@de`（de 是账号昵称），**没有 <atuserlist>**；
    #   ② 名字后面跟的是 **U+2005（四分之一全角空格）**——肉眼看不出，但真实存在。
    # 下面这几条把「实测形态」钉进回归，改判断逻辑时会立刻报警。
    print("\n-- 真实数据校准（微信 4.1.15.13 实测）--")
    real = "wxid_other_user:\n@de\u2005"           # 原样来自 dump_raw_group 的输出
    result = is_at_me(real, MY_WXID, ["de"])
    ok &= check("实测形态：`@de` + U+2005 → 命中", result.matched and result.how == "text",
                describe(result))
    ok &= check("实测形态：@ 后面**没有** XML，也没被误当 atuserlist",
                extract_atuserlist(real) == [])
    real2 = "wxid_other_user:\n@de\u2005你好"
    ok &= check("实测形态：`@de` + U+2005 + 正文 → 命中",
                is_at_me(real2, MY_WXID, ["de"]).matched)
    ok &= check("实测形态：群里的普通消息不误判（`消息2`）",
                not is_at_me("wxid_other_user:\n消息2", MY_WXID, ["de"]).matched)
    ok &= check("中文紧跟其后也命中（中文群里没人加空格）",
                is_at_me("wxid_a:\n@de你看看这个", MY_WXID, ["de"]).matched)
    ok &= check("`@demo` 不会被短昵称 de 误命中",
                not is_at_me("wxid_a:\n@demo 你看", MY_WXID, ["de"]).matched)
    ok &= check("`@de` 后面跟标点也命中",
                is_at_me("wxid_a:\n@de，在不在", MY_WXID, ["de"]).matched)
    ok &= check("长昵称优先（不会被子串抢先）",
                is_at_me("wxid_a:\n@DeepSeek 在吗", MY_WXID, ["de", "DeepSeek"]).evidence == "@DeepSeek",
                is_at_me("wxid_a:\n@DeepSeek 在吗", MY_WXID, ["de", "DeepSeek"]).evidence)

    # ---- 「这条 @ 的是别人」——追问链里靠它闭嘴。样例**原样取自 2026-10-16 真机留档** ----
    # 当时机器人对着这句回了「你俩的事你俩聊哈」：话说得得体，但**它本不该开口**。
    print("\n-- 「在叫别人」判据（真机样例）--")
    other_real = "wxid_other_user:\n@阿乙 还有这事跟你有什么关"
    ok &= check("真机样例：@ 别人 → 判为「在叫别人」",
                addresses_other(other_real, MY_WXID, ["de"]))
    ok &= check("@ 我 → **不是**在叫别人", not addresses_other(real, MY_WXID, ["de"]))
    ok &= check("没有 @ 的普通消息 → 不是", not addresses_other("wxid_a:\n消息2", MY_WXID, ["de"]))
    ok &= check("@所有人 → 不算「在叫别人」（它另有 ignore_at_all 开关管）",
                not addresses_other("<msgsource><atuserlist>announcement</atuserlist></msgsource>",
                                    MY_WXID, ["de"]))
    return ok


# --------------------------------------------- 真机链路（监听回调 → Message）

def test_live_listener_path() -> bool:
    """**真机实际执行的那条链路**：库 Listener 回调 → 原始行 dict → `_row_to_message()`。

    为什么单独一节：2026-10-16 真机上群里 @ 机器人，桥接日志一直是
    `mentions_me=False`、`at_note=` 空，机器人纹丝不动。原因不是判据写错——
    `is_at_me()` 单独测全绿——而是 **@ 判定只加在了轮询路径 `_to_message()` 里，
    真机走的却是监听回调路径 `_row_to_message()`**，那段判定代码从来没被执行过。
    图片放行（M3）当时踩的是同一个坑。

    教训：**测判据 ≠ 测链路**。本文件其余用例都在测判据函数，真能拦住线上事故的是
    这一节——它必须从真机入口进，喂真机原始行 dict。
    """
    ok = True

    def fresh(**kw) -> WechatautoHookAdapter:
        a = WechatautoHookAdapter(listen_uids=[GROUP], **kw)
        a._self_wxid = MY_WXID
        a._self_nick = "de"
        return a

    def row(content, mtype="文本", sender="wxid_other_user",
            chat=GROUP, local_id=9) -> dict:
        return {"username": chat, "type": mtype, "content": content,
                "sender_username": sender, "local_id": local_id,
                "create_time": 1700000000}

    adapter = fresh(accept_images=True)

    print("\n-- 真机链路：监听回调行 → Message（@ 判定必须在这里生效）--")
    msg = adapter._row_to_message(row("wxid_other_user:\n@de\u2005"))
    ok &= check("真机 @ 原文 → mentions_me=True",
                msg is not None and msg.mentions_me is True,
                f"mentions_me={getattr(msg, 'mentions_me', None)}")
    ok &= check("同时留下判定依据（审计里要查得到为什么理我）",
                msg is not None and bool(msg.at_note),
                f"at_note={getattr(msg, 'at_note', '')!r}")

    # sender_username 缺失时（库对 4.x 群消息的 sender_id 明确说过不可靠），
    # 发送者是靠正文前缀切出来的——这条路上 @ 判定同样不能丢。
    msg2 = adapter._row_to_message(row("wxid_other_user:\n@de\u2005在不在", sender=""))
    ok &= check("sender_username 缺失、靠正文前缀还开发送者 → 仍然命中",
                msg2 is not None and msg2.mentions_me is True,
                f"mentions_me={getattr(msg2, 'mentions_me', None)}")
    ok &= check("喂给模型的正文里不该留着 wxid 前缀，且 @ 被归一成 `@我`",
                msg2 is not None and not msg2.text.startswith("wxid_") and "@我" in msg2.text,
                repr(getattr(msg2, "text", None)))

    ok &= check("真机普通消息不误判（`消息2`）",
                adapter._row_to_message(row("wxid_other_user:\n消息2")).mentions_me is False)
    ok &= check("群里自己发的话不算被叫（否则会自问自答）",
                adapter._row_to_message(row("wxid_me_0001:\n@de 自言自语",
                                           sender=MY_WXID)).mentions_me is False)

    print("\n-- 真机链路：图片放行（M3 在同一处也漏过）--")
    pic = adapter._row_to_message(row("[图片]", mtype="图片"))
    ok &= check("图片消息能进来（不再被当非文本丢掉）",
                pic is not None and pic.kind == "image")
    ok &= check("图片正文是占位符（真取图是懒加载，等要回才取）",
                pic is not None and pic.text == "[图片]", repr(getattr(pic, "text", None)))
    ok &= check("accept_images=False 时图片仍被挡在外面（默认关，不许偷跑）",
                fresh()._row_to_message(row("[图片]", mtype="图片")) is None)
    ok &= check("语音/文件等仍然交给人工",
                adapter._row_to_message(row("x", mtype="语音")) is None)

    print("\n-- 真机链路：私聊不受群聊逻辑影响 --")
    ok &= check("私聊消息 mentions_me 保持 False（私聊另有自己的开关）",
                adapter._row_to_message(row("你好", sender="wxid_x",
                                           chat="wxid_x")).mentions_me is False)

    print("\n-- 两条路径判据必须一致（改一条忘另一条＝线上装死）--")
    for content, expected in (("wxid_other_user:\n@de\u2005", True),
                              ("wxid_other_user:\n消息3", False)):
        from_row = bool(adapter._row_to_message(row(content)).mentions_me)
        from_poll = bool(is_at_me(content, MY_WXID, ["de"]).matched)
        ok &= check(f"监听路径与轮询路径一致（{'命中' if expected else '不命中'}）",
                    from_row == from_poll == expected,
                    f"row={from_row} poll={from_poll} 期望={expected}")

    class _FakeWx:                     # 只为跨过 `self._wx is None` 那道早退
        _db = None

    adapter._wx = _FakeWx()
    ok &= check("download_image 能吃下监听路径的 dict（缺 local_id 时安全返回 None，不抛异常）",
                adapter.download_image(GROUP, {"username": GROUP}) is None)
    adapter._wx = None

    print("\n-- 真机链路：@ 别人的消息要打上 at_others --")
    m_other = adapter._row_to_message(row("wxid_other_user:\n@阿乙 还有这事跟你有什么关"))
    ok &= check("真机原文（@ 别人）→ at_others=True 且 mentions_me=False",
                m_other is not None and m_other.at_others is True and m_other.mentions_me is False,
                f"at_others={getattr(m_other, 'at_others', None)} "
                f"mentions_me={getattr(m_other, 'mentions_me', None)}")
    m_mine = adapter._row_to_message(row("wxid_other_user:\n@de\u2005在不在"))
    ok &= check("@ 我 → at_others=False（两件事不能混为一谈）",
                m_mine is not None and m_mine.at_others is False and m_mine.mentions_me is True)

    print("\n-- 真机链路：发送缓存必须被接上 --")
    # 真机上第二个坑：`send()` 只在 _chat_cache 里找 Chat 对象，而监听回调路径
    # **天生没有** Chat 对象（回调给的是原始行 dict）。不补这一步，症状是
    # "@ 判定对了、也决定要回，然后发送环节『拒绝发送：会话不在缓存里』"，
    # 排查时很容易误以为是发送模块坏了。这里用打桩确认这条线确实接上了。
    called: list[str] = []
    adapter3 = fresh()
    adapter3._ensure_chat = lambda cid: (called.append(cid), True)[1]
    adapter3._row_to_message(row("wxid_other_user:\n@de\u2005"))
    ok &= check("收到消息时会去准备发送缓存（_ensure_chat 被调用）",
                called == [GROUP], f"called={called}")
    # 私聊、群聊都要准备：发送缓存与群聊逻辑无关。
    called.clear()
    adapter3._row_to_message(row("你好", sender="wxid_x", chat="wxid_x"))
    ok &= check("私聊也会准备发送缓存", called == ["wxid_x"], f"called={called}")
    ok &= check("没连接微信时 _ensure_chat 安全返回 False（不抛异常）",
                fresh()._ensure_chat(GROUP) is False)

    return ok


# ---------------------------------------------------------------- 状态机

def test_scheduler() -> bool:
    ok = True
    clock = Clock()
    sched = GroupScheduler(GroupPolicy(idle_timeout_sec=300, followup_window_sec=90,
                                       ring_size=5, max_chain_turns=2), clock=clock)

    print("-- 激活 --")
    ok &= check("休眠时普通消息 → 只记不响",
                sched.observe(GROUP, "wxid_a", "大家在聊什么", at_me=False) == IGNORE)
    ok &= check("此时仍是休眠", sched.state_of(GROUP) == IDLE)
    ok &= check("休眠期照样写了缓冲（被 @ 时要靠它回溯）",
                len(sched.context(GROUP)) == 1)

    ok &= check("被 @ → 响应", sched.observe(GROUP, "wxid_a", "@我 在不在", at_me=True) == RESPOND)
    ok &= check("状态变激活", sched.state_of(GROUP) == ACTIVE)

    print("\n-- 激活态下的克制（关键：不是每条都回）--")
    ok &= check("激活后别人的闲聊 → 不回",
                sched.observe(GROUP, "wxid_b", "今天天气不错", at_me=False) == IGNORE)
    ok &= check("但还是记进了缓冲", len(sched.context(GROUP)) == 3)

    print("\n-- 追问窗口 --")
    clock.advance(5)
    sched.note_bot_spoke(GROUP)                      # 我刚回了话
    clock.advance(10)
    ok &= check("我发完话 10 秒内有人接话 → 回",
                sched.observe(GROUP, "wxid_a", "那你说说看", at_me=False) == RESPOND)
    clock.advance(10)
    ok &= check("链条第 2 轮还能接", sched.observe(GROUP, "wxid_a", "嗯？", at_me=False) == RESPOND)
    clock.advance(10)
    ok &= check("超过 max_chain_turns 就不再接（防自我循环）",
                sched.observe(GROUP, "wxid_a", "说话啊", at_me=False) == IGNORE)
    # 日志要能说出**真实原因**：否则"链长用尽"会被读成"AI 不认人"（真机踩过一整轮）。
    ok &= check("链长用尽时原因码是 chain_limit（不是笼统的「不是在叫我」）",
                sched.skip_reason(GROUP) == "chain_limit", sched.skip_reason(GROUP))

    print("\n-- 忽略原因码（日志措辞要能指向真因）--")
    ok &= check("@了别人 → at_others",
                sched.observe(GROUP, "wxid_a", "@张三 你来", at_me=False, at_others=True) == IGNORE
                and sched.skip_reason(GROUP) == "at_others", sched.skip_reason(GROUP))
    clock.advance(1)
    ok &= check("换了个人接话 → followup_other_sender",
                sched.observe(GROUP, "wxid_b", "我也说一句", at_me=False) == IGNORE
                and sched.skip_reason(GROUP) == "followup_other_sender", sched.skip_reason(GROUP))
    ok &= check("自己发的不触发回应 → self",
                sched.observe(GROUP, "me", "我回完了", is_self=True) == IGNORE
                and sched.skip_reason(GROUP) == "self", sched.skip_reason(GROUP))

    print("\n-- max_chain_turns=0 表示不限制 --")
    clock0 = Clock()
    sched0 = GroupScheduler(GroupPolicy(idle_timeout_sec=300, followup_window_sec=90,
                                        ring_size=5, max_chain_turns=0), clock=clock0)
    sched0.observe(GROUP, "wxid_a", "@我 在吗", at_me=True)
    sched0.note_bot_spoke(GROUP)
    rounds = 0
    for _ in range(8):
        clock0.advance(5)
        if sched0.observe(GROUP, "wxid_a", "接着说", at_me=False) == RESPOND:
            rounds += 1
            sched0.note_bot_spoke(GROUP)
    ok &= check("0 = 不限制链条（真人对话能一直聊下去）", rounds == 8, f"接了 {rounds}/8 轮")

    print("\n-- @ 能重置链条 --")
    ok &= check("再次被 @ → 回，且链条清零",
                sched.observe(GROUP, "wxid_a", "@我 回来", at_me=True) == RESPOND)
    clock.advance(1)
    sched.note_bot_spoke(GROUP)
    clock.advance(5)
    ok &= check("被 @ 之后追问窗口重新可用",
                sched.observe(GROUP, "wxid_a", "继续", at_me=False) == RESPOND)

    print("\n-- 休眠 --")
    clock.advance(120)
    ok &= check("静默 120 秒（< 300）仍醒着", sched.tick() == [] and sched.state_of(GROUP) == ACTIVE)
    clock.advance(200)
    fired = sched.tick()
    ok &= check("累计超过 idle_timeout → 休眠", fired == [GROUP], str(fired))
    ok &= check("休眠后再来普通消息只记不响",
                sched.observe(GROUP, "wxid_a", "还在吗", at_me=False) == IGNORE)

    print("\n-- 自己发的消息 --")
    sched2 = GroupScheduler(GroupPolicy(idle_timeout_sec=300), clock=Clock())
    ok &= check("自己发的 → 永不触发回应",
                sched2.observe(GROUP, MY_WXID, "我说的", is_self=True) == IGNORE)
    ok &= check("但会进缓冲（AI 需要知道自己的话）",
                any(item["self"] for item in sched2.context(GROUP)))

    print("\n-- 观察期（新群先看不说）--")
    sched3 = GroupScheduler(GroupPolicy(observe_first_msgs=3), clock=Clock())
    ok &= check("观察期第 1 条 @ 也不回",
                sched3.observe(GROUP, "wxid_a", "@我 在吗", at_me=True) == IGNORE)
    sched3.observe(GROUP, "wxid_a", "2", at_me=False)
    sched3.observe(GROUP, "wxid_a", "3", at_me=False)
    ok &= check("观察期结束，@ 开始生效",
                sched3.observe(GROUP, "wxid_a", "@我 在吗", at_me=True) == RESPOND)

    print("\n-- 主动插话开关 --")
    sched4 = GroupScheduler(GroupPolicy(proactive=True), clock=Clock())
    sched4.observe(GROUP, "wxid_a", "@我", at_me=True)
    ok &= check("proactive=True 时激活态任何消息都回",
                sched4.observe(GROUP, "wxid_b", "随便说点什么", at_me=False) == RESPOND)
    ok &= check("但休眠态依然不理", sched4.observe(OTHER_GROUP, "wxid_b", "hi") == IGNORE)

    print("\n-- 环形缓冲与多群隔离 --")
    sched5 = GroupScheduler(GroupPolicy(ring_size=3, idle_timeout_sec=300), clock=Clock())
    for i in range(10):
        sched5.observe(GROUP, "wxid_a", f"msg{i}")
    ok &= check("缓冲只留最后 N 条",
                [i["text"] for i in sched5.context(GROUP)] == ["msg7", "msg8", "msg9"],
                str([i["text"] for i in sched5.context(GROUP)]))
    sched5.observe(GROUP, "wxid_a", "@我", at_me=True)
    ok &= check("群 A 激活不影响群 B",
                sched5.state_of(GROUP) == ACTIVE and sched5.state_of(OTHER_GROUP) == IDLE)
    ok &= check("active_chats 只列醒着的", sched5.active_chats() == [GROUP])

    print("\n-- 容量上限（长时间运行不能无限涨内存）--")
    sched6 = GroupScheduler(GroupPolicy(max_chats=3), clock=Clock())
    for i in range(6):
        sched6.observe(f"g{i}@chatroom", "wxid_a", "x")
    snap = sched6.snapshot()
    ok &= check("群数量被限制在 max_chats", len(snap["chats"]) == 3, str(list(snap["chats"])))
    ok &= check("淘汰的是最久没动的", "g5@chatroom" in snap["chats"] and "g0@chatroom" not in snap["chats"])

    print("\n-- 快照与提示词渲染 --")
    sched7 = GroupScheduler(GroupPolicy(), clock=Clock())
    sched7.observe(GROUP, "wxid_a", "你好", at_me=True)
    sched7.note_bot_spoke(GROUP)
    sched7.observe(GROUP, MY_WXID, "你也好", is_self=True)
    lines = sched7.build_prompt_lines(GROUP, name_of=lambda uid: "阿伟" if uid == "wxid_a" else uid)
    ok &= check("提示词行渲染正确（自己显示为「我」）",
                lines == ["阿伟：你好", "我：你也好"], str(lines))
    ok &= check("快照含状态与策略", snap["policy"]["idle_timeout_sec"] == 300
                and sched7.snapshot()["chats"][GROUP]["state"] == ACTIVE)
    return ok


def test_multi_person() -> bool:
    """**多人同时说话时，不能对每个人都回一句**（用户提出的场景，旧实现确实会）。

    场景：甲 @ 了我、我刚回完一句；90 秒内乙和丙也各说了一句——他们是在**互相**
    聊天，不是在跟我说话。旧实现的追问判据**只看时间不看人**，于是我会一句句接上去，
    在一个活跃群里表现成"回一句之后开始对每个人点头"。
    """
    ok = True
    clock = Clock()
    sched = GroupScheduler(GroupPolicy(idle_timeout_sec=300, followup_window_sec=90), clock=clock)

    print("\n-- 多人同时说话：只接「刚才跟我说话的那个人」--")
    ok &= check("甲 @ 我 → 回", sched.observe(GROUP, "wxid_a", "@我 在不在", at_me=True) == RESPOND)
    sched.note_bot_spoke(GROUP)
    clock.advance(10)
    ok &= check("甲接着追问 → 回（还是同一个人）",
                sched.observe(GROUP, "wxid_a", "那你说说", at_me=False) == RESPOND)
    sched.note_bot_spoke(GROUP)
    clock.advance(5)
    ok &= check("**乙在窗口内说话 → 不回**（他不是在跟我说话）",
                sched.observe(GROUP, "wxid_b", "我觉得也是", at_me=False) == IGNORE)
    ok &= check("**丙在窗口内说话 → 也不回**",
                sched.observe(GROUP, "wxid_c", "同意", at_me=False) == IGNORE)
    clock.advance(5)
    ok &= check("甲继续说 → 仍然回（对话没断）",
                sched.observe(GROUP, "wxid_a", "在吗", at_me=False) == RESPOND)

    print("\n-- 乙 @ 我之后，对话对象换成乙 --")
    sched.note_bot_spoke(GROUP)
    clock.advance(5)
    ok &= check("乙 @ 我 → 回，且对象换成乙",
                sched.observe(GROUP, "wxid_b", "@我 帮我看看", at_me=True) == RESPOND)
    sched.note_bot_spoke(GROUP)
    clock.advance(5)
    ok &= check("乙接着问 → 回",
                sched.observe(GROUP, "wxid_b", "就是这个", at_me=False) == RESPOND)
    ok &= check("**此时甲插话 → 不回**（现在跟我说话的是乙）",
                sched.observe(GROUP, "wxid_a", "我也说一句", at_me=False) == IGNORE)

    print("\n-- 分不清是谁在说话时，按「不接」处理 --")
    sched2 = GroupScheduler(GroupPolicy(), clock=Clock())
    sched2.observe(GROUP, "wxid_a", "@我", at_me=True)
    sched2.note_bot_spoke(GROUP)
    ok &= check("sender_id 解不出来（空）→ 不回（宁可少接一句也不乱插嘴）",
                sched2.observe(GROUP, "", "在吗", at_me=False) == IGNORE)

    print("\n-- 关掉开关就退回旧行为（明确选择，给「只有你和我」的小群用） --")
    sched3 = GroupScheduler(GroupPolicy(followup_same_sender_only=False), clock=Clock())
    sched3.observe(GROUP, "wxid_a", "@我", at_me=True)
    sched3.note_bot_spoke(GROUP)
    ok &= check("关掉后：窗口内别人说话也会接",
                sched3.observe(GROUP, "wxid_b", "我也说", at_me=False) == RESPOND)

    print("\n-- 追问链里他 @ 了别人 ⇒ 闭嘴（真机踩到过） --")
    sched4 = GroupScheduler(GroupPolicy(), clock=Clock())
    sched4.observe(GROUP, "wxid_a", "@我 在不在", at_me=True)
    sched4.note_bot_spoke(GROUP)
    ok &= check("**甲 @ 别人（不是我）→ 不回**（他在跟别人说话）",
                sched4.observe(GROUP, "wxid_a", "@张三 这事跟你有什么关系", at_others=True) == IGNORE)
    ok &= check("紧接着甲正常追问 → 仍然回（没有把整条对话掐断）",
                sched4.observe(GROUP, "wxid_a", "你倒是说话啊", at_me=False) == RESPOND)

    sched5 = GroupScheduler(GroupPolicy(proactive=True), clock=Clock())
    sched5.observe(GROUP, "wxid_a", "@我", at_me=True)
    sched5.note_bot_spoke(GROUP)
    ok &= check("连「主动插话」模式也不给例外：别人被 @ 时照样闭嘴",
                sched5.observe(GROUP, "wxid_a", "@张三 你说呢", at_others=True) == IGNORE)
    return ok


def main() -> int:
    print("=" * 64)
    print("@ 检测自测")
    print("=" * 64)
    ok = test_at_detect()
    print("\n" + "=" * 64)
    print("真机链路自测（监听回调 → Message）")
    print("=" * 64)
    ok &= test_live_listener_path()
    print("\n" + "=" * 64)
    print("群聊状态机自测")
    print("=" * 64)
    ok &= test_scheduler()
    print("\n" + "=" * 64)
    print("多人同时说话：追问只认「跟我说话的那个人」")
    print("=" * 64)
    ok &= test_multi_person()
    print("\n" + "=" * 64)
    print("结论：" + ("群聊状态机与 @ 检测自测通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
