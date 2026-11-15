@echo off
chcp 65001 >nul
title autotrade 執行器
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo   尚未安裝，請先執行 install.bat。
    pause
    exit /b 1
)
echo 啟動自動跟單執行器...
echo *** 這將會下「真實」訂單 ***
echo 停止方式：直接關閉此視窗
".venv\Scripts\python.exe" scripts\run.py
pause
