"""图片链路探测：一步一步问「这张图卡在哪」，而不是靠猜。

## 为什么需要这个工具

`MediaDownloader.download_image()` 在**六种情况下都返回 `None`**，而且一个字的解释都不给：
消息行找不到、类型不是 3、取不到 md5、附件目录里三档全没有、只要原图但本机没原图、
解密失败。真机上的表现却完全一样——机器人回一句「图没加载出来」，
日志里既没有 error 也没有文件名，**看不出是"没下过"还是"下过但解不开"**。

这个脚本把中间状态全打出来：
    1. 会话里最近 N 条图片消息（local_id / 时间 / md5）
    2. 每条在本机的副本档位（`_h.dat` 原图 / `.dat` 压缩版 / `_t.dat` 缩略图）与大小
    3. `--download` 时真的调一次 `download_media()`，并把结果转码成视觉模型能吃的 jpg

## 跑法（**必须在沙箱外的终端**：要读 Weixin.exe 进程内存拿数据库密钥）

    python probe_image.py
    python probe_image.py --chat 11111111111@chatroom --limit 5
    python probe_image.py --download --limit 3
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYLIBS = ROOT / ".pylibs"


def prepare_path() -> None:
    for path in (ROOT, PYLIBS, PYLIBS / "pythonwin", PYLIBS / "win32", PYLIBS / "win32" / "lib", PYLIBS / "win32comext"):
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    for dll_dir in (PYLIBS / "pywin32_system32", PYLIBS / "win32"):
        if dll_dir.exists():
            try:
                os.add_dll_directory(str(dll_dir))
            except Exception:
                pass


def _size_of(value) -> float:
    """`tiers` 里可能是 (path, bytes) 也可能是纯 bytes——两种都吃。"""
    raw = value[1] if isinstance(value, (tuple, list)) and len(value) > 1 else value
    try:
        return float(raw)
    except (TypeError, ValueError):
        return -1.0


def show_tiers(tiers: dict) -> str:
    if not tiers:
        return "（本机一档都没有 = 微信还没把这张图下下来）"
    order = ("original", "mid", "thumb")
    label = {"original": "_h.dat 原图", "mid": ".dat 压缩版", "thumb": "_t.dat 缩略图"}
    parts = []
    for key in order:
        if key in tiers:
            parts.append(f"{label[key]} {_size_of(tiers[key]) / 1024:.1f} KB")
    for key in tiers:
        if key not in order:
            parts.append(f"{key} {_size_of(tiers[key]) / 1024:.1f} KB")
    return " / ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="图片链路探测（只读，不会发消息）")
    ap.add_argument("--chat", default="11111111111@chatroom", help="会话 id（群 xxx@chatroom 或 wxid）")
    ap.add_argument("--limit", type=int, default=5, help="看最近几条图片消息")
    ap.add_argument("--download", action="store_true", help="真的调一次下载并转码（默认只看状态）")
    ap.add_argument("--adapter", action="store_true",
                    help="用**桥接适配器自己的** download_image 走一遍（最贴近真机的那条路）")
    ap.add_argument("--describe", action="store_true", help="取到图后再让视觉模型描述一次")
    ap.add_argument("--config", default="state/_grouptest.json", help="--describe 用的配置")
    ap.add_argument("--save-dir", default=str(ROOT / "state" / "images"), help="下载落盘目录")
    args = ap.parse_args()
    prepare_path()

    # 这个工具会在中文 Windows 的 cp936 控制台里打印 ❌/✅/⇒（GBK 里都没有）：
    # 不托住输出流的话，诊断到一半直接 UnicodeEncodeError 退出。
    from wechat_bridge.console import install as _install_console

    _install_console()

    from wechatauto import WeChatDB
    from wechatauto.media import MediaDownloader

    print(f"== 连接数据库（要读 Weixin.exe 进程内存）==")
    db = WeChatDB()
    print(f"账号目录：{getattr(db, 'account_dir', '(未知)')}")

    media = MediaDownloader(db, save_dir=args.save_dir)

    print(f"\n== 会话 {args.chat} 的图片消息（最近 {args.limit} 条）==")
    statuses = media.list_image_status(args.chat, limit=args.limit)
    if not statuses:
        print("  （这个会话查不到图片消息：群 id 写错了，或最近确实没人发图）")
        return 1
    for st in statuses:
        print(f"  local_id={st.get('local_id')}  时间={st.get('create_time')}  "
              f"md5={(st.get('md5') or '?')[:12]}  shard={st.get('shard')}")
        print(f"      档位：{show_tiers(st.get('tiers') or {})}")
        if not st.get("available"):
            print(f"      ⇒ 取不到：reason={st.get('reason')}")

    print("\n== 逐条实取（download_media → 转码）==")
    from wechat_bridge.ffmpeg import status as ffmpeg_status, wxgf_to_jpg
    from wechat_bridge.imagefmt import describe_unknown, sniff

    print(f"  ffmpeg：{ffmpeg_status()}")

    def describe(path: Path) -> None:
        if not args.describe:
            return
        from wechat_bridge.adapters.vision_openai_compat import make_client
        from wechat_bridge.config import load_config

        cfg = load_config(args.config)
        client = make_client(cfg.vision)
        if client is None:
            print("      视觉后端没启用（vision.enabled=false），跳过描述")
            return
        try:
            print(f"      视觉模型看到：{client.describe(path)}")
        except Exception as exc:
            print(f"      ❌ 视觉模型失败：{exc!r}")

    if args.adapter:
        # 最贴近真机的一条：直接用桥接适配器的 download_image（含 local_id 兜底与重试）
        import types

        from wechat_bridge.adapters.hook_wechatauto import WechatautoHookAdapter

        adapter = WechatautoHookAdapter(accept_images=True, image_dir=args.save_dir)
        adapter._wx = types.SimpleNamespace(_db=db)          # noqa: SLF001 —— 只借它拿 _db
        for st in statuses:
            local_id = st.get("local_id")
            if local_id is None:
                continue
            got = adapter.download_image(args.chat, None, local_id)
            size = Path(got).stat().st_size if got else -1
            print(f"  local_id={local_id}：adapter.download_image → {got}  {size} B")
            if adapter.image_error:
                print(f"      失败原因：{adapter.image_error}")
            if got:
                describe(Path(got))
            break                                        # 只看最新一条
        return 0

    for st in statuses:
        local_id = st.get("local_id")
        if local_id is None:
            continue
        if not args.download:
            print(f"  local_id={local_id}：跳过（加 --download 才真的取）")
            continue
        got = media.download_media(args.chat, int(local_id), save_dir=args.save_dir)
        if not got:
            print(f"  local_id={local_id}：❌ download_media 返回 None（原因见上面的档位/reason）")
            continue
        path = Path(got)
        size = path.stat().st_size if path.exists() else -1
        print(f"  local_id={local_id}：✅ 落盘 {path.name}  {size / 1024:.1f} KB")
        head = path.read_bytes()[:32] if path.exists() else b""
        print(f"      头 8 字节：{head[:8].hex(' ')}  判定={sniff(head) or describe_unknown(head)}")
        if head[:4] == b"wxgf":
            jpg = wxgf_to_jpg(path, Path(args.save_dir) / "_convert")
            print(f"      wxgf 转码：{jpg if jpg else '❌ 失败（看 ffmpeg）'}")
            if jpg:
                describe(jpg)
        elif head[:3] == b"\xff\xd8\xff":
            print("      已经是 jpg，模型能直接读")
            describe(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
