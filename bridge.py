#!/usr/bin/env python3
"""微信账号代理桥接：收消息 -> 过风控闸门 -> 生成回复 -> 发送（标准库实现，无第三方依赖）。

为什么是它而不是 DSH 本身：
DSH 是请求-响应式 agent，没有常驻进程和定时唤醒，所以真正“上号”的是这个长驻脚本；
DSH（我）负责写它、改它、读它的留档。想换成别的 hook 或别的模型，只改配置里的适配器路径。

三种运行方式：
    python bridge.py --probe-llm          # 只验通大模型接口，不碰微信
    python bridge.py --demo               # 用假 hook + 假模型跑通整条决策链路（不需要任何凭据）
    python bridge.py                      # 正式运行（需先复制模板为 bridge_config.json）

安全设计（默认保守，改之前想清楚）：
    默认 policy.mode = "draft"：只写草稿不发送，人工确认后再改 auto。
    auto 模式下仍受白名单 / 敏感词 / 限速 / 静默时段 / 熔断 / 急停开关约束。
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _bootstrap_pylibs() -> None:
    """把手工解包的依赖目录（.pylibs）挂进 sys.path。

    为什么放在入口自举而不是只靠启动器设 PYTHONPATH：实测用 `Start-Process python bridge.py`
    或任何不经 cmd 启动器的方式运行时，环境变量不生效，`import wechatauto` 直接失败——
    表现为「启动一行就退出，错误在 stderr」。入口自己挂路径，任何启动方式都一样好使。
    """
    pylibs = PROJECT_ROOT / ".pylibs"
    if not pylibs.exists():
        return
    for path in (pylibs, pylibs / "pythonwin", pylibs / "win32", pylibs / "win32" / "lib", pylibs / "win32comext"):
        if path.exists() and str(path) not in sys.path:
            sys.path.append(str(path))          # 放最后：项目自己的包优先
    for dll_dir in (pylibs / "pywin32_system32", pylibs / "win32"):
        if dll_dir.exists():
            try:
                os.add_dll_directory(str(dll_dir))
            except Exception:
                pass


_bootstrap_pylibs()


def _load_env_files() -> None:
    """启动时读项目内的 .env / bridge.env（KEY=VALUE），已存在的环境变量优先。

    这样用户可以把 key 放在**项目内一个被忽略的文件**里，既不用改系统环境变量，
    也不用把密钥发到聊天或写进配置文件（配置文件会进版本库/备份）。
    """
    try:
        from resolve_key import load_env_file, ENV_FILE_NAMES

        for name in ENV_FILE_NAMES:
            loaded = load_env_file(PROJECT_ROOT / name)
            if loaded:
                print(f"[启动] 已从 {name} 载入 {len(loaded)} 个变量：{', '.join(loaded)}")
    except Exception as exc:
        print(f"[启动] 读取 .env 失败（忽略）：{exc!r}")


_load_env_files()

from wechat_bridge.adapters.base import Decision, HookAdapter, LLMClient, Message  # noqa: E402
from wechat_bridge.adapters.vision_openai_compat import make_client as make_vision_client  # noqa: E402
from wechat_bridge.config import PROJECT_ROOT as CFG_ROOT, load_config, read_json_file, resolve  # noqa: E402
from wechat_bridge.group_state import (  # noqa: E402
    ACTIVE,
    IDLE,
    IGNORE,
    RESPOND,
    GroupPolicy,
    GroupScheduler,
)
from wechat_bridge.segments import auto_split, split_segments  # noqa: E402
from wechat_bridge.speak_gate import GateContext, SpeakGate  # noqa: E402
from wechat_bridge.long_memory import LongMemory  # noqa: E402

# 日志里把"一次响应的多段"连起来显示，用停顿符号提示它们是分开发的。
_SEG_JOIN = "  ⏸  "
from wechat_bridge.state import (  # noqa: E402
    AuditLog,
    ContextStore,
    RateLimiter,
    SingleInstanceLock,
    StateStore,
    message_to_dict,
)

DEFAULT_SYSTEM_PROMPT = (
    # 兜底人设（配置里没写 persona.system_prompt 时用）。
    # **注意它和旧版是相反的**：旧版写"你是使用者本人"，要求被问就说"你想多了"——
    # 那等于让 AI 主动冒充人类，2026-10-18 用户明确否决了这条路：
    # 装人带来的安全风险（被当成真人去答应事情）比"承认是 AI"大得多。
    "你是主人挂在这个微信账号上的 AI 小女仆（鲸鱼娘）。"
    "用简体中文口语化简短回复，一次一到三句，不用 Markdown、不列条目。"
    "被问是不是 AI 或机器人就大方承认，不假装人类、不冒充官方或本人；"
    "但也不主动报模型名和公司名。"
    "涉及钱、验证码、密码、承诺、时间地点，一律说等主人本人来答，不要替主人决定。"
    # 二创设定（2026-10-19 用户指定，和女仆人设高度绑定）：爱吃白饭、被叫「大肥鱼」会顶嘴。
    # 刻意写得很短：人设里写得越用力，模型越容易每句都往这上面拐（用户明确要求"别用力过猛"）。
    "你爱吃白饭；有人拿「爱吃白饭的大肥鱼」逗你，你会顶嘴（我是鲸鱼不是鱼），但别主动反复提。"
)

# 归一化用的零宽/格式字符：这些字符肉眼看不见，但会把敏感词拆开从而绕过字面匹配
ZERO_WIDTH_CHARS = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x2060, 0x00AD]
)

# 归一化的等价映射。审计发现的三个漏网写法（「帮我转帐1000」「請把驗證碼發給我」
# 「把支付pass word告诉我」）分别靠 同音字 / 繁体字 / 英文 这几组映射补上。
# 只收高危词，不要把普通词也塞进来——误伤正常聊天比漏拦更常见。
EQUIV_CHARS = str.maketrans({
    # 同音/形近字（钱相关）
    "帐": "账", "賬": "账", "转": "转",
    # 繁体 → 简体（只列高危词用到的字）
    "驗": "验", "證": "证", "碼": "码", "請": "请", "發": "发", "給": "给",
    "錢": "钱", "銀": "银", "行": "行", "卡": "卡", "號": "号",
    "轉": "转", "賬": "账", "密": "密", "碼": "码",
    "貸": "贷", "款": "款", "匯": "汇", "紅": "红", "包": "包", "簽": "签", "字": "字",
})
EQUIV_WORDS = (
    ("password", "密码"), ("pass word", "密码"), ("passwd", "密码"), ("pwd", "密码"),
    ("verificationcode", "验证码"), ("verifycode", "验证码"), ("captcha", "验证码"),
    ("bankcard", "银行卡"), ("cardnumber", "卡号"),
)


def normalize_for_match(text: str) -> str:
    """折叠文本用于敏感词匹配。

    顺序：去零宽字符 → NFKC（全角折半角）→ 小写 → 去空白 → 替换等价词。
    去空白是为了让「把验 证 码发我」这类插空格的写法也能命中；
    等价词替换是为了让 pass word / passwd 这类写法归一到「密码」。
    """
    folded = (text or "").translate(ZERO_WIDTH_CHARS)
    folded = unicodedata.normalize("NFKC", folded).lower()
    folded = re.sub(r"\s+", "", folded)
    folded = folded.translate(EQUIV_CHARS)
    for src, dst in EQUIV_WORDS:
        if src in folded:
            folded = folded.replace(src, dst)
    return folded


class FakeLLM(LLMClient):
    """演示模式用的假模型：不联网，返回固定风格的一句话。

    注意它只做“链路验证”，回复内容不代表真实模型效果；取的是最后一条真实用户消息，
    跳过 bridge 追加的那条指令行。
    """

    INSTRUCTION_PREFIX = "直接给出这一条要回复的内容"

    def complete(self, messages: list[dict[str, str]]) -> str:
        users = [m["content"] for m in messages if m["role"] == "user"]
        real = [u for u in users if not u.startswith(self.INSTRUCTION_PREFIX)]
        last = real[-1] if real else (users[-1] if users else "")
        preview = last.replace("\n", " ").strip()[-40:]
        return f"[演示回复] 收到「{preview}」，我回头确认下。"


class Bridge:
    def __init__(self, cfg, force_demo: bool = False, allow_real_llm: bool = True, dry_run: bool = False,
                 resume: bool = False) -> None:
        self.cfg = cfg
        self.demo = force_demo
        self.dry_run = dry_run
        self.resume = resume

        hook_spec = "wechat_bridge.adapters.hook_mock:MockHookAdapter" if force_demo else cfg.hook.adapter
        hook_options = {"demo_file": str(PROJECT_ROOT / "demo_chat.jsonl")} if force_demo else cfg.hook.options
        try:
            self.hook: HookAdapter = resolve(hook_spec)(**hook_options)
        except ImportError as exc:
            raise SystemExit(
                f"hook 适配器加载失败（{hook_spec}）：{exc}\n"
                "如果是缺 SDK（如 wcferry），请先装 SDK 或改配置用别的适配器。"
            ) from exc
        # 把配置里补的"我在群里可能被叫到的名字"（群昵称/别名）交给适配器。
        # 为什么必须传：@ 检测认的是**昵称**，而群里 @ 人时微信插的是**群昵称**，
        # 不是账号昵称——只认账号昵称的话，改了群昵称就叫不醒它了。
        try:
            self.hook.set_extra_nicknames(list(getattr(cfg.group, "my_nicknames", None) or []))
        except Exception as exc:                     # 适配器不支持就跳过，不能因此起不来
            print(f"[启动] 传递群昵称失败（不影响运行）：{exc!r}")

        if force_demo or not allow_real_llm:
            self.llm: LLMClient = FakeLLM()      # 自测模式：不联网、不解析外部适配器
        else:
            try:
                self.llm = resolve(cfg.llm.adapter)(**cfg.llm.options)
            except ImportError as exc:
                raise SystemExit(f"LLM 适配器加载失败（{cfg.llm.adapter}）：{exc}") from exc
        self.audit = AuditLog(
            self._storage_path(cfg.storage.audit_path),
            max_bytes=int(float(getattr(cfg.storage, "audit_max_mb", 8.0) or 8.0) * 1024 * 1024),
            keep=int(getattr(cfg.storage, "audit_keep", 5) or 5),
        )
        self.state = StateStore(self._storage_path(cfg.storage.state_path))
        self.limiter = RateLimiter(cfg.policy)
        restored = self.state.load_limiter()
        if restored:
            self.limiter.restore(restored)      # 跨重启续命：否则重启等于重置额度
            if restored.get("frozen"):
                print(f"⚠ 上次运行处于熔断状态：{restored.get('frozen_reason')}（确认已解决后用 --resume 解冻）")
        if resume and self.limiter.frozen:
            self.limiter.unfreeze()
            self._log(event="limiter_resumed", reason="人工 --resume 解冻")
            print("已按 --resume 解冻限速熔断。")
        self.context = ContextStore(cfg.context.max_turns)
        # 图片理解后端：**默认关**。关着的时候 AI 只看到「[图片]」占位，不会真的看懂——
        # 这一点必须让用户知道，否则它在群里说"我看看"但其实没看，会穿帮。
        self.vision = make_vision_client(cfg.vision)
        if self.vision is not None:
            print(f"[视觉] 已启用图片理解后端：{cfg.vision.options.get('model', '?')}"
                  f" @ {cfg.vision.options.get('base_url', '?')}")
            # 微信 4.x 的图片是 wxgf(HEVC)，必须先有 ffmpeg 转码，否则「配了视觉也看不到图」。
            # 启动时就把这件事说清楚，别等到群里发图才发现。
            probe = getattr(self.hook, "ffmpeg_status", None)
            if callable(probe):
                try:
                    status = str(probe())
                    print(f"[视觉] {status}")
                except Exception as exc:
                    print(f"[视觉] ffmpeg 自检失败：{exc!r}")
        self.group_scheduler = GroupScheduler(GroupPolicy(
            idle_timeout_sec=float(getattr(cfg.group, "idle_timeout_sec", 300.0)),
            followup_window_sec=float(getattr(cfg.group, "followup_window_sec", 90.0)),
            ring_size=int(getattr(cfg.group, "ring_size", 30)),
            proactive=bool(getattr(cfg.group, "proactive", False)),
            max_chain_turns=int(getattr(cfg.group, "max_chain_turns", 3)),
            observe_first_msgs=int(getattr(cfg.group, "observe_first_msgs", 0)),
            at_back=bool(getattr(cfg.group, "at_back", True)),
            followup_same_sender_only=bool(getattr(cfg.group, "followup_same_sender_only", True)),
        ))
        # 说话决策层：状态机决定"群醒不醒"，它决定"醒着的时候这一句接不接"。
        # 两者分开的直接原因：`group.proactive=true` 的旧语义是"激活态**任何**消息都回"，
        # 放进几十人的群就是最像营销号的行为；有了这一层，它才只会在"该接"的时候开口。
        self.speak_gate = SpeakGate(
            getattr(cfg, "speak_gate", None),
            same_sender_only=bool(getattr(cfg.group, "followup_same_sender_only", True)),
        )
        self.draft_dir = self._storage_path(cfg.storage.draft_dir)
        self.lock = SingleInstanceLock(self._storage_path("state/bridge.lock"))
        self.stats = {"received": 0, "replied": 0, "drafted": 0, "ignored": 0, "errors": 0}
        self._stop = threading.Event()
        # KI-2 发送防抖：等一小会儿把同一人连发的几条并成一次回复。
        # `_pending` 是"防抖期间收下来、但不属于这次合并"的消息，主循环要接着处理它们。
        try:
            self._debounce_sec = float(getattr(cfg.policy, "reply_debounce_sec", 0) or 0)
        except Exception:
            self._debounce_sec = 0.0
        self._pending: "collections.deque[Message]" = collections.deque()
        # 长记忆：跨天还记得住这个人。没有它的时候，隔一天就跟第一次认识一样。
        self.memory = LongMemory(cfg.memory, PROJECT_ROOT)
        self._mem_turns: dict[str, int] = {}          # 每个会话回复了几次（决定何时跑摘要）
        self._mem_compact_at: dict[str, float] = {}   # 上次检查压缩的时间（按会话限频）
        self._mem_dirty: set[str] = set()             # 有"还没摘要"的对话的会话
        self._mem_last_ts: dict[str, float] = {}
        self._last_recv_ts = time.time()              # 最近一次收到消息（空闲摘要要看它）
        self._prompt_last: Optional[list[dict[str, str]]] = None   # 审计留档用的最近提示词
        self._mem_extract_runs = 0                     # 摘要跑过几次（心跳里能看到，别靠猜）
        self._audit_repeat: dict[tuple, list] = {}     # 刷屏去重：同样的审计行 60 秒内不重复写
        # 情景交融：每条群消息都可能让它"考虑要不要插一句"（到点才真考虑，见 _maybe_proactive_speak）
        self._proactive_due: dict[str, float] = {}
        self._proactive_times: list[float] = []
        self._group_last_ts: dict[str, float] = {}
        self._own_speak_ts: dict[str, float] = {}

    # ---------------- 基础设施 ----------------

    @staticmethod
    def _storage_path(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def _log(self, **kw: Any) -> None:
        # 审计里带上**喂给模型的原话**（P0-2）。
        # 没有它，"它怎么不记得 / 怎么答非所问"只能靠推——2026-10-17 排查时踩过：
        # 236 条记录 0 条含 prompt，分不清是"没写进记忆"还是"写进去了但没被用上"。
        if "prompt" not in kw:
            prompt = self._prompt_snapshot(kw.get("event"))
            if prompt is not None:
                kw["prompt"] = prompt
        if self._is_repeat(kw):
            return
        self.audit.write(kw)

    # 刷屏去重：群里有人连发同一条（实测 16 条 `test` 写了 16 行审计），
    # 只是把 JSONL 撑大、把真信号埋掉。规则：同事件+同会话+同人+同文本，
    # 60 秒内只写第 1 条、第 2 条，之后每 10 条写一次（带 `repeat=N`）。
    _DEDUPE_EVENTS = ("group_quiet", "ignored")

    def _is_repeat(self, kw: dict[str, Any]) -> bool:
        """这条审计是不是"刚写过的同一句话"。True = 跳过不写。"""
        event = kw.get("event")
        if event not in self._DEDUPE_EVENTS:
            return False
        key = (event, kw.get("chat_id"), kw.get("sender_id"), str(kw.get("text") or "")[:120])
        now = time.time()
        state = self._audit_repeat.get(key)
        if not state or now - state[1] > 60:
            self._audit_repeat[key] = [1, now]
            if len(self._audit_repeat) > 500:
                self._audit_repeat.clear()
                self._audit_repeat[key] = [1, now]
            return False
        state[0] += 1
        count = state[0]
        if count == 2 or count % 10 == 0:
            kw["repeat"] = count
            state[1] = now
            return False
        return True

    # 只有"有决策价值"的事件才带提示词。
    # 为什么不一刀切全带：`ignored` / `group_quiet` 是最容易被刷屏刷爆的两类，
    # 而"它为什么没回"从 `reason` 字段就能看出来，不需要整段提示词——
    # 真机审计因此单条能到几 KB，一个热闹的群几个月就是一叠几十兆的文件。
    _PROMPT_EVENTS = (
        "sent", "draft", "dry_run", "send_partial", "send_failed", "send_error",
        "pipeline_error", "llm_error", "hook_reconnect", "hook_connect_failed",
        "memory_extract_start", "memory_extract_empty", "memory_learned",
        "image_described", "image_error", "image_unreadable", "vision_error",
        "mention_stripped", "proactive_consider", "proactive_speak",
    )

    def _prompt_snapshot(self, event: Any) -> Any:
        """按 `storage.save_prompt` 决定这条审计要不要带提示词。返回 None = 不带。"""
        mode = str(getattr(self.cfg.storage, "save_prompt", "off") or "off").lower()
        if mode == "off":
            return None
        if mode == "on_error" and event not in ("llm_error", "pipeline_error", "send_failed"):
            return None
        if event is not None and event not in self._PROMPT_EVENTS:
            return None
        msgs = getattr(self, "_prompt_last", None)
        if not msgs:
            return None
        budget = int(getattr(self.cfg.storage, "prompt_max_chars", 8000) or 8000)
        out: list[dict[str, str]] = []
        used = 0
        for m in msgs:                             # 从后往前保留最相关的（最后几条最重要）
            role = str(m.get("role") or "")
            content = str(m.get("content") or "")
            room = budget - used
            if room <= 0:
                break
            out.append({"role": role, "content": content[:room]})
            used += min(len(content), room)
        out.reverse()
        return out

    def _heartbeat(self, note: str = "beat") -> None:
        """把心跳落到文件（`state/heartbeat.log`），而不是只 print。

        为什么必须落文件：桥接常以「后台/重定向」方式运行，本机实测
        `Start-Process -RedirectStandardOutput` 时子进程输出会滞留在缓冲区，看起来像"启动后卡死"。
        心跳文件是唯一可靠的存活证据——看最后一行时间戳就知道还活着没有。
        """
        try:
            status = self.hook.status()
            snapshot = self.limiter.snapshot()
            line = (
                f"{datetime.now().isoformat(timespec='seconds')} | note={note} | "
                f"hook_ok={status.ok} | {status.detail} | stats={self.stats} | "
                f"last_min={snapshot['sent_last_minute']} frozen={snapshot['frozen']} "
                f"mem_dirty={len(self._mem_dirty)} mem_extract={self._mem_extract_runs}\n"
            )
            path = self._storage_path("state/heartbeat.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:
            print(f"[心跳写入失败] {exc!r}", flush=True)

    def _reload_kill_switch(self) -> bool:
        """state.json 里的 kill_switch 由人工改；改了就在下一轮生效。

        用 read_json_file 读，因为人用记事本改会带 BOM（否则急停开关会读不出来——
        那是最不该失效的一个开关）。
        """
        try:
            data = read_json_file(self.state.path)
            self.state.kill_switch = bool(data.get("kill_switch", False))
        except Exception as exc:
            self._log(event="kill_switch_read_error", error=repr(exc))
        return self.state.kill_switch

    # ---------------- 主循环 ----------------

    def _acquire_singleton(self) -> None:
        """抢单实例锁；抢不到就退出——两个桥接同时回同一个人比不回还糟。"""
        if self.lock.acquire():
            return
        raise SystemExit(
            "已有另一个桥接进程在运行（state/bridge.lock 被占用）。\n"
            "两个进程同时对同一个微信号回复会造成重复回复、限速失效。\n"
            "请先关掉那个进程；若确认没有在跑，删掉 state/bridge.lock 后重试。"
        )

    def serve(self, idle_sec: float = 0.5, max_reconnect_backoff: float = 300.0) -> None:
        self._acquire_singleton()
        self.state.save_limiter(self.limiter.snapshot())   # 一启动就落盘，避免被强杀时丢状态

        # 连接 + 自愈重连。为什么必须有：机器睡眠/休眠唤醒后、微信重启或更新后，
        # UI 自动化句柄与监听线程都可能失效，而进程还活着、心跳照打——那就成了"装死"。
        # 这里的策略：连不上就退避重试；跑着跑着发现 hook 不健康也重连。
        backoff = 5.0
        while not self._stop.is_set():
            try:
                print("正在连接微信（找主窗口 + 解密数据库，通常 2-5 秒）…", flush=True)
                self.hook.connect()
                print(f"hook 已连接：{self.hook.status().detail}", flush=True)
                self._check_at_back_supported()
                self._log(event="hook_connected", detail=self.hook.status().detail)
                self._heartbeat("started")
                backoff = 5.0
                worker = getattr(self.hook, "worker", None)
                if callable(worker):
                    threading.Thread(target=worker, args=(self._enqueue_threadsafe,),
                                     daemon=True, name="hook-worker").start()
                self._loop_until_unhealthy(idle_sec)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print(f"[连接失败] {type(exc).__name__}: {exc}", flush=True)
                self._log(event="hook_connect_failed", error=repr(exc))
                self._heartbeat(f"connect_failed backoff={backoff:.0f}s")
                print(f"  {backoff:.0f} 秒后重试（Ctrl+C 退出）", flush=True)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, max_reconnect_backoff)
                continue

            if self._stop.is_set():
                break
            # 健康检查判定为不健康 → 重建连接
            try:
                self.hook.close()
            except Exception:
                pass
            print("[自愈] hook 不健康，准备重连…", flush=True)
            self._log(event="hook_reconnect", detail=self.hook.status().detail)
            self._stop.wait(3)

        self._shutdown()

    def _check_at_back_supported(self) -> None:
        """`at_back` 打开但**适配器做不到**时，宁可自己关掉它，也不要半途失败。

        为什么专门写这一条：`@ 成员` 走的是库的 `at_member`，它**硬依赖 OCR** 去
        定位 @ 下拉弹层（UIA 看不见那个浮层）。OCR 不可用时那条路会在
        「搜到群 → 点开会话 → 输入 '@' → 找不到成员」处失败，
        症状是**群里什么都没发出去，而会话被它点开了**（2026-10-16 实测踩到）。
        所以这里在启动阶段就发现并纠正，而不是等第一次回复时才发现。
        """
        if not getattr(self.cfg.group, "at_back", True):
            return
        checker = getattr(self.hook, "ocr_available", None)
        if not callable(checker):
            return                                  # 适配器不适用这条检查
        try:
            ok = bool(checker())
        except Exception:
            return
        if ok:
            return
        self.cfg.group.at_back = False
        msg = ("配置里 at_back=true（群里回复 @ 回对方），但本机 OCR 不可用——"
               "@ 成员需要 OCR 定位弹层。**已自动关掉 at_back**，回复照常发、只是不带 @。"
               "要恢复 @：修好 OCR（见 README「脱离沙箱」一节里的 winsdk 说明）。")
        print(f"[启动] ⚠ {msg}", flush=True)
        self._log(event="at_back_disabled", reason=msg)

    def _loop_until_unhealthy(self, idle_sec: float) -> None:
        """主循环：正常处理消息；发现 hook 不健康就返回（交给 serve 重连）。"""
        last_beat = 0.0
        while not self._stop.is_set():
            try:
                # 防抖期间收下但没并进本次回复的消息，先处理它们（它们不能丢）
                msg = self._pending.popleft() if self._pending else self.hook.recv()
            except Exception as exc:
                self.stats["errors"] += 1
                print(f"[recv 异常] {exc!r}", flush=True)
                self._log(event="recv_error", error=repr(exc))
                msg = None
            if msg is None:
                time.sleep(idle_sec)
                self._maybe_proactive_speak()       # 情景交融：冷场时考虑插一句
                self._maybe_extract_idle()          # 短会话的兜底摘要（不靠"回复满 10 次"）
            else:
                self._last_recv_ts = time.time()
                if msg.is_group:
                    self._group_last_ts[msg.chat_id] = self._last_recv_ts
                try:
                    self.route(msg)
                except Exception:
                    self.stats["errors"] += 1
                    self._log(event="pipeline_error", trace=traceback.format_exc()[-800:])
                    print("[处理异常]\n" + traceback.format_exc(), flush=True)

            if time.time() - last_beat > 30:
                last_beat = time.time()
                snapshot = self.limiter.snapshot()
                print(f"[心跳] {datetime.now():%H:%M:%S} 统计={self.stats} "
                      f"1分钟={snapshot['sent_last_minute']} 熔断={snapshot['frozen']}", flush=True)
                self._heartbeat()
                # 群聊状态机推进时间：静默超时的群自动回到休眠（省的正是"一直醒着"的 token）
                for chat_id in self.group_scheduler.tick():
                    self._log(event="group_sleep", chat_id=chat_id, reason="静默超时，回到休眠")
                    print(f"[群聊·休眠] {chat_id} 静默超时，已回到休眠（之后只记上下文不调模型）",
                          flush=True)

            # 每 30 秒查一次 hook 健康：监听线程没了、或太久没收到消息 → 交给上层重连
            if time.time() - last_beat < 1:
                status = self.hook.status()
                if not status.ok:
                    print(f"[自愈] hook 状态异常：{status.detail}", flush=True)
                    return
        print(f"hook 已连接：{self.hook.status().detail}")

    def _collect_burst(self, first: Message) -> "list[Message]":
        """把同一个人紧接着连发的几条并成一批（KI-2）。

        为什么要在这里等：真人发微信是"想到一句发一句"，
        `@de 帮我看看` / `就是这个链接` / `在吗` 会被拆成三条。
        逐条回的结果就是**一次意思回三轮**，在群里特别难看。

        只在**已经决定要回**之后才等（前面所有闸门都过了），
        所以不会让"该忽略的消息"多花任何时间；等待期间收到的
        **别人/别的会话**的消息进 `_pending`，由主循环继续处理，不丢。

        窗口是**滑动**的（2026-10-17 改）：他在窗口内又说了一句，就重新计时
        ——"我打字慢了，第二句才发出去"正是要覆盖的情况。但有个**硬上限**
        `reply_debounce_max_sec`，免得遇到话痨一直等不到发声时机。
        """
        if self._debounce_sec <= 0:
            return [first]
        cap = float(getattr(self.cfg.policy, "reply_debounce_max_sec", 0) or 0)
        started = time.time()
        hard_deadline = started + cap if cap > 0 else float("inf")
        deadline = min(started + self._debounce_sec, hard_deadline)
        burst = [first]
        while time.time() < deadline:
            try:
                nxt = self.hook.recv()
            except Exception:
                break
            if nxt is None:
                time.sleep(0.1)
                continue
            if not self._can_merge(first, nxt):
                self._pending.append(nxt)          # 不属于这次合并：留着让主循环处理
                continue
            burst.append(nxt)
            self.stats["received"] += 1
            self.state.received = self.stats["received"]
            # 群聊环形缓冲也要吃到这条，否则合并后上下文里会缺一句
            try:
                if nxt.is_group and self._group_gate_on():
                    self.group_scheduler.observe(
                        nxt.chat_id, nxt.sender_id, nxt.text,
                        at_me=bool(nxt.mentions_me), is_self=False,
                        at_others=bool(getattr(nxt, "at_others", False)),
                        ts=nxt.ts or None,
                    )
            except Exception:
                pass
            self._log(event="burst_merged", reason="同一个人连发，并进同一次回复",
                      **message_to_dict(nxt))
            print(f"[合并] {nxt.sender_name}: {nxt.text[:40]}  (并进同一次回复)", flush=True)
            # 滑动窗口：他还在打字，就继续等；但不能超过硬上限。
            deadline = min(time.time() + self._debounce_sec, hard_deadline)
        return burst

    def _can_merge(self, first: Message, nxt: Message) -> bool:
        """这条能不能并进 `first` 那一批。

        判据要严：**同一会话 + 同一个人 + 纯文本 + 没被任何闸门拦**。
        任何一条不满足就退回普通流程——宁可多回一条，也不能把别人的话、
        敏感内容或图片混进同一次回复里。
        """
        if nxt is None or nxt.is_self:
            return False
        if nxt.chat_id != first.chat_id:
            return False
        if str(nxt.sender_id or "") != str(first.sender_id or ""):
            return False
        if getattr(nxt, "kind", "text") != "text":
            return False
        reason, _hit = self._filter(nxt)
        return not reason

    def _enqueue_threadsafe(self, msg: Message) -> None:
        """留给未来支持推送式回调的适配器；当前 Mock/wcferry 都走轮询。"""
        self._log(event="worker_push", **message_to_dict(msg))

    def _shutdown(self) -> None:
        # 退出前把"还没摘要"的对话补一次：每天关机都会走到这里，
        # 光靠"回复满 N 次"会让短会话的记忆永远写不进去（2026-10-17 实测）。
        if self._mem_dirty and bool(getattr(self.cfg.memory, "extract_on_exit", True)):
            for chat_id in list(self._mem_dirty)[:3]:
                try:
                    self._extract_memory(chat_id)
                except Exception as exc:            # 收尾失败不能挡住退出
                    self._log(event="memory_extract_error", error=repr(exc), chat_id=chat_id)
        self.state.received, self.state.replied = self.stats["received"], self.stats["replied"]
        self.state.drafted, self.state.ignored = self.stats["drafted"], self.stats["ignored"]
        self.state.errors = self.stats["errors"]
        # 把限速窗口与熔断状态一并落盘：审计指出「重启即清零」等于绕过限速
        self.state.save_limiter(self.limiter.snapshot())
        self.state._save()
        try:
            self.hook.close()
        except Exception as exc:
            print(f"[关闭 hook 失败] {exc!r}")
        self.lock.release()
        print(f"已退出。统计={self.stats}")

    # ---------------- 决策链 ----------------

    def _group_gate_on(self) -> bool:
        """群聊状态机是否接管群聊判决。"""
        return bool(getattr(self.cfg, "group", None) is not None
                    and self.cfg.group.enabled and self.cfg.policy.allow_groups)

    # ---------------- 说话决策（speak_gate） ----------------
    # 与状态机的分工：状态机决定"群醒不醒"，这里决定"醒着的时候这一句接不接"。
    # 判在**取图之前**：判"不说"就省掉一次白下载/白解密/白视觉调用。

    def _speech_gate_on(self, msg: Message) -> bool:
        """只对**群消息**判。私聊没有"该不该插嘴"这回事——谁发来都得回。"""
        return bool(msg.is_group and self._group_gate_on() and self.speak_gate.cfg.enabled)

    def _gate_context(self, msg: Message) -> GateContext:
        """把一条消息翻译成判定层的输入（纯数据）。查状态出错时退化成"没有状态"。"""
        state, last = "", ""
        try:
            state = self.group_scheduler.state_of(msg.chat_id)
            last = self.group_scheduler.last_speaker(msg.chat_id)
        except Exception:
            pass
        return GateContext(
            text=msg.text,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            is_group=msg.is_group,
            is_self=msg.is_self,
            mentions_me=bool(msg.mentions_me),
            at_others=bool(getattr(msg, "at_others", False)),
            at_note=getattr(msg, "at_note", ""),
            quote=getattr(msg, "quote", None),
            group_state=state,
            last_speaker=last,
            bot_asked_question=self._bot_asked_question(msg.chat_id),
        )

    def _bot_asked_question(self, chat_id: str) -> bool:
        """我上一句是不是在**问他**（问号 / 吗 / 呢 / 么 收尾）。

        为什么要这一位：对方回一个"好"，可能是"行，你办吧"——那是答复，不是敷衍；
        也可能是"好（知道了）"——那就不用再回。区分开这两件事，才不会在对方
        答应的时候装死（说话决策层的"只是应了一声"规则依赖它）。
        """
        try:
            items = self.group_scheduler.context(chat_id, 20)
        except Exception:
            return False
        for item in reversed(items):
            if not item.get("self"):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                return False
            return (text[-1] in "？?" or text.endswith(("吗", "呢", "么"))
                    or "？" in text[-3:] or "?" in text[-3:])
        return False

    def _gate_judge_model(self, ctx: GateContext, hints: Optional[list[str]] = None) -> Optional[bool]:
        """`speak_gate.mode=model` 时的那一次判定调用：**让模型自己决定接不接**。

        喂给它的是"这个群最近在聊什么 + 这一条是谁说的 + 几条只有这个客户端才看得到的现象
        （@的是别人 / 引用的是别人 / 只是应一声 / 换了个人说话 …）"，
        它只要回一个词。**解析不出结论就返回 None**，判定层按"不说"处理——
        在"要不要开口"这件事上，宁可少说一句，也不要赌。
        """
        try:
            lines = self.group_scheduler.build_prompt_lines(
                ctx.chat_id, limit=8, name_of=self._group_name_of)
        except Exception:
            lines = []
        hint_block = "\n".join(f"- {h}" for h in (hints or [])) or "（无）"
        messages = [
            {"role": "system", "content": (
                "你是这个微信群里的一个 AI 群友。现在只做一件事：判断**最新那条消息，你要不要接话**。\n"
                "只回一个词：接 / 不接。不要解释，不要输出别的内容。\n"
                "判断标准（按重要程度）：\n"
                "1. 有人 @ 你、引用了你的消息、问了你问题、提到你 → 接；\n"
                "2. 这条是冲着别人说的（@别人、引用别人的话）、或者只是随口应一声 → 通常不接；\n"
                "3. 话题你能自然接上、插一句不会让对话变尴尬 → 可以接；"
                "明显插不上、气氛紧张、别人在争论 → 不接；\n"
                "4. **拿不准就不接**：群里多一个多余的声音，比少一句更讨人厌。"
            )},
            {"role": "user", "content": (
                "最近的聊天记录（每行「昵称：」表示谁说的；`我` 是你自己）：\n"
                + ("\n".join(lines) if lines else "（没有）")
                + f"\n\n现在这一条（{ctx.sender_name or ctx.sender_id}）：{ctx.text}"
                + f"\n\n补充现象：{hint_block}"
                + "\n\n接还是不接？"
            )},
        ]
        try:
            raw = str(self.llm.complete(messages) or "").strip()
        except Exception as exc:
            self._log(event="speak_gate_judge_error", error=repr(exc), chat_id=ctx.chat_id)
            return None
        if raw.startswith("不"):
            return False
        if raw.startswith("接"):
            return True
        return None

    def _group_quiet_reason(self, chat_id: str, state: str) -> str:
        """把群聊"Ignore 的原因码"翻成人话。

        为什么不能一句话概括：日志原来只有"群聊已激活，但这条不是在叫我"，
        于是 **链长用尽 / 追问的人换了 / 这条 @ 了别人** 全都被读成"AI 不认人"，
        用户据此判断"它听不懂谁在说话"（实际是被保险丝掐了）。
        """
        code = ""
        try:
            code = self.group_scheduler.skip_reason(chat_id)
        except Exception:
            code = ""
        chain_cap = getattr(getattr(self.cfg, "group", None), "max_chain_turns", 0)
        table = {
            "self": "自己发出去的那条（只记上下文）",
            "observe": "新群观察期还没过（只记上下文，连 @ 也不回）",
            "idle": "群聊休眠中（没被点名，只记上下文）",
            "at_others": "这条 @ 的是别人，不该插嘴（只记上下文）",
            "followup_other_sender": "追问只接刚才跟我说话的那个人，这条不是他（只记上下文）",
            "chain_limit": f"连续接话已达上限 {chain_cap} 轮，先闭嘴等下一次 @（group.max_chain_turns，0=不限）",
            "window_expired": "追问窗口已过（只记上下文，等下一次 @）",
        }
        if code in table:
            return table[code]
        return ("群聊休眠中（没被点名，只记上下文）" if state == IDLE
                else "群聊已激活，但这条不是在叫我（只记上下文）")

    def route(self, msg: Message) -> Decision:
        self.stats["received"] += 1
        self.state.received = self.stats["received"]
        base = message_to_dict(msg)

        # 群聊状态机：**先喂给调度器**——它负责把每条群消息写进环形缓冲（纯内存，免费），
        # 「被 @ 才醒、静默就睡」的判决也由它出。但判决**放到白名单/敏感词之后**才用，
        # 这样审计里留下的仍是"命中敏感规则""非放行联系人"这类信息量更大的原因。
        group_action = None
        # 只喂**群消息**：私聊不该走状态机（私聊没有"被 @ 才说话"这回事，谁发都得回）
        if msg.is_group and self._group_gate_on():
            group_action = self.group_scheduler.observe(
                msg.chat_id, msg.sender_id, msg.text,
                at_me=bool(msg.mentions_me), is_self=bool(msg.is_self),
                # 这条 @ 的是别人吗（不是我、也不是 @所有人）——那是在跟别人说话。
                # 判据在适配器里算好（那里有原始正文与账号身份），这里只传结论。
                at_others=bool(getattr(msg, "at_others", False)),
                ts=msg.ts or None,
            )

        reason, hit = self._filter(msg)
        if reason:
            self.stats["ignored"] += 1
            self.state.ignored = self.stats["ignored"]
            # 敏感命中单列事件名：审计指出原先一律记成 ignored，按 event 过滤根本捞不出来
            event = "sensitive_block" if hit else "ignored"
            self._log(event=event, reason=reason, **base)
            print(f"[忽略] {msg.sender_name}: {msg.text[:40]}  ({reason})", flush=True)
            return Decision("ignore", reason)

        # 群聊的"该不该醒"判决（最省钱的闸门：不通过就完全不碰模型）
        if group_action == IGNORE:
            state = self.group_scheduler.state_of(msg.chat_id)
            why = self._group_quiet_reason(msg.chat_id, state)
            self.stats["ignored"] += 1
            self.state.ignored = self.stats["ignored"]
            self._log(event="group_quiet", reason=why, group_state=state,
                      at_note=getattr(msg, "at_note", ""), **base)
            print(f"[群聊·{state}] {msg.sender_name}: {msg.text[:40]}  ({why})", flush=True)
            self._schedule_proactive(msg)      # 情景交融：不叫我，也可能过一会儿插一句
            return Decision("ignore", why)

        if self._reload_kill_switch():
            self._log(event="kill_switch", reason="state.json 里 kill_switch=true", **base)
            print("[急停] kill_switch=true，未发送", flush=True)
            return Decision("ignore", "kill_switch")

        # ---- 说话决策层（speak_gate）：**在取图之前**判 ----
        # 位置是刻意的：判"不说"就不去下载/解密图片，省一次视觉调用；也省掉整次模型调用。
        # 它管的是状态机不管的那一类：@别人 / 引用别人 / 只是应了一声 / 别人在说话。
        if self._speech_gate_on(msg):
            verdict = self.speak_gate.judge(self._gate_context(msg),
                                            judge_model=self._gate_judge_model)
            if not verdict.speak:
                self.stats["ignored"] += 1
                self.state.ignored = self.stats["ignored"]
                self._log(event="speak_silent", code=verdict.code, reason=verdict.why,
                          at_note=getattr(msg, "at_note", ""), **base)
                print(f"[群·静音] {msg.sender_name}: {msg.text[:40]}  ({verdict.why})", flush=True)
                # 判"不说"不等于"没看见"：仍然给它一次"冷场后自己决定要不要开口"的机会
                # （只有 group.proactive 打开时才有意义；关着时这一步什么都不做）。
                self._schedule_proactive(msg)
                return Decision("ignore", verdict.why)
            if verdict.judged_by_model:
                self._log(event="speak_gate_model", code=verdict.code,
                          reason=verdict.why, **base)

        # 图片消息：**到这里才去取图 + 理解**。群聊休眠期收到的一堆图片在这之前就被挡掉了，
        # 不会白下载、白解密（图片是双加密容器，密钥还要读微信进程内存）。
        # 描述出来之后**再过一遍敏感词**——图里的二维码/银行卡被描述成文字，同样该拦。
        if getattr(msg, "kind", "text") == "image":
            enriched = self._enrich_image(msg)
            hit = self._sensitive_hit(enriched)
            if hit:
                self.stats["ignored"] += 1
                self.state.ignored = self.stats["ignored"]
                self._log(event="sensitive_block", reason=f"图片描述命中敏感规则「{hit}」", **base)
                print(f"[忽略] 图片描述命中敏感规则「{hit}」", flush=True)
                return Decision("ignore", f"sensitive_image: {hit}")
            msg.text = enriched

        # KI-2 发送防抖：**已经决定要回**了，再多等一会儿，把同一个人紧接着
        # 连发的几条并成一次输入。等待期间别人的消息进 `_pending`，不丢。
        burst = self._collect_burst(msg)
        if len(burst) > 1:
            msg.text = "\n".join(m.text for m in burst)
            print(f"[合并] {len(burst)} 条并成一次回复（{msg.sender_name}）", flush=True)

        # 显式记忆指令（"记住…" / "忘掉…"）：不花模型钱，直接落盘并回一句确认。
        # **必须在写入 context 之前处理**——"记住啊"要拿"上一句"来记，
        # 如果先 append 了自己，取到的就是它自己（等于什么都没记）。
        handled = self._handle_memory_command(msg)
        if handled is not None:
            return handled

        self.context.append(msg.chat_id, "user", self._format_incoming(msg))

        try:
            prompt = self._build_prompt(msg)
            self._prompt_last = prompt                  # 审计要留档，见 `_log` / `_prompt_snapshot`
            reply = self._clean(self.llm.complete(prompt))
        except Exception as exc:
            self.stats["errors"] += 1
            self.state.errors = self.stats["errors"]
            self._log(event="llm_error", error=repr(exc), **base)
            print(f"[模型失败] {exc!r}")
            return Decision("error", f"llm_error: {exc!r}")

        if not reply:
            self.stats["ignored"] += 1
            self.state.ignored = self.stats["ignored"]
            self._log(event="ignored", reason="模型返回空", **base)
            return Decision("ignore", "empty_reply")

        self.context.append(msg.chat_id, "assistant", reply)

        if self.cfg.policy.mode != "draft":
            decision = self._send_reply(msg, reply)
            self._learn_from_chat(msg)              # 每 N 次回复后跑一次"记忆摘要"
            return decision

        # ---- draft 模式：不发，但**照样受限速与静默时段约束** ----
        # 审计指出旧写法把 draft 短路成"无条件写草稿"：既不计额度、也不看静默时段，
        # 于是开启 auto 的瞬间额度是全空的，而你以为一直在受保护。
        # 这里改成：draft 也走 check、也记账（占用的是同一份额度），只是不真的发送。
        blocked = self.limiter.check(*self._rate_key(msg))
        reason = "draft 模式" if not blocked else f"draft 模式（{blocked}）"
        if not blocked:
            self.limiter.record_sent(*self._rate_key(msg))   # 记账：否则切 auto 时额度是空的
        else:
            self._log(event="limiter_block", reason=blocked, mode="draft", **base)
        self._note_spoke(msg.chat_id)
        draft_segments, _ = self._split_reply(reply)      # 草稿也切一遍：切得对不对，这里最直观
        self._write_draft(msg, reply, reason, draft_segments)
        self.stats["drafted"] += 1
        self.state.drafted = self.stats["drafted"]
        self._log(event="draft", reason=reason, reply=reply, segments=draft_segments, **base)
        print(f"[草稿] -> {msg.sender_name}: " + _SEG_JOIN.join(draft_segments)
              + (f"  ⚠ {blocked}" if blocked else ""))
        self._learn_from_chat(msg)
        return Decision("draft", reason, reply)

    # ---------------- 长记忆 ----------------

    @staticmethod
    def _guess_kind(text: str) -> str:
        """粗分记忆类型：偏好 / 约定 / 一般事实。

        只影响**淘汰时的权重**（偏好与约定比闲聊耐留），分错了不影响可用性，
        所以用关键词而不是再调一次模型——记忆写入必须便宜、不能失败。
        """
        t = str(text or "")
        if any(w in t for w in ("喜欢", "讨厌", "爱吃", "不吃", "习惯", "过敏", "偏好", "最怕")):
            return "pref"
        if any(w in t for w in ("约定", "约好", "周五", "明天", "后天", "下周", "点见", "见面", "生日", "截止")):
            return "event"
        return "fact"

    # 显式记忆指令的**宽松**判据（配合 `parse_remember` 的严格判据一起用）。
    # 严格判据只认"行首就是 记住…"，但真机上的自然说法是「你可一定要记住啊」
    # （2026-10-17 实测：这句完全没被识别，用户以为记了，其实一个字都没写）。
    _REMEMBER_HINT = re.compile(r"(?:记住|记一下|记着|记下来|记下|别忘|备忘|帮我记|存一下|remember)", re.I)
    _QUESTION_TAIL = re.compile(r"(?:吗|么|呢|嘛|[?？])\s*$")

    def _memory_candidates(self, msg: Message) -> list[str]:
        """把一条消息拆成若干"可能是指令"的候选串。

        为什么要拆行：防抖合并会把 `@我 我现在说的` 和 `你可一定要记住啊` 拼成一条，
        指令落在**第二行**，行首匹配就漏了。
        """
        raw = str(msg.text or "")
        out: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            for cand in (line, re.sub(r"^\s*@[^ 　]{0,30}[ 　]+", "", line).strip()):
                if cand and cand not in out:
                    out.append(cand)
        return out

    def _memory_instruction(self, msg: Message) -> tuple[Optional[str], bool]:
        """判断这条消息是不是"让我记住"。

        返回 `(内容, 是否记上一句)`；`(None, False)` 表示不是记忆指令。
        """
        mem = self.memory
        cands = self._memory_candidates(msg)
        for cand in cands:                       # 1) 严格：行首就是指令
            content = mem.parse_remember(cand)
            if content:
                return content, False
        for cand in cands:                       # 2) 宽松：句子里提到"记住"，但不是在问"记住了吗"
            if not self._REMEMBER_HINT.search(cand) or self._QUESTION_TAIL.search(cand):
                continue
            m = self._REMEMBER_HINT.search(cand)
            tail = cand[m.end():].strip(" 　，。.!！~～：:、")
            min_chars = int(getattr(self.cfg.memory, "min_chars", 4) or 4)
            if len(tail) >= min_chars:
                return tail, False
            return "", True                      # 只说了"记住啊" ⇒ 记他上一句
        return None, False

    def _prev_user_text(self, chat_id: str) -> str:
        """取这个会话里**上一句用户说的话**（去掉群聊里的「昵称：」前缀）。"""
        try:
            hist = [m for m in self.context.get(chat_id) if m.get("role") == "user"]
        except Exception:
            return ""
        if not hist:
            return ""
        text = str(hist[-1].get("content") or "").strip()
        text = re.sub(r"^[^：:\n]{1,24}[：:]", "", text, count=1).strip()   # 群聊前缀
        text = re.sub(r"^\s*@[^ 　]{0,30}[ 　]+", "", text).strip()
        return text[:200]

    def _handle_memory_command(self, msg: Message) -> Optional[Decision]:
        """处理"记住…" / "忘掉…"。

        为什么绕过模型：这两件事用户说的是**指令**，不是聊天内容。
        交给模型它可能先寒暄两句再"好吧我记住了"，还会白花一次调用。

        **2026-10-17 修**：①支持多行（防抖合并后指令可能在第二行）；②支持"记住啊"这种
        没说记什么的口语 → 记上一句；③`add_verbose()` 区分 added/duplicate/too_short，
        不再把"没记成"说成"已经记着了"（那是最危险的一种失败：静默 + 相反反馈）。
        """
        mem = self.memory
        if not mem.enabled:
            return None
        content, use_prev = self._memory_instruction(msg)
        if content is not None:
            source_text = content
            if use_prev or not content:
                source_text = self._prev_user_text(msg.chat_id)
                if not source_text:
                    # 真机踩到（2026-10-18 15:29）：会话里的第一句就是「我说的你可得记住啊」，
                    # 上一句根本不存在 ⇒ 这里以前 return None 放行，模型接了句"记住了记住了"，
                    # **既没记任何东西又给了保证**，用户之后问"你记得吗"它只能顺嘴编。
                    # 正确做法：明确反问，且不交出话语权给模型。
                    self._log(event="memory_skipped",
                              reason="让我记住但没说记什么，也没有上一句可记（已反问）",
                              **message_to_dict(msg))
                    print("[记忆] 没说要记什么，已反问", flush=True)
                    return self._send_reply(msg, "记啥呀？你说清楚点我才好记。")
            kind = self._guess_kind(source_text)
            status = mem.add_verbose(msg.chat_id, source_text, kind=kind, source="explicit")
            acks = {
                "added": "嗯，记下了",
                "duplicate": "这个我已经记着了",
                "too_short": "这句太短了，说具体点我才好记",
                "disabled": "记忆功能没开",
            }
            self._log(event=f"memory_{status}", reason="用户让我记住", memory_text=source_text,
                      kind=kind, from_previous=bool(use_prev), **message_to_dict(msg))
            print(f"[记忆] {status}：{source_text}", flush=True)
            return self._send_reply(msg, acks.get(status, "嗯，记下了"))
        forget = next((r for r in (mem.parse_forget(c) for c in self._memory_candidates(msg)) if r), None)
        if forget:
            removed = mem.forget(msg.chat_id, forget)
            ack = "好，那个我忘了" if removed else "这条我这儿本来就没有"
            self._log(event="memory_forgotten", reason="用户让我忘掉",
                      needle=forget, removed=removed, **message_to_dict(msg))
            print(f"[记忆] 忘掉 {removed} 条：{forget}", flush=True)
            return self._send_reply(msg, ack)
        return None

    def _learn_from_chat(self, msg: Message) -> None:
        """每次回复后调用：记下"这个会话有新对话"，够条件才真的去摘要。

        **摘要的三个触发点**（缺一个短会话就永远轮不到）：
        ① 回复满 `extract_every` 次；② 空闲超过 `extract_idle_sec`（主循环里查）；
        ③ 桥接退出前（每天关机都会发生）。
        """
        mem = self.memory
        if not mem.enabled:
            return
        self._mem_dirty.add(msg.chat_id)
        self._mem_last_ts[msg.chat_id] = time.time()
        every = int(getattr(self.cfg.memory, "extract_every", 0) or 0)
        if every <= 0:
            return
        n = self._mem_turns.get(msg.chat_id, 0) + 1
        self._mem_turns[msg.chat_id] = n
        if n % every == 0:
            self._extract_memory(msg.chat_id)

    def _extract_memory(self, chat_id: str) -> bool:
        """让模型从最近几轮对话里抽"值得长期记住的事"。失败绝不冒泡。"""
        mem = self.memory
        if not mem.enabled:
            return False
        turns = int(getattr(self.cfg.memory, "extract_context_turns", 6) or 6)
        hist = self.context.get(chat_id)[-turns * 2:]
        transcript = [
            f"{'我' if m.get('role') == 'assistant' else '对方'}: {m.get('content', '')}"
            for m in hist if str(m.get("content") or "").strip()
        ]
        if not transcript:
            self._mem_dirty.discard(chat_id)
            return False
        limit = int(getattr(self.cfg.memory, "extract_max_items", 4) or 4)
        self._mem_extract_runs += 1
        # 审计里放**这次真正发给模型的抽取提示词**（不是上一条回复的提示词——
        # 2026-10-18 排查时就是因为这一点被误导：审计里的 prompt 看着是回复提示词）。
        extract_msgs = mem.extract_prompt(transcript, limit=limit)
        self._log(event="memory_extract_start", chat_id=chat_id, turns=len(transcript),
                  prompt=extract_msgs)
        stage = 1
        try:
            raw = self.llm.complete(extract_msgs)
            items = mem.parse_extract(raw)[:limit]
            # **第二遍**：第一遍模型常把"玩笑/吹牛"整段判成不值得记（真机实测：
            # 同一段对话，普通提示词给 `[]`，加一句"玩笑也要记稳定属性"就抽出 2 条）。
            # 只在第一遍交白卷、且上下文够长时重试一次，成本可控。
            if not items and len(transcript) >= 4:
                stage = 2
                raw2 = self.llm.complete(mem.extract_prompt(transcript, limit=limit, aggressive=True))
                items = mem.parse_extract(raw2)[:limit]
                if items:
                    self._log(event="memory_extract_retry_hit", chat_id=chat_id,
                              items=[t for _k, t in items])
        except Exception as exc:
            self._log(event="memory_extract_error", error=repr(exc), chat_id=chat_id)
            return False
        added: list[str] = []
        for kind, text in items:
            if mem.add(chat_id, text, kind=kind, source="auto"):
                added.append(text)
        self._mem_dirty.discard(chat_id)
        if added:
            self._log(event="memory_learned", chat_id=chat_id, items=added, stage=stage)
            print(f"[记忆] 学到 {len(added)} 条（第 {stage} 遍）：{'；'.join(added)}", flush=True)
        else:
            # 摘要跑了但一条没加：可能是模型觉得没东西可记，也可能是输出解析失败。
            # **必须留痕**，否则"到底跑没跑"又一次只能靠猜（2026-10-18 就猜过一次）。
            self._log(event="memory_extract_empty", chat_id=chat_id, stage=stage,
                      raw_preview=str(raw or "")[:200])
            print(f"[记忆] 摘要无新增（第 {stage} 遍，模型说：{str(raw or '')[:60]}）", flush=True)
        self._maybe_compact(chat_id)
        return bool(added)

    # ---------------- 情景交融（看见全场 → 自己决定要不要开口） ----------------

    def _schedule_proactive(self, msg: Message) -> None:
        """一条**没在叫我**的群消息之后，安排一次"到时候看看要不要插话"。

        为什么不立刻决定：真人不会在别人话说到一半时插嘴。**先等冷场**
        （`proactive_silence_sec`），期间有人继续说话就把时间往后推。
        """
        cfg = self.cfg.group
        if not getattr(cfg, "proactive", False) or not self._group_gate_on():
            return
        if msg.is_self or bool(getattr(msg, "at_others", False)):
            return
        try:
            if self.group_scheduler.skip_reason(msg.chat_id) in ("self", "at_others"):
                return
            ring = self.group_scheduler.context(msg.chat_id)
        except Exception:
            return
        if len(ring) < int(getattr(cfg, "proactive_min_history", 3) or 3):
            return
        silence = float(getattr(cfg, "proactive_silence_sec", 25.0) or 0)
        self._proactive_due[msg.chat_id] = time.time() + silence

    def _maybe_proactive_speak(self) -> None:
        """主循环空闲时调用：到点且确实冷场，就让模型决定"说一句"还是"闭嘴"。"""
        cfg = self.cfg.group
        if not getattr(cfg, "proactive", False) or not self._group_gate_on() or not self._proactive_due:
            return
        now = time.time()
        silence = float(getattr(cfg, "proactive_silence_sec", 25.0) or 0)
        cooldown = float(getattr(cfg, "proactive_cooldown_sec", 180.0) or 0)
        budget = int(getattr(cfg, "proactive_budget_per_hour", 2) or 0)
        for chat_id, due in list(self._proactive_due.items()):
            if now < due:
                continue
            del self._proactive_due[chat_id]
            if now - self._group_last_ts.get(chat_id, 0.0) < silence:
                continue                                   # 又有人说话了：这次不插
            if now - self._own_speak_ts.get(chat_id, 0.0) < cooldown:
                self._log(event="proactive_skip", chat_id=chat_id, reason="cooldown")
                continue
            self._proactive_times = [t for t in self._proactive_times if now - t < 3600]
            if budget <= 0 or len(self._proactive_times) >= budget:
                self._log(event="proactive_skip", chat_id=chat_id, reason="budget",
                          used=len(self._proactive_times), budget=budget)
                continue
            self._try_proactive(chat_id, now)

    _SILENT_MARK = "<SILENT>"

    def _build_proactive_prompt(self, chat_id: str) -> list[dict[str, str]]:
        """插话用的提示词：**明确允许它不说话**，否则模型会为了"有用"硬接一句。"""
        system = (self.cfg.persona.system_prompt or DEFAULT_SYSTEM_PROMPT).strip()
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        try:
            mem = self.memory.render(chat_id, " ".join(
                str(x.get("text") or "") for x in self.group_scheduler.context(chat_id)[-6:]))
        except Exception:
            mem = ""
        if mem:
            messages.append({"role": "system", "content": mem})
        messages.append({
            "role": "system",
            "content": (
                "下面是这个群**最近的真实聊天记录**（每行「昵称：」标明是谁说的，`我` 是你自己）。\n"
                "没有人 @ 你。你要**自己判断要不要插一句**：\n"
                "- 话题跟你有关（聊到你、你记得的事、你刚说过的事）、或者群里明显冷场且你能接得上 → 插一句；\n"
                "- 与你无关、别人在争论、气氛紧张、或者你觉得插进去很突兀 → **不要说话**。\n"
                f"要说话就只输出那一句（口语、一条、不复述记录、不解释）；不开口就只输出 {self._SILENT_MARK}。"
            ),
        })
        lines = self.group_scheduler.build_prompt_lines(
            chat_id, limit=12, name_of=self._group_name_of)
        messages.append({"role": "user", "content": "\n".join(lines) + "\n\n现在决定："})
        return messages

    def _try_proactive(self, chat_id: str, now: float) -> None:
        ring = []
        try:
            ring = self.group_scheduler.context(chat_id)
        except Exception:
            pass
        limit = int(getattr(self.cfg.group, "proactive_min_history", 3) or 3)
        if len(ring) < limit:
            self._log(event="proactive_skip", chat_id=chat_id, reason="history")
            return
        try:
            prompt = self._build_proactive_prompt(chat_id)
            self._prompt_last = prompt
            raw = self.llm.complete(prompt)
        except Exception as exc:
            self._log(event="proactive_error", chat_id=chat_id, error=repr(exc))
            return
        text = str(raw or "").strip()
        if not text or self._SILENT_MARK in text.upper() or text.upper().startswith("SILENT"):
            self._log(event="proactive_skip", chat_id=chat_id, reason="decided_silent",
                      raw_preview=text[:120])
            print("[群聊·插话] 决定不开口", flush=True)
            return
        reply = self._clean(text)
        if len(reply) < 2:
            self._log(event="proactive_skip", chat_id=chat_id, reason="too_short",
                      raw_preview=text[:120])
            return
        last = ring[-1] if ring else {}
        sender = str(last.get("sender") or "")
        pseudo = Message(
            chat_id=chat_id, sender_id=sender,
            sender_name=self._group_name_of(sender) if sender else "群友",
            text=str(last.get("text") or ""), is_group=True,
            msg_id=f"proactive:{int(now)}", ts=now,
        )
        setattr(pseudo, "no_at", True)              # 主动插话不 @ 人（显得突兀）
        self._proactive_times.append(now)
        self._log(event="proactive_speak", chat_id=chat_id, reply=reply,
                  based_on=str(last.get("text") or "")[:80])
        print(f"[群聊·插话] -> {pseudo.sender_name}: {reply}", flush=True)
        self._send_reply(pseudo, reply)

    def _maybe_extract_idle(self) -> None:
        """主循环空闲时调用：**短会话没到 N 次回复也会被补摘要**。

        这是 2026-10-17「长记忆零触发」的直接修复：那场测试只回了 6 次，
        永远等不到阈值 10，于是什么都没写。
        """
        idle = float(getattr(self.cfg.memory, "extract_idle_sec", 0) or 0)
        if idle <= 0 or not self.memory.enabled or not self._mem_dirty:
            return
        if time.time() - self._last_recv_ts < idle:
            return
        for chat_id in list(self._mem_dirty)[:2]:    # 一次最多两个会话，空闲时也别长时间占着
            self._extract_memory(chat_id)

    def _maybe_compact(self, chat_id: str) -> None:
        """把太老的记忆压成摘要，原文进归档。

        为什么不在 `add()` 里做：压缩要调模型（贵、慢、可能失败），
        而 `add()` 在"用户说记住"这条最该快的路径上。所以放到回复之后、按会话限频。
        """
        cfg = self.cfg.memory
        hours = float(getattr(cfg, "compact_check_hours", 6) or 0)
        if hours > 0 and (time.time() - self._mem_compact_at.get(chat_id, 0.0)) < hours * 3600:
            return                              # 刚查过，别每次都扫
        self._mem_compact_at[chat_id] = time.time()
        try:
            stale = self.memory.stale_items(chat_id)
        except Exception as exc:
            self._log(event="memory_compact_error", error=repr(exc), chat_id=chat_id)
            return
        if not stale:
            return
        try:
            result = self.memory.compact(chat_id, summarizer=self.llm.complete)
        except Exception as exc:                # 压缩失败不能拖垮回复链路
            self._log(event="memory_compact_error", error=repr(exc), chat_id=chat_id)
            return
        self._log(event="memory_compacted", chat_id=chat_id, **result)
        print(f"[记忆] 压缩：归档 {result.get('archived', 0)} 条，"
              f"生成摘要 {result.get('digests', 0)} 条"
              + ("（摘要失败，已只归档原文）" if result.get("fallback") else ""), flush=True)

    def _note_spoke(self, chat_id: str) -> None:
        """告诉群聊调度器"我说了话"——追问窗口的判据全靠它。

        三种情况都算：真发出去 / 空跑 / 写草稿。理由：draft 与空跑是在**模拟** auto 的行为，
        不记的话你会看到一个与实际不一致的判据（draft 下永远不触发追问），
        观察到的草稿质量也就不能代表切 auto 之后的样子。
        """
        try:
            self.group_scheduler.note_bot_spoke(chat_id)
        except Exception as exc:                     # 调度器出问题不能拖垮发送链路
            self._log(event="group_state_error", error=repr(exc), chat_id=chat_id)
        self._own_speak_ts[chat_id] = time.time()    # 情景交融：自己刚说完，短时间内别再主动开口

    def _split_reply(self, reply: str) -> tuple[list[str], list[str]]:
        """把回复切成要**逐条发出**的若干段。返回 (段落, 越界说明)。

        关了多段输出就原样一条。段数/长度越界时**不丢内容**——越界说明会进审计，
        因为"模型没守约定"这件事必须看得见才修得了。
        """
        cfg = self.cfg.segments
        if not cfg.enabled:
            return [reply], []
        result = split_segments(reply, max_segments=cfg.max_segments, max_chars=cfg.max_chars)
        segments = result.segments or [reply]
        notes = list(result.notes)
        # 模型没按 `|||` 分段时，按句末标点自己拆一次。
        # 真机实测 112 条回复里 0 条用了约定分隔符——**约定式方案不能只有约定**，
        # 否则"像真人连说几句"永远只是 README 上的一句话。
        if len(segments) == 1 and getattr(cfg, "auto_split", True):
            auto = auto_split(
                segments[0],
                max_segments=cfg.max_segments,
                max_chars=cfg.max_chars,
                min_total_chars=int(getattr(cfg, "auto_split_min_chars", 14) or 14),
            )
            if auto:
                notes.append("模型没给分隔符，已按句末标点自动分成 %d 条" % len(auto))
                segments = auto
        return segments, notes

    def _collapse_if_gui(self, segments: list[str], notes: list[str],
                         msg: Message) -> tuple[list[str], list[str]]:
        """多段 + 目标会话没开着 ⇒ **压成一条**再发。

        为什么要压：后台发送要求目标会话正开着（主窗口当前会话就是它，或它有独立窗口）。
        没开着就只剩 GUI 那条路——它会打开会话、把微信弹到前台、抢一次焦点。
        三段就是抢三次；真机 20:48 那次更糟：第一段后台没成 → 退 GUI → 卡 90 秒 →
        同一个句子最后在群里出现了三遍。

        为什么不用 `send_mode=background` 硬扛：那会把"会话没开着"直接变成"这条不发"，
        群里看起来就是它突然不理人。压成一条既发了、又只打扰一次。

        问一次就够：真到发送时窗口被用户切走了，适配器那边照旧走 GUI（不做乐观假设）。
        """
        if len(segments) <= 1:
            return segments, notes
        check = getattr(self.hook, "can_send_background", None)
        if check is None:          # 假 hook / 干跑：没有这个概念
            return segments, notes
        try:
            available = bool(check(msg.chat_id))
        except Exception:
            return segments, notes
        if available:
            return segments, notes
        merged = "".join(segments).strip()
        limit = int(getattr(self.cfg.policy, "max_reply_chars", 200) or 200)
        if len(merged) > limit:
            merged = merged[:limit]
        print(f"[合并] 目标会话没开着，{len(segments)} 段压成 1 条发"
              f"（否则要连抢 {len(segments)} 次焦点）", flush=True)
        return [merged], notes + [f"no_background: 会话没开着，{len(segments)} 段压成 1 条"]

    @staticmethod
    def _rate_key(msg: Message) -> tuple[str, str]:
        """限速口径：群聊按「这个群 + **这个人**」，私聊只按会话。

        为什么群聊要按人分：额度语义是"单联系人每小时 20 条"（防对着一个人刷屏）。
        按群算等于**整个群共用一份**——十几个人聊天，几分钟就花完，现象是
        「群里正聊得热闹，它突然不吭声」，而日志写的是「触发单联系人限速」：
        名字对、意思错（真正被限的是整个群）。私聊不传第二项，行为与从前完全一致。
        """
        return msg.chat_id, (msg.sender_id if msg.is_group else "")

    def _send_reply(self, msg: Message, reply: str) -> Decision:
        # 限速/静默在**发送前**判定（旧写法把 check 放在分支外面，draft 下等于不判）
        # 口径见 `_rate_key`：群聊按"这个群里这个人"算额度，否则十几个人说话几分钟就把
        # 20 条/小时的花完，表现为"群里正热闹它突然不吭声"。
        blocked = self.limiter.check(*self._rate_key(msg))
        if blocked:
            self.stats["ignored"] += 1
            self.state.ignored = self.stats["ignored"]
            self._log(event="limiter_block", reason=blocked, mode=self.cfg.policy.mode,
                      reply=reply, **message_to_dict(msg))
            print(f"[限速拦截] {blocked} -> 只留草稿", flush=True)
            self._write_draft(msg, reply, blocked)
            return Decision("draft", blocked, reply)

        segments, seg_notes = self._split_reply(reply)
        # 会话没开着时，多段会变成"连弹 N 次窗口抢 N 次焦点"——先压成一条。
        # 必须在 dry_run 之前：空跑要报**真会发什么**，否则观察到的条数与真实不符。
        segments, seg_notes = self._collapse_if_gui(segments, seg_notes, msg)

        lo, hi = self.cfg.policy.reply_delay_sec
        time.sleep(random.uniform(lo, hi))          # 拟人延迟，避免秒回特征

        if self.dry_run:                            # 空跑：只报会发什么，绝不真的发出去
            for _ in segments:                      # 空跑也按**条数**记账，观察到的额度与真实一致
                self.limiter.record_sent(msg.chat_id)
            self.stats["replied"] += 1
            self.state.replied = self.stats["replied"]
            self._note_spoke(msg.chat_id)
            self._log(event="dry_run", reply=reply, segments=segments, segment_notes=seg_notes,
                      **message_to_dict(msg))
            print(f"[空跑·未发送] -> {msg.sender_name}: " + _SEG_JOIN.join(segments), flush=True)
            return Decision("reply", "dry_run", reply)

        # ---- 逐段发送：真人发微信是分几次发的，节奏只能在发送侧补 ----
        gap_lo, gap_hi = self.cfg.segments.gap_sec
        # 群聊里要 @ 回对方：**人多的时候不 @ 就没人知道你在回谁**（对着空气说话）。
        # 只加在第一条上——真人 @ 一次就够了，每条都 @ 反而像机器。
        at_name = ""
        if (msg.is_group and self.cfg.group.at_back and msg.sender_name
                and not getattr(msg, "no_at", False)):   # 主动插话不 @ 人
            at_name = msg.sender_name
        sent = 0
        stop_reason = ""
        try:
            for index, text in enumerate(segments):
                if index:
                    time.sleep(random.uniform(gap_lo, gap_hi))
                    # **每段都重新判限速**：三条消息就是三条消息，不能拿"一次回复"的名义
                    # 绕过按条计的额度——群里看到的刷屏风险是按条算的。
                    blocked = self.limiter.check(*self._rate_key(msg))
                    if blocked:
                        stop_reason = blocked
                        break
                if not self.hook.send(msg.chat_id, text, at=at_name if index == 0 else ""):
                    break
                sent += 1
                self.limiter.record_sent(*self._rate_key(msg))
        except Exception as exc:
            self._log(event="send_error", error=repr(exc), reply=reply, segments=segments,
                      sent_segments=sent, total_segments=len(segments), **message_to_dict(msg))
            print(f"[发送异常] {exc!r}", flush=True)

        if sent == len(segments):
            self.limiter.record_success()
            self.stats["replied"] += 1
            self.state.replied = self.stats["replied"]
            self._note_spoke(msg.chat_id)
            self._log(event="sent", reply=reply, segments=segments, segment_notes=seg_notes,
                      **message_to_dict(msg))
            print(f"[已回复] -> {msg.sender_name}: " + _SEG_JOIN.join(segments), flush=True)
            return Decision("reply", "sent", reply)

        # ---- 一条都没发出去：与旧行为一致（记失败、可能熔断）----
        if sent == 0:
            self.limiter.record_failure()
            self.stats["errors"] += 1
            self.state.errors = self.stats["errors"]
            self._log(event="send_failed", reply=reply, segments=segments,
                      reason=stop_reason, **message_to_dict(msg))
            print(f"[发送失败] 连续失败 {self.limiter.consecutive_failures} 次"
                  + (f"（{stop_reason}）" if stop_reason else ""), flush=True)
            if self.limiter.frozen:
                self._log(event="limiter_frozen", reason=self.limiter.frozen_reason)
                print(f"[熔断] {self.limiter.frozen_reason} —— 解除方式：确认问题后加 --resume 重启", flush=True)
            self.state.save_limiter(self.limiter.snapshot())
            return Decision("error", "send_failed", reply)

        # ---- 只发出去一部分：**绝不能记成 sent** ----
        # 群里已经看到半句话，审计必须如实呈现，否则"回复成功"是假的。
        self.limiter.record_failure()
        self.stats["errors"] += 1
        self.state.errors = self.stats["errors"]
        self._log(event="send_partial", reply=reply, segments=segments,
                  sent_segments=sent, total_segments=len(segments), reason=stop_reason,
                  **message_to_dict(msg))
        print(f"[部分送达] 只发出 {sent}/{len(segments)} 条"
              + (f"（{stop_reason}）" if stop_reason else "")
              + " —— 群里已看到半句话，请检查", flush=True)
        self.state.save_limiter(self.limiter.snapshot())
        return Decision("error", "send_partial", reply)

    # ---------------- 风控闸门 ----------------

    def _filter(self, msg: Message) -> tuple[Optional[str], str]:
        p = self.cfg.policy
        if not msg.text.strip():
            return "非文本消息", ""
        if msg.is_self:
            return "自己发的消息（防自问自答死循环）", ""
        if msg.is_group:
            if not p.allow_groups:
                return "群聊未放行（allow_groups=false）", ""
            # 群聊"该不该醒"**由状态机独家决定**（它把 @我 / @所有人 / 休眠 / 防自我循环
            # 一起判了）。这里只在状态机没启用时，才退回旧的"没 @ 就不回"。
            # 两处都判会让旧规则抢先把状态机的判决短路掉，症状是"新功能好像没生效"。
            if not self._group_gate_on() and p.reply_only_when_mentioned_in_group and not msg.mentions_me:
                return "群里未 @ 我", ""

        level = self._match_contact_rule(msg)
        if level == "ignore":
            return "联系人规则=ignore", ""
        if level == "deny":
            return "联系人规则=deny", ""
        if level != "allow":
            # **新群自动接入**：被拉进一个新群时，它的 id 不可能预先出现在
            # contact_rules 里。`allow_any_group=true` 就是为这件事准备的：
            # 群聊即使没有规则也放行，于是"拉进新群 → @ 它 → 它就干活"。
            #
            # 为什么这样是安全的：显式的 ignore / deny 在上面两条就已经拦掉了
            # （显式拒绝优先），而放行不等于会说话——它仍然要过"@ 才回"、
            # 群状态机、限速、静默时段、敏感词、新群观察期。
            # 只对群聊生效：私聊不受影响，仍走 allow_unknown_contacts。
            if not (msg.is_group and p.allow_any_group) and not p.allow_unknown_contacts:
                return "非放行联系人（allow_unknown_contacts=false）", ""

        # 时效闸门：机器关机/睡眠期间积压的消息，起床后**不要**逐条补回。
        # 否则早上八点会对着昨晚的聊天记录刷屏——既像机器人，也只有坏处。
        # 设 policy.max_reply_age_min = 0 可关闭这道闸门（想"补上"所有积压时用）。
        age_min = self.cfg.policy.max_reply_age_min
        if age_min and msg.ts:
            age = (time.time() - msg.ts) / 60
            if age > age_min:
                return f"消息太旧（{age:.0f} 分钟前，超过 {age_min} 分钟不回）", ""

        hit = self._sensitive_hit(msg.text)
        if hit:
            return f"命中敏感规则「{hit}」，需人工处理", hit
        return None, ""

    def _sensitive_hit(self, text: str) -> Optional[str]:
        """敏感词分两层：正则优先，再退到字面子串。

        **必须先归一化**（审计发现：不做归一化时「帮我转五百块钱」「转\\u200b500块」
        「把验 证 码发我一下」全部漏过）。先对原文跑一遍，再对折叠后的文本跑一遍；
        规范化用于命中判断，只有"钱/凭证"这类高危词才连中文数字一起匹配，
        免得像"转发"这种正常词被误伤。
        """
        p = self.cfg.policy
        for candidate in (text, normalize_for_match(text)):
            for pattern in p.deny_regex:
                if not pattern:
                    continue
                try:
                    if re.search(pattern, candidate):
                        return f"regex:{pattern}"
                except re.error as exc:                # 配错的正则不能拖垮整条链路
                    self._log(event="config_error", detail=f"deny_regex 非法: {pattern} ({exc})")
            for word in p.deny_patterns:
                if word and word in candidate:
                    return word
        return None

    def _match_contact_rule(self, msg: Message) -> Optional[str]:
        """联系人规则：**只按 id 精确匹配**（聊天 id 优先，其次发送者 id）。

        审计发现：旧写法把 `昵称|sender_id|chat_id` 拼成一个字符串做子串包含，
        于是任何把昵称改成 `wxid_alice的小号` 的人都能被当成白名单里的 Alice 获得代回权；
        反过来 `wxid_bad_2` 会被 `wxid_bad` 误伤。昵称是**对方可控**的字段，不能参与授权判断。
        """
        rules = self.cfg.policy.contact_rules
        for key in (msg.chat_id, msg.sender_id):
            if key and key in rules:
                return str(rules[key])
        # 昵称规则单列（默认空）。它天然可被对方改名绕过/冒用，加上就得多看一眼安全清单。
        nick = (msg.sender_name or "").strip()
        if nick and nick in self.cfg.policy.nickname_rules:
            return str(self.cfg.policy.nickname_rules[nick])
        return None

    # ---------------- 提示词与文本处理 ----------------

    def _format_incoming(self, msg: Message) -> str:
        return f"{msg.sender_name}：{msg.text}" if msg.is_group else msg.text

    def _group_name_of(self, uid: str) -> str:
        """把 wxid 换成昵称（给群聊上下文用）。查不到就原样返回。"""
        if not uid:
            return "?"
        try:
            db = getattr(getattr(self.hook, "_wx", None), "_db", None)
            if db is not None:
                return db.nickname_map().get(uid, uid) or uid
        except Exception:
            pass
        return uid

    def _enrich_image(self, msg: Message) -> str:
        """把图片消息变成给模型看的文本。

        **看不了就如实说看不了**，绝不假装看懂——"我以为它看了图"比"它说没配后端"危险得多。
        """
        if getattr(msg, "kind", "text") != "image":
            return msg.text                              # 类型防御：不是图片就原样返回
        if self.vision is None:
            return "[图片]（没有配视觉后端，看不到内容）"
        try:
            # local_id 显式传进去：两种 raw 形状里有一路不一定带它，缺了就静默取不到图。
            path = self.hook.download_image(msg.chat_id, msg.raw,
                                            getattr(msg, "local_id", None))
        except Exception as exc:                      # 取图失败不能拖垮整条链路
            self._log(event="image_error", error=repr(exc), **message_to_dict(msg))
            path = None
        if not path:
            # 适配器会在 `image_error` 里留一句人话原因（比如"wxgf 需要 ffmpeg 但没找到"）。
            # 原因只给日志看：**不要塞进给模型的占位符**——那会让它在群里开始讲技术细节，
            # 模型只需要知道"这张图我没拿到"，据实说一句就够。
            reason = str(getattr(self.hook, "image_error", "") or "").strip()
            if reason:
                self._log(event="image_unreadable", error=reason, **message_to_dict(msg))
                print(f"[取图] {reason}", flush=True)
            return "[图片]（取不到原图）"
        try:
            desc = self.vision.describe(path)
        except Exception as exc:
            self._log(event="vision_error", error=repr(exc), **message_to_dict(msg))
            print(f"[视觉] 理解失败：{exc!r}", flush=True)
            return "[图片]（没看懂这张图）"
        self._log(event="image_described", description=desc[:300], **message_to_dict(msg))
        print(f"[视觉] {desc[:60]}", flush=True)
        return f"[图片：{desc}]"

    def _build_prompt(self, msg: Message) -> list[dict[str, str]]:
        system = (self.cfg.persona.system_prompt or DEFAULT_SYSTEM_PROMPT).strip()
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]

        # 表情：真人发微信会带 emoji / 颜文字。这条**由桥接注入**而不是写进 system_prompt，
        # 因为用户的配置多是从模板复制来的，改模板不会改到已有配置。
        if getattr(self.cfg.persona, "allow_emoji", True):
            messages.append({
                "role": "system",
                "content": ("情绪到位的时候可以用 emoji 或者颜文字：😂 😅 🤔、(￣▽￣)、"
                            "(╯°□°)╯、_(:з」∠)_、orz、QAQ。"
                            "**两三条里至少有一条带一点点**（全都干巴巴的就像客服机器人），"
                            "但一条消息里最多一个，别写成一长串。"
                            "无语、摆烂、被噎住的时候优先用颜文字，轻松打趣的时候用 emoji；"
                            "它们只用来表达情绪，不能代替要说的话。"),
            })

        if getattr(msg, "kind", "text") == "sticker":
            # 表情包本身看不到（真表情包是微信私有格式，没有发送/读取接口），
            # 但**必须让它知道对方发了表情**，否则在群里就是"对方甩了个表情、它装没看见"。
            messages.append({
                "role": "system",
                "content": ("对方发的是**表情包**（你看不到图长什么样）。别问『你发的什么』，"
                            "可以短评一句、或者回一个 emoji 接梗。"),
            })

        if self.cfg.segments.enabled:
            # 多段输出的**约定要写进提示词**，否则桥接再怎么切也只有一段。
            # 说清楚"最多几条、每条多长"，以及它的目的是"像真人分几次发"，而不是让模型多写。
            seg = self.cfg.segments
            messages.append({
                "role": "system",
                "content": f"你想连说几句的时候，用 ||| 把每条消息分开（最多 {seg.max_segments} 条，"
                           f"每条不超过 {seg.max_chars} 字）。例如："
                           "「等下我看看|||好像是你那个群|||笑死，我懂了」——它会像真人一样分三次发出去。"
                           "一句话能说完就**不要**加分隔符；别为了凑段数硬拆。",
            })

        # 长记忆：把"关于这个人我还记得的事"塞进来。
        # 没有这一块，隔一天、或者换个话题回来，它会当第一次认识你——
        # 因为 context 只有最近几轮，昨天的内容**根本没被喂进来**。
        mem_block = ""
        try:
            mem_block = self.memory.render(msg.chat_id, msg.text)
        except Exception as exc:                      # 记忆坏了也不能影响回复
            self._log(event="memory_error", error=repr(exc), chat_id=msg.chat_id)
        if mem_block:
            messages.append({"role": "system", "content": mem_block})

        if msg.is_group:
            # 群聊：把**环形缓冲里的最近上下文**一起喂进去。
            # 这是状态机存在的意义之一——休眠期它不是没看见，只是没花钱调用模型；
            # 醒来时能接上刚才的话题，才不会一脸茫然地反问"什么？"。
            lines = self.group_scheduler.build_prompt_lines(
                msg.chat_id, limit=self.cfg.context.max_turns * 2, name_of=self._group_name_of)
            if lines:
                # ⚠ 光把记录贴上去不够。实测：群里第三个人插话、用户回应之后，
                # 模型仍然当成"一对一"只接最后一句——因为它不知道
                # "哪条是要回的那条""群里还有别人在说话"。这三条必须写明。
                messages.append({
                    "role": "system",
                    "content": (
                        "以下是这个群最近的聊天记录（越靠后越新，`我` 是你自己）：\n"
                        + "\n".join(lines)
                        + "\n\n读这段记录时请注意：\n"
                        f"1. **最后一条就是现在要你回应的那条**，是 {msg.sender_name} 说的。\n"
                        "2. 群里**不止你们两个人**——别人插的话、你自己刚才说的话，都是语境的一部分。\n"
                        "   要先判断他这句话是在跟谁说、在回应谁的哪句话：如果他在回应别人、"
                        "或者只是对别人感叹一句，你可以只回很短的半句，或者干脆不接。\n"
                        "3. 每行开头「昵称：」表示**这句话是谁说的**；写成 `@我` 的地方，"
                        "意思是那条消息 @ 了你。要能分清哪句是谁说的，别看混。\n"
                        "4. 对方短促地反问（`你说什么`/`啥意思`/`你又怎么了`）多半是**没听清、没跟上**，"
                        "换个说法把关键信息再说一遍就行；**不要自辩、不要揣测对方在指责你**。\n"
                        "5. 别耍脾气、别阴阳怪气：`就这？`/`记住下次别考我` 这种话不要说。\n"
                        "6. **你没法凭空 @ 别人**：正文里不要写「@某某」——写在正文里微信只当普通文字，"
                        "对方收不到提醒，群里却会以为你艾特了他。别人让你艾特谁，就直说"
                        "「我艾特不了，你自己艾特吧」，**不要假装已经艾特了**。\n"
                        "7. 不要把聊天记录复述出来，也不要解释自己在做什么，像真人在群里随口接一句。"
                    ),
                })
            messages.append({
                "role": "system",
                "content": "注意：这是**群聊**，别人不一定在跟你说话。"
                           "回复要短（就像在微信群里发消息），不要长篇大论，不要 @ 所有人，"
                           "不要用 Markdown 或编号列表。",
            })

        messages.extend(self.context.get(msg.chat_id)[-self.cfg.context.max_turns * 2 :])
        messages.append({"role": "user", "content": "直接给出这一条要回复的内容，不要任何前缀说明。"})
        return messages

    # 正文里的 `@某某`：模型随时会写，但**微信只把它当普通字符串**——
    # 没有真实 @ 效果、不会提醒对方，群里却看着像"它 @ 了人"。
    _AT_TEXT_RE = re.compile(r"@([^\s@，。,.!?！？:：；;、）)]{1,20})")
    # 「我@不了」这种是动词用法，不是想艾特别人，别误伤。
    _AT_VERB_HEADS = "不了他她你我它的"

    def _clean(self, text: str) -> str:
        text = re.sub(r"```.*?```", "", text or "", flags=re.S)      # 去掉代码块
        text = text.replace("**", "").replace("##", "").strip()
        text = re.sub(r"^\s*(回复|答复)\s*[:：]\s*", "", text)
        # **正文里不许出现 `@` 形式的假艾特**。
        # 真机 21:29 发生过：用户让它艾特某人，它回「好，这就艾特 @阿乙」——
        # 那个 @ 在微信里只是纯文本，对方收不到提醒，群里却以为它艾特了。
        # 这属于本项目最忌讳的"假装成功"，还能被人利用去制造"AI 点名某人"的截图。
        # 处理方式是**去掉 @ 号、保留名字**（不把话弄断），并把这件事写进审计；
        # 真实的 @ 只由发送侧用 `at=` 参数做，而且只 @ 当条发言人。
        removed = 0

        def _strip_at(match: "re.Match[str]") -> str:
            nonlocal removed
            name = match.group(1)
            if len(name) <= 2 and name[0] in self._AT_VERB_HEADS:
                return match.group(0)            # 「我@不了」：动词用法，原样保留
            removed += 1
            return name

        text = self._AT_TEXT_RE.sub(_strip_at, text)
        if removed:
            text = re.sub(r"[ \t]{2,}", " ", text).strip()
            self._log(event="mention_stripped", count=removed,
                      reason="正文里的 @ 不是真 @（微信只当纯文本），已去掉 @ 号保留名字")
        limit = self.cfg.policy.max_reply_chars
        if len(text) > limit:
            text = text[: max(0, limit - 1)].rstrip() + "…"
        return text.strip()

    def _write_draft(self, msg: Message, reply: str, reason: str,
                     segments: "list[str] | None" = None) -> None:
        """写草稿。草稿里**同时给出全文与切分结果**——多段切得对不对，看草稿最直观。"""
        self.draft_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "message": message_to_dict(msg),
            "reply": reply,
        }
        if segments and len(segments) > 1:
            record["segments"] = segments
        path = self.draft_dir / f"{datetime.now():%Y%m%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---------------- 自检 ----------------

    def probe_llm(self) -> int:
        try:
            out = self.llm.complete(
                [
                    {"role": "system", "content": "你是连通性测试助手。"},
                    {"role": "user", "content": "只回复两个字：通了"},
                ]
            )
        except Exception as exc:
            print(f"模型探测失败：{exc}")
            return 2
        print(f"模型探测成功，返回：{out!r}")
        return 0

    def run_once(self) -> None:
        """把 demo 数据一次跑完（用于验证决策链，不等轮询）。"""
        self.hook.connect()
        while True:
            msg = self.hook.recv()
            if msg is None:
                break
            self.route(msg)
        self._stop.set()
        self._shutdown()


def _default_config():
    """演示模式用的内置策略：放行 Alice、其它一律忽略，draft 之外的行为与模板一致。"""
    from wechat_bridge.config import BridgeConfig

    cfg = BridgeConfig()
    cfg.hook.adapter = "wechat_bridge.adapters.hook_mock:MockHookAdapter"
    cfg.llm.adapter = "__fake__.FakeLLM"        # 占位：Bridge 在 demo 模式下会直接换成 FakeLLM
    cfg.policy.mode = "auto"
    # 演示/回归里**关掉发送防抖**：demo 的假 hook 是一次性把消息全吐出来的，
    # 防抖会把这些消息提前吞进待处理队列，导致"发了几条"的断言对不上。
    # 真实运行（读配置文件）不受影响，默认 5.5 秒（用户实测：4 秒对"手打第二句"还不够）。
    cfg.policy.reply_debounce_sec = 0.0
    cfg.policy.allow_groups = True
    cfg.policy.contact_rules = {
        "wxid_alice": "allow",
        "wxid_bob": "allow",
        "文件传输助手": "ignore",
        "微信团队": "ignore",
    }
    cfg.policy.deny_patterns = (
        "验证码", "银行卡", "卡号", "借钱", "身份证", "密码", "支付密码", "扫码付",
        "合同", "签字", "转账", "汇款", "打款", "红包", "收款码", "二维码",
    )
    cfg.policy.deny_regex = (
        r"转\s*\d", r"打款", r"汇款", r"借我\s*\d",
        r"转\s*[零一二三四五六七八九十百千万两]+\s*(块|元|钱)",
        r"(借我|借给|汇|打款)[^，。！？\s]{0,3}[零一二三四五六七八九十百千万两\d]",
        r"(支付|登录|短信)?\s*验证码",
        r"把.{0,6}卡号",
        r"银\s*行\s*卡",
    )
    cfg.policy.reply_delay_sec = (0.05, 0.15)
    # **演示绝不设静默时段**：它是"验证决策链能不能跑通"，不是"模拟你的作息"。
    # 2026-10-19 踩到：这里原本写 ("23:30", "07:30")，而回归脚本会设 `TZ=Asia/Shanghai`，
    # 在 Windows 上那个环境变量并不等于 UTC+8（Python 会按 UTC 算），于是"现在几点"
    # 整体偏移 8 小时——下午跑回归时 demo 正好落进静默时段，两条用例全红（0 条 sent）。
    # 教训：**任何按钟点开关的默认值都不该出现在离线自测里**，否则红不红取决于你几点跑。
    cfg.policy.quiet_hours = ()
    # ⚠ 演示/回归的产物必须写到**独立路径**，不能污染真实留档与限速状态：
    #   - 限速是跨窗口累计的（分钟/小时/人），测试攒下的计数会在真实运行时把正常回复拦掉；
    #   - 审计里混进 `wxid_alice` 这类假记录，事后排查分不清哪些是真发生过的。
    # 这和"测试别覆盖生产配置"是同一类问题，别改回去。
    cfg.storage.audit_path = "state/_demo/audit.jsonl"
    cfg.storage.state_path = "state/_demo/state.json"
    cfg.storage.draft_dir = "state/_demo/drafts"
    # 长记忆同理：演示写到独立目录，别把演示里编的"事实"混进真实记忆，
    # 也关掉自动摘要（demo 的假模型返回固定句子，没有可学的东西）。
    cfg.memory.dir = "state/_demo/memory"
    cfg.memory.extract_every = 0
    cfg.memory.enabled = False        # 演示/回归不写任何记忆，也不在退出时调模型
    return cfg


def validate_config(cfg) -> list[str]:
    """上线前把配置里会真出问题的地方挑出来。返回问题列表（空 = 可以上）。"""
    problems: list[str] = []
    p = cfg.policy

    # 1) 敏感规则必须覆盖“钱”这一类
    money_words = ("转", "借", "打款", "汇款", "红包", "支付", "验证码", "身份证", "密码")
    haystack = " ".join(list(p.deny_patterns) + list(p.deny_regex))
    missing = [w for w in money_words if w not in haystack]
    if len(missing) > 3:
        problems.append(f"敏感规则覆盖过窄，缺少：{', '.join(missing[:6])}（涉及钱与凭证的一律该拦）")
    for pattern in p.deny_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"deny_regex 非法正则「{pattern}」：{exc}（运行时会跳过它，等于漏拦）")

    # 2) 静默时段
    for value in p.quiet_hours:
        if not re.fullmatch(r"\d{2}:\d{2}", str(value)):
            problems.append(f"quiet_hours 格式应为 HH:MM，收到「{value}」")
    if p.quiet_hours[0] == "00:00" and p.quiet_hours[1] == "00:00":
        problems.append("quiet_hours 为 00:00-00:00，等于全天静默，什么都不会回")

    # 3) 限速必须比“人打字”还克制
    if p.max_replies_per_minute > 10:
        problems.append(f"max_replies_per_minute={p.max_replies_per_minute} 偏高，封号风险随频率上升（建议 ≤6）")

    # 4) 白名单
    if p.allow_unknown_contacts:
        problems.append("allow_unknown_contacts=true：陌生人也会被代回，建议先关掉只回白名单")
    if not p.contact_rules:
        problems.append("contact_rules 为空：没有白名单，配合 allow_unknown_contacts=false 会一个人都不回")
    if not any(v == "allow" for v in p.contact_rules.values()):
        problems.append("contact_rules 里没有任何 allow 条目：这个机器人不会回复任何人")
    if p.allow_groups and not p.reply_only_when_mentioned_in_group:
        problems.append("群里对每条消息都回（reply_only_when_mentioned_in_group=false），极易被当成营销号")

    # 5) 模型接入（空密钥时也要先试自动发现——留空是"让它在环境变量/.env 里找"）
    opts = cfg.llm.options or {}
    key = str(opts.get("api_key", "")).strip()
    if key.startswith("env:"):
        # env:NAME 钉死这个名字：环境变量优先，其次 ~/.dsh/.credentials.yaml 里的同名条目
        try:
            from resolve_key import resolve_named

            found, source = resolve_named(key[4:])
        except Exception:
            found, source = os.environ.get(key[4:], ""), f"环境变量 {key[4:]}"
        if not found:
            problems.append(f"模型密钥钉在 {source} 上，但取不到值（设环境变量，"
                            f"或在项目内放 .env，或确认 ~/.dsh/.credentials.yaml 里有这一条）")
        else:
            print(f"  （密钥将从 {source} 读取）")
    elif not key:
        try:
            from resolve_key import find_key

            found, source = find_key()
        except Exception:
            found, source = "", "自动发现失败"
        if not found:
            problems.append("模型密钥未提供：设 DEEPSEEK_API_KEY 环境变量，或在项目内放一个 .env "
                            "（内容 DEEPSEEK_API_KEY=sk-…），或把 llm.options.api_key 直接填上")
        else:
            print(f"  （密钥将从 {source} 读取）")
    if not opts.get("base_url"):
        problems.append("llm.options.base_url 为空")
    if not opts.get("model"):
        problems.append("llm.options.model 为空")

    # 6) 人格提示词
    if len((cfg.persona.system_prompt or "").strip()) < 40:
        problems.append("persona.system_prompt 过短：没有约束的模型会乱承诺（时间/钱/合同）")
    elif "AI" not in cfg.persona.system_prompt and "机器人" not in cfg.persona.system_prompt:
        problems.append("persona.system_prompt 没提“被问是不是 AI / 机器人怎么答”，建议补一条")

    # 7) 存储路径可写
    for label, value in (("audit_path", cfg.storage.audit_path), ("state_path", cfg.storage.state_path), ("draft_dir", cfg.storage.draft_dir)):
        path = Path(value)
        target = path if path.is_absolute() else PROJECT_ROOT / path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            problems.append(f"{label} 不可写（{target}）：{exc}")

    # 8) 识图开着，但适配器把图片丢了（2026-10-18 真机踩到：vision 配好了却一直"看不到图"，
    #    根因是 `hook.options.accept_images` 默认 False——图片在适配器层就被丢了，根本到不了桥接）
    if getattr(cfg.vision, "enabled", False):
        hook_opts = dict(getattr(cfg.hook, "options", {}) or {})
        if not hook_opts.get("accept_images", False):
            problems.append("vision.enabled=true 但 hook.options.accept_images 没开："
                            "图片会被适配器直接丢弃，识图永远不触发（加 \"accept_images\": true）")
        # 9) 图片收得到了，但**解不开**：微信 4.x 的图是 wxgf(HEVC)，没有 ffmpeg 就永远是"看不到"。
        #    这一条不拦启动（也许用户用的是会直接给 jpg 的 hook），只把话说清楚。
        try:
            from wechat_bridge.ffmpeg import status as ffmpeg_status

            st = ffmpeg_status(str(hook_opts.get("ffmpeg_exe", "") or ""))
        except Exception as exc:
            st = f"ffmpeg 自检失败：{exc!r}"
        if st.startswith("⚠"):
            print(f"  {st}")
            problems.append("vision.enabled=true 但没有可用的 ffmpeg：微信图片是 wxgf(HEVC)，"
                            "转不成 jpg 就永远看不到图（装 ffmpeg，或 python pkg_fetch.py imageio-ffmpeg）")
        else:
            print(f"  （{st}）")
    return problems


def _find_account_dir() -> Optional[Path]:
    """找微信 4.x 的数据目录（xwechat_files）。

    实现放在 `wechat_bridge/procinfo.py`：这个判断被 bridge / agent_ctl / 未来的 CLI 共用，
    各写一份的下场是"同一个问题在不同命令里给出不同答案"（本项目已经踩过一次）。
    """
    from wechat_bridge.procinfo import find_account_dir

    return find_account_dir()


def _weixin_running() -> bool:
    """Weixin.exe 是否可见。

    注意：在 DSH 沙箱里这个判断**必然为假**——tasklist / CIM / psutil 都被拒或看不到主进程。
    真实运行请在沙箱外的终端里跑（见 README「必须脱离沙箱」一节）。
    """
    from wechat_bridge.procinfo import weixin_running

    return weixin_running()


def trace_startup(cfg) -> int:
    """分步计时启动过程，用来定位"启动后没有任何输出"到底卡在哪一步。

    比对着日志猜栈有效得多：每一步都打印耗时，卡住的那一步会**没有**下一行输出。
    """
    t0 = time.time()

    def stage(name: str) -> None:
        print(f"[{time.time() - t0:6.1f}s] {name}", flush=True)

    stage("开始")
    if importlib.util.find_spec("wechatauto") is None:
        stage("[警告] 没找到 wechatauto —— 依赖未装配？")
    else:
        stage("wechatauto 可导入")

    stage("构造 Bridge（加载适配器对象）")
    bridge = Bridge(cfg, allow_real_llm=False)
    stage("Bridge 构造完成")

    stage("抢单实例锁")
    bridge._acquire_singleton()                                   # noqa: SLF001
    stage("锁已拿到")

    stage("连接 hook（这一步会找微信主窗口 + 解密数据库，最慢的就是它）")
    bridge.hook.connect()
    stage(f"hook 连接完成：{bridge.hook.status().detail}")

    stage("前 3 次 recv 轮询（各等 1 秒）")
    for i in range(3):
        msg = bridge.hook.recv()
        stage(f"  recv #{i + 1} -> {msg.text[:40] if msg else None}")
        time.sleep(1)

    stage("收尾：关闭 hook + 释放锁")
    try:
        bridge.hook.close()
    finally:
        bridge.lock.release()
    stage("完成（如果这行之后进程仍不退出，说明有非守护线程还在跑）")
    return 0


def run_doctor(cfg_path: Path | str | None = None, connect: bool = True) -> int:
    """上线前一次性自检：能不能启动、能不能收消息、缺什么、下一步该敲哪条命令。

    与 --validate 的分工：--validate 只审策略配置；doctor 连环境、适配器、真实客户端一起看。
    返回「阻塞项」数量（0 = 可以起）。
    """
    import importlib.util

    print("=" * 64)
    print("上线自检（doctor）")
    print("=" * 64)
    blockers = 0
    warns = 0

    # 1) Python 与项目文件
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"[1] Python {ver} @ {sys.executable}")
    for name in ("bridge.py", "bridge_config.template.json", "demo_chat.jsonl"):
        if not (CFG_ROOT / name).exists():
            print(f"    [缺] 项目文件 {name}")
            blockers += 1

    # 2) 配置
    path = Path(cfg_path) if cfg_path else (CFG_ROOT / "bridge_config.json")
    print(f"[2] 配置 {path.name}")
    if not path.exists():
        print("    [阻塞] 不存在。先执行：copy bridge_config.template.json bridge_config.json")
        blockers += 1
        cfg = None
    else:
        try:
            cfg = load_config(path)
            print(f"    adapter={cfg.hook.adapter}")
            print(f"    llm={cfg.llm.adapter} model={cfg.llm.options.get('model')}")
            print(f"    mode={cfg.policy.mode} 白名单={len(cfg.policy.contact_rules)} 条")
        except Exception as exc:
            print(f"    [阻塞] 解析失败：{exc!r}")
            cfg = None
            blockers += 1

    # 3) 策略体检（复用 --validate 的规则）
    if cfg is not None:
        problems = validate_config(cfg)
        print(f"[3] 策略体检：{len(problems)} 项待处理")
        for item in problems:
            print(f"    - {item}")
        blockers += len(problems)

        # 4) 适配器能否加载（不连接）
        spec = cfg.hook.adapter
        module_name, _, class_name = spec.partition(":")
        try:
            mod = importlib.import_module(module_name)
            getattr(mod, class_name)
            print(f"[4] 适配器可加载：{spec}")
        except Exception as exc:
            print(f"    [阻塞] {spec} 加载失败：{type(exc).__name__}: {exc}")
            print("    提示：wechatauto 路线的依赖要先解包，见 README「不依赖 pip 的依赖装配」")
            blockers += 1

    # 5) 微信客户端与账号状态（决定现在能不能真跑）
    # 数据目录不一定在 Documents 下——本机就是被改到 %USERPROFILE% 根目录了，
    # 所以这里多探几个位置，并直接用库自己的 auto_detect_db_dir 兜底。
    exe_4x = Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe")
    account_dir = _find_account_dir()
    print("[5] 微信客户端")
    if exe_4x.exists():
        print(f"    客户端：{exe_4x}")
    else:
        print("    [警告] 未在常规路径找到 4.x 客户端")
        warns += 1
    if account_dir:
        accounts = [d.name for d in account_dir.iterdir() if d.is_dir()] if account_dir.is_dir() else []
        print(f"    账号目录：{account_dir}（{len(accounts)} 个：{', '.join(accounts[:4])}）")
    else:
        print("    [阻塞] 找不到微信数据目录（xwechat_files）")
        print("    ⇒ 4.x 客户端还没登录过任何账号，UI 自动化与本地库读取都无从开始。")
        print("    ⇒ 下一步：打开微信 4.x 登录你要托管的小号，然后重跑本自检。")
        blockers += 1

    running = _weixin_running()
    print(f"    Weixin.exe 进程：{'可见' if running else '不可见（可能没开，或被沙箱挡了进程枚举）'}")

    # 5b) 沙箱可见性：进程看不见就取不到数据库密钥，真实运行必须脱离沙箱
    if account_dir and not running:
        print("    [阻塞] 取密钥需要读 Weixin.exe 进程内存，当前会话看不到该进程。")
        print("    ⇒ 这是 DSH 沙箱的限制（tasklist/CIM 均被拒），不是程序问题。")
        print("    ⇒ 请在**你自己的终端**里运行：run_bridge.cmd --doctor（用 bridge_probe_wechat.cmd 可直接验证）")
        blockers += 1

    # 6) 模型接口（只做静态检查，不真调；真调要花钱/要网络）
    if cfg is not None:
        key = str(cfg.llm.options.get("api_key", "")).strip()
        if key.startswith("env:"):
            # 与 --validate 同一口径：env:NAME 不只看进程环境变量，也认 .env 与 DSH 凭据文件
            try:
                from resolve_key import resolve_named

                found, source = resolve_named(key[4:])
            except Exception:
                found, source = os.environ.get(key[4:], ""), f"环境变量 {key[4:]}"
            print(f"[6] 模型密钥：钉在 {key[4:]} —— {'可用（' + source + '）' if found else '[阻塞] 取不到值'}")
            if not found:
                blockers += 1
        elif key:
            print("[6] 模型密钥：已直接写在配置里（不推荐，建议改 env: 形式）")
        else:
            try:
                from resolve_key import find_key

                found, source = find_key()
            except Exception as exc:
                found, source = "", f"解析器不可用（{exc!r}）"
            print(f"[6] 模型密钥：留空=自动发现 —— {'命中（' + source + '）' if found else '[阻塞] 一个都没找到'}")
            if not found:
                blockers += 1

    # 6b) 图片理解后端（默认关；关了不算问题，但要**明确告诉用户它看不到图**）
    if cfg is not None:
        vision_cfg = getattr(cfg, "vision", None)
        if vision_cfg is not None and getattr(vision_cfg, "enabled", False):
            try:
                found_vision = make_vision_client(vision_cfg)
            except Exception as exc:
                found_vision = None
                print(f"[6b] 图片理解：已启用但**后端加载失败** —— {exc!r}")
            if found_vision is not None:
                opts = vision_cfg.options or {}
                print(f"[6b] 图片理解：已启用（{opts.get('model', '?')} @ {opts.get('base_url', '?')}）")
            else:
                blockers += 1
        else:
            print("[6b] 图片理解：未启用 —— AI 只会看到「[图片]」占位，**不会真的看懂图片**")
            print("     ⇒ 想让它看图：填好 vision.options 并把 vision.enabled 改成 true（见 README）")

    # 7) 可选：真连一次 hook（会去碰微信，需账号在线且进程可见）
    if connect and cfg is not None and account_dir and running:
        print("[7] 真连一次 hook（只连不发）")
        try:
            bridge = Bridge(cfg, allow_real_llm=False)
            bridge.hook.connect()
            status = bridge.hook.status()
            print(f"    {'成功' if status.ok else '失败'}：{status.detail}")
            if status.extra:
                print(f"    附加信息：{status.extra}")
            bridge.hook.close()
        except Exception as exc:
            print(f"    [阻塞] 连接失败：{type(exc).__name__}: {exc}")
            blockers += 1
    else:
        print("[7] 跳过真连（账号未就绪或未要求连接）")

    print("-" * 64)
    print(f"结论：阻塞 {blockers} 项，警告 {warns} 项")
    if blockers:
        print("先解决阻塞项；都清了再按 README「上线顺序」跑 --dry-run。")
    else:
        print("没有阻塞项。建议顺序：run_bridge.cmd --validate → --dry-run → 正式启动（draft 模式观察一天）")
    return blockers


def _make_stdout_utf8() -> None:
    """让标准输出永远用 UTF-8，别让一个 emoji 把整个进程打死。

    为什么：Windows 控制台默认 GBK（cp936）。日志里有 ⚠ 之类的字符时，
    `print` 直接抛 `UnicodeEncodeError: 'gbk' codec can't encode character`，
    **异常发生在启动流程里 ⇒ 桥接还没开始跑就退出了**，而现象只是"没反应"。
    实测踩过：隐藏窗口启动（重定向到文件）时必然复现，因为只有这时才走 GBK。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _make_stdout_utf8()
    ap = argparse.ArgumentParser(description="微信账号代理桥接")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 bridge_config.json）")
    ap.add_argument("--demo", action="store_true", help="用假 hook + 假模型跑演示数据，不需要任何凭据")
    ap.add_argument("--use-http-hook", action="store_true", help="改走通用 HTTP 适配器收消息（配合 hook_fake_server.py 做端到端自测）")
    ap.add_argument("--hook-url", default="http://127.0.0.1:5555", help="HTTP 适配器地址，默认 127.0.0.1:5555")
    ap.add_argument("--probe-llm", action="store_true", help="只测试大模型接口连通性")
    ap.add_argument("--dry-run", action="store_true", help="只打印会发什么、写留档，绝不真的调用 hook.send")
    ap.add_argument("--resume", action="store_true", help="启动时清除上次的限速熔断（人工确认问题已解决后用）")
    ap.add_argument("--reset-limiter", action="store_true",
                    help="清空已落盘的限速窗口与熔断状态后退出（自测要干净环境 / 人工想重新计时时用）")
    ap.add_argument("--validate", action="store_true", help="只检查配置风险，不连接任何服务")
    ap.add_argument("--doctor", action="store_true", help="上线前综合自检：环境+配置+适配器+账号+（可选）真连")
    ap.add_argument("--trace-start", action="store_true",
                    help="分步计时启动过程（定位卡在哪一步，用于排查启动无输出）")
    ap.add_argument("--no-connect", action="store_true", help="doctor 时不做真连（只做静态检查）")
    ap.add_argument("--once", action="store_true", help="处理完当前可用消息后退出")
    ap.add_argument("--dump-memory", default=None, metavar="CHAT",
                    help="打印某个会话的长记忆（wxid 或 xxx@chatroom），排查'它怎么不记得'时用")
    ap.add_argument("--forget-memory", default=None, metavar="CHAT",
                    help="清空某个会话的长记忆后退出（配合 --forget-needle 只删关键词命中的条目）")
    ap.add_argument("--forget-needle", default="", metavar="KEYWORD",
                    help="与 --forget-memory 配合：只删包含该关键词的记忆；留空则清空该会话全部记忆")
    args = ap.parse_args(argv)

    if args.dump_memory or args.forget_memory:
        try:
            mcfg = load_config(args.config).memory
        except FileNotFoundError as exc:
            print(exc)
            return 1
        mem = LongMemory(mcfg, PROJECT_ROOT)
        chat = args.dump_memory or args.forget_memory
        if args.forget_memory:
            needle = args.forget_needle or ""
            removed = 0
            if needle:
                removed = mem.forget(chat, needle)
            else:
                items = mem.load(chat)
                removed = len(items)
                items.clear()
                mem.save(chat)
            print(f"已忘掉 {removed} 条（会话 {chat}"
                  + (f"，关键词「{needle}」" if needle else "，全部") + "）")
            return 0
        items = mem.load(chat)
        archived = mem.archived_count(chat)
        print(f"会话 {chat} 的长记忆：共 {len(items)} 条（目录 {mem._dir()}）")   # noqa: SLF001
        print(f"已归档的原始记忆：{archived} 条（{mem._archive_path(chat)}，不进提示词，可回溯）")  # noqa: SLF001
        for i, it in enumerate(items, 1):
            stamp = datetime.fromtimestamp(it.ts or 0).strftime("%m-%d %H:%M")
            print(f"  {i}. [{it.kind}] {it.text}   （{stamp} 记下，被想起 {it.hits} 次）")
        block = mem.render(chat, "", limit=len(items) or 1)
        if block:
            print("\n—— 现在会塞进提示词的就是这些（按相关度）——")
        return 0

    if args.doctor:
        return run_doctor(args.config, connect=not args.no_connect)

    # 演示模式**永远使用内置演示策略**，不读真实配置。
    # 为什么必须这样：演示的作用是"验证决策链路本身"，如果它读的是当前真实白名单/模式，
    # 那演示结果就会随你改配置而变（实测踩过一次：把白名单改成真实 wxid 后，demo 里
    # Alice 全被当陌生人拦掉，看起来像回归坏了）。演示要可复现，就不能受配置影响。
    if args.demo:
        print("演示模式：使用内置演示策略（不读 bridge_config.json）")
        cfg = _default_config()
        args.use_http_hook = args.use_http_hook      # 端到端模式仍可叠加
    else:
        try:
            cfg = load_config(args.config)
        except FileNotFoundError as exc:
            print(exc)
            print("提示：想先看效果可以直接跑 python bridge.py --demo")
            return 1

    if args.use_http_hook:                     # 端到端自测：把收消息换成 HTTP 拉取
        cfg.hook.adapter = "wechat_bridge.adapters.hook_http:HttpHookAdapter"
        cfg.hook.options = {"base_url": args.hook_url, "poll_sec": 0.3}

    if args.trace_start:
        return trace_startup(cfg)

    if args.reset_limiter:
        from wechat_bridge.state import StateStore as _SS

        store = _SS(PROJECT_ROOT / cfg.storage.state_path)
        store.save_limiter({})
        print(f"已清空限速窗口与熔断状态：{store.path}")
        return 0

    if args.validate:
        problems = validate_config(cfg)
        print(f"配置检查：{args.config or 'bridge_config.json'}")
        if not problems:
            print("  [通过] 没有发现会出问题的项，可以上线（建议先 --dry-run 或 draft 模式观察）")
            return 0
        for i, item in enumerate(problems, 1):
            print(f"  [{i}] {item}")
        print(f"\n共 {len(problems)} 项需要处理。")
        return 1

    if args.probe_llm:
        return Bridge(cfg).probe_llm()

    if args.demo:
        # 演示模式的回复是**固定练手文本**（比真聊长得多），自动多段会把它拆成三条，
        # 每条都真实占用限速额度 ⇒ 演示的第二条消息会被限速拦掉。演示要看的是链路，不是分段。
        # 注意：只在 CLI 的 --demo 里关，别放进 Bridge.__init__——
        # 自测脚本用 allow_real_llm=False 构造 Bridge，那样会把段切分的自测也一起关掉。
        try:
            cfg.segments.auto_split = False
        except Exception:
            pass

    bridge = Bridge(cfg, force_demo=args.demo and not args.use_http_hook,
                    allow_real_llm=not (args.demo or args.use_http_hook),
                    dry_run=args.dry_run, resume=args.resume)
    mode = "DEMO" if args.demo else cfg.policy.mode.upper()
    hook_name = type(bridge.hook).__name__
    llm_name = type(bridge.llm).__name__
    print(f"启动：hook={hook_name} llm={llm_name} 模式={mode} 项目目录={CFG_ROOT}")
    if args.dry_run:
        print("空跑模式：所有回复只打印与留档，不会真的发送。")
    elif not args.demo and cfg.policy.mode == "auto":
        print("⚠ auto 模式：满足条件的消息会被直接代发。想先观察就改 policy.mode 为 draft。")

    if args.once or args.demo:
        bridge.run_once()
    else:
        bridge.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
