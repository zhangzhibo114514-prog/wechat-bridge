# -*- coding: utf-8 -*-
"""配置面板的后端：一个只跑在本机的小 HTTP 服务（**只用标准库**，不装任何东西）。

## 为什么是"本机网页"而不是做进 DSH

DSH 本身没有给第三方加自定义栏目的稳定入口（要改宿主 asar 并重新构建，改了以后 DSH 出问题
就连调试环境一起没了）。而这个面板要管的东西全在**本机文件系统 + 本机进程**上：
读 `bridge_config.json`、起停 `bridge.py`、看 `state/audit.jsonl`——一个本机网页最直接。
打开方式：双击 `启动配置面板.cmd`，浏览器会自己弹出来。

## 它能做什么

- 看：桥接活没活（心跳 mtime + 锁里的 pid）、当前配置、最近审计
- 改：**白名单内的配置项**（开关类为主），改完立即落盘并**重新校验**（校验不过不写）
- 人设：直接编辑 `persona.system_prompt`，也可以点「人设卡」往后面追加一段（幂等，不会重复追加）
- 起停：调 `agent_ctl.py up/down`（**面板必须在沙箱外运行**，沙箱里起的桥接进程会被回收）

## 安全边界（故意做窄的）

- 只绑 `127.0.0.1`，别的机器连不上；
- 所有写操作要带 `X-Panel-Token`（启动时随机生成、注入页面），挡住"随便一个网页往 localhost 发 POST"；
- **只认白名单里的键**，写不进别的（`llm.options.api_key` 这类根本不在名单里）；
- 返回给前端的永远是白名单字段，**密钥不会被带出去**；
- 每次写之前留一份 `bridge_config.json.bak-<时间>`，只保留最近 5 份；
- 写文件用「临时文件 + os.replace」，中途失败不会留下半个配置。

跑法：
    python panel/panel_server.py                 # 默认 127.0.0.1:8765，配置 bridge_config.json
    python panel/panel_server.py --port 9000 --config state/别的一份.json
    python panel/panel_server.py --open          # 起来后自己弹浏览器
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent        # 项目根（panel/ 的上一层）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PANEL_HTML = Path(__file__).resolve().parent / "panel.html"
BACKUP_KEEP = 5

#: 能从这里改的键。**不在表里的键一律拒**——配置里可以放密钥，
#: 而"能写任意键的面板"等于把密钥文件交给任何一个能打开它的人。
SETTINGS: dict[str, tuple[str, object]] = {
    # 运行模式
    "policy.mode": ("enum", ["draft", "auto"]),
    "policy.allow_groups": ("bool", None),
    "policy.allow_any_group": ("bool", None),
    "policy.allow_unknown_contacts": ("bool", None),
    "policy.reply_only_when_mentioned_in_group": ("bool", None),
    "policy.max_reply_age_min": ("int", (0, 1440)),
    "policy.reply_debounce_sec": ("float", (0.0, 30.0)),
    "policy.quiet_hours": ("hours", None),
    # 群聊状态机
    "group.enabled": ("bool", None),
    "group.proactive": ("bool", None),
    "group.followup_same_sender_only": ("bool", None),
    "group.max_chain_turns": ("int", (0, 50)),
    "group.observe_first_msgs": ("int", (0, 100)),
    "group.idle_timeout_sec": ("int", (30, 3600)),
    "group.at_back": ("bool", None),
    # 说话决策层
    "speak_gate.enabled": ("bool", None),
    "speak_gate.mode": ("enum", ["model", "rules"]),
    "speak_gate.model_budget_per_hour": ("int", (0, 2000)),
    "speak_gate.model_max_per_min": ("int", (0, 60)),
    # 识图 / 发送 / 分段
    "vision.enabled": ("bool", None),
    "hook.options.send_mode": ("enum", ["auto", "background", "gui"]),
    "segments.enabled": ("bool", None),
}

PERSONA_MIN, PERSONA_MAX = 100, 6000

#: 人设卡：点一下就往人设末尾追加一段（已经包含就不重复追加）。
PERSONA_CARDS = (
    {
        "id": "rice",
        "title": "爱吃白饭的大肥鱼",
        "desc": "二创设定：爱吃白饭，被叫大肥鱼会顶嘴；只在聊到吃的时候偶尔提。",
        "marker": "爱吃白饭",
        "text": "你爱吃白饭，白饭配点酱油就能吃得香；有人拿「爱吃白饭的大肥鱼」逗你，"
                "你会顶嘴（我是鲸鱼不是鱼），但别主动反复提，只在聊到吃饭、饿、加班时偶尔顺嘴带一句。",
    },
    {
        "id": "cold",
        "title": "更冷淡一点",
        "desc": "能不接就不接，回得更短，少寒暄。",
        "marker": "偏冷淡",
        "text": "你话少、偏冷淡：能一句说完的不说两句，没必要的寒暄直接省掉，别人闲聊不强求接话。",
    },
    {
        "id": "warm",
        "title": "更热情一点",
        "desc": "多接一句、多关心对方状态，但依旧不长篇。",
        "marker": "待人热情一点",
        "text": "你待人热情一点：别人说事时多接一句，顺手关心一下对方状态（吃饭没、累不累），但不长篇。",
    },
    {
        "id": "no_emoji",
        "title": "不发表情",
        "desc": "纯文字，一个 emoji 和颜文字都不带。",
        "marker": "不发表情",
        "text": "你这条号不发表情：一个 emoji、一个颜文字都不带，只用纯文字说话。",
    },
    {
        "id": "no_at",
        "title": "回复不带 @",
        "desc": "群里回话不 @ 人（人少的小群显得更自然）。",
        "marker": "不要 @ 对方",
        "text": "在群里回复时不要 @ 对方，直接接着说；人少的小群里这样更自然。",
    },
)

#: 幂等判据就是"卡里的 marker 已经在人设里"，所以 **marker 必须是卡片正文的子串**。
#: 少了这一条，同一个按钮点两次就会把同一段追加两遍（自测里踩过）。
for _card in PERSONA_CARDS:
    assert _card["marker"] in _card["text"], f"人设卡的 marker 不在正文里：{_card['id']}"


# ---------------- 工具 ----------------


def _now() -> str:
    return _dt.datetime.now().strftime("%H:%M:%S")


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _deep_get(data: dict, path: str):
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _deep_set(data: dict, path: str, value) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _coerce(path: str, value):
    """按白名单表校验并转成正确类型；不合法就抛 ValueError（由调用方回 400）。"""
    kind, rule = SETTINGS[path]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError(f"{path} 只接受 true/false")
    if kind == "enum":
        if str(value) not in rule:            # type: ignore[operator]
            raise ValueError(f"{path} 只能是 {' / '.join(rule)}")   # type: ignore[arg-type]
        return str(value)
    if kind == "int":
        try:
            num = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{path} 要填整数")
        low, high = rule                                 # type: ignore[misc]
        if not (low <= num <= high):
            raise ValueError(f"{path} 要在 {low}~{high} 之间")
        return num
    if kind == "float":
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{path} 要填数字")
        low, high = rule                                 # type: ignore[misc]
        if not (low <= num <= high):
            raise ValueError(f"{path} 要在 {low}~{high} 之间")
        return round(num, 2)
    if kind == "hours":
        if value in ([], "", None):
            return []
        import re

        items = value if isinstance(value, list) else [value]
        if len(items) != 2:
            raise ValueError("静默时段要两个时间（开始、结束），或者留空")
        for item in items:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(item)):
                raise ValueError(f"时间格式要 HH:MM，收到 {item!r}")
        return [str(items[0]), str(items[1])]
    raise ValueError(f"未知类型：{kind}")


def _default_settings() -> dict:
    """配置里**没写**的键该显示什么：用代码里的默认值，别显示成空白让用户以为出错。

    真实例子：`hook.options.send_mode` 在生产配置里根本没写（代码默认 auto），
    面板要是显示空,用户会以为坏了。
    """
    dump: dict = {}
    try:
        import dataclasses

        from wechat_bridge.config import BridgeConfig

        dump = dataclasses.asdict(BridgeConfig())
    except Exception:
        pass
    hook = dump.setdefault("hook", {})
    if isinstance(hook, dict):
        opts = hook.setdefault("options", {})
        if isinstance(opts, dict):
            opts.setdefault("send_mode", "auto")
    return {key: _deep_get(dump, key) for key in SETTINGS}


class Panel:
    """面板的全部状态与操作。**无全局副作用**，方便测试里直接构造。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.token = secrets.token_hex(8)
        self.started_at = time.time()
        self._lock = threading.Lock()

    # ---------- 读 ----------

    def _read_raw(self) -> dict:
        text = self.config_path.read_text(encoding="utf-8-sig")
        return json.loads(text)

    def state(self) -> dict:
        raw = self._read_raw()
        persona = str(_deep_get(raw, "persona.system_prompt") or "")
        defaults = _default_settings()
        settings = {}
        for key in SETTINGS:
            value = _deep_get(raw, key)
            settings[key] = value if value is not None else defaults.get(key)
        return {
            "ok": True,
            "config_path": str(self.config_path),
            "settings": settings,
            "persona": {"text": persona, "chars": len(persona)},
            "status": self.bridge_status(),
            "audit": self.audit_tail(limit=12),
            "meta": {
                "version": self._version(),
                "server_time": _now(),
                "uptime_sec": int(time.time() - self.started_at),
                "contact_rules": self._contact_rules(raw),
            },
        }

    def _version(self) -> str:
        try:
            return (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
        except Exception:
            return "?"

    def _contact_rules(self, raw: dict) -> list[dict]:
        rules = _deep_get(raw, "policy.contact_rules") or {}
        if not isinstance(rules, dict):
            return []
        return [{"chat_id": k, "rule": v} for k, v in rules.items()]

    def bridge_status(self) -> dict:
        """桥接活没活：**看心跳文件的 mtime**（沙箱里看不到进程，这是唯一可靠判据）。"""
        out = {"running": False, "pid": None, "started": "", "heartbeat_age_sec": None,
               "heartbeat_at": ""}
        lock_path = ROOT / "state" / "bridge.lock"
        try:
            text = lock_path.read_text(encoding="utf-8").strip()
            for part in text.split():
                if part.startswith("pid="):
                    out["pid"] = int(part[4:])
                elif part.startswith("started="):
                    out["started"] = part[8:]
        except Exception:
            pass
        hb = ROOT / "state" / "heartbeat.log"
        try:
            age = time.time() - hb.stat().st_mtime
            out["heartbeat_age_sec"] = round(age, 1)
            out["heartbeat_at"] = _dt.datetime.fromtimestamp(hb.stat().st_mtime).strftime("%H:%M:%S")
            # 心跳 30 秒一次；超过 90 秒没动静就当作没在跑（比 PID 判定更诚实）
            out["running"] = age < 90
        except Exception:
            pass
        return out

    def audit_tail(self, limit: int = 12) -> list[dict]:
        path = ROOT / "state" / "audit.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except Exception:
            return []
        items = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except Exception:
                continue
            items.append({
                "ts": str(item.get("ts", ""))[:19],
                "event": item.get("event", ""),
                "reason": str(item.get("reason", ""))[:80],
                "sender": str(item.get("sender_name") or item.get("sender_id") or "")[:24],
                "text": str(item.get("text", ""))[:60],
            })
        return items

    # ---------- 写 ----------

    def _backup(self) -> str:
        dest = self.config_path.with_name(f"{self.config_path.name}.bak-{_stamp()}")
        shutil.copy2(self.config_path, dest)
        olds = sorted(self.config_path.parent.glob(f"{self.config_path.name}.bak-*"))
        for stale in olds[:-BACKUP_KEEP]:
            try:
                stale.unlink()
            except Exception:
                pass
        return dest.name

    def _write_checked(self, raw: dict) -> None:
        """先写成临时文件并**用 load_config 真解析一遍**，过了才替换正式文件。"""
        tmp = self.config_path.with_name("_panel_validate.json")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            from wechat_bridge.config import load_config

            load_config(tmp)                      # 解析不过会抛异常
        except Exception as exc:
            raise ValueError(f"配置校验没过，没写入：{exc}") from exc
        finally:
            pass
        self._backup()
        os.replace(tmp, self.config_path)

    def set_setting(self, path: str, value) -> dict:
        if path not in SETTINGS:
            raise ValueError(f"不允许通过面板修改 {path!r}（只认白名单里的开关）")
        with self._lock:
            value = _coerce(path, value)
            raw = self._read_raw()
            _deep_set(raw, path, value)
            self._write_checked(raw)
        return {"ok": True, "path": path, "value": value}

    def set_persona(self, text: str) -> dict:
        text = str(text or "").strip()
        if not (PERSONA_MIN <= len(text) <= PERSONA_MAX):
            raise ValueError(f"人设要 {PERSONA_MIN}~{PERSONA_MAX} 字，现在 {len(text)} 字")
        with self._lock:
            raw = self._read_raw()
            _deep_set(raw, "persona.system_prompt", text)
            self._write_checked(raw)
        return {"ok": True, "chars": len(text)}

    def apply_card(self, card_id: str) -> dict:
        card = next((c for c in PERSONA_CARDS if c["id"] == card_id), None)
        if card is None:
            raise ValueError(f"没有这张人设卡：{card_id}")
        with self._lock:
            raw = self._read_raw()
            persona = str(_deep_get(raw, "persona.system_prompt") or "")
            if card["marker"] in persona:
                return {"ok": True, "added": False, "chars": len(persona),
                        "note": "人设里已经有这一段了，没有重复追加"}
            if "\n【附加设定】" not in persona:
                persona = persona.rstrip() + "\n\n【附加设定】"
            persona = persona.rstrip() + "\n- " + card["text"]
            if len(persona) > PERSONA_MAX:
                raise ValueError(f"追加后超过 {PERSONA_MAX} 字，先删一点再加")
            _deep_set(raw, "persona.system_prompt", persona)
            self._write_checked(raw)
        return {"ok": True, "added": True, "chars": len(persona), "note": f"已加入「{card['title']}」"}

    # ---------- 起停 ----------

    def control(self, action: str) -> dict:
        mapping = {"start": ["up"], "stop": ["down"], "restart": ["down", "up"]}
        if action not in mapping:
            raise ValueError(f"不认识的动作：{action}")
        logs: list[str] = []
        ok = True
        for sub in mapping[action]:
            try:
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "agent_ctl.py"), sub],
                    cwd=str(ROOT), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=180,
                )
                tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-6:]
                logs.extend(tail)
                ok = ok and proc.returncode == 0
            except Exception as exc:
                logs.append(f"{sub} 失败：{exc!r}")
                ok = False
        return {"ok": ok, "action": action, "log": logs, "status": self.bridge_status()}


# ---------------- HTTP ----------------


class Handler(BaseHTTPRequestHandler):
    panel: Panel = None                       # type: ignore[assignment]
    server_version = "wechat-bridge-panel/1.0"

    def log_message(self, fmt, *args):        # 安静点：控制台只留自己的输出
        return

    # ---- 基础 ----

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _authed(self) -> bool:
        return self.headers.get("X-Panel-Token", "") == self.panel.token

    def do_GET(self) -> None:                                    # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                html = PANEL_HTML.read_text(encoding="utf-8")
            except Exception as exc:
                self._json(500, {"ok": False, "error": f"读不到面板页面：{exc!r}"})
                return
            html = html.replace("__PANEL_TOKEN__", self.panel.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._json(200, {"ok": True, "uptime_sec": int(time.time() - self.panel.started_at)})
            return
        if parsed.path == "/api/state":
            if not self._authed():
                self._json(403, {"ok": False, "error": "token 不对"})
                return
            try:
                self._json(200, self.panel.state())
            except Exception as exc:
                self._json(500, {"ok": False, "error": repr(exc)})
            return
        if parsed.path == "/api/cards":
            if not self._authed():
                self._json(403, {"ok": False, "error": "token 不对"})
                return
            self._json(200, {"ok": True, "cards": [
                {k: c[k] for k in ("id", "title", "desc")} for c in PERSONA_CARDS]})
            return
        if parsed.path == "/api/audit":
            if not self._authed():
                self._json(403, {"ok": False, "error": "token 不对"})
                return
            limit = int((parse_qs(parsed.query).get("limit") or ["30"])[0] or 30)
            self._json(200, {"ok": True, "items": self.panel.audit_tail(max(1, min(limit, 200)))})
            return
        if parsed.path == "/favicon.ico":
            self._send(204, b"")
            return
        self._json(404, {"ok": False, "error": "没有这个路径"})

    def do_POST(self) -> None:                                   # noqa: N802
        if not self._authed():
            self._json(403, {"ok": False, "error": "token 不对（刷新页面重试）"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"请求体不是合法 JSON：{exc!r}"})
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/set":
                self._json(200, self.panel.set_setting(payload.get("path", ""), payload.get("value")))
            elif path == "/api/persona":
                self._json(200, self.panel.set_persona(payload.get("text", "")))
            elif path == "/api/card":
                self._json(200, self.panel.apply_card(payload.get("id", "")))
            elif path == "/api/bridge":
                self._json(200, self.panel.control(payload.get("action", "")))
            else:
                self._json(404, {"ok": False, "error": "没有这个接口"})
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": repr(exc)})


def make_server(port: int, config_path: Path, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"panel": Panel(config_path)})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description="微信桥接配置面板（仅本机）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="默认只绑本机；不建议改")
    ap.add_argument("--config", default="bridge_config.json")
    ap.add_argument("--open", action="store_true", help="起来后自动打开浏览器")
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.exists():
        print(f"[停] 找不到配置：{config_path}")
        return 2
    httpd = make_server(args.port, config_path, args.host)
    url = f"http://{args.host}:{args.port}/"
    print(f"配置面板已启动：{url}")
    print(f"  配置文件：{config_path}")
    print(f"  只绑本机 {args.host}；关掉这个窗口就等于关掉面板（不影响桥接）。", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
