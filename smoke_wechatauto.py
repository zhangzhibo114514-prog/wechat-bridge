"""离线冒烟测试：不解包安装，直接把 .pylibs 挂进 sys.path 验证 wechatauto 能否 import。

用途：本机 pip 不可用（沙箱禁止写临时目录），依赖是用 pkg_fetch.py 手动解包到 .pylibs 的。
这个脚本回答两个问题：
    1. 这些手工解包的依赖（含 pywin32 / cryptography / Pillow / opencv 等二进制包）能不能被 import；
    2. wechatauto 本身能不能 import，能否构造出 WeChatDB / WeChat 对象。

跑法：python smoke_wechatauto.py
输出依赖链上第一个失败点，便于针对性补包。
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYLIBS = ROOT / ".pylibs"

# 二进制包依赖的 DLL 目录（pywin32 的 pywin32_system32 等），必须显式加进搜索路径
DLL_DIRS = [PYLIBS / "pywin32_system32", PYLIBS / "win32", PYLIBS / "win32" / "lib"]

CHECKS = [
    ("uiautomation", "UIA 控件树驱动"),
    ("win32api", "pywin32 核心"),
    ("pythoncom", "COM（UIA 需要）"),
    ("PIL", "Pillow"),
    ("psutil", "psutil"),
    ("cryptography", "cryptography（数据库解密）"),
    ("zstandard", "zstandard（红包等压缩内容）"),
    ("cv2", "opencv（OCR 相关）"),
    ("pyperclip", "剪贴板"),
]


def main() -> int:
    print(f"PYLIBS = {PYLIBS}")
    if not PYLIBS.exists():
        print("[失败] .pylibs 不存在，先跑： python pkg_fetch.py <包名...>")
        return 2

    sys.path.insert(0, str(PYLIBS))
    # pywin32 的 win32ui.pyd 等扩展模块放在 wheel 顶层的 pythonwin/ 目录里，
    # 正规安装靠 pywin32.pth 挂路径；这里是手工解包，得自己挂。
    for sub in ("pythonwin", "win32", "win32/lib", "win32comext"):
        d = PYLIBS / sub
        if d.exists():
            sys.path.insert(0, str(d))
    for d in DLL_DIRS:
        if d.exists():
            try:
                import os
                os.add_dll_directory(str(d))       # Python 3.8+ 加载扩展模块 DLL 的正规入口
                print(f"[DLL] 已加入搜索路径：{d.name}")
            except Exception as exc:
                print(f"[DLL] {d} 加入失败：{exc!r}")

    failed: list[str] = []
    print("\n== 依赖 import 检查 ==")
    for mod, note in CHECKS:
        try:
            importlib.import_module(mod)
            print(f"  [OK]   {mod:14} {note}")
        except Exception as exc:
            failed.append(mod)
            print(f"  [失败] {mod:14} {note} -> {type(exc).__name__}: {exc}")

    print("\n== wechatauto 本体 ==")
    try:
        import wechatauto
        print(f"  [OK] 版本 {getattr(wechatauto, '__version__', '未知')}")
    except Exception:
        print("  [失败] import wechatauto 失败：")
        print("  " + traceback.format_exc().replace("\n", "\n  ")[-1500:])
        return 1

    for factory, note in (("WeChatDB", "数据库读取（监听用）"), ("WeChat", "客户端封装（发送用）")):
        obj = getattr(wechatauto, factory, None)
        print(f"  [{'OK' if obj else '失败'}] 导出 {factory} —— {note}")
        if obj is None:
            failed.append(factory)

    print("\n== 结论 ==")
    if failed:
        print(f"  有 {len(failed)} 项未通过：{', '.join(failed)}")
        print("  处理办法：python pkg_fetch.py <缺失的包> --index https://mirrors.aliyun.com/pypi/simple")
        return 1
    print("  全部通过：wechatauto 及其依赖在离线 .pylibs 形态下可用。")
    print("  实际收发消息还需要：微信客户端已登录目标小号 + 有 GUI 会话（UI 自动化依赖前台窗口）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
