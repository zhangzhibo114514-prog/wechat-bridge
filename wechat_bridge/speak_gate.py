# -*- coding: utf-8 -*-
"""说话决策（speak_gate）：这条消息，我现在开口合适吗？

**为什么要单独一层。** 现在"该不该说"散在三处：群状态机（@我 / 休眠 / 链长）、
`group.proactive`（激活态**任何**消息都回）、追问窗口（只接刚才那个人）。
每加一种情形都得动状态机，而状态机的本职是"要不要**唤醒**"，不是"要不要**说话**"。
两者混在一起，`proactive=true` 就退化成"激活态下谁说话我都回"——放进几十人的群，
那就是最像营销号的行为。

这一层只回答一个问题：**这条消息，我现在开口合适吗？** 三种出口：说、不说、交给模型判。

## 谁来做决定（两种模式）

- `mode=model`（默认）：**模型自己判断**。除了两条结构性过滤（自己发的、正文是空的）
  以及"被点名"这一条快路（@我 / 引用我的消息 ⇒ 直接说，不花判定钱），
  其余情形一律把**上下文 + 现象提示**交给模型，由它回一个词：接 / 不接。
- `mode=rules`：**只用确定性规则**（零成本、零延迟、可解释）。留给"没有判定预算 /
  模型不稳定 / 想先看规则版行为"的场景。两种模式共用同一套现象提示，
  所以从 rules 切到 model 时，判断口径是连续的，不是换了个东西。

**为什么还要"现象提示"而不用裸判**：模型看聊天记录能读出"他在跟谁说话"，
但读不出这个客户端特有的信号——比如"这条 @ 的是别人""这条引用了别人的话"
（引用在库里被归成「文件/链接/卡片」，正文里只是 `[引用 谁：原话]`）。
把这些作为**提示**而不是**判决**给模型，既不替它做决定，又不让它瞎猜。

## 结构性过滤（不属于"判断"，是常识）

    自己发的 / 正文是空的            → 不说（结构性，永不交给模型）
    @ 我 / 引用我的消息（且不是纯应答） → 说（被点名了；这是它存在的契约，也不再花钱问一遍）

## 现象提示（两种模式共用，只做提示）

    - 这条 @ 的是别人（名字是别人）
    - 这条引用的是别人的话
    - 这条看起来只是在应一声（嗯/对/好…）
    - 说话的不是刚才跟你对话的那个人
    - 你上一句是在问他（他说的是"好/行"时可能是在回答你）

## 兜底

模型判定**报错 / 超时 / 超出每小时预算或每分钟上限** ⇒ 一律按**不说**处理。
宁可少说一句，也不要在一群真人面前说错一句。

测试见 `speak_gate_selftest.py`（纯逻辑 + 桥接层各一组）。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: 判"是不是只有应答词"用的内置表。只用于**提示**与 rules 模式，不用于 model 模式下的硬判。
DEFAULT_ACK_WORDS = (
    # 单字
    "嗯", "恩", "哦", "噢", "喔", "对", "是", "好", "行", "中", "妥", "成", "哈",
    # 常见整词
    "好的", "好滴", "好嘞", "好吧", "行吧", "对的", "是的", "是啊", "对啊",
    "收到", "明白", "知道", "了解", "谢谢", "可以", "没问题", "确实", "没错",
    "哈哈", "hhh", "ok", "6",
    # 语气词（"好的呀""行了啦"这类）
    "呀", "啊", "啦", "了",
)

#: 纯标点/emoji/空白：判"是不是只有应答词"之前先清掉它们。
_NOISE_RE = re.compile(r"[^\w]")


@dataclass
class GateContext:
    """判一次需要知道的东西。**不含 IO**——纯数据，方便单测直接构造。"""

    text: str = ""
    chat_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    is_group: bool = True
    is_self: bool = False
    mentions_me: bool = False
    at_others: bool = False
    at_note: str = ""
    # `parse_quote()` 的结果（不是引用时为 None）。判定层只关心两件事：
    # 有没有引用块、被引用的是不是我（后者已经折算成 `mentions_me`）。
    quote: Optional[dict] = None
    group_state: str = ""
    last_speaker: str = ""
    #: 我上一句是不是在问他（以问号 / 吗 / 呢 收尾）。
    #: 为什么要这一位：对方回一个"好"，可能是"行，你办吧"——那是**答复**，不是敷衍。
    bot_asked_question: bool = False


@dataclass
class Verdict:
    """判决结果。`code` 是给机器/审计看的，`why` 是人看的，`hints` 是喂给模型的提示。"""

    speak: bool
    code: str = ""
    why: str = ""
    judged_by_model: bool = False
    hints: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:          # `if verdict:` 直接问"要不要说"
        return self.speak


@dataclass
class SpeakGateConfig:
    """说话决策的开关。"""

    enabled: bool = True
    #: model = 交给模型判断（默认）；rules = 只用确定性规则（零成本、可解释）。
    mode: str = "model"
    #: 留空 = 用内置表（`DEFAULT_ACK_WORDS`）。想加方言就写在这里，会**追加**到内置表。
    ack_words: list[str] = field(default_factory=list)
    ack_max_chars: int = 6
    #: 每小时 / 每分钟最多让模型判几次。超了就按"不说"处理（成本与延迟的双重保险）。
    model_budget_per_hour: int = 60
    model_max_per_min: int = 6


class SpeakGate:
    """判定层：无 IO、时钟可注入；模型只通过 `judge_model` 回调接入（方便测试）。"""

    def __init__(
        self,
        config: Optional[SpeakGateConfig] = None,
        same_sender_only: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config or SpeakGateConfig()
        # rules 模式下"别人在说话"这条是否生效，直接跟随 `group.followup_same_sender_only`
        # ——同一个概念不要配两个开关（两处不一致时没人能说清以哪个为准）。
        self.same_sender_only = bool(same_sender_only)
        self._clock = clock
        self._judge_times: deque[float] = deque()
        words = list(DEFAULT_ACK_WORDS) + [str(w) for w in (self.cfg.ack_words or []) if str(w).strip()]
        # 长的排前面：贪心匹配"好的"不会先被"好"吃掉。
        self.ack_words = tuple(sorted({w.lower() for w in words}, key=len, reverse=True))

    # ---------------- 对外 ----------------

    def judge(self, ctx: GateContext,
              judge_model: Optional[Callable[[GateContext, list[str]], Optional[bool]]] = None
              ) -> Verdict:
        """判这一条要不要开口。`judge_model` 只在 `mode=model` 时被调用。"""
        if not self.cfg.enabled or not ctx.is_group:
            return Verdict(True, "disabled", "判定层未启用（照旧说）")
        if ctx.is_self:
            return Verdict(False, "self", "自己发出去的那条")
        text = (ctx.text or "").strip()
        if not text:
            return Verdict(False, "empty", "正文是空的")

        hints = self.hints(ctx)
        ack = self._ack_only(text)
        # 被点名 = 契约：除非它只是应了一声（那种情况交给模型判，可能真不用回）。
        if ctx.mentions_me and not ack:
            return Verdict(True, "at_me", "这条是冲我来的（@ 我 / 引用了我的消息）", hints=hints)

        if self._mode() == "model" and judge_model is not None:
            return self._judge_with_model(ctx, hints, judge_model)
        return self._rule_verdict(ctx, ack, hints)

    def hints(self, ctx: GateContext) -> list[str]:
        """把"这个客户端才有的现象"翻译成给模型看的提示。

        **注意这是提示不是判决**：`mode=model` 时这些只作为参考信息，
        最终接不接仍由模型决定；`mode=rules` 时才拿去当硬判据。
        """
        out: list[str] = []
        if ctx.mentions_me:
            out.append("这条 @ 了你（或者引用了你的消息）")
        if ctx.at_others:
            out.append("这条 @ 的是别人，不是在叫你")
        if ctx.quote and not ctx.mentions_me:
            who = str((ctx.quote or {}).get("quoted_name") or "别人")
            out.append(f"这条引用的是{who}的话，像是在跟那个人说话")
        if self._ack_only((ctx.text or "").strip()):
            out.append("这条看起来只是在应一声（嗯/对/好…）")
        if self.same_sender_only and ctx.last_speaker and ctx.sender_id != ctx.last_speaker:
            out.append("说话的人不是刚才跟你对话的那个人")
        if ctx.bot_asked_question:
            out.append("你上一句是在问他；他这句可能是在回答你")
        return out

    def snapshot(self) -> dict[str, Any]:
        """给 `status --json` 用：这一层现在是什么档位、最近判过几次。"""
        return {
            "enabled": bool(self.cfg.enabled),
            "mode": self._mode(),
            "same_sender_only": self.same_sender_only,
            "ack_words": len(self.ack_words),
            "model_judged_last_hour": len(self._judge_times),
            "model_budget_per_hour": int(self.cfg.model_budget_per_hour),
            "model_max_per_min": int(self.cfg.model_max_per_min),
        }

    # ---------------- 内部 ----------------

    def _mode(self) -> str:
        mode = str(self.cfg.mode or "model").strip().lower()
        return mode if mode in ("model", "rules") else "model"

    def _rule_verdict(self, ctx: GateContext, ack: bool, hints: list[str]) -> Verdict:
        """`mode=rules`：确定性规则。**不含"不懂就问模型"那一层**，所以刻意保守。"""
        if ctx.at_others:
            return Verdict(False, "at_others", "这条 @ 的是别人，不该插嘴", hints=hints)
        if ctx.quote and not ctx.mentions_me:
            who = str((ctx.quote or {}).get("quoted_name") or "别人")
            return Verdict(False, "quote_others", f"这条引用的是{who}的话（别人之间的对话）",
                           hints=hints)
        if ack:
            if ctx.bot_asked_question:
                return Verdict(True, "ack_answers_question",
                               "这句是在回答我上一句问的（不能装没看见）", hints=hints)
            return Verdict(False, "ack_only",
                           "只是应了一声（嗯/对/好…）" + ("，虽然 @ 了我" if ctx.mentions_me else ""),
                           hints=hints)
        if ctx.mentions_me:
            return Verdict(True, "at_me", "这条是冲我来的（@ 我 / 引用了我的消息）", hints=hints)
        if self.same_sender_only and ctx.last_speaker and ctx.sender_id != ctx.last_speaker:
            return Verdict(False, "other_sender", "群醒着，但说话的不是刚才跟我对话的人",
                           hints=hints)
        return Verdict(True, "no_rule_hit", "规则没有命中，保持旧行为（说）", hints=hints)

    def _judge_with_model(self, ctx: GateContext, hints: list[str],
                          judge_model: Callable[[GateContext, list[str]], Optional[bool]]) -> Verdict:
        """交给模型判断。**失败一律按"不说"处理**（宁少说一句，不乱插一句）。"""
        now = self._clock()
        self._judge_times = deque(t for t in self._judge_times if now - t < 3600)
        if len(self._judge_times) >= max(0, int(self.cfg.model_budget_per_hour)):
            return Verdict(False, "judge_budget", "判定调用超过每小时预算，按不说处理",
                           hints=hints)
        per_min = sum(1 for t in self._judge_times if now - t < 60)
        if per_min >= max(0, int(self.cfg.model_max_per_min)):
            return Verdict(False, "judge_rate", "判定调用太频繁（每分钟上限），按不说处理",
                           hints=hints)
        self._judge_times.append(now)
        try:
            answer = judge_model(ctx, hints)
        except Exception:
            answer = None
        if answer is True:
            return Verdict(True, "judge_speak", "模型判断：这句可以接",
                           judged_by_model=True, hints=hints)
        if answer is False:
            return Verdict(False, "judge_silent", "模型判断：这句不该接",
                           judged_by_model=True, hints=hints)
        return Verdict(False, "judge_unknown", "判定没给出结论（报错/超时），按不说处理",
                       judged_by_model=True, hints=hints)

    def _ack_only(self, text: str) -> bool:
        """整条消息是不是"只是应了一声"。

        三步：①问号/疑问收尾直接判否（"嗯？"是在要我再说一遍）②去掉标点 emoji 后不能
        超过 `ack_max_chars` 个字 ③剩下的字符能被应答词**完整**切分（贪心，长词优先）。
        这样 "嗯" / "好的呀" / "哈哈哈哈" / "收到，谢谢" 命中，
        而 "好，那你说说这个方案怎么改" / "不知道" / "是吗" 不会命中。
        """
        if "?" in text or "？" in text:
            return False
        s = _NOISE_RE.sub("", text.lower())
        if not s:
            return False
        if s.endswith("吗") or s.endswith("么"):
            return False
        if len(s) > max(0, int(self.cfg.ack_max_chars)):
            return False
        i = 0
        while i < len(s):
            for word in self.ack_words:
                if s.startswith(word, i):
                    i += len(word)
                    break
            else:
                return False
        return True
