# CLAUDE.md — Binance 期貨監控機器人

## 目前進行中

- `feature/type1-short` 分支：Type 1 Short 已完成並測試通過，待確認後 merge main
- 下一步：（待指定）

---

## 專案概覽

自動化監控 Binance 永續合約，偵測「4h 拉漲後盤整突破 / 均線反彈」進場機會，透過 Telegram Bot 發訊號並支援自動下單。監控全 USDT 合約，運行於本機 Docker。

---

## 架構與資料流

```
Binance WebSocket (markPrice + kline_15m/1h/4h)
        │
        ├─ markPrice → _handle_mark_price() → 更新 last_price
        ├─ 15m 收盤  → on_new_15m_candle()  → Type 1 突破訊號
        ├─ 1h  收盤  → on_new_1h_candle()   → Type 2 反彈訊號
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

**多頭 Run**
1. 陽線（close > open）啟動 run，記錄第一根 open / low / high
2. 後續每根 4h K：
   - `high > run_high` → 延伸 run（更新 run_high）
   - `high ≤ run_high` → run 停止，計算累積漲幅 = `(run_high - run_start_open) / run_start_open`
     - ≥ PUMP_THRESHOLD% → 進入 TRACKING；< 門檻 → 重置 run
3. `consolidation_low` = run 第一根的 low（廢棄線）
4. `consolidation_high` = run 期間最高 high
5. `consolidation_start_ts` = 最後一次創新高的時間（16h 計時起點）

**空頭 Run（完全對稱）**
1. 陰線啟動 run；`low < run_low` 延伸；`low ≥ run_low` 停止，檢查累積跌幅
2. `consolidation_high` = run 第一根的 high（廢棄線）
3. `consolidation_low` = run 期間最低 low

### 狀態轉換

**多頭（strategy_state）**
```
IDLE
 │ 觸發：累積漲幅 >= PUMP_THRESHOLD%，遇第一根不創新高的 K 棒
 ▼
TRACKING
 │ 廢棄：4h K low < consolidation_low → IDLE（即時掃描亦觸發）
 │ 延伸：4h K 創新高 → 更新 consolidation_high，重置 16h 計時
 │ Method B：盤整內新 sub-run 達標且起始 low > consolidation_low → 完整重置（底部上移）
 │ 進展：停止創新高後盤整 >= CONSOLIDATION_MIN_HOURS
 ▼
READY
 │ 廢棄：同上（創新高退回 TRACKING；Method B 同樣適用）
 ├─ 每根 15m 收盤 → Type 1 帶量突破（做多）
 └─ 每根 1h  收盤 → Type 2 均線反彈（做多）
```

**空頭（strategy_state_short）**
```
IDLE
 │ 觸發：累積跌幅 >= PUMP_THRESHOLD%，遇第一根不創新低的 K 棒
 ▼
TRACKING
 │ 廢棄：4h K high > consolidation_high → IDLE
 │ 延伸：4h K 創新低 → 更新 consolidation_low，重置計時
 │ Method B：新 sub-run 達標且起始 high < consolidation_high → 完整重置（頂部下移）
 │ 進展：停止創新低後盤整 >= CONSOLIDATION_MIN_HOURS
 ▼
READY
 └─ 每根 15m 收盤 → Type 1 Short 帶量跌破（做空）
```

### 進場訊號條件

**Type 1（帶量突破，做多）**
- 15m close > `consolidation_high`
- 15m 成交量 > 前 192 根平均 × `BREAKOUT_VOLUME_MULT`
- 止損 = 往回掃連續放量 K 的最低 low（限當前 4h K 起點後）

**Type 1 Short（帶量跌破，做空）**
- 15m close < `consolidation_low`
- 15m 成交量 > 前 192 根平均 × `BREAKOUT_VOLUME_MULT`
- 止損 = 往回掃連續放量 K 的最高 high（限當前 4h K 起點後）

**Type 2（均線反彈，做多）**
- 1h 最低 ≤ 任一 4h EMA（15/30/45/60） × (1 + `EMA_TOUCH_THRESHOLD`/100)
- 開盤 > 觸碰的 EMA；close > low × (1 + `WICK_THRESHOLD`/100)
- 盈虧比 = (consolidation_high - close) / (close - consolidation_low) ≥ `STRATEGY_RR_MIN`
- 止損 = `consolidation_low`

### strategy_state 欄位（多頭/空頭共用，語意對調）

| 欄位 | 多頭 | 空頭 |
|------|------|------|
| `consolidation_low` | run 第一根 low（廢棄線）；Method B 時更新 | run 最低點；創新低時更新 |
| `consolidation_high` | run 最高點；創新高時更新 | run 第一根 high（廢棄線）；Method B 時更新 |
| `consolidation_start_ts` | 最後一次創新高的時間（16h 起點） | 最後一次創新低的時間 |
| `run_start_open` | run 第一根 open | 同左 |
| `run_start_low` / `run_high` / `run_high_ts` | 多頭 run 追蹤 | 不使用 |
| `run_start_high` / `run_low` / `run_low_ts` | 不使用 | 空頭 run 追蹤 |

### Candle Tuple 格式

- 4h / 1h：`(open_time_ms, open, high, low, close)`
- 15m：`(open_time_ms, open, high, low, close, quote_volume)`
- 時間欄位必須用 `k["t"]`（open_time_ms），不得用 `k["T"]`（close_time_ms）

---

## 可設定參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `PUMP_THRESHOLD` | 8 | run 累積漲跌幅門檻（%） |
| `CONSOLIDATION_MIN_HOURS` | 16 | 最低盤整時數（從最後一次創新高/低起算） |
| `BREAKOUT_VOLUME_MULT` | 3 | Type 1 量能倍數（相對前 192 根平均） |
| `EMA_TOUCH_THRESHOLD` | 0.5 | Type 2 EMA 觸碰容忍距離（%） |
| `WICK_THRESHOLD` | 3 | Type 2 有效收針（%） |
| `STRATEGY_RR_MIN` | 1.0 | Type 2 最低盈虧比 |
| `STRATEGY_COOLDOWN` | 14400 | 告警冷卻秒數（4h） |

**user_config 新增欄位**
- `TP_STRATEGY_SHORT`：空頭止盈策略（選填，fallback 到 `TP_STRATEGY`）
- `STRATEGY` 可填 `"TYPE1_SHORT"`：啟用帶量跌破空頭策略

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

**conftest.py 注意：** `_DEFAULT_RUNTIME_CONFIG` 使用保守值（`CONSOLIDATION_MIN_HOURS=12`、`WICK_THRESHOLD=2`），與正式預設值不同，為方便控制測試邊界。

`tests/test_ws_diag.py` 是手動診斷工具（需真實網路），不納入自動測試。

---

## 待辦規劃

- [ ] 有條件使用機制（推薦碼 / 月費訂閱 / 帶單抽成）
- [ ] 導入 AI 模型訓練更合理的止盈止損位置
