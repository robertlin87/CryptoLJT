#!/usr/bin/env bash
# Linux 桌面版安裝程式（等價 Windows install.bat）
cd "$(dirname "$0")"

echo "============================================"
echo "  CryptoLJT 安裝程式"
echo "============================================"
echo
echo "[1/3] 檢查 Python 3.11+ ..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "  錯誤：找不到 Python3。"
    echo "  請先安裝 Python 3.11 或更新版本：https://www.python.org"
    echo
    read -rp "  按 Enter 結束..." _
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; then
    echo "  錯誤：Python 版本過舊，需要 3.11 或更新版本。"
    echo "  目前版本："
    python3 --version
    echo
    read -rp "  按 Enter 結束..." _
    exit 1
fi
echo "  完成"
echo
echo "[2/3] 建立虛擬環境（.venv）..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
echo "  完成"
echo
echo "[3/3] 安裝相依套件（首次可能需要幾分鐘）..."
if ! ".venv/bin/python" -m pip install -e .; then
    echo "  第一次安裝失敗，改用相容的相依套件版本重試..."
    echo "  （這是為了解決較新 Python 上某些套件尚未提供預編譯版本的問題）"
    if ! ".venv/bin/python" -m pip install -e . "ccxt<4.5.65"; then
        echo "  [錯誤] 相依套件安裝失敗，請往上查看錯誤訊息。"
        echo "  常見原因：網路不通、防火牆擋住 pypi.org，"
        echo "  或某些套件尚未支援你安裝的 Python 版本。"
        read -rp "  按 Enter 結束..." _
        exit 1
    fi
fi
echo "  完成"
echo
echo "完成。啟動 CryptoLJT..."
".venv/bin/python" scripts/run.py
read -rp "  按 Enter 結束..." _
