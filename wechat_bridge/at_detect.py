"""判断群消息是不是在 @ 我。

**为什么单独一个模块**：微信群 @ 的存储形态**没有权威文档**，只能从真实消息里反推。
写这个文件时，开发机上那个微信号还没有任何群，所以这里的分支是
"按已知形态实现 + 可诊断 + 可校准"，而不是"已经在真机上验证过"。

**实测结论（2026-10-16，微信 4.1.15.13）**：群里 @ 机器人时，数据库里的正文是
**纯文本 `@de\u2005`**（`de` 是账号昵称，`\u2005` 是微信插进去的**四分之一全角空格**，
肉眼看不出）——**没有 `<atuserlist>` 那段 XML**。
所以：**文本判据才是这条路上的主力，XML 判据是留给别的客户端版本的保险**。

两种形态都要覆盖：

    A. XML 形态：正文里带 ``<msgsource>`` / ``<sysmsg>`` 之类的附加 XML，
       其中 ``<atuserlist>`` 是被 @ 的人的 wxid 列表（逗号分隔）。**本机实测未出现**。
    B. 文本形态：正文里直接出现 ``@我的群昵称``。**本机实测就是这个**。

以及第三种：**@所有人**。它在 ``atuserlist`` 里的表示各版本不一致
（``announcement`` / ``notify@all`` / ``@all`` 都见过），所以用一组候选标记去匹配。

**重新校准的方式**：拿到一条真实的群 @ 消息后跑 ``python dump_raw_group.py``，
把原始 ``message_content`` 打出来对照本文件的假设。规则不该靠猜，该由真实数据定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# 被 @ 的人的 wxid 列表。4.x 实测是逗号分隔；也见过用分隔符 `,` 前后带空格的。
_ATUSERS_RE = re.compile(r"<atuserlist>(.*?)</atuserlist>", re.S | re.I)
# 微信正文里可能带 BOM，标签前后也常有空白
_TAG_STRIP_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

# @所有人的候选标记（小写比较）。**故意写得宽**：宁可多认几个，
# 也不要因为版本差异漏掉"@所有人"，那会导致 AI 在全员通知下面接话。
AT_ALL_MARKERS = frozenset({
    "announcement", "notify@all", "@all", "all", "__all__", "atall",
})

# 正文里的会话前缀：群消息形如 `wxid_xxx:\n正文`，前缀是发送者，不是正文。
_SENDER_PREFIX_RE = re.compile(r"^wxid_[0-9a-zA-Z_]+:\s*\n?")


@dataclass
class AtResult:
    """一次 @ 判定的结果。

    ``how`` 取值：``xml``（命中 atuserlist）/ ``text``（命中字面量 @昵称）/
    ``at_all``（@所有人）/ ``""``（没命中）。
    ``evidence`` 是**命中的原文片段**——排查"为什么它没理我"时全靠这个。
    """

    matched: bool = False
    at_all: bool = False
    how: str = ""
    evidence: str = ""
    at_userlist: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:                       # 方便 `if at_me:`
        return self.matched


def strip_sender_prefix(content: str) -> str:
    """去掉群消息正文的 `wxid_xxx:\\n` 前缀。

    前缀是**发送者**，不是正文的一部分；拿去匹配 @昵称 会被它干扰，
    拿去喂模型也会让它以为有人在喊那个 wxid。
    """
    if not content:
        return ""
    return _SENDER_PREFIX_RE.sub("", content.lstrip("\ufeff"), count=1)


def extract_atuserlist(content: str) -> list[str]:
    """从正文里取出 `<atuserlist>` 的内容，拆成 wxid 列表；没有就返回空列表。"""
    if not content:
        return []
    match = _ATUSERS_RE.search(content)
    if not match:
        return []
    raw = _TAG_STRIP_RE.sub(r"\1", match.group(1)).strip()
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[,;，；]", raw) if part.strip()]


def is_at_me(
    raw_content: str,
    self_wxid: str = "",
    nicknames: Optional[Iterable[str]] = None,
    at_all_markers: Iterable[str] = AT_ALL_MARKERS,
) -> AtResult:
    """判断这条群消息是否在 @ 我（或 @所有人）。

    参数：
        raw_content: **数据库里的原始正文**（带 `wxid_xxx:` 前缀没关系，会剥掉）
        self_wxid:   本账号 wxid
        nicknames:   我在这个群里可能被叫到的名字（群昵称、昵称、备注都传进来）
        at_all_markers: @所有人 的候选标记，一般不用改

    判定顺序（先 XML 后文本）：XML 是**权威**的——文本里出现的 `@某某` 可能是
    引用别人的话，也可能是我自己的名字碰巧出现在句子中间。
    """
    content = strip_sender_prefix(raw_content or "")
    if not content:
        return AtResult()

    markers = {str(m).strip().lower() for m in at_all_markers if str(m).strip()}

    # ---- 1) XML 形态：atuserlist 是权威判据 ----
    listed = extract_atuserlist(content)
    if listed:
        lowered = [item.strip().lower() for item in listed]
        if self_wxid and any(item.lower() == self_wxid.lower() for item in listed):
            return AtResult(True, False, "xml", f"<atuserlist>{','.join(listed)}</atuserlist>", listed)
        hit = next((item for item in lowered if item in markers), "")
        if hit:
            return AtResult(True, True, "at_all", f"<atuserlist> 含 @所有人 标记 {hit!r}", listed)
        # 有 atuserlist 但都不是我 —— 说明这条是 @ 别人，**明确不是给我的**
        return AtResult(False, False, "", f"<atuserlist>{','.join(listed)}</atuserlist> 不含我", listed)

    # ---- 2) 文本形态：正文里出现 @我的名字（**本机实测走的就是这一条**）----
    # 长名字优先，避免短昵称抢先匹配掉长昵称。
    for name in sorted({str(n).strip() for n in (nicknames or []) if str(n).strip()},
                       key=len, reverse=True):
        # 两个都别写错：
        #   · **不能要求 @ 后面有空格** —— 中文群里没人写 "@de 你好"，都是 "@de你好"，
        #     要求空格会漏掉绝大多数真实的 @（这是实测才发现的）；
        #   · **也不能让 "@demo" 被 "de" 命中** —— 所以要求后面不是 ASCII 字母/数字/下划线。
        #     中文不算"延续字符"，于是 "@de的图" 依然能命中，这正是我们要的。
        pattern = re.compile(r"@" + re.escape(name) + r"(?![A-Za-z0-9_])")
        match = pattern.search(content)
        if match:
            return AtResult(True, False, "text", match.group(0).strip())

    # ---- 3) 兜底：正文里孤零零一个 @（常见于联系人名字解析不到时）----
    # 只在**整条消息几乎就是 @ 我**的时候才认，避免把 "@某人 你看" 误判成叫我
    stripped = content.strip()
    if stripped in ("@", "@我") or stripped.startswith("@"):
        head = stripped.split()[0] if stripped.split() else stripped
        if head in ("@", "@我"):
            return AtResult(True, False, "text", head)

    return AtResult()


def addresses_other(raw_content: str, self_wxid: str = "",
                    nicknames: Optional[Iterable[str]] = None) -> bool:
    """这条消息里的 `@` 是不是**冲着别人**去的。

    为什么需要它：追问链里"刚才那个人"再说话我就接——但他说的话可能是
    `@张三 这事跟你有什么关系`，那是在跟**别人**说话，我接过去就是插嘴。
    实测踩到过：机器人回了句"你俩的事你俩聊哈"——话说得得体，但**它本不该开口**。

    判据刻意宽松：**只要出现 `@` 且不是在叫我（也不是 @所有人），就算在叫别人**。
    宁可少接一句（我安静着不尴尬），也不要多接一句（@ 别人的话被我接过去就很尴尬）。
    代价：消息里出现 `@` 但并非 @ 人（例如邮箱地址）时也会被判成"在叫别人"——
    方向是**更保守**，可以接受。
    """
    content = strip_sender_prefix(raw_content or "")
    if not content or "@" not in content:
        return False
    return not is_at_me(content, self_wxid, nicknames).matched


def describe(result: AtResult) -> str:
    """把判定结果写成人话，用于日志/审计。"""
    if not result.matched:
        return "未 @ 我"
    if result.at_all:
        return f"@所有人（{result.evidence}）"
    return f"@我（{result.how}：{result.evidence}）"
