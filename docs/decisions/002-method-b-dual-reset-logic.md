# ADR-002: Method B 的雙軌重置邏輯

## 狀態
Accepted

## 背景

READY 狀態下出現新的強觸發 K 棒時，需要決定如何更新盤整邊界：

- **問題 1**：新觸發 K 的頂部（high）可能低於現有的 `consolidation_high`，若直接覆蓋會退縮突破目標
- **問題 2**：原觸發 K 的漲幅大小，影響「新觸發 K 是否真的更強」的判斷門檻

---

## 決策

Method B 採用雙軌邏輯，根據**原觸發 K 的實體漲幅**決定處理方式：

### Case 1：原觸發 K 漲幅 > 10%（`METHOD_B_RELAXED_THRESHOLD`）

```python
# 完整重置，頂部直接改為新觸發 K 的 high
consolidation_high = new_high
consolidation_low  = new_low
```

原觸發 K 已經非常強勢（漲幅 >10%），任何符合觸發條件的新 K 都可以取而代之，不需要額外的比較優勢。

### Case 2：原觸發 K 漲幅 ≤ 10%

```python
# Method B 重置：保留較高的頂部
consolidation_high = max(old_high, new_high)
consolidation_low  = new_low
```

需要新 K 的實體漲幅 > 原漲幅 × 1.10（有 10% 的比較優勢）才觸發。

---

## 理由

**Case 1 的設計**：原觸發 K 已達 >10% 漲幅，代表初始動能極強；此後任何再次符合觸發條件的 K 棒，都足夠資格重置計時，不需要額外的「優勢」比較。

**Case 2 的 `max(old_high, new_high)` 設計**：READY 狀態代表盤整高點已確立，若新觸發 K 的 high 較低，直接覆蓋會讓突破門檻退縮。保留 `max` 確保突破目標只進不退。

**`1.10` 比較門檻的設計**：防止「稍微強一點點的 K 棒」無限重置計時，避免策略永遠進不了 READY。設 10% 優勢門檻是為了確保新觸發 K 明顯更強，而不是僅略微超出。

---

## 後果

- **正面**：突破目標（`consolidation_high`）不會因 Method B 而退縮
- **正面**：漲幅極強的場景（>10%）不需要繁瑣比較，直接重置
- **負面**：`max(old_high, new_high)` 可能造成突破目標比新觸發 K 高出許多，進場門檻偏高
