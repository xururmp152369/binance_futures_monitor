# ADR-001: 廢棄判斷使用實體而非影線

## 狀態
Accepted

## 背景

多頭策略需要判斷「K 棒是否跌破盤整底部（consolidation_low）」來廢棄當前追蹤狀態。K 棒的低點有兩種選擇：

- **low**（含下影線）：K 棒的最低成交價
- **min(open, close)**（實體低點）：K 棒收盤後的實體邊界

---

## 決策

廢棄判斷使用**實體低點** `min(open, close)`，而不是 `low`。

```python
# analysis_utils.py
def body_barrier_price(open_: float, close: float, direction: Direction) -> float:
    return min(open_, close) if direction == Direction.LONG else max(open_, close)

def is_invalidated(open_: float, close: float, st: dict, direction: Direction) -> bool:
    barrier = body_barrier_price(open_, close, direction)
    return barrier < st["consolidation_low"]
```

---

## 理由

**下影線代表「被市場拒絕的低點」**，而不是真正的賣壓突破。

當 K 棒出現下影線刺穿底部但收盤在底部上方時，代表空頭嘗試突破但被多頭承接拉回，這是盤整支撐有效的訊號，而不是廢棄的訊號。

只有實體收破底部，才代表市場參與者在收盤前接受了該價位，空頭力量真正勝出。

**即時廢棄（markPrice）用的是即時價格**，邏輯一樣：markPrice 跌破底部才重置，中途的瞬間跌深只要最終拉回就無效。

---

## 後果

- **正面**：減少因為長下影線 K 棒造成的假廢棄，保留有效盤整更長
- **負面**：若市場真的已崩跌但收盤勉強守住，可能延遲廢棄一根 4h K 棒的時間
