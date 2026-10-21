"""无人值守入口的离线自测：`agent_ctl` / `procinfo` / `setup_autostart` 的任务 XML。

跑法：python agent_selftest.py

**重点测的是幂等性**：开机自启任务每 10 分钟会调一次 `up`。如果它不幂等，
就会每 10 分钟多起一个桥接进程，而**多个桥接同时挂着同一个号 = 同一条消息被回复多次**，
且各自的限速计数互相看不见。这个后果比"起不来"严重得多，所以必须在离线阶段钉死。

不碰微信、不联网、不改任何真实状态：锁与心跳一律指向 `state/_ctltest*/` 下的临时文件。

**这条是硬约束，不是口号**：曾经用真实锁文件测 `down`，而机器上正跑着桥接，
于是"跑一次单元测试"变成了"去 taskkill 真实进程"（`state/bridge.lock` 也是桥接
自己判断陈旧锁的依据，被测试改写会让它在生产里做出错误决定）。
要动真实状态就**另开一个显式命名的集成测试**，别混进离线自测里。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import agent_ctl                                                          # noqa: E402
import setup_autostart                                                    # noqa: E402
from wechat_bridge import console, procinfo, state                           # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else '失败'}] {label}{(' —— ' + detail) if detail else ''}")
    return ok


def capture(func, *a, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*a, **kw)
    return buf.getvalue()


# ---------------------------------------------------------------- procinfo

def test_procinfo() -> bool:
    ok = True
    print("-- 进程与路径探测 --")
    ok &= check("自己的 pid 判定为存活", procinfo.pid_alive(os.getpid()))
    ok &= check("pid=0 判定为不存活", not procinfo.pid_alive(0))
    ok &= check("不存在的 pid 判定为不存活", not procinfo.pid_alive(999_999_999))

    # 注意：**不要用 tempfile.mkdtemp()** —— 系统临时目录在 DSH 沙箱里不可写
    # （PermissionError），本项目的自测一律把临时物放在工作区的 state/ 下。
    tmp = ROOT / "state" / "_ctltest"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        lock = tmp / "bridge.lock"
        lock.write_text(f"pid={os.getpid()} started=2026-01-01T00:00:00\n", encoding="utf-8")
        ok &= check("能从锁文件里读出 pid", procinfo.read_lock_holder(lock) == os.getpid())
        lock.write_text("这不是锁文件\n", encoding="utf-8")
        ok &= check("锁文件内容异常时返回 None（不抛异常）", procinfo.read_lock_holder(lock) is None)
        ok &= check("锁文件不存在时返回 None", procinfo.read_lock_holder(tmp / "nope.lock") is None)

        # 账号目录：**只认 wxid_ 开头**。把 Backup/all_users 算成账号会让
        # "微信开着但没登录"被误判成"已登录"，于是 up 跳过提示直接去启动桥接然后失败。
        acct = tmp / "xwechat_files"
        for name in ("wxid_aaa_1111", "wxid_bbb_2222", "all_users", "Backup", "notes.txt"):
            path = acct / name
            if name.endswith(".txt"):
                path.write_text("x", encoding="utf-8")
            else:
                path.mkdir(parents=True)
        found = procinfo.account_dirs(acct)
        ok &= check("账号目录只认 wxid_ 开头的", found == ["wxid_aaa_1111", "wxid_bbb_2222"], str(found))
        ok &= check("目录不存在时返回空表不抛异常", procinfo.account_dirs(tmp / "nope") == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


# ---------------------------------------------------------------- agent_ctl

def test_agent_ctl() -> bool:
    ok = True
    print("\n-- 状态采集与输出 --")

    status = agent_ctl.collect_status()
    required = {"running", "pid", "wechat", "account_dir", "logged_in",
                "last_heartbeat_sec", "mode", "blocked", "hint"}
    ok &= check("collect_status 含全部文档化字段", required <= set(status), str(sorted(set(status))))
    ok &= check("结果可 JSON 序列化（agent 要能直接读）",
                json.dumps(status, ensure_ascii=False) != "")
    ok &= check("blocked 是列表", isinstance(status["blocked"], list))

    text = capture(agent_ctl.print_status, status, False)
    ok &= check("人话输出含三行关键信息",
                "桥接进程" in text and "微信进程" in text and "运行模式" in text, text.splitlines()[0])
    ok &= check("人话输出会给出下一步", "下一步" in text or "一切正常" in text)

    as_json = capture(agent_ctl.print_status, status, True)
    ok &= check("--json 输出可被解析回 dict",
                isinstance(json.loads(as_json), dict))

    print("\n-- up 的幂等性（最关键的一条）--")
    # 全程用**临时**的锁与心跳文件，绝不动真实的 state/bridge.lock。
    # 旧写法是"备份真实文件 → 覆盖 → 还原"：覆盖期间真锁里写着测试进程的 pid，
    # 而机器上可能正跑着桥接——桥接自己会读这个锁（判断自己是不是陈旧锁），
    # `down` 也会读它。测试与生产抢同一个文件，属于自找的竞态。
    tmpdir = ROOT / "state" / "_ctltest_up"
    tmpdir.mkdir(parents=True, exist_ok=True)
    lock, beat = tmpdir / "bridge.lock", tmpdir / "heartbeat.log"
    saved_paths = (agent_ctl.LOCK, agent_ctl.HEARTBEAT)
    agent_ctl.LOCK, agent_ctl.HEARTBEAT = lock, beat
    # 冒充"微信在跑、账号已登录"，否则 up 会真的去拉起微信
    saved_wx, saved_acct = agent_ctl.weixin_running, agent_ctl.account_dirs
    agent_ctl.weixin_running = lambda: True
    agent_ctl.account_dirs = lambda *a, **kw: ["wxid_fake_0000"]
    stale = datetime.now() - timedelta(seconds=agent_ctl.HEARTBEAT_STALE_SEC * 2)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"pid={os.getpid()} started=2026-01-01T00:00:00\n", encoding="utf-8")

        # A) 进程在 + 心跳新鲜 ⇒ 真正健康，up 必须什么都不做
        beat.write_text(f"{datetime.now().isoformat(timespec='seconds')} | note=beat\n", encoding="utf-8")
        out = capture(agent_ctl.cmd_up, Namespace(wait=1, json=False))
        ok &= check("健康在跑时 up 只报告、不再起进程",
                    "已经在跑" in out and "不做任何事" in out, out.strip().splitlines()[0])

        # B) 进程在 + 心跳停住 ⇒ 应提示"卡住了"，且**不**盲目再起一个
        beat.write_text(f"{stale.isoformat(timespec='seconds')} | note=beat\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = agent_ctl.cmd_up(Namespace(wait=1, json=False))
        out = buf.getvalue()
        ok &= check("心跳停住时提示卡住而不是再起一个",
                    "心跳停在" in out and rc == 1, out.strip().splitlines()[0])

        # C) 陈旧锁（pid 已死）不该被当成"在跑"
        lock.write_text("pid=999999999 started=2026-01-01T00:00:00\n", encoding="utf-8")
        st = agent_ctl.collect_status()
        ok &= check("陈旧锁（pid 已死）不算在跑",
                    not st["running"] and "bridge_not_running" in st["blocked"])
    finally:
        agent_ctl.weixin_running, agent_ctl.account_dirs = saved_wx, saved_acct
        agent_ctl.LOCK, agent_ctl.HEARTBEAT = saved_paths
        shutil.rmtree(tmpdir, ignore_errors=True)     # 用完即删，真实状态一点没碰

    print("\n-- down 的边界 --")
    # ⚠ 这一条必须把锁指到**临时路径**再测。
    # 直接用真实锁的话：机器上正跑着桥接时，`down` 会去 `taskkill` **真实进程**——
    # 等于"跑一次单元测试就杀掉生产进程"。这与当初 selftest.ps1 删掉真实
    # state/audit.jsonl 是同一类错误：**测试污染了它本该只观察的东西**。
    # 已经实测踩到过：本机桥接在跑，这里打印出「正在停止 pid=17860 …」并判失败。
    tmpdir = ROOT / "state" / "_ctltest_down"
    tmpdir.mkdir(parents=True, exist_ok=True)
    saved_lock = agent_ctl.LOCK
    agent_ctl.LOCK = tmpdir / "nope.lock"          # 指向不存在的锁 ⇒ 必然"没在跑"
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = agent_ctl.cmd_down(Namespace())
        out = buf.getvalue()
    finally:
        agent_ctl.LOCK = saved_lock
        shutil.rmtree(tmpdir, ignore_errors=True)
    # 标签原来写着"返回 0"却只检查了文案——顺手把返回值也真正断言上。
    ok &= check("没在跑时 down 是安全的（返回 0，不报错）",
                rc == 0 and "没在跑" in out, f"rc={rc} 输出={out.strip()!r}")
    return ok


# ---------------------------------------------------------------- 自启任务

def test_autostart() -> bool:
    ok = True
    print("\n-- 开机自启任务 XML --")
    xml = setup_autostart.build_xml()
    problems = setup_autostart.validate_xml(xml)
    ok &= check("生成的 XML 通过离线校验", not problems, "; ".join(problems))
    ok &= check("跑的是幂等入口 agent_ctl.py up", "agent_ctl.py" in xml and " up" in xml)
    ok &= check("含登录触发器", "<LogonTrigger>" in xml)
    ok &= check("含每 10 分钟重复触发器（自愈）", "<Interval>PT10M</Interval>" in xml)
    ok &= check("重复不会因时长到期而停", "<StopAtDurationEnd>false</StopAtDurationEnd>" in xml)
    ok &= check("用 pythonw（不弹黑窗）", "pythonw" in xml.lower())
    ok &= check("允许多个实例被忽略（防重复启动）",
                "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml)
    ok &= check("不因电池而停（笔记本上也要跑）",
                "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml)

    print("\n-- 校验器本身要能挑出毛病 --")
    ok &= check("坏 XML 被拦下", setup_autostart.validate_xml("<Task><broken>") != [])
    missing = setup_autostart.validate_xml(
        '<Task><RegistrationInfo/><Triggers/><Principals/><Settings/></Task>')
    ok &= check("缺必需节点被拦下", len(missing) >= 3, f"{len(missing)} 项")
    bad_interval = setup_autostart.validate_xml(xml.replace("PT10M", "10 minutes"))
    ok &= check("非法的重复间隔被拦下", len(bad_interval) >= 1, "; ".join(bad_interval))
    return ok


def test_audit_rotation() -> bool:
    """审计轮转：一个热闹的群 + 每条带提示词，几个月就能把 jsonl 撑到几十兆。

    这里钉两件事：①到上限会轮转；②只留最近 keep 个归档。
    还有一条不写出来的坑：Windows 的 `rename` 在目标已存在时**直接失败**（不像 POSIX 覆盖），
    所以轮转文件名必须唯一直到微秒级，否则同一秒里连转两次就会静默失效。
    """
    ok = True
    print("\n-- 审计轮转 --")
    tmp = ROOT / "state" / "_audittest"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        log = state.AuditLog(tmp / "audit.jsonl", max_bytes=4096, keep=3)
        for i in range(300):
            log.write({"event": "ignored", "text": "x" * 80, "i": i})
        active = (tmp / "audit.jsonl").stat().st_size
        archives = sorted(p.name for p in tmp.glob("audit-*.jsonl"))
        ok &= check("达到上限后会轮转（活跃文件不再无限增长）", active <= 4096 * 2, f"{active} 字节")
        ok &= check("归档只留 keep 个", len(archives) == 3, str(archives))
        ok &= check("活跃文件名不变（工具照旧读 audit.jsonl）", (tmp / "audit.jsonl").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


class _FakeStream:
    """假的输出流：只有 install() 会问的三件事（编码 / 是不是终端 / reconfigure）。"""

    def __init__(self, encoding: str, tty: bool) -> None:
        self.encoding = encoding
        self._tty = tty
        self.calls: list = []

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_console_encoding() -> bool:
    """中文 Windows 控制台（cp936）打不出 ✔/✘/⚠ —— 这是**新用户的第一条命令**。

    第一次装机跑 `python agent_ctl.py status` 就是在这上面炸的：
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2714'`，
    整个进程带 traceback 退出。开发机上没暴露，是因为开发流程习惯带
    `PYTHONIOENCODING=utf-8`——掩盖工具的输出编码不等于工具的默认行为正确。

    两条路要分开断言，因为它们**不该用同一个办法**：
      终端（人看）→ 编码不动，只降级记号；硬塞 UTF-8 字节到 cp936 控制台是乱码。
      管道（机器读）→ 改 UTF-8，记号不用降级。
    """
    ok = True
    print("\n-- 控制台编码（cp936 机器上不能崩）--")
    ok &= check("UTF-8 流用 ✔/✘/⚠", console._pick_marks("utf-8") == ("✔", "✘", "⚠"),
                str(console._pick_marks("utf-8")))
    ok &= check("cp936(GBK) 降级成 √/×/!（GBK 里确实有这三个）",
                console._pick_marks("gbk") == ("√", "×", "!"), str(console._pick_marks("gbk")))
    ok &= check("纯 ASCII 流再降级成 [OK]/[X]/[!]",
                console._pick_marks("ascii") == ("[OK]", "[X]", "[!]"),
                str(console._pick_marks("ascii")))

    tty = _FakeStream("gbk", True)
    tty_marks = console.install(streams=(tty,))
    ok &= check("终端：记号降级成 GBK 能显示的 √/×/!",
                tty_marks == ("√", "×", "!"), str(tty_marks))
    ok &= check("终端：**不动编码**，只把错误从抛异常改成替换（否则中文乱码）",
                tty.calls == [{"errors": "replace"}], str(tty.calls))

    pipe = _FakeStream("gbk", False)
    pipe_marks = console.install(streams=(pipe,))
    ok &= check("管道：改成 UTF-8（消费方按 UTF-8 读）",
                pipe.calls == [{"encoding": "utf-8", "errors": "replace"}], str(pipe.calls))
    ok &= check("管道：记号不降级，✔ 保得住", pipe_marks == ("✔", "✘", "⚠"), str(pipe_marks))

    def run_status(extra_env: dict) -> tuple:
        env = dict(os.environ)
        env.update(extra_env)
        proc = subprocess.run([sys.executable, "agent_ctl.py", "status"],
                              cwd=str(ROOT), env=env, capture_output=True, timeout=120)
        return proc.stdout, proc.stderr, proc.returncode

    for label, extra in (("默认环境（沿用机器 cp936）", {"PYTHONIOENCODING": ""}),
                         ("强制 PYTHONIOENCODING=gbk", {"PYTHONIOENCODING": "gbk"})):
        try:
            raw, err, rc = run_status(extra)
        except Exception as exc:                   # 起不来也算失败，别静默跳过
            ok &= check(f"{label}：status 跑通", False, repr(exc))
            continue
        text = raw.decode("utf-8", errors="replace")
        ok &= check(f"{label}：退出码 0（原来抛异常退出 1）", rc == 0, f"rc={rc}")
        ok &= check(f"{label}：没有 traceback",
                    b"Traceback" not in raw and b"Traceback" not in err,
                    err.decode("utf-8", errors="replace")[-200:])
        ok &= check(f"{label}：中文能按 UTF-8 读回「桥接进程」", "桥接进程" in text,
                    text.strip().splitlines()[0] if text.strip() else "(空)")
    return ok


def main() -> int:
    print("=" * 64)
    print("无人值守入口自测")
    print("=" * 64)
    ok = test_procinfo()
    ok &= test_agent_ctl()
    ok &= test_autostart()
    ok &= test_audit_rotation()
    ok &= test_console_encoding()
    print("\n" + "=" * 64)
    print("结论：" + ("无人值守入口自测通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
