"""把群消息的**原始内容**打出来 —— 用来校准 @ 检测规则。

## 为什么需要这个

微信群里 @ 的存储形态**没有权威文档**，`wechat_bridge/at_detect.py` 里的规则是
按已知的两种形态（``<atuserlist>`` XML / 正文字面量 ``@昵称``）写的，但**没有在真机上验证过**，
因为开发这个功能时那个微信号还没有任何群。

规则不该靠猜。用法：

    1. 把机器人所在的号拉进一个群
    2. 在群里 @ 它一下
    3. 跑本脚本，看 `@ 线索` 那一行输出的是什么
    4. 如果和 at_detect.py 的假设不一致，改那个文件即可（改完跑 group_selftest.py）

## 跑法（**必须在沙箱外的终端**：取数据库密钥要读 Weixin.exe 进程内存）

    python dump_raw_group.py                    # 先列出所有群
    python dump_raw_group.py --group 12345678@chatroom --limit 5
    python dump_raw_group.py --group 家长群 --limit 5 --hex

只读：不连 UI、不发消息、不改任何文件。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYLIBS = ROOT / ".pylibs"

TYPE_NAMES = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "动画表情",
    48: "位置", 49: "文件/链接/卡片", 50: "音视频通话", 10000: "系统消息",
    11000: "动画表情", 8594229559345: "红包",
}


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


def type_name(local_type) -> str:
    """微信 4.x 是**复合类型码**（如 57<<32|49），低 32 位才是真实类型。"""
    if not isinstance(local_type, int):
        return f"类型{local_type}"
    for candidate in (local_type, local_type & 0xFFFFFFFF, (local_type & 0xFFFFFFFF) & 0xFF):
        if candidate in TYPE_NAMES:
            return TYPE_NAMES[candidate]
    return f"类型{local_type}"


def decode(raw) -> str:
    """message_content 是 bytes 时：先当 utf-8，失败再当 ZSTD 压缩的 XML。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        import zstandard as zstd

        return zstd.ZstdDecompressor().decompress(bytes(raw), max_output_size=200000).decode("utf-8", "ignore")
    except Exception:
        return f"<无法解码 {len(raw)} 字节>"


def list_groups(db) -> list[dict]:
    for rel, path, _ in db._db_files:                      # noqa: SLF001 —— 库未暴露公开接口
        if os.path.basename(path) != "contact.db":
            continue
        conn = db._open(rel)                               # noqa: SLF001
        try:
            rows = conn.execute(
                "SELECT username, nick_name, remark FROM contact WHERE username LIKE '%@chatroom'"
            ).fetchall()
        finally:
            conn.close()
        return [{"wxid": r[0], "name": r[2] or r[1] or r[0]} for r in rows]
    return []


def raw_messages(db, chat_id: str, limit: int) -> list[dict]:
    """取该群最新 N 条**原始行**（保留 message_content 的原始字节）。

    同一会话的表可能分散在多个 message_*.db 分片里，所以要跨库聚合后按 sort_seq 排序。
    """
    md5hex = hashlib.md5(chat_id.encode()).hexdigest()
    table = f"Msg_{md5hex}"
    rows: list[dict] = []
    for rel in db._message_dbs():                          # noqa: SLF001
        conn = db._open(rel)                               # noqa: SLF001
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            cur = conn.execute(
                f"SELECT local_id, local_type, create_time, message_content, sort_seq FROM {table}")
            cols = [c[0] for c in cur.description]
            rows.extend(dict(zip(cols, r)) for r in cur.fetchall())
        finally:
            conn.close()
    rows.sort(key=lambda r: r.get("sort_seq") or 0)
    return rows[-limit:]


class _Tee:
    """把输出同时写到屏幕和日志文件。

    为什么需要：这个脚本经常被**双击**运行——Windows 会临时开一个控制台，
    脚本一结束窗口就关掉，屏幕上的东西瞬间消失。只往 stdout 打印 = 什么都留不下。
    """

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            try:
                stream.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass


def _configure_console() -> None:
    """让中文在 cmd 里也能正常显示（双击运行时控制台默认是 GBK）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _run(args) -> int:
    prepare_path()
    try:
        from wechatauto import WeChatDB

        db = WeChatDB()
    except Exception as exc:
        print(f"[停] 打不开微信数据库：{type(exc).__name__}: {exc}")
        print("  常见原因：①微信没开 ②在 DSH 沙箱里跑（进程枚举被拒，取不到密钥）")
        print("  对策：在你自己的终端（cmd/PowerShell 窗口）里运行本脚本")
        return 2

    try:
        me = db.get_self_info()
    except Exception:
        me = {}
    my_wxid = str(me.get("username") or "")
    print(f"本账号：{me.get('nick_name') or '(无昵称)'} / {my_wxid or '(未知)'}\n")

    groups = list_groups(db)
    if not groups:
        print("这个账号没有任何群。")
        print("⇒ 先让别的微信号把你拉进一个群，然后重跑本脚本。")
        return 0

    print(f"共 {len(groups)} 个群：")
    for g in groups:
        print(f"  {g['wxid']:<38} {g['name']}")
    print()

    if not args.group:
        print("用法：本脚本加 --group <wxid 或 群名关键词> 看某个群的原始消息。")
        return 0

    target = next((g for g in groups if g["wxid"] == args.group), None)
    if target is None:
        target = next((g for g in groups if args.group in g["name"]), None)
    if target is None:
        print(f"没找到群：{args.group!r}（上面列表里挑一个）")
        return 1

    print("=" * 78)
    print(f"群：{target['name']}  ({target['wxid']})")
    print("=" * 78)

    names = {}
    try:
        names = db.nickname_map()
    except Exception:
        pass

    rows = raw_messages(db, target["wxid"], args.limit)
    if not rows:
        print("这个群没有消息记录。")
        return 0

    hits = 0
    for row in rows:
        content = decode(row.get("message_content"))
        kind = type_name(row.get("local_type"))

        # 群文本消息的正文形如 `wxid_xxx:\n内容`，发送者藏在**正文前缀**里
        sender = ""
        body = content
        if content.startswith("wxid_"):
            head, _, rest = content.partition(":")
            if head.startswith("wxid_") and rest:
                sender = names.get(head, head)
                body = rest.lstrip("\r\n")

        has_atlist = "<atuserlist>" in content
        mark = "  ★@线索" if has_atlist else ""
        if has_atlist:
            hits += 1

        print(f"\n[{kind}] {sender or '(发送者未知)'}  local_id={row.get('local_id')}{mark}")
        print(f"  正文：{body.strip()[:200]}")
        if has_atlist:
            import re

            for m in re.finditer(r"<atuserlist>(.*?)</atuserlist>", content, re.S):
                print(f"  ★atuserlist = {m.group(1)!r}      ← at_detect.py 就是认这个")
        if args.hex and isinstance(row.get("message_content"), (bytes, bytearray)):
            head = bytes(row["message_content"])[:48]
            print(f"  原始字节头：{head.hex(' ')}")

    print("\n" + "-" * 78)
    if hits:
        print(f"本次看到 {hits} 条带 <atuserlist> 的消息 ⇒ at_detect.py 的 XML 判据是对的，无需修改。")
    else:
        print("本次没看到 <atuserlist>。两种可能：")
        print("  ① 这段消息里确实没有人 @ —— 请在群里 **@ 一下机器人**再重跑")
        print("  ② @ 是别的存法 —— 把上面那句「正文」原样发给我，我改 at_detect.py")
        print("     （改完跑 python group_selftest.py 确认没改坏别的判据）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="打印群消息原始内容（校准 @ 检测）")
    ap.add_argument("--group", default=None, help="群 wxid（xxx@chatroom）或群名关键词；不给则只列群")
    ap.add_argument("--limit", type=int, default=10, help="看最近多少条，默认 10")
    ap.add_argument("--hex", action="store_true", help="额外打印原始字节的十六进制头")
    ap.add_argument("--out", default="state/dump_raw_group.log",
                    help="同时把输出存一份到这个文件，默认 state/dump_raw_group.log")
    ap.add_argument("--no-pause", action="store_true",
                    help="跑完不停留（给 agent / 脚本调用用；双击运行不要加这个）")
    args = ap.parse_args()

    _configure_console()
    log_path = Path(args.out)
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 输出**同时**进屏幕和文件：双击运行时窗口会瞬间关掉，只打印等于什么都留不下。
    with log_path.open("w", encoding="utf-8") as log_file:
        saved = sys.stdout
        sys.stdout = _Tee(saved, log_file)
        try:
            code = _run(args)
        except Exception:
            import traceback
            traceback.print_exc()
            print("\n[出错] 上面是完整堆栈，可以整段发给我。")
            code = 1
        finally:
            sys.stdout = saved

    print(f"\n[输出已保存] {log_path}")
    print("  打不开日志的话，直接把这个文件拖给我就行。")

    # 双击运行时控制台一结束就关，所以停住等用户看完。
    # **只在真的有终端时才等**——agent/脚本用管道调用时 isatty() 为假，不会卡住。
    if not args.no_pause:
        try:
            if sys.stdin and sys.stdin.isatty():
                input("\n按回车键关闭这个窗口…")
        except Exception:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
