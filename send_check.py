"""发送链路自检：用**桥接自己的适配器**发一条消息，验证「发得出去 + 落库能核对」。

和 `probe_wechat.py --send` 的区别：那个直接调库，验的是**框架**；
这个走 `WechatautoHookAdapter.send()`，验的是**我们自己写的那层**——
包括它独有的两件事：
    1. 会话没缓存过就拒发（不去猜、不盲发）；
    2. 库报「成功」之后**回读数据库复核**（库的成功判据是像素启发式，窗口移位/锁屏/剪贴板被占都可能误判）。

默认目标是**文件传输助手**（给自己发，最安全）。要发真人请显式 --to。
跑法（**必须在沙箱外**，要读 Weixin.exe 进程内存）：
    python send_check.py                              # 默认发一条带时间戳的测试消息
    python send_check.py --text "连通性测试，请忽略"
    python send_check.py --to wxid_xxxx --text "..."  # 发真人（会真的发出去，谨慎）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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


def main() -> int:
    ap = argparse.ArgumentParser(description="用桥接的适配器发一条消息，验证发送链路")
    ap.add_argument("--to", default="filehelper", help="目标会话 id，默认 filehelper（给自己发）")
    ap.add_argument("--text", default=None, help="消息内容，默认是带时间戳的自检文本")
    ap.add_argument("--config", default="bridge_config.json", help="从哪个配置读 hook 选项")
    ap.add_argument("--no-verify", action="store_true", help="关掉发送后的数据库回读复核")
    ap.add_argument("--listen", action="append", default=None,
                    help="连接时必须注册的监听会话（适配器要求至少一个，默认沿用配置里的）")
    args = ap.parse_args()

    text = args.text or f"[自检] 发送链路测试 {time.strftime('%Y-%m-%d %H:%M:%S')}"

    options: dict = {}
    cfg_path = ROOT / args.config
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            options = dict(raw.get("hook", {}).get("options", {}))
            options.pop("_verify_note", None)
        except Exception as exc:
            print(f"[警告] 读配置失败（改用默认选项）：{exc}")
    # 适配器 connect() 要求「至少注册上一个监听会话」——它刻意不允许
    # "连上了但永远收不到消息"这种假成功。本脚本只发不收，所以沿用配置里的监听 id 即可。
    options["listen_uids"] = args.listen or options.get("listen_uids") or ["filehelper"]
    options["listen_chats"] = []
    if args.no_verify:
        options["verify_send"] = False

    prepare_path()
    try:
        from wechat_bridge.adapters.hook_wechatauto import WechatautoHookAdapter
    except Exception as exc:
        print(f"[停] 加载适配器失败：{type(exc).__name__}: {exc}")
        print("  对策：python smoke_wechatauto.py 看依赖链是否完整")
        return 1

    print(f"目标：{args.to}")
    print(f"内容：{text}")
    print(f"选项：verify_send={options.get('verify_send', True)} "
          f"send_timeout={options.get('send_timeout_sec', 90)}s")

    hook = WechatautoHookAdapter(**options)
    try:
        hook.connect()
    except Exception as exc:
        print(f"[停] 连接微信失败：{type(exc).__name__}: {exc}")
        print("  常见原因：①微信没开 ②窗口最小化到托盘 ③在沙箱里跑（进程枚举被拒）")
        return 2

    print(f"已连接：{hook.status().detail}")

    # 缓存预热：正常回复流程靠**入站消息**把 Chat 对象缓存起来；本脚本没有入站消息，
    # 所以显式预热一次，否则 send() 会按设计拒绝（"宁可不发也不发错人"）。
    if not hook.prime_chat(args.to):
        print("[停] 预热会话缓存失败，无法验证发送。")
        hook.close()
        return 3

    try:
        ok = bool(hook.send(args.to, text))
    except Exception as exc:
        print(f"[停] 发送抛异常：{type(exc).__name__}: {exc}")
        hook.close()
        return 3

    status = hook.status()
    print(f"\n发送结果：{'成功（库确认 + 回读复核一致）' if ok else '未确认（按未送达处理）'}")
    if status.extra:
        print(f"适配器统计：{status.extra}")
    else:
        print(f"适配器状态：{status.detail}")

    if ok:
        print("\n[结论] 发送链路通 —— 这一步验证的正是切 auto 之后要用的那条路。")
    else:
        print("\n[结论] 发送未确认。这是**有意的保守设计**：")
        print("        宁可当成没发出去，也不要在切 auto 后出现「以为发了其实没发」。")
        print("        排查顺序：微信窗口是否在前台且未最小化 → 是否锁屏 → 剪贴板是否被别的程序占着。")

    hook.close()
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
