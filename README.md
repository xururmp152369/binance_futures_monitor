此專案為監測幣安合約幣種的機器人，當監測到異常持倉或價格變化時，會透過TG機器人發送相關幣種訊息給使用者。

告警條件基本設置於config.py，說明如下
    必要符合項：
        1. 24hr 成交量 > QUOTE_VOLUME
        2. 最近1hr成交量 > 最近 48hr 的成交量 VOLUME_THRESHOLD 倍
        3. 只擷取漲的幣種，且 15m 內漲幅超過 PRICE_THRESHOLD %
        4. 1小時呈多頭趨勢
    選擇的監聽項：
        1. 持倉量 1hr 內正向漲幅 OI_THRESHOLD %
        2. 4小時呈多頭趨勢

power shell exection command
    1. pip list # 展示已安裝套件
    2. pip install package # 安裝指定套件，後面可選用 --upgrade # 保證最新版本
    3. py codeName # 執行程式

如何進入venv?
    cmd ==> venv\Scripts\activate