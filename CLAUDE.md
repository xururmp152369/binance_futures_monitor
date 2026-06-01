# CLAUDE.md — Binance 期貨監控機器人

## 專案概覽

自動化監控 Binance 永續合約，偵測三種進場機會，透過 Telegram Bot 發訊號並支援自動下單。監控全 USDT 合約，運行於本機 Docker。

- **Type 1**（多頭）：4h 拉漲後盤整突破
- **Type 2**（空頭）：跌破反彈 123 法則
- **Type 3**（空頭）：死亡叉制空（日線格局 + 1H EMA200 壓制）

---

## 架構與資料流

```
Binance WebSocket (markPrice + kline_15m/1h/4h/1d)
        │
        ├─ markPrice → _handle_mark_price()     → 更新 last_price
        ├─ 15m 收盤  → on_new_15m_candle()      → Type 1（多頭）/ Type 2（空頭）訊號
        ├─ 1h  收盤  → on_new_1h_candle()       → Type 3（死亡叉）進場信號
        ├─ 4h  收盤  → on_new_4h_candle()       → 多頭/空頭狀態機轉換
        └─ 1d  收盤  → on_new_daily_candle()    → 死亡叉 Layer1/2 狀態轉換
                              │
              strategy/state_machine.py（策略協調器）
                 ├─ strategy/long_breakout.py      IDLE → TRACKING → READY
                 ├─ strategy/short_bounce.py       SHORT_WATCHING → SHORT_READY
                 ├─ strategy/death_cross_short.py  IDLE → WATCHING → ALERT
                 └─ strategy/analysis_utils.py    （EMA、ATR、量能、K棒形態共用工具）
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
| `app/setting/models.py` | 全域狀態容器（symbol_state, strategy_state, short_strategy_state, death_cross_state） |
| `app/datacenter/binance_opendata.py` | WebSocket 監聽（15m/1h/4h/1d）、歷史資料載入、自動重連 |
| `app/strategy/state_machine.py` | 策略協調器（對外公開 API，呼叫各策略模組） |
| `app/strategy/long_breakout.py` | 多頭盤整突破策略狀態機（IDLE/TRACKING/READY） |
| `app/strategy/short_bounce.py` | 空頭跌破反彈123法則（SHORT_WATCHING/SHORT_READY） |
| `app/strategy/death_cross_short.py` | 死亡叉制空策略（IDLE/WATCHING/ALERT，日線格局 + 1H 進場） |
| `app/strategy/analysis_utils.py` | 共用分析工具（EMA/ATR 計算、量能基準、K棒形態判斷） |
| `app/strategy/strategy_alerts.py` | Telegram 訊號格式化與多使用者廣播 |
| `app/tgbot/monitor.py` | 週期任務（幣種清單更新、廢棄掃描、session 過期） |
| `app/command/command.py` | Telegram 指令處理（帳號/設定/查詢） |
| `app/user/user_config.py` | 帳號系統（註冊/登入/登出）、Fernet 加密設定、session 管理 |
| `app/trading/order_manager.py` | 自動下單（市價開倉 + SL/TP，支援 -1007 重試） |

---

## 使用者設定欄位

有效策略代號：`"long_breakout"`（多頭盤整突破）、`"short_bounce"`（空頭跌破反彈123）、`"death_cross_short"`（死亡叉制空）

| 欄位 | 說明 |
|------|------|
| `PRD_STRATEGY` | 觸發**正式**自動下單的策略代號陣列（選填；`[]` 停用正式下單） |
| `DEV_STRATEGY` | 觸發**模擬**自動下單的策略代號陣列（選填；`[]` 停用模擬下單） |
| `NOTIFY_STRATEGY` | 接收訊號通知的策略代號陣列（選填；`[]` 靜默；欄位不存在則發提醒） |
| `LONG_ORDER_LIMIT` | 同時持有多單部位數上限（必填正整數） |
| `SHORT_ORDER_LIMIT` | 同時持有空單部位數上限（選填正整數） |

兩個策略欄位均可同時設定，同一訊號可同時觸發正式與模擬兩筆下單。
`PRD_STRATEGY` 非空時需填 `PRD_API_KEY`/`PRD_SECRET_KEY`；`DEV_STRATEGY` 非空時需填 `API_KEY`/`SECRET_KEY`。

訊號通知路由（`strategy_alerts.py`）：Type 1 → `"long_breakout"`；Type 2 → `"short_bounce"`；Type 3 → `"death_cross_short"`。
公頻 `CHAT_ID` 不受 `NOTIFY_STRATEGY` 限制，永遠收到所有訊號。

---

## 多頭策略狀態機規格（long_breakout.py）

### 觸發偵測（單根帶量 K 棒）

**觸發條件**：單根 4h K 棒同時滿足以下兩條件 → IDLE 進入 TRACKING：
1. 陽線且單根漲幅 ≥ `PUMP_THRESHOLD`%：`(close - open) / open × 100 >= PUMP_THRESHOLD`
2. 當根量能 > 前 `TRIGGER_VOLUME_BASELINE_N` 根均量 × `TRIGGER_VOLUME_MULT`

觸發後：
- `consolidation_low` = 觸發 K 棒的 low（廢棄線）
- `consolidation_high` = 觸發 K 棒的 high（突破目標）
- `consolidation_start_ts` = 觸發 K 棒的 open_time（12h 計時起點）
- `pump_candle_open/close/low/high/time` = 觸發 K 棒資訊（用於告警訊息與 Method B 比較）

### 多頭狀態轉換

```
IDLE
 │ 觸發：單根 4h 陽線漲幅 >= PUMP_THRESHOLD% 且量能 > 前 N 根均量 × TRIGGER_VOLUME_MULT
 ▼
TRACKING
 │ 廢棄：4h K 實體低點 min(open,close) < consolidation_low → IDLE（即時掃描亦觸發）
 │       → 同時觸發空頭策略進入 SHORT_WATCHING（僅 4h 收盤廢棄，不含即時掃描）
 │ 延伸：4h K high > consolidation_high → 更新 consolidation_high，重置 12h 計時
 │ 進展：最後一次創新高後盤整 >= CONSOLIDATION_MIN_HOURS
 ▼
READY
 │ 廢棄：同上（創新高退回 TRACKING）
 │ Method B：READY 狀態內出現符合觸發條件的 K，且其漲幅 > pump_candle 漲幅 × (1 + METHOD_B_GAIN_ADVANTAGE/100) → 完整重置觸發 K
 │           （若原 pump_candle 漲幅 > METHOD_B_RELAXED_THRESHOLD，則任何觸發 K 均完整重置）
 └─ 每根 15m 收盤 → Type 1 帶量突破（做多）
```

### Type 1 進場訊號條件（做多）

- 15m close > `consolidation_high × (1 + BREAKOUT_BODY_PCT)`（實體收超頂部 N%）
- 15m 成交量 > 前 192 根平均 × `BREAKOUT_VOLUME_MULT`（排除當根：`kline_15m_ohlc[-193:-1]`）
- 止損 = 往回掃連續放量 K 的最低 low（限當前 4h K 起點後，放量門檻 = 192 根均量 × `LOOKBACK_VOLUME_MULT`）

### 多頭 strategy_state 欄位

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

---

## 空頭策略狀態機規格（short_bounce.py）

### 策略邏輯概述（跌破反彈123法則）

1. **第一步**：多頭策略廢棄（4h K 實體跌破 consolidation_low）→ 自動進入 SHORT_WATCHING
2. **第二步**：等待反彈，觀察反彈是否被均線或靜態壓力位壓制（無量拒絕）
3. **第三步**：反彈確認被壓後，15m 帶量收破廢棄 K 低點 → 空頭進場

### 空頭狀態轉換

```
（由多頭策略廢棄事件觸發，4h 收盤時）
 ▼
SHORT_WATCHING（觀察反彈）
 記錄：
   short_resistance    = 廢棄 K 的 high（靜態壓力位）
   abandonment_low     = 廢棄 K 的 low（空頭進場觸發線）
   short_watch_start_ts = 廢棄 K 的 open_time（觀察計時起點，秒）
 │
 ├─ 超時：已過 SHORT_WATCHING_MAX_CANDLES 根 4h K → IDLE
 │
 ├─ [移除觀察] 反彈 4h K 實體收超 short_resistance 且帶量 → IDLE
 │    max(open,close) > short_resistance 且 volume > 前N根均量 × TRIGGER_VOLUME_MULT
 │
 ├─ [A 靜態壓制] 反彈 4h K 同時滿足：
 │    (1) high ≥ short_resistance（觸到靜態壓力位）
 │    (2) high ≤ short_resistance × (1 + EMA_TOLERANCE_PCT)（未過深，1.5%）
 │    (3) max(open,close) ≤ short_resistance × (1 - BODY_SUPPRESS_PCT)（實體壓制，0.8%）
 │    (4) (high - max(open,close)) > abs(close-open) × WICK_BODY_RATIO（射擊之星，1.5×）
 │    (5) volume < 前N根均量 × SHORT_BOUNCE_VOLUME_MAX（無量，< 1.5×）
 │    → 記錄 short_rejection_high = 此 K 的 high → SHORT_READY
 │
 ├─ [C1 EMA60 壓制] 反彈 4h K 同時滿足：
 │    (1) high ≥ EMA60（觸到均線）
 │    (2) high ≤ EMA60 × (1 + EMA_TOLERANCE_PCT)（未過深）
 │    (3) max(open,close) ≤ EMA60 × (1 - BODY_SUPPRESS_PCT)（實體壓制）
 │    (4) 射擊之星形態（同上）
 │    (5) 無量（同上）
 │    → SHORT_READY
 │
 └─ [C2 EMA15 壓制＋死叉] 反彈 4h K 同時滿足：
      上述 (1)-(5) 條件，但基準改用 EMA15
      + EMA15 < EMA60（死叉格局）
      → SHORT_READY

SHORT_READY（就緒，監控 15m 跌破）
 │ 廢棄：帶量 4h K 實體收超 short_resistance → IDLE
 │ 即時廢棄：markPrice > short_resistance → IDLE（不觸發新一輪 SHORT_WATCHING）
 └─ 每根 15m 收盤 → Type 2 帶量跌破（做空）
```

### Type 2 進場訊號條件（做空）

- 15m close < `abandonment_low × (1 - BREAKOUT_BODY_PCT)`（實體收破底部 0.5%）
- 15m 成交量 > 前 192 根平均 × `SHORT_ENTRY_VOLUME_MIN`（低門檻，1.0×）
- 止損 = `short_rejection_high`（壓制 K 的 high）

### 空頭 short_strategy_state 欄位

| 欄位 | 說明 |
|------|------|
| `phase` | ShortPhase 枚舉（IDLE / WATCHING / READY） |
| `short_resistance` | 廢棄 K 的 high（靜態壓力位） |
| `abandonment_low` | 廢棄 K 的 low（空頭進場觸發線） |
| `short_watch_start_ts` | 廢棄 K 的 open_time（Unix 秒，計時起點） |
| `short_rejection_high` | 壓制 K 的 high（Type 2 止損參考） |

---

## 死亡叉策略狀態機規格（death_cross_short.py）

### 策略邏輯概述（日線死亡叉格局 + 1H EMA200 壓制）

1. **Layer 1**（日線，持續監控）：EMA50(D) < EMA200(D) → 格局確立，進入 WATCHING
2. **Layer 2**（日線，每日確認）：close(D) 跌破 EMA200(D)，時效性（48H）與幅度（10%）檢查通過 → 進入 ALERT 48H 窗口
3. **Layer 3**（1H，即時監控）：ALERT 窗口內，1H 出現信號 A 或 B → 做空進場

### 死亡叉狀態轉換

```
IDLE（EMA50 ≥ EMA200，不監控）
 │ EMA50(D) < EMA200(D)
 ▼
WATCHING（格局確立，等待日線跌破 EMA200）
 │ EMA50 穿回 EMA200 上方 → IDLE
 │ close(D) < EMA200(D)，且：
 │   - 時效：距上次 close > EMA200 ≤ 48H（跌破需新鮮）
 │   記錄 Close_T0 = 當根日線收盤價，alert_time = T0
 ▼
ALERT（48H 監控窗口，等待 1H 進場信號）
 │ EMA50 穿回 EMA200 上方 → IDLE
 │ 超過 48H → WATCHING
 │ 日線收盤 > Close_T0 × 1.10（回漲超 10%）→ WATCHING
 └─ 每根 1H 收盤 → 偵測信號 A 或 B → Type 3 做空
```

### Type 3 進場信號定義

**信號 A：拒絕蠟燭（Rejection Candle）**
```
1. high(1H) > EMA200(1H)                             ← 上影線刺穿 EMA200
2. close(1H) < EMA200(1H)                            ← 收盤壓回下方
3. (EMA200 - close) / EMA200 >= DC_REJECTION_BODY_PCT ← 至少 0.5% 壓制幅度
4. close(1H) < open(1H)                              ← 陰線實體向下
```

**信號 B：吞噬型態（Engulfing Pattern）**
```
1. close(1H) < EMA200(1H)
2. open(1H) > close(1H-1)                                      ← 跳空高開
3. close(1H) < close(1H-1)                                     ← 收盤低於前根
4. |close - open| > |close_prev - open_prev| × DC_ENGULF_BODY_RATIO  ← 實體吞噬 1.5×
5. volume(1H) > volume(1H-1) × DC_ENGULF_VOLUME_RATIO                ← 帶量 1.5×
```

**止損計算：** `EMA200(1H) + ATR(14, 1H)`

**進場限制：**
- 每個 ALERT 窗口最多 2 次（`DC_MAX_ENTRIES_PER_ALERT`）
- 兩次進場間需間隔 `STRATEGY_COOLDOWN`（4H）
- 再進場需等待新的信號 A 或 B 確認

### 死亡叉 death_cross_state 欄位

| 欄位 | 說明 |
|------|------|
| `phase` | DeathCrossPhase 枚舉（IDLE / WATCHING / ALERT） |
| `alert_time` | T0 Unix 秒數（ALERT 窗口起點） |
| `close_t0` | T0 當下日線收盤價（幅度保護基準，10% 上限） |
| `entry_count` | 本 ALERT 窗口已進場次數（上限 2） |
| `last_entry_ts` | 上次進場時間戳（冷卻計算用） |

### 歷史回播

啟動時呼叫 `replay_historical_daily_candles_dc(symbol)`，依序重播所有歷史日線 K 棒恢復狀態。需要 `kline_daily_ohlc` 內至少 200 根（EMA200 最低需求）。

---

## Candle Tuple 格式

所有時框統一格式：`(open_time_ms, open, high, low, close, quote_volume)`

- **1d**：`kline_daily_ohlc`，`deque(maxlen=250)`（EMA200 需 200 根，250 根緩衝）
- **4h**：`kline_4h_ohlc`，`deque(maxlen=200)`（200 根 ≈ 33 天，啟動時載入月歷史供狀態機回播）
- **1h**：`kline_1h_ohlc`，`deque(maxlen=250)`（EMA200 需 200 根，ATR 需 15 根）
- **15m**：`kline_15m_ohlc`，`deque(maxlen=200)`

時間欄位必須用 `k["t"]`（open_time_ms），不得用 `k["T"]`（close_time_ms）

---

## 可設定參數

### 多頭策略

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `PUMP_THRESHOLD` | 3 | 觸發 K 單根漲幅門檻（%）`(close-open)/open×100` |
| `TRIGGER_VOLUME_MULT` | 3 | 觸發 K 量能倍數（相對前 N 根均量） |
| `TRIGGER_VOLUME_BASELINE_N` | 12 | 觸發量能基準根數（前 N 根 4h K 棒） |
| `CONSOLIDATION_MIN_HOURS` | 12 | 最低盤整時數（從最後一次創新高起算） |
| `BREAKOUT_VOLUME_MULT` | 3.5 | Type 1 量能倍數（相對前 192 根 15m 均量） |
| `BREAKOUT_BODY_PCT` | 0.005 | Type 1/2 實體超頂/破底幅度（0.5%） |
| `LOOKBACK_VOLUME_MULT` | 2.5 | Type 1 回掃止損放量門檻倍數 |
| `METHOD_B_GAIN_ADVANTAGE` | 10.0 | Method B：新觸發 K 漲幅需 > pump_candle 漲幅 × (1 + N/100)，即比例優勢 N% |
| `METHOD_B_RELAXED_THRESHOLD` | 10.0 | 原 pump_candle 漲幅超過此值時，Method B 無需比較優勢直接重置 |
| `STRATEGY_COOLDOWN` | 14400 | 告警冷卻秒數（4h，三個策略共用） |

### 空頭策略（short_bounce）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `SHORT_WATCHING_MAX_CANDLES` | 8 | 空頭觀察超時根數（8 根 4h = 32h） |
| `EMA_LONG_PERIOD` | 60 | 主壓制均線週期（EMA60，4h） |
| `EMA_SHORT_PERIOD` | 15 | 死叉確認均線週期（EMA15，4h） |
| `EMA_TOLERANCE_PCT` | 0.015 | EMA/壓力位 wick 超出容忍值（1.5%） |
| `BODY_SUPPRESS_PCT` | 0.008 | 實體壓制幅度（實體需低於壓力位 0.8%） |
| `WICK_BODY_RATIO` | 1.5 | 射擊之星：上影線需 ≥ 實體 × 1.5 |
| `SHORT_BOUNCE_VOLUME_MAX` | 1.5 | 反彈量能上限倍數（超過視為帶量，移除觀察） |
| `SHORT_ENTRY_VOLUME_MIN` | 1.0 | Type 2 進場量能下限倍數 |

### 死亡叉策略（death_cross_short）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `DC_DAILY_EMA_FAST` | 50 | Layer 1 短均線週期（EMA50，日線） |
| `DC_DAILY_EMA_SLOW` | 200 | Layer 1/2 長均線週期（EMA200，日線） |
| `DC_1H_EMA_PERIOD` | 200 | 1H 壓制均線週期（EMA200，1H） |
| `DC_1H_ATR_PERIOD` | 14 | 止損 ATR 週期（1H） |
| `DC_ALERT_WINDOW_HOURS` | 48 | ALERT 監控窗口時數（日線跌破後有效期） |
| `DC_MAX_ROLLBACK_HOURS` | 48 | 時效性：距上次 close > EMA200 不超過此小時數 |
| `DC_PRICE_RECOVERY_PCT` | 1.10 | 幅度保護：日線收盤超過 Close_T0 × 此值則廢棄 |
| `DC_MAX_ENTRIES_PER_ALERT` | 2 | 每 ALERT 窗口最大進場次數 |
| `DC_REJECTION_BODY_PCT` | 0.005 | 信號 A：收盤低於 EMA200(1H) 至少 0.5% |
| `DC_ENGULF_BODY_RATIO` | 1.5 | 信號 B：當根實體需 > 前根實體 × 此值 |
| `DC_ENGULF_VOLUME_RATIO` | 1.5 | 信號 B：量能需 > 前根 × 此值 |

---

## 關鍵注意事項

1. **WebSocket 不得阻塞**：策略函數內所有 I/O（Telegram、下單）必須用 `asyncio.create_task()`
2. **15m 量能 baseline**：用 `kline_15m_ohlc[-193:-1]`（192 根），排除當前未收盤根
3. **多頭歷史回播**：啟動時 `replay_historical_4h_candles()` 恢復多頭盤整狀態；空頭策略由廢棄事件觸發，不需歷史回播
4. **死亡叉歷史回播**：啟動時 `replay_historical_daily_candles_dc()` 依序重播日線 K 棒，恢復 WATCHING/ALERT 狀態
5. **廢棄條件即時掃描**：`scan_strategy()` 每 10 秒比對 markPrice；即時廢棄**不觸發**空頭策略，只有 4h 收盤廢棄才觸發
6. **自動下單模式**：使用者設定 `PRD_STRATEGY` 有值 → `testnet=False`（正式）；`DEV_STRATEGY` 有值 → `testnet=True`（模擬），兩者可同時運行
7. **廢棄條件用實體**：4h K 廢棄判斷以 `min(open, close)` 為準，下影線不觸發廢棄
8. **空頭廢棄事件傳遞**：`on_new_4h_candle_long()` 廢棄時回傳事件 dict，由 `state_machine.py` 協調器轉送給 `short_bounce.enter_short_watching()`
9. **EMA 計算**：`analysis_utils` 提供 `get_4h_ema`、`get_daily_ema`、`get_1h_ema`，各自從對應 ohlc deque 的 close 序列計算；`get_1h_atr` 計算 1H ATR(14)
10. **Method B 僅在 READY**：Method B 重置邏輯只在 `StrategyPhase.READY` 觸發，TRACKING 不處理
11. **歷史資料 API 限流**：`load_historical_data_batch()` 每幣種載入順序為 4h → 15m → 1h → 1d，每次 API 呼叫後 sleep 0.5s，每幣種完成後 sleep 1.0s，每批次間 sleep 3.0s

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

## 回測系統

回測腳本位於 `backtest/`，重用現有策略狀態機對歷史資料進行統計，**完全不影響正式環境**（獨立 Python process，只讀 Binance 公開 REST API，不發 Telegram、不下單）。

### 執行方式

```bash
# 單一策略
python backtest/run.py --strategy long_breakout
python backtest/run.py --strategy short_bounce
python backtest/run.py --strategy death_cross_short

# 三個策略全跑
python backtest/run.py --strategy all

# 指定回測天數（預設 30 天）
python backtest/run.py --strategy long_breakout --days 30

# 強制重新下載資料（不使用快取）
python backtest/run.py --strategy all --no-cache
```

### 輸出

每個策略產出一份 CSV，路徑：`backtest/results/{strategy}_{YYYYMMDD}.csv`

| 欄位 | 說明 |
|------|------|
| `symbol` | 交易對 |
| `signal_time` | 訊號時間（UTC+8） |
| `direction` | LONG / SHORT |
| `entry_price` | 進場價（訊號收盤價） |
| `stop_loss` | 止損位 |
| `risk_1r_pct` | 每 1R 佔進場價的 % |
| `max_r_reached` | 止損前最高到達的 R 數（整數 0~10） |
| `stop_hit` | 是否觸及止損 |

### 注意事項

- 首次執行需下載所有幣種歷史資料，約需 20-30 分鐘；資料快取於 `backtest/cache/`，之後再執行只需幾分鐘
- 歷史資料使用 Binance 公開 REST API，**無需 API Key**
- 建議不要在正式環境高頻交易期間同時執行，以避免 API 頻率衝突

---

## 待辦規劃

- [ ] 有條件使用機制（推薦碼 / 月費訂閱 / 帶單抽成）
- [ ] 導入 AI 模型訓練更合理的止盈止損位置
