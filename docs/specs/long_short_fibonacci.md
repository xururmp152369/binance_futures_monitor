# Fibonacci 篩選器 - 完整規格書

> **交易所**：Binance（期貨）  
> **策略代號**：`fibonacci_long`（多單）、`fibonacci_short`（空單）  
> **方向**：雙向（可分開啟用）  
> **K 線週期**：參數可設置（預設 1h）  
> **版本**：V1.1

---

## 策略概述

一個基於 Fibonacci 延伸層級的自動開單策略，通過識別「底底高」（多單）和「頂頂低」（空單）的形態，結合 Fib 1.73 層級的確認，在第 8 根 K 棒後發出進場訊號。

### 核心特性

- ✅ 自動形態識別（無需人工判斷）
- ✅ 多層次 K 棒確認（bar5、bar8 用 wick 確認）
- ✅ 動態無效化評估（順序推移）
- ✅ 自動止損 / 止盈計算
- ✅ 多幣併發掃描（可自訂）
- ✅ 多空獨立啟用（`fibonacci_long` / `fibonacci_short` 分開設置）

---

## 一、Fibonacci 形態識別

### 1.1 多單形態（底底高 / Higher Low）

**基礎條件**（必須同時滿足）

```
條件 1：相鄰兩根 K 棒（barA、barB）
       barA 是第 i 根 K 棒
       barB 是第 i+1 根 K 棒

條件 2：收盤在 EMA 上方
       barA.close >= EMA_value
       barB.close >= EMA_value

條件 3：形成更高的底（底底高）
       barB.low >= barA.low

條件 4：後續 K 棒未破底
       從 barB 之後的所有 K 棒中
       沒有任何 K 棒的 close < barA.low
       （若有，該形態無效，重新評估）
```

**持續確認條件**（bar5 和 bar8）

```
bar5（barA+4）：
  - 影線需觸及 Fib_1.73 層級（用 high 判斷）
  - bar5.high >= Fib_1.73_value

bar8（barA+7）：
  - 影線需觸及 Fib_1.73 層級（用 high 判斷）
  - bar8.high >= Fib_1.73_value

若任一不符合 → 該形態無效，重新評估
```

---

### 1.2 空單形態（頂頂低 / Lower High）

**基礎條件**（必須同時滿足）

```
條件 1：相鄰兩根 K 棒（barA、barB）
       barA 是第 i 根 K 棒
       barB 是第 i+1 根 K 棒

條件 2：收盤在 EMA 下方
       barA.close <= EMA_value
       barB.close <= EMA_value

條件 3：形成更低的頂（頂頂低）
       barB.high <= barA.high

條件 4：後續 K 棒未破頂
       從 barB 之後的所有 K 棒中
       沒有任何 K 棒的 close > barA.high
       （若有，該形態無效，重新評估）
```

**持續確認條件**（bar5 和 bar8）

```
bar5（barA+4）：
  - 影線需觸及 Fib_1.73 層級（用 low 判斷）
  - bar5.low <= Fib_1.73_value

bar8（barA+7）：
  - 影線需觸及 Fib_1.73 層級（用 low 判斷）
  - bar8.low <= Fib_1.73_value

若任一不符合 → 該形態無效，重新評估
```

---

## <span style="color: red;">**二、無效化重評估邏輯**</span>

<span style="color: red;">

### 狀態轉移流程

當某根 K 棒不符合無效化條件時，該 K 棒自動成為新的起點，順序推移重新評估：

```
初始形態：
K1(barA) → K2(barB) ✓ 符合「底底高」
         ↓
檢查 K3：
  - 多單：close < barA.low？
  - 若是 → 無效化

無效後的重評估：
K3(新 barA) → K4(新 barB) → 重新檢查「底底高」條件
  - K4.low >= K3.low？
  - K4.close >= EMA?
  - 若都符合 → 開始新的 Fib 計算
  - 若不符 → 繼續推移

持續流程：
若 K4 也無效 → K4(新 barA) → K5(新 barB) ...
若 K5 符合 → 開始追蹤 K6、K7（bar5、bar8）...
```

### 實例說明

```
多單場景：
K1: open=100, close=103, low=99, high=105
K2: open=103, close=104, low=100, high=106  ✓ 符合「底底高」(100 >= 99)

K3: open=104, close=98, low=97, high=108    ✗ 無效化 (98 < 99)
↓ K3 重新成為 barA

K3(新): open=104, close=98, low=97, high=108
K4:     open=98, close=101, low=95, high=102
  檢查：K4.low (95) >= K3.low (97)？否 → 不符合「底底高」

K4 也無效 ↓ K4 重新成為 barA

K4(新): open=98, close=101, low=95, high=102
K5:     open=101, close=105, low=94, high=107
  檢查：K5.low (94) >= K4.low (95)？否 → 不符合

K5 也無效 ↓ K5 重新成為 barA

...繼續推移，直到找到符合「底底高」的形態
```

</span>

---

## 三、Fibonacci 計算公式

### 3.1 多單（底底高）

```
基準點：
  fib0 = min(barA.close 實體低點, barB.close 實體低點)
  fib1 = max(barA.close 實體高點, barB.close 實體高點)
  fib_range = fib1 - fib0

Fib 層級計算：
  Fib_X = fib0 + (fib_range × X)

關鍵層級：
  Fib_1.73  = fib0 + (fib_range × 1.73)   ← bar5、bar8 影線確認層級
  Fib_6.92  = fib0 + (fib_range × 6.92)   ← TP1 止盈層級
  Fib_12.11 = fib0 + (fib_range × 12.11)  ← 次要止盈（可選）
  其他層級  = Fib_0, Fib_1, Fib_1.46, Fib_17.3 ... Fib_105.53

範例計算：
  barA: open=100, close=103, low=99, high=105
  barB: open=103, close=104, low=100, high=106
  
  fib0 = min(103, 104) = 103（實體低點）
  fib1 = max(103, 104) = 104（實體高點）
  fib_range = 104 - 103 = 1
  
  Fib_1.73 = 103 + (1 × 1.73) = 104.73
  Fib_6.92 = 103 + (1 × 6.92) = 109.92
```

### 3.2 空單（頂頂低）

```
基準點：
  fib0 = max(barA.close 實體高點, barB.close 實體高點)
  fib1 = min(barA.close 實體低點, barB.close 實體低點)
  fib_range = fib0 - fib1  （注意：高 - 低）

Fib 層級計算：
  Fib_X = fib0 - (fib_range × X)  （往下延伸）

關鍵層級：
  Fib_1.73  = fib0 - (fib_range × 1.73)   ← bar5、bar8 影線確認層級
  Fib_6.92  = fib0 - (fib_range × 6.92)   ← TP1 止盈層級

範例計算：
  barA: open=105, close=100, low=95, high=105
  barB: open=100, close=99, low=98, high=104
  
  fib0 = max(100, 99) = 100（實體高點）
  fib1 = min(100, 99) = 99（實體低點）
  fib_range = 100 - 99 = 1
  
  Fib_1.73 = 100 - (1 × 1.73) = 98.27
  Fib_6.92 = 100 - (1 × 6.92) = 93.08
```

---

## 四、止損（SL）計算

### 多單止損

```
取以下三根 K 棒的最低點：
  SL = min(barA.low, barB.low, barA+7.low)

說明：
  - barA.low：觸發 K 棒的最低點
  - barB.low：確認 K 棒的最低點
  - barA+7.low（bar8）：最後確認 K 棒的最低點

邏輯：
  取三根關鍵 K 棒中的極值，作為防守線
  確保 SL 在有實際支撑的位置

範例：
  barA.low = 99
  barB.low = 100
  bar8.low = 98
  
  SL = min(99, 100, 98) = 98
```

### 空單止損

```
取以下三根 K 棒的最高點：
  SL = max(barA.high, barB.high, barA+7.high)

說明：
  - barA.high：觸發 K 棒的最高點
  - barB.high：確認 K 棒的確認高點
  - barA+7.high（bar8）：最後確認 K 棒的最高點

邏輯：
  取三根關鍵 K 棒中的極值，作為防守線

範例：
  barA.high = 105
  barB.high = 106
  bar8.high = 107
  
  SL = max(105, 106, 107) = 107
```

---

## 五、開單觸發條件

### <span style="color: red;">**掃描觸發時機**</span>

<span style="color: red;">

```
掃描週期：FIB_K_INTERVAL（參數設置，預設 1h）
掃描時機：K 棒閉合後立即掃描
掃描內容：
  - 固定窗口掃描：barA 固定在 klines[-9]，bar9 固定在 klines[-1]（當前 K 棒）
  - 每根 K 棒閉合後重新從 buffer 掃描，自然實現無效化重評估

開單觸發條件：
  當 bar9 確認時，同時滿足以下條件才開單：
  1. bar9 已形成（K 棒數量 >= barA + 8）
  2. 未停損：從 bar5 到 bar9，沒有任何 K 棒碰到 SL
  3. 未到 TP1：從 bar5 到 bar9，沒有任何 K 棒碰到 Fib_6.92
  4. 方向正確：
     多單 → 當前市價 > SL
     空單 → 當前市價 < SL
  5. 止損距離不超過 FIB_MAX_SL_PCT（預設 5%）：
     多單 → (market_price - SL) / market_price * 100 < 5%
     空單 → (SL - market_price) / market_price * 100 < 5%
     （超過 5% 的形態風險過大，不發訊號）
  6. 該 bar9 時間戳未重複觸發（防止同一 bar9 重複發訊號）
  7. 保證金充足（由用戶自行設置的開倉邏輯決定）
```

### **開單後重置（防止連續開單）**

```
觸發條件全部成立並送出開單後，立即執行重置：
  1. 將 bar9 重新定義為新的 barA
  2. 清除目前的形態追蹤狀態（barB、bar5、bar8、Fib 層級、SL、TP1）
  3. 從新 barA 開始重新執行完整監測流程
     （需重新找 barB、確認 bar5、bar8，再等 bar9）

目的：
  - 避免同一形態持續符合條件而連續開單
  - 強制要求開單後必須重新建立新的 Fib 形態才能再次進場
  - 新 barA 本身仍需符合「底底高/頂頂低 + EMA 條件」才能成為有效起點

流程示意：
  正常流程：K1(barA) → K2(barB) → ... → K9(bar9) → 開單
                                                          ↓
  開單後重置：K9 重新成為 barA → K10(新 barB)？ → 重新監測
```

</span>

---

## <span style="color: red;">**六、邊界情況與定義**</span>

<span style="color: red;">

### 邊界值定義（全部採用包含）

```
EMA 邊界：
  多單：close >= EMA_value（大於等於）
  空單：close <= EMA_value（小於等於）

Fib 層級邊界（影線判斷）：
  多單：high >= Fib_value（大於等於）
  空單：low <= Fib_value（小於等於）

底高/頂低比較：
  多單：barB.low >= barA.low（大於等於）
  空單：barB.high <= barA.high（小於等於）

無效化條件：
  多單：close < barA.low（嚴格小於）
  空單：close > barA.high（嚴格大於）
```

### 浮點數精度處理

```
問題：
  由於浮點數計算，Fib 層級值可能有微小誤差
  如：計算結果 1.730000001 vs 1.73

解決方案：
  設置 EPSILON = 1e-9，允許微小偏差
  多單：high >= Fib_value - EPSILON 視為通過
  空單：low <= Fib_value + EPSILON 視為通過

應用：
  所有邊界判斷都加上 epsilon 容差
```

</span>

---

## 七、訊號輸出格式

### 開單訊號

```json
{
  "type": "fibonacci_long",
  "direction": "LONG",
  "symbol": "ETHUSDT",
  "close": 2850.50,
  "stop_loss": 2840.25,
  "take_profit_1": 2920.75,
  "bar_a_time": 1700000000,
  "bar_b_time": 1700000900,
  "bar_9_time": 1700008900,
  "fib_0": 2845.00,
  "fib_1": 2855.00,
  "fib_range": 10.00,
  "fib_1_73": 2862.30,
  "fib_6_92": 2914.20,
  "interval": "1h",
  "scan_timestamp": 1700009000
}
```

---

## 八、回測系統

### 執行指令

```bash
# 只跑多單（fibonacci_long）
python backtest/run.py --strategy fibonacci_long

# 只跑空單（fibonacci_short）
python backtest/run.py --strategy fibonacci_short

# 指定天數（預設 30 天）
python backtest/run.py --strategy fibonacci_long --days 60

# 指定回測區間
python backtest/run.py --strategy fibonacci_long --start 2025-06-03 --end 2025-06-06

# 多空 + 其他策略一起跑
python backtest/run.py --strategy all

# 帳戶模式（依帳戶止盈策略計算 USDT 損益）
python backtest/run.py --strategy fibonacci_long --account 帳號名稱
```

### 快取資料

| 區間 | date_key | 說明 |
|------|---------|------|
| 2025-06-03 ~ 2025-06-06 | `20250606` | 已有快取，可直接使用 |

> 指定區間模式快取永久有效（歷史資料不變），重新執行同一區間不需重新下載。

### 與正式系統一致

回測直接呼叫 `app/strategy/long_short_fibonacci.py`，讀取 `app/setting/config.py` 的參數，修改 `FIB_K_INTERVAL`、`FIB_EMA_PERIOD` 等參數會同時影響正式系統與回測結果。

---

## 九、關鍵參數設置

| 參數 | 預設值 | 說明 | 單位 |
|------|--------|------|------|
| **FIB_K_INTERVAL** | `"1h"` | K 線週期（`"15m"` / `"1h"` / `"4h"`） | 字串 |
| **FIB_EMA_PERIOD** | 55 | 指數移動平均線週期 | K 棒數 |
| **FIB_CONFIRM_LEVEL** | 1.73 | 確認層級（bar5、bar8 影線） | Fib 倍數 |
| **FIB_TP1_LEVEL** | 6.92 | TP1 止盈層級 | Fib 倍數 |
| **FIB_MAX_SL_PCT** | 5.0 | 止損距離上限，超過此值不發訊號 | % |

### 策略代號（用於 PRD_STRATEGY / DEV_STRATEGY）

| 代號 | 說明 |
|------|------|
| `fibonacci_long` | 只啟用多單方向 |
| `fibonacci_short` | 只啟用空單方向 |

---

## <span style="color: red;">**十、實裝檢查清單**</span>

<span style="color: red;">

### 邏輯實裝檢查

- [ ] **形態識別**
  - [ ] 多單：barB.low >= barA.low（大於等於）
  - [ ] 多單：barA.close >= EMA && barB.close >= EMA
  - [ ] 空單：barB.high <= barA.high（小於等於）
  - [ ] 空單：barA.close <= EMA && barB.close <= EMA

- [ ] **無效化監控**
  - [ ] 多單：從 barB 開始逐根檢查 close < barA.low
  - [ ] 空單：從 barB 開始逐根檢查 close > barA.high
  - [ ] 邊界值全部用「包含」（>=、<=）

- [ ] **Fib 層級計算**
  - [ ] 多單：fib0 = min(barA、barB 實體低點)
  - [ ] 多單：Fib_1.73 計算正確
  - [ ] 多單：bar5.high >= Fib_1.73（影線判斷）
  - [ ] 多單：bar8.high >= Fib_1.73（影線判斷）
  - [ ] 空單：fib0 = max(barA、barB 實體高點)
  - [ ] 空單：Fib_1.73 計算正確
  - [ ] 空單：bar5.low <= Fib_1.73（影線判斷）
  - [ ] 空單：bar8.low <= Fib_1.73（影線判斷）

- [ ] **止損計算**
  - [ ] 多單：SL = min(barA.low, barB.low, bar8.low)
  - [ ] 空單：SL = max(barA.high, barB.high, bar8.high)

### 開單執行檢查

- [ ] **掃描觸發**
  - [ ] 掃描時機：FIB_K_INTERVAL K 棒閉合後立即掃描
  - [ ] 固定窗口：barA=klines[-9]，bar9=klines[-1]

- [ ] **開單條件**
  - [ ] bar9 已形成
  - [ ] 未停損（SL 未被碰）
  - [ ] 未到 TP1（Fib_6.92 未被碰）
  - [ ] 方向正確（多：市價 > SL；空：市價 < SL）
  - [ ] 止損距離 < FIB_MAX_SL_PCT（預設 5%），超過不發訊號
  - [ ] 同一 bar9 時間戳不重複觸發

- [ ] **開單後重置**
  - [ ] 開單成功後，將 bar9 重新設為新的 barA
  - [ ] 清除既有形態狀態（barB、Fib 層級、SL、TP1）
  - [ ] 從新 barA 重新執行完整監測流程

### 監控與統計

- [ ] 每日開單數量統計
- [ ] 勝率計算（到達 TP1 的比例）
- [ ] 平均 RR（實際止盈 / 止損距離）
- [ ] 形態無效率統計

</span>

---

## 附錄：術語表

| 術語 | 定義 |
|------|------|
| **barA** | 形態的第一根 K 棒（觸發 K） |
| **barB** | 形態的第二根 K 棒（確認 K） |
| **bar5** | barA+4，第 5 根 K 棒（Fib 影線確認 1） |
| **bar8** | barA+7，第 8 根 K 棒（Fib 影線確認 2） |
| **bar9** | barA+8，第 9 根 K 棒（開單判斷點） |
| **底底高** | barB.low >= barA.low（多單形態） |
| **頂頂低** | barB.high <= barA.high（空單形態） |
| **無效化** | 形態不符合條件，停止追蹤 |
| **重評估** | 固定窗口掃描天然實現：每根 K 棒閉合重新判斷 barA 位置 |
| **Fib_1.73** | 確認層級，bar5、bar8 影線必須觸及 |
| **Fib_6.92** | 止盈層級，TP1 設置位置 |
| **SL** | 止損價格，根據三根 K 棒計算 |
| **TP1** | 第一止盈點，Fib_6.92 層級 |
| **市價進場** | 使用市場成交價立即進場 |

---

**規格書完成。請確認邏輯是否完整準確，有無需要修改的地方。**
