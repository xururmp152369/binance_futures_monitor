# Binance 合約監控機器人

此專案為監測幣安（Binance）合約幣種的監控機器人。當偵測到異常的持倉或價格變化時，會透過 Telegram 機器人發送相關幣種的通知給使用者。

## 主要功能

- 根據成交量、價格變化、持倉量與趨勢條件過濾潛在異常幣種
- 支援可配置的告警條件（在 config.py 中設定）
- 利用 Telegram Bot 發送告警通知

## 告警條件（基本設定）

**觸發時機**：只在 15 分鐘 K 線收盤時（00/15/30/45 分）檢查條件，避免中途誤判

必要條件（全部需符合）：

1. 24 小時成交量（以報價貨幣計）大於 QUOTE_VOLUME
2. **最新一根 15m K 成交量** > 48h 平均 15m 成交量 × VOLUME_THRESHOLD
3. 只擷取上漲幣種，且 15 分鐘內漲幅超過 PRICE_THRESHOLD (%)

參考項目（會顯示但不強制）：

1. 持倉量（Open Interest）在 1 小時內正向漲幅 > OI_THRESHOLD (%)
2. 1h / 4h K 線呈現多頭趨勢（EMA 15>30>45>60）

## 常見設定參數（位於 config.py）

- **QUOTE_VOLUME**: 24 小時成交量最低門檻（以報價貨幣計，例：USDT）
- **VOLUME_THRESHOLD**: 判斷「最新一根 15m K 成交量」是否顯著高於「過去 48 小時平均 15m 成交量」的倍數（例如 6.0 表示 6 倍）
- **PRICE_THRESHOLD**: 15 分鐘內的價格漲幅百分比門檻（例如 2.0 表示 2.0%）
- **OI_THRESHOLD**: 1 小時內持倉量增幅百分比（參考項目）
- **ALERT_COOLDOWN**: 同一幣種告警冷卻時間（秒），基於 15m K 收盤時間間隔（例如 3600 秒 = 4 根 15m K）

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
docker compose up -d
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
