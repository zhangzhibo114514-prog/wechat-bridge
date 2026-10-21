"""无人值守入口：让 **agent 或 Windows 计划任务**用一条命令把整套拉起来。

## 两个命令就是全部接口

    python agent_ctl.py up                # 幂等：该起的都起；已经在跑就只报告，不起第二个
    python agent_ctl.py status --json     # 机器可读状态；agent 照 hint 做事即可
    python agent_ctl.py down              # 停掉

用户不需要记这些——只要跟 agent 说「**把微信机器人拉起来**」。

## 为什么是幂等的

开机自启任务每 10 分钟会调一次同样的 `up`。如果不幂等，就会每 10 分钟多起一个进程，
而多个桥接同时挂着同一个号 = **同一条消息被回复多次**，且各自的限速计数互相看不见。
所以 `up` 的第一件事是查单实例锁：在跑就只报告状态。

## 关于"没有服务器"

这个程序跑在用户自己的电脑上。**关机 / 睡眠 / 锁屏时它就是不工作的**——没有云端进程可以代跑，
这是物理限制。`up` 能自动做的是：

    微信没开 → 拉起它          （能自动）
    微信没登录 → **做不到**     （有时需要手机上点确认，脚本替不了）
    桥接没跑 → 启动它          （能自动）

所以"重启后完全无人干预"成立的条件是：Windows 自动登录 + 微信自动登录 + 自启任务已登记。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wechat_bridge import console               # noqa: E402
from wechat_bridge.procinfo import (          # noqa: E402
    account_dirs,
    find_account_dir,
    pid_alive,
    read_lock_holder,
    weixin_exe_path,
    weixin_running,
)

# 中文 Windows 控制台是 cp936，打不出 ✔/✘/⚠ 会直接抛 UnicodeEncodeError 把命令打死。
# up / status / down 都在这上面，所以第一件事就是把输出流托住：终端保留编码、只降级记号；
# 管道/重定向改 UTF-8（agent 按 UTF-8 读）。详见 wechat_bridge/console.py。
OK, BAD, WARN = console.install()

LOCK = ROOT / "state" / "bridge.lock"
HEARTBEAT = ROOT / "state" / "heartbeat.log"
CONFIG = ROOT / "bridge_config.json"
BRIDGE = ROOT / "bridge.py"

# 心跳超过这个秒数就认为"进程还在但卡住了"
HEARTBEAT_STALE_SEC = 180.0


# ---------------------------------------------------------------- 状态采集

def _config_path(explicit: str = "") -> Path:
    """实际要用的配置文件。

    为什么要能指定：`up` 起来的那个实例用的就是**这一份**。默认只用 `bridge_config.json`，
    但实测阶段常跑 `state/_grouptest.json`（mode=auto、群聊放行）——两者行为差很多，
    不一致时"开机叫一句"起来的是个什么都不干的实例，而且 status 还会照着另一份配置汇报。
    """
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else (ROOT / p)
    return CONFIG


def _config_summary(config_path: Optional[Path] = None) -> dict:
    """读配置里几个适合展示的字段。读不到不算错误——agent 需要的是"现在能不能跑"。"""
    path = config_path or CONFIG
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return {
            "mode": raw.get("policy", {}).get("mode", "?"),
            "allow_groups": bool(raw.get("policy", {}).get("allow_groups", False)),
            "hook": str(raw.get("hook", {}).get("adapter", "")).split(":")[-1],
            "listen_uids": raw.get("hook", {}).get("options", {}).get("listen_uids", []),
            "config": str(path),
            "config_ok": True,
        }
    except Exception as exc:
        return {"mode": "?", "allow_groups": False, "hook": "", "listen_uids": [],
                "config": str(path),
                "config_ok": False, "config_error": f"{type(exc).__name__}: {exc}"}


def _last_heartbeat_age() -> float | None:
    """最近一次心跳距今多少秒。没有心跳文件返回 None。"""
    try:
        head = HEARTBEAT.read_text(encoding="utf-8-sig").strip().splitlines()[-1]
        stamp = head.split("|", 1)[0].strip()
        return (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
    except Exception:
        return None


def collect_status(config_path: Optional[Path] = None) -> dict:
    """把"现在能不能跑、卡在哪"汇总成一个 dict。**只读、不需要任何权限。**"""
    holder = read_lock_holder(LOCK) if LOCK.exists() else None
    running = bool(holder and pid_alive(holder))
    wx_proc = weixin_running()          # True / False / None（看不到，不知道）
    account_dir = find_account_dir()
    accounts = account_dirs(account_dir)
    beat_age = _last_heartbeat_age()
    cfg = _config_summary(config_path)

    blocked: list[str] = []
    # 「看不到」不等于「没开」：沙箱里进程枚举被拒，此时乱报会把人带去修错东西
    if wx_proc is False:
        blocked.append("wechat_not_running")
    if not account_dir or not accounts:
        blocked.append("wechat_not_logged_in")
    if not running:
        blocked.append("bridge_not_running")
    elif beat_age is not None and beat_age > HEARTBEAT_STALE_SEC:
        blocked.append("bridge_stalled")
    if not cfg["config_ok"]:
        blocked.append("config_unreadable")

    # hint 按"先修哪个"排序：微信 → 登录 → 桥接 → 卡住
    if "config_unreadable" in blocked:
        hint = f"配置读不了：{cfg.get('config_error')}。先跑 python make_config.py 重新生成。"
    elif wx_proc is False:
        hint = "微信没开。跑 `python agent_ctl.py up` 会自动把它拉起来。"
    elif wx_proc is None and not running:
        hint = ("当前环境看不到进程列表（沙箱权限），没法确认微信开没开。"
                "先跑 `python agent_ctl.py up`；它起不来会自己说卡在哪。")
    elif "wechat_not_logged_in" in blocked:
        hint = ("微信开着，但没登录过任何账号。打开微信登录一下"
                "（有时需要在手机上点确认，这一步脚本替不了）。")
    elif "bridge_stalled" in blocked:
        hint = (f"桥接进程还在（pid={holder}），但心跳停在 {beat_age:.0f} 秒前，可能卡住了。"
                f"跑 `python agent_ctl.py down` 再 `up`。")
    elif not running:
        hint = "桥接没在跑。跑 `python agent_ctl.py up` 启动。"
    else:
        hint = ""

    return {
        "running": running,
        "pid": holder if running else None,
        "wechat": "ok" if wx_proc else ("unknown" if wx_proc is None else "not_running"),
        "wechat_exe": str(weixin_exe_path() or ""),
        "account_dir": str(account_dir or ""),
        "accounts": accounts,
        "logged_in": bool(accounts),
        "last_heartbeat_sec": round(beat_age, 1) if beat_age is not None else None,
        "mode": cfg["mode"],
        "allow_groups": cfg["allow_groups"],
        "config": cfg.get("config", ""),
        "hook": cfg["hook"],
        "listen_uids": cfg["listen_uids"],
        "blocked": blocked,
        "hint": hint,
    }


def print_status(status: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    mark = lambda ok: OK if ok else BAD      # noqa: E731
    print(f"{mark(status['running'])} 桥接进程：" +
          (f"在跑（pid={status['pid']}）" if status["running"] else "没在跑"))
    wx = status["wechat"]
    print(f"{mark(wx == 'ok')} 微信进程：" +
          {"ok": "在跑", "unknown": "看不到（当前环境禁止枚举进程，不代表没开）"}
          .get(wx, "没开"))
    print(f"{mark(status['logged_in'])} 微信账号：" +
          (f"已登录 {', '.join(status['accounts'])}" if status["logged_in"] else "未登录过"))
    if status["last_heartbeat_sec"] is not None:
        print(f"  最近心跳：{status['last_heartbeat_sec']:.0f} 秒前")
    print(f"  运行模式：{status['mode']}" +
          ("（群聊已放行）" if status["allow_groups"] else "（群聊未放行）"))
    if status.get("config"):
        print(f"  配置文件：{status['config']}")
    if status["hint"]:
        print(f"\n→ 下一步：{status['hint']}")
    else:
        print("\n一切正常。")


# ---------------------------------------------------------------- 动作

def _start_bridge(config_path: Optional[Path] = None) -> subprocess.Popen:
    """后台启动桥接。用 pythonw：不弹黑窗，关掉终端也不影响它。

    **stdout/stderr 写文件、不丢进 DEVNULL**：`up` 是用户唯一的日常入口，
    起来之后"为什么它不理我"全靠这些日志（`[视觉]`、`[取回]`、`[合并]` 都只在 stdout）。
    丢进黑洞的话，开机叫一句启动的实例出问题时，agent 手上什么都没有。
    """
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    logs = ROOT / "state"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "bridge.stdout.log", "a", encoding="utf-8", errors="replace")
    err = open(logs / "bridge.stderr.log", "a", encoding="utf-8", errors="replace")
    kwargs = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": err}
    if sys.platform == "win32":
        # 脱离当前控制台：否则 agent 会话结束会把它一起带走
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    cmd = [str(exe), str(BRIDGE)]
    if config_path is not None and Path(config_path) != CONFIG:
        cmd += ["--config", str(config_path)]
    return subprocess.Popen(cmd, **kwargs)


def cmd_up(args) -> int:
    cfg_path = _config_path(getattr(args, "config", "") or "")
    status = collect_status(cfg_path)

    # 1) 微信进程。**只有确认没开才去拉**：沙箱里看不到进程列表时（unknown），
    #    盲拉会白开一个窗口；先让它继续，真有问题桥接连接时会自己报出来。
    if status["wechat"] == "not_running":
        exe = weixin_exe_path()
        if exe is None:
            print(f"{BAD} 找不到微信可执行文件（常见路径与注册表都没查到）。")
            print("  请手动打开微信，然后重跑本命令。")
            return 1
        print(f"微信没开，正在拉起：{exe}")
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except Exception as exc:
            print(f"{BAD} 启动微信失败：{type(exc).__name__}: {exc}")
            return 1
        deadline = time.time() + args.wait
        while time.time() < deadline:
            if weixin_running() is not False:
                print("微信已启动。")
                break
            time.sleep(2)
        else:
            print(f"{BAD} 等了 {args.wait} 秒，微信进程仍未出现。请手动打开看看是不是弹了什么窗口。")
            return 1
    elif status["wechat"] == "unknown":
        print("（当前环境看不到进程列表，跳过「微信开没开」的检查，直接往下走）")

    # 2) 登录：这一步**做不到自动**
    if not account_dirs():
        print(f"{BAD} 微信开着，但没有登录过任何账号。")
        print("  这一步脚本替不了：请在微信窗口里登录（有时还需要在手机上点一下确认）。")
        print("  登录完成后重跑本命令即可。")
        return 1

    # 3) 已经在跑？幂等：只报告，不起第二个
    if status["running"]:
        if "bridge_stalled" in status["blocked"]:
            print(f"{WARN} 桥接进程在（pid={status['pid']}），但心跳停在 "
                  f"{status['last_heartbeat_sec']:.0f} 秒前。建议先 down 再 up。")
            return 1
        print(f"{OK} 已经在跑（pid={status['pid']}），不做任何事。")
        if args.json:
            print(json.dumps(collect_status(cfg_path), ensure_ascii=False, indent=2))
        return 0

    # 4) 启动
    print("正在启动桥接…")
    try:
        proc = _start_bridge(cfg_path)
    except Exception as exc:
        print(f"{BAD} 启动失败：{type(exc).__name__}: {exc}")
        return 1

    deadline = time.time() + args.wait
    while time.time() < deadline:
        time.sleep(2)
        holder = read_lock_holder(LOCK) if LOCK.exists() else None
        if holder and pid_alive(holder):
            age = _last_heartbeat_age()
            if age is not None and age < 60:
                print(f"{OK} 已启动（pid={holder}），心跳正常。")
                print("  下一步：想确认它真的在跑，执行 python agent_ctl.py status --json")
                if args.json:
                    print(json.dumps(collect_status(cfg_path), ensure_ascii=False, indent=2))
                return 0
    print(f"{WARN} 进程 {proc.pid} 起来了，但 {args.wait} 秒内没等到心跳。")
    print("  最常见的原因：微信主窗口被最小化到托盘了（UI 自动化要求窗口可见）。")
    print("  排查：python agent_ctl.py status        # 看 hint")
    print("        python bridge.py --doctor         # 看完整自检")
    return 1


def cmd_down(args) -> int:
    holder = read_lock_holder(LOCK) if LOCK.exists() else None
    if not holder or not pid_alive(holder):
        print("桥接没在跑。")
        if LOCK.exists():
            print(f"（发现陈旧锁 {LOCK.name}，下次 up 会自动接管，不用手动删）")
        return 0
    print(f"正在停止 pid={holder} …")
    try:
        subprocess.run(["taskkill", "/PID", str(holder), "/T", "/F"],
                       capture_output=True, text=True, errors="ignore")
    except Exception as exc:
        print(f"{BAD} 停止失败：{type(exc).__name__}: {exc}")
        return 1
    time.sleep(1.5)
    if pid_alive(holder):
        print(f"{BAD} 进程还在。可以到任务管理器里结束 pythonw.exe。")
        return 1
    print(f"{OK} 已停止。")
    print("  提示：强杀可能留下 state/bridge.lock，下次 up 会自动接管（已实测）。")
    return 0


def cmd_status(args) -> int:
    print_status(collect_status(_config_path(getattr(args, "config", "") or "")), args.json)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="无人值守入口：把微信 AI 群友拉起来 / 查状态 / 停掉",
        epilog="给人用的说明在 README；给 agent 用的就是 up 与 status --json 两条。")
    sub = ap.add_subparsers(dest="action", required=True)

    p_up = sub.add_parser("up", help="幂等地把整套跑起来")
    p_up.add_argument("--wait", type=int, default=60, help="每步最多等多少秒，默认 60")
    p_up.add_argument("--config", default="", help="用哪份配置启动（默认 bridge_config.json）")
    p_up.add_argument("--json", action="store_true", help="结束时额外打印一份 JSON 状态")
    p_up.set_defaults(func=cmd_up)

    p_st = sub.add_parser("status", help="看状态（只读，不需要任何权限）")
    p_st.add_argument("--config", default="", help="按哪份配置汇报（默认 bridge_config.json）")
    p_st.add_argument("--json", action="store_true", help="机器可读输出")
    p_st.set_defaults(func=cmd_status)

    p_dn = sub.add_parser("down", help="停掉桥接")
    p_dn.set_defaults(func=cmd_down)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
