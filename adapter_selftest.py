"""适配器离线自测：不连微信，验证「消息 → 统一 Message → 队列」这条转换链。

背景：本机微信 4.x 客户端尚未登录任何账号，端到端没法跑。这里退一步，
用假的 msg/chat 对象喂进 `WechatautoHookAdapter._to_message`，验证：
    - 文本消息能被正确转换（字段映射对不对）；
    - 非文本（图片/文件）会被丢弃；
    - 自己发的消息被标记 is_self（bridge 靠这个防自问自答）；
    - 去重逻辑生效（同一条消息重复投递只出一次）；
    - 队列取空后返回 None（bridge 主循环据此休眠）。

跑法：python adapter_selftest.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wechat_bridge.adapters.hook_wechatauto import WechatautoHookAdapter  # noqa: E402


class FakeChat:
    def __init__(self, who: str, nickname: str = "") -> None:
        self.who = who
        self.nickname = nickname or who


class FakeMsg:
    """模拟 wechatauto 回调里那个 Message 对象（字段名照 wx.py 的实际实现）。"""

    def __init__(self, **kw) -> None:
        self.type = kw.get("type", "文本")
        self.content = kw.get("content", "")
        self.sender_id = kw.get("sender_id")
        self.sender_wxid = kw.get("sender_wxid", "")
        self.sender = kw.get("sender", "")
        self.attr = kw.get("attr", "friend")
        self.create_time = kw.get("create_time", 1_700_000_000.0)
        self.local_id = kw.get("local_id", 1)
        self.sort_seq = kw.get("sort_seq", 1)


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


def main() -> int:
    adapter = WechatautoHookAdapter(listen_chats=["文件传输助手"])
    adapter._self_wxid = "wxid_me"          # noqa: SLF001 —— 离线自测直接注入自身身份
    ok = True

    ok &= check("构造后状态为未连接", adapter.status().ok is False, adapter.status().detail)

    private_chat = FakeChat("wxid_alice", "Alice")
    group_chat = FakeChat("12345@chatroom", "测试群")

    # 1) 单聊文本
    item = adapter._to_message(FakeMsg(content="明天的会还开不开？", sender_wxid="wxid_alice", sender="Alice"), private_chat)
    ok &= check("单聊文本转换", item is not None and item.text == "明天的会还开不开？", f"chat_id={getattr(item, 'chat_id', None)}")
    ok &= check("单聊 is_group=False", item is not None and item.is_group is False)
    ok &= check("发送者昵称映射", item is not None and item.sender_name == "Alice", getattr(item, "sender_name", ""))

    # 2) 群聊
    g = adapter._to_message(FakeMsg(content="大家好", sender_wxid="wxid_bob", sender="Bob"), group_chat)
    ok &= check("群聊 is_group=True", g is not None and g.is_group is True, getattr(g, "chat_id", ""))

    # 2b) 群消息正文里的 `wxid_xxx:` 前缀必须剥掉，`@我的昵称` 要归一成 `@我`。
    #     真机踩到过：提示词里是「阿甲：wxid_5azem...:\n不像」，模型分不清谁在说话。
    row_msg = adapter._row_to_message({                              # noqa: SLF001
        "username": "12345@chatroom", "sender_username": "wxid_bob",
        "type": "text", "content": "wxid_bob:\n@de 大家好", "create_time": 1_700_000_000.0,
        "local_id": 7, "sort_seq": 7,
    })
    ok &= check("群消息剥掉 wxid 前缀", row_msg is not None and row_msg.text == "@de 大家好",
                getattr(row_msg, "text", "<None>"))
    adapter.set_extra_nicknames(["de"])                              # noqa: SLF001
    row_msg2 = adapter._row_to_message({                             # noqa: SLF001
        "username": "12345@chatroom", "sender_username": "wxid_bob",
        "type": "text", "content": "wxid_bob:\n@de 大家好", "create_time": 1_700_000_000.0,
        "local_id": 8, "sort_seq": 8,
    })
    ok &= check("`@我的昵称` 归一成 `@我`", row_msg2 is not None and row_msg2.text == "@我 大家好",
                getattr(row_msg2, "text", "<None>"))
    plain = adapter._strip_sender_prefix("时间 12:30 见", "wxid_bob")   # noqa: SLF001
    ok &= check("正文里的普通冒号不被误剥", plain == "时间 12:30 见", plain)

    # 3) 自己发的
    mine = adapter._to_message(FakeMsg(content="我自己发的", sender_wxid="wxid_me", attr="self"), private_chat)
    ok &= check("自己发的被标记 is_self", mine is not None and mine.is_self is True)

    # 4) 非文本被丢弃
    img = adapter._to_message(FakeMsg(type="图片", content="[图片]"), private_chat)
    ok &= check("图片消息被丢弃", img is None)

    # 5) 入队 + 去重 + 取空
    adapter._queue.put(item)                                   # noqa: SLF001
    first = adapter.recv()
    second = adapter.recv()                                    # 同一 msg_id 应被去重
    third = adapter.recv()                                     # 队列空
    ok &= check("recv 取到消息", first is not None and first.chat_id == "wxid_alice")
    ok &= check("重复 msg_id 被去重", second is None)
    ok &= check("队列取空返回 None", third is None)

    # 6) 全局监听模式下按 wxid 白名单过滤
    all_adapter = WechatautoHookAdapter(listen_chats=["wxid_alice"], listen_all=True)
    all_adapter._self_wxid = "wxid_me"                          # noqa: SLF001
    kept = all_adapter._to_message(FakeMsg(content="hi", sender_wxid="wxid_alice"), private_chat)
    dropped = all_adapter._to_message(FakeMsg(content="hi", sender_wxid="wxid_carol"), FakeChat("wxid_carol", "Carol"))
    ok &= check("全局监听保留白名单会话", kept is not None)
    ok &= check("全局监听丢弃非白名单会话", dropped is None)

    # 7) 单实例锁：**跨进程**才作数（同进程内重复加锁在 Windows 上不会互斥，
    #    这也是审计里容易被误判成"锁没用"的地方）。所以起一个子进程去抢锁。
    import subprocess

    from wechat_bridge.state import SingleInstanceLock

    lock_path = ROOT / "state" / "_locktest.lock"
    child = ROOT / "state" / "_lockchild.py"
    child.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from pathlib import Path\n"
        "from wechat_bridge.state import SingleInstanceLock\n"
        f"lock = SingleInstanceLock(Path(r'{lock_path}'))\n"
        "print('ACQUIRED' if lock.acquire() else 'REFUSED')\n"
        "import time; time.sleep(6)\n",
        encoding="utf-8",
    )
    first = SingleInstanceLock(lock_path)
    ok &= check("第一个实例拿到锁", first.acquire())
    proc = subprocess.Popen([sys.executable, str(child)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        out, _ = proc.communicate(timeout=20)
        ok &= check("第二个进程被拒绝", "REFUSED" in (out or ""), (out or "").strip())
    except subprocess.TimeoutExpired:
        proc.kill()
        ok &= check("第二个进程被拒绝", False, "子进程超时（可能卡在等待锁上）")
    first.release()
    third = SingleInstanceLock(lock_path)
    ok &= check("释放后可以重新获取", third.acquire() is True)
    third.release()
    for p in (lock_path, child):
        try:
            p.unlink()
        except OSError:
            pass

    # 8) 带 BOM 的 JSON 要能读（记事本改配置/急停开关的现实情况）
    import json as _json

    from wechat_bridge.config import read_json_file

    bom_path = ROOT / "state" / "_bomtest.json"
    bom_path.write_bytes(b"\xef\xbb\xbf" + _json.dumps({"kill_switch": True}, ensure_ascii=False).encode("utf-8"))
    try:
        data = read_json_file(bom_path)
        ok &= check("带 BOM 的 JSON 可解析", data.get("kill_switch") is True)
    except Exception as exc:
        ok &= check("带 BOM 的 JSON 可解析", False, repr(exc))
    try:
        bom_path.unlink()
    except OSError:
        pass

    # 9) 发送能力自检：三条分支都必须给出**正确且是人话**的结论。
    #    为什么要锁住它：这条链路的失败症状极隐蔽——UIA 拿不到时库降级到 OCR，
    #    而 OCR 的 winsdk 在某些环境里是坏的，最终报的是
    #    `ImportError: DLL load failed while importing _winrt`，
    #    **看着完全不像"发不出去"**（实测为此白跑了一整轮真机验证）。
    import types as _types

    probe_adapter = WechatautoHookAdapter(listen_uids=["filehelper"])

    def _pkg(name):
        mod = _types.ModuleType(name)
        mod.__path__ = []
        return mod

    def _probe_with(uia_ok: bool, ocr_ok: bool):
        """在受控的假模块环境下跑一次自检，跑完完整还原 sys.modules。"""
        saved: dict = {}

        def put(name, mod):
            saved.setdefault(name, sys.modules.get(name, "__absent__"))
            sys.modules[name] = mod

        for name in list(sys.modules):
            if name == "winsdk" or name.startswith(("winsdk.", "wechatauto")):
                saved.setdefault(name, sys.modules[name])
                del sys.modules[name]

        class _UIA:
            def ensure_window(self):
                return uia_ok

        uia_mod = _pkg("wechatauto.uia_driver")
        uia_mod.WeChatUIA = _UIA
        put("wechatauto", _pkg("wechatauto"))
        put("wechatauto.uia_driver", uia_mod)
        if ocr_ok:
            ocr = _types.ModuleType("winsdk.windows.media.ocr")
            ocr.OcrEngine = object
            for name in ("winsdk", "winsdk.windows", "winsdk.windows.media"):
                put(name, _pkg(name))
            put("winsdk.windows.media.ocr", ocr)
        else:
            put("winsdk", None)          # None ⇒ 任何 winsdk 导入都 ImportError
        try:
            return probe_adapter._probe_send_capability()
        finally:
            for name, old in saved.items():
                if old == "__absent__":
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old

    can, why = _probe_with(uia_ok=True, ocr_ok=False)
    ok &= check("UIA 可用 → 判定能发送，且说明走主路径",
                can is True and "UIA" in why, f"{can} / {why}")
    can, why = _probe_with(uia_ok=False, ocr_ok=True)
    ok &= check("UIA 不可用但 OCR 可用 → 能发，且**如实说明已降级**（不能假装一切正常）",
                can is True and "OCR" in why, f"{can} / {why}")
    can, why = _probe_with(uia_ok=False, ocr_ok=False)
    ok &= check("UIA 与 OCR 双双不可用 → 判定**不能**发送（真机踩的就是这个组合）",
                can is False and "OCR" in why, f"{can} / {why}")

    # 10) 发送回读复核：库里报"成功"不等于真发出去（成功判据是"输入框看起来空了"这种像素启发式）
    import time as _time

    class SendDB:
        def __init__(self, rows): self.rows = rows
        def get_messages(self, user, limit=5): return self.rows[:limit]

    class SendWx:
        def __init__(self, rows): self._db = SendDB(rows)

    class SendChat:
        who = "filehelper"
        # 假对象必须与**真实** Chat.SendMsg 的签名一致（它有 at= 参数，用于群聊 @ 人）——
        # 少一个参数不会让测试失败得"早"，只会让它在别处莫名其妙地红。
        def SendMsg(self, text, at=None): return {"status": "成功", "message": "ok"}

    verify_adapter = WechatautoHookAdapter(listen_uids=["filehelper"], verify_window_sec=20)
    verify_adapter._self_wxid = "wxid_me"                     # noqa: SLF001
    verify_adapter._chat_cache["filehelper"] = SendChat()     # noqa: SLF001
    now = _time.time()
    verify_cases = [
        ("库里有刚发的这条 → 算送达", [{"content": "你好呀", "sender_username": "wxid_me", "create_time": now - 3}], True),
        ("库里查不到 → 判未送达", [{"content": "别的", "sender_username": "wxid_other", "create_time": now - 3}], False),
        ("只有很久以前的同文本 → 不算", [{"content": "你好呀", "sender_username": "wxid_me", "create_time": now - 600}], False),
    ]
    for label, rows, want in verify_cases:
        verify_adapter._wx = SendWx(rows)                     # noqa: SLF001
        ok &= check(label, verify_adapter.send("filehelper", "你好呀") is want)

    class BrokenDB:
        def get_messages(self, user, limit=5): raise RuntimeError("db locked")

    verify_adapter._wx = type("W", (), {"_db": BrokenDB()})()  # noqa: SLF001
    ok &= check("数据库读不了时按已发送处理（复核不能反过来误判）",
                verify_adapter.send("filehelper", "你好呀") is True)

    # 11) 自己实现的 @（KI-1 的改法 A + B）。
    #     这三条是**真机踩出来的教训**，用假 GUI 锁住它们，别让它再退回去：
    #       ① 只导航一次——@ 失败后补发**不能**再 open_chat（用户看到的就是搜两次群）；
    #       ② 弹层要"等"、匹配要能容忍 OCR 抖动（库里死睡 0.9 秒 + 单判据，真机就是这么漏的）；
    #       ③ 补正文**不能清空**输入框——`input_text()` 开头的 Ctrl+A→Delete
    #          会把刚选中的 @ 一起删掉，等于白选。
    class FakeInput:
        def __init__(self, gui):
            self.gui = gui

        def type_unicode(self, text):
            self.gui.typed.append(text)

        def key(self, vk, ctrl=False):
            self.gui.calls.append(("key", vk, ctrl))
            if vk == 0x41 and ctrl:          # Ctrl+A ⇒ 后面的清除动作
                self.gui.cleared += 1

    class FakeGui:
        def __init__(self, current, ocr_script):
            self.calls = []
            self.typed = []
            self.cleared = 0
            self.sent = []
            self.clipboard = ""
            self.ocr_script = list(ocr_script)
            self._current_chat = current
            self.render_w, self.render_h = 1280, 800
            self.right_pane_left = 300
            self.origin_x, self.origin_y = 100, 50
            self._last_input_box = None
            self._pending_text = None
            self._input = FakeInput(self)

        def ensure_visible(self):
            self.calls.append(("ensure_visible",))
            return True

        def _get_uia(self):
            return None

        def get_input_box(self):
            return (300, 600, 1200, 720)

        def focus_input(self, box=None):
            self.calls.append(("focus_input",))
            return True

        def ocr(self, rel):
            return self.ocr_script.pop(0) if self.ocr_script else []

        def wx_click(self, x, y):
            self.calls.append(("click", x, y))

        def open_chat(self, who):
            self.calls.append(("open_chat", who))
            self._current_chat = who
            return True

        def set_clipboard(self, text):
            self.clipboard = text
            return True

        def _input_box_has_text(self, box=None):
            return True

        def click_send(self):
            self.calls.append(("click_send",))
            self.sent.append(self.clipboard)
            return True

        def input_text(self, text, box=None, fast=False):
            self.calls.append(("input_text", text))
            self.sent.append(text)
            return True

    def _n(gui, name):
        return sum(1 for call in gui.calls if call[0] == name)

    class AtChat:
        who = "测试群"

        def __init__(self):
            self.called = False

        def SendMsg(self, text, at=None):
            self.called = True
            return {"status": "成功", "message": "ok"}

    def _at_adapter(gui):
        adapter = WechatautoHookAdapter(listen_uids=["x@chatroom"], verify_send=False)
        adapter._ocr_ok = True                                   # noqa: SLF001
        adapter.at_popup_timeout = 0.05                          # 别让自测等真 3 秒
        adapter.at_popup_gone_timeout = 0.05
        adapter._self_wxid = "wxid_me"                           # noqa: SLF001
        adapter._wx = type("W", (), {"_gui": gui})()             # noqa: SLF001
        return adapter

    # 11a 正常路径：已在目标群、弹层一次命中
    gui = FakeGui("测试群", [[("阿甲", 340, 400, 80, 24)]])
    resp = _at_adapter(gui)._send_at(AtChat(), "收到", "阿甲")   # noqa: SLF001
    ok &= check("11a @ 成功", resp.get("status") == "成功", str(resp))
    ok &= check("11a 已在目标会话 ⇒ 不重复导航", _n(gui, "open_chat") == 0, str(gui.calls))
    ok &= check("11a 先敲 @", gui.typed[:1] == ["@"], str(gui.typed))
    ok &= check("11a 点中成员一次", _n(gui, "click") == 1, str(gui.calls))
    ok &= check("11a 正文用 Ctrl+V 补上", any(c[0] == "key" and c[1] == 0x56 and c[2] for c in gui.calls))
    ok &= check("11a 补正文前没清空输入框（否则 @ 会被删掉）", gui.cleared == 0, f"cleared={gui.cleared}")
    ok &= check("11a 正文真的发出去了", gui.sent == ["收到"], str(gui.sent))

    # 11b 首轮弹层里没有目标 ⇒ 键入名字前缀过滤后再匹配（库里没有这一步，是真机漏匹配的主因之一）
    gui = FakeGui("测试群", [[("张三", 340, 400, 80, 24)], [("阿甲", 340, 410, 80, 24)]])
    resp = _at_adapter(gui)._send_at(AtChat(), "收到", "阿甲")   # noqa: SLF001
    ok &= check("11b 首轮没命中 → 键入前缀过滤后命中", resp.get("status") == "成功" and "史莱" in gui.typed,
                str(gui.typed))

    # 11c OCR 抖动：读成带空格 / 截断的昵称也要能匹配上
    for label, read in (("带空格", "史 莱 姆"), ("截断", "史莱")):
        gui = FakeGui("测试群", [[(read, 340, 400, 80, 24)]])
        resp = _at_adapter(gui)._send_at(AtChat(), "收到", "阿甲")   # noqa: SLF001
        ok &= check(f"11c OCR 把昵称读成{label}也能匹配", resp.get("status") == "成功", f"{read!r} → {resp}")

    # 11d 匹配不上：判失败、交回 _chat_open、且清掉残留的 @/过滤词（别把「@史」当正文发出去）
    gui = FakeGui("测试群", [[("张三", 340, 400, 80, 24)], [("张三", 340, 400, 80, 24)]])
    resp = _at_adapter(gui)._send_at(AtChat(), "收到", "阿甲")   # noqa: SLF001
    ok &= check("11d 匹配不上 → 失败但会话状态交回上层",
                resp.get("status") == "失败" and resp.get("_chat_open") is True, str(resp))
    ok &= check("11d 失败时清空输入框里的 @ / 过滤词", gui.cleared >= 1, f"cleared={gui.cleared}")

    # 11e 整体链路：@ 失败 → **原地**补发，全程只导航一次（这是 KI-1 的核心回归）
    gui = FakeGui("", [[("张三", 340, 400, 80, 24)], [("张三", 340, 400, 80, 24)]])
    adapter_at = _at_adapter(gui)
    chat_at = AtChat()
    adapter_at._chat_cache["x@chatroom"] = chat_at             # noqa: SLF001
    ok &= check("11e @ 失败后原地补发成功", adapter_at.send("x@chatroom", "收到", at="阿甲") is True)
    ok &= check("11e 全程只导航一次（不再搜第二次群）", _n(gui, "open_chat") == 1, str(gui.calls))
    ok &= check("11e 补发走 input_text（就地），没走 chat.SendMsg",
                _n(gui, "input_text") == 1 and not chat_at.called, str(gui.calls))

    # 11f 不带 @ 的普通发送仍然走库的 SendMsg（别把主路径改坏）
    gui = FakeGui("测试群", [])
    adapter_plain = _at_adapter(gui)
    chat_plain = AtChat()
    adapter_plain._chat_cache["x@chatroom"] = chat_plain       # noqa: SLF001
    ok &= check("11f 普通发送仍走 chat.SendMsg", adapter_plain.send("x@chatroom", "嗯") is True and chat_plain.called)

    # 12) 后台发送接线：会话已打开就走后台（不动鼠标），没打开才退回 GUI。
    #     这是「全面后台化」的第一步，接线错了会**静默退回老路径**——
    #     用户感受到的就是"还是会被打扰"，但日志里什么都看不出来。
    from bg_selftest import FakeUIA as _BgFakeUIA
    from wechat_bridge.bg_send import BackgroundSender as _BgSender

    class _BgGui:
        def __init__(self, uia):
            self.uia = uia

        def _get_uia(self):
            return self.uia

    def _bg_adapter(uia, mode="auto"):
        adapter = WechatautoHookAdapter(listen_uids=["x@chatroom"], verify_send=False,
                                        send_mode=mode)
        adapter._ocr_ok = True                                  # noqa: SLF001
        adapter._wx = type("W", (), {"_gui": _BgGui(uia), "_db": None})()   # noqa: SLF001
        sender = _BgSender(uia, type("W", (), {"NativeWindowHandle": 777})())
        sender._user32 = uia.user32                             # noqa: SLF001 —— 模拟回车真的发出去了
        adapter._bg = sender                                    # noqa: SLF001
        # 离线自测不该去枚举真实桌面窗口：把缓存"刚刷新过"占住
        adapter._bg_windows = {}                                # noqa: SLF001
        adapter._bg_windows_at = _time.time()                   # noqa: SLF001
        adapter.bg_idle_gate_sec = 0.0                          # 自测不等用户空闲
        chat = AtChat()
        adapter._chat_cache["x@chatroom"] = chat                # noqa: SLF001
        return adapter, chat

    # 12a 会话已打开 ⇒ 后台发送，一次都不碰 GUI
    uia = _BgFakeUIA(chat="测试群")
    adapter_bg, chat_bg = _bg_adapter(uia)
    ok &= check("12a 会话已打开 → 走后台发送", adapter_bg.send("x@chatroom", "收到") is True)
    ok &= check("12a 全程没走 chat.SendMsg（不动鼠标）", chat_bg.called is False)
    ok &= check("12a 正文确实是 SetValue 写进去的", uia.store["sets"] == ["收到"], str(uia.store["sets"]))
    ok &= check("12a 计入 sent_bg", adapter_bg._stats.get("sent_bg") == 1, str(adapter_bg._stats))  # noqa: SLF001

    # 12b 会话没打开 ⇒ 不硬发，退回 GUI 去打开（auto 模式）
    uia = _BgFakeUIA(chat="别的群")
    adapter_bg, chat_bg = _bg_adapter(uia)
    ok &= check("12b 会话没打开 → 退回 GUI 且发送成功",
                adapter_bg.send("x@chatroom", "收到") is True and chat_bg.called is True)
    ok &= check("12b 没有往错的会话里写", uia.store["sets"] == [], str(uia.store["sets"]))

    # 12c background 模式：会话没打开就**不发**（不打扰优先，绝不退回 GUI）
    uia = _BgFakeUIA(chat="别的群")
    adapter_bg, chat_bg = _bg_adapter(uia, mode="background")
    ok &= check("12c background 模式拒绝退回 GUI", adapter_bg.send("x@chatroom", "收到") is False,
                str(chat_bg.called))
    ok &= check("12c 拒绝时确实没调 chat.SendMsg", chat_bg.called is False)

    # 12d 会话已打开但输入框里有草稿 ⇒ 后台拒发，auto 模式退回 GUI（消息不能丢）
    uia = _BgFakeUIA(chat="测试群", value="用户正在打的草稿")
    adapter_bg, chat_bg = _bg_adapter(uia)
    ok &= check("12d 有草稿 → 后台不发、但消息最终仍发出去了",
                adapter_bg.send("x@chatroom", "收到") is True and chat_bg.called is True)
    ok &= check("12d 草稿没被覆盖", uia.store["value"] == "用户正在打的草稿", uia.store["value"])

    # 12e 主窗口开着别的会话，但目标有**自己的独立窗口** ⇒ 走独立窗口后台发送。
    #     这是"全面后台化"的关键：微信 4.x 的独立会话窗口（mmui::ChatSingleWindow）
    #     自带 chat_input_field，于是"切会话"被彻底绕开。
    uia_main = _BgFakeUIA(chat="别的群")
    adapter_bg, chat_bg = _bg_adapter(uia_main)
    uia_det = _BgFakeUIA(chat="测试群")
    det = _BgSender(uia_det, type("W", (), {"NativeWindowHandle": 999})(), chat_name="测试群")
    det._user32 = uia_det.user32                                # noqa: SLF001
    adapter_bg._bg_windows = {"测试群": 999}                    # noqa: SLF001
    adapter_bg._bg_windows_at = _time.time()                    # noqa: SLF001
    adapter_bg._bg_window_senders[999] = det                    # noqa: SLF001
    ok &= check("12e 有独立窗口 → 后台发送成功，不碰 GUI",
                adapter_bg.send("x@chatroom", "收到") is True and chat_bg.called is False)
    ok &= check("12e 发进的是独立窗口", uia_det.store["sets"] == ["收到"], str(uia_det.store["sets"]))
    ok &= check("12e 主窗口一个字都没被写", uia_main.store["sets"] == [], str(uia_main.store["sets"]))

    # 12f 目标既不在主窗口、也没有独立窗口 ⇒ auto 模式退回 GUI
    uia = _BgFakeUIA(chat="别的群")
    adapter_bg, chat_bg = _bg_adapter(uia)
    ok &= check("12f 没有可用窗口 → 退回 GUI 且消息发出去了",
                adapter_bg.send("x@chatroom", "收到") is True and chat_bg.called is True)

    # 12g 用户正在打字时先等一等（微信发送会抬窗口，不能在人敲键盘的时候插进去）
    _orig_idle = _BgSender.user_idle_sec
    try:
        uia = _BgFakeUIA(chat="测试群")
        adapter_bg, chat_bg = _bg_adapter(uia)
        adapter_bg.bg_idle_gate_sec = 0.8
        adapter_bg.bg_idle_wait_sec = 0.2
        _BgSender.user_idle_sec = staticmethod(lambda: 99.0)     # 用户早就没动了
        ok &= check("12g 用户空闲 → 立刻发送",
                    adapter_bg.send("x@chatroom", "收到") is True and uia.store["sets"] == ["收到"])
        uia2 = _BgFakeUIA(chat="测试群")
        adapter_bg2, chat_bg2 = _bg_adapter(uia2)
        adapter_bg2.bg_idle_gate_sec = 0.8
        adapter_bg2.bg_idle_wait_sec = 0.15
        _BgSender.user_idle_sec = staticmethod(lambda: 0.0)      # 用户一直在敲
        ok &= check("12g 用户一直在操作 → 等不到也照发（消息不能压着）",
                    adapter_bg2.send("x@chatroom", "收到") is True)
    finally:
        _BgSender.user_idle_sec = _orig_idle

    # 12h-12j 发送失败后必须清掉**留在输入框里的那半句**。
    #   真机 2026-10-18 出现两次（同一形态）：
    #     20:48 发送失败 → 20:56 群里连发三遍「哈哈别动手…」
    #     22:39 发送失败 → 22:47 群里连发三遍「小升初？」
    #   都是 GUI 发送失败 → 那半句留在输入框 → 7 分钟后库自己补回车把它冲出去，
    #   而且**完全不走 send()**，日志/审计/限速/@ 清洗全绕过。
    def _boom_timeout(*a, **kw):
        raise TimeoutError("模拟 GUI 发送超时")

    uia = _BgFakeUIA(chat="测试群", value="小升初？")      # 框里就是刚才没发出去的那句
    adapter_lo, chat_lo = _bg_adapter(uia, mode="gui")
    adapter_lo._gui_is_wechat = lambda: True                # noqa: SLF001
    adapter_lo._send_with_timeout = _boom_timeout           # noqa: SLF001
    ok &= check("12h 发送超时判失败", adapter_lo.send("x@chatroom", "小升初？") is False)
    ok &= check("12h 残留被清空（不清就会在 7 分钟后自己发出去）",
                uia.store["value"] == "", repr(uia.store["value"]))
    ok &= check("12h 记进 leftover_cleared",
                adapter_lo._stats.get("leftover_cleared") == 1, str(adapter_lo._stats))  # noqa: SLF001

    # 12i 框里是**用户自己在打的草稿** ⇒ 一个字都不许动（这是安全底线）
    uia = _BgFakeUIA(chat="测试群", value="我正在打的一段话")
    adapter_dr, _chat_dr = _bg_adapter(uia, mode="gui")
    adapter_dr._gui_is_wechat = lambda: True                # noqa: SLF001
    adapter_dr._send_with_timeout = _boom_timeout           # noqa: SLF001
    adapter_dr.send("x@chatroom", "小升初？")
    ok &= check("12i 用户草稿没被动过",
                uia.store["value"] == "我正在打的一段话", repr(uia.store["value"]))

    # 12j 读不到输入框内容 ⇒ 不赌，记下来下次发送前再清
    uia = _BgFakeUIA(chat="测试群", value="小升初？", readable=False)
    adapter_ur, _chat_ur = _bg_adapter(uia, mode="gui")
    adapter_ur._gui_is_wechat = lambda: True                # noqa: SLF001
    adapter_ur._send_with_timeout = _boom_timeout           # noqa: SLF001
    adapter_ur.send("x@chatroom", "小升初？")
    ok &= check("12j 读不到时记进待清列表（下次发送前再清）",
                adapter_ur._leftovers == ["小升初？"], str(adapter_ur._leftovers))   # noqa: SLF001

    # 12k/12l 超时后必须**松开鼠标键**。
    #   2026-10-19 真机：每次发送超时之后，鼠标光标偶尔会滑动，把 DSH 网页里的文字
    #   整片选中变蓝（一上午三次）。根因是 GUI 发送「DOWN…UP」中间被 20 秒超时打断，
    #   系统以为左键一直按着 ⇒ 之后任何光标移动都成了拖拽选择。
    import sys as _sys
    import types as _types

    _mouse_calls = []

    def _fake_api(pressed_vk, sink):
        return _types.SimpleNamespace(
            GetAsyncKeyState=lambda vk: (0x8000 if vk == pressed_vk else 0),
            mouse_event=lambda flag, dx, dy, data, extra: sink.append(flag))

    _saved_api = _sys.modules.get("win32api")
    _sys.modules["win32api"] = _fake_api(0x01, _mouse_calls)
    try:
        uia = _BgFakeUIA(chat="测试群")
        adapter_mouse, _chat_mouse = _bg_adapter(uia)
        released = adapter_mouse._release_stuck_mouse("自测")          # noqa: SLF001
    finally:
        if _saved_api is None:
            _sys.modules.pop("win32api", None)
        else:
            _sys.modules["win32api"] = _saved_api
    ok &= check("12k 左键卡住时会被松开（0x0004 = LEFTUP）",
                released is True and _mouse_calls == [0x0004], str(_mouse_calls))

    _mouse_calls.clear()
    _sys.modules["win32api"] = _fake_api(0, _mouse_calls)          # 什么都没按
    try:
        released2 = adapter_mouse._release_stuck_mouse("自测")        # noqa: SLF001
    finally:
        if _saved_api is None:
            _sys.modules.pop("win32api", None)
        else:
            _sys.modules["win32api"] = _saved_api
    ok &= check("12l 没按住就绝不动鼠标", released2 is False and _mouse_calls == [],
                str(_mouse_calls))

    # 12m/12n 失败后**读不到**输入框时，后台看门狗必须继续盯着并最终清掉。
    #   2026-10-19 真机：0.1.1 的做法是"记下来，下次发送前再清"——可那次失败之后
    #   整个下午都没有下一次发送，于是没机会清，8 分钟后库自己补回车，群里三连。
    #   所以清理不能只挂在"下一次发送"上。
    import time as _t

    uia = _BgFakeUIA(chat="测试群", value="我是住这号里的 AI")
    uia.readable = False                      # 失败那一刻读不到（真机最常见的形态）
    adapter_wd, _chat_wd = _bg_adapter(uia)
    adapter_wd._schedule_leftover_cleanup("我是住这号里的 AI", seconds=8, interval=0.15)  # noqa: SLF001
    _t.sleep(0.5)
    ok &= check("12m 读不到时不会瞎清（宁可留着）",
                uia.store["value"] == "我是住这号里的 AI", repr(uia.store["value"]))
    adapter_wd._sending = True                 # 正在发别的消息：看门狗必须让路  # noqa: SLF001
    uia.readable = True
    _t.sleep(0.6)
    ok &= check("12m 它自己在发消息时，看门狗不插手（否则会把正文清掉）",
                uia.store["value"] == "我是住这号里的 AI", repr(uia.store["value"]))
    adapter_wd._sending = False                # noqa: SLF001
    _t.sleep(1.2)
    ok &= check("12n 一旦框可读且没人正在发，看门狗把残留清掉",
                uia.store["value"] == "", repr(uia.store["value"]))

    # 12o-12q 微信「引用」消息：必须能看懂，而且**引用我的消息＝在跟我说话**。
    #   2026-10-19 真机：用户引用机器人上一句再说话，机器人完全不回——因为引用是
    #   appmsg(type=57)，在库里被归成「文件/链接/卡片」，按类型名判会整类丢掉，
    #   连 received 都不涨（一条日志都没有）。
    from wechat_bridge.adapters.hook_wechatauto import parse_quote as _parse_quote

    _quote_xml = ("wxid_alice:<?xml version=\"1.0\"?><msg><appmsg><title>你还内涵上我了是吧</title>"
                  "<type>57</type><refermsg><type>1</type><chatusr>wxid_me</chatusr>"
                  "<displayname>DeepSeek</displayname><content>地主杀不完，你也逃课逃不完</content>"
                  "</refermsg></appmsg></msg>")
    _q = _parse_quote(_quote_xml)
    ok &= check("12o 能解析出引用块", _q is not None and _q["quoted_name"] == "DeepSeek", str(_q))
    ok &= check("12o 正文带上被引用的原话（否则模型答非所问）",
                _q is not None and "地主杀不完" in _q["text"] and "你还内涵上我了是吧" in _q["text"],
                (_q or {}).get("text", ""))
    ok &= check("12o 非引用消息不会被误判",
                _parse_quote("普通一句话") is None
                and _parse_quote('<msg><appmsg><title>文件.pdf</title></appmsg></msg>') is None)

    uia = _BgFakeUIA(chat="测试群")
    adapter_q, _chat_q = _bg_adapter(uia)
    adapter_q._self_wxid = "wxid_me"                      # noqa: SLF001
    _row = {"username": "x@chatroom", "type": "文件/链接/卡片", "content": _quote_xml,
            "sender_username": "wxid_alice", "local_id": 458, "create_time": _time.time()}
    _msg = adapter_q._row_to_message(_row)                # noqa: SLF001
    ok &= check("12p 引用消息不再被丢掉（能变成 Message）",
                _msg is not None and _msg.kind == "text", str(_msg and _msg.kind))
    ok &= check("12p 引用我的消息 → mentions_me=True（不用再额外 @ 一次）",
                _msg is not None and _msg.mentions_me is True,
                str(_msg and (_msg.mentions_me, _msg.at_note)))
    _file_row = {"username": "x@chatroom", "type": "文件/链接/卡片",
                 "content": '<msg><appmsg><title>合同.pdf</title></appmsg></msg>',
                 "sender_username": "wxid_alice", "local_id": 459, "create_time": _time.time()}
    ok &= check("12q 普通文件/链接仍然不接（别把人工该看的东西塞给模型）",
                adapter_q._row_to_message(_file_row) is None)   # noqa: SLF001

    # 12r 引用的**不是**我：正文照样要带上原话（模型才看得懂语境），
    #     但 `mentions_me` 必须保持 False —— "别人之间的对话"由说话决策层负责闭嘴，
    #     适配器不能替它决定要不要回。结构化信息也要留在 Message.quote 上。
    _quote_other = ("wxid_bob:<?xml version=\"1.0\"?><msg><appmsg><title>你算什么东西</title>"
                    "<type>57</type><refermsg><type>1</type><chatusr>wxid_alice</chatusr>"
                    "<displayname>阿甲</displayname><content>那事你别管</content>"
                    "</refermsg></appmsg></msg>")
    _row_other = {"username": "x@chatroom", "type": "文件/链接/卡片", "content": _quote_other,
                  "sender_username": "wxid_bob", "local_id": 460, "create_time": _time.time()}
    _msg_other = adapter_q._row_to_message(_row_other)      # noqa: SLF001
    ok &= check("12r 引用别人的话：mentions_me 仍为 False（回不回由判定层定）",
                _msg_other is not None and _msg_other.mentions_me is False,
                str(_msg_other and (_msg_other.mentions_me, _msg_other.at_note)))
    ok &= check("12r 但引用信息与原文都要留下（否则模型看不懂在回哪句）",
                _msg_other is not None
                and (_msg_other.quote or {}).get("quoted_name") == "阿甲"
                and "那事你别管" in str(_msg_other.text)
                and (_msg or {}).quote is not None
                and (_msg.quote or {}).get("quoted_name") == "DeepSeek",
                str(_msg_other and (_msg_other.quote, _msg_other.text)))

    # 13) GUI 发送前必须核对「GUI 引擎绑的是不是微信窗口」。
    #     真机踩到过：桥接启动时 WeChatGUI 把前台窗口（DSH 的浏览器）当成了微信，
    #     这条路径一旦真被用到，它会把字打进那个窗口里。
    class _GuiWrongWindow:
        hwnd = 1                      # 无效句柄 ⇒ GetClassName 拿不到 "Qt…"

        def _get_uia(self):
            return None

    uia_none = _BgFakeUIA(chat="别的群")
    adapter_guard = WechatautoHookAdapter(listen_uids=["x@chatroom"], verify_send=False,
                                           send_mode="gui")
    adapter_guard._ocr_ok = True                                # noqa: SLF001
    adapter_guard._wx = type("W", (), {"_gui": _GuiWrongWindow(), "_db": None})()   # noqa: SLF001
    chat_guard = AtChat()
    adapter_guard._chat_cache["x@chatroom"] = chat_guard         # noqa: SLF001
    ok &= check("13 浏览器窗口类名不算微信",
                adapter_guard._window_looks_like_wechat("Chrome_WidgetWin_1") is False)  # noqa: SLF001
    ok &= check("13 Qt 窗口类名算微信",
                adapter_guard._window_looks_like_wechat("Qt51514QWindowIcon") is True)   # noqa: SLF001
    # 端到端：核对不通过时必须**真的**被拦住（不能只是有个判断函数没人调）
    adapter_guard._gui_is_wechat = lambda: False                # noqa: SLF001
    ok &= check("13 GUI 绑错窗口时拒绝发送（不往别的窗口打字）",
                adapter_guard.send("x@chatroom", "收到") is False and chat_guard.called is False)

    # 14) 自愈判据：**安静 ≠ 死**。
    #     真机踩到（2026-10-18 19:44）：群里 15 分钟没人说话，自愈就把连接重建了一次，
    #     而重连要重新初始化 GUI + 解密数据库——正是最容易抢焦点、闪一下的动作。
    #     新判据要求**正向证据**：库里有比"我们收到过的"更新的消息，却没回调到我们。
    class _AliveListener:
        class _Thread:
            @staticmethod
            def is_alive():
                return True

        _thread = _Thread()

    class _QuietDB:
        """库里最新一条比我们收到的**旧**（=这段时间就是没人说话）。"""

        def __init__(self, newest):
            self.newest = newest

        def get_messages(self, chat, limit=1):
            return [{"create_time": self.newest, "content": "旧消息"}]

    adapter_health = WechatautoHookAdapter(listen_uids=["g@chatroom"], stale_after_sec=900)
    adapter_health._self_wxid = "wxid_me"                       # noqa: SLF001
    adapter_health._seen_chats["g@chatroom"] = 1000.0           # noqa: SLF001
    adapter_health._last_seen_ts = 1000.0                       # noqa: SLF001
    adapter_health._last_recv_ts = time.time() - 1000           # noqa: SLF001 超过 900 秒没消息

    def _wire(db):
        adapter_health._wx = type("W", (), {"_listener": _AliveListener(), "_db": db})()   # noqa: SLF001
        adapter_health._probe_at = 0.0                          # noqa: SLF001 清掉探测缓存

    _wire(_QuietDB(900.0))
    st = adapter_health.status()
    ok &= check("14 只是没人说话 ⇒ 判健康（不再白重连）", st.ok is True, st.detail)

    _wire(_QuietDB(2000.0))                                     # 库里有更新的消息却没回调
    st = adapter_health.status()
    ok &= check("14 库里有更新的消息却没收到 ⇒ 判失效（该重连）", st.ok is False, st.detail)

    # 静默超过 3 倍阈值：连库都问不出来时按失效处理，避免永远不自愈
    adapter_health._last_recv_ts = time.time() - 3000           # noqa: SLF001
    adapter_health._probe_at = 0.0                              # noqa: SLF001
    _wire(type("DeadDB", (), {"get_messages": lambda self, chat, limit=1: []})())
    st = adapter_health.status()
    ok &= check("14 静默过久（>3 倍阈值）⇒ 兜底判失效", st.ok is False, st.detail)

    # 15) 表情包（动画表情）必须**看得见**。
    #     真机踩过的同类坑：图片在适配器层被整类丢掉 ⇒ 桥接完全不知道有这回事，
    #     表现就是"对方甩了个表情、它装没看见"。表情包只要"知道有这回事"，不用取图。
    sticker_msg = FakeMsg(type="动画表情", sender_wxid="wxid_bob", sender="Bob")
    st = adapter._to_message(sticker_msg, group_chat)                 # noqa: SLF001
    ok &= check("15 群里的动画表情被放行", st is not None and st.kind == "sticker",
                f"kind={getattr(st, 'kind', None)}")
    ok &= check("15 正文是「[表情包]」占位（不是空串）",
                st is not None and st.text == "[表情包]", getattr(st, "text", ""))

    # ⚠ 库里有两套类型名：消息行 dict 里是中文「动画表情」，而 Message 对象上是
    # `EmojiMessage.type = 'emotion'`。真机就栽在这：只认中文名 ⇒ 监听回调那条路
    # 永远匹配不上 ⇒ 表情包静默消失（心跳不涨、审计无行、stderr 也没有）。
    st_emoji = adapter._to_message(FakeMsg(type="emotion"), group_chat)   # noqa: SLF001
    ok &= check("15 Message 对象的 type='emotion' 也要认（两套命名）",
                st_emoji is not None and st_emoji.kind == "sticker",
                f"kind={getattr(st_emoji, 'kind', None)}")

    sticker_row = {"username": "12345@chatroom", "type": "动画表情",
                   "content": "", "sender_username": "wxid_bob",
                   "create_time": 1_700_000_000.0, "local_id": 9}
    st_row = adapter._row_to_message(sticker_row)                     # noqa: SLF001
    ok &= check("15 监听回调那条路也放行（两条路径必须一致）",
                st_row is not None and st_row.kind == "sticker" and st_row.text == "[表情包]",
                f"kind={getattr(st_row, 'kind', None)} text={getattr(st_row, 'text', None)}")

    quiet = WechatautoHookAdapter(listen_uids=["12345@chatroom"], accept_stickers=False)
    quiet._self_wxid = "wxid_me"                                      # noqa: SLF001
    ok &= check("15 accept_stickers=false 时仍然丢掉（开关真的有效）",
                quiet._to_message(FakeMsg(type="动画表情"), group_chat) is None)   # noqa: SLF001
    ok &= check("15 语音/文件等其它类型照旧丢掉",
                adapter._to_message(FakeMsg(type="语音"), group_chat) is None)      # noqa: SLF001

    print(f"\n结论：{'适配器离线自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
