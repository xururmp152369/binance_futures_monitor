# CLAUDE.md — Binance 期貨監控機器人

## 專案概覽

自動化監控 Binance 永續合約，偵測「4h 拉漲後盤整突破 / 均線反彈」進場機會，並透過 Telegram Bot 發送訊號通知。

監控幣種範圍為全USDT合約，運行環境為本機上的Docker

---

## 架構與資料流

```
Binance WebSocket (markPrice + kline_15m/1h/4h)
        │
        ▼
binance_opendata.py — handle_price_websocket()
        │
        ├─ 15m 收盤 → on_new_15m_candle() → Type 1 突破訊號
        ├─ 1h  收盤 → on_new_1h_candle()  → Type 2 反彈訊號
        └─ 4h  收盤 → on_new_4h_candle()  → 狀態機轉換
                              │
                              ▼
                  strategy/state_machine.py
                  IDLE → TRACKING → READY
                              │
                              ▼
                  strategy/strategy_alerts.py → Telegram Bot
```

---

## 模組說明

| 路徑 | 職責 |
|------|------|
| `app/main.py` | 進入點，啟動三個 async 任務 + Telegram polling |
| `app/setting/config.py` | 環境變數讀取、策略參數常數 |
| `app/setting/models.py` | 全域狀態容器（symbol_state, strategy_state 等） |
| `app/datacenter/binance_opendata.py` | WebSocket 監聽、歷史資料載入、幣種初始化 |
| `app/strategy/state_machine.py` | 策略狀態機核心邏輯 |
| `app/strategy/strategy_alerts.py` | Telegram 訊號格式化與發送 |
| `app/tgbot/monitor.py` | 週期任務（幣種清單更新、廢棄掃描） |
| `app/command/command.py` | Telegram 指令處理（/s, /strategy, /config） |
| `app/extension/utils.py` | 日誌設定、長訊息切塊工具 |

---

## 策略狀態機規格

### 狀態轉換

```
IDLE
 │ 觸發：4h K 棒 (close-open)/open >= PUMP_THRESHOLD(8%)
 ▼
TRACKING（不限時長，持續追蹤盤整）
 │ 廢棄：current_price < pump_candle_low → 回 IDLE
 │ 進展：已盤整 >= CONSOLIDATION_MIN_HOURS(12h)
 ▼
READY（監控進場訊號）
 │ 廢棄：同上
 ├─ 每根 15m 收盤 → Type 1 帶量突破
 └─ 每根 1h  收盤 → Type 2 均線反彈
```

### 進場訊號條件

**Type 1（帶量突破）**
- 15m 收盤 > 盤整頂部（consolidation_high）
- 15m 成交量 > 前 192 根平均 × BREAKOUT_VOLUME_MULT(3)
- 止損 = 該 15m K 棒最低價

**Type 2（均線反彈）**
- 1h K 最低 ≤ 任一 4h EMA（15/30/45/60） × (1 + EMA_TOUCH_THRESHOLD/100)
- 有效收針：close > low × (1 + WICK_THRESHOLD/100)
- 盈虧比：(consolidation_high - close) / (close - consolidation_low) ≥ STRATEGY_RR_MIN
- 止損 = consolidation_low（= pump_candle_low）

### strategy_state[symbol] 結構

```python
{
    "phase":                  StrategyPhase.IDLE,
    "pump_candle_open":       None,
    "pump_candle_close":      None,
    "pump_candle_low":        None,   # 盤整底部，廢棄門檻，固定不變
    "pump_candle_high":       None,
    "pump_candle_time":       None,   # Unix 秒
    "consolidation_low":      None,   # = pump_candle_low
    "consolidation_high":     None,   # 後續 4h K 最高值（持續更新）
    "consolidation_start_ts": None,
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
| `PUMP_THRESHOLD` | 8 | 4h K 棒漲幅門檻 (%) |
| `CONSOLIDATION_MIN_HOURS` | 12 | 最低盤整時數 |
| `BREAKOUT_VOLUME_MULT` | 3 | Type 1 突破量能倍數 |
| `EMA_TOUCH_THRESHOLD` | 0.5 | Type 2 EMA 觸碰容忍 (%) |
| `WICK_THRESHOLD` | 2 | Type 2 有效收針門檻 (%) |
| `STRATEGY_RR_MIN` | 1.0 | Type 2 最低盈虧比 |
| `STRATEGY_COOLDOWN` | 14400 | 告警冷卻秒數（4h） |

> 執行中可透過 `/config set PARAM VALUE` 動態調整（寫入 runtime_config）。

---

## 常用指令

```bash
# 進入venv模式
專案路徑\venv\Scripts\activate.bat

# 退出venv模式
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

## 環境變數（.env）

```
BOT_TOKEN=<Telegram Bot Token>
CHAT_ID=<Telegram Chat ID>
```

`.env` 不得提交 git。

---

## 測試規範

### 每次修改策略邏輯後，Claude 必須：

1. 在 `tests/` 目錄下新增或更新對應測試（使用 pytest + mock 資料）
2. 執行 `python -m pytest tests/ -v` 並確認全數通過
3. 至少覆蓋以下情境：
   - IDLE → TRACKING 轉換（正常拉漲）
   - TRACKING → READY 轉換（盤整時間達標）
   - Type 1 訊號觸發（量能足夠且突破頂部）
   - Type 2 訊號觸發（EMA 觸碰 + 有效收針 + 盈虧比）
   - 廢棄條件（price < pump_candle_low → 回 IDLE）
   - 邊界值（剛好達標 vs 差一點不達標）
4. 測試完成後，主動提出**可改善的地方**供討論

### Mock 資料規範

- Candle 格式與正式程式一致：4h/1h `(open_time_ms, open, high, low, close)`、15m 多一個 `quote_volume`
- 時間用固定假值（例如 `1700000000000`），不依賴系統時間
- 未來加入回測模組後，測試也應涵蓋模擬帳戶的資金計算邏輯

---

## 關鍵注意事項（修改前必讀）

1. **WebSocket 不得阻塞**：策略函數內所有 I/O（Telegram 發送）必須用 `asyncio.create_task()` 非同步執行，`send_strategy_alert` 必須包 try/except，避免冒泡中斷連線。
2. **15m 量能 baseline**：計算時用 `kline_15m_ohlc[-193:-1]`（共 192 根），排除當前未收盤的這根。
3. **歷史回播**：啟動時 `replay_historical_4h_candles()` 會重播歷史，恢復進行中的盤整狀態，不需等待下一根 4h K。
4. **廢棄條件是即時的**：`scan_strategy()` 每 10 秒被 `periodic_screen()` 呼叫，會即時比對 markPrice vs pump_candle_low。
5. **盤整頂部會更新**：TRACKING 階段每根 4h K 收盤都可能更新 consolidation_high，不是固定值。

---

## 給 Claude 的提示

當你給我任務時，以下資訊越完整越好：

### 修改策略邏輯
- 說明**想改哪個策略條件**（Type 1 / Type 2 / 廢棄條件）
- 提供**新的計算公式或門檻**
- 說明**預期行為**（例：「拉漲後第一根盤整 4h K 不應更新頂部」）

### 新增功能
- 說明**觸發時機**（哪個 K 棒週期、收盤時 or 即時）
- 說明**輸出方式**（Telegram 訊息 / log / 新指令）
- 告訴我是否需要新的 config 參數

### 回報 Bug
- 貼上 **log 片段**（含時間戳）
- 說明**預期 vs 實際行為**
- 如果有特定幣種和時間點，一起提供

### 提問策略邏輯
- 直接問，我會查 state_machine.py 給你答案

---

## 未來規劃

1. 實現幣安合約自動下單
2. 使用者可自行定義盈虧比，可設置多組TP與應TP數量，每次下單保證金與槓桿
3. 可選擇要執行的策略類型，可多選，並各自依據策略設置個別的TP/SL
4. 多帳號設定，每份TG帳號僅能建立一份帳號文件模擬資料庫方式，於其中儲存使用者設定資訊，只會有0或1份
5. 導入AI模型，透過每一次的策略告警，訓練AI更為合理的止盈止損位置，並於告警的資訊中從旁輔助說明
6. 現況TG只能單一個聊天室使用，需要調整成"使用者加入機器人後，利用類似註冊方式(避免TG帳號遺失)"來提供調用
7. 有條件的使用機制(幣安推薦人 or 收費 or 抽取利潤?)

---

## 不應修改的區域

無
