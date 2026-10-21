"""图片理解链路的离线自测（不需要微信、不需要网络、不需要真 key）。

跑法：python vision_selftest.py

验三件事：
  1. **视觉客户端**（`VisionCompatClient`）——用本机假端点跑真实 HTTP，包括各种异常响应
     必须**报错而不是假装成功**；
  2. **降级路径**——没配后端 / 取不到图 / 后端报错时，必须**如实说"看不到"**，
     绝不能返回空串让模型以为"图里什么都没有"；
  3. **描述要再过一遍敏感词**——图里的二维码/银行卡被描述成文字，同样该拦下。
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import shutil
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fake_llm_server import REPLY_TEXT, Handler                              # noqa: E402
from wechat_bridge.adapters.base import Message                              # noqa: E402
from wechat_bridge.adapters.vision_openai_compat import (                    # noqa: E402
    VisionCompatClient,
    make_client,
)

PORT = 8898
BASE = f"http://127.0.0.1:{PORT}"
SANDBOX = ROOT / "state" / "_visiontest"

# 1×1 的合法 PNG（最小可用图，避免依赖任何图像库）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return ok


def wait_ready(timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE}/v1/models", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


class StubHook:
    """只实现 download_image 的假 hook——用于隔离测试桥接的取图分支。"""

    def __init__(self, path=None, boom: bool = False):
        self.path, self.boom = path, boom

    def download_image(self, chat_id, raw, local_id=None):
        if self.boom:
            raise RuntimeError("假装取图炸了")
        return str(self.path) if self.path else None


class StubVision:
    def __init__(self, text: str = "", boom: bool = False):
        self.text, self.boom = text, boom

    def describe(self, path, prompt=None):
        if self.boom:
            raise RuntimeError("假装视觉炸了")
        return self.text


def image_message() -> Message:
    return Message(chat_id="g@chatroom", sender_id="wxid_a", sender_name="阿伟",
                   text="[图片]", is_group=True, kind="image")


def make_bridge():
    import bridge as B

    cfg = B._default_config()
    cfg.storage.audit_path = "state/_visiontest/audit.jsonl"
    cfg.storage.state_path = "state/_visiontest/state.json"
    cfg.storage.draft_dir = "state/_visiontest/drafts"
    return B, B.Bridge(cfg, force_demo=True)


def test_client() -> bool:
    ok = True
    png = SANDBOX / "ok.png"
    png.write_bytes(PNG_1PX)
    empty = SANDBOX / "empty.png"
    empty.write_bytes(b"")
    huge = SANDBOX / "huge.png"
    huge.write_bytes(b"x" * (9 * 1024 * 1024))

    print("-- 正常路径 --")
    client = VisionCompatClient(base_url=BASE, api_key="k", model="fake-vl", timeout_sec=10)
    text = client.describe(png)
    ok &= check("拿到描述文本", text == REPLY_TEXT, repr(text))

    print("\n-- 异常响应必须报错，不能假装成功 --")
    for label, path in (("缺 choices", "/bad/no-choices"),
                        ("不是 JSON", "/bad/malformed"),
                        ("HTTP 500", "/bad/500")):
        bad = VisionCompatClient(base_url=BASE, api_key="k", model="m", path=path, timeout_sec=10)
        try:
            got = bad.describe(png)
            ok &= check(label, False, f"居然没抛异常，返回 {got!r}")
        except RuntimeError as exc:
            ok &= check(label, True, str(exc)[:60])
        except Exception as exc:
            ok &= check(label, False, f"抛的是 {type(exc).__name__}，应统一为 RuntimeError")

    print("\n-- 本地图片问题也必须是 RuntimeError，不能静默返回空 --")
    for label, target in (("文件不存在", SANDBOX / "nope.png"),
                          ("空文件", empty),
                          ("超大文件", huge)):
        try:
            client.describe(target)
            ok &= check(label, False, "居然没抛异常")
        except RuntimeError as exc:
            ok &= check(label, True, str(exc)[:50])
        except Exception as exc:
            ok &= check(label, False, f"抛的是 {type(exc).__name__}")

    print("\n-- 格式按字节判，不按扩展名（真机 wxgf 事故）--")
    # 关键回归：扩展名是 .png、内容是微信 wxgf(HEVC) 容器。
    # 旧行为按扩展名贴 image/png 发出去 ⇒ 远端 400 unsupported image，本地什么都看不出。
    # 新行为必须**本地就抛 RuntimeError**，且不能真的发请求（base_url 指向关闭的端口）。
    fake_png = SANDBOX / "actually-hevc.png"
    fake_png.write_bytes(b"wxgf" + b"\x00" * 12 + b"\x00\x00\x00\x01\x40\x01" + b"\xff" * 40)
    offline = VisionCompatClient(base_url="http://127.0.0.1:1", api_key="k", model="m", timeout_sec=3)
    try:
        offline.describe(fake_png)
        ok &= check("假图片必须被拦下", False, "居然发出去/返回了")
    except RuntimeError as exc:
        msg = str(exc)
        ok &= check("假图片被本地拦下（不是远端 400）", "不是模型能读的图片格式" in msg, msg[:70])
        ok &= check("错误里说清它其实是 wxgf", "wxgf" in msg, msg[:70])
        ok &= check("错误里给出补法（ffmpeg）", "ffmpeg" in msg, msg[:70])
    except Exception as exc:
        ok &= check("假图片抛 RuntimeError", False, f"抛的是 {type(exc).__name__}")

    from wechat_bridge.imagefmt import describe_unknown, mime_of, sniff          # noqa: E402

    print("\n-- 四种允许格式的字节判定 --")
    gif = b"GIF89a" + b"\x00" * 20
    webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    for label, blob, expect in (("png", PNG_1PX, "png"), ("jpeg", jpeg, "jpeg"),
                                ("gif", gif, "gif"), ("webp", webp, "webp"),
                                ("未知", b"hello world??", None)):
        got = sniff(blob)
        ok &= check(f"sniff {label}", got == expect, f"{got!r}")
    ok &= check("mime 与 sniff 一致", mime_of("jpeg") == "image/jpeg", mime_of("jpeg"))
    ok &= check("未知字节讲得清是什么", "wxgf" in describe_unknown(b"wxgf" + b"\x00" * 10))

    print("\n-- 开关与密钥解析 --")
    class Cfg:
        enabled = False
        adapter = ""
        options: dict = {}

    ok &= check("enabled=false 时不造客户端（默认关）", make_client(Cfg()) is None)

    Cfg.enabled = True
    Cfg.adapter = "wechat_bridge.adapters.vision_openai_compat:VisionCompatClient"
    Cfg.options = {"base_url": BASE, "model": "m", "api_key": "env:NO_SUCH_KEY_HERE"}
    ok &= check("钉错的密钥名不静默换别的 key",
                make_client(Cfg).api_key == "")
    return ok


def test_bridge_paths() -> bool:
    ok = True
    B, bridge = make_bridge()
    png = SANDBOX / "ok.png"

    print("\n-- 桥接里的四条降级路径（都要如实说「看不到」）--")
    saved_vision, saved_hook = bridge.vision, bridge.hook
    try:
        bridge.vision = None
        out = bridge._enrich_image(image_message())
        ok &= check("没配后端 ⇒ 明说看不到", "没有配视觉后端" in out, out)

        bridge.vision = StubVision("一只猫在键盘上")
        bridge.hook = StubHook(path=None)
        out = bridge._enrich_image(image_message())
        ok &= check("取不到原图 ⇒ 明说取不到", "取不到原图" in out, out)

        bridge.hook = StubHook(boom=True)
        out = bridge._enrich_image(image_message())
        ok &= check("取图抛异常 ⇒ 不崩、明说", "取不到原图" in out, out)

        bridge.hook = StubHook(path=png)
        bridge.vision = StubVision(boom=True)
        out = bridge._enrich_image(image_message())
        ok &= check("后端报错 ⇒ 明说没看懂", "没看懂" in out, out)

        bridge.vision = StubVision("一只橘猫趴在键盘上")
        out = bridge._enrich_image(image_message())
        ok &= check("正常 ⇒ 描述进上下文", out == "[图片：一只橘猫趴在键盘上]", out)
        ok &= check("描述不会被当成纯文本丢掉", out.startswith("[图片："))
    finally:
        bridge.vision, bridge.hook = saved_vision, saved_hook

    print("\n-- 描述必须再过一遍敏感词 --")
    for text, expect in (("[图片：这是一张银行卡的正反面]", True),
                         ("[图片：屏幕上是一个登录验证码]", True),
                         ("[图片：一张风景照]", False)):
        hit = bridge._sensitive_hit(text)
        ok &= check(f"{'该拦' if expect else '不该拦'}：{text[4:16]}…",
                    bool(hit) == expect, f"命中={hit!r}")

    print("\n-- 非图片消息不受影响 --")
    plain = Message(chat_id="c", sender_id="s", sender_name="n", text="你好")
    ok &= check("文本消息原样返回（不会被当成图片去取图）",
                bridge._enrich_image(plain) == "你好", repr(bridge._enrich_image(plain)))
    return ok


def test_wxgf() -> bool:
    """wxgf(HEVC) → jpg 的转码链路，以及"没 ffmpeg 时怎么说人话"。

    真机事故（2026-10-18）：图片落盘成 `.wxgf`，视觉接口回 400 unsupported image。
    所以这里既验**能转**，也验**转不了时错误信息能指路**——后者才是当初最缺的东西。
    """
    import subprocess

    from wechat_bridge import ffmpeg as ff

    ok = True
    print("\n-- wxgf(HEVC) 转 jpg --")
    exe = ff.find()
    if not exe:
        print("  [跳过] 本机没有 ffmpeg（这正是「识图永远看不到」的那个原因）")
        return ok
    print(f"  ffmpeg: {exe}")
    work = SANDBOX / "wxgf"
    work.mkdir(parents=True, exist_ok=True)
    hevc = work / "probe.hevc"
    gen = subprocess.run(
        [exe, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=1",
         "-frames:v", "1", "-c:v", "libx265", "-x265-params", "log-level=error",
         "-f", "hevc", str(hevc)],
        capture_output=True, text=True,
    )
    if gen.returncode != 0 or not hevc.is_file():
        print(f"  [跳过] 这个 ffmpeg 编不出 HEVC 样本：{(gen.stderr or '')[:80]}")
        return ok
    # 微信容器头长度随版本变化 ⇒ 故意塞一段垃圾头，验证"从第一个起始码开始截"
    wxgf = work / "chat_1.wxgf"
    wxgf.write_bytes(b"wxgf" + b"\x13\x00\x02\x05" + b"\x00" * 18 + hevc.read_bytes())
    jpg = ff.wxgf_to_jpg(wxgf, work / "_convert")
    from wechat_bridge.imagefmt import sniff

    ok &= check("wxgf 转出了 jpg", jpg is not None and jpg.suffix == ".jpg", str(jpg))
    ok &= check("转出来真的是 jpeg 字节", bool(jpg) and sniff(jpg.read_bytes()) == "jpeg")
    ok &= check("中间产物已清理（不留 .hevc）", not list((work / "_convert").glob("*.hevc")))
    ok &= check("非 wxgf 的垃圾不会被当成图片转", ff.wxgf_to_jpg(SANDBOX / "ok.png", work / "_convert") is None)

    print("\n-- 没有 ffmpeg 时必须说人话（不能静默返回原始 HEVC）--")
    from wechat_bridge.adapters import hook_wechatauto as hw

    adapter = hw.WechatautoHookAdapter(image_dir=str(work / "img"))
    saved = hw.find_ffmpeg
    try:
        hw.find_ffmpeg = lambda *_a, **_k: None          # 假装本机没装 ffmpeg
        got = adapter._model_readable(str(wxgf))
        ok &= check("没 ffmpeg ⇒ 返回 None（不把 HEVC 当图片交出去）", got is None, str(got))
        ok &= check("原因里点名 wxgf", "wxgf" in adapter.image_error, adapter.image_error[:60])
        ok &= check("原因里给出补法", "ffmpeg" in adapter.image_error, adapter.image_error[:60])
    finally:
        hw.find_ffmpeg = saved

    adapter.image_error = ""
    got = adapter._model_readable(str(wxgf))
    ok &= check("有 ffmpeg ⇒ 返回 jpg 路径", bool(got) and str(got).endswith(".jpg"), str(got))

    junk = work / "junk.img"
    junk.write_bytes(b"\x01\x02\x03\x04" * 10)
    adapter.image_error = ""
    ok &= check("垃圾文件 ⇒ None + 说清不是图片",
                adapter._model_readable(str(junk)) is None and "不是图片" in adapter.image_error,
                adapter.image_error[:60])
    adapter.image_error = ""
    ok &= check("已经是 jpg 就原样返回（不重复转码）",
                adapter._model_readable(str(jpg)) == str(jpg))

    print("\n-- 取图：local_id 兜底 + 优先中图 + 短退避重试 --")
    thumb = work / "thumb.jpg"                  # 缩略图也做成合法 jpg（真机就是 jpg 缩略图）
    thumb.write_bytes(jpg.read_bytes())
    adapter._wx = object()                      # 假装已连上微信
    calls = {"n": 0, "tiers": []}

    class TieredMedia:
        """第 1 次只要 md 就扑空、第 2 次给 md；记录每次请求的 tier。"""

        def download_image(self, chat, local_id, save_dir=None, tier=None):
            calls["n"] += 1
            calls["tiers"].append(tier)
            if calls["n"] < 2:
                return None
            return str(jpg)

        def download_media(self, chat, local_id, save_dir=None):
            calls["tiers"].append("media")
            return str(jpg)

        def _img_md5(self, row):
            return None

        @staticmethod
        def _image_files(user, md5):
            return {}

    adapter._media = TieredMedia()
    got = adapter.download_image("g@chatroom", None, 287)      # raw=None，只给 local_id
    ok &= check("raw 为 None 时靠 local_id 仍然取得到", got is not None, str(got))
    ok &= check("**先要压缩版**（tier='mid'），不是抓到什么用什么",
                calls["tiers"][0] == "mid", str(calls["tiers"]))
    ok &= check("扑空会自动重试", calls["n"] >= 2, f"尝试 {calls['n']} 次")
    ok &= check("取到的是模型能读的 jpg", bool(got) and str(got).endswith(".jpg"), str(got))

    class ThumbOnlyMedia:
        """只有缩略图、中图永远等不到 —— 等够了要退回去收下缩略图，不能一直空手。"""

        def download_image(self, chat, local_id, save_dir=None, tier=None):
            return str(thumb) if tier is None else None

        def _img_md5(self, row):
            return None

        @staticmethod
        def _image_files(user, md5):
            return {}

    adapter._media = ThumbOnlyMedia()
    got = adapter.download_image("g@chatroom", None, 289)
    ok &= check("中图等不到 ⇒ 退回用缩略图（不空手）", got == str(thumb), str(got))

    class DeadMedia:
        def download_media(self, chat, local_id, save_dir=None):
            return None

    adapter._media = DeadMedia()
    adapter.image_error = ""
    ok &= check("怎么都取不到 ⇒ None + 原因写清", adapter.download_image("g@chatroom", None, 288) is None)
    ok &= check("原因里带上诊断信息（试了几次 / 档位）",
                "试了" in adapter.image_error and "档位" in adapter.image_error,
                adapter.image_error[:70])
    adapter._media = None
    return ok


def main() -> int:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="fake-vl").start()
    ok = True
    try:
        if not wait_ready():
            print("假模型服务没起来，测试中止")
            return 1
        ok &= test_client()
        ok &= test_bridge_paths()
        ok &= test_wxgf()
        print("\n" + "=" * 64)
        print("结论：" + ("图片理解链路自测通过" if ok else "存在失败项"))
        return 0 if ok else 1
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(SANDBOX, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
