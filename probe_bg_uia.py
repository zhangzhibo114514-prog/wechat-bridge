#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探测：微信 4.x 能不能**不抢焦点、不动鼠标**地完成"切会话 → 输入 → 发送"。

为什么需要这个探针
------------------
现在的发送走的是「桌面操控」：`ensure_window()` 把窗口拉到前台、`_click_ctrl()`
真的移动鼠标去点、`SendKeys("{Enter}")` 真的敲键盘。用户在用电脑时会被打断——
这是**必须迈过去的坎**（见 DESIGN-BACKGROUND.md）。

后台发送要成立，需要三件事同时成立：
  1. 切会话：会话列表项支持 `Invoke` / `SelectionItem` 模式（不是只能点坐标）；
  2. 输入：聊天输入框支持 `ValuePattern.SetValue`（不聚焦也能写进去）；
  3. 发送：界面上有一个能 `Invoke` 的「发送」按钮（不靠回车键）。

**本探针默认只读**：只枚举控件与模式，不输入、不发送、不移动鼠标。
真正会改状态的只有 `--dry-input`（写进输入框再读回再清空，不发送）。

跑法：
    python probe_bg_uia.py                # 只读探测
    python probe_bg_uia.py --dry-input    # 额外验证 SetValue 能不能写进聊天输入框
    python probe_bg_uia.py --wake         # 树没物化时热激活 accessibility gate 再试

判定口径（打印在结论里）：
    ✅ 后台可行：三项全绿
    ⚠️ 半后台：输入可以、切会话/发送只能靠模拟输入
    ❌ 后台不可行：输入框不暴露 ValuePattern，只能走桌面操控
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# 依赖目录 + pywin32 的 DLL 目录都交给适配器那套（自己抄一遍迟早漏 dll 目录）
from wechat_bridge.adapters.hook_wechatauto import _ensure_pylibs_on_path  # noqa: E402

_ensure_pylibs_on_path()


def _foreground() -> int:
    try:
        import win32gui
        return int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _patterns(ctrl) -> list[str]:
    """这个控件到底支持哪些 UIA 模式（只询问，不调用动作）。"""
    out = []
    for name, getter in (
        ("Value", "GetValuePattern"),
        ("Invoke", "GetInvokePattern"),
        ("SelectionItem", "GetSelectionItemPattern"),
        ("ScrollItem", "GetScrollItemPattern"),
        ("Toggle", "GetTogglePattern"),
        ("Text", "GetTextPattern"),
        ("Legacy", "GetLegacyIAccessiblePattern"),
    ):
        fn = getattr(ctrl, getter, None)
        if fn is None:
            continue
        try:
            if fn() is not None:
                out.append(name)
        except Exception:
            pass
    return out


def _brief(ctrl) -> str:
    try:
        rect = ctrl.BoundingRectangle
        box = f"{rect.left},{rect.top},{rect.width()}x{rect.height()}"
    except Exception:
        box = "?"
    return (f"[{ctrl.ControlTypeName}] name={(ctrl.Name or '')[:24]!r} "
            f"aid={(getattr(ctrl, 'AutomationId', '') or '')!r} cls={(ctrl.ClassName or '')[:24]!r} "
            f"box={box} patterns={_patterns(ctrl)}")


def _walk(ctrl, depth=0, max_depth=12, limit=400, acc=None):
    acc = acc if acc is not None else []
    if depth > max_depth or len(acc) >= limit:
        return acc
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    for child in children:
        acc.append((depth, child))
        _walk(child, depth + 1, max_depth, limit, acc)
        if len(acc) >= limit:
            break
    return acc


def probe_search(uia, win, keyword: str) -> None:
    """验证「搜索 → 结果」能不能全程只用 UIA 模式（不摸鼠标、不敲键盘）。

    背景：左侧会话列表在微信 4.x 里是自绘的，**不暴露子项**（实测 session_list
    下没有可 Invoke 的行），所以"点列表切会话"没法后台化。但搜索这条路不一样：
    搜索框支持 `SetValue`（库已经在用），结果列表 `search_list` 下有真实的
    `search_item_*` 控件——只要它们支持 Invoke，切会话就能全程后台完成。
    """
    print("\n[5] 搜索链路（切会话的后台候选路径）")
    fg0 = _foreground()

    btn = None
    for _, ctrl in _walk(win):
        if (ctrl.Name or "").strip() == "搜索" and "Button" in ctrl.ControlTypeName:
            btn = ctrl
            break
    if btn is None:
        print("    ✘ 没找到「搜索」按钮")
        return
    print(f"    搜索按钮：{_brief(btn)}")
    try:
        pat = btn.GetInvokePattern()
        if pat is None:
            print("    ✘ 搜索按钮不支持 Invoke")
            return
        pat.Invoke()
        print("    · 已用 Invoke 展开搜索框（没有鼠标参与）")
    except Exception as exc:
        print(f"    ✘ Invoke 失败：{exc!r}")
        return
    time.sleep(1.0)

    box = None
    try:
        box = uia._search_box(win)                             # noqa: SLF001
    except Exception as exc:
        print(f"    定位搜索框异常：{exc!r}")
    if box is None:
        print("    ✘ 搜索框没出现")
        return
    print(f"    搜索框：{_brief(box)}")

    written = False
    try:
        vp = box.GetValuePattern()
        if vp is not None and not vp.IsReadOnly:
            vp.SetValue(keyword)
            written = True
    except Exception as exc:
        print(f"    SetValue 失败：{exc!r}")
    print(f"    SetValue 写入 {keyword!r}：{'✅' if written else '❌'}（全程没敲键盘）")
    time.sleep(1.5)

    lst = None
    for _, ctrl in _walk(win):
        if (getattr(ctrl, "AutomationId", "") or "") in ("search_list",):
            lst = ctrl
            break
    if lst is None:
        print("    ✘ 结果列表 search_list 没出现")
    else:
        rows = []
        try:
            rows = [c for c in lst.GetChildren()][:8]
        except Exception:
            pass
        print(f"    结果项 {len(rows)} 个：")
        invokable = 0
        for row in rows:
            pats = _patterns(row)
            if "Invoke" in pats or "SelectionItem" in pats:
                invokable += 1
            print(f"      {_brief(row)[:160]}")
        print(f"    可编程点开的结果：{invokable}/{len(rows)}"
              f"{'  ✅' if invokable else '  ❌'}")

    # 收尾：把搜索框清掉，别给用户留下半截状态
    try:
        vp = box.GetValuePattern()
        if vp is not None:
            vp.SetValue("")
    except Exception:
        pass
    print(f"    搜索链路结束时前台窗口：{_foreground()}"
          f"（{'没变 ✅' if _foreground() == fg0 else '变了 ⚠️'}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-input", action="store_true",
                        help="把一段文字用 SetValue 写进聊天输入框、读回、再清空（不发送）")
    parser.add_argument("--wake", action="store_true",
                        help="树没物化时热激活 accessibility gate")
    parser.add_argument("--dump", type=int, default=0,
                        help="额外打印前 N 个控件（排查用，默认 0 = 不打印）")
    parser.add_argument("--try-search", default="",
                        help="验证搜索链路能否全程用 UIA 模式（给一个关键词，例如 文件传输助手）")
    parser.add_argument("--show-noactivate", action="store_true",
                        help="窗口最小化时用 SW_SHOWNOACTIVATE 还原（渲染出来但不抢焦点）")
    args = parser.parse_args()

    from wechatauto.uia_driver import WeChatUIA

    uia = WeChatUIA()
    fg_before = _foreground()

    print("=" * 72)
    print("后台能力探测：微信 4.x（不抢焦点 / 不动鼠标）")
    print("=" * 72)

    if not uia._find_main():                                   # noqa: SLF001
        if args.wake:
            print("· 树还没物化，热激活 accessibility gate（只写 1 字节，不抢焦点）…")
            uia.ensure_materialized(timeout=8.0, force=True)
        else:
            print("· 树还没物化（`mmui::` 控件扫不到）。加 --wake 再试一次。")
    win = uia._find_main()                                     # noqa: SLF001
    if win is None:
        print("✘ 找不到微信主窗口：请确认微信已启动并已登录。")
        return 2

    print(f"· 主窗口：{_brief(win)}")
    print(f"· 探测期间前台窗口：{fg_before}（窗口句柄：{win.NativeWindowHandle}）")

    # 最小化时 Qt 不会给列表项做布局 ⇒ UIA 里看不到会话行。
    # SW_SHOWNOACTIVATE(4) 只让它"显示出来"，**不激活、不抢焦点**——这是后台化必须的那一手。
    if args.show_noactivate:
        try:
            import win32con
            import win32gui
            hwnd = win.NativeWindowHandle
            iconic = bool(win32gui.IsIconic(hwnd))
            print(f"· 窗口最小化：{iconic}；IsWindowVisible={bool(win32gui.IsWindowVisible(hwnd))}")
            if iconic:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
                time.sleep(1.5)
                print(f"· 已用 SW_SHOWNOACTIVATE 还原（前台窗口仍是 {_foreground()}）")
        except Exception as exc:
            print(f"· 还原窗口失败：{exc!r}")

    if args.dump:
        print(f"\n[0] 控件树前 {args.dump} 个节点")
        for depth, ctrl in _walk(win, max_depth=10, limit=args.dump):
            print(f"    {'  ' * depth}{_brief(ctrl)[:150]}")

    # —— 1. 聊天输入框 ——
    print("\n[1] 聊天输入框")
    edit = None
    try:
        edit = uia._chat_input(win)                            # noqa: SLF001
    except Exception as exc:
        print(f"    定位异常：{exc!r}")
    if edit is None:
        print("    ✘ 没找到 chat_input_field（会话可能没打开）")
        value_ok = False
    else:
        print(f"    {_brief(edit)}")
        readonly = None
        try:
            vp = edit.GetValuePattern()
            readonly = bool(vp.IsReadOnly) if vp is not None else None
        except Exception:
            pass
        value_ok = "Value" in _patterns(edit) and readonly is not True
        print(f"    ValuePattern 可写：{'✅' if value_ok else '❌'}（IsReadOnly={readonly}）")

    # —— 2. 发送按钮 ——
    print("\n[2] 发送按钮（能 Invoke 就不用敲回车）")
    send_ctrl = None
    for depth, ctrl in _walk(win):
        name = (ctrl.Name or "")
        aid = (getattr(ctrl, "AutomationId", "") or "").lower()
        if "发送" in name or "send" in aid:
            print(f"    {_brief(ctrl)}")
            if send_ctrl is None and "Invoke" in _patterns(ctrl):
                send_ctrl = ctrl
    invoke_ok = send_ctrl is not None
    print(f"    可 Invoke 的发送按钮：{'✅' if invoke_ok else '❌（只能靠回车键）'}")

    # —— 3. 会话列表（切会话）——
    print("\n[3] 会话列表项（能不能 Invoke/选中，而不是点坐标）")
    session_ctrl = None
    for depth, ctrl in _walk(win):
        if (getattr(ctrl, "AutomationId", "") or "") == "session_list":
            session_ctrl = ctrl
            break
    switch_ok = False
    if session_ctrl is None:
        print("    ✘ 没找到 session_list")
    else:
        items = []
        try:
            items = [c for c in session_ctrl.GetChildren()][:5]
        except Exception:
            pass
        for item in items:
            pats = _patterns(item)
            print(f"    项 {_brief(item)[:150]}")
            if "Invoke" in pats or "SelectionItem" in pats:
                switch_ok = True
        if not items:
            print("    （session_list 下没有子项）")
    print(f"    可编程切会话：{'✅' if switch_ok else '❌（只能点坐标/搜索）'}")

    if args.try_search:
        probe_search(uia, win, args.try_search)

    # —— 4. dry-input（可选，会短暂写进输入框）——
    if args.dry_input and edit is not None and value_ok:
        print("\n[4] dry-input：SetValue 写进聊天输入框 → 读回 → 清空（**不发送**）")
        probe = "后台输入探针 ABC123"
        try:
            vp = edit.GetValuePattern()
            vp.SetValue(probe)
            time.sleep(0.5)
            uia._paste_landed(edit, probe)                     # noqa: SLF001
            landed = uia._read_edit_value(edit)                # noqa: SLF001
            print(f"    读回：{landed!r}")
            vp.SetValue("")
            time.sleep(0.3)
            print(f"    清空后读回：{uia._read_edit_value(edit)!r}")   # noqa: SLF001
            input_landed = bool(landed and probe in str(landed)) or bool(landed)
        except Exception as exc:
            input_landed = False
            print(f"    ✘ 写入异常：{exc!r}")
    else:
        input_landed = value_ok
        if args.dry_input:
            print("\n[4] 跳过 dry-input：输入框不可写或没定位到")

    # —— 结论 ——
    fg_after = _foreground()
    print("\n" + "=" * 72)
    print(f"前台窗口在探测前后是否变化：{'没变 ✅' if fg_after == fg_before else f'变了 ⚠️ {fg_before} → {fg_after}'}")
    if edit is None and session_ctrl is None:
        print("结论：⚠️ 探测不完整——微信刚启动 / 没打开任何会话，`chat_input_field` 与 "
              "`session_list` 都还没渲染。**先手动点开一个会话**再跑一次本探针。")
        return 3
    if session_ctrl is not None and not switch_ok:
        print("注：会话列表项不支持 Invoke/SelectionItem ⇒ 切会话仍得靠搜索框 SetValue + 点击结果，"
              "后者要摸鼠标。这是后台化最大的缺口，需要单独想办法（见 DESIGN-BACKGROUND.md 方案 B/C）。")
    if input_landed and invoke_ok and switch_ok:
        print("结论：✅ 三项全绿 —— 可以做成**完全后台**发送（不抢焦点、不动鼠标）。")
        return 0
    if input_landed:
        print("结论：⚠️ 半后台 —— 输入可以后台完成，切会话/发送仍有部分要动桌面。")
        return 1
    print("结论：❌ 后台不可行（输入框不暴露可写 ValuePattern）——只能继续桌面操控，或走注入路线。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
