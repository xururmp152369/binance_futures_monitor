# 網頁儀表板架構

> 儀表板是唯讀展示層，直接讀取 `app/setting/models.py` 中的全域狀態 dict，不修改任何策略邏輯。

---

## 目錄結構

```
web/
├── api/                        ← FastAPI 後端（Python）
│   ├── app.py                  ← FastAPI 工廠函式 create_web_app()
│   ├── schemas.py              ← Pydantic response models
│   ├── ws_manager.py           ← WebSocket ConnectionManager（broadcast）
│   └── routes/
│       ├── strategies.py       ← REST API endpoints
│       └── ws.py               ← /ws WebSocket endpoint
└── frontend/                   ← Vue 3 前端（TypeScript + Vite）
    ├── src/
    │   ├── types.ts            ← TypeScript 型別（對應 schemas.py）
    │   ├── style.css           ← Tailwind CSS 入口
    │   ├── App.vue             ← 根元件（直接掛載 Dashboard）
    │   ├── composables/
    │   │   └── useWebSocket.ts ← WS hook（自動重連 3s）
    │   ├── components/
    │   │   ├── CoinCard.vue    ← 幣種卡片（含迷你 TradingView + 操作按鈕）
    │   │   ├── ChartDialog.vue ← TradingView Widget iframe Dialog（90vw × 80vh）
    │   │   └── Pagination.vue  ← 分頁控制
    │   └── views/
    │       └── Dashboard.vue   ← 主視圖（Tab、篩選、Grid、分頁）
    ├── dist/                   ← build 後靜態檔（由 FastAPI 掛載）
    ├── tailwind.config.js
    └── vite.config.ts          ← dev proxy: /api、/ws → localhost:8000
```

---

## 整合方式

`app/main.py` 在啟動時加入第四個 asyncio task：

```python
web_config = uvicorn.Config(app=create_web_app(), host="0.0.0.0", port=8000, log_level="warning")
web_task = asyncio.create_task(uvicorn.Server(web_config).serve())
await asyncio.gather(price_task, screen_task, restart_task, web_task)
```

uvicorn 與 Telegram Bot / Binance WebSocket 共用同一個 asyncio event loop，無需額外程序。

---

## API Endpoints

| Method | Path | Query Params | 說明 |
|--------|------|-------------|------|
| GET | `/api/health` | — | 服務狀態 + 各策略幣種數 |
| GET | `/api/strategies/long_breakout` | `phase=TRACKING\|READY\|ALL` `page` `per_page` | 多頭策略清單 |
| GET | `/api/strategies/death_cross` | `phase=WATCHING\|ALERT\|ALL` `page` `per_page` | 死亡叉策略清單 |
| GET | `/api/strategies/fibonacci` | `direction=long\|short` `page` `per_page` | Fibonacci 觸發紀錄 |
| WS | `/ws` | — | 即時推送（狀態轉換事件） |

所有 GET endpoints 僅讀取 `models.py` 全域 dict，不寫入任何狀態。

---

## 前端功能

| 功能 | 說明 |
|------|------|
| 策略 Tab | Long Breakout / Death Cross / Fib Long / Fib Short |
| 狀態篩選 | 依策略顯示對應 phase 選項 |
| 幣種卡片 | 迷你 TradingView iframe 縮圖、關鍵指標、開啟線圖按鈕 |
| ChartDialog | TradingView Widget（`BINANCE:BTCUSDT.P`，EMA200 疊加，90vw × 80vh） |
| Binance 連結 | 直接跳轉 `binance.com/en/futures/{symbol}` |
| 分頁 | 每頁 10/25/50 筆 |
| 即時更新 | WebSocket push + setInterval 5s 輪詢雙保險 |
| RWD | 1 欄（mobile）/ 2 欄（tablet）/ 3 欄（desktop） |

---

## 啟動指南

### 開發模式

```bash
# Terminal 1：後端（完整策略邏輯 + FastAPI）
python -m app.main

# Terminal 2：前端 Vite dev server（proxy /api /ws → :8000）
cd web/frontend
npm run dev
# → http://localhost:5173
```

### 生產模式

```bash
cd web/frontend
npm run build                  # 輸出至 web/frontend/dist/
python -m app.main             # FastAPI 自動掛載 dist/ → http://localhost:8000
```

### Docker

Dockerfile 使用 **multi-stage build**，前端由 Node 18 自動 build，不需要手動執行 `npm run build`：

```
Stage 1 (node:18-slim)    → npm ci + npm run build → dist/
Stage 2 (python:3.11-slim) → 安裝 Python 依賴 + COPY dist/ from Stage 1
```

```bash
docker-compose up --build   # 完整 build（含前端）
docker-compose up           # 使用已有 image
```

容器啟動後直接存取 `http://<host>:8000`。

> **注意**：`web/frontend/dist/` 不進 git（`.gitignore` 已設定），也不需要 —  
> Docker build 時 Stage 1 會自動產生。`web/frontend/node_modules/` 同樣不進 Docker context（`.dockerignore` 已設定）。

---

## Mock 測試資料

`web/frontend/src/views/Dashboard.vue` 頂部有 `MOCK` 常數，用於在不啟動 Docker 的情況下預覽 UI 樣式。

**移除方式（確認樣式後）：**
1. 刪除 `// TODO: 測試用 mock 資料` 區塊（`const MOCK = { ... }`）
2. 刪除 `fetchData()` 裡的兩行注入（`const mock = MOCK[s]...`）
3. 重新 build：`npm run build`

---

## VSCode 開發環境

安裝 **Vue - Official**（`Vue.volar`）擴充以獲得正確的 `.vue` 型別提示與語法高亮。

> `.vscode/extensions.json` 已設定建議安裝，開啟專案時 VSCode 會自動提示。

TypeScript 的 `.vue` 模組型別宣告在 `web/frontend/src/vite-env.d.ts`，未安裝 Volar 時也能消除 import 報錯。
