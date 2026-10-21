# -*- coding: utf-8 -*-
"""后台发送模块的离线自测（不连微信）。

锁住的是三件**真机上要命**的事：
  1. 输入框里有草稿时**绝不允许覆盖**（那是用户没发出去的话）；
  2. 读不到输入框内容时也不许发（分不清"空"和"读不到"就不能赌）；
  3. 回车后输入框没清空 ⇒ 判失败，并把写进去的内容清掉（不留半截状态）。

跑法：python bg_selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wechat_bridge.bg_send import BackgroundSender  # noqa: E402


class FakeValuePattern:
    def __init__(self, store, readonly=False):
        self._store = store
        self.IsReadOnly = readonly

    def SetValue(self, value):
        if self.IsReadOnly:
            raise RuntimeError("read only")
        self._store["value"] = value
        self._store["sets"].append(value)

    @property
    def Value(self):
        return self._store["value"]


class FakeCtrl:
    def __init__(self, store):
        self._store = store
        self._vp = FakeValuePattern(store)

    def GetValuePattern(self):
        return self._vp

    def SetFocus(self):
        self._store["focused"] = True


class FakeUser32:
    """记录 PostMessage；`sends=True` 时模拟"回车真的发出去了"。"""

    def __init__(self, store, sends=True):
        self._store = store
        self._sends = sends
        self.posts = []

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.posts.append((hwnd, msg, wparam))
        if msg == 0x0101 and wparam == 0x0D and self._sends:
            self._store["value"] = ""        # 真实微信：发送后输入框清空
        return 1


class FakeUIA:
    def __init__(self, value="", readable=True, has_ctrl=True, readonly=False,
                 sends=True, chat="测试群"):
        self.store = {"value": value, "sets": [], "focused": False}
        self.chat = chat
        self.readable = readable
        self.has_ctrl = has_ctrl
        self.ctrl = FakeCtrl(self.store) if has_ctrl else None
        if self.ctrl is not None:
            self.ctrl._vp = FakeValuePattern(self.store, readonly=readonly)
        self.user32 = FakeUser32(self.store, sends=sends)

    # —— 被 BackgroundSender 用到的驱动接口 ——
    def ensure_materialized(self, timeout=6.0):
        return True

    def _find_main(self):
        return type("W", (), {"NativeWindowHandle": 424242})()

    def _chat_input(self, win=None):
        return self.ctrl

    def _read_edit_value(self, ctrl):
        return self.store["value"] if self.readable else None

    def current_chat(self):
        return self.chat


def make(uia):
    sender = BackgroundSender(uia, type("W", (), {"NativeWindowHandle": 424242})())
    sender._user32 = uia.user32          # noqa: SLF001 —— 换成记录用的假 user32
    return sender


def check(label, condition, detail=""):
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


def main() -> int:
    ok = True

    # 1) 正常发送：写进去、回车、输入框清空
    uia = FakeUIA()
    sender = make(uia)
    good, why = sender.send("收到", settle=0.3)
    ok &= check("正常后台发送成功", good, why)
    ok &= check("走的是 SetValue（不敲键盘）", uia.store["sets"] == ["收到"], str(uia.store["sets"]))
    ok &= check("PostMessage 发了回车", any(p[1] == 0x0101 and p[2] == 0x0D for p in uia.user32.posts))
    # **不能**调 SetFocus：它会绕过 WS_EX_NOACTIVATE 把微信窗口抬到前台（实测）
    ok &= check("没有调 SetFocus（否则会抢焦点）", uia.store["focused"] is False)

    # 2) 输入框里有草稿 ⇒ 一条都不许写（这是安全规则的底线）
    uia = FakeUIA(value="我正在打的一段话")
    good, why = make(uia).send("收到", settle=0.2)
    ok &= check("有草稿时拒绝发送", good is False and "已有内容" in why, why)
    ok &= check("有草稿时**没有**调用 SetValue（没覆盖用户输入）", uia.store["sets"] == [], str(uia.store["sets"]))
    ok &= check("有草稿时不发回车", uia.user32.posts == [])

    # 3) 读不到输入框内容 ⇒ 也不许赌"它是空的"
    uia = FakeUIA(readable=False)
    good, why = make(uia).send("收到", settle=0.2)
    ok &= check("读不到内容时拒绝发送", good is False and "读不到" in why, why)

    # 4) 回车后没清空（模拟没发出去）⇒ 判失败，并把写进去的清掉
    uia = FakeUIA(sends=False)
    good, why = make(uia).send("收到", settle=0.4)
    ok &= check("回车无效时判失败", good is False and "没清空" in why, why)
    ok &= check("失败后清空输入框（不留半截）", uia.store["value"] == "" and uia.store["sets"][-1] == "")

    # 5) 输入框不存在（目标会话没打开）
    uia = FakeUIA(has_ctrl=False)
    good, why = make(uia).send("收到", settle=0.2)
    ok &= check("会话没打开时明确报错", good is False and "没打开" in why, why)

    # 6) 输入框只读
    uia = FakeUIA(readonly=True)
    good, why = make(uia).send("收到", settle=0.2)
    ok &= check("输入框只读时拒绝", good is False and "不可写" in why, why)

    # 7) 空正文
    uia = FakeUIA()
    good, why = make(uia).send("   ", settle=0.2)
    ok &= check("空正文拒绝", good is False and "为空" in why, why)

    # 8) 只读探测：draft / writable 的判断要跟真实状态一致
    uia = FakeUIA(value="草稿")
    sender = make(uia)
    ok &= check("draft 读到草稿", sender.draft() == "草稿", sender.draft())
    ok &= check("writable 判定为可写", sender.writable() is True)
    ok &= check("current_chat 透传", sender.current_chat() == "测试群")

    # 9) **重复调用 `_set_no_activate(True)` 不能把样式永久留在窗口上**。
    #    多段回复会连调好几次（发前一次、收尾一次、定时器再兜一次），
    #    如果每次都覆盖保存值，第二次保存到的就是"已带 NOACTIVATE"的值 ⇒
    #    还原后这个位永远留着，用户点那个微信窗口再也激活不了。
    import sys as _sys
    import types as _types

    NOACT = 0x08000000

    class _FakeW32:
        def __init__(self):
            self.style = 0x00000100
            self.sets = []

        def GetWindowLong(self, hwnd, index):
            return self.style

        def SetWindowLong(self, hwnd, index, value):
            self.sets.append(value)
            self.style = value
            return value

    fake = _FakeW32()
    saved_mod = _sys.modules.get("win32gui", "__absent__")
    _sys.modules["win32gui"] = fake
    try:
        s = BackgroundSender(FakeUIA(), _types.SimpleNamespace(NativeWindowHandle=99))
        s._set_no_activate(True)          # noqa: SLF001
        s._set_no_activate(True)          # noqa: SLF001 —— 第二段回复又开一次
        ok &= check("重复开启时确实加上了 NOACTIVATE", bool(fake.style & NOACT))
        s._set_no_activate(False)         # noqa: SLF001
        ok &= check("一次性还原后 NOACTIVATE 被清掉（不会永久卡住）",
                    not (fake.style & NOACT), hex(fake.style))
        s._set_no_activate(False)         # noqa: SLF001 —— 多调一次也不能出错
        ok &= check("重复还原是安全的", not (fake.style & NOACT), hex(fake.style))
    finally:
        if saved_mod == "__absent__":
            _sys.modules.pop("win32gui", None)
        else:
            _sys.modules["win32gui"] = saved_mod

    print(f"\n结论：{'后台发送离线自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
