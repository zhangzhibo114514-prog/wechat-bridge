"""wechatauto-replica 适配器（微信 4.x，UI 自动化 + 本地库读取）—— 已按真实 API 实现。

依赖形态（本机实测）：
    本机 pip 不可用（沙箱禁止写系统临时目录），依赖由 `pkg_fetch.py` 解包到 `.pylibs/`，
    `python smoke_wechatauto.py` 冒烟通过（wechatauto 1.2.6.1 + pywin32/comtypes/cryptography/
    Pillow/opencv/zstandard/cffi/numpy 等）。本适配器会自动把 `.pylibs` 挂进 sys.path，
    因此**不需要 pip 安装**也能跑。

工作原理：
    - 收消息：`wechatauto.WeChat` 内部用本地解密库增量轮询（db.Listener，后台线程回调）；
      本适配器把回调转成统一 Message 塞进队列，由 bridge 主循环 `recv()` 取出。
    - 发消息：`Chat.SendMsg`（UIA/坐标 + 剪贴板），**依赖前台焦点，不能并发**；bridge 侧
      已有全局限速与拟人延迟，这里不再加锁。

配置示例：
    "hook": {
      "adapter": "wechat_bridge.adapters.hook_wechatauto:WechatautoHookAdapter",
      "options": {"listen_chats": ["文件传输助手"], "gui": true}
    }
    先用只监听「文件传输助手」跑通，再放白名单里的真实联系人。
"""

from __future__ import annotations

import difflib
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..at_detect import addresses_other, describe, is_at_me
from ..bg_send import BackgroundSender
from ..ffmpeg import find as find_ffmpeg, status as ffmpeg_status, wxgf_to_jpg
from ..imagefmt import WXGF_MAGIC, describe_unknown, sniff
from .base import HookAdapter, HookStatus, Message

# 项目根 = `wechat-bridge/`。
# ⚠ **别再用 parents[N] 硬数层级**：这里曾经写成 `parents[3]`，注释却写着 "wechat-bridge/"，
# 实际指向的是**工作区**那一级。后果是 `_ensure_pylibs_on_path()` 静默失效（找不到 .pylibs
# 就直接 return），以及图片默认目录跑到工作区根上。从 `bridge.py` 启动时完全看不出来——
# 它自己先把 .pylibs 挂好了——但任何**直接 import 适配器**的新入口（自检脚本、探针、
# 别人 clone 下来写的第一个脚本）都会 `ModuleNotFoundError: No module named 'wechatauto'`。
# 现在改成"向上找含 .pylibs 的那一级"，找不到才退回按目录层级推断。
_HERE = Path(__file__).resolve()
PROJECT_ROOT = next((p for p in _HERE.parents if (p / ".pylibs").exists()), _HERE.parents[2])
PYLIBS = PROJECT_ROOT / ".pylibs"

# Windows 虚拟键码（自己实现 @ 时要用：清空输入框 / 粘贴正文）。
# 不直接 import guia 里的常量：`.pylibs` 是手工解包的第三方副本，换版本时这些名字没有契约。
VK_A = 0x41
VK_V = 0x56
VK_DELETE = 0x2E

# 库给"表情包/动画表情"的类型名 —— **注意有两套**：
#   · 消息行 dict 里的 `type` 是中文名（`WeChatDB._msg_type_name` 的产物）：`动画表情`
#   · 库构造出的 Message 对象的 `.type` 是**类属性英文**（`EmojiMessage.type = 'emotion'`）
# 真机踩过：只按中文名匹配 ⇒ 监听回调那条路（拿到的是 Message 对象）永远匹配不上，
# 表情包静默消失（心跳 received 不涨、审计一行没有、stderr 也看不见）。
STICKER_TYPES = ("emotion", "emoji", "sticker", "表情", "动画表情")

#: 「引用」消息（微信 appmsg type=57）的硬特征。
#: **不要靠类型名判**：同一个东西在库里有两套名字（行 dict 是中文、构造对象是英文），
#: 之前「动画表情」就是只认中文名而整类被丢掉，而且**无声无息**（received 都不涨）。
#: `<refermsg>` 只出现在引用里，按它判最稳。
_REFER_RE = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_XML_ENTITIES = {"&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&apos;": "'"}


def _unescape_xml(value: str) -> str:
    for entity, char in _XML_ENTITIES.items():
        value = value.replace(entity, char)
    return value


def _tag_text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return _unescape_xml(m.group(1)).strip() if m else ""


def parse_quote(content: str) -> Optional[dict]:
    """解析「引用」消息，返回 {text, from_me_hint, quoted_name, quoted_text}；不是引用返回 None。

    `text` 是喂给模型的正文，形如：
        [引用 阿甲：地主杀不完，你也逃课逃不完] 你还内涵上我了是吧

    为什么要带上前因：引用本身就是"我在回那句话"，模型看不到被引用的内容就会答非所问。
    """
    if not content or "<refermsg>" not in content:
        return None
    refer = _REFER_RE.search(content)
    if refer is None:
        return None
    refer_block = refer.group(1)
    # 引用块之前的部分才是"这次新说的话"（它的 <title> 是第一条）
    head = content[: refer.start()]
    new_text = _unescape_xml(_TITLE_RE.search(head).group(1)).strip() if _TITLE_RE.search(head) else ""
    quoted_name = _tag_text(refer_block, "displayname")
    quoted_raw = _tag_text(refer_block, "content")
    # 被引用的内容本身也可能是 XML（图片/链接/表情），只取它的 title 更可读
    quoted_text = _tag_text(quoted_raw, "title") or quoted_raw
    quoted_text = quoted_text.replace("\n", " ").strip()
    if len(quoted_text) > 80:
        quoted_text = quoted_text[:80] + "…"
    quoted_from = _tag_text(refer_block, "chatusr") or _tag_text(refer_block, "fromusr")
    prefix = f"[引用 {quoted_name or '某人'}：{quoted_text}]" if quoted_text else f"[引用 {quoted_name or '某人'}的消息]"
    return {
        "text": f"{prefix} {new_text}".strip(),
        "quoted_from": quoted_from,
        "quoted_name": quoted_name,
        "quoted_text": quoted_text,
    }



def _ensure_pylibs_on_path() -> None:
    """把手工解包的依赖目录挂进 sys.path（pywin32 的扩展模块在 pythonwin/ 里）。"""
    if not PYLIBS.exists():
        return
    for path in (PYLIBS, PYLIBS / "pythonwin", PYLIBS / "win32", PYLIBS / "win32" / "lib", PYLIBS / "win32comext"):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    for dll_dir in (PYLIBS / "pywin32_system32", PYLIBS / "win32"):
        if dll_dir.exists():
            try:
                import os
                os.add_dll_directory(str(dll_dir))       # 加载 pywin32 的 pyd 需要
            except Exception:
                pass


class WechatautoHookAdapter(HookAdapter):
    def __init__(
        self,
        listen_uids: Optional[list[str]] = None,
        listen_chats: Optional[list[str]] = None,
        listen_all: bool = False,
        gui: bool = True,
        send_timeout_sec: float = 20.0,
        stale_after_sec: float = 900.0,
        verify_send: bool = True,
        verify_window_sec: float = 20.0,
        verify_timeout_sec: float = 8.0,
        send_mode: str = "auto",
        bg_idle_gate_sec: float = 0.8,
        bg_idle_wait_sec: float = 6.0,
        bg_minimize_after: bool = False,
        ignore_at_all: bool = True,
        accept_images: bool = False,
        accept_stickers: bool = True,
        image_dir: str = "",
        ffmpeg_exe: str = "",
        **_: Any,
    ) -> None:
        # listen_uids 优先：直接给 wxid（"filehelper" / 对方 wxid / xxx@chatroom），确定可靠。
        # listen_chats 是显示名，交给库去解析，解析不到时不可靠（已实测 AddListenChat 会误判失败）。
        self.listen_uids = list(listen_uids or [])
        self.listen_chats = list(listen_chats or [])
        self.listen_uids_registered: list[str] = []
        self.listen_all = listen_all
        self.use_gui = gui
        self.send_timeout_sec = send_timeout_sec
        self.stale_after_sec = stale_after_sec
        self.verify_send = verify_send
        self.verify_window_sec = verify_window_sec
        self.verify_timeout_sec = verify_timeout_sec
        # 发送走哪条路：
        #   auto（默认）= 目标会话**当前就开着**时用后台发送（不动鼠标/不抢焦点），
        #                 否则退回 GUI（会打开会话、会抢一次焦点）；
        #   background  = 只允许后台发送，做不成就报失败（不打扰优先）；
        #   gui         = 老行为，全程 GUI（排查用）。
        mode = str(send_mode or "auto").strip().lower()
        self.send_mode = mode if mode in ("auto", "background", "gui") else "auto"
        self._bg: "BackgroundSender | None" = None
        # 微信「独立会话窗口」：会话名 → hwnd（5 秒缓存，窗口是用户手动开关的）
        self._bg_windows: dict[str, int] = {}
        self._bg_windows_at = 0.0
        self._bg_window_senders: dict[int, Any] = {}
        # 用户正在打字时先别发：微信发送会把窗口抬起来，那一下会打断他。
        # 等到他停手 `bg_idle_gate_sec` 秒再发，最多等 `bg_idle_wait_sec` 秒。
        # 设 0 关闭这道闸门（立刻发，可能在用户打字中途）。
        self.bg_idle_gate_sec = float(bg_idle_gate_sec or 0.0)
        self.bg_idle_wait_sec = float(bg_idle_wait_sec or 0.0)
        # 发完把窗口最小化：焦点抢不回来时靠它把焦点还给用户（代价是窗口闪一下）
        self.bg_minimize_after = bool(bg_minimize_after)
        # 正在发送：清残留的后台线程看到这个标志就跳过，免得把新写的正文清掉
        self._sending = False
        # 发送失败后"没清干净"的正文（见 _clear_input_if_matches 那段注释）。
        # 库会在几分钟后自己补回车把输入框里的残留冲出去——必须抢在它前面清掉。
        self._leftovers: list[str] = []
        self.ignore_at_all = ignore_at_all
        self.accept_images = accept_images
        # 表情包（动画表情）默认放行：群里别人甩表情是常态，整类丢掉会让它像没看见。
        # 注意这里**不取图**——真表情包是微信私有格式，只给它一句「对方发了个表情包」。
        self.accept_stickers = accept_stickers
        self.image_dir = image_dir or str(PROJECT_ROOT / "state" / "images")
        # 微信 4.x 的普通图片在本机是 **wxgf 容器 + HEVC 裸流**，云视觉接口一律拒收；
        # 必须先转成 jpg。转码只有 ffmpeg 能做。这个选项用于显式指定（其余情况自动找）。
        self.ffmpeg_exe = str(ffmpeg_exe or "").strip()
        # 上一次取图失败的人话原因（bridge 会把它写进日志，别让失败无声无息）
        self.image_error = ""
        self._ffmpeg_warned = False
        self._media = None
        self._wx = None
        self._queue: "queue.Queue[Message]" = queue.Queue()
        self._lock = threading.Lock()
        self._self_wxid = ""
        self._self_nick = ""
        # 配置里补充的"我在群里可能被叫到的名字"（群昵称/别名），见 set_extra_nicknames
        self.extra_nicknames: list[str] = []
        self._chat_cache: dict[str, Any] = {}
        self._stats = {
            "received": 0, "sent": 0, "duplicate": 0,
            "error": 0, "refused": 0, "send_failed": 0, "timeout": 0,
            "send_unverified": 0, "sent_bg": 0,
        }
        self._seen: set[str] = set()
        # 健康判定用的两个水位：
        #   `_last_seen_ts` = 我们**收到过的**最新一条消息时间
        #   `_seen_chats`   = 见过的会话（探测"库里有没有更新的消息"时只查这几个）
        self._last_seen_ts = 0.0
        self._seen_chats: dict[str, float] = {}          # chat_id -> 最近一条消息时间（保序用）
        # 探测结果缓存：别每 30 秒就把库翻一遍
        self._probe_at = 0.0
        self._probe_interval = 300.0
        self._probe_dead = False
        self._probe_reason = ""
        self._last_recv_ts = time.time()
        self._listen_failed = 0
        # 发送能力自检结果：connect() 时填。None = 还没测过（不要当成"能发"）。
        self._can_send: "bool | None" = None
        self._send_why = ""
        # OCR 兜底是否可用（@ 成员那条路只能靠它）。None = 还没测过。
        self._ocr_ok: "bool | None" = None
        # @ 成员时等弹层的上限。默认 3 秒：实测界面刚热激活/重校准布局后，
        # 弹层出现要 1–2 秒，库里原来死睡 0.9 秒不够（KI-1 的原因 1）。
        self.at_popup_timeout = 3.0
        # 点完成员后等弹层收起的上限（用来判定到底选没选中）。
        self.at_popup_gone_timeout = 1.5

    # ---------------- 生命周期 ----------------

    def _connect_with_timeout(self, gui: bool, timeout: float = 60.0):
        """带超时地构造 WeChat 客户端。

        库的构造函数会去找主窗口 + 解密数据库（可能要读微信进程内存），
        其中任一环节卡住都会让调用方无限等待——桥接会"启动成功"但永远收不到消息。
        """
        from wechatauto import WeChat

        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                box["wx"] = WeChat(gui=None if gui else False)
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True, name="wx-connect")
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise TimeoutError(
                f"连接微信超过 {timeout:.0f} 秒未完成。常见原因：微信主窗口被最小化到托盘、"
                "被锁屏挡住、或未登录。请把微信窗口打开并保持可见后重试。"
            )
        if "error" in box:
            raise box["error"]
        return box.get("wx")

    def connect(self) -> None:
        _ensure_pylibs_on_path()
        try:
            from wechatauto import WeChat
        except ImportError as exc:
            raise ImportError(
                "wechatauto 不可用。先补齐依赖：\n"
                "  python pkg_fetch.py wechatauto-replica uiautomation pywin32 comtypes cryptography "
                "Pillow psutil colorama zstandard opencv-python cffi numpy pyperclip "
                "-i https://mirrors.aliyun.com/pypi/simple\n"
                "  然后 python smoke_wechatauto.py 验证"
            ) from exc

        # gui=True 时构造函数会初始化 WeChatGUI（要微信在主窗口状态）；dry-run 观察阶段可设 false
        # 构造函数里会去找主窗口、解密数据库，任一步卡住都会让整个桥接**静默挂起**，
        # 所以这里也套一层超时：宁可报错退出，也不要看起来"启动了"其实永远连不上。
        self._wx = self._connect_with_timeout(gui=self.use_gui)

        try:
            info = self._wx._db.get_self_info()           # noqa: SLF001 —— 库未暴露公开接口，沿用其内部对象
            self._self_wxid = str(info.get("username") or "")
            self._self_nick = str(info.get("nick_name") or "")
        except Exception as exc:
            print(f"[wechatauto] 读取自身账号信息失败（不影响监听）: {exc!r}")

        if self._self_wxid:
            print(f"[wechatauto] 自身身份：wxid={self._self_wxid} 昵称={self._self_nick or '(无)'}"
                  f"（群聊 @ 检测会用它们）")

        # 启动就问一句「现在真发得出去吗」。见 _probe_send_capability() 的说明：
        # 发不出去时的报错是 `ImportError: DLL load failed while importing _winrt`，
        # **看着完全不像"发不出去"**，实测为此白跑了一轮真机验证。
        can_send, why = self._probe_send_capability()
        self._can_send, self._send_why = can_send, why
        if can_send:
            print(f"[wechatauto] 发送能力自检：{why}")
        else:
            print(f"[wechatauto] ⚠ 发送能力自检未通过：{why}")
            print("[wechatauto]   ⇒ 收消息、判定、生成回复都会正常，但**发不出去**。"
                  "auto 模式下请勿上线；draft 模式只观察不受影响。")

        def on_message(msg: Any, chat: Any) -> None:
            try:
                item = self._to_message(msg, chat)
                if item is None:
                    return
                with self._lock:
                    self._queue.put(item)
                    self._stats["received"] += 1
            except Exception as exc:                      # 回调里抛异常会打断监听线程
                self._stats["error"] += 1
                print(f"[wechatauto] 回调处理失败: {exc!r}")

        if self.listen_all:
            self._wx.AddListenAll(on_message, discover=True)
            print("[wechatauto] 已开启全局监听（discover=True，新会话会自动加入）")
        else:
            # **顺序很重要**：库的 `WeChat.StartListening()` 内部会走 `_listener_start()`，
            # 而它会 `self._listener = DBListener(...)` **重建** Listener 对象——
            # 如果先注册回调再 StartListening，回调表会被整个丢掉（实测 `_callbacks` 变回 {}，
            # 表现就是"连接成功、心跳正常、但永远收不到消息"）。
            # 所以必须先 StartListening，再往那个**最终的** listener 上注册。
            self._wx.StartListening()
            registered = self._register_by_wxid(on_message)
            if registered == 0 and self.listen_chats:
                # 只有显示名可用时，退回库的高层入口（它对 filehelper 之外的会话才可能成功）
                for name in self.listen_chats:
                    resp = self._wx.AddListenChat(name, on_message)
                    ok = bool(resp.get("is_success", False)) if isinstance(resp, dict) else bool(getattr(resp, "is_success", False))
                    if ok:
                        registered += 1
                        print(f"[wechatauto] 按显示名监听「{name}」成功")
                    else:
                        self._listen_failed += 1
                        detail = resp.get("message", "") if isinstance(resp, dict) else ""
                        print(f"[wechatauto] 按显示名监听「{name}」失败：{detail}")
            if registered == 0:
                # 一个都没注册上还往下走，就会变成"连接成功但永远收不到消息"
                raise RuntimeError(
                    "没有注册上任何监听会话。对策：在配置里用 listen_uids 写**会话 wxid**"
                    "（私聊=对方 wxid，群=xxx@chatroom，文件传输助手=filehelper）；"
                    "会话 id 可以用 `python probe_wechat.py` 输出里的会话来确认。"
                )
        time.sleep(1.0)                                   # 给监听线程一点启动时间
        self._last_recv_ts = time.time()
        callbacks = {k: len(v) for k, v in getattr(getattr(self._wx, "_listener", None), "_callbacks", {}).items()}
        print(f"[wechatauto] 已连接，自身 wxid={self._self_wxid or '未知'}，"
              f"监听线程={'活' if self._listener_alive() else '未起来'}，回调表={callbacks}")

    def _register_by_wxid(self, on_message) -> int:
        """直接用底层 Listener 按 wxid 注册回调。

        为什么不走 `WeChat.AddListenChat`：它的守卫条件是
        `if get_messages(wxid, limit=1) == [] and not chat._wxid` —— 两个条件用 and 连接，
        而解析出来的 wxid 永远非空，于是 `not chat._wxid` 恒为假，**该判断永远不成立**，
        注册失败时它也不会报错（库自带 demo 用的是底层 Listener，与此处一致）。
        所以这里按 wxid 直接注册：确定、可控、失败能立刻看见。
        """
        self_uid = self._self_wxid
        ids = [uid for uid in self.listen_uids if uid]
        if not ids:
            return 0

        def on_row(row: dict, _listener=None) -> None:
            try:
                msg = self._row_to_message(row)
                if msg is None:
                    return
                with self._lock:
                    self._queue.put(msg)
                    self._stats["received"] += 1
            except Exception as exc:                      # 回调里抛异常会打断监听线程
                self._stats["error"] += 1
                print(f"[wechatauto] 行回调处理失败: {exc!r}")

        listener = getattr(self._wx, "_listener", None)
        if listener is None:
            try:
                from wechatauto.db import Listener

                self._wx._listener = Listener(self._wx._db)
                listener = self._wx._listener
                print("[wechatauto] 自行创建了底层 Listener")
            except Exception as exc:
                print(f"[wechatauto] 创建 Listener 失败: {exc!r}")
                return 0

        count = 0
        for uid in ids:
            try:
                listener.add_listener(uid, on_row)
                self.listen_uids_registered.append(uid)
                count += 1
                print(f"[wechatauto] 已按 wxid 监听：{uid}")
            except Exception as exc:
                print(f"[wechatauto] 监听 {uid} 失败: {exc!r}")
        if on_message is not None and self_uid:
            pass                                          # 保留签名兼容，实际无需 self_uid
        return count

    def _row_to_message(self, row: dict) -> Optional[Message]:
        """把 Listener 回调的原始行（dict）转成统一 Message。

        行字段（见库 db.Listener 文档）：username / sender_id / type / content /
        create_time / local_id / sort_seq / sender_username（部分分片才有）。

        ⚠️ **这条才是真机实际执行的那条路**（`listen_uids` 注册的是库的 `Listener`，
        回调给的是原始行 dict，不是 Chat/Message 对象）。所以 `_to_message()` 里的
        图片放行与 @ 判定，在这里**必须同样实现一遍**。

        这不是风格问题，是踩过的坑：@ 判定曾经只加在 `_to_message()` 里，而真机走的是
        这里 ⇒ 群里 @ 机器人，日志里 `mentions_me=False`、`at_note=` 空，
        机器人一直装死，而单元测试（直接测 `is_at_me`）全绿。
        **改这两个函数中的任何一个，另一个必须同步改**；回归测试要**经过本函数**，
        只测判据函数等于没测到这条链路。
        """
        chat_id = str(row.get("username") or row.get("chat_id") or "")
        if not chat_id:
            return None
        mtype = str(row.get("type") or "")
        # **引用先判**：微信的"引用"是 appmsg(type=57)，在库里被归成「文件/链接/卡片」，
        # 按类型名判会整类丢掉——真机表现就是"引用它的时候它完全没反应"，
        # 而且连 `received` 都不涨（一条日志都没有）。所以按 `<refermsg>` 这个硬特征先认。
        _raw_for_quote = row.get("content") or ""
        if isinstance(_raw_for_quote, bytes):
            _raw_for_quote = _raw_for_quote.decode("utf-8", "replace")
        quote = parse_quote(str(_raw_for_quote))
        kind = "text"
        if quote is not None:
            kind = "text"
        elif mtype and mtype not in ("文本", "text", "Text"):
            if self.accept_images and mtype in ("图片", "image", "Image"):
                kind = "image"                            # 放行图片；不在这里取图，见 download_image
            elif self.accept_stickers and mtype in STICKER_TYPES:
                kind = "sticker"                          # 表情包：只记"有这回事"，不取图
            else:
                return None                               # 其余类型（语音/文件/位置…）仍交给人工
        sender_wxid = str(row.get("sender_username") or "").strip()
        if sender_wxid.isdigit():
            sender_wxid = ""                              # rowid 冒充用户名的情况，丢掉
        content = row.get("content") or ""
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        raw_content = str(content)                        # 剥前缀**之前**的原文，@ 判定要用
        if quote is not None:
            # 正文换成「[引用 谁：原话] 新内容」——不带上前因，模型一定答非所问
            raw_content = quote["text"]
            content = quote["text"]
        # 群消息正文形如 "wxid_xxx: 内容"，库文档明确说 4.x 群消息 sender_id 不可靠。
        # 两种情况都要处理：① sender_username 缺失 → 从前缀里补；② 有 → 前缀也要剥掉，
        # 否则提示词里会出现「昵称：wxid_xxx: 正文」这种双重标注（实测模型因此读不懂语境）。
        if chat_id.endswith("@chatroom") and ":" in content[:64]:
            head, _, _tail = content.partition(":")
            if head.strip().startswith("wxid_"):
                if not sender_wxid:
                    sender_wxid = head.strip()
        content = self._strip_sender_prefix(content, sender_wxid)
        if chat_id.endswith("@chatroom"):
            content = self._normalize_mentions(content)
        is_self = bool(self._self_wxid) and sender_wxid == self._self_wxid
        is_group = chat_id.endswith("@chatroom")

        # 群内 @ 判断。用 `raw_content`（含 `wxid_xxx:` 前缀也没关系，`is_at_me` 自己会剥），
        # 判据与 `_to_message()` 完全一致，含 @所有人 的处理。
        mentions_me = False
        at_others = False
        at_note = ""
        if is_group and not is_self:
            names = self._nicknames()                     # 查一次库，两处判据共用
            at = is_at_me(raw_content, self._self_wxid, names)
            if at.matched and at.at_all and self.ignore_at_all:
                at_note = "@所有人（按配置忽略）"
            else:
                mentions_me = at.matched
                if at.matched:
                    at_note = describe(at)
            # 在叫别人吗：追问链里要靠它闭嘴（`@张三 这事跟你有什么关系` 不该我接）
            at_others = not at.matched and addresses_other(raw_content, self._self_wxid, names)
            # **引用我的消息 = 在跟我说话**：用户已经明确指向，不该再要求他额外打个 @
            if quote is not None and self._self_wxid and quote.get("quoted_from") == self._self_wxid:
                mentions_me = True
                at_others = False
                at_note = (at_note + "；引用了我") if at_note else "引用了我"

        # 发送缓存：`send()` 只在缓存里找 Chat 对象，而这条路**天生拿不到** Chat
        # （回调给的是原始行 dict）。不在这里补上，真机上的表现就是"@ 检测对了、
        # 也决定了要回，然后发送环节一句『拒绝发送：会话不在缓存里』"。
        self._ensure_chat(chat_id)

        local_id = row.get("local_id")
        dedup_key = f"{chat_id}:{local_id if local_id is not None else row.get('sort_seq')}"
        self._note_seen(chat_id, float(row.get("create_time") or 0))

        if kind == "image":
            placeholder = "[图片]"
        elif kind == "sticker":
            placeholder = "[表情包]"
        else:
            placeholder = str(content)

        return Message(
            chat_id=chat_id,
            sender_id=sender_wxid,
            sender_name=self._nickname(sender_wxid) or sender_wxid or chat_id,
            text=placeholder,
            is_group=is_group,
            kind=kind,
            msg_id=dedup_key,
            local_id=local_id,
            ts=float(row.get("create_time") or time.time()),
            is_self=is_self,
            mentions_me=mentions_me,
            at_others=at_others,
            at_note=at_note,
            # 结构化留一份引用信息：判定层要靠它区分"引用了我"与"引用了别人"。
            quote=quote,
            raw=row,
        )

    def _nickname(self, wxid: str) -> str:
        if not wxid:
            return ""
        try:
            return str(self._wx._db.get_nickname(wxid) or "")
        except Exception:
            return ""

    def close(self) -> None:
        if self._wx is not None:
            try:
                self._wx.StopListening(remove=True)
            except Exception as exc:
                print(f"[wechatauto] 停止监听失败（可忽略）: {exc!r}")
            self._wx = None
        self._media = None                      # 绑定在旧 db 上，重连后要重建

    # ---------------- 收 ----------------

    @staticmethod
    def _strip_sender_prefix(content: str, sender_wxid: str = "") -> str:
        """去掉群消息正文里自带的 `wxid_xxx:` 前缀。

        微信 4.x 的群消息正文形如 `wxid_xxx:\\n正文`。这个前缀就是**发送者标识**，
        我们从别处已经拿到了（`sender_username` / 库解析出来的 sender_id），
        留在正文里只会污染提示词——实测模型看到的群聊记录是：

            阿甲：wxid_other_user:
            不像

        同一条消息既标了昵称又标了一遍 wxid，语境被切碎，
        模型分不清谁在跟谁说话（用户反馈"读不懂群里的语境"就是它）。
        只剥**看起来确实是发送者**的那种前缀，正文里正常出现的冒号不动。
        """
        text = str(content or "")
        head, sep, tail = text.partition(":")
        if not sep:
            return text.strip()
        head = head.strip()
        if not (head.startswith("wxid_") or (sender_wxid and head == sender_wxid)):
            return text.strip()
        return tail.strip()

    def _normalize_mentions(self, text: str) -> str:
        """把正文里的 `@我的某个昵称` 统一写成 `@我`。

        为什么：提示词里给模型看的是 `阿甲：@de 过来说两句话`，
        而模型并不知道 `de` 就是它自己——它会以为群里还有第三个人叫 de。
        改成 `@我` 之后，"这条是在叫我"就一目了然了。
        """
        out = str(text or "")
        try:
            names = sorted((n for n in self._nicknames() if n), key=len, reverse=True)
        except Exception:
            return out
        for name in names:
            out = out.replace(f"@{name}", "@我")
        return out

    def _to_message(self, msg: Any, chat: Any) -> Optional[Message]:
        chat_id = str(getattr(chat, "who", "") or "")
        if not chat_id:
            return None
        # AddListenChat 按显示名注册，回调给的是 wxid，两者对不上；所以白名单过滤只能在
        # 全局监听（AddListenAll）时按 wxid 做，逐会话注册时交给库自己筛。
        if self.listen_all and self.listen_chats and chat_id not in self.listen_chats:
            return None

        mtype = str(getattr(msg, "type", "") or "")
        # 引用先判（同 _row_to_message，两处必须一致：真机走的是行 dict 那条路）
        _raw_for_quote = str(getattr(msg, "content", "") or "")
        quote = parse_quote(_raw_for_quote)
        kind = "text"
        if quote is not None:
            kind = "text"
        elif mtype and mtype not in ("文本", "text", "Text"):
            if self.accept_images and mtype in ("图片", "image", "Image"):
                kind = "image"                            # 放行图片；**不在这里取图**，见 download_image
            elif self.accept_stickers and mtype in STICKER_TYPES:
                kind = "sticker"                          # 表情包：只记"有这回事"，不取图
            else:
                return None                               # 其余类型（语音/文件/位置…）仍交给人工

        self._chat_cache[chat_id] = chat
        if len(self._chat_cache) > 500:
            self._chat_cache.pop(next(iter(self._chat_cache)))

        local_id = getattr(msg, "local_id", None)
        sort_seq = getattr(msg, "sort_seq", None)
        dedup_key = f"{chat_id}:{local_id if local_id is not None else sort_seq}:{getattr(msg, 'create_time', '')}"
        self._note_seen(chat_id, float(getattr(msg, "create_time", 0) or 0))
        is_self = getattr(msg, "attr", "") == "self" or str(getattr(msg, "sender_wxid", "")) == self._self_wxid

        raw_content = str(getattr(msg, "content", "") or "")
        if quote is not None:
            raw_content = quote["text"]
        is_group = chat_id.endswith("@chatroom")

        # 群内 @ 判断：这里能拿到**原始正文**（含 atuserlist XML 与 `wxid_xxx:` 前缀），
        # 是唯一适合做这件事的地方——到了桥接层正文已经被当成纯文本了。
        # @所有人 默认不算"叫我"（别在全员通知下面插嘴），可用 ignore_at_all=False 改。
        mentions_me = False
        at_others = False
        at_note = ""
        if is_group and not is_self:
            names = self._nicknames()                     # 查一次库，两处判据共用
            at = is_at_me(raw_content, self._self_wxid, names)
            if at.matched and at.at_all and self.ignore_at_all:
                at_note = f"@{'所有人'}（按配置忽略）"
            else:
                mentions_me = at.matched
                if at.matched:
                    at_note = describe(at)
            # 在叫别人吗：追问链里要靠它闭嘴（`@张三 这事跟你有什么关系` 不该我接）
            at_others = not at.matched and addresses_other(raw_content, self._self_wxid, names)
            if quote is not None and self._self_wxid and quote.get("quoted_from") == self._self_wxid:
                mentions_me = True
                at_others = False
                at_note = (at_note + "；引用了我") if at_note else "引用了我"

        if kind == "image":
            placeholder = "[图片]"
        elif kind == "sticker":
            placeholder = "[表情包]"
        else:
            placeholder = (
                self._normalize_mentions(self._strip_sender_prefix(
                    raw_content, str(getattr(msg, "sender_wxid", "") or "")))
                if is_group else self._strip_sender_prefix(
                    raw_content, str(getattr(msg, "sender_wxid", "") or "")))

        return Message(
            chat_id=chat_id,
            sender_id=str(getattr(msg, "sender_wxid", "") or ""),
            sender_name=str(getattr(msg, "sender", "") or getattr(msg, "sender_wxid", "") or chat_id),
            text=placeholder,
            is_group=is_group,
            kind=kind,
            msg_id=dedup_key,
            local_id=local_id,
            ts=float(getattr(msg, "create_time", 0) or time.time()),
            is_self=is_self,
            mentions_me=mentions_me,
            at_others=at_others,
            at_note=at_note,
            quote=quote,
            raw=msg,
        )

    def set_extra_nicknames(self, names: "list[str] | None") -> None:
        """补一批"我在群里可能被叫到的名字"（来自 `group.my_nicknames`）。

        为什么需要：@ 检测认**昵称**（这台客户端不吐 `<atuserlist>`），
        而**群里 @ 人时微信插进去的是群昵称**，不是账号昵称。
        只认账号昵称的话，改了群昵称就再也叫不醒它。
        """
        self.extra_nicknames = [str(n).strip() for n in (names or []) if str(n).strip()]

    def _nicknames(self) -> list[str]:
        """我在群里可能被叫到的名字：配置补充的别名 + 账号昵称 + 库里查到的昵称/备注。"""
        names = list(self.extra_nicknames)
        if self._self_nick:
            names.append(self._self_nick)
        try:
            db = getattr(self._wx, "_db", None)
            if db is not None and self._self_wxid:
                for rel, path, _ in db._db_files:          # noqa: SLF001
                    if os.path.basename(path) != "contact.db":
                        continue
                    conn = db._open(rel)                   # noqa: SLF001
                    try:
                        row = conn.execute(
                            "SELECT nick_name, remark FROM contact WHERE username=?",
                            (self._self_wxid,)).fetchone()
                    finally:
                        conn.close()
                    if row:
                        names.extend([str(row[0] or ""), str(row[1] or "")])
                    break
        except Exception:
            pass
        return [n for n in dict.fromkeys(names) if n]

    def identity(self) -> tuple[str, list[str]]:
        return self._self_wxid, self._nicknames()

    def _find_ffmpeg(self) -> Optional[str]:
        """找 ffmpeg（实现在 `wechat_bridge/ffmpeg.py`，bridge 的配置体检也用它）。"""
        return find_ffmpeg(self.ffmpeg_exe)

    def ffmpeg_status(self) -> str:
        """人话报告 ffmpeg 情况（bridge 启动日志 / `check_env.py` 用）。"""
        return ffmpeg_status(self.ffmpeg_exe)

    def _model_readable(self, path: str) -> Optional[str]:
        """把"落盘路径"变成"模型真能读的图片路径"；做不到就返回 None 并写清原因。"""
        p = Path(path)
        try:
            head = p.read_bytes()[:32]
        except OSError as exc:
            self.image_error = f"读不到文件 {p.name}（{exc.__class__.__name__}）"
            return None
        if sniff(head) is not None:                       # 已经是 jpg/png/gif/webp
            return str(p)
        if head[:4] == WXGF_MAGIC:
            if not self._find_ffmpeg():
                self.image_error = (
                    "这张图是微信 wxgf(HEVC) 格式，需要 ffmpeg 转码，但本机没找到 ffmpeg"
                    "（配 hook.options.ffmpeg_exe，或 python pkg_fetch.py imageio-ffmpeg）"
                )
                if not self._ffmpeg_warned:
                    self._ffmpeg_warned = True
                    print(f"[取图] {self.image_error}", flush=True)
                return None
            jpg = wxgf_to_jpg(p, Path(self.image_dir) / "_convert", self.ffmpeg_exe)
            if jpg is None:
                self.image_error = f"wxgf 转码失败（{p.name}）：ffmpeg 转出来不是 jpg"
                return None
            return str(jpg)
        self.image_error = f"落盘文件不是图片（{describe_unknown(head)}）"
        return None

    def _diagnose_image(self, chat_id: str, local_id: Any) -> str:
        """取图失败时把**中间状态抄下来**（这条日志就是为了不再一轮一轮地猜）。

        `MediaDownloader.download_image()` 有六种返回 `None` 的路径、一句解释都没有，
        真机上统统表现为「图没加载出来」。这里把能查的都查一遍：
        消息行有几条、类型是什么、md5 取到没有、本机到底有没有 `.dat` 那一份。
        """
        if self._media is None or self._wx is None:
            return "适配器还没准备好（media/db 未初始化）；本机档位未知"
        try:
            rows = self._wx._db.get_message_rows_for_media(chat_id, int(local_id))   # noqa: SLF001
        except Exception as exc:
            return f"查消息行失败（{exc.__class__.__name__}: {exc}）；本机档位未知"
        if not rows:
            return (f"库里查不到 local_id={local_id} 这一行（分片/会话 id 对不上？）；"
                    f"本机档位未知")
        types = [row.get("local_type") for row in rows]
        md5 = ""
        files: dict = {}
        for row in rows:
            md5 = self._media._img_md5(row) or ""        # noqa: SLF001 —— 只是读内容里的 md5
            if md5:
                files = self._media._image_files(chat_id, md5)   # noqa: SLF001
                break
        tiers = {k: v[1] for k, v in files.items()} if files else {}
        return (f"行数={len(rows)} local_type={types} md5={(md5[:12] or '取不到')} "
                f"本机档位={tiers or '一档都没有（微信还没把这张图下下来）'}")

    def download_image(self, chat_id: str, raw_msg: Any, local_id: Any = None) -> Optional[str]:
        """把一条图片消息的原图取到本地，返回**模型能读的**文件路径；取不到返回 None。

        返回前会做一次格式适配：微信 4.x 的普通图片是 wxgf/HEVC，这里转成 jpg
        （`state/images/_convert/` 里跑转码，成功后在同目录留一份 `.jpg`）。

        **懒加载**：收到图片时只放一个「[图片]」占位，等真的决定要回复了才来取。
        群里休眠期会收到一堆图片，提前取等于白下载 + 白解密（图片是 AES+XOR 双加密容器，
        解密密钥要从 `Weixin.exe` 进程内存里扫）。

        解密由库自带的 `MediaDownloader` 完成——**这一层我们不重复造**。

        Args:
            local_id: 消息行号。调用方从 `raw_msg` 里取不到时**从桥接侧补进来**
                （`Message.msg_id` 里就带着它）。真机踩过：两种 raw 形状里有一路不一定
                带 `local_id`，取不到就静默返回 None，日志里一个字都没有。
        """
        self.image_error = ""
        if self._wx is None:
            self.image_error = "适配器还没连上微信（_wx 为空）"
            return None
        if raw_msg is None and local_id is None:
            self.image_error = "这条图片消息既没有 raw 也没有 local_id，无从取图"
            return None
        # 两种 raw 都要吃：监听回调路径给的是库的 Message 对象（属性），
        # 轮询路径给的是原始行 dict（键）。
        if local_id is None:
            if isinstance(raw_msg, dict):
                local_id = raw_msg.get("local_id")
            else:
                local_id = getattr(raw_msg, "local_id", None)
        if local_id is None:
            self.image_error = (f"这条图片消息没有 local_id（raw 类型 "
                                f"{type(raw_msg).__name__}），取不到图")
            return None
        try:
            if self._media is None:
                from wechatauto import MediaDownloader

                self._media = MediaDownloader(self._wx._db)   # noqa: SLF001 —— 库未暴露公开入口
            Path(self.image_dir).mkdir(parents=True, exist_ok=True)

            def fetch(tier: Optional[str] = None) -> Optional[str]:
                """取一次图。`tier='mid'` 只要压缩版（比缩略图清楚得多）。"""
                fn = getattr(self._media, "download_image", None)
                if fn is not None:
                    try:
                        return fn(chat_id, int(local_id), save_dir=self.image_dir, tier=tier)
                    except TypeError:                                  # 老版本库没有 tier 形参
                        return fn(chat_id, int(local_id), save_dir=self.image_dir)
                return self._media.download_media(                     # noqa: SLF001
                    chat_id, int(local_id), save_dir=self.image_dir)

            # 图片刚发出来时，微信先落缩略图（6 KB）、压缩版（40 KB+）要等一两秒。
            # **先等压缩版**：缩略图对梗图够用，但小字截图会糊成一片，识图质量全毁在这一步。
            # 最多等 4 秒；等不到就退回"本机有什么拿什么"（压缩版 → 缩略图），
            # 总重试 6 秒封顶——回复本来就有 5.5 秒防抖，用户感知不到这点延迟。
            attempts, delay, wait_mid_until = 6, 1.0, 4
            path = None
            for attempt in range(1, attempts + 1):
                path = fetch("mid")
                if path:
                    break
                if attempt >= wait_mid_until:                          # 等够了，缩略图也先收下
                    path = fetch(None)
                    if path:
                        break
                if attempt < attempts:
                    time.sleep(delay)
            if not path:
                self.image_error = (f"本机没有这张图的文件（试了 {attempts} 次）："
                                    + self._diagnose_image(chat_id, local_id))
                return None
            return self._model_readable(str(path))
        except Exception as exc:
            self.image_error = f"取图异常：{exc!r}"
            print(f"[wechatauto] 取图失败（local_id={local_id}）：{exc!r}", flush=True)
            return None


    def recv(self) -> Optional[Message]:
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return None
        if item.msg_id in self._seen:                     # 库里可能重复投递同一条
            self._stats["duplicate"] += 1
            return None
        self._seen.add(item.msg_id)
        if len(self._seen) > 5000:
            self._seen.clear()
        return item

    # ---------------- 发 ----------------

    def _probe_send_capability(self) -> tuple[bool, str]:
        """上线前问一句：**现在真发得出去吗**。返回 (能否发送, 人话原因)。

        为什么必须在启动时问，而不是等第一次发送时才知道：
        发送走 UIA 控件树；UIA 拿不到时库会降级到 **OCR 驱动**，而 OCR 依赖
        `winsdk._winrt`——某些环境里这个扩展的依赖 DLL 是坏的。两条路同时断掉时，
        症状是「消息收到了、@ 判定对了、回复也生成好了，然后一条
        `ImportError: DLL load failed while importing _winrt`」——
        **这条报错看着完全不像"发不出去"**，实测为此白跑了一整轮真机验证。

        最常见的原因不是代码：**在受限/沙箱环境里跑**。UIA 热激活要定位目标进程里的
        `Weixin.dll` 模块（读模块表），沙箱/完整性级别不一致会把它拦掉。
        对策是让桥接与微信跑在**同一个普通用户会话**里，不要套沙箱、也不要提权。

        代价：UIA 可用时约 2 秒；不可用时要约 30 秒（库要试完才放弃），只发生在启动时。
        """
        uia_why = ""
        # 先验 OCR（很便宜；而且 **@ 成员那条路只能靠它**——见 send() 里的拒绝分支）。
        try:
            from winsdk.windows.media.ocr import OcrEngine       # noqa: F401

            self._ocr_ok = True
        except Exception:
            self._ocr_ok = False

        try:
            from wechatauto.uia_driver import WeChatUIA

            if WeChatUIA().ensure_window():
                return True, ("UIA 控件树可用（发送走主路径）"
                              + ("；OCR 也可用" if self._ocr_ok
                                 else "；⚠ OCR 不可用 ⇒ 群里 @ 回对方（at_back）用不了"))
            uia_why = "UIA 控件树拿不到（多半是在受限/沙箱环境里跑，热激活读不到 Weixin.dll 模块表）"
        except Exception as exc:
            uia_why = f"UIA 引擎不可用（{type(exc).__name__}: {exc}）"

        if self._ocr_ok:
            return True, f"{uia_why}；只剩 OCR 兜底可用（能发出去，但定位靠识别，不如 UIA 稳）"
        return False, (f"{uia_why}；OCR 兜底也不可用（winsdk 装不上、或与当前 Python 版本不匹配）"
                       "——两条路都断了，发送必然失败")

    def ocr_available(self) -> bool:
        """OCR 兜底是否可用。

        **@ 成员（群里回复带 @）硬依赖它**：库的 `at_member` 要用 OCR 定位 @ 下拉弹层
        （UIA 看不见那个浮层）。桥接启动时会问一次，答"不可用"就自动关掉 at_back。
        """
        return bool(self._ocr_ok)

    def _ensure_chat(self, chat_id: str) -> bool:
        """把 `chat_id` 对应的 Chat 对象放进**发送缓存**（真机路径专用）。

        为什么需要它：`send()` 的硬规则是"缓存里没有这个会话就拒绝发送"，
        而缓存原本只由 `_to_message()`（轮询路径）填充——真机走的是监听回调路径
        `_row_to_message()`，那条路拿到的只是原始行 dict，**根本没有 Chat 对象**。
        于是 @ 判定修好之后，真机上依然一句都发不出去（全是"拒绝发送"）。
        这和 @ 判定漏判是**同一个根因的两半**：能力只加在了两条路径中的一条上。

        安全性：这里**不是**旧写法那种 `SendMsg(text, chat_id)`（它的第二个位置参数是
        `who`，传 wxid 打不中人，会退化成"发给当前打开的会话"）。这里是显式构造一个
        绑定该 wxid 的 `Chat`，而且在缓存前**校验它解析出来的 wxid 与请求的一致**：
        解析不出来、或解析成了别的会话，就拒绝缓存——宁可发不出去，也不发错人。
        """
        with self._lock:
            if chat_id in self._chat_cache:
                return True
        if self._wx is None:
            return False
        try:
            from wechatauto.wx import Chat

            chat = Chat(chat_id, getattr(self._wx, "_gui", None), getattr(self._wx, "_db", None))
        except Exception as exc:
            print(f"[wechatauto] 会话解析失败（{chat_id}）：{exc!r}")
            return False
        resolved = str(getattr(chat, "_wxid", "") or "")
        if not resolved:
            print(f"[wechatauto] 会话解析不出 wxid，拒绝缓存：{chat_id}")
            return False
        if resolved != chat_id:
            print(f"[wechatauto] 会话解析不一致，拒绝缓存：请求 {chat_id} → 解析出 {resolved}")
            return False
        with self._lock:
            self._chat_cache[chat_id] = chat
            if len(self._chat_cache) > 500:
                self._chat_cache.pop(next(iter(self._chat_cache)))
        print(f"[wechatauto] 已缓存会话（真机路径）：{chat_id}")
        return True

    def prime_chat(self, chat_id: str) -> bool:
        """把一个会话的 Chat 对象**手动**放进缓存（只给上线前自检用）。

        平时不需要它：正常回复流程里，入站消息的回调本身就带着 Chat 对象，
        `_to_message()` 会顺手缓存，紧接着的回信自然拿得到。
        但 `send_check.py` 这种"还没有入站消息、只想验证发得出去"的场景，缓存是空的，
        `send()` 会按设计拒绝——所以补一个显式预热入口。

        **别把它接到自动回复路径上当兜底**：缓存为空还硬发，正是下面那条
        "宁可不发也不发错人"要防的情况。
        （真机监听路径的自动缓存是 `_ensure_chat()`，它带 wxid 一致性校验，两者别混。）
        """
        if self._wx is None:
            return False
        try:
            from wechatauto.wx import Chat
        except Exception as exc:
            print(f"[wechatauto] 预热会话失败（导入 Chat）: {exc!r}")
            return False
        try:
            chat = Chat(chat_id, getattr(self._wx, "_gui", None), getattr(self._wx, "_db", None))
        except Exception as exc:
            print(f"[wechatauto] 预热会话失败: {exc!r}")
            return False
        with self._lock:
            self._chat_cache[chat_id] = chat
        print(f"[wechatauto] 已预热会话缓存：{chat_id}"
              f"（who={getattr(chat, 'who', '')!r}，解析出的 wxid={getattr(chat, '_wxid', '')!r}）")
        return True

    def send(self, chat_id: str, text: str, at: str = "") -> bool:
        """UI 自动化发送：依赖微信窗口在前台，且不能并发（剪贴板是独占资源）。

        **两条硬规则（都是审计后加的，别退回旧写法）**：
        1. 缓存里没有这个会话的 Chat 对象就**拒绝发送**。绝不能退化成
           `self._wx.SendMsg(text, chat_id)` —— `Chat.SendMsg(self, msg, who=None, ...)` 的第二个
           位置参数是 `who`，而 `WeChat.who` 是自己的昵称，传 wxid 进去打不中任何人，
           库会退到"当前打开的会话"把回复发进错的人那里，还返回成功。
        2. 成功判据只认 `status == "成功"`。库里 `WxResponse` 有三种状态
           （param.py: success/failure/error → 成功/失败/错误），把 `错误` 算成功会掩盖真实失败。
        """
        if self._wx is None:
            return False
        # 上次失败没清掉的残留，先趁现在清一次：库会在几分钟后自己补回车，
        # 那就会把残留发出去，必须抢在它前面。
        self._sweep_leftovers()
        # **@ 消息要在碰 UI 之前就拒绝**：`at_member` 会用 OCR 找 @ 下拉弹层里的成员，
        # OCR 不可用时它会「搜到群 → 点开会话 → 输入 '@' → 找不到成员」地半途失败，
        # 症状是**群里什么都没发出去，而会话被点开了**（2026-10-16 实测踩到）。
        # 宁可什么都不做，也不要留下这种半成品状态。
        if at and not self._ocr_ok:
            self._stats["refused"] += 1
            print("[wechatauto] 拒绝发送 @ 消息：@ 成员需要 OCR 定位弹层，而本机 OCR 不可用。"
                  "把配置里的 group.at_back 关掉即可恢复正常发送（回复照发，只是不带 @）。")
            return False
        with self._lock:
            chat = self._chat_cache.get(chat_id)
        if chat is None:
            self._stats["refused"] += 1
            print(f"[wechatauto] 拒绝发送：会话 {chat_id} 不在缓存里（拿不到 Chat 对象，宁可不发也不发错人）")
            return False

        # —— 后台发送优先（「全面后台化」第一步）——
        # 目标会话**当前就开着**时，走 `SetValue + PostMessage 回车`：
        # 不动鼠标、不抢焦点，窗口最小化也能发（2026-10-17 真机实测）。
        # 会话不对、或者要带 @（@ 要用弹层，只能 GUI）才退回下面那条路。
        if not at and self.send_mode != "gui":
            # 发送期间挂标志：清残留的后台看门狗看到它会跳过，
            # 否则它可能在 SetValue 之后、回车之前把正文清掉（等于这条发不出去）。
            self._sending = True
            try:
                attempted, bg_ok = self._try_background(chat_id, chat, text)
            finally:
                self._sending = False
            if attempted and bg_ok:
                return True
            if self.send_mode == "background":
                # 目标会话没打开、或后台发送没成 —— 都不许退回 GUI：
                # 那条路会打开会话、把微信拉到前台，正是这个模式要避免的事。
                self._stats["send_failed"] += 1
                print("[wechatauto] send_mode=background：目标会话没打开或后台发送没成 ⇒ 这条不发"
                      "（不打扰优先）。要允许退回 GUI，把 send_mode 改成 auto。")
                return False
            if attempted:
                print("[wechatauto] 后台发送没成，退回 GUI 路径（这一步会打开会话、会抢一次焦点）")
            else:
                # `attempted=False` 表示**根本没找到能后台发的窗口**（目标会话没开着）。
                # 旧写法在这里一声不吭地掉进 GUI，于是"有时候它会弹窗抢焦点"没法从日志里查。
                print(f"[wechatauto] 会话 {chat_id} 现在没开着 ⇒ 走 GUI（会打开会话、抢一次焦点）",
                      flush=True)

        # GUI 发送前最后一关：GUI 引擎绑定的必须真是微信窗口，
        # 否则它会把字打进那个窗口（实测绑到过 DSH 的浏览器窗口）。
        if not self._gui_is_wechat():
            self._stats["refused"] += 1
            return False

        # 上一次超时可能把鼠标键留在“按下”状态，先松开再动界面（见 _release_stuck_mouse）
        self._release_stuck_mouse("发送前")
        self._sending = True
        try:
            try:
                resp = self._send_with_timeout(chat, text, at)
            finally:
                self._sending = False
        except TimeoutError:
            self._stats["timeout"] += 1
            print(f"[wechatauto] 发送超时（>{self.send_timeout_sec:.0f}s），判定失败并触发重连")
            self._ensure_no_leftover(text)
            self._release_stuck_mouse("超时后")
            return False
        except Exception as exc:
            self._stats["error"] += 1
            if "winrt" in str(exc).lower():
                # 这条报错**看着不像"发不出去"**（实测为此白跑了一轮真机验证），所以特判：
                # UIA 控件树拿不到时会降级到 OCR 驱动，而 OCR 依赖 winsdk._winrt。
                print("[wechatauto] 发送异常：OCR 兜底驱动不可用（winsdk._winrt 加载失败）。"
                      "根因通常不是发送本身，而是 **UIA 控件树拿不到**——"
                      "最常见是在受限/沙箱环境里跑（热激活要读 Weixin.dll 的模块表，被拦）。"
                      "对策：让桥接与微信在同一个普通用户会话里运行，不要套沙箱、也不要提权。")
            else:
                print(f"[wechatauto] 发送异常: {exc!r}")
            self._ensure_no_leftover(text)
            self._release_stuck_mouse("异常后")
            return False

        status = str(resp.get("status", "") if isinstance(resp, dict) else getattr(resp, "status", ""))
        message = resp.get("message", "") if isinstance(resp, dict) else getattr(resp, "message", "")
        ok = status == "成功"

        # **@ 失败不能让整条消息丢掉**：降级成不带 @ 的普通发送。
        # 为什么必须这样：加不上一个 @ 不该让回复一个字都说不出去。
        # 实测踩过：群里什么都没发出去，会话却被点开了，用户看到的就是"它动了但没说话"。
        #
        # **但降级不许重新导航**（KI-1 的 Bug A）：老写法在这里调 `_send_with_timeout(chat, text, "")`
        # → `chat.SendMsg` → `gui.send_msg` → `uia.open_chat(who)`，会话明明已经打开却
        # 又搜了一次群，这就是用户看到的"搜两次群"。现在改成原地补发。
        if at and not ok:
            self._stats["at_fallback"] = self._stats.get("at_fallback", 0) + 1
            print(f"[wechatauto] @ 成员失败（status={status!r} message={message!r}）"
                  "⇒ 降级为不带 @ 的普通发送（消息比 @ 重要）")
            # 先看数据库里有没有：@ 路径的最后一步可能已经点过发送，
            # 只是 `click_send` 没确认到输入框清空。直接补发会变成重复消息。
            if text and self.last_sent_landed(chat_id, text, within_sec=15, strict=True):
                print("[wechatauto] 数据库里已能查到这条消息 ⇒ 判为已发送，不再补发")
                at = ""
                ok = True
            elif isinstance(resp, dict) and resp.get("_chat_open"):
                resp = self._send_plain_inplace(chat, text)
                status = str(resp.get("status", "") if isinstance(resp, dict) else getattr(resp, "status", ""))
                message = resp.get("message", "") if isinstance(resp, dict) else getattr(resp, "message", "")
                ok = status == "成功"
                at = ""
            else:
                # 连"目标会话是不是开着的"都没确认：就地补发可能发给别人。
                # 宁可这条不发，也不破坏"绝不发错人"这条硬规则。
                self._stats["send_failed"] += 1
                print("[wechatauto] @ 失败且会话未确认打开 ⇒ 拒绝降级补发（宁可不发也不发错人）")
                self._ensure_no_leftover(text)
                return False

        if not ok:
            self._stats["send_failed"] += 1
            print(f"[wechatauto] 发送未成功：status={status!r} message={message!r}")
            # ★ 2026-10-18 两次「连发三遍」就是从这儿漏的：GUI 发送失败时
            #   那半句还留在输入框里，7 分钟后被库的界面维护动作冲出去。
            self._ensure_no_leftover(text)
            self._release_stuck_mouse("未成功后")
            return False

        # **库里报"成功"不等于真发出去了**：它的成功判据是"输入框看起来空了"（像素启发式），
        # 窗口移位/锁屏/剪贴板被占都可能让它误判。所以再回读一次数据库确认落库。
        # 复核窗口取 tighten（默认 20 秒）：既排除历史旧消息，也避免撞上时效闸门（15 分钟）。
        if self.verify_send:
            landed = self.last_sent_landed(chat_id, text, within_sec=self.verify_window_sec)
            if not landed:
                self._stats["send_unverified"] += 1
                self._stats["send_failed"] += 1
                print(f"[wechatauto] 已返回成功但 {self.verify_window_sec:.0f} 秒内数据库里查不到这条消息，"
                      f"按未送达处理（宁可重试也不要假成功）")
                return False

        self._stats["sent"] += 1
        return True

    # -- 后台发送（不动鼠标、不抢焦点） --------------------------------

    def _uia(self) -> Any:
        """拿库的 UIA 引擎（拿不到返回 None）。"""
        gui = self._gui_engine()
        if gui is None:
            return None
        try:
            return gui._get_uia()
        except Exception:
            return None

    # -- 「发送失败后输入框里剩下的那半句」 ------------------------------
    #
    # 2026-10-18 真机踩到**两次**（同一形态）：
    #   20:48 GUI 发送失败 → 20:56 群里连发三遍「哈哈别动手，我这不是夸你入味嘛」
    #   22:39 GUI 发送失败 → 22:47 群里连发三遍「小升初？」
    # 时间差都是 7-8 分钟，间隔都是 27 秒，而且**完全没有日志**。
    #
    # 机制：GUI 发送是「把字写进输入框 → 回车」。失败时那半句**留在框里**，
    # 之后库自己做界面维护（重新校准布局、点发送按钮、补回车）把它冲出去了。
    # 这条路径不经过 `send()`，所以限速、审计、`_clean()` 的 @ 清洗**全都绕过了**——
    # 最坏的情况是：框里残留一句带 @ 或带「验证码」的话，它照样会发出去。
    #
    # 安全前提：这个框**也是用户自己的草稿框**。所以只在
    # 「框里的内容 == 我们刚尝试发送的那串字」时才清，绝不无脑 Ctrl+A。

    def _input_box_text(self) -> Optional[str]:
        """读输入框当前正文。读不到返回 None（**不等于空**）。"""
        uia = self._uia()
        if uia is None:
            return None
        try:
            ctrl = uia._chat_input()             # noqa: SLF001 —— 库只提供这个方法
        except Exception:
            return None
        if ctrl is None:
            return None
        try:
            return uia._read_edit_value(ctrl)    # noqa: SLF001
        except Exception:
            return None

    @staticmethod
    def _norm_box(text: str) -> str:
        """比对用的归一化：去掉所有空白（框里可能有换行/全角空格）。"""
        return "".join(str(text or "").split())

    def _clear_input_if_matches(self, expected: str) -> str:
        """把我们写失败的那串字从输入框里抹掉。

        返回：`cleared` / `empty` / `not_ours` / `unreadable` / `error`。
        `not_ours` 是**正常且有价值**的结果——说明框里是用户自己在打的草稿，
        一个字都不碰。
        """
        want = self._norm_box(expected)
        if not want:
            return "empty"
        current = self._input_box_text()
        if current is None:
            return "unreadable"                  # 读不到就别赌，宁可留着
        got = self._norm_box(current)
        if not got:
            return "empty"
        # 完全一致最好；对不上时允许"包含"——真机上残留常被截断或和别的东西拼在一起，
        # 只认全等会漏。长度门槛 10 字，避免把用户一句偶然相同的话当残留清掉。
        same = got == want
        overlap = len(want) >= 10 and (want in got or got in want)
        if not (same or overlap):
            return "not_ours"
        uia = self._uia()
        ctrl = uia._chat_input() if uia is not None else None   # noqa: SLF001
        if ctrl is None:
            return "unreadable"
        try:
            vp = ctrl.GetValuePattern()
            if vp is None or vp.IsReadOnly:
                return "unreadable"
            vp.SetValue("")                      # SetValue 不敲键盘、不动焦点
        except Exception:
            return "error"
        self._stats["leftover_cleared"] = self._stats.get("leftover_cleared", 0) + 1
        print(f"[wechatauto] 发送失败后清掉了输入框残留（{len(want)} 字）：{want[:20]}",
              flush=True)
        return "cleared"

    def _remember_leftover(self, text: str) -> None:
        """记下"这次没清干净"的正文，下一次发送前再试一次。

        为什么还要这一步：失败的那一刻输入框可能正好读不到（窗口在动、UIA 抽风），
        但过几十秒往往就正常了。库会在几分钟后自己补回车，所以**必须清掉**，
        不能只记一笔。
        """
        if not text:
            return
        with self._lock:
            self._leftovers.append(text)
            del self._leftovers[:-5]             # 只留最近 5 条，别无限长

    # -- 「鼠标按键被卡住」导致整页文字被选中 ------------------------------
    #
    # 2026-10-19 真机现象：**每次发送超时之后，鼠标光标偶尔会滑动**，
    # 然后 DSH 网页里的文字整片被选中变蓝（用户一上午撞到三次）。
    #
    # 根因：GUI 发送是「SetCursorPos + mouse_event(LEFTDOWN) + mouse_event(LEFTUP)」，
    # 而 `_send_with_timeout` 在 20 秒处**放弃等待**（工作线程还在跑）。如果放弃点正好
    # 落在 DOWN 与 UP 之间，系统就认为左键一直按着 —— 之后任何一次移动光标都会变成
    # **拖拽选择**；在微信以外的窗口上，就是整页文字变蓝。
    #
    # 所以：GUI 发送**开始前**和**失败后**都查一次按键状态，按着就松开。
    # 只在这条路径上做，避免误松用户自己正在拖拽的按键。

    def _release_stuck_mouse(self, when: str = "") -> bool:
        """左/右键还按着就松开它。返回是否真的松了。"""
        try:
            import win32api
        except Exception:
            return False
        released = False
        try:
            for vk, upflag, name in ((0x01, 0x0004, "左键"), (0x02, 0x0008, "右键")):
                if win32api.GetAsyncKeyState(vk) & 0x8000:
                    win32api.mouse_event(upflag, 0, 0, 0, 0)
                    released = True
                    print(f"[wechatauto] 松开卡住的鼠标{name}（{when}）——"
                          "否则接下来的光标移动会变成拖拽选择", flush=True)
        except Exception:
            return False
        if released:
            self._stats["mouse_released"] = self._stats.get("mouse_released", 0) + 1
        return released

    def _schedule_leftover_cleanup(self, text: str,
                                   seconds: float = 180.0,
                                   interval: float = 1.5) -> None:
        """失败后在**后台反复**尝试清掉输入框里那句残留。

        为什么必须反复（2026-10-19 血的教训）：失败那一刻窗口正在动，UIA 常常读不到值
        （`unreadable`）。0.1.1 的做法是"记下来，下次发送前再清"——可那一次失败之后
        **整个下午都没有下一次发送**，于是没有任何机会清，8 分钟后库自己补回车，
        群里照样三连。所以：不能等下一次发送，得自己盯着。
        """
        if not text:
            return

        def loop() -> None:
            deadline = time.time() + max(5.0, seconds)
            while time.time() < deadline:
                time.sleep(interval)
                if self._sending:          # 正在发别的消息：别插进去把正文清掉
                    continue
                try:
                    status = self._clear_input_if_matches(text)
                except Exception:
                    continue
                if status == "cleared":
                    return
                if status in ("empty", "not_ours"):
                    return                 # 没得清 / 是别人的草稿：收工
            print(f"[wechatauto] 残留清理超时放弃（{seconds:.0f}s 内一直读不到输入框）：{text[:20]}",
                  flush=True)

        threading.Thread(target=loop, daemon=True, name="wx-leftover-clean").start()

    def _ensure_no_leftover(self, text: str) -> None:
        """发送失败后的统一收尾：能清就清，清不了就记下来、下次发送前再清。

        为什么记下来也要做：失败那一刻窗口可能正在动、UIA 读不到值，
        但几十秒后往往就正常了；而库是在**几分钟后**才补回车的，来得及。
        """
        status = self._clear_input_if_matches(text)
        if status in ("unreadable", "error"):
            self._remember_leftover(text)
            # 立刻起个后台看门狗盯着清（只试一次几乎必然试不到，见上面那段注释）
            self._schedule_leftover_cleanup(text)

    def _sweep_leftovers(self) -> None:
        """发送前顺手把上次没清掉的残留再清一次（清不掉就继续留着下次试）。"""
        with self._lock:
            pending = list(self._leftovers)
        if not pending:
            return
        keep = []
        for text in pending:
            if self._clear_input_if_matches(text) != "cleared":
                keep.append(text)
        with self._lock:
            self._leftovers = keep

    def _bg_sender(self) -> Optional[BackgroundSender]:
        """惰性拿一个**主窗口**的后台发送器；拿不到返回 None（调用方退回 GUI）。

        为什么惰性：`WeChatUIA()` 要去找窗口、读 `Weixin.dll` 的模块表，
        `--doctor` / `--dry-run` 这类不发送的运行不该为它付代价。
        """
        uia = self._uia()
        if uia is None:
            return None
        with self._lock:
            if self._bg is not None:
                return self._bg
        sender = BackgroundSender.attach(uia)
        if sender is None:
            return None
        with self._lock:
            self._bg = sender
        print(f"[wechatauto] 已启用后台发送（主窗口句柄 {sender.hwnd}）："
              "目标会话已经开着时，不再移动鼠标、不再抢焦点")
        return sender

    def _refresh_bg_windows(self) -> dict:
        """扫描微信的「独立会话窗口」，返回 {会话名: hwnd}。

        **这是"全面后台化"的关键一环**：会话列表在 UIA 里没有行，所以"点开另一个
        会话"只能靠鼠标；但微信 4.x 支持把会话拖成独立窗口（`mmui::ChatSingleWindow`），
        每个这样的窗口**自带 `chat_input_field`**，而且 `ValuePattern` 可写
        （2026-10-17 实测）。于是"一个会话一个窗口、永不切换"就把切会话整个绕过去了。

        缓存 5 秒：窗口是用户手动开/关的，没必要每次发送都枚举全桌面。
        """
        now = time.time()
        if self._bg_windows_at and now - self._bg_windows_at < 5.0:
            return self._bg_windows
        uia = self._uia()
        if uia is None:
            return self._bg_windows
        try:
            import uiautomation as auto
            import win32gui
        except Exception:
            return self._bg_windows

        found: dict[str, tuple] = {}

        def visit(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                cls = win32gui.GetClassName(hwnd) or ""
                # 便宜的先过滤：别对桌面上每个窗口都建一个 UIA 节点
                if not cls.startswith("Qt") or "QWindowIcon" not in cls:
                    return True
                title = (win32gui.GetWindowText(hwnd) or "").strip()
                if not title or title in ("微信", "Weixin"):
                    return True
                ctrl = auto.ControlFromHandle(hwnd)
                if ctrl is None or (ctrl.ClassName or "") != "mmui::ChatSingleWindow":
                    return True
                # **把控件对象一起存下来**：后面发送时直接用它，
                # 省掉一次会偶发 COM 错误的 ControlFromHandle。
                found[title] = (int(hwnd), ctrl)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(visit, None)
        except Exception as exc:
            print(f"[wechatauto] 枚举独立会话窗口失败：{exc!r}")
            return self._bg_windows
        self._bg_windows = found
        self._bg_windows_at = now
        if found:
            print("[wechatauto] 独立会话窗口：" + "、".join(sorted(found)))
        return found

    def _sender_for(self, who: str) -> Optional[BackgroundSender]:
        """给某个会话找个能**后台发送**的窗口。

        顺序：①主窗口（当前会话正好是它）→ ②独立会话窗口（窗口标题就是会话名）。
        都没有返回 None，由调用方决定退回 GUI 还是放弃。
        """
        if not who:
            return None
        # ⚠ `chat.who` 是 **wxid**（`11111111111@chatroom`），而独立窗口的标题是
        # **显示名**（`测试群`）。只按 who 匹配永远匹配不上，于是静默退回 GUI 路径
        # （表现就是"照样把窗口弹出来"）。所以两个名字都要试。
        names = [who]
        try:
            nick = self._nickname(who)
            if nick and nick not in names:
                names.append(nick)
        except Exception:
            pass
        main = self._bg_sender()
        if main is not None:
            try:
                current = main.current_chat()
                if current and current in names:
                    return main
            except Exception:
                pass
        windows = self._refresh_bg_windows()
        matched = ""
        item = None
        for name in names:
            if name in windows:
                matched, item = name, windows[name]
                break
        if not item:
            return None
        try:
            hwnd, ctrl = item
        except Exception:
            hwnd, ctrl = int(item), None
        with self._lock:
            sender = self._bg_window_senders.get(hwnd)
        if sender is not None:
            return sender
        # 优先复用扫描时拿到的控件对象（不再调 ControlFromHandle，少一次偶发 COM 错误）；
        # 对象失效时再按句柄重绑，并且**重试几次**——一次失败就退回 GUI 会把窗口弹出来。
        sender = BackgroundSender.attach_control(self._uia(), ctrl, chat_name=matched)
        for attempt in range(3):
            if sender is not None:
                break
            time.sleep(0.3)
            sender = BackgroundSender.attach_window(self._uia(), hwnd, chat_name=matched)
            if sender is not None:
                print(f"[wechatauto] 独立会话窗口「{matched}」按句柄重绑成功（第 {attempt + 1} 次重试）")
        if sender is None:
            print(f"[wechatauto] ⚠ 独立会话窗口「{matched}」绑不上（hwnd={hwnd}）——"
                  "后台路径不可用，只能退回 GUI（会把窗口弹出来）")
            return None
        with self._lock:
            self._bg_window_senders[hwnd] = sender
        print(f"[wechatauto] 绑定独立会话窗口「{matched}」（hwnd={hwnd}）")
        return sender

    @staticmethod
    def _window_looks_like_wechat(cls: str) -> bool:
        """窗口类名像不像微信。微信 4.x 全是 Qt 窗口（`Qt51514QWindowIcon` 等），
        浏览器是 `Chrome_WidgetWin_1`，其它应用各有各的类名。"""
        return str(cls or "").startswith("Qt")

    def _gui_is_wechat(self) -> bool:
        """GUI（OCR/坐标）引擎当前绑定的**是不是微信窗口**。

        实测踩到过：桥接启动时 `WeChatGUI` 把**前台窗口**当成了微信
        （日志：`WeChatGUI 初始化成功：hwnd=198172`，而那是 DSH 的浏览器窗口，
        rect 完全对不上）。这条路径一旦真被用到，它会把字打进那个窗口里——
        轻则发不出，重则往别人的页面里打字。所以 GUI 发送前必须核对一次。

        判据：微信 4.x 的窗口类名都以 `Qt` 开头（`Qt51514QWindowIcon`），
        浏览器是 `Chrome_WidgetWin_1`、其它应用也各有各的类名。
        取不到句柄时不拦（保持老行为，不因为探测失败就发不出去）。
        """
        gui = self._gui_engine()
        hwnd = int(getattr(gui, "hwnd", 0) or 0)
        if not hwnd:
            return True
        try:
            import win32gui
            cls = win32gui.GetClassName(hwnd) or ""
        except Exception:
            return True
        if self._window_looks_like_wechat(cls):
            return True
        print(f"[wechatauto] ⚠ 拒绝走 GUI 发送：GUI 引擎绑定的窗口 {hwnd} 类名是 {cls!r}，"
              "不是微信（微信窗口类名以 Qt 开头）。这通常是启动时把前台窗口误当成微信——"
              "继续走 GUI 会把内容打进别的窗口。请重启桥接。")
        return False

    def can_send_background(self, chat_id: str) -> bool:
        """这个会话**现在**能不能不抢焦点地发（不真的发，只查窗口）。

        判据和 `_try_background` 完全一致（同一套主窗口/独立窗口查找）。
        桥接用它决定"多段回复能不能真的分成几条"：

            目标会话没开着 ⇒ 每一段都要走一次 GUI（打开会话 + 抢焦点）。
            分三段就是弹三次窗口。对正在用电脑的人来说，这比"少说两句"难受得多，
            所以桥接会先把多段压成一条再发。

        约束：只在**发送前**问一次。问完到真发之间窗口状态可能变
        （用户手动切走了），那就照旧走 GUI——这里不做乐观假设。
        """
        if self._wx is None or self.send_mode == "gui":
            return False
        with self._lock:
            chat = self._chat_cache.get(chat_id)
        if chat is None:
            return False
        who = str(getattr(chat, "who", "") or "")
        try:
            return self._sender_for(who) is not None
        except Exception:
            return False

    def _try_background(self, chat_id: str, chat: Any, text: str) -> tuple[bool, bool]:
        """尝试后台发送，返回 (有没有试, 成没成)。

        **会话必须已经有窗口**（主窗口正开着它，或它有自己的独立窗口）才试。
        后台发送不做任何导航，会话不对就必须交给 GUI 路径去打开——
        否则消息会落进当前那个会话（发错人）。
        """
        who = str(getattr(chat, "who", "") or "")
        sender = self._sender_for(who)
        if sender is None:
            return False, False
        # —— 用户在打字就先等一等 ——
        # 微信发送时会把窗口抬到前台（客户端行为），如果此刻他正在别处敲键盘，
        # 那一下会被打断。等他一停手再发，体感好得多；等不到也照发（消息不能压着）。
        if self.bg_idle_gate_sec > 0:
            deadline = time.time() + max(0.0, self.bg_idle_wait_sec)
            waited = 0.0
            while BackgroundSender.user_idle_sec() < self.bg_idle_gate_sec:
                if time.time() >= deadline:
                    print(f"[wechatauto] 用户一直在操作（已等 {self.bg_idle_wait_sec:.0f}s），仍然发送")
                    break
                time.sleep(0.3)
                waited += 0.3
            if waited >= 0.3:
                print(f"[wechatauto] 为了让用户先停手，这条回复等了 {waited:.1f}s 才发")
        ok, why = sender.send(text, minimize_after=self.bg_minimize_after)
        print(f"[wechatauto] 后台发送（{who}）：{why}")
        if not ok:
            return True, False
        self._stats["sent_bg"] += 1
        if self.verify_send and not self.last_sent_landed(chat_id, text,
                                                          within_sec=self.verify_window_sec):
            # 输入框已清空 ⇒ 微信那边确实提交了，只是库还没落盘（或暂时读不到）。
            # 这里**绝不重发**：重发就是重复消息。只记一笔，让审计看得见。
            self._stats["send_unverified"] += 1
            print(f"[wechatauto] 后台发送已提交，但 {self.verify_window_sec:.0f} 秒内数据库还查不到"
                  "（按已发送处理，不重发）")
        self._stats["sent"] += 1
        return True, True

    def last_sent_landed(self, chat_id: str, text: str, within_sec: float = 20.0,
                         strict: bool = False) -> bool:
        """回读本地数据库，确认这条消息真的以「我」的身份落库了。

        判据（三条同时满足）：会话对得上、发送者是自己、正文包含发出的文本、且是最近
        `within_sec` 秒内的记录。数据库读不到（异常/超时）时**返回 True**——
        复核是加分项，不能因为它自己出问题就把已经发出去的消息判成失败。

        `strict=True` 把上面那条兜底反过来：读不到/出错都算**没查到**。
        用途是「@ 失败后要不要补发」这种会**产生重复消息**的判断——那里问的不是
        "能不能证明没发出去"，而是"有没有证据证明已经发出去了"，两者不能共用一个默认值。
        """
        if self._wx is None:
            return False if strict else True
        if not text:
            return True
        db = getattr(self._wx, "_db", None)
        if db is None:
            return False if strict else True

        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                rows = db.get_messages(chat_id, limit=5)
                box["rows"] = list(rows or [])
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True, name="wx-verify")
        thread.start()
        thread.join(timeout=self.verify_timeout_sec)
        if thread.is_alive():
            print("[wechatauto] 回读数据库超时，跳过复核（按已发送处理）")
            return False if strict else True
        if "error" in box:
            print(f"[wechatauto] 回读数据库失败，跳过复核（按已发送处理）: {box['error']!r}")
            return False if strict else True

        import time as _time

        now = _time.time()
        needle = (text or "").strip()[:40]
        for row in box.get("rows", []):
            content = row.get("content") or ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            sender = str(row.get("sender_username") or row.get("sender_id") or "")
            if sender not in (self._self_wxid, "2") and sender != self._self_wxid:
                continue
            if needle and needle not in str(content):
                continue
            try:
                age = now - float(row.get("create_time") or 0)
            except Exception:
                age = 0.0
            if age <= within_sec:
                return True
        return False

    def _send_with_timeout(self, chat: Any, text: str, at: str = ""):
        """给发送套一层超时。

        为什么需要：库的发送路径最坏会走到 75 秒（OCR 兜底 + 前台窗口等待），
        而底层锁是普通的 `with`，一旦卡死会永久持锁、后续所有界面操作全挂，
        调用方还看不到（一直阻塞、不抛异常）。这里超时就放弃并让上层重连。

        `at` 非空时**不再交给库的 `at_member`**，改走本类的 `_send_at`。库那条路
        有两个硬伤（KI-1，都是真机踩出来的）：

        1. **失败后补发会再导航一次**：`at_member` 自己 `open_chat`，失败后我们调
           `send_msg` 兜底，而 `send_msg` 又 `open_chat` —— 用户看到的就是「搜两次群」。
        2. **选中成员等于白选**：`at_member` 点完成员后调 `input_text()`，而
           `input_text` 开头就是 `Ctrl+A → Delete`（为了清掉上一轮残留），
           会**把刚插进输入框的 @ 一起删掉**，最后发出去的还是不带 @ 的正文。
           这条是读代码看出来的，还没在真机复现过——但改完之后 @ 真能成，
           它一定会在下一步暴露，所以一并解掉：正文改用「剪贴板 + Ctrl+V」，
           **不清空**，让 @ 留在原地。
        """
        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                box["resp"] = self._send_at(chat, text, at) if at else chat.SendMsg(text, at=None)
            except Exception as exc:                      # 线程里的异常要捞出来
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True, name="wx-send")
        started = time.time()
        thread.start()
        thread.join(timeout=self.send_timeout_sec)
        took = time.time() - started
        if took > 8:
            # 慢发送必须留痕：整条主线是同步的，一次慢发送 = 一段时间里它谁都不理。
            # 真机 2026-10-18 20:48 就卡了 90 秒，看起来完全像"机器人死了"。
            print(f"[wechatauto] 发送很慢：{took:.0f} 秒（阈值 {self.send_timeout_sec:.0f}s）",
                  flush=True)
        if thread.is_alive():
            raise TimeoutError(f"SendMsg 超过 {self.send_timeout_sec:.0f} 秒未返回")
        if "error" in box:
            raise box["error"]
        return box.get("resp", {})

    # -- 自己实现的 @（KI-1 的改法 A + B）--------------------------------
    #
    # 调用约定：`_send_at` 永远返回一个 dict（含 `status` / `message`），
    # 并用私有键 `_chat_open` 告诉上层「目标会话此刻是不是开着的」——
    # 只有它为 True 时，上层才允许**就地降级**补发正文（不重新导航）。

    def _gui_engine(self) -> Any:
        """拿库的 GUI 引擎（`WeChatGUI`）。没有它就什么都发不出去。"""
        return getattr(self._wx, "_gui", None) if self._wx is not None else None

    @staticmethod
    def _current_chat_name(gui: Any) -> str:
        """当前打开的是哪个会话（UIA 优先，退到 GUI 自己记的名字）。读不到返回空串。"""
        try:
            uia = gui._get_uia() if hasattr(gui, "_get_uia") else None
            if uia is not None:
                name = uia.current_chat()
                if name:
                    return str(name)
        except Exception:
            pass
        return str(getattr(gui, "_current_chat", "") or "")

    @staticmethod
    def _match_member(items: list, member: str) -> Optional[tuple]:
        """在 OCR 出来的候选里找最像 `member` 的那一项。

        多策略、依次放宽：全等 → 前缀 → 包含 → 模糊相似度 ≥ 0.6。
        为什么不能只用库里的 `member in t`：OCR 会把昵称读成带空格 / 缺字 /
        多一个字的形态，库原来那条单判据在真机上正是这么漏掉的（KI-1 的 Bug B）。
        """
        def norm(value: Any) -> str:
            return "".join(str(value or "").split())

        target = norm(member)
        if not target:
            return None
        best, best_score = None, 0.0
        for item in items or []:
            try:
                text = norm(item[0])
            except Exception:
                continue
            if not text:
                continue
            if text == target:
                score = 1.0
            elif text.startswith(target) or (len(text) >= 2 and target.startswith(text)):
                score = 0.9
            elif target in text:
                score = 0.8
            else:
                score = difflib.SequenceMatcher(None, text, target).ratio()
            if score > best_score:
                best, best_score = item, score
        return best if best_score >= 0.6 else None

    @staticmethod
    def _popup_rect(gui: Any, box: Optional[tuple]) -> tuple:
        """@ 成员弹层在窗口里的相对区域（和库里的算法一致，但输入框拿不到时有兜底）。"""
        render_h = int(getattr(gui, "render_h", 800) or 800)
        render_w = int(getattr(gui, "render_w", 1200) or 1200)
        left = int(getattr(gui, "right_pane_left", 0) or 0)
        bottom = box[1] if box else int(render_h * 0.77)
        return (left, max(80, bottom - 500), render_w, bottom)

    @staticmethod
    def _wait_input_box(gui: Any, timeout: float = 2.5) -> Optional[tuple]:
        """等输入框被探测出来。刚切会话/刚唤醒时探测会连续失败，所以别只试一次。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                box = gui.get_input_box()
            except Exception:
                box = None
            if box:
                return box
            time.sleep(0.3)
        return None

    @staticmethod
    def _poll_popup(gui: Any, popup: tuple, timeout: float = 3.0,
                    interval: float = 0.35) -> tuple[list, float]:
        """轮询 OCR 直到弹层里有内容（或超时）。

        **这是 KI-1 的核心修法之一**：库只 `sleep(0.9)` 然后 OCR 一次。界面刚经历
        「最小化遮挡窗口 → UIA 热激活 → 输入框连续探测失败 → 重校准布局」时，
        0.9 秒根本不够弹层出现——OCR 拿到空列表，于是报「未在成员列表中找到」。
        改成"有内容就继续、没内容就再等"，把时间花在真正需要等的地方。
        """
        t0 = time.time()
        items: list = []
        while True:
            try:
                items = list(gui.ocr(popup) or [])
            except Exception as exc:
                print(f"[at] OCR 异常：{exc!r}")
                items = []
            if items or time.time() - t0 >= timeout:
                return items, time.time() - t0
            time.sleep(interval)

    @staticmethod
    def _wait_popup_gone(gui: Any, popup: tuple, timeout: float = 1.5,
                         interval: float = 0.3) -> bool:
        """点完成员后等弹层收起。收起 ≠ 彻底空（弹层外还有聊天区），所以只看时间窗。"""
        time.sleep(0.35)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if not gui.ocr(popup):
                    return True
            except Exception:
                return True
            time.sleep(interval)
        return False

    @staticmethod
    def _clear_input(gui: Any) -> None:
        """清掉我们敲进输入框的 `@` / 过滤词，免得降级时把 `@史…` 当正文发出去。"""
        try:
            gui._input.key(VK_A, ctrl=True)
            gui._input.key(VK_DELETE)
            time.sleep(0.25)
        except Exception as exc:
            print(f"[at] 清空输入框失败：{exc!r}")

    def _send_at(self, chat: Any, text: str, member: str) -> dict:
        """@ 某人再发正文：自己实现，替代库的 `at_member`。

        四步（每一步都有证据可查，出问题看日志就知道卡在哪）：
          1. **只导航一次**——当前会话已经是目标就不再 `open_chat`；
          2. **等弹层而不是死睡**——轮询 OCR 直到有内容；
          3. **键入名字前缀过滤**——微信弹层只列最近 @ 过的人，不过滤常常匹配不到；
          4. **多策略匹配**——全等 / 前后缀 / 包含 / 模糊，容忍 OCR 抖动。

        任何一步失败都**不重新导航**，只把 `_chat_open` 交回上层，由上层决定
        原地补发（`_send_plain_inplace`）。
        """
        gui = self._gui_engine()
        who = str(getattr(chat, "who", "") or "")
        record: dict[str, Any] = {"status": "失败", "message": "", "_chat_open": False}
        if gui is None:
            record["message"] = "没有 GUI 引擎"
            return record
        if not who:
            record["message"] = "会话名为空（不敢在这些前提下动界面）"
            return record

        def fail(message: str) -> dict:
            record["message"] = message
            print(f"[at] 失败：{message}（member={member!r} who={who!r}）")
            return record

        try:
            if not gui.ensure_visible():
                return fail("微信窗口不可见（可能锁屏/最小化）")
        except Exception as exc:
            return fail(f"ensure_visible 异常：{exc!r}")

        # —— 1. 只导航一次 ——
        try:
            current = self._current_chat_name(gui)
            if current != who:
                if not gui.open_chat(who):
                    return fail(f"打不开会话：{who}")
                time.sleep(0.8)
                current = self._current_chat_name(gui)
            if current and current != who:
                return fail(f"打开的会话不是目标（当前 {current!r}）")
        except Exception as exc:
            return fail(f"打开会话异常：{exc!r}")
        # 到这一步会话一定已经走到目标（或读不到名字但打开成功），允许上层就地降级。
        record["_chat_open"] = True

        # —— 2. 输入框 + 敲 '@' ——
        box = self._wait_input_box(gui)
        if not box:
            return fail("输入框定位不到")
        try:
            if not gui.focus_input(box):
                return fail("输入框聚焦失败")
            gui._input.type_unicode("@")
        except Exception as exc:
            return fail(f"键入 @ 失败：{exc!r}")

        # —— 3. 等弹层 + 找成员 ——
        popup = self._popup_rect(gui, box)
        items, waited = self._poll_popup(gui, popup, timeout=self.at_popup_timeout)
        print(f"[at] 弹层第 1 轮：等 {waited:.2f}s 拿到 {len(items)} 项；"
              f"items={[i[0] for i in items][:12]} popup={popup} box={box}")
        target = self._match_member(items, member)

        # —— 4. 没匹配到就输入名字前缀过滤，再等一轮 ——
        if target is None:
            prefix = "".join(str(member or "").split())[:2]
            if prefix:
                try:
                    gui._input.type_unicode(prefix)
                except Exception as exc:
                    print(f"[at] 键入过滤词失败：{exc!r}")
                items2, waited2 = self._poll_popup(
                    gui, self._popup_rect(gui, box), timeout=self.at_popup_timeout)
                print(f"[at] 弹层第 2 轮（过滤 {prefix!r}）：等 {waited2:.2f}s 拿到 "
                      f"{len(items2)} 项；items={[i[0] for i in items2][:12]}")
                target = self._match_member(items2, member)
        if target is None:
            self._clear_input(gui)
            return fail(f"弹层里没匹配到成员（共看到 {len(items)} 项）")

        # —— 5. 点中成员 ——
        try:
            cx = int(getattr(gui, "origin_x", 0)) + int(target[1]) + int(target[3]) // 2
            cy = int(getattr(gui, "origin_y", 0)) + int(target[2]) + int(target[4]) // 2
            t_click = time.time()
            gui.wx_click(cx, cy)
            gone = self._wait_popup_gone(gui, self._popup_rect(gui, box),
                                         timeout=self.at_popup_gone_timeout)
            print(f"[at] 点中 {target[0]!r} @({cx},{cy})，"
                  f"弹层{'已收起' if gone else '仍在'}（{time.time() - t_click:.2f}s）")
            if not gone:
                # 弹层没收起 ⇒ 这一下没选中。别硬发：此时输入框里是 '@史'，
                # 直接粘贴正文会变成「@史 收到」发出去。
                self._clear_input(gui)
                return fail("点击成员后弹层没收起（没选中）")
        except Exception as exc:
            self._clear_input(gui)
            return fail(f"点击成员异常：{exc!r}")

        # —— 6. 补正文：剪贴板 + Ctrl+V，**不清空**（清空会把 @ 一起删掉）——
        if not text:
            record["status"] = "失败"
            record["message"] = "正文为空"
            return record
        try:
            if gui.set_clipboard(text) is False:
                self._clear_input(gui)
                return fail("剪贴板写入失败")
            gui._input.key(VK_V, ctrl=True)
            time.sleep(0.5)
            if not gui._input_box_has_text(box):
                self._clear_input(gui)
                return fail("粘贴正文未确认（输入框还是空的）")
            gui._last_input_box = box
            sent = bool(gui.click_send())
        except Exception as exc:
            return fail(f"补正文/发送异常：{exc!r}")

        record["status"] = "成功" if sent else "失败"
        record["message"] = "@成员消息已发送" if sent else "回车发送未确认"
        print(f"[at] 收尾：{record['status']}（member={member!r}）")
        return record

    def _send_plain_inplace(self, chat: Any, text: str) -> dict:
        """在**当前已打开的会话**里补发正文——全程不导航。

        @ 失败后的降级路径。这是"消息比 @ 重要"的落点：加不上 @ 也要把话说出去，
        但**不能重新打开会话**（那正是用户看到的第二次搜索）。

        两条前提缺一不补：
          ① 上层已确认目标会话就是当前会话（`_chat_open`）；
          ② 现在读到的当前会话名仍然等于目标（防止这中间界面被切走）。
        """
        gui = self._gui_engine()
        who = str(getattr(chat, "who", "") or "")
        if gui is None:
            return {"status": "失败", "message": "没有 GUI 引擎"}
        current = self._current_chat_name(gui)
        if who and current and current != who:
            print(f"[at] 当前会话已不是 {who!r}（现在是 {current!r}），拒绝就地补发")
            return {"status": "失败", "message": "会话已切换"}
        try:
            gui._pending_text = None            # 别让上一次的待发文本误拦这一次
            ok = bool(gui.input_text(text)) and bool(gui.click_send())
        except Exception as exc:
            return {"status": "错误", "message": repr(exc)}
        print(f"[at] 就地补发{'成功' if ok else '失败'}（未重新导航，不再搜群）")
        return {"status": "成功" if ok else "失败", "message": "就地补发"}


    def _listener_alive(self) -> bool:
        """底层监听线程还活着吗。

        审计结论：库的 `Listener._run` 会把所有异常吞掉只写 stderr（db.py 的 listener poll error），
        线程永远不退出，所以「线程死没死」只能查这个；而"线程活着但一直报错"要靠水位/收消息时间戳判断。
        """
        if self._wx is None:
            return False
        listener = getattr(self._wx, "_listener", None)
        if listener is None:
            return False
        thread = getattr(listener, "_thread", None)
        return bool(thread is not None and thread.is_alive())

    # ---------------- 健康判定：看"漏了没有"，不看"多久没动静" ----------------

    def _note_seen(self, chat_id: str, ts: float) -> None:
        """记下"我们收到过的最新消息"，供健康判定比对。"""
        try:
            if ts and ts > self._last_seen_ts:
                self._last_seen_ts = float(ts)
            if chat_id:
                self._seen_chats[chat_id] = max(float(ts or 0), self._seen_chats.get(chat_id, 0.0))
                if len(self._seen_chats) > 200:          # 别无限长，留最近的
                    for key, _ in sorted(self._seen_chats.items(), key=lambda kv: kv[1])[:50]:
                        self._seen_chats.pop(key, None)
        except Exception:
            pass

    def _newest_in_db(self, chats: "list[str]") -> tuple[float, str]:
        """这些会话里最新一条消息的 create_time（0 表示查不到）。"""
        db = getattr(self._wx, "_db", None) if self._wx is not None else None
        if db is None:
            return 0.0, ""
        best, best_chat = 0.0, ""
        for chat in chats[:8]:
            try:
                rows = db.get_messages(chat, limit=1)
            except Exception:
                continue
            for row in rows or []:
                try:
                    ts = float(row.get("create_time") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts > best:
                    best, best_chat = ts, chat
        return best, best_chat

    def _looks_dead(self) -> tuple[bool, str]:
        """「很久没收到消息」时再问一句：**库里有没有比我们收到的更新的消息？**

        有 → 监听真的漏了（该重连）；没有 → 这段时间就是没人说话（安静 ≠ 死）。
        为什么非改不可：原判据只看静默时长，于是群里一安静就每 15 分钟白重连一次，
        而重连要重新初始化 GUI + 重新解密数据库——正是最容易抢焦点、闪一下的动作。
        探测本身限频（默认 5 分钟一次），命中后由调用方决定是否重连。
        """
        now = time.time()
        if now - self._probe_at < self._probe_interval:
            return self._probe_dead, self._probe_reason
        self._probe_at = now
        chats = [c for c, _ in sorted(self._seen_chats.items(), key=lambda kv: kv[1], reverse=True)]
        chats = chats[:8] or [uid for uid in self.listen_uids if uid][:8]
        if not chats:
            self._probe_dead, self._probe_reason = False, "还没有已知会话，判不了"
            return self._probe_dead, self._probe_reason
        newest, chat = self._newest_in_db(chats)
        if not newest:
            self._probe_dead = False
            self._probe_reason = "库查询不到新消息（按安静处理）"
        elif newest > self._last_seen_ts + 5:
            self._probe_dead = True
            self._probe_reason = (f"库里有比我们收到的更新的消息（{chat}，"
                                  f"晚 {int(newest - self._last_seen_ts)} 秒），监听确实漏了")
        else:
            self._probe_dead = False
            self._probe_reason = "库里没有更新的消息（只是没人说话）"
        return self._probe_dead, self._probe_reason

    @property
    def stale_seconds(self) -> float:
        return time.time() - self._last_recv_ts

    def status(self) -> HookStatus:
        if self._wx is None:
            return HookStatus(ok=False, detail="未连接")
        alive = self._listener_alive()
        stale_sec = self.stale_seconds
        detail = (
            f"wechatauto-replica（UI 自动化；wxid={self._self_wxid or '未知'}；"
            f"监听线程={'活' if alive else '已停'}；距上次收消息 {stale_sec / 60:.1f} 分钟）"
        )
        if not alive:
            return HookStatus(ok=False, detail="[需要重连·监听线程已停] " + detail,
                              extra=dict(self._stats))
        if stale_sec <= self.stale_after_sec:
            return HookStatus(ok=True, detail=detail, extra=dict(self._stats))
        # 静默超过阈值：只有**正向证据**（库里有更新的消息却没回调）才判死。
        dead, why = self._looks_dead()
        hard = stale_sec > self.stale_after_sec * 3
        if dead:
            return HookStatus(ok=False, detail=f"[{why}] " + detail, extra=dict(self._stats))
        if hard:
            return HookStatus(
                ok=False,
                detail=(f"[静默 {stale_sec / 60:.0f} 分钟仍无动静（超过 "
                        f"{self.stale_after_sec * 3 / 60:.0f} 分钟兜底阈值），按失效处理] " + detail),
                extra=dict(self._stats))
        return HookStatus(ok=True, detail=f"[{why}] " + detail, extra=dict(self._stats))
