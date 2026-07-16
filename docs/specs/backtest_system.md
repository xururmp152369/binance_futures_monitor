# 回測系統規格書

## 1. 概述

回測系統重播歷史 K 棒資料，將其餵入**與正式系統完全相同的策略狀態機**，收集歷史進場訊號後計算績效指標。

**重要前提**：回測不是獨立實作的策略邏輯，而是直接呼叫 `app/strategy/long_breakout.py`、`app/strategy/death_cross_short.py` 和 `app/strategy/long_short_fibonacci.py`，並使用 `app/setting/config.py` 的參數。因此，**回測結果完全反映目前的策略設定**，修改 config.py 的任何參數都會同時影響正式系統與回測結果。

---

## 2. 模組架構

```
backtest/
├── run.py          — CLI 入口點，串接所有模組
├── engine.py       — 核心引擎，驅動狀態機逐根處理 K 棒
├── data_fetcher.py — 從 Binance API 下載歷史 K 棒，支援快取
├── evaluator.py    — 計算每筆訊號的 R 倍數與 P&L
└── reporter.py     — 輸出 CSV 報告與終端摘要
```

**呼叫鏈與參數來源**：

```
run.py
  └→ engine.py
       └→ app/strategy/state_machine.py
            ├→ app/strategy/long_breakout.py       ──→ app/setting/config.py（共用參數）
            ├→ app/strategy/death_cross_short.py   ──→ app/setting/config.py（共用參數）
            └→ app/strategy/long_short_fibonacci.py ──→ app/setting/config.py（共用參數）
```

---

## 3. 執行方式

```bash
# 基礎回測（只看 R 倍數，不計算 USDT 損益）
make backtest STRATEGY=long_breakout
make backtest STRATEGY=death_cross_short
make backtest STRATEGY=fibonacci_long
make backtest STRATEGY=fibonacci_short
make backtest                              # 等同 STRATEGY=all

# 指定回測天數（預設 30 天，從今天往前推算）
make backtest DAYS=60

# 指定回測區間（從 2025-06-04 模擬到 2026-06-04）
make backtest START=2025-06-04 END=2026-06-04

# 只指定起始，END 預設今天
make backtest START=2025-06-04

# 強制重新下載資料（忽略快取）
make backtest NO_CACHE=1

# 帳戶模式（套用帳戶止盈策略，計算 USDT 損益）
make backtest ACCOUNT=帳號名稱

# 先用少數幣種快速驗證
make backtest START=2025-06-04 END=2026-06-04 SYMBOLS=BTCUSDT,ETHUSDT
```

**CLI 參數**：

| 參數 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `--strategy` / `-s` | str | 必填 | `long_breakout` \| `death_cross_short` \| `fibonacci_long` \| `fibonacci_short` \| `all` |
| `--days` / `-d` | int | 30 | 回測天數（從今天往前推算；`--start` 存在時忽略） |
| `--start` | str | "" | 回測起始日期 `YYYY-MM-DD`（優先於 `--days`） |
| `--end` | str | "" | 回測結束日期 `YYYY-MM-DD`（預設今天） |
| `--no-cache` | flag | False | 強制重新下載，不使用快取 |
| `--symbols` | str | "" | 指定幣種（逗號分隔），空白代表全部 USDT 永續合約 |
| `--account` / `-a` | str | "" | 帳號名稱，啟用帳戶模式 |

**`--start`/`--end` 與 `--days` 的差異**：

| 模式 | 資料起點 | 訊號記錄範圍 | 快取 key |
|------|---------|------------|---------|
| `--days N` | 今天往前 N 天 | 同上 | 含今天日期（每日更新） |
| `--start`/`--end` | start 往前 260 天（暖機） | start → end | 含結束日期（永久快取） |

---

## 4. 資料下載（data_fetcher.py）

### 4.1 資料來源

- **Binance Futures 公開 REST API**：`https://fapi.binance.com/fapi/v1/klines`
- **無需 API Key**
- **幣種範圍**：自動取得所有 USDT 永續合約（`quoteAsset=USDT`、`contractType=PERPETUAL`、`status=TRADING`）

### 4.2 時框與頁數

每個時框 API 每次最多 1500 根，系統根據 `backtest_days` 自動計算需要的頁數：

| 時框 | 計算方式 | 用途 |
|------|----------|------|
| 1d | 固定 1 頁 | 死亡叉 Layer 1/2 日線 EMA |
| 4h | `ceil((days × 6 + 200) / 1500)` 頁 | 多頭觸發 K 偵測 |
| 1h | `ceil((days × 24 + 250) / 1500)` 頁 | 死亡叉 Layer 3 信號 + EMA200 暖機 |
| 15m | `ceil(days × 96 / 1500)` 頁 | 多頭突破進場 + 止損 + 評估期 K 棒 |

額外加 14 天緩衝（評估窗口），確保最後一批訊號有足夠後續 K 棒可評估。

**區間模式（`--start`/`--end`）的 `backtest_days` 計算**：
```
backtest_days = 區間天數 + 260（暖機）
```
260 天暖機確保 EMA200 日線（需 200 根）在回測起點前已充分暖機。`_calc_pages` 內部再加 14 天評估緩衝。

**一年區間的頁數範例**（`--start 2025-06-04 --end 2026-06-04`，backtest_days = 625）：

| 時框 | 頁數 | 覆蓋天數 |
|------|------|---------|
| 1d | 1 頁 | 1500 天 |
| 4h | 3 頁 | ~750 天 |
| 1h | 11 頁 | ~687 天 |
| 15m | 41 頁 | ~639 天 |
| **合計** | **56 頁/幣種** | 全幣種首次下載 ~5 小時 |

### 4.3 快取機制

快取路徑：`backtest/cache/{symbol}_{interval}_{backtest_days}d_{date_key}.pkl`

| 模式 | date_key | 說明 |
|------|---------|------|
| `--days` 模式 | 今天日期 `YYYYMMDD` | 每日更新，確保拿到最新 K 棒 |
| `--start`/`--end` 模式 | 結束日期 `YYYYMMDD` | 永久快取（歷史資料不會改變） |

**已有快取的區間**：

| 策略 | 區間 | date_key | backtest_days | 說明 |
|------|------|---------|--------------|------|
| 全策略（all） | 2025-06-03 ~ 2025-06-06 | `20250606` | 263 | 可直接使用，無需重新下載 |

- `--no-cache` 強制重新下載，忽略任何快取
- 快取 key 含 `backtest_days`，不同天數的快取不互相污染
- 區間模式同一個 `--start`/`--end` 組合只需下載一次

---

## 5. 回測引擎（engine.py）

### 5.1 核心設計：對齊正式系統 replay 行為

正式系統啟動時只重播最後 N 根 K 棒（由 deque maxlen 決定）。回測必須對齊此行為，否則狀態機因處理過多歷史 K 棒而產生與正式系統截然不同的狀態。

| 時框 | deque maxlen | 對應天數 | 回測行為 |
|------|-------------|---------|---------|
| 4h | 200 根 | ≈33 天 | 短回測（≤33天）：只對最後 200 根呼叫狀態機 |
| 1d | 250 根 | 250 天 | 長回測（>250天）：全程呼叫狀態機 |
| 1h | 無限制 | — | 全程呼叫（正式系統無 1h replay） |
| 15m | 無限制 | — | 全程呼叫（正式系統無 15m replay） |

**區間模式自動進入長期模式**：由於 `backtest_days = 區間天數 + 260`，即使只回測 1 天，`backtest_days = 261 > 250`，兩個時框都走全程呼叫路徑，確保暖機完整。

### 5.2 時間補丁

回測期間將 `time.time()` 替換為當前處理的 K 棒收盤時間，讓策略的**冷卻邏輯**使用 K 棒時間而非真實系統時間：

```python
_patch_time(candle_close_ts)  # 處理前替換
try:
    on_new_Xh_candle(symbol, candle)
finally:
    _restore_time()            # 處理後還原
```

### 5.3 多時框事件佇列

所有時框的 K 棒合併為單一佇列，按收盤時間排序，優先順序：`1d > 4h > 1h > 15m`（同時刻時較大時框先處理）。

### 5.4 單一幣種回測流程

```
1. 初始化符號狀態
   └→ 清空 strategy_state[symbol]、death_cross_state[symbol]、fibonacci_state[symbol]
   └→ 建立 deque（4h: 200根, 1d: 250根, 1h/15m: 無限制）

2. 計算回測期間邊界
   └→ --days 模式：backtest_start_ms = 最後一根15m K棒時間 - backtest_days × 86400000
   └→ --start/--end 模式：backtest_start_ms = start_date 的 Unix 時間戳（ms）
                          backtest_end_ms   = end_date 的 Unix 時間戳（ms）

3. 計算狀態機啟動邊界（對齊 deque maxlen）
   └→ 4h: 從第 (len-200) 根開始呼叫狀態機
   └→ 1d: 從第 (len-250) 根開始呼叫狀態機

4. 逐根 K 棒處理
   ├→ 每根 K 棒都更新 deque（全程）
   ├→ 達到啟動邊界後開始呼叫策略狀態機
   └→ 訊號記錄條件：
        --days 模式：candle >= backtest_start_ms
        --start/--end 模式：backtest_start_ms <= candle < backtest_end_ms
      （end 之後的 K 棒繼續餵入狀態機但不記錄訊號，供評估窗口使用）

5. 回傳訊號列表
```

### 5.5 訊號格式

**Type 1（多頭）**：
```python
{
    "type": "type1",
    "symbol": "BTCUSDT",
    "close": 64500.50,       # 進場價
    "stop_loss": 64000.25,   # 止損價
    "top": 64800.0,          # 突破目標（consolidation_high）
    "bottom": 63500.0,       # 盤整低點
    "vol_ratio": 4.2,        # 突破量能倍數
    "candle_open_time_ms": 1234567890000,
}
```

**Type 3（空頭）**：
```python
{
    "type": "type3",
    "symbol": "BNBUSDT",
    "close": 620.50,         # 進場價
    "stop_loss": 650.75,     # 止損價（EMA200(1H) + ATR14(1H)）
    "signal_type": "rejection",  # "rejection" 或 "engulfing"
    "ema200_1h": 645.0,
    "atr_14_1h": 5.75,
    "close_t0": 618.0,       # 日線跌破當天收盤（幅度保護基準）
    "vol_ratio": 1.8,
    "candle_open_time_ms": 1234567890000,
}
```

**Fibonacci（多單 / 空單）**：
```python
{
    "type": "fibonacci_long",    # 或 "fibonacci_short"
    "symbol": "ETHUSDT",
    "close": 2850.50,            # 進場價（bar9 收盤）
    "stop_loss": 2840.25,        # 止損價
    "take_profit_1": 2920.75,    # TP1（Fib_6.92）
    "bar_a_time": 1700000000,    # barA 開盤時間（秒）
    "bar_9_time": 1700008900,    # bar9 開盤時間（秒）
    "fib_0": 2845.00,
    "fib_1": 2855.00,
    "fib_range": 10.00,
    "fib_1_73": 2862.30,
    "fib_6_92": 2914.20,
    "interval": "1h",
    "candle_open_time_ms": 1700008900000,  # bar9 開盤時間（ms，供評估器使用）
}
```

---

## 6. 策略邏輯（完全對應正式系統）

回測直接呼叫正式策略函數，以下說明兩種策略的進場條件。

### 6.1 Long Breakout（Type 1）

**狀態機**：

```
IDLE
  ↓ 4h 陽線：漲幅 ≥ 3% + 量能 > 前12根均量 × 3
TRACKING（記錄盤整高低點）
  ↓ 4h 實體收破 consolidation_low
IDLE（廢棄）
  ←
  ↓ 4h 創新高（high > consolidation_high）
TRACKING（更新 consolidation_high、重置計時）
  ↓ 盤整 ≥ 12 小時
READY
  ↓ 15m 帶量突破（見下方條件）
進場訊號
```

**IDLE → TRACKING 觸發條件（同時滿足）**：
1. 陽線（`close > open`）
2. 單根漲幅 ≥ `PUMP_THRESHOLD`（3%）
3. 成交量 > 前 12 根 4h 均量 × `TRIGGER_VOLUME_MULT`（3，嚴格大於）

**READY → 進場條件（15m，同時滿足）**：
1. `close > consolidation_high × (1 + BREAKOUT_BODY_PCT)`（超頂 0.5%）
2. 成交量 > 前 192 根 15m 均量（`kline_15m_ohlc[-193:-1]`）× `BREAKOUT_VOLUME_MULT`（3.5）
3. 冷卻時間未到（距上次進場訊號 ≥ `STRATEGY_COOLDOWN` = 14400 秒）

**止損設置（回掃邏輯）**：
從當前 15m K 棒往前掃，找當前 4h 時間邊界內、量能超過 `LOOKBACK_VOLUME_MULT`（2.5）倍的 K 棒低點，連續命中則取最低點；遇到低量 K 棒則停止。

### 6.2 Death Cross Short（Type 3）

**三層架構**：

| 層 | 時框 | 條件 | 觸發效果 |
|----|------|------|---------|
| Layer 1 | 日線 | EMA50(D) < EMA200(D) | IDLE → WATCHING |
| Layer 2 | 日線 | close(D) < EMA200(D) + 時效檢查 | WATCHING → ALERT（48H 窗口） |
| Layer 3 | 1H | 信號 A 或信號 B | 做空進場訊號 |

**Layer 2 時效性檢查**：
- 距上次 `close > EMA200(D)` 的時間 ≤ `DC_MAX_ROLLBACK_HOURS`（48 小時）

**ALERT 廢棄條件**：
- EMA50(D) ≥ EMA200(D)（Layer 1 失效）→ 回 IDLE
- 48H 窗口到期（`candle_ts - alert_time > DC_ALERT_WINDOW_HOURS × 3600`）→ 回 WATCHING
- 日線收盤 > `close_t0 × DC_PRICE_RECOVERY_PCT`（回漲 10%）→ 回 WATCHING

**Layer 3 進場條件（ALERT 狀態下，1H K 棒收盤）**：

先做共同檢查：
- ALERT 窗口未到期
- 幅度保護未超限
- 進場次數 < `DC_MAX_ENTRIES_PER_ALERT`（2）
- 冷卻時間未到（≥ 14400 秒）

信號 A（拒絕蠟燭，優先判斷）：
1. `high > EMA200(1H)`（上影線刺穿）
2. `close < EMA200(1H)`（收盤壓回）
3. `(EMA200 - close) / EMA200 ≥ DC_REJECTION_BODY_PCT`（0.5% 壓制幅度）
4. `close < open`（陰線實體）

信號 B（吞噬型態，信號 A 不成立時判斷）：
1. `close < EMA200(1H)`
2. `open > prev_close`（跳空高開）
3. `close < prev_close`（收盤低於前根收盤）
4. `|close - open| > |prev_close - prev_open| × DC_ENGULF_BODY_RATIO`（實體吞噬，1.5 倍）
5. `volume > prev_volume × DC_ENGULF_VOLUME_RATIO`（帶量，1.5 倍）

**止損設置**：`EMA200(1H) + ATR14(1H)`

### 6.3 Fibonacci（多單 / 空單）

**掃描機制**：固定窗口，每根 `FIB_K_INTERVAL` K 棒閉合後呼叫一次 `on_new_fib_candle`，barA 固定指向 `klines[-9]`，bar9 固定指向 `klines[-1]`（最新閉合 K 棒）。

**多單形態（fibonacci_long）**：

```
條件 1：barA.close >= EMA_value（barA 在 EMA 上方）
條件 2：barB.close >= EMA_value（barB 在 EMA 上方）
條件 3：barB.low >= barA.low（底底高）
條件 4：bar5.high >= Fib_1.73（第 5 根影線觸及確認層級）
條件 5：bar8.high >= Fib_1.73（第 8 根影線觸及確認層級）
條件 6：bar2 ~ bar9 無任何 close < barA.low（未破底）
條件 7：bar5 ~ bar9 未觸及止損（SL）
條件 8：bar5 ~ bar9 未觸及 TP1（Fib_6.92）
```

**空單形態（fibonacci_short）**：條件方向相反（頂頂低，EMA 下方，low 觸及 Fib_1.73）。

**Fib 計算**：
- 多單：`fib0 = min(barA、barB 實體低點)`，`fib1 = max(實體高點)`，延伸方向朝上
- 空單：`fib0 = max(barA、barB 實體高點)`，`fib1 = min(實體低點)`，延伸方向朝下

**止損（SL）**：
- 多單：`min(barA.low, barB.low, bar8.low)`
- 空單：`max(barA.high, barB.high, bar8.high)`

`on_new_fib_candle` 回傳 **list**（0～2 筆訊號），engine.py 用 `signals.extend(fib_signals)` 收集。

---

## 7. 策略參數與 config.py 對應

**回測直接讀取 `app/setting/config.py`，與正式系統共用同一份參數。**

### Long Breakout 參數

| 參數名稱 | config.py 欄位 | 預設值 | 影響邏輯 |
|----------|---------------|--------|---------|
| PUMP_THRESHOLD | `PUMP_THRESHOLD` | 3% | IDLE→TRACKING 觸發漲幅門檻 |
| TRIGGER_VOLUME_MULT | `TRIGGER_VOLUME_MULT` | 3× | IDLE→TRACKING 觸發量能倍數 |
| CONSOLIDATION_MIN_HOURS | `CONSOLIDATION_MIN_HOURS` | 12h | TRACKING→READY 最低盤整時數 |
| BREAKOUT_VOLUME_MULT | `BREAKOUT_VOLUME_MULT` | 3.5× | 15m 突破量能門檻 |
| BREAKOUT_BODY_PCT | `BREAKOUT_BODY_PCT` | 0.5% | 15m 收盤超頂幅度 |
| LOOKBACK_VOLUME_MULT | `LOOKBACK_VOLUME_MULT` | 2.5× | 止損回掃放量門檻 |
| METHOD_B_GAIN_ADVANTAGE | `METHOD_B_GAIN_ADVANTAGE` | 10% | Method B 新觸發 K 漲幅優勢 |
| METHOD_B_RELAXED_THRESHOLD | `METHOD_B_RELAXED_THRESHOLD` | 10% | Method B 直接重置門檻 |
| STRATEGY_COOLDOWN | `STRATEGY_COOLDOWN` | 14400s | 兩策略共用冷卻時間 |

### Death Cross Short 參數

| 參數名稱 | config.py 欄位 | 預設值 | 影響邏輯 |
|----------|---------------|--------|---------|
| DC_DAILY_EMA_FAST | `DC_DAILY_EMA_FAST` | 50 | Layer 1 短均線（日線） |
| DC_DAILY_EMA_SLOW | `DC_DAILY_EMA_SLOW` | 200 | Layer 1/2 長均線（日線） |
| DC_1H_EMA_PERIOD | `DC_1H_EMA_PERIOD` | 200 | Layer 3 壓制均線（1H） |
| DC_1H_ATR_PERIOD | `DC_1H_ATR_PERIOD` | 14 | 止損 ATR 週期 |
| DC_ALERT_WINDOW_HOURS | `DC_ALERT_WINDOW_HOURS` | 48h | ALERT 窗口有效期 |
| DC_MAX_ROLLBACK_HOURS | `DC_MAX_ROLLBACK_HOURS` | 48h | Layer 2 時效性 |
| DC_PRICE_RECOVERY_PCT | `DC_PRICE_RECOVERY_PCT` | 1.10 | 幅度保護（回漲 10%） |
| DC_MAX_ENTRIES_PER_ALERT | `DC_MAX_ENTRIES_PER_ALERT` | 2 | 每窗口最大進場次數 |
| DC_REJECTION_BODY_PCT | `DC_REJECTION_BODY_PCT` | 0.005 | 信號 A 壓制幅度門檻 |
| DC_ENGULF_BODY_RATIO | `DC_ENGULF_BODY_RATIO` | 1.5 | 信號 B 實體吞噬倍數 |
| DC_ENGULF_VOLUME_RATIO | `DC_ENGULF_VOLUME_RATIO` | 1.5 | 信號 B 量能倍數 |

### Fibonacci 參數

| 參數名稱 | config.py 欄位 | 預設值 | 影響邏輯 |
|----------|---------------|--------|---------|
| FIB_K_INTERVAL | `FIB_K_INTERVAL` | `"1h"` | K 棒週期（`"15m"` / `"1h"` / `"4h"`） |
| FIB_EMA_PERIOD | `FIB_EMA_PERIOD` | 55 | barA/barB EMA 過濾週期 |
| FIB_CONFIRM_LEVEL | `FIB_CONFIRM_LEVEL` | 1.73 | bar5/bar8 影線確認的 Fib 倍數 |
| FIB_TP1_LEVEL | `FIB_TP1_LEVEL` | 6.92 | TP1 止盈的 Fib 倍數 |

---

## 8. 交易評估（evaluator.py）

### 8.1 評估窗口

每筆訊號向後取 **672 根 15m K 棒（= 7 天）**作為評估期。

### 8.2 基礎模式：R 倍數計算

計算訊號在止損前最高達到的整數 R 倍數（0～50R）。

**保守原則**：同一根 K 棒先判斷止損，命中則停止，不再計算該根的盈利幅度。

```
1R 風險距離 = |entry - stop_loss|

多頭每根 K 棒：
  if low ≤ stop_loss → stop_hit = True，結束
  else 依序檢查 1R, 2R, ... 是否達到 high ≥ entry + n × 1R

空頭每根 K 棒：
  if high ≥ stop_loss → stop_hit = True，結束
  else 依序檢查是否達到 low ≤ entry - n × 1R
```

**輸出欄位**：`max_r_reached`（0～50）、`stop_hit`、`risk_1r`、`risk_1r_pct`、`eval_incomplete`

### 8.3 帳戶模式：P&L 計算

套用帳戶設定中的止盈策略（`TP_STRATEGY` 陣列，每筆含 `RR_RATIO` 和 `PERCENT`）。

**每 1R 美元損益計算**：

| RISK_TYPE | 計算方式 |
|-----------|---------|
| 0（固定倉位） | `(risk_1r / entry) × RISK_AMOUNT × RISK_LEVERAGE` |
| 1（固定損失） | 固定為 `RISK_AMOUNT` |

**P&L 計算**：
- 逐根 15m K 棒掃描，先檢查止損（保守原則）
- 止損命中：`pnl -= risk_per_1r × remaining_pct / 100`（剩餘倉位全部按 1R 虧損）
- 止盈命中：`pnl += RR_RATIO × risk_per_1r × PERCENT / 100`（按比例計算該格損益）

**評估不完整條件**：後續 K 棒不足 672 根 AND 未止損 AND 未達滿足所有止盈 → `eval_incomplete = True`

---

## 9. 輸出格式（reporter.py）

### 9.1 基礎模式 CSV

**檔名**：`backtest/results/{strategy}_{YYYYMMDD}.csv`

| 欄位 | 說明 | 範例 |
|------|------|------|
| symbol | 幣種 | BTCUSDT |
| strategy | 策略名稱 | long_breakout |
| signal_time | 訊號時間（台北時區，UTC+8） | 2024-06-02 14:30 |
| direction | 方向 | LONG / SHORT |
| entry_price | 進場價 | 64500.50 |
| stop_loss | 止損價 | 64000.25 |
| risk_1r_pct | 1R 風險百分比（%） | 0.7750 |
| risk_1r | 1R 價差絕對值 | 500.25 |
| max_r_reached | 評估期最高 R 倍數 | 2 |
| stop_hit | 是否止損 | False |
| eval_incomplete | 評估窗口不足 7 天 | False |

### 9.2 帳戶模式 CSV

**檔名**：`backtest/results/{strategy}_{account}_{YYYYMMDD}.csv`

基礎欄位相同，尾部追加：

| 欄位 | 說明 |
|------|------|
| tp{n}_rr | 第 n 個止盈的 RR 比例 |
| tp{n}_hit | 第 n 個止盈是否被觸發 |
| win | 是否勝利（任一止盈被觸發） |
| pnl_usdt | 實現損益（USDT） |
| stop_hit | 是否止損 |
| eval_incomplete | 評估不完整 |

### 9.3 終端摘要

**基礎模式**：
```
策略：long_breakout
訊號：25（完整：24，不完整：1）
止損率：3/24 = 12.5%  未止損：87.5%
R 分佈（max_r_reached）：
  0R :   3  ███
  1R :  10  ██████████
  2R :   8  ████████
  3R :   3  ███
```

**帳戶模式**：
```
策略：long_breakout  帳戶：xururmp152369
訊號：25（完整：24，不完整：1）
勝率：20/24 = 83.3%
總損益：+450.50 USDT  每筆均：+18.77 USDT
TP1（1.5R）命中：15/24 = 62.5%
TP2（3.0R）命中：8/24 = 33.3%
```

---

## 10. 參數一致性說明

**回測是否遵照現在策略的設定？**

**答：是，完全遵照。** 呼叫鏈如下：

```
engine.py
  └→ state_machine.py（on_new_4h_candle / on_new_1h_candle / on_new_daily_candle / on_new_fib_candle）
       └→ long_breakout.py       ──→ from ..setting.config import PUMP_THRESHOLD, BREAKOUT_VOLUME_MULT, ...
       └→ death_cross_short.py   ──→ from ..setting.config import DC_DAILY_EMA_FAST, DC_ALERT_WINDOW_HOURS, ...
       └→ long_short_fibonacci.py ──→ from ..setting.config import FIB_K_INTERVAL, FIB_EMA_PERIOD, ...
```

回測不維護自己的參數副本，直接 import `app/setting/config.py`。因此：

- 若修改 `PUMP_THRESHOLD` 從 3% 改為 4%，下次執行回測會立即使用新值
- 若帳號 JSON 有自訂的 `LONG_TP_STRATEGY`，帳戶模式會讀取並套用
- 若新增或移除 config.py 的參數，回測行為也會同步改變

**唯一例外**：評估期的止盈止損邏輯（`evaluator.py`）是回測專屬，正式系統另有自動下單模組（`app/order/`）負責管理持倉。回測的評估是理想假設（以 15m 收盤價為進場點、止損以 K 棒影線觸碰為準），可能與真實市價單的成交有些微差異。

---

## 11. 已知限制

1. **進場價假設**：以訊號發生當根 15m K 棒收盤價為進場價，實際市場可能有滑點
2. **止損評估保守**：同一根 K 棒若先碰止損再創高，回測計為止損（不計後續盈利）；實際交易中掛單可能在不同時序成交
3. **多幣種同時觸發**：回測各幣種獨立評估，不考慮同時多個訊號時的資金分配限制（`LONG_ORDER_LIMIT` / `SHORT_ORDER_LIMIT` 未在回測中執行）
4. **評估不完整**：回測最後幾天的訊號可能因後續 K 棒不足 7 天而標記 `eval_incomplete=True`，建議使用 `--days` 加一些緩衝天數
5. **快取資料**：快取以「當天」為 key，同一天內不同時段執行結果會相同（加 `--no-cache` 可強制更新）
