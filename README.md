# 加密林教頭VIP跟單助手（autotrade）— Telegram 信號自動跟單機器人

幣圈 Telegram 私密信號群 → 自動解析報單 → 交易所永續合約全自動跟單。

- **授權模型**：VIP 用自己的 TG 帳號登入軟件監聽群，被踢出群即失效（群資格即授權）
- **主觸發**：進場即在交易所掛 SL + TP1-4 限價單（價格驅動）；群消息（新消息 + 原帖編輯）作為備份核對與人為操作指令通道
- **交易所**：Bybit / Binance / Bitget / Gate / Deepcoin / Bitunix（六家，ccxt 統一引擎 + Bitunix 手寫 REST）

## 使用者使用指南（面向不懂技術的使用者）

**原始碼分發（主要方式，無需打包 exe）**

1. 拿到原始碼資料夾後，先確認本機已裝 **Python 3.11+**（安裝時勾選「Add Python to PATH」）
2. 雙擊 `install.bat`：自動建虛擬環境、裝依賴，隨後進入**首次設定向導**
3. 首次設定向導（在同一個黑色視窗裡逐項引導，全程不彈窗）：
   - 輸入 TG 的 `api_id` / `api_hash`（到 <https://my.telegram.org> 申請）
   - 按提示輸入手機號、收到的驗證碼（有 2FA 再輸密碼），自動驗證信號群資格
   - **選擇你的交易所**（輸入數字：`1` Bybit / `2` Binance / `3` Bitget / `4` Gate / `5` Deepcoin / `6` Bitunix），按頁面提示填寫 API 欄位，頁面會顯示該所的權限注意事項
   - 填寫**風險乘數 R**（預設 1，直接回車即可）
   - 填寫**止盈部位比例**（預設 25% / 25% / 25% / 25%，直接回車沿用；只填數字、不用填 %，四檔加總必須等於 100）
   - 全部通過驗證後自動開始監聽
4. 以後每次想跟單，雙擊 `start_run.bat`：
   - 彈出設定選單：`1` 改 Telegram / `2` 改交易所 / `3` 改風險乘數（顯示目前值）/ `4` 改止盈部位比例（顯示目前值）/ **直接回車**開始監聽
   - 關掉視窗即停止

> 網絡要求：需能正常連線 Telegram 與交易所；軟件會自動適配本機網絡設定。

**打包 exe（備選，仍可用）**

見 `docs/HANDOFF.md` 的打包說明；產物在 `dist/autotrade/`，需連資料夾一起發。

### 原始碼分發清單（發出前檢查）

運行 `scripts/check_package.py` 自動核對。發出的原始碼包**必須包含**：`src/`、`scripts/`、`config/config.example.yaml`、`install.bat`、`start_run.bat`、`run_app.py`、`pyproject.toml`、`README.md`、說明檔案與測試。

**絕不能**隨包發出（含你自己的密鑰 / 運行數據）：`config/config.yaml`、`autotrade_session.session*`、`data/`、`temp/`、`logs/`、`.venv/`、`dist/`、`build/`、`.git/`、`.codebuddy/`。

## 開發者快速開始

```bash
python -m venv .venv                               # 獨立環境（本機有 3.12/3.15 兩個 Python，venv 固定用 3.12）
.venv\Scripts\python -m pip install -e .           # Windows；Linux/macOS 用 .venv/bin/python
.venv\Scripts\python -m unittest discover -s tests -v   # 單測
.venv\Scripts\python run_app.py                    # 啟動應用（嚮導/監聽）
.venv\Scripts\python scripts\run.py --config config\config.yaml   # 命令行啟動
```

### 打包 exe

```bash
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python -m PyInstaller autotrade.spec
# 產物在 dist/autotrade/ 目錄
```

## 說明檔案

- 完整實施計劃：[docs/PLAN.md](docs/PLAN.md)
- 設定模板：[config/config.example.yaml](config/config.example.yaml)

## 目錄速覽

```
src/autotrade/
├── app.py            # 應用入口（嚮導/監聽）
├── gui/              # GUI 嚮導、登入彈窗、運行視窗
├── paths.py          # 外部目錄管理（打包後寫 %APPDATA%/autotrade）
├── proxy.py          # 網絡連線適配
├── parser/           # 報單/通知/操作行解析器
├── risk/             # 風控、手數公式、精度
├── exchange/         # 六家交易所適配 + schemas.py（嚮導動態表單）
├── execution/        # OrderManager、TriggerRouter
├── storage/          # SQLite（信號/事件冪等層）
└── signal_source/    # Telethon userbot
```
