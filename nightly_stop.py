"""每晚收工：干净地停掉桥接（可选连电脑一起关）。

为什么不能直接关机了事：
    桥接是常驻进程，被强杀会留下 `state/bridge.lock`（下次启动要等它判陈旧才接管）、
    限速窗口可能来不及落盘、`state.json` 的统计也可能停在上一轮。
    这个脚本先让它**有序退出**，再关机——省事且不留尾巴。

用法（在自己终端里跑，或挂个计划任务每天 22:55）：
    python nightly_stop.py                 # 只停桥接，不关机
    python nightly_stop.py --shutdown      # 停桥接后关机（60 秒倒计时，可 shutdown /a 取消）
    python nightly_stop.py --shutdown --at 23:00   # 到点才关（前台等待，可 Ctrl+C 取消）

怎么停：优先发 Windows 事件让窗口进程自己退出；不适用时按命令行匹配 python 进程并结束，
并在结束后清理残留锁文件、打印最终统计。
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
STATE = ROOT / "state"
LOCK = STATE / "bridge.lock"
BRIDGE = ROOT / "bridge.py"


def find_bridge_pids() -> list[int]:
    """找出跑本项目的 python 进程（按命令行匹配，不误杀别的 python）。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*bridge.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, errors="ignore")
    pids = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def stop_bridge() -> int:
    pids = find_bridge_pids()
    if not pids:
        print("没有正在运行的桥接进程。")
    else:
        print(f"正在停止桥接进程：{pids}")
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                           capture_output=True, text=True, errors="ignore")
        for _ in range(20):
            time.sleep(0.5)
            if not find_bridge_pids():
                break
        left = find_bridge_pids()
        if left:
            print(f"仍未退出，强制结束：{left}")
            for pid in left:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, errors="ignore")

    if LOCK.exists():
        try:
            LOCK.unlink()
            print("已清理残留锁文件 bridge.lock")
        except Exception as exc:
            print(f"锁文件删除失败（下次启动会自动接管）：{exc!r}")

    state_file = STATE / "state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8-sig"))
            print(f"今日统计：收到 {data.get('received')} / 已回 {data.get('replied')} / "
                  f"草稿 {data.get('drafted')} / 忽略 {data.get('ignored')} / 错误 {data.get('errors')}")
        except Exception:
            pass
    hb = STATE / "heartbeat.log"
    if hb.exists():
        last = hb.read_text(encoding="utf-8").strip().splitlines()[-1]
        print(f"最后心跳：{last[:110]}")
    return 0


def wait_until(hhmm: str) -> None:
    while True:
        now = datetime.now().strftime("%H:%M")
        if now >= hhmm:
            return
        time.sleep(20)


def main() -> int:
    ap = argparse.ArgumentParser(description="每晚收工：停桥接（可选关机）")
    ap.add_argument("--shutdown", action="store_true", help="停完桥接后关机")
    ap.add_argument("--at", default=None, help="等到这个时刻（HH:MM）再执行")
    ap.add_argument("--delay", type=int, default=60, help="关机倒计时秒数，默认 60")
    args = ap.parse_args()

    if args.at:
        print(f"等到 {args.at} 再执行…（Ctrl+C 取消）")
        try:
            wait_until(args.at)
        except KeyboardInterrupt:
            print("已取消。")
            return 0

    stop_bridge()
    if not args.shutdown:
        print("桥接已停。要连电脑一起关：python nightly_stop.py --shutdown")
        return 0

    print(f"{args.delay} 秒后关机（要取消就执行 shutdown /a）")
    subprocess.run(["shutdown", "/s", "/t", str(args.delay),
                    "/c", "wechat-bridge nightly stop"], capture_output=True, text=True)
    print("已安排关机。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
