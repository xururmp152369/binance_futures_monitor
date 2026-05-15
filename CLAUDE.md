# CLAUDE.md — Binance 期貨監控機器人

## 目前進行中

（無進行中的任務）

---

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
| `app/setting/models.py` | 全域狀態容器（symbol_state, strategy_state, runtime_config） |
| `app/datacenter/binance_opendata.py` | WebSocket 監聽、歷史資料載入、自動重連 |
| `app/strategy/state_machine.py` | 策略狀態機核心（IDLE/TRACKING/READY） |
| `app/strategy/strategy_alerts.py` | Telegram 訊號格式化與多使用者廣播 |
| `app/tgbot/monitor.py` | 週期任務（幣種清單更新、廢棄掃描、session 過期） |
| `app/command/command.py` | Telegram 指令處理（帳號/設定/查詢） |
| `app/user/user_config.py` | 帳號系統（註冊/登入/登出）、Fernet 加密設定、session 管理 |
| `app/trading/order_manager.py` | 自動下單（市價開倉 + SL/TP，支援 -1007 重試） |

---

## 策略狀態機規格

### 觸發偵測（Run 追蹤）

**Run 偵測**
1. 陽線（close > open）啟動 run，記錄第一根 open / low / high；同時從 `kline_4h_ohlc` 取前 `RUN_VOLUME_BASELINE_N` 根計算基準均量 `run_volume_baseline`，初始化 `run_candle_count=1`、`run_volume_sum=quote_volume`
2. 後續每根 4h K：
   - `high > run_high` → 延伸 run（更新 run_high），`run_candle_count += 1`，`run_volume_sum += quote_volume`
   - `high ≤ run_high` → run 停止，**三重評估**：
     1. 累積漲幅 = `(run_high - run_start_open) / run_start_open × 100` ≥ `PUMP_THRESHOLD`%
     2. `run_candle_count` ≤ `RUN_MAX_CANDLES`（超過視為緩漲）
     3. `run_volume_sum / run_candle_count` ≥ `run_volume_baseline × RUN_VOLUME_MULT`（baseline 不足時跳過此條件）
     - 三者全部通過 → 進入 TRACKING；任一不通過 → 重置 run
3. `consolidation_low` = run 第一根的 low（廢棄線）
4. `consolidation_high` = run 期間最高 high
5. `consolidation_start_ts` = 最後一次創新高的時間（16h 計時起點）

### 狀態轉換

```
IDLE
 │ 觸發：run 三重評估通過（漲幅/根數/均量），遇第一根不創新高的 K 棒
 ▼
TRACKING
 │ 廢棄：4h K low < consolidation_low → IDLE（即時掃描亦觸發）
 │ 延伸：4h K 創新高 → 更新 consolidation_high，重置 16h 計時，清空 run 量能欄位
 │ Method B：盤整內新 sub-run 三重評估通過，且起始 low > consolidation_low → 完整重置（底部上移）
 │ 進展：停止創新高後盤整 >= CONSOLIDATION_MIN_HOURS
 ▼
READY
 │ 廢棄：同上（創新高退回 TRACKING；Method B 同樣適用）
 └─ 每根 15m 收盤 → Type 1 帶量突破（做多）
```

### 進場訊號條件

**Type 1（帶量突破，做多）**
- 15m close > `consolidation_high`
- 15m 成交量 > 前 192 根平均 × `BREAKOUT_VOLUME_MULT`
- 止損 = 往回掃連續放量 K 的最低 low（限當前 4h K 起點後）

### strategy_state 欄位

| 欄位 | 說明 |
|------|------|
| `consolidation_low` | run 第一根 low（廢棄線）；Method B 時更新 |
| `consolidation_high` | run 期間最高點；創新高時更新 |
| `consolidation_start_ts` | 最後一次創新高的時間（16h 起點） |
| `run_start_open` | run 第一根 open |
| `run_start_low` / `run_high` / `run_high_ts` | run 追蹤欄位 |
| `run_candle_count` | run 期間已累積根數（三重評估條件②） |
| `run_volume_sum` | run 期間 quote_volume 累積總量 |
| `run_volume_baseline` | run 啟動時快照的前 N 根基準均量（三重評估條件③） |

### Candle Tuple 格式

- 4h：`(open_time_ms, open, high, low, close, quote_volume)`
- 15m：`(open_time_ms, open, high, low, close, quote_volume)`
- 時間欄位必須用 `k["t"]`（open_time_ms），不得用 `k["T"]`（close_time_ms）

---

## 可設定參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `PUMP_THRESHOLD` | 8 | run 累積漲幅門檻（%） |
| `CONSOLIDATION_MIN_HOURS` | 16 | 最低盤整時數（從最後一次創新高起算） |
| `BREAKOUT_VOLUME_MULT` | 3 | Type 1 量能倍數（相對前 192 根平均） |
| `STRATEGY_COOLDOWN` | 14400 | 告警冷卻秒數（4h） |
| `RUN_MAX_CANDLES` | 6 | Run 最多允許根數（超過視為緩漲，= 1 天） |
| `RUN_VOLUME_MULT` | 1.5 | Run 均量門檻倍數（相對前 N 根基準均量） |
| `RUN_VOLUME_BASELINE_N` | 20 | Run 量能基準參考根數（前 N 根 4h K 棒） |

---

## 關鍵注意事項

1. **WebSocket 不得阻塞**：策略函數內所有 I/O（Telegram、下單）必須用 `asyncio.create_task()`
2. **15m 量能 baseline**：用 `kline_15m_ohlc[-193:-1]`（192 根），排除當前未收盤根
3. **歷史回播**：啟動時 `replay_historical_4h_candles()` 恢復進行中的盤整，不需等下一根 4h K
4. **廢棄條件即時掃描**：`scan_strategy()` 每 10 秒比對 markPrice vs consolidation_low/high
5. **自動下單模式**：`order_manager.py` 頂部 `USE_TESTNET`，正式上線前須改 `False`

---

## 測試工作流程

```bash
python -m pytest tests/ -v --ignore=tests/test_ws_diag.py
```

**測試分工原則（新功能開發時）：**
- 開發 Agent：實作功能
- 測試 Agent（worktree 隔離）：獨立閱讀規格 + 最終程式碼，撰寫測試後執行

既有功能的回歸測試直接在主流程跑 pytest，不需獨立 Agent。

**conftest.py 注意：** `_DEFAULT_RUNTIME_CONFIG` 使用保守值（`CONSOLIDATION_MIN_HOURS=12`），與正式預設值不同，為方便控制測試邊界。

`tests/test_ws_diag.py` 是手動診斷工具（需真實網路），不納入自動測試。

---

## 待辦規劃

- [ ] 有條件使用機制（推薦碼 / 月費訂閱 / 帶單抽成）
- [ ] 導入 AI 模型訓練更合理的止盈止損位置
