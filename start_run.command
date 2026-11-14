#!/usr/bin/env bash
# macOS 執行器（等價 Windows start_run.bat，雙擊即可執行）
cd "$(dirname "$0")"
echo "啟動自動跟單執行器..."
echo "*** 這將會下「真實」訂單 ***"
echo "停止方式：直接關閉此視窗"
".venv/bin/python" scripts/run.py
read -rp "  按 Enter 結束..." _
