# CLAUDE.md — Binance 期貨監控機器人

## 專案概覽

自動化監控 Binance 永續合約，偵測「4h 拉漲後盤整突破 / 均線反彈」進場機會，並透過 Telegram Bot 發送訊號通知。支援多使用者帳號系統與自動下單。

監控幣種範圍為全 USDT 合約，運行環境為本機上的 Docker。

---

## 架構與資料流

```
Binance WebSocket (markPrice + kline_15m/1h/4h)
        │
        ▼
binance_opendata.py — handle_price_websocket()
        │
        ├─ markPrice → _handle_mark_price() → 更新 last_price / price_history
        ├─ 15m 收盤  → _handle_kline_15m()  → on_new_15m_candle() → Type 1 突破訊號
        ├─ 1h  收盤  → _handle_kline_1h()   → on_new_1h_candle()  → Type 2 反彈訊號
        └─ 4h  收盤  → _handle_kline_4h()   → on_new_4h_candle()  → 狀態機轉換
                              │
                              ▼
                  strategy/state_machine.py
                  IDLE → TRACKING → READY
                              │
                              ▼
                  strategy/strategy_alerts.py → Telegram Bot
                              │
                              ▼
                  trading/order_manager.py → Binance API 自動下單
```

---

## 模組說明

| 路徑 | 職責 |
|------|------|
| `app/main.py` | 進入點，啟動背景任務 + Telegram polling，管理 session refresh handler |
| `app/setting/config.py` | 環境變數讀取、策略參數常數 |
| `app/setting/models.py` | 全域狀態容器（symbol_state, strategy_state, runtime_config 等） |
| `app/datacenter/binance_opendata.py` | WebSocket 監聽、歷史資料載入、幣種初始化、自動重連 |
| `app/strategy/state_machine.py` | 策略狀態機核心邏輯（IDLE/TRACKING/READY） |
| `app/strategy/strategy_alerts.py` | Telegram 訊號格式化與多使用者廣播 |
| `app/tgbot/monitor.py` | 週期任務（幣種清單更新、廢棄掃描、session 過期檢查） |
| `app/command/command.py` | Telegram 指令處理（帳號/設定/查詢） |
| `app/command/bot_enum.py` | TGBotCommand StrEnum，集中管理指令字串 |
| `app/user/user_config.py` | 帳號系統（註冊/登入/登出）、Fernet 加密設定檔讀寫、session 管理 |
| `app/trading/order_manager.py` | 自動下單邏輯（市價開倉 + SL/TP，支援 -1007 重試） |
| `app/extension/utils.py` | 日誌設定、長訊息切塊工具 |

---

## 帳號與 Session 系統

### 資料目錄

```
data/
├── accounts/{account_name}.json   # 明文 JSON：password_hash / tg_chat_id / session_expires_at
└── configs/{account_name}.json    # Fernet 加密：交易設定（API Key 等敏感資訊）
```

### Session 機制

- 有效期：30 天（`SESSION_DURATION = 30 * 86400`）
- **任何與 Bot 的互動**都會延長 session（`TypeHandler` in group=-1，先於所有指令觸發）
- 超過 30 天無互動 → `check_expired_sessions()` 停用 `ENABLED`，Bot 通知使用者
- 登入時以 `tg_chat_id` 覆蓋舊綁定，舊裝置立即失效

### 加密設計

- 設定檔以伺服器端 `ENCRYPTION_KEY`（Fernet）加密，存放在 `data/configs/`
- 帳號檔（`data/accounts/`）明文儲存，僅含密碼雜湊（SHA-256 + random salt）
- `ENCRYPTION_KEY` 遺失 = 所有設定無法解密，需提醒使用者備份 `.env`

---

## 策略狀態機規格

### 狀態轉換

```
IDLE
 │ 觸發：4h K 棒為陽線，且 (high-low)/low >= PUMP_THRESHOLD%
 ▼
TRACKING（不限時長，持續追蹤盤整）
 │ 廢棄：4h K 棒 low < pump_candle_low → 回 IDLE（即時掃描亦會觸發）
 │ 延伸：後續 4h K 棒創新高 → 更新 consolidation_high，重置盤整計時
 │ 進展：停止創新高後，已盤整 >= CONSOLIDATION_MIN_HOURS(16h)
 ▼
READY（監控進場訊號）
 │ 廢棄：同上（含創新高時退回 TRACKING）
 ├─ 每根 15m 收盤 → Type 1 帶量突破
 └─ 每根 1h  收盤 → Type 2 均線反彈
```

### 進場訊號條件

**Type 1（帶量突破）**
- 15m 收盤 > `consolidation_high`
- 15m 成交量 > 前 192 根平均 × `BREAKOUT_VOLUME_MULT`(3)
- 止損 = 該 15m K 棒最低價

**Type 2（均線反彈）**
- 1h K 最低 ≤ 任一 4h EMA（15/30/45/60） × (1 + `EMA_TOUCH_THRESHOLD`/100)
- 多頭確認：1h K 開盤 > 觸碰的 EMA 值
- 有效收針：close > low × (1 + `WICK_THRESHOLD`/100)
- 盈虧比：(consolidation_high - close) / (close - consolidation_low) ≥ `STRATEGY_RR_MIN`
- 止損 = `consolidation_low`（= pump_candle_low）

### strategy_state[symbol] 結構

```python
{
    "phase":                  StrategyPhase.IDLE,
    "pump_candle_open":       None,
    "pump_candle_close":      None,
    "pump_candle_low":        None,   # 盤整底部 / 廢棄門檻（固定不變）
    "pump_candle_high":       None,
    "pump_candle_time":       None,   # Unix 秒（open_time）
    "consolidation_low":      None,   # = pump_candle_low
    "consolidation_high":     None,   # 後續 4h K 最高值（延伸時持續更新）
    "consolidation_start_ts": None,   # 最後一次創新高的 open_time（秒）
    "last_alert_ts":          0.0,
    "last_signal_type":       None,   # "type1" / "type2"
}
```

### Candle Tuple 格式（統一規範）

- 4h / 1h：`(open_time_ms, open, high, low, close)`
- 15m：`(open_time_ms, open, high, low, close, quote_volume)`
- 時間欄位**必須用 `k["t"]`（open_time_ms）**，不得用 `k["T"]`（close_time_ms）

---

## 可設定參數（config.py）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `PUMP_THRESHOLD` | 8 | 4h K 棒拉漲門檻：`(high-low)/low >= N%` |
| `CONSOLIDATION_MIN_HOURS` | 16 | 最低盤整時數（從最後一次創新高起算） |
| `BREAKOUT_VOLUME_MULT` | 3 | Type 1 突破量能倍數（相對前 192 根平均） |
| `EMA_TOUCH_THRESHOLD` | 0.5 | Type 2 EMA 觸碰容忍距離（%） |
| `WICK_THRESHOLD` | 3 | Type 2 有效收針：`close > low × (1 + N%)` |
| `STRATEGY_RR_MIN` | 1.0 | Type 2 最低盈虧比 |
| `STRATEGY_COOLDOWN` | 14400 | 告警冷卻秒數（4h） |
| `BATCH_SIZE` | 20 | WebSocket 每批幣種數量 |
| `QUOTE_VOLUME` | 0 | 24h 最低成交量篩選（0 = 不篩選） |

> 執行中可透過 `/config set PARAM VALUE` 動態調整（寫入 `runtime_config`，重啟後還原）。

---

## 環境變數（.env）

```
BOT_TOKEN=<Telegram Bot Token>
CHAT_ID=<可選，公開播報頻道 Chat ID>
ENCRYPTION_KEY=<必填，Fernet 加密金鑰>
LOG_TO_FILE=<可選，1 = 同時寫入 _codeExecution.log>
```

`.env` 不得提交 git。`ENCRYPTION_KEY` 必須備份，遺失後無法解密使用者設定。

---

## 常用指令

```bash
# 進入 venv
venv\Scripts\activate.bat

# 退出 venv
deactivate

# 本機開發啟動
python -m app.main

# Docker 啟動
docker-compose up -d

# 查看 log
docker-compose logs -f

# 停止
docker-compose down

# Docker 重新編譯啟動
docker-compose up -d --build --force-recreate
```

---

## 測試

### 測試套件

```bash
python -m pytest tests/ -v
```

### 測試檔案說明

#### `tests/conftest.py`
共用 fixtures 與 helper 工廠函數。

- `reset_global_state`（autouse fixture）：每個測試前後清空 `strategy_state` / `symbol_state`，還原 `runtime_config` 為固定預設值（避免測試間污染）
- `make_pump_candle(ts, ...)` / `make_flat_candle(ts, ...)` / `make_15m_candle(...)` / `make_1h_candle(...)`：建立符合 candle tuple 格式的假資料
- `make_15m_ohlc_deque(count, base_volume)`：產生固定量能的 15m K 棒序列（用於量能計算測試）
- `setup_symbol_state(symbol, ...)`：建立 `symbol_state[symbol]`，填入最小必要欄位

**注意**：`conftest.py` 的 `_DEFAULT_RUNTIME_CONFIG` 使用較保守的值（`CONSOLIDATION_MIN_HOURS=12`、`WICK_THRESHOLD=2`），與正式 `config.py` 預設值不同，目的是讓測試邊界更容易控制。

#### `tests/test_state_machine.py`
策略狀態機完整測試，涵蓋：

- `TestPumpDetection`：IDLE → TRACKING 觸發條件、各邊界值
- `TestConsolidation`：TRACKING → READY 時序、延伸創新高重置計時、回退邏輯
- `TestInvalidation`：廢棄條件（4h K low 跌破底部）
- `TestType1Signal`：帶量突破觸發、量能不足不觸發、冷卻時間
- `TestType2Signal`：EMA 觸碰 + 收針 + 盈虧比組合條件

#### `tests/test_order_manager.py`
自動下單邏輯測試（全部 mock，不打真實 API）：

- `test_normal_success`：正常開倉，驗證 SL 用 `closePosition=True`、單筆 TP 用 `closePosition=True`
- `test_multi_tp_last_is_close_position`：多筆 TP，驗證非最後一筆用 `quantity+reduceOnly`、最後一筆用 `closePosition=True`
- `test_1007_query_shows_filled`：開倉逾時(-1007) → 查詢已成立 → 繼續設 SL/TP
- `test_1007_retry_succeeds_on_second_attempt`：逾時 → 查詢未成立 → 第 2 次重試成功
- `test_1007_all_5_retries_exhausted`：5 次重試全部逾時 → 放棄，不設 SL/TP
- `test_other_error_aborts_immediately`：非 -1007 錯誤 → 立即放棄

#### `tests/test_ws_diag.py`
WebSocket 連線診斷工具（**不是自動化測試**，需手動執行，且需要實際網路連線）：

```bash
python tests/test_ws_diag.py
```

比較 raw websockets 與 BinanceSocketManager 兩種連線方式，用於排查 WebSocket 斷線問題。

---

## 關鍵注意事項（修改前必讀）

1. **WebSocket 不得阻塞**：策略函數內所有 I/O（Telegram 發送、下單）必須用 `asyncio.create_task()` 非同步執行，避免冒泡中斷連線。
2. **15m 量能 baseline**：計算時用 `kline_15m_ohlc[-193:-1]`（共 192 根），排除當前未收盤的這根。
3. **歷史回播**：啟動時 `replay_historical_4h_candles()` 會重播歷史，恢復進行中的盤整狀態，不需等待下一根 4h K。
4. **廢棄條件是即時的**：`scan_strategy()` 每 10 秒被 `periodic_screen()` 呼叫，即時比對 markPrice vs pump_candle_low。
5. **盤整頂部會更新**：TRACKING 階段每根 4h K 收盤創新高都會更新 `consolidation_high` 並重置計時，不是固定值。
6. **WebSocket 自動重連**：`handle_price_websocket()` 有外層 while 迴圈，斷線後指數退避（5→10→20→40→60 秒）自動重連。
7. **自動下單測試模式**：`order_manager.py` 頂部有 `USE_TESTNET = True`，測試完畢後需改為 `False` 才會打正式環境。

---

## 給 Claude 的提示

### 修改策略邏輯
- 說明**想改哪個策略條件**（Type 1 / Type 2 / 廢棄條件）
- 提供**新的計算公式或門檻**
- 說明**預期行為**
- 修改後執行 `python -m pytest tests/ -v` 確認全數通過

### 新增功能
- 說明**觸發時機**（哪個 K 棒週期、收盤時 or 即時）
- 說明**輸出方式**（Telegram 訊息 / log / 新指令）
- 告訴我是否需要新的 config 參數

### 回報 Bug
- 貼上 **log 片段**（含時間戳）
- 說明**預期 vs 實際行為**
- 如果有特定幣種和時間點，一起提供

### 提問策略邏輯
- 直接問，我會查 `state_machine.py` 給你答案

---

## 未來規劃

1. ~~實現幣安合約自動下單~~ ✅
2. ~~使用者可自行定義盈虧比、多組 TP、下單保證金與槓桿~~ ✅
3. ~~可選擇要執行的策略類型（TYPE1 / TYPE2）~~ ✅
4. ~~多帳號系統，每份 TG 帳號對應一份設定~~ ✅
5. 導入 AI 模型，透過每次策略告警訓練更合理的止盈止損位置
6. ~~多使用者機制（帳號密碼登入，防 TG 帳號遺失）~~ ✅
7. 有條件的使用機制（推薦碼 / 月費訂閱 / 帶單抽成）

---

## 不應修改的區域

無

