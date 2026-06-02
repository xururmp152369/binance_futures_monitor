# 文件索引

## 架構

- [architecture/overview.md](architecture/overview.md) — 系統全貌、資料流、模組職責、啟動流程

## 策略規格（Specs）

- [specs/long_breakout.md](specs/long_breakout.md) — Type 1 多頭盤整突破（含 Mermaid 狀態圖）
- [specs/death_cross_short.md](specs/death_cross_short.md) — Type 3 死亡叉制空（含 Mermaid 狀態圖）

## 設計決策（ADR）

- [decisions/README.md](decisions/README.md) — ADR 索引
- [decisions/001](decisions/001-abandonment-uses-body-not-wick.md) — 廢棄判斷使用實體而非影線
- [decisions/002](decisions/002-method-b-dual-reset-logic.md) — Method B 的雙軌重置邏輯
- [decisions/003](decisions/003-death-cross-48h-validity-window.md) — 死亡叉策略的 48H 時效性設計
- [decisions/004](decisions/004-signal-a-no-volume-requirement.md) — 死亡叉信號 A 不設量能要求
- [decisions/005](decisions/005-stop-loss-continuous-volume-lookback.md) — Type 1 止損回掃採用連續性而非全段最低點
