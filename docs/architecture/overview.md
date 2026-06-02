# Architecture Overview

> 本文描述系統整體架構、資料流向與各模組職責。策略的詳細行為規格見 [`docs/specs/`](../specs/)，設計決策背後的理由見 [`docs/decisions/`](../decisions/)。

---

## 系統全貌

```mermaid
flowchart TD
    BN[Binance Futures\nWebSocket / REST API]

    subgraph DataLayer[資料層]
        WS[binance_opendata.py\nWebSocket 監聽 + 歷史載入]
    end

    subgraph StateLayer[策略狀態層]
        SM[state_machine.py\n協調器]
        LB[long_breakout.py\nType 1 多頭]
        DC[death_cross_short.py\nType 3 死亡叉空頭]
        AU[analysis_utils.py\nEMA / ATR / 形態工具]
    end

    subgraph OutputLayer[輸出層]
        SA[strategy_alerts.py\nTelegram 發送]
        OM[order_manager.py\n自動下單]
    end

    subgraph BotLayer[Bot 控制層]
        CMD[command.py\nTelegram 指令]
        MON[monitor.py\n週期掃描]
        UC[user_config.py\n帳號 / 設定]
    end

    subgraph Storage[全域狀態]
        MS[models.py\nsymbol_state\nstrategy_state\ndeath_cross_state]
    end

    BN -->|markPrice + kline| WS
    WS -->|每根收盤 K| SM
    SM --> LB
    SM --> DC
    LB --> AU
    DC --> AU
    LB & DC -->|訊號 dict| SA
    LB & DC -->|訊號 dict| OM
    SA -->|Telegram| USER[使用者]
    OM -->|REST API| BN
    CMD -->|查詢/設定| UC
    MON -->|即時廢棄掃描| SM
    WS & SM & LB & DC <-->|讀寫| MS
```

---

## 資料流：從 WebSocket 到訊號

```mermaid
sequenceDiagram
    participant BN as Binance WS
    participant OD as binance_opendata
    participant SM as state_machine
    participant ST as 策略模組
    participant SA as strategy_alerts
    participant OM as order_manager

    BN->>OD: kline 收盤事件
    OD->>OD: 去重 + 存入 deque
    OD->>SM: on_new_Xh_candle(symbol, candle)
    SM->>ST: 轉派至對應策略
    ST->>ST: 狀態機轉換
    alt 產生進場訊號
        ST-->>SM: 返回 signal dict
        SM-->>OD: 返回 signal dict
        OD->>SA: asyncio.create_task(send_alert)
        OD->>OM: asyncio.create_task(place_orders)
        SA->>SA: 廣播給 CHAT_ID + 各使用者
        OM->>OM: 依使用者設定個別下單
    else 無訊號
        ST-->>SM: 返回 None
    end
```

> **關鍵原則**：WebSocket handler 不得阻塞。所有 Telegram 發送與 Binance 下單都透過 `asyncio.create_task()` 非同步執行。

---

## 模組職責

### 資料層

| 模組 | 職責 |
|------|------|
| `binance_opendata.py` | WebSocket 多路監聽（markPrice + kline_15m/1h/4h/1d）<br>歷史資料批次載入（REST API，限流控制）<br>自動重連（指數退避，最高 60s）<br>去重處理（`last_kline_close_time_Xm`） |

### 策略狀態層

| 模組 | 職責 |
|------|------|
| `state_machine.py` | 協調器（Orchestrator）：對外維持統一 API，內部分派給各策略<br>外部模組只 import 此檔，不直接 import 策略模組 |
| `long_breakout.py` | Type 1 多頭狀態機（IDLE / TRACKING / READY）<br>4h 收盤觸發狀態轉換，15m 收盤偵測進場訊號 |
| `death_cross_short.py` | Type 3 死亡叉狀態機（IDLE / WATCHING / ALERT）<br>日線收盤更新格局，1h 收盤偵測進場信號 |
| `analysis_utils.py` | 共用工具：EMA 計算、ATR 計算、K 棒形態判斷、方向感知函數 |

### 輸出層

| 模組 | 職責 |
|------|------|
| `strategy_alerts.py` | 訊號格式化（Markdown）<br>通知路由：CHAT_ID 全量接收；個別使用者依 `NOTIFY_STRATEGY` 過濾<br>開單結果廣播 |
| `order_manager.py` | 依使用者設定（PRD/DEV）決定正式或模擬環境<br>市價開倉 + SL/TP 設定<br>Binance -1007 逾時自動查詢重試<br>部位上限檢查、加倉保護 |

### Bot 控制層

| 模組 | 職責 |
|------|------|
| `command.py` | Telegram 指令處理：`/register`、`/login`、`/setup`、`/myconfig`、`/tracking` 等 |
| `monitor.py` | 週期掃描（每 10 秒）：幣種清單更新、即時廢棄掃描、session 過期檢查 |
| `user_config.py` | 帳號系統（Fernet 加密存檔）、session 管理、使用者設定讀寫 |

### 全域狀態

| 容器 | 內容 | 管理方 |
|------|------|--------|
| `symbol_state` | 每幣種即時狀態（price, kline deques） | `binance_opendata` |
| `strategy_state` | 多頭策略狀態 dict（per symbol） | `long_breakout` |
| `death_cross_state` | 死亡叉策略狀態 dict（per symbol） | `death_cross_short` |

---

## 啟動流程

```mermaid
flowchart TD
    A[main.py 啟動] --> B[驗證 ENCRYPTION_KEY]
    B --> C[初始化 Telegram Application]
    C --> D[建立 Binance AsyncClient]
    D --> E[initialize_symbols\n初始化幣種清單]
    E --> F[load_historical_data_batch\n載入 4h/15m/1h/1d 歷史資料]
    F --> G[replay_historical_4h_candles\n多頭狀態回播]
    F --> H[replay_historical_daily_candles_dc\n死亡叉狀態回播]
    G & H --> I[啟動三個背景任務]
    I --> J[monitor_price_websocket\nWebSocket 即時更新]
    I --> K[periodic_screen\n每 10 秒週期掃描]
    I --> L[monthly_restart_scheduler\n每月 1 日重啟]
    J & K & L --> M[Telegram polling 啟動]
```

> **歷史回播的目的**：啟動時從 Binance 載入歷史 K 棒，依序重播以恢復策略狀態，確保重啟後不遺失進行中的盤整追蹤或死亡叉監控。

---

## 週期掃描（每 10 秒）

```mermaid
flowchart LR
    T[periodic_screen] --> A[initialize_symbols]
    A -->|新幣種| B[載入歷史 + 回播]
    A -->|失效幣種| C[清理 symbol_state\nstrategy_state\ndeath_cross_state]

    T --> D[scan_strategy]
    D -->|每個 symbol| E[check_long_invalidation_realtime\nmarkPrice < consolidation_low?]
    E -->|是| F[重置多頭狀態 → IDLE]

    T -->|每小時| G[check_expired_sessions\n停用過期帳號的自動下單]
```

---

## Candle Tuple 格式

所有時框統一：`(open_time_ms, open, high, low, close, quote_volume)`

| 時框 | deque | maxlen | 用途 |
|------|-------|--------|------|
| 1d | `kline_daily_ohlc` | 250 | EMA200(D) 需 200 根，250 根緩衝 |
| 4h | `kline_4h_ohlc` | 200 | 多頭狀態機驅動 + EMA 計算 |
| 1h | `kline_1h_ohlc` | 250 | 死亡叉 1H 信號 + EMA200(1H) + ATR(14) |
| 15m | `kline_15m_ohlc` | 200 | Type 1 突破進場量能計算（192 根 baseline） |

> 時間欄位用 `k["t"]`（open_time_ms），不用 `k["T"]`（close_time_ms）。

---

## 自動下單邏輯

```mermaid
flowchart TD
    SIG[訊號 dict 進入] --> CHK[使用者設定檢查]
    CHK -->|PRD_STRATEGY 符合| PRD[正式環境 testnet=False]
    CHK -->|DEV_STRATEGY 符合| DEV[模擬環境 testnet=True]
    CHK -->|兩者皆無| SKIP[跳過]

    PRD & DEV --> LMT[部位上限檢查\nLONG_ORDER_LIMIT / SHORT_ORDER_LIMIT]
    LMT -->|超限| SKIP2[跳過]
    LMT -->|未超限| ORDER[市價開倉\n± 重試最多 5 次]
    ORDER -->|-1007 逾時| QUERY[查詢訂單狀態確認]
    QUERY --> SLTP[設定止損 + 止盈]
    ORDER --> SLTP
    SLTP --> NOTIFY[send_order_results\nTelegram 通知]
```

---

## 訊號通知路由

| 接收對象 | 條件 | 收到哪些訊號 |
|---------|------|------------|
| `CHAT_ID`（公頻） | 無條件 | 所有 Type 1 + Type 3 |
| 個別使用者（有設定 `NOTIFY_STRATEGY`） | 策略代號在陣列中 | 只收符合的策略訊號 |
| 個別使用者（`NOTIFY_STRATEGY = []`） | 靜默 | 不收任何訊號 |
| 個別使用者（無 `NOTIFY_STRATEGY` 欄位） | 提醒設定 | 收到提示訊息要求更新設定 |
