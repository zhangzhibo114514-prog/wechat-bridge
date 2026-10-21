@echo off
chcp 65001 >nul
rem Connectivity probe for the WeChat 4.x bridge.
rem
rem Why a separate launcher exists: reading the WeChat database key requires reading the
rem Weixin.exe process memory, which the DSH sandbox blocks (tasklist / CIM / psutil are all
rem denied there). So this must be started from YOUR OWN terminal, not from inside DSH.
rem
rem Usage:
rem   bridge_probe_wechat.cmd                 run the 4-step probe (read-only up to step 3)
rem   bridge_probe_wechat.cmd --send "test"   also send one message to File Transfer
rem
rem NOTE: this file must stay ASCII-only (cmd.exe parses .cmd bytes with the OEM codepage).

setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%~dp0.pylibs;%~dp0.pylibs\pythonwin;%~dp0.pylibs\win32;%~dp0.pylibs\win32\lib;%~dp0.pylibs\win32comext
python probe_wechat.py %*
endlocal
