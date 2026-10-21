"""模型链路自测：起一个假 OpenAI 兼容服务，用**真实 HTTP** 验证桥接的模型调用与解析。

为什么必须做这一步：`--probe-llm` 只能证明"打得通 DeepSeek"（假 key 会拿到 401），
但**响应解析**（choices[0].message.content）、HTTP 错误映射、异常返回的处理从没被真实响应覆盖过。
真要是解析写错了，就变成"用户填完 key 才发现"。

本脚本自己起服务、跑完四种情况、再关掉，全程本地、不需要网络也不需要 key。

跑法：python llm_endpoint_selftest.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fake_llm_server import REPLY_TEXT, Handler                     # noqa: E402
from http.server import ThreadingHTTPServer                         # noqa: E402
from wechat_bridge.adapters.llm_openai_compat import OpenAICompatClient  # noqa: E402

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"


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


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="fake-llm")
    thread.start()
    ok = True
    try:
        if not wait_ready():
            print("假模型服务没起来，测试中止")
            return 1

        good = OpenAICompatClient(base_url=BASE, api_key="anything", model="fake-model",
                                  path="/v1/chat/completions", timeout_sec=10)

        print("-- 正常响应 --")
        text = good.complete([{"role": "user", "content": "在吗"}])
        ok &= check("拿到回复文本", text == REPLY_TEXT, repr(text))

        print("\n-- 异常响应必须报错，不能假装成功 --")
        cases = [
            ("缺 choices 字段", "/bad/no-choices"),
            ("返回的不是 JSON", "/bad/malformed"),
            ("HTTP 500", "/bad/500"),
        ]
        for label, path in cases:
            client = OpenAICompatClient(base_url=BASE, api_key="k", model="m",
                                        path=path, timeout_sec=10)
            try:
                got = client.complete([{"role": "user", "content": "hi"}])
                ok &= check(label, False, f"居然没抛异常，返回 {got!r}")
            except RuntimeError as exc:
                ok &= check(label, True, str(exc)[:60])
            except Exception as exc:
                ok &= check(label, False, f"抛的是 {type(exc).__name__}，应统一为 RuntimeError：{exc}")

        print("\n-- 走完整桥接：假模型 + 假 hook + draft 模式 --")
        ok &= bridge_level_check()

        print(f"\n结论：{'模型链路自测通过' if ok else '存在失败项'}")
        return 0 if ok else 1
    finally:
        server.shutdown()
        server.server_close()


def bridge_level_check() -> bool:
    """把桥接指到这个假模型上跑一条消息，确认真实 HTTP 那一跳是通的。"""
    import os
    import tempfile

    import bridge as B
    from wechat_bridge.config import BridgeConfig
    from wechat_bridge.adapters.hook_wechatauto import WechatautoHookAdapter

    cfg = BridgeConfig()
    cfg.llm.adapter = "wechat_bridge.adapters.llm_openai_compat:OpenAICompatClient"
    cfg.llm.options = {"base_url": BASE, "path": "/v1/chat/completions",
                       "api_key": "anything", "model": "fake-model", "timeout_sec": 10}
    cfg.policy.mode = "draft"
    cfg.policy.contact_rules = {"filehelper": "allow"}
    cfg.policy.max_reply_age_min = 0                      # 合成消息的时间戳可能是旧的，关掉闸门
    cfg.storage.draft_dir = "state/_llmselftest_drafts"
    cfg.storage.audit_path = "state/_llmselftest_audit.jsonl"
    cfg.storage.state_path = "state/_llmselftest_state.json"

    b = B.Bridge(cfg, allow_real_llm=True)                # 真走 HTTP，不替换 FakeLLM
    b.hook = WechatautoHookAdapter(listen_uids=["filehelper"])
    row = {"username": "filehelper", "type": "文本", "content": "在吗？",
           "sender_username": "wxid_somebody", "local_id": "1",
           "sort_seq": "1", "create_time": str(time.time())}
    decision = b.route(b.hook._row_to_message(row))
    passed = decision.action == "draft" and REPLY_TEXT in (decision.reply_text or "")
    print(f"     决策={decision.action} 回复={decision.reply_text!r}")

    # 清掉这次自测的产物
    for rel in ("state/_llmselftest_drafts",):
        target = ROOT / rel
        if target.exists():
            for f in target.glob("*.jsonl"):
                f.unlink()
            try:
                target.rmdir()
            except OSError:
                pass
    for rel in ("state/_llmselftest_audit.jsonl", "state/_llmselftest_state.json"):
        target = ROOT / rel
        if target.exists():
            target.unlink()
    return check("桥接经真实 HTTP 调通了模型并拿到回复", passed)


if __name__ == "__main__":
    raise SystemExit(main())
