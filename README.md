# Binance 合約監控機器人

此專案為監測幣安（Binance）合約幣種的監控機器人。當偵測到異常的持倉或價格變化時，會透過 Telegram 機器人發送相關幣種的通知給使用者。

## 主要功能
- 根據成交量、價格變化、持倉量與趨勢條件過濾潛在異常幣種
- 支援可配置的告警條件（在 config.py 中設定）
- 利用 Telegram Bot 發送告警通知

## 告警條件（基本設定）
必要條件（全部需符合）：
1. 24 小時成交量（以報價貨幣計）大於 QUOTE_VOLUME  
2. 最近 1 小時成交量 > 最近 48 小時平均成交量 × VOLUME_THRESHOLD  
3. 只擷取上漲幣種，且 15 分鐘內漲幅超過 PRICE_THRESHOLD (%)  
4. 1 小時呈現多頭趨勢（由程式內的趨勢判斷邏輯決定）

可選的監聽項目（依 config 設定是否啟用）：
1. 持倉量（Open Interest）在 1 小時內正向漲幅 > OI_THRESHOLD (%)  
2. 4 小時呈現多頭趨勢

## 常見設定參數（位於 config.py）
- QUOTE_VOLUME: 24 小時成交量最低門檻（以報價貨幣計，例：USDT）  
- VOLUME_THRESHOLD: 判斷 1 小時成交量是否顯著高於過去 48 小時平均的倍數（例如 2.0 表示 2 倍）  
- PRICE_THRESHOLD: 15 分鐘內的價格漲幅百分比門檻（例如 1.5 表示 1.5%）  
- OI_THRESHOLD: 1 小時內持倉量增幅百分比（可選）  

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
py monitor.py
# 或
python monitor.py
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
