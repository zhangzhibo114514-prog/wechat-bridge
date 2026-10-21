# -*- coding: utf-8 -*-
"""后台发送：不抢焦点、不动鼠标地把消息发出去。

为什么会有这个模块
------------------
原来的发送走「桌面操控」：`ensure_window()` 把微信拉到前台、`_click_ctrl()` 真的移动
鼠标、`Ctrl+V` / `Enter` 真的敲键盘。用户在用电脑时会被打断——这是必须迈过去的坎。

2026-10-17 实测（微信 4.1.15.13，见 DESIGN-BACKGROUND.md）确认了三条：

1. 聊天输入框 `chat_input_field`（`mmui::ChatInputField`）**暴露 UIA ValuePattern**，
   `SetValue` 能把文本写进去 —— 不需要焦点，也不发任何按键；
2. 给输入框 `SetFocus()` 之后，向主窗口 `PostMessage(WM_KEYDOWN/WM_KEYUP, VK_RETURN)`
   **真的能把消息发出去**（实测输入框被清空、数据库里查得到这条消息）；
3. **窗口最小化时同样有效**，而且前台窗口全程不变 —— 用户在别处打字完全不受影响。

所以「输入 + 发送」这两步已经可以完全后台化。**还没解决的是切会话**：
微信 4.x 的会话列表在 UIA 里不暴露行（左栏是自绘的单个 `mmui::XView`），
`Ctrl+F` 这类全局快捷键也吃不掉 PostMessage（Qt 只把消息路由给有焦点的控件）。
⇒ 本模块只负责「目标会话**已经打开**时怎么发」，切会话仍由上层/GUI 层处理。

安全规则（一条都不能少）
------------------------
- **输入框里已经有内容就不发**：那可能是用户正在打的草稿。`SetValue` 会整体覆盖，
  为了我们自己发一条回复而毁掉用户没发出去的话，比"晚回一条消息"严重得多。
- **只发纯文本**：不做任何光标移动、不插入表情、不改选区。
- 发完**回读输入框**确认它空了；没空就当失败，并把内容清掉（不留下半截状态）。

用法
----
    from wechat_bridge.bg_send import BackgroundSender
    sender = BackgroundSender.attach(uia)          # uia = WeChatUIA()
    if sender and sender.current_chat() == "测试群":
        ok, why = sender.send("收到")
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any, Optional, Tuple

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_RETURN = 0x0D

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000


class BackgroundSender:
    """把文本写进**当前已打开会话**的输入框并用消息方式回车发送。"""

    def __init__(self, uia: Any, win: Any, chat_name: str = "") -> None:
        self._uia = uia
        self._win = win
        self._hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
        self._user32 = ctypes.windll.user32
        # 发送期间临时给窗口加 WS_EX_NOACTIVATE，发完原样还回去
        self._saved_exstyle: Optional[int] = None
        # 还原样式前多按住多久：微信的抬窗是异步的（实测按住 4 秒再放开，
        # 它就不再补抬），所以用一个定时器异步还原，不阻塞发送流程。
        self.no_activate_hold = 4.0
        self._na_lock = threading.Lock()
        self._na_timer: Optional[threading.Timer] = None
        # 独立会话窗口（`mmui::ChatSingleWindow`）的标题就是会话名；
        # 主窗口没有这个属性，只能靠输入框的 Name 反推当前会话。
        self.chat_name = str(chat_name or "")

    # ---------------------------------------------------------------- 生命周期

    @classmethod
    def attach(cls, uia: Any) -> Optional["BackgroundSender"]:
        """绑定**主窗口**。拿不到（没登录/窗口没起来）返回 None。"""
        if uia is None:
            return None
        try:
            uia.ensure_materialized(timeout=6.0)
            win = uia._find_main()                       # noqa: SLF001
        except Exception:
            return None
        if win is None:
            return None
        sender = cls(uia, win)
        if not sender._hwnd:
            return None
        return sender

    @classmethod
    def attach_window(cls, uia: Any, hwnd: int, chat_name: str = "") -> Optional["BackgroundSender"]:
        """绑定一个**指定的窗口**（用于微信的「独立会话窗口」）。

        微信 4.x 把会话拖出去会得到 `mmui::ChatSingleWindow`，它自带
        `chat_input_field`（2026-10-17 实测：`ValuePattern` 可写）。于是"切会话"
        这件事可以完全绕过——一个会话一个窗口，按标题对号入座即可。

        ⚠ `ControlFromHandle` 会偶发抛 `COMError(-2147220991, '事件无法调用任何订户')`。
        调用方**不要**因为一次失败就退回 GUI（那会把窗口弹出来），重试几次。
        """
        if uia is None or not hwnd:
            return None
        try:
            import uiautomation as auto
            win = auto.ControlFromHandle(int(hwnd))
        except Exception:
            return None
        if win is None:
            return None
        sender = cls(uia, win, chat_name=chat_name)
        sender._cleanup_leftover_noactivate()      # noqa: SLF001
        return sender

    @classmethod
    def attach_control(cls, uia: Any, ctrl: Any, chat_name: str = "") -> Optional["BackgroundSender"]:
        """直接用**已经拿到的控件对象**绑定（避免二次 `ControlFromHandle` 失败）。

        扫描窗口时本来就已经 `ControlFromHandle` 过一次了，把它留着直接用，
        就少了一次会偶发 COM 错误的调用——这条路径的稳定性就靠这个。
        """
        if uia is None or ctrl is None:
            return None
        sender = cls(uia, ctrl, chat_name=chat_name)
        sender._cleanup_leftover_noactivate()      # noqa: SLF001
        return sender

    @property
    def hwnd(self) -> int:
        return self._hwnd

    # ---------------------------------------------------------------- 只读探测

    def current_chat(self) -> str:
        """这个发送器对应哪个会话。独立窗口直接用标题；主窗口问 UIA。"""
        if self.chat_name:
            return self.chat_name
        try:
            return str(self._uia.current_chat() or "")
        except Exception:
            return ""

    @staticmethod
    def _restore_foreground(hwnd: int) -> bool:
        """把前台窗口还给 `hwnd`，成功返回 True。

        为什么要 `AttachThreadInput`：Windows 只允许**当前前台进程**调
        `SetForegroundWindow`，后台进程直接调会被拒绝（实测返回 False，窗口停在微信上）。
        先附到当前前台线程再调就能过，用完立刻解除附着。
        """
        if not hwnd:
            return False
        try:
            import win32api
            import win32gui
            import win32process
        except Exception:
            return False
        cur = win32api.GetCurrentThreadId()
        fg = win32gui.GetForegroundWindow()
        fgt = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        attached = False
        try:
            if fgt and fgt != cur:
                attached = bool(win32process.AttachThreadInput(cur, fgt, True))
            win32gui.SetForegroundWindow(hwnd)
            return win32gui.GetForegroundWindow() == hwnd
        except Exception:
            return False
        finally:
            if attached:
                try:
                    win32process.AttachThreadInput(cur, fgt, False)
                except Exception:
                    pass

    def _input(self):
        try:
            return self._uia._chat_input(self._win)      # noqa: SLF001
        except Exception:
            return None

    def draft(self) -> str:
        """输入框里现有的内容。空串 = 空（或读不到，见 `readable()`）。"""
        ctrl = self._input()
        if ctrl is None:
            return ""
        try:
            return str(self._uia._read_edit_value(ctrl) or "")   # noqa: SLF001
        except Exception:
            return ""

    def readable(self) -> bool:
        """能不能读到输入框内容。读不到时**不能**用"草稿为空"当判据。"""
        ctrl = self._input()
        if ctrl is None:
            return False
        try:
            return self._uia._read_edit_value(ctrl) is not None  # noqa: SLF001
        except Exception:
            return False

    def writable(self) -> bool:
        """输入框是否存在且可写（ValuePattern 非只读）。"""
        ctrl = self._input()
        if ctrl is None:
            return False
        try:
            vp = ctrl.GetValuePattern()
            return vp is not None and not vp.IsReadOnly
        except Exception:
            return False

    # ---------------------------------------------------------------- 发送

    @staticmethod
    def user_idle_sec() -> float:
        """用户已经多少秒没碰键鼠了（`GetLastInputInfo`）。

        用途：**用户正在打字的时候不要发**。微信发送时会把窗口抬起来，
        如果他此刻正在别处敲键盘，那一下会被打断。等他一停手再发，体感好得多。
        读不到返回 0（当作"正在用"，宁可多等一秒）。
        """
        try:
            import ctypes
            from ctypes import wintypes

            class _LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
        except Exception:
            return 0.0

    def _set_no_activate(self, on: bool) -> None:
        """临时切 `WS_EX_NOACTIVATE`：让微信窗口在**被写入和发送时无法被激活**。

        2026-10-17 实测（微信 4.1.15.13）：
        - 不加这个样式时，连 `ValuePattern.SetValue` 都会把窗口抬到前台
          （Qt 在写输入框时会 `setFocus`）——用户正在别处打字就会被抢走焦点；
        - 加上之后，`SetValue` + `PostMessage(VK_RETURN)` 全程**前台窗口不变**，
          输入框照样清空、消息照样发出（数据库可回读）。

        代价：这一两秒内用户点这个窗口它不会激活。发完立刻还原原样式。
        """
        try:
            import win32gui
        except Exception:
            return
        try:
            style = win32gui.GetWindowLong(self._hwnd, GWL_EXSTYLE)
            if on:
                # ⚠ **只在第一次保存原样式**。多段回复会连着调这个函数
                # （发之前一次、收尾一次、定时器再兜一次），如果每次都覆盖，
                # 第二次保存到的就是"已经带上 NOACTIVATE"的值 ⇒ 还原时把
                # 这个位永久留在窗口上，用户以后点那个窗口**再也激活不了**。
                if self._saved_exstyle is None:
                    self._saved_exstyle = style
                win32gui.SetWindowLong(self._hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
            elif self._saved_exstyle is not None:
                win32gui.SetWindowLong(self._hwnd, GWL_EXSTYLE, self._saved_exstyle)
                self._saved_exstyle = None
        except Exception as exc:
            print(f"[bg] 调整窗口激活样式失败（继续发送）：{exc!r}")
            self._saved_exstyle = None

    def _cleanup_leftover_noactivate(self) -> None:
        """清掉**上一次运行残留**的 `WS_EX_NOACTIVATE`。

        为什么需要：早期版本每次开启都会覆盖保存值，导致还原时把这个位永久留在窗口上，
        用户点那个微信窗口**再也激活不了**（2026-10-17 真机踩到：主窗口和独立窗口
        都中招，必须手动清）。现在除了修好保存逻辑，再在绑定窗口时兜一道保险：
        只要这个位在、而我们自己没在发送中，就立刻清掉。
        """
        try:
            import win32gui
        except Exception:
            return
        try:
            style = win32gui.GetWindowLong(self._hwnd, GWL_EXSTYLE)
            if style & WS_EX_NOACTIVATE and self._saved_exstyle is None:
                win32gui.SetWindowLong(self._hwnd, GWL_EXSTYLE, style & ~WS_EX_NOACTIVATE)
                print(f"[bg] 清掉了窗口 {self._hwnd} 上残留的 WS_EX_NOACTIVATE"
                      "（旧版本会把它永久留下，导致窗口点不动）")
        except Exception:
            pass

    def _hold_no_activate(self, seconds: float) -> None:
        """打开"禁止激活"并安排 `seconds` 秒后**异步**还原（可重复调用，会顺延）。

        为什么异步：发送本身只要几百毫秒，但微信的抬窗尝试是"发出后几秒内"才发生的，
        而且只尝试一次。按住 4 秒再放开，它就不补抬了（实测）。
        同步 sleep 会把每条回复拖慢 4 秒，多段回复更糟，所以用定时器。
        """
        with self._na_lock:
            if self._na_timer is not None:
                self._na_timer.cancel()
                self._na_timer = None
            self._set_no_activate(True)
            timer = threading.Timer(max(0.1, float(seconds)), self._release_no_activate)
            timer.daemon = True
            self._na_timer = timer
            timer.start()

    def _release_no_activate(self) -> None:
        with self._na_lock:
            self._na_timer = None
            self._set_no_activate(False)

    def _reminimize_watchdog(self, seconds: float = 2.5) -> None:
        """盯 `seconds` 秒：只要窗口又被还原出来，就再最小化回去。

        微信/Qt 的还原是异步的（发完之后还会补一次 `SW_RESTORE`），只还一次手会被它
        覆盖掉——用户看到的就是"第一次会弹一下，后面就一直开着"。这里用后台线程盯住，
        不阻塞发送流程。
        """
        hwnd = self._hwnd

        def worker() -> None:
            try:
                import win32con
                import win32gui
            except Exception:
                return
            end = time.time() + max(0.5, seconds)
            while time.time() < end:
                time.sleep(0.2)
                try:
                    if not win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                except Exception:
                    return

        thread = threading.Thread(target=worker, daemon=True, name="bg-remin")
        thread.start()

    def send(self, text: str, settle: float = 0.6,
             restore_focus: bool = True,
             minimize_after: bool = False) -> Tuple[bool, str]:
        """把 `text` 发到当前会话。返回 (成功?, 人话原因)。

        调用方要先自己确认「当前会话就是目标会话」——本模块**不做**任何导航。

        `restore_focus=True`（默认）会在**发完之后立刻把前台窗口还给原来那个**：
        微信的输入框要拿到 UIA 焦点才能吃回车，而 `SetFocus` 会把窗口抬到前台
        （2026-10-17 实测：可见窗口会被抬起；最小化的窗口不会）。
        还回去之后用户只会看到一次极短的闪烁，鼠标不会被挪走、键盘不会被打断。
        """
        text = (text or "").strip()
        if not text:
            return False, "正文为空"

        ctrl = self._input()
        if ctrl is None:
            return False, "输入框不存在（目标会话没打开）"
        try:
            vp = ctrl.GetValuePattern()
        except Exception as exc:
            return False, f"取不到 ValuePattern：{exc!r}"
        if vp is None or vp.IsReadOnly:
            return False, "输入框不可写（ValuePattern 只读或缺失）"

        # 安全规则 1：输入框里已有内容 = 可能是用户的草稿 ⇒ 一律不发，也不覆盖。
        if not self.readable():
            return False, "读不到输入框内容，无法确认不会覆盖草稿"
        existing = self.draft()
        if existing.strip():
            return False, f"输入框里已有内容（{len(existing)} 字），不覆盖"

        prev_fg = 0
        if restore_focus:
            try:
                prev_fg = int(self._user32.GetForegroundWindow() or 0)
            except Exception:
                prev_fg = 0

        # **最小化的窗口先不激活地还原**。
        # 为什么必须自己还原：Qt 在写输入框时会自己 `ShowWindow(SW_RESTORE)`，
        # 那个还原**绕过 WS_EX_NOACTIVATE**、而且控件还没渲染好就写 ⇒
        # 用户看到的是"窗口弹出来、字却没发出去"（2026-10-17 实测）。
        # 用 SW_SHOWNOACTIVATE 自己还原、等控件就绪再写，发完再最小化回去。
        was_iconic = False
        try:
            import win32con
            import win32gui
            if win32gui.IsIconic(self._hwnd):
                was_iconic = True
                win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
                time.sleep(1.2)          # 等 Qt 把输入框渲染出来
                print("[bg] 目标窗口原本最小化：已不激活地还原（发完会再最小化回去）")
        except Exception as exc:
            print(f"[bg] 还原最小化窗口失败（继续尝试发送）：{exc!r}")

        # ★ 整段「写入 + 回车 + 确认」期间让窗口**无法被激活**。
        #   实测不加这个样式，光是 `SetValue` 就会把微信窗口抬到前台
        #   （Qt 在写输入框时会内部 setFocus）；加上之后前台窗口全程不动。
        sent = False
        self._hold_no_activate(self.no_activate_hold + 1.0)
        try:
            try:
                vp.SetValue(text)
            except Exception as exc:
                return False, f"SetValue 失败：{exc!r}"
            time.sleep(0.25)

            # 写进去了没有？读不回来时按"写进去了"继续（回读能力不是必须的）。
            try:
                now = self._uia._read_edit_value(ctrl)       # noqa: SLF001
            except Exception:
                now = None
            if now is not None and not str(now).strip():
                return False, "SetValue 后输入框仍是空的"

            # **不要** `ctrl.SetFocus()`：它会绕过 WS_EX_NOACTIVATE 把窗口抬起来
            # （实测：加了样式，SetFocus 照样激活）。而回车是 PostMessage 直接投递给
            # 窗口的，Qt 会交给它自己的内部焦点控件处理——不需要先 SetFocus。
            #
            # **回车要补投一次**：真机出现过"回车后输入框没清空"，随后退回 GUI 路径，
            # 结果 GUI 卡了 90 秒、整条桥接跟着停摆（2026-10-18 20:48）。
            # PostMessage 投给未聚焦窗口偶尔会丢一拍，补投一次的代价远小于退回 GUI。
            for attempt in range(2):
                time.sleep(0.15 if attempt == 0 else 0.25)
                self._user32.PostMessageW(self._hwnd, WM_KEYDOWN, VK_RETURN, 1)
                time.sleep(0.1)
                self._user32.PostMessageW(self._hwnd, WM_KEYUP, VK_RETURN, 0xC0000001)

                deadline = time.time() + max(0.4, settle)
                while time.time() < deadline:
                    time.sleep(0.15)
                    try:
                        left = self._uia._read_edit_value(ctrl)  # noqa: SLF001
                    except Exception:
                        left = None
                    if left is not None and not str(left).strip():
                        sent = True
                        break
                if sent:
                    break

            if not sent:
                # 没清空 = 没发出去。把内容清掉，别在输入框里留半截。
                try:
                    vp.SetValue("")
                except Exception:
                    pass
        finally:
            # 发送本身做完了，但微信的抬窗还没尝试完 ⇒ 重新计时（异步还原，不阻塞）
            if self._saved_exstyle is not None:
                self._hold_no_activate(self.no_activate_hold)
            if was_iconic:
                # 用户本来就是最小化的：还回去，并且**盯一小会儿**。
                # 为什么不能只还一次：Qt 的还原是异步的，我们最小化之后它可能又把它
                # 拉出来（实测"第一次会弹、后面就不弹"，正是因为那次还原把它留在了桌面上）。
                try:
                    import win32con
                    import win32gui
                    win32gui.ShowWindow(self._hwnd, win32con.SW_MINIMIZE)
                except Exception:
                    pass
                self._reminimize_watchdog()

        if not sent:
            if restore_focus and prev_fg and prev_fg != self._hwnd:
                self._restore_foreground(prev_fg)
            return False, "回车后输入框没清空（消息可能没发出）"

        # 正常情况下 `WS_EX_NOACTIVATE` 已经保证前台没变过；这里只是兜底。
        restored = False
        try:
            fg_now = int(self._user32.GetForegroundWindow() or 0)
        except Exception:
            fg_now = 0
        if restore_focus and prev_fg and fg_now and fg_now != prev_fg:
            restored = self._restore_foreground(prev_fg)
            if not restored:
                time.sleep(0.25)
                restored = self._restore_foreground(prev_fg)
        if minimize_after:
            # 兜底：万一还是被抬起来了，把窗口收起来，焦点会自然落回用户那边。
            try:
                import win32con
                import win32gui
                win32gui.ShowWindow(self._hwnd, win32con.SW_MINIMIZE)
            except Exception:
                pass
        note = "，全程没有抢焦点" if not restored else "，已把焦点还回去"
        if minimize_after:
            note = "，已收起窗口"
        return True, f"后台发送成功（输入框已清空{note}）"
