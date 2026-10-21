"""按**文件头字节**判断一张图片到底是什么格式（只认视觉模型能吃的四种）。

## 为什么不能靠扩展名

本机真机事故（2026-10-18）：群里发的一张图，落盘文件名是 `.wxgf`——
微信 4.x 的普通图片在本机是 **wxgf 容器 + HEVC 裸流**，不是 jpg/png。
视觉客户端当时按扩展名猜 MIME（`mimetypes.guess_type` 认不出 `.wxgf` 就默认
`image/png`），于是把 45 KB 的 HEVC 字节贴上 `image/png` 标签发给云模型，
收到一个让人完全摸不着头脑的 **HTTP 400 unsupported image**。

⇒ 规矩：**先看字节，再定 MIME；看不懂就本地报错**，绝不猜一个格式硬发出去。
猜错的代价是"失败发生在远端"，日志里只剩一句模型侧的英文报错，排查要绕一大圈。

只认四种：jpeg / png / gif / webp —— 这四种是各家 VL 接口的公共交集
（bmp/tiff 在部分接口上会被拒，不认比认了更安全）。
"""

from __future__ import annotations

from typing import Optional

# 顺序无关，匹配靠签名
_MAGIC: tuple[tuple[str, str], ...] = (
    ("jpeg", "image/jpeg"),
    ("png", "image/png"),
    ("gif", "image/gif"),
    ("webp", "image/webp"),
)

_MIME = dict(_MAGIC)

# 微信自己的容器：解密后仍是 HEVC 裸流，必须先用 ffmpeg 转码
WXGF_MAGIC = b"wxgf"


def sniff(data: bytes) -> Optional[str]:
    """返回 `jpeg` / `png` / `gif` / `webp`；都不是（或数据太短）返回 None。"""
    if not data or len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"GIF8":
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def mime_of(kind: str) -> str:
    return _MIME.get(kind, "application/octet-stream")


def describe_unknown(data: bytes) -> str:
    """给错误信息用：说清这堆字节**看起来是什么**（人能据此知道下一步做什么）。"""
    head = data[:32] if data else b""
    if head[:4] == WXGF_MAGIC:
        return "微信 wxgf 容器（内部是 HEVC 编码，不是 jpg/png，需要 ffmpeg 转码）"
    if b"\x00\x00\x00\x01" in head:
        return "HEVC/H.265 裸流（需要 ffmpeg 转成 jpg/png）"
    if not data:
        return "空文件"
    return "未知二进制（头 16 字节：" + head[:16].hex(" ") + "）"
