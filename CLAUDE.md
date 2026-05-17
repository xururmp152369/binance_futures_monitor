# CLAUDE.md — Binance 期貨監控機器人

## 專案概覽

自動化監控 Binance 永續合約，偵測「4h 拉漲後盤整突破」進場機會，透過 Telegram Bot 發訊號並支援自動下單。監控全 USDT 合約，運行於本機 Docker。

---

## 架構與資料流

```
Binance WebSocket (markPrice + kline_15m/4h)
        │
        ├─ markPrice → _handle_mark_price() → 更新 last_price
        ├─ 15m 收盤  → on_new_15m_candle()  → Type 1 突破訊號
        └─ 4h  收盤  → on_new_4h_candle()   → 狀態機轉換
                              │
                  strategy/state_machine.py  IDLE → TRACKING → READY
                              │
                  strategy/strategy_alerts.py → Telegram Bot
                              │
                  trading/order_manager.py → Binance API 自動下單
```

---

## 模組說明

| 路徑 | 職責 |
|------|------|
| `app/main.py` | 進入點，啟動背景任務 + Telegram polling |
| `app/setting/config.py` | 環境變數讀取、策略參數常數 |
| `app/setting/models.py` | 全域狀態容器（symbol_state, strategy_state） |
| `app/datacenter/binance_opendata.py` | WebSocket 監聽、歷史資料載入、自動重連 |
| `app/strategy/state_machine.py` | 策略狀態機核心（IDLE/TRACKING/READY） |
| `app/strategy/strategy_alerts.py` | Telegram 訊號格式化與多使用者廣播 |
| `app/tgbot/monitor.py` | 週期任務（幣種清單更新、廢棄掃描、session 過期） |
| `app/command/command.py` | Telegram 指令處理（帳號/設定/查詢） |
| `app/user/user_config.py` | 帳號系統（註冊/登入/登出）、Fernet 加密設定、session 管理 |
| `app/trading/order_manager.py` | 自動下單（市價開倉 + SL/TP，支援 -1007 重試） |

---

## 策略狀態機規格

### 觸發偵測（單根帶量 K 棒）

**觸發條件**：單根 4h K 棒同時滿足以下兩條件 → IDLE 進入 TRACKING：
1. 陽線且單根漲幅 ≥ `PUMP_THRESHOLD`%：`(close - open) / open × 100 >= PUMP_THRESHOLD`
2. 當根量能 > 前 `TRIGGER_VOLUME_BASELINE_N` 根均量 × `TRIGGER_VOLUME_MULT`

觸發後：
- `consolidation_low` = 觸發 K 棒的 low（廢棄線）
- `consolidation_high` = 觸發 K 棒的 high（突破目標）
- `consolidation_start_ts` = 觸發 K 棒的 open_time（12h 計時起點）
- `pump_candle_open/close/low/high/time` = 觸發 K 棒資訊（用於告警訊息與 Method B 比較）

### 狀態轉換

```
IDLE
 │ 觸發：單根 4h 陽線漲幅 >= PUMP_THRESHOLD% 且量能 > 前 N 根均量 × TRIGGER_VOLUME_MULT
 ▼
TRACKING
 │ 廢棄：4h K 實體低點 min(open,close) < consolidation_low → IDLE（即時掃描亦觸發）
 │ 延伸：4h K high > consolidation_high → 更新 consolidation_high，重置 12h 計時
 │ Method B：盤整內出現符合觸發條件的 K，且其漲幅 > pump_candle 漲幅 + 1% → 完整重置觸發 K
 │ 進展：最後一次創新高後盤整 >= CONSOLIDATION_MIN_HOURS
 ▼
READY
 │ 廢棄：同上（創新高退回 TRACKING；Method B 同樣適用）
 └─ 每根 15m 收盤 → Type 1 帶量突破（做多）
```

### 進場訊號條件

**Type 1（帶量突破，做多）**
- 15m close > `consolidation_high × (1 + BREAKOUT_BODY_PCT)`（實體收超頂部 N%）
- 15m 成交量 > 前 192 根平均 × `BREAKOUT_VOLUME_MULT`（排除當根：`kline_15m_ohlc[-193:-1]`）
- 止損 = 往回掃連續放量 K 的最低 low（限當前 4h K 起點後，放量門檻 = 192 根均量 × `LOOKBACK_VOLUME_MULT`）

### strategy_state 欄位

| 欄位 | 說明 |
|------|------|
| `consolidation_low` | 觸發 K 棒的 low（廢棄線）；Method B 時更新 |
| `consolidation_high` | 觸發 K 棒的 high；創新高時更新 |
| `consolidation_start_ts` | 最後一次創新高的時間（12h 計時起點） |
| `pump_candle_open` | 觸發 K 棒的 open（Method B 比較漲幅用） |
| `pump_candle_close` | 觸發 K 棒的 close（Method B 比較漲幅用） |
| `pump_candle_low` | 觸發 K 棒的 low（廢棄線，同 consolidation_low） |
| `pump_candle_high` | 觸發 K 棒的 high（同 consolidation_high 初始值） |
| `pump_candle_time` | 觸發 K 棒的 open_time（Unix 秒） |

### Candle Tuple 格式

- 4h：`(open_time_ms, open, high, low, close, quote_volume)`
- 15m：`(open_time_ms, open, high, low, close, quote_volume)`
- 時間欄位必須用 `k["t"]`（open_time_ms），不得用 `k["T"]`（close_time_ms）

---

## 可設定參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `PUMP_THRESHOLD` | 3 | 觸發 K 單根漲幅門檻（%）`(close-open)/open×100` |
| `TRIGGER_VOLUME_MULT` | 3 | 觸發 K 量能倍數（相對前 N 根均量） |
| `TRIGGER_VOLUME_BASELINE_N` | 12 | 觸發量能基準根數（前 N 根 4h K 棒） |
| `CONSOLIDATION_MIN_HOURS` | 12 | 最低盤整時數（從最後一次創新高起算） |
| `BREAKOUT_VOLUME_MULT` | 4.5 | Type 1 量能倍數（相對前 192 根 15m 均量） |
| `BREAKOUT_BODY_PCT` | 0.005 | Type 1 實體超頂幅度（0.5%） |
| `LOOKBACK_VOLUME_MULT` | 3 | Type 1 回掃止損放量門檻倍數 |
| `STRATEGY_COOLDOWN` | 14400 | 告警冷卻秒數（4h） |

---

## 關鍵注意事項

1. **WebSocket 不得阻塞**：策略函數內所有 I/O（Telegram、下單）必須用 `asyncio.create_task()`
2. **15m 量能 baseline**：用 `kline_15m_ohlc[-193:-1]`（192 根），排除當前未收盤根
3. **歷史回播**：啟動時 `replay_historical_4h_candles()` 恢復進行中的盤整，不需等下一根 4h K
4. **廢棄條件即時掃描**：`scan_strategy()` 每 10 秒比對 markPrice vs consolidation_low
5. **自動下單模式**：`order_manager.py` 頂部 `USE_TESTNET`，正式上線前須改 `False`
6. **廢棄條件用實體**：4h K 廢棄判斷以 `min(open, close)` 為準，下影線不觸發廢棄

---

## 測試工作流程

```bash
python -m pytest tests/ -v --ignore=tests/test_ws_diag.py
```

**測試分工原則（新功能開發時）：**
- 開發 Agent：實作功能
- 測試 Agent（worktree 隔離）：獨立閱讀規格 + 最終程式碼，撰寫測試後執行

既有功能的回歸測試直接在主流程跑 pytest，不需獨立 Agent。

`tests/test_ws_diag.py` 是手動診斷工具（需真實網路），不納入自動測試。

---

## 待辦規劃

- [ ] 有條件使用機制（推薦碼 / 月費訂閱 / 帶單抽成）
- [ ] 導入 AI 模型訓練更合理的止盈止損位置
