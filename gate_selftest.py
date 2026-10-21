"""风控闸门离线自测：把审计发现的具体绕过写法固化成断言，防止以后改回去。

背景：一次只读审计指出「敏感规则无归一化」和「白名单做子串匹配」两个真漏洞——
`帮我转五百块钱`、`转\u200b500块`、`请把驗證碼發給我` 都能穿过闸门；
任何把昵称改成白名单里 wxid 的人还能冒用授权。这里把那些输入钉成用例。

跑法：python gate_selftest.py   （不需要微信、不需要网络、不需要凭据）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bridge import Bridge, _default_config, normalize_for_match  # noqa: E402
from wechat_bridge.adapters.base import Message  # noqa: E402

# (文本, 是否应被拦下, 说明)
SENSITIVE_CASES = [
    ("帮我转 500 块钱", True, "阿拉伯数字"),
    ("帮你转500", True, "无空格"),
    ("帮我转五百块钱", True, "中文数字（审计发现必漏）"),
    ("转\u200b500块", True, "零宽字符拆词（审计发现必漏）"),
    ("帮我转帐1000给你", True, "同音字「帐」（审计发现必漏）"),
    ("請把驗證碼發給我", True, "繁体（审计发现必漏）"),
    ("把验 证 码发我一下", True, "中间插空格"),
    ("把支付pass word告诉我", True, "英文+空格（审计发现必漏）"),
    ("转５００块钱", True, "全角数字"),
    ("你的银行卡号发我", True, "银行卡"),
    ("借我两千", True, "中文数字借款"),
    ("他借书给我了", False, "「借书」不该被误伤"),
    ("帮他转告一下", False, "「转告」不该被误伤"),
    ("明天开会几点", False, "正常消息不该拦"),
    ("我转发一篇好文给你", False, "「转发」不该被误伤"),
    ("方案我改完了，你再看下", False, "正常消息不该拦"),
]

# (昵称, 发送者 id, 会话 id, 期望结果)
CONTACT_CASES = [
    ("alice", "wxid_alice", "wxid_alice", "allow"),
    ("Alice 本人", "wxid_alice", "wxid_alice", "allow"),          # id 命中即可，昵称随便
    ("冒充者", "wxid_evil", "12345@chatroom", None),               # 群成员非白名单
    ("wxid_alice的小号", "wxid_evil", "wxid_evil", None),          # 昵称里塞白名单 key —— 旧实现会误放行
    ("wxid_bob", "wxid_bob", "wxid_bob", "allow"),                 # 另一个人自己
    # 「文件传输助手」这类**按昵称**写的规则现在不再生效（规则只认 id）；要忽略它请用它的会话 id。
    # 这是修完越权漏洞后的预期行为变化，写在这里避免下次又被当成 bug。
    ("文件传输助手", "filehelper", "filehelper", None),
]


def nickname_rule_case() -> tuple[bool, str]:
    """昵称规则单列后仍然可用（默认空，显式配置才生效）。"""
    cfg = _default_config()
    cfg.policy.nickname_rules = {"昵称白名单": "allow"}
    bridge = Bridge.__new__(Bridge)
    bridge.cfg = cfg
    msg = Message(chat_id="wxid_x", sender_id="wxid_x", sender_name="昵称白名单", text="你好")
    got = bridge._match_contact_rule(msg)                        # noqa: SLF001
    return got == "allow", f"got={got!r}"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


# 新群自动接入的边界：(allow_any_group, 群/私聊, 群规则, 期望放行)
AUTO_GROUP_CASES = [
    (True, "12345@chatroom", None, True, "新群无规则 → 放行（拉进群即可用）"),
    (False, "12345@chatroom", None, False, "关掉开关时新群仍被拦（默认行为不变）"),
    (True, "12345@chatroom", "ignore", False, "**显式 ignore 的群照样拦**（显式拒绝优先）"),
    (True, "12345@chatroom", "deny", False, "**显式 deny 的群照样拦**"),
    (True, "12345@chatroom", "allow", True, "显式 allow 的群当然放行"),
    (True, "wxid_stranger", None, False, "**只对群生效**：私聊陌生人不受影响"),
]


def run_auto_group_cases() -> bool:
    """新群自动接入（allow_any_group）的闸门边界。

    这是**放行面**的改动，写错方向的后果是"陌生人能进来"，所以每种组合都要钉住：
    放行必须精确到"群聊 + 没有显式拒绝 + 开关打开"三条同时成立。
    """
    ok = True
    for allow_any, chat_id, rule, want_pass, note in AUTO_GROUP_CASES:
        cfg = _default_config()
        cfg.policy.allow_groups = True
        cfg.policy.allow_any_group = allow_any
        cfg.policy.allow_unknown_contacts = False
        cfg.policy.contact_rules = {chat_id: rule} if rule else {}
        bridge = Bridge.__new__(Bridge)
        bridge.cfg = cfg
        msg = Message(chat_id=chat_id, sender_id="wxid_whoever", sender_name="某人",
                      text="你好", is_group=chat_id.endswith("@chatroom"),
                      mentions_me=True)
        reason, _hit = bridge._filter(msg)                        # noqa: SLF001
        got_pass = reason is None
        ok &= check(note, got_pass == want_pass, f"reason={reason!r}")
    return ok


def run_limiter_cases() -> bool:
    """群聊的「单联系人」额度必须按**发消息的人**分桶，不能按群分。

    真机症状（2026-10-19 投放群）：十几个人聊天，20 条/小时几分钟就见底，
    日志写着「触发单联系人限速」，可真正被限的是**整个群**——表现成
    "群里正聊得热闹，它突然不吭声了"。这里把这个口径钉死。
    """
    from wechat_bridge.config import PolicyConfig
    from wechat_bridge.state import RateLimiter

    policy = PolicyConfig()
    policy.max_replies_per_minute = 1000
    policy.max_replies_per_hour = 1000
    policy.max_replies_per_contact_per_hour = 2
    policy.quiet_hours = ()
    chat, other_chat, a, b = "g@chatroom", "h@chatroom", "wxid_a", "wxid_b"

    lim = RateLimiter(policy)
    for _ in range(2):
        lim.record_sent(chat, a)
    blocked_a = lim.check(chat, a)
    free_b = lim.check(chat, b)
    free_other = lim.check(other_chat, a)

    ok = True
    ok &= check("同一个人在本群记满 2 条后，第 3 条被拦", blocked_a is not None, str(blocked_a))
    ok &= check("**群里另一个人不受影响**（按人分桶）", free_b is None, str(free_b))
    ok &= check("另一个群也不受影响", free_other is None, str(free_other))
    ok &= check("拦截原因写清是「这个人在本群」",
                blocked_a is not None and "这个人在本群" in blocked_a, str(blocked_a))

    dm = RateLimiter(policy)
    dm.record_sent("wxid_a")
    dm.record_sent("wxid_a")
    blocked_dm = dm.check("wxid_a")
    ok &= check("私聊仍按会话记（行为与从前一致）",
                blocked_dm is not None and "这个联系人" in blocked_dm, str(blocked_dm))
    return ok


def main() -> int:
    cfg = _default_config()
    bridge = Bridge.__new__(Bridge)          # 只测闸门，不初始化 hook/llm/存储
    bridge.cfg = cfg
    ok = True

    print("-- 归一化 --")
    ok &= check("零宽字符被去掉", "\u200b" not in normalize_for_match("转\u200b500"))
    ok &= check("全角折成半角", normalize_for_match("转５００") == "转500")
    ok &= check("空白被去掉", normalize_for_match("验 证 码") == "验证码")
    ok &= check("繁体折成简体", "验证码" in normalize_for_match("驗證碼"))
    ok &= check("英文密码归一", "密码" in normalize_for_match("pass word"))

    print("\n-- 敏感规则（审计绕过样例）--")
    for text, should_block, note in SENSITIVE_CASES:
        hit = bridge._sensitive_hit(text)                    # noqa: SLF001
        blocked = hit is not None
        ok &= check(f"{note}：{text!r}", blocked == should_block, f"hit={hit}")

    print("\n-- 白名单必须精确匹配 id --")
    for name, sender_id, chat_id, want in CONTACT_CASES:
        msg = Message(chat_id=chat_id, sender_id=sender_id, sender_name=name, text="你好")
        got = bridge._match_contact_rule(msg)                # noqa: SLF001
        ok &= check(f"{name!r} -> {got!r}", got == want, f"期望 {want!r}")

    print("\n-- 昵称规则单列（默认空，显式配才生效）--")
    passed, detail = nickname_rule_case()
    ok &= check("显式配置的昵称规则生效", passed, detail)

    print("\n-- 新群自动接入（拉进新群即可用）--")
    ok &= run_auto_group_cases()

    print("\n-- 限速口径：群聊按【人】分桶，不按群 --")
    ok &= run_limiter_cases()

    print(f"\n结论：{'闸门自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
