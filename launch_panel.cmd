@echo off
chcp 65001 >nul
cd /d "%~dp0"
title wechat-bridge panel
python panel\panel_server.py --open
echo.
echo Panel stopped. You can close this window.
pause
