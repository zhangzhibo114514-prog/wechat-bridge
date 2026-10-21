"""环境自检：一条命令看清“微信 hook 路线”当前卡在哪。

跑法：
    python check_env.py                      # 只读自检
    python check_env.py --proxy 127.0.0.1:7890   # 顺带验证代理能不能拉通 GitHub / PyPI

只读，不改任何系统设置、不碰微信；输出里每一步都会给出「已满足 / 缺什么 / 怎么补」。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OK, BAD, WARN = "[OK]  ", "[缺]  ", "[注意]"

# 输出流先托住：下面的 ffmpeg 自检会原样打印 `wechat_bridge.ffmpeg.status()`，
# 它缺 ffmpeg 时开头就是个「⚠」；中文 Windows 控制台是 cp936，编不出来直接
# UnicodeEncodeError ——**而"缺 ffmpeg"恰好是新机器最常见的状态**，
# 于是自检本来要告诉人「怎么补」，结果自己先崩了。见 wechat_bridge/console.py。
try:
    from wechat_bridge.console import install as _install_console

    _install_console()
except Exception:            # 自检工具本身不能因为托底手段而拒绝运行
    pass

# 4.x 分支适配框架公开声称的版本适配表（来自项目主页，仅供比对；本项目不替其真伪背书）
WXHOOK_SUPPORTED = [
    "3.6.0.18", "3.9.2.23", "3.9.5.81", "3.9.10.19", "3.9.10.27", "3.9.11.17", "3.9.12.15",
    "3.9.12.45", "3.9.12.51", "4.0.1.21", "4.0.3.22", "4.1.0.34", "4.1.2.17", "4.1.5.16",
    "4.1.6.14", "4.1.6.46", "4.1.7.30", "4.1.8.27", "4.1.8.67", "4.1.11.24", "4.1.12.26",
    "4.1.12.55", "4.1.13.12", "4.1.13.56",
]


def line(tag: str, text: str) -> None:
    print(f"{tag} {text}")


def check_wechat() -> None:
    print("\n== 微信客户端 ==")
    exe = Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe")
    legacy = Path(r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe")
    if exe.exists():
        ver = _file_version(exe)
        line(WARN, f"检测到微信 4.x：{exe}  版本 {ver}")
        _compare_wxhook(ver)
    elif legacy.exists():
        line(OK, f"检测到微信 3.9.x：{legacy}  版本 {_file_version(legacy)}（与 wcferry 支持线匹配）")
    else:
        line(BAD, "未在常规路径找到微信客户端（3.9.x 与 4.x 都没有）")

    running = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
        capture_output=True, text=True, errors="ignore",
    ).stdout
    line(OK if "Weixin.exe" in running or "WeChat.exe" in running else WARN,
         "微信进程：" + ("运行中" if "Weixin.exe" in running or "WeChat.exe" in running else "未运行（hook 需要客户端在线）"))


def _compare_wxhook(version: str) -> None:
    """把本机 4.x 版本号和公开适配表比对，直接告诉用户差在哪。"""
    if version in WXHOOK_SUPPORTED:
        line(OK, f"该版本在 4.x 适配表内（wxhook 支持列表），可直接按框架文档接入")
        return
    older = [v for v in WXHOOK_SUPPORTED if _vkey(v) < _vkey(version)]
    if older:
        line(BAD, f"本机 {version} 比适配表里最新的 {max(older, key=_vkey)} 还新 —— 框架大概率认不出这个客户端")
        line(WARN, "两条出路：①把客户端装成适配表内的版本（如 4.1.13.56）②先拿框架自带 demo 试，能连上再继续")
    else:
        line(WARN, f"本机 {version} 不在适配表内，也不比表内版本新（可能是精简版/内测版），需实测")


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def _file_version(path: Path) -> str:
    try:
        return str(subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{path}').VersionInfo.FileVersion"],
            capture_output=True, text=True, errors="ignore",
        ).stdout.strip() or "未知")
    except Exception:
        return "未知"


def check_python() -> None:
    print("\n== Python 环境 ==")
    ver = sys.version_info
    line(OK if (3, 9) <= (ver.major, ver.minor) <= (3, 12) else WARN,
         f"当前解释器 {ver.major}.{ver.minor}.{ver.micro} @ {sys.executable}")
    if not ((3, 9) <= (ver.major, ver.minor) <= (3, 12)):
        line(WARN, "wcferry 的官方 wheel 只到 Python 3.12；需另装 3.11/3.12 建虚拟环境（本机 3.14 装不上）")
    for mod, hint in (("wcferry", "pip install wcferry"), ("wxhook", "第三方框架，需按其文档安装")):
        found = importlib.util.find_spec(mod) is not None
        line(OK if found else BAD, f"{mod}: {'已安装' if found else '未安装（' + hint + '）'}")
    check_ffmpeg()


def check_ffmpeg() -> None:
    """识图的硬前置：微信 4.x 的图是 wxgf(HEVC)，没有 ffmpeg 就永远"看不到图"。"""
    try:
        from wechat_bridge.ffmpeg import status as ffmpeg_status

        text = ffmpeg_status()
    except Exception as exc:
        line(BAD, f"ffmpeg 自检失败：{exc!r}")
        return
    line(OK if not text.startswith("⚠") else BAD, text)
    if text.startswith("⚠"):
        line(WARN, "补法（任选其一）：装系统 ffmpeg 放进 PATH；或 `python pkg_fetch.py imageio-ffmpeg`"
                   "（解包进 .pylibs，自带 win64 二进制）；或设环境变量 WECHAT_BRIDGE_FFMPEG")


def check_network() -> None:
    print("\n== 网络（装 SDK 与调模型都要用）==")
    for host in ("pypi.org", "github.com"):
        try:
            socket.create_connection((host, 443), timeout=5).close()
            line(OK, f"{host}:443 可达")
        except Exception as exc:
            line(BAD, f"{host}:443 不可达（{exc}）— 装不了 SDK，需要开代理或用离线包")
    line(WARN, "直连不通时也可走镜像：pip install -i https://pypi.tuna.tsinghua.edu.cn/simple wcferry")


def check_llm(cfg_path: Path) -> None:
    print("\n== 模型接口 ==")
    if not cfg_path.exists():
        line(WARN, f"没有 {cfg_path.name}，先复制模板再填 base_url / model")
        return
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        opts = raw.get("llm", {}).get("options", {})
        base = str(opts.get("base_url", ""))
        line(OK, f"llm.adapter = {raw.get('llm', {}).get('adapter', '未设置')}")
        line(OK, f"model = {opts.get('model', '未设置')}   base_url = {base}")
        if base and "127.0.0.1" not in base and "localhost" not in base:
            try:
                urllib.request.urlopen(base, timeout=6)
                line(OK, f"{base} 可达")
            except Exception as exc:
                line(WARN, f"{base} 不可达（{exc}）— 若走官方 API 记得先解决网络出口")
        key = str(opts.get("api_key", ""))
        if key.startswith("env:"):
            # env:NAME 不是只能靠环境变量——同名条目也可能登记在 ~/.dsh/.credentials.yaml 里
            try:
                sys.path.insert(0, str(ROOT))
                from resolve_key import resolve_named

                found, source = resolve_named(key[4:])
            except Exception as exc:
                found, source = "", f"解析失败（{exc!r}）"
            line(OK if found else BAD,
                 f"api_key 钉在 {key[4:]}：{'可用 —— ' + source if found else '取不到值（' + source + '）'}")
        elif key:
            line(WARN, "api_key 直接写在配置文件里了，建议改成 env:变量名")
        else:
            try:
                sys.path.insert(0, str(ROOT))
                from resolve_key import find_key

                found, source = find_key()
            except Exception as exc:
                found, source = "", f"解析失败（{exc!r}）"
            line(OK if found else BAD,
                 f"api_key 留空=自动发现：{'命中 —— ' + source if found else '一个都没找到'}")
    except Exception as exc:
        line(BAD, f"配置文件解析失败：{exc}")


def main() -> int:
    print(f"项目目录：{ROOT}")
    check_wechat()
    check_python()
    check_network()
    check_llm(ROOT / "bridge_config.json")
    print("\n提示：链路本身随时可以用 python bridge.py --demo 验证（不需要微信、不需要网络、不需要凭据）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
