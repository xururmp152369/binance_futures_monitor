# Binance 合約監控機器人

此專案為監測幣安（Binance）合約幣種的監控機器人。當偵測到異常的持倉或價格變化，或策略進場訊號出現時，會透過 Telegram 機器人發送通知給使用者。

## 主要功能

- 根據成交量、價格變化、持倉量與趨勢條件過濾潛在異常幣種（即時告警）
- 自動偵測「4h 拉漲後盤整突破 / 均線反彈」進場訊號（策略告警）
- 支援可配置的告警條件（在 `config.py` 中設定）
- 利用 Telegram Bot 發送告警通知

---

## 即時告警模組

**觸發時機**：只在 15 分鐘 K 線收盤時（00/15/30/45 分）檢查條件，避免中途誤判

必要條件（全部需符合）：

1. 24 小時成交量（以報價貨幣計）大於 QUOTE_VOLUME
2. **最新一根 15m K 成交量** > 48h 平均 15m 成交量 × VOLUME_THRESHOLD
3. 只擷取上漲幣種，且 15 分鐘內漲幅超過 PRICE_THRESHOLD (%)

參考項目（會顯示但不強制）：

1. 持倉量（Open Interest）在 1 小時內正向漲幅 > OI_THRESHOLD (%)
2. 1h / 4h K 線呈現多頭趨勢（EMA 15>30>45>60）

---

## 策略模組：拉漲盤整突破 / 均線反彈進場訊號

### 策略概述

以人工觀察 Binance 期貨「4h 拉漲後盤整，等待突破或回踩均線」的進場邏輯為基礎，系統化自動偵測狀態並發送 Telegram 告警。

### 狀態機流程

```
IDLE
 │ 觸發：4h K 棒為陽線，且 (high - low) / low >= PUMP_THRESHOLD%
 ▼
TRACKING（偵測到拉漲，追蹤延伸走勢）
 │ 若後續 4h K 棒創新高 → 更新盤整頂部，重置盤整計時（延伸中）
 │ 廢棄條件：任何 4h/1h K 棒低點 or 即時價格 < 拉漲 K 低點 → 回 IDLE
 │ 進展條件：停止創新高後，持續盤整 >= CONSOLIDATION_MIN_HOURS
 ▼
READY（開始監控進場訊號）
 │ 廢棄條件：同上
 │ 每根 15m 收盤 → Type 1 檢查（帶量突破盤整頂部）
 └ 每根 1h 收盤  → Type 2 檢查（回踩 4h EMA + 有效收針 + 盈虧比）
```

### 盤整區間定義

| 欄位 | 說明 |
|------|------|
| `consolidation_low` | 拉漲 K 棒最低價（固定不變，亦是廢棄門檻） |
| `consolidation_high` | 後續 4h K 棒最高點的最大值（每次創新高時更新，同時重置計時） |

盤整計時從「最後一次創新高的 K 棒開盤時間」起算，達到 `CONSOLIDATION_MIN_HOURS` 後才進入 READY。

### 進場訊號細節

**Type 1（帶量突破）**
- 15m K 棒收盤 > 盤整頂部
- 15m 成交量 > 前 192 根平均 × BREAKOUT_VOLUME_MULT 倍
- 止損 = 該 15m K 棒最低價
- 目標 = 收盤 + (收盤 - 止損) × 1.5（1.5R）

**Type 2（均線反彈）**
- 1h K 棒最低價 ≤ 任一 4h EMA（15/30/45/60）× (1 + EMA_TOUCH_THRESHOLD%)
- 多頭趨勢確認：開盤價 > 觸碰的 EMA 值（過濾空頭假訊號）
- 有效收針：close > low × (1 + WICK_THRESHOLD%)
- 盈虧比：(盤整頂部 - close) / (close - 拉漲 K 低點) ≥ STRATEGY_RR_MIN
- 止損 = 拉漲 K 棒最低價（固定失效線）
- 目標 = 收盤 + (收盤 - 止損) × 1.5（1.5R）

### Telegram 告警範例

**Type 1 帶量突破**
```
🎯 策略訊號 — Type 1 帶量突破
幣種：BTCUSDT.P | 2026/04/20 14:15

📅 拉漲 K 棒：2026/04/18 00:00 | 最高 65,000 | 最低 60,000
⏰ 突破 K 棒：2026/04/20 14:00（15m）

💰 收盤：64,500
🔼 突破頂部：64,200 (+0.47%)
📊 量能：4.2× 均值

🔴 止損：64,100（本 K 低點，-0.62%）
🟢 目標 (1.5R)：64,800

📦 盤整區間：60,000 ~ 64,200（幅度 7.0%）
```

**Type 2 均線反彈**
```
🎯 策略訊號 — Type 2 均線反彈
幣種：BTCUSDT.P | 2026/04/20 16:00

📅 拉漲 K 棒：2026/04/18 00:00 | 最高 65,000 | 最低 60,000
⏰ 觸發 K 棒：2026/04/20 16:00（1h）

💰 收盤：62,500
📉 觸碰：4h EMA45 = 62,200
🕯️ 收針幅度：2.5%
📊 盈虧比 (至頂)：1.36:1

🔴 止損：60,000（拉漲 K 低點，-4.00%）
🟢 目標 (1.5R)：65,500
📦 盤整頂部參考：64,200 (+2.72%)
```

---

## 設定參數（位於 `config.py`）

### 即時告警參數

| 參數 | 說明 |
|------|------|
| `QUOTE_VOLUME` | 24h 成交量最低門檻（報價貨幣，如 USDT） |
| `VOLUME_THRESHOLD` | 最新 15m K 成交量相對 48h 平均的倍數門檻 |
| `PRICE_THRESHOLD` | 15m 漲幅門檻 (%) |
| `OI_THRESHOLD` | 1h 持倉量增幅門檻 (%)（參考項目） |
| `ALERT_COOLDOWN` | 同一幣種告警冷卻秒數 |

### 策略參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `PUMP_THRESHOLD` | 8 | 4h 拉漲 K 棒：陽線且 (high-low)/low >= 8% |
| `CONSOLIDATION_MIN_HOURS` | 12 | 停止創新高後，需持續盤整的最低時數 |
| `BREAKOUT_VOLUME_MULT` | 3 | Type 1 突破所需量能倍數（相對 192 根平均） |
| `EMA_TOUCH_THRESHOLD` | 0.5 | Type 2 EMA 觸碰容忍距離 (%) |
| `WICK_THRESHOLD` | 2 | Type 2 有效收針：close > low × (1 + N%) |
| `STRATEGY_RR_MIN` | 1.0 | Type 2 最低盈虧比（至盤整頂部） |
| `STRATEGY_COOLDOWN` | 14400 | 策略告警冷卻秒數（預設 4 小時） |

---

## 執行說明（Windows PowerShell / CMD）

- 檢查已安裝套件：

```powershell
pip list
```

- 安裝指定套件（可加 --upgrade 保證為最新版本）：

```powershell
pip install package-name
pip install -r requirements.txt --upgrade
```

- 執行 Python 程式（範例）：

```powershell
py -m app.main
# 或
python -m app.main
```

## 進入虛擬環境（Windows）

- CMD：

```cmd
venv\Scripts\activate
```

- PowerShell（若遭到執行策略阻擋，可能需要先允許執行）：

```powershell
.\venv\Scripts\Activate.ps1
# 或使用:
venv\Scripts\activate
```

---

## 使用 Docker / Docker Compose

此專案已提供 `Dockerfile` 與 `docker-compose.yml`，可用 Docker 方式常駐執行。

### 1) 準備環境變數（.env）

請在專案根目錄建立 `.env`，至少包含：

```env
BOT_TOKEN=你的_telegram_bot_token
CHAT_ID=你的_chat_id
```

（其餘策略參數請至 `app/setting/config.py` 調整）

### 2) 常用指令

- Build 映像檔：

```powershell
docker compose build
```

- 背景啟動：

```powershell
docker compose up -d --build
```

- 查看 logs：

```powershell
docker compose logs -f --tail=200
```

- 重新啟動服務：

```powershell
docker compose restart
```

- 停止並移除容器：

```powershell
docker compose down
```

### 3) 補充說明

- 容器啟動指令為：`python -m app.main`
- Compose 服務使用 `restart: unless-stopped`，適合長時間監控
