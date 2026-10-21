# -*- coding: utf-8 -*-
"""配置面板自测：**真的起一个 HTTP 服务、真的发请求**，再把配置文件读回来看。

要锁住的事（每一条都对应面板上可能出事的真实情况）：
  1. 不带 token 的请求一律拒（挡住"随便一个网页往 localhost 发 POST"）；
  2. `/api/state` 里**不能出现密钥**（面板只认白名单字段）；
  3. 白名单外的键改不动（比如 `llm.options.api_key`）；
  4. 非法值（枚举写错、数字越界）被拒，且**文件没被改动**；
  5. 合法的开关能落盘，并且**先留 .bak**；
  6. 人设能改，太短/太长的被拒；
  7. 人设卡**幂等**（同一张卡加两次不会重复）；
  8. 写完之后配置仍然能被 `load_config` 解析（面板不会把配置写坏）。

跑法：python panel_selftest.py
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "state" / "_panel_tests"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if condition else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return condition


def main() -> int:
    from wechat_bridge.config import load_config
    from panel.panel_server import make_server

    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    cfg_path = TMP / "panel_config.json"
    shutil.copy2(ROOT / "bridge_config.json", cfg_path)

    httpd = make_server(0, cfg_path)
    port = httpd.server_address[1]
    token = httpd.RequestHandlerClass.panel.token          # noqa: SLF001
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"测试服务：{base}\n")

    def req(path: str, payload=None, with_token: bool = True):
        url = base + path
        data = None
        headers = {}
        if with_token:
            headers["X-Panel-Token"] = token
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(url, data=data, headers=headers,
                                   method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    ok = True
    print("-- 安全边界 --")
    page = req("/")[1]
    ok &= check("1 页面能打开，且 token 已注入（不是占位符）",
                "__PANEL_TOKEN__" not in page and token in page and "人设" in page)
    code, body = req("/api/state", with_token=False)
    ok &= check("2 不带 token ⇒ 403", code == 403, f"HTTP {code}")

    code, body = req("/api/state")
    ok &= check("3 带 token 能读状态", code == 200 and '"settings"' in body, f"HTTP {code}")
    ok &= check("4 **状态里没有密钥字段**", "api_key" not in body and "sk-" not in body,
                body[:0])
    state = json.loads(body)
    ok &= check("5 白名单开关都在（抽查 mode / speak_gate.mode / send_mode）",
                {"policy.mode", "speak_gate.mode", "hook.options.send_mode"} <= set(state["settings"]),
                str(sorted(state["settings"])[:4]))
    ok &= check("6 人设与状态一起返回（字数为正）", state["persona"]["chars"] > 0,
                str(state["persona"]["chars"]))

    print("-- 写入白名单 --")
    before = cfg_path.read_text(encoding="utf-8")
    code, body = req("/api/set", {"path": "llm.options.api_key", "value": "sk-evil"})
    ok &= check("7 白名单外的键（api_key）改不动", code == 400, f"HTTP {code} {body[:60]}")
    ok &= check("7 而且文件没被改", cfg_path.read_text(encoding="utf-8") == before)

    code, body = req("/api/set", {"path": "policy.mode", "value": "nonsense"})
    ok &= check("8 枚举写错被拒（policy.mode）", code == 400, f"HTTP {code}")

    code, body = req("/api/set", {"path": "group.max_chain_turns", "value": 999})
    ok &= check("9 数字越界被拒（max_chain_turns 上限 50）", code == 400, f"HTTP {code}")
    ok &= check("9 越界后文件仍未被改", cfg_path.read_text(encoding="utf-8") == before)

    old_proactive = state["settings"]["group.proactive"]
    code, body = req("/api/set", {"path": "group.proactive", "value": not old_proactive})
    ok &= check("10 合法开关能落盘", code == 200, f"HTTP {code} {body[:60]}")
    cfg = load_config(cfg_path)
    ok &= check("10 落盘后 load_config 读到的是新值",
                bool(cfg.group.proactive) == (not old_proactive),
                f"{cfg.group.proactive}")
    backups = list(TMP.glob("panel_config.json.bak-*"))
    ok &= check("11 写之前留了备份", len(backups) >= 1, str([b.name for b in backups]))

    code, body = req("/api/set", {"path": "policy.quiet_hours", "value": ["08:00", "22:30"]})
    ok &= check("12 静默时段能设", code == 200 and load_config(cfg_path).policy.quiet_hours == ["08:00", "22:30"],
                f"HTTP {code}")
    code, body = req("/api/set", {"path": "policy.quiet_hours", "value": ["8点", "22:30"]})
    ok &= check("13 时间格式写错被拒", code == 400, f"HTTP {code}")
    code, body = req("/api/set", {"path": "policy.quiet_hours", "value": []})
    ok &= check("14 静默时段能清空（= 全天可用）",
                code == 200 and load_config(cfg_path).policy.quiet_hours == [], f"HTTP {code}")

    print("-- 人设 --")
    code, body = req("/api/persona", {"text": "太短了"})
    ok &= check("15 人设太短被拒", code == 400, f"HTTP {code}")
    long_text = "你是测试人设。" + "这是一句用来凑长度的设定，不需要有意义。" * 20
    code, body = req("/api/persona", {"text": long_text})
    ok &= check("16 人设能保存且字数对", code == 200 and json.loads(body)["chars"] == len(long_text),
                f"HTTP {code}")
    ok &= check("16 文件里真的是新的人设",
                load_config(cfg_path).persona.system_prompt == long_text)

    code, body = req("/api/card", {"id": "cold"})
    first = json.loads(body) if code == 200 else {}
    ok &= check("17 人设卡能追加（cold）", code == 200 and first.get("added") is True, body[:80])
    code, body = req("/api/card", {"id": "cold"})
    second = json.loads(body) if code == 200 else {}
    persona_now = load_config(cfg_path).persona.system_prompt
    ok &= check("18 同一张卡加第二次**不重复**", second.get("added") is False
                and persona_now.count("偏冷淡") == 1,
                f"added={second.get('added')} 出现次数={persona_now.count('偏冷淡')}")
    code, body = req("/api/card", {"id": "no_such_card"})
    ok &= check("19 不存在的卡被拒", code == 400, f"HTTP {code}")

    print("-- 起停（不真跑：只验参数与拒绝路径） --")
    code, body = req("/api/bridge", {"action": "rm -rf"})
    ok &= check("20 不认识的动作被拒、不会去执行任何东西", code == 400, f"HTTP {code}")

    print("-- 收尾 --")
    ok &= check("21 折腾一圈之后配置仍可解析（没被写坏）",
                load_config(cfg_path).persona.system_prompt == persona_now)
    code, body = req("/api/health", with_token=False)
    ok &= check("22 健康检查不需要 token（只回运行时长）", code == 200 and "uptime_sec" in body,
                f"HTTP {code}")

    httpd.shutdown()
    httpd.server_close()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n结论：{'配置面板自测全部通过' if ok else '存在失败项，见上'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
