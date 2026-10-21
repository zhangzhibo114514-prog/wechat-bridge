# -*- coding: utf-8 -*-
"""识图后端自检：拿一张本地图片，走**配置里的视觉后端**，打印它看到了什么。

为什么单独做一个命令：识图配错时症状很隐蔽——桥接只会退回「[图片]」占位，
看起来像"模型没看懂"，实际是后端没通。一条命令就能把"通没通"变成事实。

用法：
    python vision_check.py 图片路径
    python vision_check.py 图片路径 --config state/_grouptest.json

判定：
    打印出描述 = 通了（描述质量就是模型质量）；
    抛 RuntimeError = 没通，错误里会写清楚是 HTTP 几、还是返回空。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wechat_bridge.adapters.vision_openai_compat import make_client  # noqa: E402
from wechat_bridge.config import load_config                          # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="识图后端自检")
    ap.add_argument("image", help="本地图片路径")
    ap.add_argument("--config", default=None, help="配置文件（默认 bridge_config.json）")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    print(f"配置：{args.config or 'bridge_config.json'} | vision.enabled={cfg.vision.enabled}")
    if not cfg.vision.enabled:
        print("vision.enabled=false —— 图片只会得到「[图片]」占位，不会真的看懂。")
        return 1
    opts = dict(cfg.vision.options or {})
    print(f"后端：{opts.get('model', '?')} @ {opts.get('base_url', '?')}{opts.get('path', '')}")
    client = make_client(cfg.vision)
    if client is None:
        print("视觉客户端没造出来（adapter 加载失败，看上面那行 [视觉] 提示）")
        return 1
    try:
        desc = client.describe(args.image)
    except Exception as exc:
        print(f"失败：{exc}")
        return 1
    print("\n它看到的：")
    print(desc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
