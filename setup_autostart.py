"""登记/移除「开机自动启动微信 AI 群友」的计划任务（自愈 + 重试）。

## 为什么需要

代理跑在**你自己的电脑上**——关机 / 睡眠 / 锁屏时它都不工作（没有服务器可以代跑）。
关机绕不过去，但至少要做到"开机登录后自己起来、崩了自己恢复"，不用每次手动敲命令。

## 任务做了什么

跑的是 `agent_ctl.py up`——**和 agent 手动拉起用的是同一个入口**，而且是**幂等**的：
已经在跑就什么都不做。这一点很关键，因为任务会**每 10 分钟**执行一次。

    触发 1：当前用户登录时（延迟 1 分钟，等微信自己先起来）
    触发 2：每 10 分钟一次，**永不过期**  ← 自愈：跑着跑着崩了也能自己回来
    动作：  pythonw agent_ctl.py up      （pythonw 不弹黑窗）

为什么需要触发 2：计划任务自带的 `RestartOnFailure` 只覆盖「**启动失败**」，
不覆盖「**跑着跑着卡死**」；而且 Windows **不会因为睡眠唤醒而补触发**登录触发器。
一个 10 分钟一次的幂等检查把这两种情况一起兜住了。

## 用法（在当前用户账户下，不需要管理员）

    python setup_autostart.py install
    python setup_autostart.py status
    python setup_autostart.py run        # 立刻试跑一次（验证配置对不对）
    python setup_autostart.py remove
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASK_NAME = "WeChatBridge"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
AGENT_CTL = ROOT / "agent_ctl.py"


def run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, errors="ignore",
    )


def build_xml(interval_minutes: int = 10) -> str:
    python = PYTHONW if PYTHONW.exists() else Path(sys.executable)
    # StartBoundary 必须是过去的某个时刻，否则任务不会立刻开始重复
    start = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>微信 AI 群友：登录后自动启动 + 每 {interval_minutes} 分钟自愈检查（{ROOT}）</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT{interval_minutes}M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>"{AGENT_CTL}" up --wait 120</Arguments>
      <WorkingDirectory>{ROOT}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _create(task_name: str, xml: str) -> subprocess.CompletedProcess:
    xml_path = ROOT / "state" / f"_{task_name}.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    result = run_ps(f'schtasks /Create /TN "{task_name}" /XML "{xml_path}" /F')
    try:
        xml_path.unlink()
    except OSError:
        pass
    return result


def install() -> int:
    result = _create(TASK_NAME, build_xml())
    print(result.stdout.strip() or result.stderr.strip())
    ok = result.returncode == 0
    if not ok:
        print("登记失败，看上面的报错。")
        return 1
    print("登记成功。")
    print("  触发 1：登录后 1 分钟自动启动")
    print("  触发 2：每 10 分钟检查一次，不在跑就拉起（自愈）")
    print("  注意：睡眠唤醒不会补触发登录触发器，但会被触发 2 兜住（最多 10 分钟）")
    print(f"\n看状态：python {Path(__file__).name} status")
    print(f"不想要了：python {Path(__file__).name} remove")
    return 0


def remove() -> int:
    result = run_ps(f'schtasks /Delete /TN "{TASK_NAME}" /F')
    print(result.stdout.strip() or result.stderr.strip())
    return 0 if result.returncode == 0 else 1


def status() -> int:
    result = run_ps(
        f'schtasks /Query /TN "{TASK_NAME}" /V /FO LIST | Select-String -Pattern '
        '"TaskName|Status|Last Run Time|Last Result|Next Run Time|状态|上次运行时间|上次结果|下次运行时间"'
    )
    print(result.stdout.strip() or "(没有查到任务，可能还没登记)")
    hb = ROOT / "state" / "heartbeat.log"
    if hb.exists():
        last = hb.read_text(encoding="utf-8-sig").strip().splitlines()[-1]
        print(f"\n最近一次心跳：\n  {last}")
        print("（心跳时间距今超过几分钟，说明代理没在跑或已卡住）")
    else:
        print("\n还没有 state/heartbeat.log —— 代理从未成功跑起来过。")
    print("\n更直观的判断：python agent_ctl.py status")
    return 0


def run_now() -> int:
    result = run_ps(f'schtasks /Run /TN "{TASK_NAME}"')
    print(result.stdout.strip() or result.stderr.strip())
    print("已请求立即运行；等 10 秒再跑 `python agent_ctl.py status` 看有没有起来。")
    return 0 if result.returncode == 0 else 1


def validate_xml(xml: str) -> list[str]:
    """离线校验任务 XML：良构 + 必需节点 + 重复触发器。返回问题列表（空 = 通过）。

    为什么要有这一步：`schtasks /Create` 在**DSH 沙箱里必然失败**（它要写系统的 Tasks 目录），
    而那个报错（"系统找不到指定的路径"）看起来像"XML 写错了"——很容易被误导成去改 XML。
    所以先把能离线验的验掉，剩下的失败才归因于权限。
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return [f"XML 不是良构的：{exc}"]

    for elem in root.iter():                                  # 去掉命名空间，路径才好写
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    required = [
        "RegistrationInfo",
        "Triggers/LogonTrigger",
        "Triggers/TimeTrigger/Repetition/Interval",
        "Principals/Principal",
        "Settings",
        "Actions/Exec/Command",
        "Actions/Exec/Arguments",
        "Actions/Exec/WorkingDirectory",
    ]
    problems = [f"缺少必需节点 {path}" for path in required if root.find(path) is None]

    interval = root.find("Triggers/TimeTrigger/Repetition/Interval")
    if interval is not None and not (interval.text or "").startswith("PT"):
        problems.append(f"重复间隔必须是 ISO8601 时长（形如 PT10M），收到 {interval.text!r}")

    command = root.find("Actions/Exec/Command")
    if command is not None and not (command.text or "").lower().endswith((".exe",)):
        problems.append(f"Command 应当是可执行文件，收到 {command.text!r}")

    return problems


def selftest() -> int:
    """离线校验任务 XML；再试着真注册一次（沙箱里会失败，属预期）。"""
    xml = build_xml()
    problems = validate_xml(xml)
    print("-- 离线校验任务 XML --")
    for problem in problems:
        print(f"  [失败] {problem}")
    if not problems:
        print("  [OK] XML 良构，必需节点齐全")
        print("  [OK] 含登录触发器 + 每 10 分钟重复触发器（自愈）")
    ok = not problems

    print("\n-- 试注册（用临时任务名，不动正式的 WeChatBridge）--")
    probe = f"{TASK_NAME}_selftest"
    result = _create(probe, xml)
    registered = result.returncode == 0
    output = (result.stdout or "") + (result.stderr or "")
    if registered:
        query = run_ps(f'schtasks /Query /TN "{probe}" /FO LIST')
        has_next = "Next Run Time" in query.stdout or "下次运行时间" in query.stdout
        print(f"  [{'OK' if has_next else '失败'}] 已登记且带下次运行时间（重复触发器生效）")
        ok &= has_next
        run_ps(f'schtasks /Delete /TN "{probe}" /F')
        print("  [OK] 临时任务已清理")
    else:
        print(f"  [跳过] 注册被拒：{output.strip().splitlines()[0] if output.strip() else '(无输出)'}")
        print("         这通常是**沙箱**造成的（注册任务要写系统的 Tasks 目录）。")
        print("         请在**你自己的终端**里跑：python setup_autostart.py install")

    print("\n结论：" + ("任务 XML 可用" if ok else "存在问题，见上"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="微信 AI 群友 · 开机自启任务管理")
    ap.add_argument("action", choices=["install", "remove", "status", "run", "selftest"])
    args = ap.parse_args()
    return {"install": install, "remove": remove, "status": status,
            "run": run_now, "selftest": selftest}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
