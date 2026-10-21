# -*- coding: utf-8 -*-
"""探测本机是否有**可被外部进程调用**的视觉/模型端点。

为什么需要它：DSH 的识图插件大多是 harness 内部工具，微信桥接（独立进程）调不到。
只有当某个插件/服务在本机开了 **OpenAI 兼容端口**（`/v1/models`、`/v1/chat/completions`）时，
桥接才能直接用它当视觉后端——这正是开源 README 里「装个插件再桥接」要验证的事。

用法：
    python probe_vision_endpoints.py            # 扫常见端口
    python probe_vision_endpoints.py 8000 9527  # 只探指定端口

判定：
    ① 端口通 + `/v1/models` 返回模型列表 → 可接入（OpenAI 兼容）
    ② 模型名里带 vl / vision / llava / qwen-vl / 多模态关键词 → 大概率能识图
    ③ 返回 401/403 → 服务在，但要 key（配置里写 env:变量名）
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 常见本地服务端口：LM Studio / Ollama / vLLM / Gradio / 各类插件自带的服务
PORTS = [1234, 11434, 8000, 8080, 8081, 3000, 4000, 5000, 5001, 5002,
         7860, 7861, 8788, 8877, 9100, 9527, 12345, 18080, 19387]

VISION_HINTS = ("vl", "vision", "llava", "minicpm", "qwen2-vl", "internvl", "molmo", "pixtral")


def port_open(port: int, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def probe(port: int) -> dict:
    """返回 {'port', 'status', 'models', 'vision_like', 'note'}。"""
    info = {"port": port, "status": "-", "models": [], "vision_like": False, "note": ""}
    if not port_open(port):
        return info
    for path in ("/v1/models", "/api/tags", "/v1/chat/completions"):
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=2.5) as resp:
                raw = resp.read(20000).decode("utf-8", "replace")
                info["status"] = f"HTTP {resp.status} {path}"
        except urllib.error.HTTPError as exc:
            info["status"] = f"HTTP {exc.code} {path}"
            if exc.code in (401, 403):
                info["note"] = "需要鉴权（配置里写 env:变量名）"
            continue
        except Exception:
            continue
        if path == "/v1/models":
            try:
                data = json.loads(raw)
                names = [str(m.get("id") or m.get("name") or "") for m in data.get("data", [])]
            except Exception:
                names = []
            info["models"] = [n for n in names if n][:8]
            info["vision_like"] = any(h in " ".join(names).lower() for h in VISION_HINTS)
        elif path == "/api/tags":
            try:
                data = json.loads(raw)
                info["models"] = [str(m.get("name") or "") for m in data.get("models", [])][:8]
                info["vision_like"] = any(h in " ".join(info["models"]).lower() for h in VISION_HINTS)
            except Exception:
                pass
        break
    return info


def main(argv: list[str]) -> int:
    ports = [int(x) for x in argv if x.isdigit()] or PORTS
    print(f"扫描 {len(ports)} 个本地端口（只读探测，不发任何业务请求）…\n")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(probe, ports))
    hit = [r for r in results if r["status"] != "-"]
    if not hit:
        print("没有发现 OpenAI 兼容端点。")
        print("⇒ 说明当前这台机器上没有任何可被外部调用的视觉服务；")
        print("  要么装一个会开本地端口的插件，要么走云端 VL，要么退回本地 OCR。")
        return 1
    print(f"{'端口':<7}{'状态':<28}{'可识图':<8}模型")
    print("-" * 90)
    for r in hit:
        mark = "是" if r["vision_like"] else "?"
        models = ", ".join(r["models"])[:60] or "-"
        print(f"{r['port']:<7}{r['status']:<28}{mark:<8}{models} {r['note']}")
    print("\n接入方式（vision.options）：")
    for r in hit:
        if r["vision_like"]:
            print(f'  "base_url": "http://127.0.0.1:{r["port"]}", "model": "{r["models"][0] if r["models"] else ""}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
