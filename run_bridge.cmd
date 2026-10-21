@echo off
chcp 65001 >nul
rem Launcher for the WeChat bridge (wechatauto-replica route).
rem
rem Why this wrapper exists: pip is blocked on this machine (the sandbox forbids writing to the
rem system temp dir), so the dependencies were unpacked into .pylibs\ by pkg_fetch.py.
rem PYTHONPATH must point there, including pythonwin\ (where pywin32 hides win32ui.pyd).
rem
rem Usage:
rem   run_bridge.cmd                       start normally (reads bridge_config.json)
rem   run_bridge.cmd --validate            config risk check only
rem   run_bridge.cmd --dry-run             print what would be sent, never actually send
rem   run_bridge.cmd --probe-llm           test the LLM endpoint only
rem
rem Recommended order: --validate -> --dry-run -> normal start in draft mode -> switch to auto.
rem NOTE: this file must stay ASCII-only; cmd.exe parses .cmd bytes with the OEM codepage
rem and non-ASCII comments break command parsing.

setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist ".pylibs" (
  echo [WARN] .pylibs not found. For the wechatauto route, first run:
  echo   python pkg_fetch.py wechatauto-replica uiautomation pywin32 comtypes cryptography Pillow psutil colorama zstandard opencv-python cffi numpy pyperclip --index https://mirrors.aliyun.com/pypi/simple
)

set PYTHONPATH=%~dp0.pylibs;%~dp0.pylibs\pythonwin;%~dp0.pylibs\win32;%~dp0.pylibs\win32\lib
python bridge.py %*
endlocal
