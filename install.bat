@echo off
chcp 65001 >nul
title autotrade 安裝程式
setlocal
cd /d "%~dp0"
echo ============================================
echo   CryptoLJT 安裝程式
echo ============================================
echo.
echo [1/3] 尋找 Python 3.11 或更新版本 ...
set "PY="
for %%C in ("py -3" "python" "py" "python3") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%~C"
    )
)
if not defined PY goto nopython
echo   使用：%PY%
%PY% --version
echo.
echo [2/3] 建立虛擬環境 .venv ...
if exist ".venv\Scripts\python.exe" goto venvok
%PY% -m venv .venv
if errorlevel 1 goto venvfail
:venvok
echo   完成
echo.
echo [3/3] 安裝相依套件（首次可能需要幾分鐘）...
".venv\Scripts\python.exe" -m pip install -e .
if not errorlevel 1 goto pipok
echo.
echo   First install failed, retrying with a compatible dependency set...
echo   (this handles newer Python where one package has no prebuilt wheel yet)
".venv\Scripts\python.exe" -m pip install -e . "ccxt<4.5.65"
if errorlevel 1 goto pipfail
:pipok
echo   完成
echo.
echo 安裝完成，啟動 CryptoLJT...
".venv\Scripts\python.exe" scripts\run.py
pause
exit /b 0

:nopython
echo.
echo   [錯誤] 找不到 Python 3.11 或更新版本。
echo   請到 https://www.python.org 下載安裝 Python，
echo   安裝時請勾選「Add Python to PATH」。
echo   安裝完成後，請重新開啟這個視窗再執行一次。
echo.
echo   若你已經安裝了，請在一個新的命令提示字元視窗輸入：
echo       python --version
echo       py --version
echo       where python
echo   把輸出結果截圖回報。
pause
exit /b 1

:venvfail
echo.
echo   [錯誤] 無法建立虛擬環境 .venv。
echo   如果 .venv 資料夾已經存在，請先刪除它，再重新執行本檔案。
pause
exit /b 1

:pipfail
echo.
echo   [錯誤] 相依套件安裝失敗，請往上查看錯誤訊息。
echo   常見原因：網路不通、防火牆擋住 pypi.org，
echo   或某些套件尚未支援你安裝的 Python 版本。
echo   若屬後者，通常稍等一段時間等套件更新後再試即可。
pause
exit /b 1
