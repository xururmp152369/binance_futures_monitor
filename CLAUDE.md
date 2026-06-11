# CLAUDE.md — Binance 期貨監控機器人

## 專案概覽

自動化監控 Binance 永續合約，偵測三種進場機會，透過 Telegram Bot 發訊號並支援自動下單。監控全 USDT 合約，運行於本機 Docker。

- **Type 1**（多頭）：4h 拉漲後盤整突破 → `long_breakout.py`
- **Type 3**（空頭）：死亡叉制空（日線格局 + 1H EMA200 壓制）→ `death_cross_short.py`
- **Fibonacci**（雙向）：底底高/頂頂低 + Fib 1.73 影線確認 → `long_short_fibonacci.py`

---

## 📚 文件索引

| 文件 | 說明 |
|------|------|
| [docs/architecture/overview.md](docs/architecture/overview.md) | 系統全貌、資料流、模組職責、啟動流程 |
| [docs/specs/long_breakout.md](docs/specs/long_breakout.md) | Type 1 完整行為規格 + 狀態機圖 |
| [docs/specs/death_cross_short.md](docs/specs/death_cross_short.md) | Type 3 完整行為規格 + 狀態機圖 |
| [docs/specs/long_short_fibonacci.md](docs/specs/long_short_fibonacci.md) | Fibonacci 策略完整行為規格 |
| [docs/decisions/README.md](docs/decisions/README.md) | 設計決策索引（ADR） |

---

## 使用者設定欄位

有效策略代號：`"long_breakout"`、`"death_cross_short"`、`"fibonacci_long"`、`"fibonacci_short"`

| 欄位 | 說明 |
|------|------|
| `PRD_STRATEGY` | 觸發**正式**自動下單的策略代號陣列（選填；`[]` 停用） |
| `DEV_STRATEGY` | 觸發**模擬**自動下單的策略代號陣列（選填；`[]` 停用） |
| `NOTIFY_STRATEGY` | 接收訊號通知的策略代號陣列（選填；`[]` 靜默；欄位不存在則發提醒） |
| `LONG_ORDER_LIMIT` | 同時持有多單部位數上限（必填正整數） |
| `SHORT_ORDER_LIMIT` | 同時持有空單部位數上限（選填正整數） |

`PRD_STRATEGY` 非空時需填 `PRD_API_KEY`/`PRD_SECRET_KEY`；`DEV_STRATEGY` 非空時需填 `API_KEY`/`SECRET_KEY`。  
訊號路由：Type 1 → `"long_breakout"`；Type 3 → `"death_cross_short"`。公頻 `CHAT_ID` 永遠收到所有訊號。

---

## 可設定參數

### 多頭策略（long_breakout）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `PUMP_THRESHOLD_BULL` | 3.5 | BTC 牛市（24h > 3%）時的觸發漲幅門檻（%） |
| `PUMP_THRESHOLD_NORMAL` | 3.0 | BTC 震盪市時的觸發漲幅門檻（%） |
| `PUMP_THRESHOLD_BEAR` | 2.5 | BTC 熊市（24h < -3%）時的觸發漲幅門檻（%） |
| `BTC_BULL_THRESHOLD` | 3.0 | 判定牛市的 BTC 1d 漲幅門檻（%） |
| `BTC_BEAR_THRESHOLD` | -3.0 | 判定熊市的 BTC 1d 跌幅門檻（%） |
| `TRIGGER_VOLUME_MULT` | 3 | 觸發 K 量能倍數（嚴格 `>`，前 12 根 4h 均量） |
| `CONSOLIDATION_MIN_HOURS` | 12 | 最低盤整時數（從最後一次創新高起算） |
| `METHOD_B_GAIN_ADVANTAGE` | 10.0 | Method B 新觸發 K 需超過原實體漲幅的比例優勢（%） |
| `METHOD_B_RELAXED_THRESHOLD` | 10.0 | 原觸發 K 實體漲幅超過此值時，Method B 直接重置 |
| `METHOD_B_VOLUME_RATIO` | 0.8 | Method B 體量驗證：新 K volume / 前觸發 K volume 最低比例 |
| `BREAKOUT_VOLUME_MULT` | 3.5 | Type 1 突破量能倍數（前 192 根 15m 均量） |
| `BREAKOUT_BODY_PCT` | 0.005 | Type 1 實體超頂幅度（0.5%） |
| `BREAKOUT_BODY_RATIO` | 0.60 | Type 1 進場 K 實體強度門檻（(close-open)/(high-low) ≥ 60%） |
| `BREAKOUT_ATR_PERIOD` | 14 | Type 1 ATR 計算週期（4h K 棒根數） |
| `BREAKOUT_ATR_RATIO` | 0.30 | Type 1 突破 ATR 力度：close - top ≥ ATR × 30% |
| `LOOKBACK_VOLUME_MULT` | 2.5 | Type 1 止損回掃放量門檻倍數（非連續） |
| `PUMP_CANDLE_TAKER_BUY_MIN` | 0.65 | Pump Candle 最低 Taker Buy Ratio（> 65%） |
| `TREND_FILTER_SMA_PERIOD` | 200 | 技術面濾波 SMA 週期（4h K 收盤） |
| `TREND_FILTER_ENABLED` | True | 是否啟用技術面濾波（False 可臨時關閉） |
| `STRATEGY_COOLDOWN` | 14400 | 全局告警冷卻（秒，4h，三層冷卻第二層） |
| `LIQUIDATION_BUFFER_CONFIRM_COUNT` | 3 | 即時廢棄連續確認次數（每次 10 秒） |

### 死亡叉策略（death_cross_short）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `DC_DAILY_EMA_FAST` | 50 | Layer 1 短均線（EMA50，日線） |
| `DC_DAILY_EMA_SLOW` | 200 | Layer 1/2 長均線（EMA200，日線） |
| `DC_1H_EMA_PERIOD` | 200 | Layer 3 壓制均線（EMA200，1H） |
| `DC_1H_ATR_PERIOD` | 14 | 止損 ATR 週期（1H） |
| `DC_ALERT_WINDOW_HOURS` | 48 | ALERT 窗口時數（日線跌破後有效期） |
| `DC_MAX_ROLLBACK_HOURS` | 48 | 時效性：距上次 close > EMA200 上限（小時） |
| `DC_PRICE_RECOVERY_PCT` | 1.10 | 幅度保護：Close_T0 × 此值超限廢棄 |
| `DC_MAX_ENTRIES_PER_ALERT` | 2 | 每 ALERT 窗口最大進場次數 |
| `DC_REJECTION_BODY_PCT` | 0.005 | 信號 A 壓制幅度（收盤低於 EMA200 至少 0.5%） |
| `DC_ENGULF_BODY_RATIO` | 1.5 | 信號 B 實體吞噬倍數 |
| `DC_ENGULF_VOLUME_RATIO` | 1.5 | 信號 B 量能倍數 |
| `DC_RISK_PCT_MIN` | 3.0 | 止損距離下限（%），過窄訊號勝率低 |
| `DC_RISK_PCT_MAX` | 12.0 | 止損距離上限（%），過寬訊號 EV 差 |

### Fibonacci 策略（fibonacci_long / fibonacci_short）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `FIB_K_INTERVAL` | `"1h"` | K 線週期（`"15m"` / `"1h"` / `"4h"`） |
| `FIB_EMA_PERIOD` | 55 | EMA 週期（barA/barB 收盤需在 EMA 同側） |
| `FIB_CONFIRM_LEVEL` | 1.73 | bar5/bar8 影線確認 Fib 倍數 |
| `FIB_TP1_LEVEL` | 6.92 | TP1 止盈 Fib 倍數 |

---

## 關鍵注意事項

1. **WebSocket 不得阻塞**：策略函數內所有 I/O（Telegram、下單）必須用 `asyncio.create_task()`
2. **15m 量能 baseline**：用 `kline_15m_ohlc[-193:-1]`（192 根），排除當前未收盤根
3. **多頭歷史回播**：啟動時 `replay_historical_4h_candles()` 恢復多頭盤整狀態
4. **死亡叉歷史回播**：啟動時 `replay_historical_daily_candles_dc()` 依序重播日線 K 棒
5. **廢棄條件即時掃描**：`scan_strategy()` 每 10 秒比對 markPrice；即時廢棄只重置多頭狀態
6. **廢棄條件用實體**：4h K 廢棄判斷以 `min(open, close)` 為準，下影線不觸發廢棄（見 [ADR-001](docs/decisions/001-abandonment-uses-body-not-wick.md)）
7. **Method B 僅在 READY**：TRACKING 狀態下出現新觸發 K 不處理 Method B（見 [ADR-002](docs/decisions/002-method-b-dual-reset-logic.md)）
8. **信號 A 無量能要求**：vol_ratio 僅供顯示，不構成觸發條件（見 [ADR-004](docs/decisions/004-signal-a-no-volume-requirement.md)）
9. **歷史資料 API 限流**：`load_historical_data_batch()` 每幣種載入順序為 4h → 15m → 1h → 1d，每次 API 呼叫後 sleep 0.5s，每幣種完成後 sleep 1.0s，每批次間 sleep 3.0s

---

## 測試工作流程

```bash
python -m pytest tests/ -v --ignore=tests/test_ws_diag.py
```

- 新功能開發：開發 Agent 實作；測試 Agent（worktree 隔離）獨立撰寫測試
- 既有功能回歸：直接跑 pytest，不需獨立 Agent
- `tests/test_ws_diag.py` 是手動診斷工具（需真實網路），不納入自動測試

---

## 回測系統

```bash
python backtest/run.py --strategy long_breakout
python backtest/run.py --strategy death_cross_short
python backtest/run.py --strategy all
python backtest/run.py --strategy all --days 30 --no-cache
python backtest/run.py --strategy all --account 帳號名稱
python backtest/run.py --strategy all --start 2025-06-04 --end 2026-06-04
python backtest/run.py --strategy all --start 2025-06-04  # --end 預設今天
```

- `--days`：從今天往前推算 N 天（預設 30）
- `--start`/`--end`：指定固定區間，系統自動追加 260 天暖機；優先於 `--days`
- 首次執行 30 天約需 20-30 分鐘；一年區間全幣種約需 4-5 小時；快取於 `backtest/cache/`
- 區間模式快取永久有效（歷史資料不變）；`--days` 模式每日更新
- 輸出 CSV：`backtest/results/{strategy}_{YYYYMMDD}.csv`
- 無需 API Key（Binance 公開 REST API）

---

## 待辦規劃

- [ ] 有條件使用機制（推薦碼 / 月費訂閱 / 帶單抽成）
- [ ] 導入 AI 模型訓練更合理的止盈止損位置
