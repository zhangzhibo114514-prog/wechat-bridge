"""微信 4.x 连通性探测：分四步定位「到底卡在哪」。

设计原则：**每一步只做一件事，失败就停**，并明确标出是哪一类问题——
是环境（沙箱挡了进程枚举）、是客户端（没开/没登录）、还是库/代码。

四步：
    1. 找数据目录与账号        （只读文件系统）
    2. 取数据库密钥并解密      （要读 Weixin.exe 进程内存）
    3. 连 UI（WeChatGUI）      （要主窗口可见、不能最小化到托盘）
    4. 【可选】发一条到文件传输助手（--send "内容"，会在你屏幕上操作微信）

跑法（**必须在沙箱外的终端**，否则第 2 步必然失败）：
    python probe_wechat.py
    python probe_wechat.py --send "连通性测试，请忽略"
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYLIBS = ROOT / ".pylibs"


def prepare_path() -> None:
    for path in (PYLIBS, PYLIBS / "pythonwin", PYLIBS / "win32", PYLIBS / "win32" / "lib", PYLIBS / "win32comext"):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    for dll_dir in (PYLIBS / "pywin32_system32", PYLIBS / "win32"):
        if dll_dir.exists():
            try:
                os.add_dll_directory(str(dll_dir))
            except Exception:
                pass


def step(label: str) -> None:
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")


def main() -> int:
    ap = argparse.ArgumentParser(description="微信 4.x 连通性探测")
    ap.add_argument("--send", default=None, help="向文件传输助手发一条文本（会在屏幕上操作微信，慎用）")
    args = ap.parse_args()
    prepare_path()

    step("[1/4] 数据目录与账号")
    try:
        from wechatauto.db import auto_detect_db_dir, list_accounts

        db_dir = auto_detect_db_dir()
        print(f"数据目录：{db_dir}")
        accounts = list_accounts()
        if not accounts:
            print("[停] 没有可用账号目录 —— 客户端未登录，或数据目录指向别处。")
            return 1
        for acc in accounts:
            print(f"  账号 {acc.get('wxid')}  目录={Path(acc.get('path', '')).name}")
    except Exception:
        print("[停] 目录探测失败：")
        print(traceback.format_exc())
        return 1

    step("[2/4] 取数据库密钥并解密（需要读 Weixin.exe 进程内存）")
    db = None
    try:
        from wechatauto import WeChatDB

        db = WeChatDB()
        info = db.get_self_info()
        self_wxid = info.get("username") or ""
        print(f"自身账号：{info.get('nick_name') or '(无昵称)'} / {self_wxid}")
        sessions = db.get_sessions(limit=5)
        print(f"解密成功，读到 {len(sessions)} 个会话（示例）：")
        for s in sessions[:5]:
            print(f"   {s.get('username', '')[:26]:<28} 未读={s.get('unread')} {(s.get('summary') or '')[:24]}")
        print("\n【下一步要用到的白名单 id】")
        print(f"  你自己的账号：{self_wxid}")
        print(f"  文件传输助手：filehelper（如果会话列表里能看到它，用它返回的 username）")
        print("  把要代回的**会话 id** 记下来，稍后执行：")
        print(f"    python make_config.py --allow {self_wxid or '<对方的wxid>'}")
    except Exception as exc:
        print(f"[停] 取密钥/解密失败：{type(exc).__name__}: {exc}")
        print("\n常见原因与对策：")
        print("  1) 提示「未检测到 Weixin.exe」：")
        print("     a. 微信没开 → 打开并登录；")
        print("     b. **在 DSH 沙箱里跑** → 沙箱屏蔽了进程枚举（tasklist/CIM 均被拒），")
        print("        必须在你自己的终端里运行本脚本（或 run_bridge.cmd --doctor）。")
        print("  2) 提示「数据库解密结果校验失败」：微信正在写库，等几秒重试。")
        return 2

    step("[3/4] 连 UI（需要微信主窗口可见，不能最小化到托盘）")
    try:
        from wechatauto import WeChatGUI

        gui = WeChatGUI()
        print(f"已连上主窗口 hwnd={gui.main_hwnd}")
    except Exception as exc:
        print(f"[停] UI 连接失败：{type(exc).__name__}: {exc}")
        print("对策：确认微信窗口没有最小化到托盘、没有被锁屏挡住，然后重试。")
        return 3

    if not args.send:
        step("[4/4] 跳过发送（未加 --send）")
        print("连接性已全部验证通过。要验证发送，请加：--send \"测试内容\"")
        return 0

    step(f"[4/4] 发送到「文件传输助手」：{args.send!r}")
    try:
        resp = gui.send_msg(args.send, "文件传输助手", verify=True)
        status = resp.get("status") if isinstance(resp, dict) else getattr(resp, "status", "")
        message = resp.get("message") if isinstance(resp, dict) else getattr(resp, "message", "")
        print(f"结果：status={status!r} message={message!r}")
        if str(status) == "成功":
            print("请到微信「文件传输助手」里确认是否收到这条消息。")
            return 0
        print("[注意] 未返回成功——可能是窗口不在前台、剪贴板被占用，或数据库未确认落库。")
        return 4
    except Exception:
        print("[停] 发送异常：")
        print(traceback.format_exc())
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
