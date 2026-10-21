"""控制台安全：中文 Windows 默认编码是 cp936（GBK），打印不了的字符会**把命令打死**。

## 为什么需要这个模块

cp936 里没有 U+2714「✔」、U+2718「✘」、U+26A0「⚠」。`print` 一个这样的字符，
Python 在写流时抛 `UnicodeEncodeError`，未捕获 → 整个进程带着 traceback 退出：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2714' in position 0

用户第一次装的电脑就是这样——`python agent_ctl.py status` 是他敲的第一条命令，
于是第一个印象是"这玩意儿一跑就报错"。开发机上没暴露，是因为开发流程习惯带
`PYTHONIOENCODING=utf-8`，把问题盖住了（**掩盖工具的输出编码 ≠ 工具的默认行为正确**）。

## 两条路，按"谁来读"分开

`install()` 看第一个流是不是终端，选不同的策略：

- **终端**（人看）：**不动编码**。控制台是 cp936，硬塞 UTF-8 字节会变成乱码；
  只把 `errors` 改成 `replace`，并把对勾降级成 cp936 真有、且长得像的 `√` / `×`。
- **管道 / 重定向**（机器读）：改成 UTF-8。消费方（agent、`Get-Content`、
  另一个脚本）默认按 UTF-8 解码，这里顺带把 `✔` 保住。

这和 `bridge.py` 里的 `_make_stdout_utf8()` 是**同一类但不同场景**的处理：
桥接的 stdout 永远重定向进日志文件，所以它无条件用 UTF-8；CLI 还要给人看，所以分岔。
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence, Tuple

#: 候选记号，按「好看程度」从高到低；cp936 里 √ 和 × 都有，U+2714/2718 没有。
_GROUPS: Tuple[Tuple[str, str, str], ...] = (
    ("✔", "✘", "⚠"),      # UTF-8 / 现代终端
    ("√", "×", "!"),        # cp936：√ 与 × 都在 GBK 里，警告号没有就退成 !
    ("[OK]", "[X]", "[!]"),  # ascii 兜底
)


def _can_encode(text: str, encoding: str) -> bool:
    try:
        text.encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def _pick_marks(encoding: Optional[str] = None) -> Tuple[str, str, str]:
    """挑一组当前流真能显示的对勾/叉/警告号。

    `encoding` 只用于测试（模拟 cp936 机器），正常运行取 `sys.stdout.encoding`。
    """
    enc = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    for ok, bad, warn in _GROUPS:
        if _can_encode(ok, enc) and _can_encode(bad, enc) and _can_encode(warn, enc):
            return ok, bad, warn
    return _GROUPS[-1]


#: 导入即可用的记号（按导入时的真实 stdout 猜）。要按流重新算，用 `install()` 的返回值。
OK, BAD, WARN = _pick_marks()


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:                       # pythonw 下 stdout 是 None
        return False


def _reconfigure(stream, **kwargs) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(**kwargs)
    except (ValueError, OSError):            # 流已关 / 不支持，不值得为此中断
        pass


def install(streams: Optional[Sequence] = None) -> Tuple[str, str, str]:
    """把输出流改成"编不出来也不会崩"，并返回该流能用的 `(OK, BAD, WARN)`。

    用法：`OK, BAD, WARN = console.install()`，位置在**任何 print 之前**。
    """
    targets = tuple(streams) if streams else (sys.stdout, sys.stderr)
    first = targets[0] if targets else sys.stdout
    interactive = _isatty(first)
    if interactive:
        # 控制台：保留编码（乱改会乱码），只兜住编码失败
        for stream in targets:
            _reconfigure(stream, errors="replace")
        marks = _pick_marks(getattr(first, "encoding", None))
    else:
        # 管道 / 重定向：消费方按 UTF-8 读，记号也不用降级
        for stream in targets:
            _reconfigure(stream, encoding="utf-8", errors="replace")
        marks = _pick_marks("utf-8")
    return marks
