"""下载出口排查 + 备选镜像取件工具。

本机实测（2026-10-15）：TCP 443 能握手，但 HTTPS 请求与 pip 下载全部超时 —— 属于“网络通、内容拿不到”。
本脚本用来回答两个问题：
    1. 现在到底有没有可用的出口（代理 / 直连 / 镜像）？
    2. 有出口的话，怎么把 GitHub 上的文件拿下来（含备用镜像）？

用法：
    python fetch_github.py --check                       # 只探测各个出口是否可用
    python fetch_github.py --check --proxy 127.0.0.1:7890
    python fetch_github.py --url <文件直链> --out xx.zip  # 用当前可用出口下载
    python fetch_github.py --url <仓库zip直链>            # 默认存到 downloads/

只读探测 + 下载到本地，不改系统设置。
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOWNLOAD_DIR = ROOT / "downloads"

# 常用 GitHub 加速前缀：直连不通时依次尝试（第三方服务，只做取件用，不投喂任何凭据）
GH_MIRRORS = [
    "",                                   # 直连
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://mirror.ghproxy.com/",
]

PROBE_TARGETS = [
    ("PyPI 官方", "https://pypi.org/simple/"),
    ("PyPI 清华镜像", "https://pypi.tuna.tsinghua.edu.cn/simple/"),
    ("GitHub 主站", "https://github.com"),
    ("GitHub codeload", "https://codeload.github.com"),
    ("微信官方下载页", "https://pc.weixin.qq.com/"),
]


def build_opener(proxy: str | None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": f"http://{proxy}", "https": f"http://{proxy}"}))
    else:
        env = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
        handlers.append(urllib.request.ProxyHandler(env) if env else urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


def probe(proxy: str | None, timeout: float = 10.0) -> dict[str, bool]:
    print(f"\n== 出口探测（代理: {proxy or '未指定/直连'}）==")
    opener = build_opener(proxy)
    result: dict[str, bool] = {}
    for name, url in PROBE_TARGETS:
        start = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fetch_github probe)"})
            with opener.open(req, timeout=timeout) as resp:
                size = len(resp.read(2048))
            ok = True
            tail = f"HTTP {resp.status}, 首包 {size} 字节, {time.time() - start:.1f}s"
        except Exception as exc:
            ok = False
            tail = f"{type(exc).__name__}: {exc}"
        result[name] = ok
        print(f"{'[通]' if ok else '[断]'} {name:<16} {tail}")
    return result


def download(url: str, out: Path, proxy: str | None, timeout: float = 60.0) -> Path | None:
    out.parent.mkdir(parents=True, exist_ok=True)
    opener = build_opener(proxy)
    candidates = [url] if url.startswith(("http://", "https://")) and "github" not in url else [
        (prefix + url) if prefix else url for prefix in GH_MIRRORS
    ]
    for cand in candidates:
        try:
            print(f"尝试: {cand}")
            req = urllib.request.Request(cand, headers={"User-Agent": "Mozilla/5.0 (fetch_github)"})
            with opener.open(req, timeout=timeout) as resp, out.open("wb") as fh:
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
            print(f"  成功：{out}（{total / 1024:.1f} KB）")
            return out
        except Exception as exc:
            print(f"  失败：{type(exc).__name__}: {exc}")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载出口排查与备选镜像取件")
    ap.add_argument("--check", action="store_true", help="只探测各出口是否可用")
    ap.add_argument("--proxy", default=None, help="形如 127.0.0.1:7890；不填则用环境变量或直连")
    ap.add_argument("--url", default=None, help="要下载的直链（GitHub 链接会自动套用加速镜像重试）")
    ap.add_argument("--out", default=None, help="保存路径，默认 downloads/<文件名>")
    args = ap.parse_args(argv)

    if args.check or not args.url:
        probe(args.proxy)
        print("\n提示：拿到可用代理后再跑一次 --check 确认；下载用 --url <直链>。")
        return 0

    out = Path(args.out) if args.out else DOWNLOAD_DIR / Path(args.url).name
    ok = download(args.url, out, args.proxy)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
