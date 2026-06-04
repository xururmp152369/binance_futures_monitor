# [DIAG] 診斷計數器：Type 1 進場過濾漏斗

> **目的**：找出 V2 哪一層過濾條件吃掉最多訊號
> **加入時間**：2026-06-04
> **移除前提**：診斷完畢，確認門檻合理後移除

---

## 加入的程式碼位置

### 1. `app/strategy/long_breakout.py`

#### 新增區塊：模組頂部（`log = setup_logging()` 之後）

```python
# ─── [DIAG] 診斷計數器 ───────────────────────────────────────────────────────
_DIAG: dict[str, int] = { ... }

def get_diag_stats() -> dict[str, int]: ...
def print_diag_stats() -> None: ...
# ─── [DIAG END] ──────────────────────────────────────────────────────────────
```

#### 新增行：`on_new_15m_candle_long` 函數內

| 位置 | 計數器 | 說明 |
|------|--------|------|
| READY 判斷後，candle 解包前 | `_DIAG["ready_candles"] += 1` | READY 狀態的 15m K 總數 |
| `close > breakout_threshold` 通過後 | `_DIAG["price_breakout"] += 1` | 實體突破 0.5% |
| `vol_ratio >= BREAKOUT_VOLUME_MULT` 通過後 | `_DIAG["volume_ok"] += 1` | 量能 3.5× |
| SMA 200 過濾通過後（if 區塊外） | `_DIAG["sma_passed"] += 1` | SMA 200 技術面濾波 |
| `candle_body_ratio >= BREAKOUT_BODY_RATIO` 通過後 | `_DIAG["body_passed"] += 1` | 實體強度 60% |
| ATR 力度通過後 | `_DIAG["atr_passed"] += 1` | ATR 突破力度 30% |
| Taker Buy Ratio 通過後 | `_DIAG["taker_passed"] += 1` | Taker Buy Ratio 65% |
| 三層冷卻通過後 | `_DIAG["cooldown_passed"] += 1` | 三層冷卻全通過 |
| 發出訊號前 | `_DIAG["signal_fired"] += 1` | 最終訊號 |

### 2. `backtest/run.py`

#### 新增 import

```python
from app.strategy.long_breakout import print_diag_stats  # [DIAG]
```

#### 新增呼叫（`收集到 N 筆訊號` 之後）

```python
print_diag_stats()  # [DIAG]
```

---

## 移除方法

確認診斷完畢後，依序移除以下內容：

1. **`app/strategy/long_breakout.py`**
   - 移除 `# ─── [DIAG]` 到 `# ─── [DIAG END]` 之間的整個區塊（含空行）
   - 搜尋 `# [DIAG]` 找出所有計數行，共 9 行，逐一刪除

2. **`backtest/run.py`**
   - 移除 `from app.strategy.long_breakout import print_diag_stats  # [DIAG]`
   - 移除 `print_diag_stats()  # [DIAG]`

---

## 如何解讀報告

跑完回測後會在 console 看到：

```
===== [DIAG] Type 1 進場過濾漏斗 =====
  READY 狀態 15m K              12345
  ✓ 實體突破頂部 0.5%             890  (7.2%)
  ✓ 量能 ≥ 3.5×                  210  (23.6%)
  ✓ SMA 200 技術面濾波            180  (85.7%)
  ✓ 實體強度 ≥ 60%                 45  (25.0%)
  ✓ ATR 突破力度 ≥ 30%             30  (66.7%)
  ✓ Taker Buy Ratio ≥ 65%          0  (0.0%)   ← 這層把所有人殺掉了
  ✓ 三層冷卻通過                    0
  ★ 最終發出訊號                    0
==========================================
```

**判讀重點**：
- 哪一層之後數字驟降 → 那層門檻可能過嚴
- `sma_passed` 遠低於 `volume_ok` → SMA 200 過嚴，考慮關閉 `TREND_FILTER_ENABLED`
- `taker_passed = 0` → Taker Buy Ratio 65% 可能是主要殺手，考慮降低 `PUMP_CANDLE_TAKER_BUY_MIN`
- `body_passed` 遠低於 `sma_passed` → 實體強度 60% 過嚴，考慮降至 0.50
