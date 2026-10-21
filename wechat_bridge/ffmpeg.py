"""找 ffmpeg，并把微信的 `wxgf`（HEVC 容器）转成模型能看的 jpg。

## 为什么这是识图的**硬前置条件**

微信 4.x（本机 4.1）的普通图片在本地落盘是 `xxx.wxgf`：**wxgf 容器套 HEVC 裸流**。
- Pillow / opencv 解不了 HEVC；
- 云端视觉接口（DeepSeek/OpenAI 各家）一律拒收，返回 `400 unsupported image`。

真机事故（2026-10-18）：`accept_images` 开了、vision 也配了，日志里只有一句
`[视觉] 理解失败：... unsupported image`——因为库自带的 wxgf 转码是**静默兜底**：
找不到 ffmpeg 就把原始 HEVC 落盘、当作"图片"返回，桥接再把 HEVC 字节发给云模型。
失败发生在远端，本地看起来一切正常。

⇒ 所以 ffmpeg 的查找、转码、失败原因**都在这一层显式处理**，不依赖任何库的兜底：
找不到就直说"没有 ffmpeg，识图会一直是看不到"，而不是发一串模型看不懂的字节。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .imagefmt import sniff

# 用户可以用环境变量钉死路径（比改配置文件省事，也方便 CI）
ENV_VAR = "WECHAT_BRIDGE_FFMPEG"
# 找不到也别每条图都扫一遍磁盘；但过一会儿要重试（用户可能中途装了 ffmpeg）
CACHE_SEC = 300.0

_cache: dict[str, tuple[str, float]] = {}


def _candidates(explicit: str) -> list[str]:
    out: list[str] = []
    if explicit:
        out.append(explicit)                                   # ① 配置钉死
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        out.append(env)                                        # ② 环境变量
    found = shutil.which("ffmpeg")
    if found:
        out.append(found)                                      # ③ PATH
    try:                                                       # ④ imageio-ffmpeg 自带那份
        from .adapters.hook_wechatauto import PYLIBS, _ensure_pylibs_on_path

        _ensure_pylibs_on_path()
        del PYLIBS
        import imageio_ffmpeg

        out.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    here = Path(__file__).resolve().parents[1]                  # wechat-bridge/
    out.extend([                                               # ⑤ 常见安装位置
        str(here / "tools" / "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser(r"~\scoop\shims\ffmpeg.exe"),
    ])
    return out


def find(explicit: str = "") -> Optional[str]:
    """返回可用的 ffmpeg 路径；没有就返回 None（带 5 分钟缓存）。"""
    key = explicit or ""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[1] < CACHE_SEC:
        return hit[0] or None
    for cand in _candidates(explicit):
        if cand and Path(cand).is_file():
            _cache[key] = (cand, now)
            return cand
    _cache[key] = ("", now)
    return None


def status(explicit: str = "") -> str:
    """人话状态：给启动日志和 `check_env.py` 用。"""
    exe = find(explicit)
    if exe:
        return f"ffmpeg 可用：{exe}"
    return ("⚠ 没有 ffmpeg：微信图片是 wxgf(HEVC)，转不成 jpg 就**永远看不到图**。"
            "装一份（或 `python pkg_fetch.py imageio-ffmpeg`），"
            f"也可用 hook.options.ffmpeg_exe / 环境变量 {ENV_VAR} 指定路径")


def wxgf_to_jpg(wxgf_path: str | Path, work_dir: str | Path,
                explicit: str = "") -> Optional[Path]:
    """把 wxgf 转成同名 `.jpg`（只取第一帧）。失败返回 None，**不抛异常**。"""
    exe = find(explicit)
    if not exe:
        return None
    src_file = Path(wxgf_path)
    try:
        data = src_file.read_bytes()
    except OSError:
        return None
    if data[:4] != b"wxgf" and b"\x00\x00\x00\x01" not in data[:64]:
        return None
    # 容器头里夹着长度前缀的参数集，头长度随微信版本变化 ⇒ 从**第一个 Annex-B 起始码**截
    start = data.find(b"\x00\x00\x00\x01")
    if start < 0:
        return None
    work = Path(work_dir)
    hevc = work / (src_file.stem + ".hevc")
    frame = work / (src_file.stem + ".jpg")
    try:
        work.mkdir(parents=True, exist_ok=True)
        hevc.write_bytes(data[start:])
        proc = subprocess.run(
            [exe, "-y", "-v", "error", "-i", str(hevc),
             "-frames:v", "1", "-f", "image2", str(frame)],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0 or not frame.is_file():
            return None
        blob = frame.read_bytes()
    except Exception:
        return None
    finally:
        # 中间产物必删：群里一天几百张图，攒着会把磁盘喂满
        for leftover in (hevc, frame):
            try:
                leftover.unlink()
            except OSError:
                pass
    if sniff(blob) != "jpeg":
        return None
    out = src_file.with_suffix(".jpg")
    try:
        out.write_bytes(blob)
    except OSError:
        return None
    return out
