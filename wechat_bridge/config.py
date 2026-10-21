"""配置加载：bridge_config.template.json -> 强类型配置对象。

字段名与模板一一对应；新增字段必须同时在模板里加注释说明，否则下一个人不知道该填什么。
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .speak_gate import SpeakGateConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "bridge_config.json"
TEMPLATE_CONFIG = PROJECT_ROOT / "bridge_config.template.json"


@dataclass
class HookConfig:
    adapter: str = "wechat_bridge.adapters.hook_mock:MockHookAdapter"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMConfig:
    adapter: str = "wechat_bridge.adapters.llm_openai_compat:OpenAICompatClient"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class PersonaConfig:
    name: str = "我"
    system_prompt: str = ""
    # 允许在回复里用 emoji / 颜文字（😂 😅 ？？？ 草）。默认开：群里真人几乎都会用。
    # 由桥接**额外注入一句 system 提示**来保证生效——写进 `system_prompt` 的话，
    # 用户自己那份配置（大多数是复制模板来的）不会跟着变。
    allow_emoji: bool = True


@dataclass
class PolicyConfig:
    mode: str = "draft"                                  # auto | draft
    allow_groups: bool = False
    reply_only_when_mentioned_in_group: bool = True
    allow_unknown_contacts: bool = False
    # 允许**任意群聊**：被拉进一个新群就能直接工作，不用改配置、不用加白名单。
    # 仍然受"@ 才回 / 限速 / 静默时段 / 敏感词 / 新群观察期"全套约束；
    # contact_rules 里**显式写 ignore 或 deny 的群依然被拒**（显式拒绝优先）。
    # 只对群聊生效——私聊不受影响（私聊走 allow_unknown_contacts）。
    allow_any_group: bool = False
    contact_rules: dict[str, str] = field(default_factory=dict)   # 会话/发送者 id -> allow|deny|ignore（精确匹配）
    nickname_rules: dict[str, str] = field(default_factory=dict)  # 昵称 -> allow|deny|ignore（可被对方改名绕过，慎用）
    deny_patterns: tuple[str, ...] = ()                           # 子串匹配（字面量）
    deny_regex: tuple[str, ...] = ()                              # 正则匹配（处理“转 500”这类带空格的写法）
    max_replies_per_minute: int = 6
    max_replies_per_hour: int = 60
    max_replies_per_contact_per_hour: int = 20
    quiet_hours: tuple[str, str] = ("23:30", "07:30")
    reply_delay_sec: tuple[float, float] = (1.5, 4.0)
    freeze_after_consecutive_failures: int = 3
    max_reply_chars: int = 200
    max_reply_age_min: int = 15                              # 超过这个分钟数的消息不再回复（防"早晨补发刷屏"）
    # 发送防抖：决定要回之后，再多等这么久，把同一个人紧接着连发的几条并成一次回复。
    # 真人发微信是"想到一句发一句"，逐条回会把一次意思拆成三轮（KI-2）。设 0 关闭。
    #
    # 2026-10-17：1.8 → 5.5。用户实测"我发两条它就回两条，是我打字慢了"——
    # 4.0 还是短（真人手打第二句经常 5 秒以上）。窗口**滑动**：他又说一句就重新计时。
    reply_debounce_sec: float = 5.5
    # 滑动窗口的硬上限（秒）：一直有人连发也不能把回复无限期拖下去，到点就按已合并的内容回。
    # 0 = 不设上限（不推荐，遇到话痨会一直等）。
    reply_debounce_max_sec: float = 12.0


@dataclass
class ContextConfig:
    max_turns: int = 8
    separate_group_context: bool = True


@dataclass
class GroupConfig:
    """群聊状态机（`wechat_bridge.group_state`）的参数。

    语义：群聊里被 @ 才激活，静默超时自动休眠；休眠期只写环形缓冲、**不调用模型**。
    """

    enabled: bool = True                                   # 关掉就退回旧的"未 @ 就不回"行为
    idle_timeout_sec: float = 300.0                        # 静默多久休眠（秒）
    followup_window_sec: float = 90.0                      # 我发完话后多久内的接话算"追问"
    ring_size: int = 30                                    # 每群保留多少条上下文
    proactive: bool = False                                # 是否允许主动插话（默认关，容易被举报）
    max_chain_turns: int = 10                              # 连续多少轮无 @ 的接话后停下（防自我循环；0=不限）
    # **追问只接"刚才跟我说话的那个人"**。关掉它，我发完话 90 秒内**任何人**说话我都接——
    # 于是甲 @ 我一次之后，乙丙丁的相互闲聊我也会一句句接上去（在不该插嘴的地方插嘴）。
    # 只有在"群里基本只有你和我说话"的小群里才建议关。
    followup_same_sender_only: bool = True
    observe_first_msgs: int = 0                            # 新群观察期：前 N 条只观察不回
    # 群里回复时 **@ 回对方**。人多的时候不 @ 就没人知道你在回谁（对着空气说话）；
    # 只有三五个人的小群可以关掉，显得更自然。
    # ⚠ 这个字段曾经只写在 GroupPolicy 里、**没有写进 GroupConfig** ⇒ 配置文件里写它
    #   完全不生效（桥接用 getattr 兜了默认值，所以还不报错）。改配置模型时请对照 GroupPolicy。
    at_back: bool = True
    ignore_at_all: bool = True                             # @所有人 不接话（别在全员通知下面插嘴）
    self_wxid: str = ""                                    # 本账号 wxid（留空则问适配器要）
    my_nicknames: list[str] = field(default_factory=list)  # 我在群里可能被叫到的名字（补适配器的不足）
    # ---- 情景交融：看见全场 + 自己决定要不要开口（2026-10-18）----
    # 原先 `proactive=True` 的语义是"追问窗口内**任何人**说话都接"——真机验证过会滥用
    # （甲 @ 我一次之后，乙丙丁的相互闲聊也一句句接），所以默认关。
    # 新语义：**先看见全部消息**（环形缓冲本来就有），等**冷场**再让模型决定要不要插一句。
    proactive_silence_sec: float = 25.0    # 群里安静这么久才考虑插话（秒）
    proactive_budget_per_hour: int = 2     # 每小时最多主动开口几次（0=不许）
    proactive_cooldown_sec: float = 180.0  # 自己说完话之后多久内不再主动开口
    proactive_min_history: int = 3         # 环形缓冲至少有几条才考虑（太少就是没话可说）


@dataclass
class VisionConfig:
    """图片理解后端。**默认关闭**——关着的时候 AI 只看到「[图片]」占位，不会真的看懂。

    为什么必须单独配：主模型 `deepseek-chat` 不支持图片输入。
    本机 LM Studio 跑个 7B 级 VL 模型是免费选项，云端 VL API 也行。
    """

    enabled: bool = False
    adapter: str = "wechat_bridge.adapters.vision_openai_compat:VisionCompatClient"
    options: dict[str, Any] = field(default_factory=dict)
    max_images_per_turn: int = 2                           # 一次响应最多理解几张图（防一次发 20 张把账单打爆）


@dataclass
class SegmentsConfig:
    """多段输出（拟人节奏）：把一次响应拆成几条先后发出，而不是压成一条长消息。

    真人发微信是分几次发的。模型一次就把话全生成了，所以"节奏"只能在发送侧补。
    约定见 DESIGN.md 2.5：模型用 `|||` 分隔，桥接切开逐段发、段间随机 1.5–4 秒。
    """

    enabled: bool = True
    max_segments: int = 3                                  # 一次响应最多几条（防刷屏）
    max_chars: int = 80                                    # 单条最长多少字（防话痨）
    gap_sec: tuple[float, float] = (1.5, 4.0)              # 段间随机间隔区间
    # 模型没给 `|||` 时，按标点自己拆（只在"拆了更像人说话"时拆，见 `segments.auto_split`）。
    # 为什么默认开：真机 112 条回复里 0 条用了约定分隔符 ⇒ 只靠约定等于没实现。
    auto_split: bool = True
    auto_split_min_chars: int = 14                         # 短于这个字数不拆（一句话就够用了）


@dataclass
class StorageConfig:
    audit_path: str = "state/audit.jsonl"
    state_path: str = "state/state.json"
    draft_dir: str = "state/drafts"
    # 审计里是否保存**喂给模型的完整提示词**。
    # 为什么必须能存：2026-10-17 排查"长记忆没生效"时，236 条审计记录 0 条含 prompt，
    # 只能靠猜——分不清是"没写进记忆"还是"写进去了但没被召回/没被模型用上"。
    # off=不存 | on_error=只在出错时存 | always=每条回复都存（排障期用这个）
    save_prompt: str = "always"
    prompt_max_chars: int = 8000           # 单条 prompt 的存档上限（超出截断，防审计膨胀）
    # 审计轮转：一个热闹的群 + 每条都带提示词，几个月就能把 jsonl 撑到几十兆。
    # 到了上限就改名成 `audit-<时间戳>.jsonl`，只留最近 audit_keep 个。
    audit_max_mb: float = 8.0
    audit_keep: int = 5


@dataclass
class MemoryConfig:
    """长记忆（`wechat_bridge/long_memory.py`）：跨天还记得住这个人。

    没有它的时候，隔一天、或者换个话题回来，它当第一次认识你——
    因为 `context` 只留最近几轮、群聊环形缓冲只留 30 条，**旧内容根本没被喂进来**。
    """

    enabled: bool = True
    dir: str = "state/memory"              # 落盘目录（每会话一个 JSON）
    max_items: int = 80                    # 每个会话最多记多少条（超了按价值淘汰）
    min_chars: int = 4                     # 短于这个字数不记（"嗯""好"不算记忆）
    recall_limit: int = 6                  # 每次回复最多带几条进提示词
    extract_every: int = 10                # 每 N 次回复跑一次"模型摘要"（0=只记显式的）
    extract_max_items: int = 4             # 一次摘要最多新增几条
    extract_context_turns: int = 6         # 摘要时喂最近几轮对话
    # **兜底摘要**：只靠"回复满 N 次"这一个触发点，短会话永远轮不到
    # （2026-10-17 实测：只回了 6 次 ⇒ 一条记忆都没写）。所以再加两个触发点：
    extract_idle_sec: float = 180.0        # 连续这么久没有新消息，就把还没摘要的对话补一次（0=关）
    extract_on_exit: bool = True           # 退出前把没摘要的补一次（每天关机都会发生）
    # ---- 分层：旧的压成摘要，原文进归档 ----
    # 三层记忆：近的走上下文注入；旧一点的留成长记忆条目；太老的先**摘要**成
    # 几条 `digest`（继续进提示词），原文追加进 `archive/*.jsonl`（不再进提示词，但查得到）。
    archive_after_days: float = 21.0       # 超过这么多天的条目进压缩（0=不按时间压）
    archive_batch: int = 8                 # 一次压缩多少条（太大压不干净，太小摘要碎）
    digest_max_items: int = 3              # 一次压缩最多产出几条摘要
    archive_dir: str = ""                  # 留空 = <dir>/archive（跟着 dir 走，测试/demo 不会污染真实归档）
    compact_check_hours: float = 6.0       # 同一会话最快多久检查一次压缩（别每次回复都扫）


@dataclass
class BridgeConfig:
    hook: HookConfig = field(default_factory=HookConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    group: GroupConfig = field(default_factory=GroupConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    segments: SegmentsConfig = field(default_factory=SegmentsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    # 说话决策层（`speak_gate.py`）：这条消息现在开口合适吗？
    # 为什么从 `group` 里独立出来：它管的不是"群醒没醒"（那是状态机），
    # 而是"醒了之后这一句要不要接"——@别人 / 引用别人 / 只是应了一声 都属于后者。
    speak_gate: SpeakGateConfig = field(default_factory=SpeakGateConfig)


def _fill(cls: type, data: dict[str, Any]) -> Any:
    """按 dataclass 字段名筛掉未知键（模板里的 _comment 之类），缺失的用默认值。"""
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def read_json_file(path: Path) -> Any:
    """读 JSON，容忍 BOM。

    为什么必须容忍：本项目的配置与 state.json 是给**人**改的，而记事本（以及 PowerShell 的
    `Set-Content -Encoding UTF8`）保存时会写 UTF-8 BOM，`json.loads` 遇到 BOM 直接抛
    `Unexpected UTF-8 BOM`——用户改个白名单就起不来了。所以统一走 utf-8-sig 解码，
    非 BOM 文件行为不变。
    """
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(text)


def load_config(path: Path | str | None = None) -> BridgeConfig:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {cfg_path}\n先复制模板：copy \"{TEMPLATE_CONFIG.name}\" \"{DEFAULT_CONFIG.name}\""
        )
    raw = read_json_file(cfg_path)
    return BridgeConfig(
        hook=_fill(HookConfig, raw.get("hook", {})),
        llm=_fill(LLMConfig, raw.get("llm", {})),
        persona=_fill(PersonaConfig, raw.get("persona", {})),
        policy=_fill(PolicyConfig, raw.get("policy", {})),
        context=_fill(ContextConfig, raw.get("context", {})),
        group=_fill(GroupConfig, raw.get("group", {})),
        vision=_fill(VisionConfig, raw.get("vision", {})),
        segments=_fill(SegmentsConfig, raw.get("segments", {})),
        storage=_fill(StorageConfig, raw.get("storage", {})),
        memory=_fill(MemoryConfig, raw.get("memory", {})),
        speak_gate=_fill(SpeakGateConfig, raw.get("speak_gate", {})),
    )


def resolve(spec: str) -> Any:
    """把 "包.模块:类名" 解析成类对象。"""
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"适配器写法应为 '包.模块:类名'，收到: {spec!r}")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    return getattr(importlib.import_module(module_name), class_name)
