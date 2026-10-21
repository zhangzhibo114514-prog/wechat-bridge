"""打包：把这个仓库打成一个**能给别人**的 zip。

## 为什么要有这个脚本，而不是右键压缩

仓库里混着**绝对不能发出去**的东西：

- `state/`：真实审计、长记忆、草稿、心跳——里面有聊天内容；
- `.pylibs/`：几百兆离线依赖；
- `bridge_config.json` 与各种 `.bak-*`：**带真实 wxid、真实群 id、真实的限速状态**；
- `.env`、`.tmp/`、`__pycache__/`。

手动压缩必然漏一次，而漏一次的代价是把号主的微信号和群 id 发给陌生人。
所以这里一律用**白名单**：只有 `FILES` / `DIRS` 里列出来的东西会进包，
新增文件默认不进——想发出去，回来把它加进列表。

## 还有两步是"发之前"必须做的

1. **脱敏**：真实 wxid / 群 id / 本机用户名替换成占位符（见 `SANITIZE`）。
2. **自查**：打完扫一遍，剩下任何真实 wxid、`sk-` 开头的密钥就直接中止，
   并把命中的文件打出来。宁可这次不打包，也不要发出去再撤回。

## 跑法

    python make_release.py                    # 版本号取自 VERSION 文件
    python make_release.py --version 0.1.1    # 指定版本（对外发布才升号）
    python make_release.py --dry-run          # 只打印清单，不写盘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
NAME = "wechat-bridge"

#: 只有白名单里的文件会进包。新增文件要发出去，先加到这里。
FILES = [
    # —— 程序 ——
    "bridge.py", "agent_ctl.py", "make_config.py", "check_env.py", "resolve_key.py",
    "pkg_fetch.py", "list_contacts.py", "send_check.py", "probe_send_cache.py",
    "digest.py", "analyze_drafts.py", "nightly_stop.py", "setup_autostart.py",
    # —— 工具 / 探针 ——
    "persona_probe.py", "probe_input_box.py", "probe_image.py", "probe_wechat.py", "probe_vision_endpoints.py",
    "probe_bg_uia.py",
    "dump_raw_group.py", "vision_check.py", "smoke_wechatauto.py", "wxhook_probe.py",
    "fetch_github.py", "hook_fake_server.py", "fake_llm_server.py", "make_release.py",
    # —— 配置面板（本机网页，只用标准库）——
    "panel/panel_server.py", "panel/panel.html", "launch_panel.cmd",
    # —— 自测 ——
    "adapter_selftest.py", "agent_selftest.py", "bg_selftest.py", "debounce_selftest.py",
    "gate_selftest.py", "group_selftest.py", "key_resolve_selftest.py",
    "llm_endpoint_selftest.py", "long_memory_selftest.py", "proactive_selftest.py",
    "panel_selftest.py", "speak_gate_selftest.py",
    "segment_selftest.py", "vision_selftest.py", "selftest.ps1",
    # —— 启动器 / 配置模板 ——
    "run_bridge.cmd", "bridge_probe_wechat.cmd", "bridge_config.template.json",
    ".env.example", ".gitignore", ".deps.txt", "demo_chat.jsonl",
    # —— 文档 ——
    "README.md", "CHANGELOG.md", "LICENSE", "README_FULL.md", "STATUS.md", "DESIGN.md", "DESIGN-BACKGROUND.md",
    "OPEN-BUGS.md", "KNOWN_ISSUES.md", "NEXT-SESSION.md", "ACCEPTANCE.md",
]

#: 整个目录都要（只收 .py，跳过 __pycache__）
DIRS = ["wechat_bridge"]

#: 脱敏：真实身份 → 占位符。顺序有讲究：长路径先换，再换用户名，最后换昵称。
SANITIZE = [
    (r"wxid_your_account", "wxid_your_account"),
    (r"wxid_other_user", "wxid_other_user"),
    (r"11111111111@chatroom", "11111111111@chatroom"),
    (r"22222222222@chatroom", "22222222222@chatroom"),
    (r"C:\\Users\\<你>\\Documents\\deepseek-harness\\default-workspace\\wechat-bridge",
     "<微信桥接目录>"),
    (r"C:\\Users\\<你>", r"C:\\Users\\<你>"),
    (r"<你>", "<你>"),
    (r"阿甲", "阿甲"),
    (r"阿乙", "阿乙"),
]

#: 打完包再扫这些，扫到就中止（这些是**不该存在**的东西，不是脱敏对象）
FORBIDDEN = [
    (r"wxid_your_account", "真实账号 wxid"),
    (r"wxid_other_user", "真实测试号 wxid"),
    (r"11111111111@chatroom", "真实群 id"),
    (r"22222222222@chatroom", "真实群 id"),
    (r"<你>", "本机用户名"),
    (r"sk-[A-Za-z0-9]{20,}", "疑似 API Key"),
]

#: 打包后要跑的离线自测（不需要微信、不需要 .pylibs）
SMOKE_TESTS = ["segment_selftest.py", "group_selftest.py", "gate_selftest.py"]


def sanitize(text: str) -> str:
    for pattern, repl in SANITIZE:
        text = re.sub(pattern, repl, text)
    return text


def collect() -> list[tuple[Path, str]]:
    """返回 [(源文件, 包内相对路径)]，顺序稳定。"""
    picked: list[tuple[Path, str]] = []
    for name in FILES:
        path = ROOT / name
        if not path.exists():
            print(f"  [跳过] {name}（仓库里没有）")
            continue
        picked.append((path, name))
    for dirname in DIRS:
        base = ROOT / dirname
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            picked.append((path, path.relative_to(ROOT).as_posix()))
    return picked


def stage(items: list[tuple[Path, str]], version: str) -> tuple[Path, list[str]]:
    target = DIST / f"{NAME}-{version}"
    if target.exists():
        shutil.rmtree(target)          # 旧包留着会让"打的是哪一版"变成悬案
    target.mkdir(parents=True)
    written: list[str] = []
    for source, rel in items:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            dest.write_bytes(raw)      # 二进制原样拷
        else:
            bom = raw.startswith(b"\xef\xbb\xbf")
            cleaned = sanitize(text)
            # newline="" 关掉换行翻译：write_text 在 Windows 上会把 \n 写成 \r\n，
            # 原文本来就是 \r\n 时（.ps1/.cmd）就变成 \r\r\n，行数直接翻倍。真踩过
            # —— README_FULL.md 在包里一度变成 1486 行。
            with open(dest, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
                fh.write(cleaned)
        written.append(rel)
    (target / "VERSION").write_text(version + "\n", encoding="utf-8")
    written.append("VERSION")
    return target, written


def verify(target: Path) -> bool:
    """编译一遍 + 跑几个离线自测。包里的东西必须是**能跑的代码**，不是一堆文件。"""
    ok = True
    py_files = sorted(target.rglob("*.py"))
    print(f"\n[校验] py_compile {len(py_files)} 个文件")
    for path in py_files:
        # 必须显式 utf-8：子进程按我们的约定输出 utf-8，而 Windows 默认按 gbk 解码，
        # 直接 text=True 会拿中文报错（UnicodeDecodeError: 'gbk' codec ...）——
        # 这一条踩过，别改回默认。
        result = subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print(f"  [失败] {path.relative_to(target)}: {(result.stderr or '').strip()[:200]}")
            ok = False
    for name in SMOKE_TESTS:
        path = target / name
        if not path.exists():
            continue
        result = subprocess.run([sys.executable, name], cwd=target,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = result.stdout or ""
        tail = [line for line in out.splitlines() if "结论" in line or "失败" in line]
        passed = result.returncode == 0
        print(f"  [{'通过' if passed else '失败'}] {name}: {tail[-1].strip() if tail else ''}")
        if not passed:
            print("      " + (out or result.stderr or "")[-300:].replace("\n", " | "))
        ok &= passed
    return ok


def scan(target: Path) -> bool:
    """扫包内所有文本文件，命中 FORBIDDEN 就中止。

    **跳过 `__pycache__` 与 `.pyc`**：py_compile 产出的字节码里嵌着绝对路径
    （含本机用户名），扫它必然报警——但那些文件本来就不会进 zip。
    """
    problems = []
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in FORBIDDEN:
            if re.search(pattern, text):
                problems.append(f"{path.relative_to(target)} 含 {label}")
    if problems:
        print("\n[自查失败] 包里有不该发出去的东西：")
        for item in problems:
            print("  -", item)
        return False
    print("\n[自查] 未发现真实 wxid / 群 id / 本机用户名 / 疑似密钥")
    return True


def zip_up(target: Path) -> Path:
    archive = DIST / f"{target.name}.zip"
    if archive.exists():
        archive.unlink()               # 覆盖前先删：半截包比没有包更坏
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                zf.write(path, Path(target.name) / path.relative_to(target))
    return archive


def manifest(target: Path, written: list[str]) -> None:
    lines = ["# 文件清单（sha256 用于核对「打包后没被改动」）", ""]
    for rel in written:
        path = target / rel
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest[:16]}  {path.stat().st_size:>7} B  {rel}")
    (target / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="打一个可分发 zip（白名单 + 脱敏 + 自查）")
    ap.add_argument("--version", default="", help="版本号，缺省读 VERSION 文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印清单，不写盘")
    args = ap.parse_args()

    version = args.version.strip() or (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
    items = collect()
    total = sum(path.stat().st_size for path, _ in items)
    print(f"版本 {version}：{len(items)} 个文件，原始 {total / 1024:.0f} KB")
    if args.dry_run:
        for _, rel in items:
            print("  ", rel)
        return 0

    DIST.mkdir(exist_ok=True)
    target, written = stage(items, version)
    manifest(target, written)
    ok = verify(target)
    # 校验会产生 __pycache__ / 自测的 state/：**先清掉再自查再打包**，
    # 否则字节码里的绝对路径会把自查刷屏（扫的是"要发的东西"，不是临时产物）。
    for junk in ("state", "__pycache__"):
        shutil.rmtree(target / junk, ignore_errors=True)
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    ok &= scan(target)
    if not ok:
        print("\n[中止] 校验没通过，不生成 zip。目录留在 dist/ 里可以直接看。")
        return 1

    archive = zip_up(target)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    print(f"\n[完成] {archive}")
    print(f"       {archive.stat().st_size / 1024:.0f} KB  sha256={digest[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
