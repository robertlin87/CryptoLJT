# 實施計劃（備份）

> 本檔案為已批准實施計劃的備份副本（批准日期 2026-10-31）。

# Telegram 信號自動跟單機器人 — 實施計劃

## Context

使用者運營幣圈 Telegram 私密信號群，交易員以統一格式發佈永續合約報單（進場價/現價或掛單/槓桿/保證金%/TP1-4/SL/分析師），單筆風控 ≤ equity 2%。要做成產品：軟件監聽群消息全自動跟單；VIP 用自己的 TG 帳號進群即可用，被踢出群即失效（**群資格即授權**）。倉庫 e:\VibeCoding 為空（greenfield）。

## 可行性結論（已完成 GitHub 調研）

完全可行。參考項目：[amirphl/Telegram-Trading-Bot](https://github.com/amirphl/Telegram-Trading-Bot)（Telethon+CCXT 信號跟單，最接近）、[adiel098/Telegram-AutoTrade-Bot](https://github.com/adiel098/Telegram-AutoTrade-Bot)（TG→Binance 自動交易）、[aradotso/binance-futures-signal-bot](https://www.claudepluginhub.com/skills/aradotso-aradotso-trending-skills-37/binance-futures-signal-bot)、[DePacifier/BitgetLiveTrader](https://github.com/DePacifier/BitgetLiveTrader)、[ngzhenghui94/CryptoTrading](https://raw.githubusercontent.com/ngzhenghui94/CryptoTrading/refs/heads/main/README.md)。授權門檻（getChatMember 校驗成員）是 Telegram 生態標準做法。

**前提：群必須是私密群**——私密群被踢後失去全部消息訪問權（CHANNEL_PRIVATE），授權嚴格成立；公開群被踢者仍可輪詢歷史，授權被繞過。

## 已確認決策

- **交易所四家全做**（Bitget/Bybit/Deepcoin/Bitunix），ccxt 統一引擎，MVP 先 Bybit；Bitunix 實測 ccxt 不支持則手寫 REST 接同一接口
- **品種**：永續合約；**分析師白名單設定，預設全部**（已見 Lazycat、Trader X）
- **授權架構 A 先做**（每個 VIP 自己的 TG 帳號 userbot 監聽，被踢即失效），signal_source 抽象層預留 B（官方 Bot + getChatMember + API 推送）
- **觸發模型（使用者明確）**：
  - **主觸發 = 報單價位**：進場即在交易所掛 SL + TP1-4 reduce-only 限價單，價格碰到自動出場，不依賴後續消息
  - **操作記錄/操作通知 = 安全冗餘備份**：核對"該出場的是否已出場"，沒出場就補出場一次
  - **人為操作 = 指令（兩類）**：① 移動保本/套保 → 跟著 amend SL；② 手工平倉 → 跟著全平剩餘 + 撤該 symbol 掛單。任一通道（新消息或編輯）出現即執行
- **TP 分批均分**：n 檔各 1/n；手數精度不能均分時前 n-1 檔向下取整，末檔平掉全部剩餘；SL 或最終 TP 觸發 → 平剩餘 + 撤銷該 symbol 全部掛單
- **手數公式（使用者指定）**：`張數 = equity × R% ÷ |建倉價 − 止損價| ÷ 合約倍數`。R% = 報單"控倉維持 x% 風控"的 x；**合約倍數 = 各交易所每張合約的幣數**（內部變量，從市場信息取，ccxt `market['contractSize']`，各所不同）；向下取整。保證金額%（報單中）作為上限復算校驗。
- **雙通道監聽**：手工操作時群會**同時**發一條緊湊新消息（如 `📌訂單 188 GIGGLEUSDT 做多 🟥 已平倉`）**並**編輯原報單追加操作記錄行。兩條通道都監聽，互為備份，統一事件層冪等去重。

## 真實報單樣例（已收 7 條 → tests/fixtures/messages/）

報單：訂單216 LITUSDT 做多 / 213 ATOMUSDT 做多 / 196 C98USDT 做空 / 359 DUSKUSDT 做多 / 353 ASTERUSDT 做多 / 188 GIGGLEUSDT 做多 / 187 SYNUSDT 做空。

**報單欄位**：訂單ID、幣種、📈做多/📉做空、進場價+現價/掛單、槓桿、保證金%（`≤x%` 或 `x%（上限）`）、TP1-4、SL、風控行（止損距離% → 控倉%）、分析師。消息隨進展被原帖編輯，追加「操作記錄」行（帶時間戳）。

**操作記錄行類型**（編輯通道）：

| 行 | 類型 | 動作 |
|---|---|---|
| `🎯 TPn 達成 ➜ 價` | 記錄 | 核對該檔 TP 單已成交；未成交 → 市價補平該檔 |
| `✅ 全部止盈` | 記錄 | 倉位尚在 → 全平剩餘 + 撤該 symbol 掛單 |
| `🔒 自動移損 ➜ 價（TPn 後）` | 指令 | amend SL 至該價 |
| `🔒 套保（SL → 進場價 X）` | 指令 | amend SL 至 X（放保本單） |
| `🔒 套保出場 ➜ 價` | 記錄 | 保本單被掃出場；核對倉位，尚在則補平 |
| `🟥 已平倉` / `平倉` 字樣 | 記錄+指令 | 手工平倉：全平剩餘 + 撤該 symbol 掛單 |
| `💬 …`（分析師備註） | 備註 | 忽略，僅落庫 |

**緊湊新消息**（新消息通道，與編輯同時出現）：`📌訂單 (\d+) ([A-Z0-9]+)USDT (做多|做空) 🟥 已平倉`（可帶 `💬` 備註行）。手工平倉/手工保本等操作均以此雙通道出現；套保類緊湊消息變體待採樣，模板可擴展。

**手數公式驗證**（訂單216）：equity=1000 → 1000×2% ÷ |3.042−2.962| = 250 LIT 幣 ≈ 250 張（合約倍數為 1 時）。保證金額復算：2% ÷ (2.63%×10) = 7.6% ✓ 與報單 ≤7.61% 吻合。

## 技術棧

Python 3.11+（本機 3.15 滿足）；Telethon；ccxt；SQLite（aiosqlite）；PyYAML；pyinstaller 打包。**多交易所統一引擎**：下單/改單/平倉邏輯寫一遍，換所換設定（passphrase、IP 白名單、TP/SL 掛法、精度與合約倍數）。

## 實施方案

### 目錄結構

```
e:\VibeCoding\
├─ pyproject.toml
├─ config\config.example.yaml
├─ config\templates\entry.yaml        # 報單模板（繁體原樣）
├─ config\templates\notice.yaml       # 緊湊操作通知模板（新消息）
├─ config\templates\op_lines.yaml     # 操作記錄行模板
├─ src\autotrade\
│  ├─ main.py   config.py
│  ├─ signal_source\base.py               # SignalSource ABC（架構 B 插入點）
│  ├─ signal_source\telegram_userbot.py   # NewMessage + MessageEdited
│  ├─ signal_source\bot_api.py            # 架構 B 佔位
│  ├─ parser\entry_parser.py  notice_parser.py  op_parser.py  llm_fallback.py
│  ├─ risk\engine.py  precision.py
│  ├─ exchange\base.py  bybit.py  bitget.py  deepcoin.py
│  │  bitunix_ccxt.py  bitunix_rest.py  factory.py
│  ├─ execution\order_manager.py  position_tracker.py  reconciler.py
│  ├─ storage\db.py   notify\notifier.py   wizard.py
├─ tests\fixtures\messages\*.txt
└─ scripts\run.py
```

`signal_source/base.py` 接口：`start(handler: Callable[[RawMessage], Awaitable[None]])`；RawMessage 含 chat_id/msg_id/text/is_edit。架構 B 同接口實現，執行引擎零改動。

### 數據流

`Userbot(NewMessage+MessageEdited) → 三類解析器 → 統一事件層(signal_ops 冪等) → TriggerRouter（主=交易所掛單價格觸發 / 備=事件核對補單）→ OrderManager(ExchangeAdapter) → PositionTracker → Notifier`

- 新消息：報單模板 → 新單；緊湊通知模板 → 手工操作事件
- 編輯消息：原報單全文重新解析 → 操作行差量事件
- 兩通道事件統一去重（見 SQLite）

### SQLite（storage/db.py）

- `signals`：去重鍵 `UNIQUE(chat_id, order_id)`，編輯事件按 order_id upsert 更新 raw_text；欄位含 analyst/symbol/side/entry/is_limit/leverage/sl/tps_json/margin_pct/risk_pct/stop_dist_pct/status
- `signal_ops`（統一事件層）：`UNIQUE(chat_id, order_id, line_hash)`，source∈{edit, notice}，action_type∈{tp_hit, all_tp, move_sl, hedge, hedge_exit, close, unknown}，processed 標誌——**編輯行與新消息通知都進此層，僅新增事件觸發動作**
- `orders` / `positions`（tp_hit_count、remaining_qty）/ `events`（審計）

### 解析器（parser/）

- `entry.yaml`：繁體模板 `訂單(\d+)\s*([A-Z0-9]+)USDT\s*(做多📈|做空📉)…進場[:：]([\d.]+)(現價|掛單)` + 可選組（槓桿/保證金/SL/TP1-4/風控/分析師）；symbol_map 映射 ccxt symbol；做空校驗 TP < entry < SL
- `notice.yaml`：`📌訂單 (\d+) ([A-Z0-9]+)USDT (做多|做空) 🟥 已平倉`（+可選 `💬` 行）；套保類緊湊變體待採樣後補模板
- `op_lines.yaml`：上表行模板，容忍行尾時間戳與 `（TP1 後）` 註記；`💬` 行忽略；未匹配行落 unknown 供模板補齊
- LLM 兜底（可選）：模板未命中時 OpenAI JSON 輸出同 Signal/Event 結構

### 風控引擎（risk/engine.py）

- **張數 = floor(equity × R% ÷ |entry − stop| ÷ contractSize)**（使用者指定公式）；minNotional/最小下單量校驗
- 保證金復算：`|margin_pct − R%/(stop_dist_pct×leverage)| ≤ 0.1%`，超差只寫告警、照常下單（不攔單）
- 分析師白名單、同 symbol 反向信號 reject；同時持有的幣種數不設上限、單日虧損上限均已移除

### 執行與狀態機（execution/）

state ∈ {PENDING, FILLED, PARTIAL_TP, BREAKEVEN, CLOSED, CANCELED, UNKNOWN}

- **進場**：set_leverage → 市價/限價開倉 → **掛 SL + n 檔 TP reduce-only 限價單**（檔位：floor 均分，末檔 = 總量 − 前檔合計）→ 主觸發通道即建立，此後不依賴 Telegram
- **主觸發（價格）**：TP 檔成交 → 記錄 tp_hit_count；SL 觸發或末檔 TP 成交 → 撤該 symbol 其餘掛單，state=CLOSED
- **備份觸發（事件核對，每次動作前先查交易所實際狀態再決定）**：
  - `🎯 TPn 達成`：該檔 TP 單未成交/減倉不足 → 市價補平該檔（滑點閾值校驗）
  - `🔒 自動移損 / 套保（SL → X）`：目前 SL ≠ X → amend SL 至 X（可反覆 = 移動止損），state=BREAKEVEN
  - `🟥 已平倉 / 全部止盈 / 套保出場`：倉位尚在 → 市價全平 + 撤該 symbol 全部掛單；已被交易所平掉 → 僅記錄（手工平倉為人為操作指令，雙通道任一出現即執行）
- 冪等：事件層 UNIQUE + 動作前查 position/open_orders 實態；改單失敗重試 3 次 → 降級市價平倉 → 高優先級告警
- **reconciler**：重啟時 DB 非終態持倉與交易所 fetch_positions/open_orders 對賬（DB 有、交易所無 → CLOSED，人工平倉場景）；signal_ops 未處理事件重放

### 交易所適配層（exchange/）

ExchangeAdapter ABC：fetch_equity / fetch_market(contractSize) / set_leverage / create_order(帶 sl/tp) / attach_sl_tp / amend_order / cancel_order / cancel_all_for_symbol / fetch_position / fetch_open_orders + capabilities（supports_attached_sl）。

- **Bybit（MVP）**：統一帳戶、隨單附 TP/SL，先 testnet
- **Bitget**：必填 passphrase；TP/SL 獨立掛載
- **Deepcoin**：實測精度、合約倍數與部分成交後的掛單行為
- **Bitunix**：探測 ccxt.bitunix，無則手寫 REST（HMAC 簽名）實現同一 ABC

### 里程碑

- **M1**：骨架 + userbot（新消息+編輯雙通道）+ 三類解析器 + SQLite 統一事件層，dry_run 只記錄通知。驗收：7 條樣例 + 雙通道重複觸發 fixtures 解析率 ≥95%，同一事件只觸發一次
- **M2**：risk + order_manager + Bybit testnet → 主網最小倉，跑通「TP 檔價格觸發 → 移損 → 全平」主鏈路 + 事件備份補單
- **M3**：套保/移損 amend SL、手工平倉雙通道核對、斷線恢復、events 審計
- **M4**：Bitget → Deepcoin → Bitunix 逐個接入（每家跑同一 paper 腳本）
- **M5**：pyinstaller 打包 + wizard.py（TG 登入、key 輸入、連通自檢）→ VIP 分發

## 驗證方案

1. **單測**：fixtures 真實原文斷言 Signal/Event 全欄位（繁體/📈📉/時間戳/💬 忽略）；雙通道同一操作重複出現只觸發一次；手數公式參數化（7 樣例）
2. **dry-run**：NoopAdapter 全鏈路不碰交易所，回放真實群消息
3. **paper**：Bybit testnet；無 testnet 的所用內存撮合（模擬部分成交/改單失敗/雙通道競態）
4. **實盤**：最小倉限價遠掛驗證「掛單→撤→成交→TP/SL 價格觸發→套保改單→手工平倉」全路徑，再放開 2% 風控實盤

## 風險清單

- **TG 封號**（userbot 違反 ToS）：老號、只監聽不回覆、重連退避；架構 B 為最終形態
- **私密群前提**：啟動時校驗群類型；被踢即失效是授權設計本身
- **雙通道重複觸發**：統一事件層 UNIQUE + 動作前查交易所實態，杜絕雙賣
- **斷線/重啟**：Telethon 自動重連 + 交易所端 TP/SL 主觸發兜底 + reconciler 對賬
- **編輯競態**：UNIQUE 約束 + 事務；**部分成交**：改單按剩餘量；**滑點**：max_slippage 拒絕
- Deepcoin/Bitunix 行為未知（含合約倍數）：paper 全路徑驗證後方可實盤
- 部分地區的手機號無法註冊 TG；交易所 key 只開交易權限 + IP 白名單；信號截圖外洩技術上無解
