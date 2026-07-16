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
│       ├── strategies.py       ← REST API endpoints + /chart/klines proxy
│       └── ws.py               ← /ws WebSocket endpoint
└── frontend/                   ← Vue 3 前端（TypeScript + Vite）
    ├── src/
    │   ├── types.ts            ← TypeScript 型別（對應 schemas.py）
    │   ├── style.css           ← Tailwind CSS 入口
    │   ├── App.vue             ← 根元件（直接掛載 Dashboard）
    │   ├── composables/
    │   │   └── useWebSocket.ts ← WS hook（自動重連 3s）
    │   ├── components/
    │   │   ├── CoinCard.vue          ← 幣種卡片（關鍵指標 + K線圖/TV/Binance 三按鈕）
    │   │   ├── ChartDialog.vue       ← TradingView 免費 Widget (tv.js)，EMA 15/30/45/60/200
    │   │   ├── KlineChartDialog.vue  ← lightweight-charts 自訂圖表（見下方說明）
    │   │   └── Pagination.vue        ← 分頁控制
    │   └── views/
    │       └── Dashboard.vue   ← 主視圖（Tab、篩選、Grid、分頁）
    ├── dist/                   ← build 後靜態檔（由 FastAPI 掛載）
    ├── tailwind.config.js
    └── vite.config.ts          ← dev proxy: /api、/ws → localhost:8000
```

---

## 整合方式

`app/main.py` 在啟動時共跑三個 asyncio task：

```python
web_config = uvicorn.Config(app=create_web_app(), host="0.0.0.0", port=8000, log_level="warning")
web_task = asyncio.create_task(uvicorn.Server(web_config).serve())
await asyncio.gather(price_task, screen_task, web_task)
```

uvicorn 與 Telegram Bot / Binance WebSocket 共用同一個 asyncio event loop，無需額外程序。

> **注意**：每月自動重啟排程（`monthly_restart_scheduler`）已停用，服務除非手動停止或主機重開機，否則持續運行。

---

## API Endpoints

| Method | Path | Query Params | 說明 |
|--------|------|-------------|------|
| GET | `/api/health` | — | 服務狀態 + 各策略幣種數 |
| GET | `/api/strategies/long_breakout` | `phase=TRACKING\|READY\|ALL` `page` `per_page` | 多頭策略清單 |
| GET | `/api/strategies/death_cross` | `phase=WATCHING\|ALERT\|ALL` `page` `per_page` | 死亡叉策略清單 |
| GET | `/api/strategies/fibonacci` | `direction=long\|short` `page` `per_page` | Fibonacci 觸發紀錄 |
| GET | `/api/chart/klines` | `symbol` `interval=4h` `limit=500` | K 線資料（proxy Binance fapi），回傳 `{time, open, high, low, close, volume}` |
| WS | `/ws` | — | 即時推送（狀態轉換事件） |

所有 GET endpoints 僅讀取 `models.py` 全域 dict，不寫入任何狀態。

---

## 前端功能

| 功能 | 說明 |
|------|------|
| 策略 Tab | Long Breakout / Death Cross / Fib Long / Fib Short |
| 狀態篩選 | 依策略顯示對應 phase 選項 |
| 幣種卡片 | 關鍵指標顯示，底部三個按鈕：K線圖 / TV / Binance |
| K線圖（KlineChartDialog）| 自訂 lightweight-charts 圖表（見下方） |
| TV（ChartDialog） | TradingView 免費 Widget（tv.js），symbol `BINANCE:{SYMBOL}.P`，EMA 15/30/45/60/200 |
| Binance 連結 | 直接跳轉 `binance.com/en/futures/{symbol}` |
| 分頁 | 每頁 10/25/50 筆 |
| 即時更新 | WebSocket push + setInterval 5s 輪詢雙保險 |
| RWD | 1 欄（mobile）/ 2 欄（tablet）/ 3 欄（desktop） |

---

## KlineChartDialog 功能說明

`KlineChartDialog.vue` 使用 `lightweight-charts@4.2.3`（TradingView 開源庫），主要功能：

| 功能 | 說明 |
|------|------|
| 時間軸切換 | Header 右側 `15m / 1h / 4h / 1d` 按鈕，切換後自動重新載入 |
| EMA 均線 | EMA 15（橘）/ 30（藍）/ 45（紫）/ 60（紅）/ 200（綠），右軸標籤隨十字線移動更新 |
| 成交量柱 | 底部 25% 區域，漲棒綠色半透明 / 跌棒紅色半透明 |
| OHLC 資訊列 | Header 下方顯示 O / H / L / C / 漲跌幅，隨十字線即時更新 |
| 起漲 K 高亮 | Long Breakout 策略：`trigger_time_ts` 對應的 4H K 棒顯示白色高亮，下方箭頭標記「起漲K」（1d 時間軸略過，因邊界不對齊） |
| 即時價格更新 | 每 5 秒自動 poll `/api/chart/klines?limit=1` 更新最後一根 K 棒；若新 K 棒開盤則完整重載（重算 EMA） |
| 時區 | 依瀏覽器本地時區顯示，時間格式 `YYYY-MM-DD HH:mm` |

---

## 啟動指南

### 開發模式

```bash
# Terminal 1：後端
make run             # 完整 Bot（Telegram + Binance WebSocket + FastAPI）
make api             # 僅 FastAPI（不需要 Telegram / Binance 金鑰）

# Terminal 2：前端 Vite dev server（proxy /api /ws → :8000）
make dev             # → http://localhost:5173
```

### 生產模式

```bash
make build           # 前端 build，輸出至 web/frontend/dist/
make run             # FastAPI 自動掛載 dist/ → http://localhost:8000
```

### Docker

Dockerfile 使用 **multi-stage build**，前端由 Node 18 自動 build，不需要手動執行 `npm run build`：

```
Stage 1 (node:18-slim)     → npm ci + npm run build → dist/
Stage 2 (python:3.11-slim) → 安裝 Python 依賴 + COPY dist/ from Stage 1
```

```bash
make up              # 完整 build（含前端）+ 背景啟動
make logs            # 查看即時 log
make down            # 停止
```

容器啟動後直接存取 `http://<host>:8000`。

> **注意**：`web/frontend/dist/` 不進 git（`.gitignore` 已設定），Docker build 時 Stage 1 會自動產生。

---

## 外部存取（Cloudflare Tunnel）

`docker-compose.yml` 已內建 `cloudflared` service，`docker compose up -d` 後自動建立公開 HTTPS 網址，**不需要開放路由器 port 或安裝任何本機軟體**。

```yaml
cloudflared:
  image: cloudflare/cloudflared:latest
  restart: unless-stopped
  command: tunnel --no-autoupdate --url http://binance-futures-monitor:8000
  depends_on:
    - binance-futures-monitor
```

**查詢分配到的網址（啟動後約 5 秒出現）：**
```bash
make logs-cf
# 找這行：INF | Your quick Tunnel has been created! Visit it at:
#          INF | https://xxxx-xxxx-xxxx.trycloudflare.com
```

**注意事項：**
- 臨時 URL：每次 `docker compose down` 再 `up`，或主機重開機後，URL 會重新分配
- 固定 URL 需要 Cloudflare 帳號 + 自有 domain，設定 Named Tunnel
- 儀表板目前無認證，URL 持有者可查看所有策略狀態；若需限制存取，可在 Nginx 前置層加 `auth_basic`

---

## VSCode 開發環境

安裝 **Vue - Official**（`Vue.volar`）擴充以獲得正確的 `.vue` 型別提示與語法高亮。

> `.vscode/extensions.json` 已設定建議安裝，開啟專案時 VSCode 會自動提示。

TypeScript 的 `.vue` 模組型別宣告在 `web/frontend/src/vite-env.d.ts`，未安裝 Volar 時也能消除 import 報錯。

### Volar / vue-tsc 已知限制

- `vue-tsc` 需 `>=2.x`（已安裝 `3.3.7`），舊版 `1.x` 會對 `<script setup>` 元件誤報 TS 1192 "has no default export"
- `vue/no-multiple-template-root`：Vue 3 Fragment（多根元素）合法，但 Volar 某些版本下仍顯示 error，不影響 build
- 以上兩者為 IDE 誤報，`vite build` 不受影響
