"""看看微信输入框里现在有什么 —— 专治「发送失败后那半句留在框里」。

## 为什么需要它

2026-10-18 真机出现两次：GUI 发送失败 → 那半句**留在输入框里** →
几分钟后库自己做界面维护（重新校准布局、点发送按钮、补回车）把它冲出去，
群里出现三条一模一样的消息，而且**没有任何日志**（不走 `send()`）。

修法已经在适配器里（失败即清，且只清"和自己写的内容完全一致"的框）。
这个脚本是**修之前/修之后的手工核查工具**：先看清框里到底是什么，
再用 `--clear-if` 精确匹配地清掉——**绝不做无脑清空**，那可能删掉用户自己的草稿。

## 跑法（要能读到微信 UIA，沙箱外）

    python probe_input_box.py                                   # 只看
    python probe_input_box.py --clear-if "小升初？"              # 内容完全一致才清
    python probe_input_box.py --clear-if-file state/_leftover.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYLIBS = ROOT / ".pylibs"
for _p in (ROOT, PYLIBS, PYLIBS / "pythonwin", PYLIBS / "win32", PYLIBS / "win32" / "lib",
           PYLIBS / "win32comext"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# pywin32 的 pyd（win32ui 等）要显式挂 DLL 目录，否则报
# 「DLL load failed while importing win32ui」——那不是没装，是没找着依赖。
import os as _os
for _d in (PYLIBS / "pywin32_system32", PYLIBS / "win32", PYLIBS / "pythonwin"):
    if _d.exists():
        try:
            _os.add_dll_directory(str(_d))
        except Exception:
            pass


def norm(text: str) -> str:
    return "".join(str(text or "").split())


def main() -> int:
    ap = argparse.ArgumentParser(description="读/清微信输入框残留（只清完全一致的）")
    ap.add_argument("--clear-if", default="", help="输入框内容与它完全一致时才清空")
    ap.add_argument("--clear-if-file", default="", help="同上，从文件读（避免命令行转义）")
    args = ap.parse_args()

    from wechat_bridge import console
    console.install()

    try:
        from wechatauto.uia_driver import WeChatUIA
    except Exception as exc:
        print(f"[停] 装不上 wechatauto：{exc!r}")
        return 2

    uia = WeChatUIA()
    if not uia.ensure_window():
        print("[停] 找不到微信主窗口（微信没开 / 没登录 / 在沙箱里）")
        return 2

    try:
        ctrl = uia._chat_input()             # noqa: SLF001
    except Exception as exc:
        print(f"[停] 读输入框失败：{exc!r}")
        return 2
    if ctrl is None:
        print("[停] 输入框控件没找到（会话可能没打开）")
        return 2

    current = uia._read_edit_value(ctrl)      # noqa: SLF001
    if current is None:
        print("[停] 输入框不暴露值（读不到 ≠ 空，不敢猜）")
        return 2
    print(f"输入框当前内容：{current!r}（{len(current)} 字）")

    want = args.clear_if
    if args.clear_if_file:
        want = Path(args.clear_if_file).read_text(encoding="utf-8-sig").strip()
    if not want:
        print("（只读模式。要清就加 --clear-if \"<应当删除的正文>\"）")
        return 0

    if not norm(current):
        print("输入框是空的，不用清。")
        return 0
    if norm(current) != norm(want):
        print("⚠ 内容和 --clear-if 不一致 ⇒ **不清**（可能是你自己在打的草稿）")
        return 1

    try:
        vp = ctrl.GetValuePattern()
        if vp is None or vp.IsReadOnly:
            print("[停] 输入框不可写（ValuePattern 只读或缺失）")
            return 2
        vp.SetValue("")
    except Exception as exc:
        print(f"[停] 清空失败：{exc!r}")
        return 2
    print("✔ 已清空输入框（内容与 --clear-if 完全一致，没动别的）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
