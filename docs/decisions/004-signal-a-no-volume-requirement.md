# ADR-004: 死亡叉信號 A 不設量能要求

## 狀態
Accepted

## 背景

死亡叉策略的兩個 1H 進場信號在量能處理上不一致：

- **信號 B（吞噬型態）**：明確要求 `volume > prev_volume × 1.5`，量能是觸發條件之一
- **信號 A（拒絕蠟燭）**：只顯示 `vol_ratio`（相對前根量能倍數），但不用於判斷是否觸發

---

## 決策

信號 A 不設量能觸發要求，`vol_ratio` 僅作為訊號輸出的參考資訊。

```python
def _check_signal_a(symbol: str, candle: tuple, ema200_1h: float) -> dict | None:
    # 只判斷 4 個價格條件，無量能檢查
    if not (
        high > ema200_1h
        and close < ema200_1h
        and (ema200_1h - close) / ema200_1h >= DC_REJECTION_BODY_PCT
        and close < open_
    ):
        return None
    vol_ratio = _prev_vol_ratio(symbol, vol)  # 僅供顯示
    return {"signal_type": "rejection", "vol_ratio": vol_ratio}
```

---

## 理由

**拒絕蠟燭的本質是「空頭力量展現在形態上」**：K 棒上影線刺穿 EMA200 但收盤壓回，代表空頭在 EMA200 處直接壓制了試圖突破的多頭。這個壓制行為本身就包含了空頭力量的訊息，不需要額外的量能確認。

**信號 B 需要量能是因為吞噬型態依賴量能確認**：吞噬型態（跳空高開後收低）在沒有帶量的情況下可能只是低流動性的噪音，帶量才代表空頭確實主動賣壓。

**加量能要求反而可能過濾掉有效信號**：在低波動期間，EMA200 的拒絕蠟燭即使量能不大，仍然代表價格受到均線壓制，是有效的空頭信號。

---

## 後果

- **正面**：信號 A 的觸發更靈敏，不會因為低量而錯失 EMA200 壓制的有效訊號
- **負面**：在量能極低的市場環境（如深夜盤）可能觸發較多噪音信號
- **監控方式**：`vol_ratio` 仍輸出在訊號 dict 中，Telegram 訊息可以讓使用者自行判斷當時的量能狀況
