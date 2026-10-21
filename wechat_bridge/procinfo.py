"""进程与路径探测：微信在不在、数据目录在哪、某个 pid 还活着吗。

**为什么单独一个模块**：这几件事被多处用到——`bridge.py`（就绪自检）、
`agent_ctl.py`（无人值守入口）、以及将来的 CLI。各写一份的下场是
「同一个判断在不同命令里给出不同答案」——本项目已经踩过一次：
`--validate` 与 `--doctor` 的密钥判断不一致，同一份配置两个命令结论相反。

**沙箱注意**：DSH 沙箱会屏蔽进程枚举，`weixin_running()` 在里面**必然返回 False**。
这不是程序问题；真机判断要在普通终端里做。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

PYLIBS = Path(__file__).resolve().parent.parent / ".pylibs"

# 微信 4.x 的常见安装位置。找不到时再退回注册表。
WEIXIN_CANDIDATES = (
    Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
    Path(r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"),
    Path(r"C:\Program Files\Tencent\WeChat\WeChat.exe"),
    Path(r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe"),
)


def pid_alive(pid: int) -> bool:
    """进程是否还活着（打不开句柄一律按"死了"算，宁可误判成死也不误判成活）。"""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_lock_holder(lock_path: Path) -> Optional[int]:
    """从单实例锁文件里读出持有者 pid（文件形如 `pid=1234 started=...`）。"""
    try:
        head = lock_path.read_text(encoding="utf-8-sig").strip().splitlines()[0]
        return int(head.split("=", 1)[1].split()[0])
    except Exception:
        return None


def _tasklist_weixin() -> Optional[bool]:
    """用 tasklist 查微信进程。`None` = **查不了**（多半是被沙箱拒了进程枚举）。"""
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
            capture_output=True, text=True, errors="ignore",
        )
    except Exception:
        return None
    out = (proc.stdout or "")
    err = (proc.stderr or "")
    if "Weixin.exe" in out:
        return True
    if "Access denied" in err or "拒绝访问" in err or "拒绝访问" in out:
        return None
    if proc.returncode == 0 and ("INFO" in out or "信息" in out or not out.strip()):
        return False
    return None


def weixin_running() -> Optional[bool]:
    """微信主进程是否可见。

    **三态**：`True` 在跑 / `False` 确实没开 / `None` **看不到，不知道**。

    为什么必须区分 `None`：DSH 沙箱里进程枚举一律被拒（psutil 拿不到句柄、tasklist 报
    Access denied）。此时"看不到" ≠ "没开"，直接当没开就会把人带去修一个根本没坏的东西——
    `agent_ctl status` 就踩过：桥接明明在跑、心跳 9 秒前，它却报"微信进程：没开"。
    """
    verdict = _tasklist_weixin()
    if verdict is not None:
        return verdict
    try:
        import psutil                                        # 手工解包在 .pylibs 里
    except Exception:
        return None
    seen = 0
    try:
        for proc in psutil.process_iter(["name"]):
            seen += 1
            if (proc.info.get("name") or "").lower() in ("weixin.exe", "wechat.exe"):
                return True
    except Exception:
        return None                                          # 枚举被拒
    if seen == 0:
        return None                                          # 一个进程都看不到 ⇒ 权限问题，不是"没开"
    return False


def weixin_exe_path() -> Optional[Path]:
    """找到微信可执行文件。先查常见路径，再查注册表。"""
    for path in WEIXIN_CANDIDATES:
        if path.exists():
            return path
    try:
        import winreg

        for root, key in ((winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Weixin"),
                          (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat")):
            try:
                with winreg.OpenKey(root, key) as handle:
                    for name in ("InstallPath", "Path", "InstallDir"):
                        try:
                            value, _ = winreg.QueryValueEx(handle, name)
                        except OSError:
                            continue
                        candidate = Path(str(value))
                        if candidate.is_dir():
                            candidate = candidate / "Weixin.exe"
                        if candidate.exists():
                            return candidate
            except OSError:
                continue
    except Exception:
        pass
    return None


def find_account_dir() -> Optional[Path]:
    """找微信 4.x 的数据目录（xwechat_files）。

    本机实测：数据目录可能被改到 `%USERPROFILE%\\xwechat_files`，不在默认的 Documents 下，
    所以固定路径判断会误报"未登录"。按候选列表找，最后用库自己的探测兜底。
    """
    candidates = [
        Path.home() / "xwechat_files",
        Path.home() / "Documents" / "xwechat_files",
        Path.home() / "Documents" / "WeChatFiles",
        Path.home() / "Documents" / "WeChat Files",
        Path("D:/xwechat_files"),
        Path("E:/xwechat_files"),
    ]
    for path in candidates:
        if path.is_dir():
            return path
    try:                                                     # 库的探测读注册表与配置，最准
        if PYLIBS.exists() and str(PYLIBS) not in sys.path:
            sys.path.insert(0, str(PYLIBS))
        from wechatauto.db import auto_detect_db_dir

        detected = auto_detect_db_dir()
        if detected:
            return Path(detected)
    except Exception:
        pass
    return None


def account_dirs(root: Optional[Path] = None) -> list[str]:
    """数据目录下的**账号**目录名（形如 `wxid_xxx_abcd`）。

    只认 `wxid_` 开头的：同一个目录下还有 `all_users` / `Backup` 这类**系统目录**，
    把它们算成账号会让"微信开着但没登录"被误判成"已登录"——那样 `up` 就会跳过
    "请先登录"的提示，直接去启动桥接，然后以一句"没检测到 Weixin.exe"失败收场。
    """
    root = root or find_account_dir()
    if not root or not root.is_dir():
        return []
    try:
        return sorted(p.name for p in root.iterdir()
                      if p.is_dir() and p.name.startswith("wxid_"))
    except OSError:
        return []
