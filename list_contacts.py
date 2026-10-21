"""列出最近的会话 id —— 给白名单挑 id 用。

为什么需要它：白名单**只认 id**（`wxid_xxx` / `xxx@chatroom` / `filehelper`），不认昵称。
昵称是对方随时能改的字段，拿昵称授权等于把钥匙交给对方（见 bridge.py `_match_contact_rule` 的注释）。
但 id 本身不可读，所以这里把「id ↔ 昵称」对照着打出来，你照着复制就行。

跑法（**必须在沙箱外的终端**，第 2 步要读 Weixin.exe 进程内存取数据库密钥）：
    python list_contacts.py                  # 最近 20 个会话
    python list_contacts.py --limit 50
    python list_contacts.py --search 张三     # 按昵称/备注/微信号搜联系人
    python list_contacts.py --allow-lines     # 只输出可直接粘贴的 make_config 命令

本脚本**只读**：不连 UI、不发消息、不碰剪贴板。
"""

from __future__ import annotations

import argparse
import os
import sys
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
    ap = argparse.ArgumentParser(description="列出最近会话的 id（白名单用）")
    ap.add_argument("--limit", type=int, default=20, help="最多列出多少个会话，默认 20")
    ap.add_argument("--search", default=None, help="按昵称/备注/微信号搜索联系人（不列会话）")
    ap.add_argument("--allow-lines", action="store_true", help="只输出可直接粘贴的 make_config 命令")
    args = ap.parse_args()

    prepare_path()
    try:
        from wechatauto import WeChatDB
    except Exception as exc:
        print(f"[停] 加载 wechatauto 失败：{type(exc).__name__}: {exc}")
        print("  对策：确认 .pylibs 已由 pkg_fetch.py 解包完整（python smoke_wechatauto.py）")
        return 1

    try:
        db = WeChatDB()
    except Exception as exc:
        print(f"[停] 取数据库密钥失败：{type(exc).__name__}: {exc}")
        print("  常见原因：①微信没开 ②在 DSH 沙箱里跑（进程枚举被拒）")
        print("  对策：在你自己的终端里运行本脚本（或 run_bridge.cmd --doctor）")
        return 2

    try:
        info = db.get_self_info()
    except Exception:
        info = {}
    self_wxid = info.get("username") or ""
    print(f"自身账号：{info.get('nick_name') or '(无昵称)'} / {self_wxid or '(未知)'}")

    # 搜索模式：直接给联系人（更适合"我知道要找谁，但不知道他的 wxid"）
    if args.search:
        try:
            hits = db.search_contact(args.search)
        except Exception as exc:
            print(f"[停] 搜索失败：{type(exc).__name__}: {exc}")
            return 3
        print(f"\n匹配「{args.search}」的联系人 {len(hits)} 个：")
        for hit in hits:
            label = hit.get("remark") or hit.get("nick_name") or "(未命名)"
            print(f"  {hit.get('username', ''):<34} {label}")
        if hits:
            print("\n要代回谁就把 id 加进白名单：")
            print("  python make_config.py " + " ".join(f"--allow {h.get('username')}" for h in hits[:5]))
        return 0

    try:
        sessions = db.get_sessions(limit=args.limit)
    except Exception as exc:
        print(f"[停] 读会话列表失败：{type(exc).__name__}: {exc}")
        print("  提示：微信正在写库时解密结果可能校验失败，等几秒重试。")
        return 4

    if not sessions:
        print("\n会话列表是空的 —— 这个账号还没跟任何人聊过？")
        return 0

    print(f"\n最近 {len(sessions)} 个会话（按最后活跃时间排序）：")
    print(f"  {'会话 id':<34} {'未读':>4}  {'对方/群':<14} 最后一条")
    print("  " + "-" * 92)
    ids: list[str] = []
    for s in sessions:
        chat_id = str(s.get("username") or "")
        if not chat_id:
            continue
        ids.append(chat_id)
        name = str(s.get("last_sender") or "")[:14]
        summary = str(s.get("summary") or "").replace("\n", " ")[:34]
        print(f"  {chat_id:<34} {s.get('unread', 0):>4}  {name:<14} {summary}")

    print("\n【怎么用】白名单只认 id。把要代回的会话 id 挑出来，然后：")
    print("  python make_config.py --allow <id1> --allow <id2> --quiet 23:00-08:00")
    if args.allow_lines:
        print("\n-- 可直接粘贴 --")
        for chat_id in ids:
            print(f"python make_config.py --allow {chat_id}")
    print("\n提醒：id 里带 @chatroom 的是**群**；群消息默认不代回（allow_groups=false）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
