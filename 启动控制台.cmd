@echo off
chcp 65001 >nul
title Douyin Sniper Console
cd /d "%~dp0"
set "PY=C:\Program Files\Python313\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -X utf8 -m sniper.app %*
echo.
pause
