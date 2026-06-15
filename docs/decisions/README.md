# Architecture Decision Records

記錄此專案中**非顯而易見的設計決策**與背後的理由。新功能若有重要的設計取捨，應在此補充一筆。

| 編號 | 決策 | 狀態 |
|------|------|------|
| [001](001-abandonment-uses-body-not-wick.md) | 廢棄判斷使用實體而非影線 | Accepted |
| [002](002-method-b-dual-reset-logic.md) | Method B 的雙軌重置邏輯 | Accepted |
| [003](003-death-cross-48h-validity-window.md) | 死亡叉策略的 48H 時效性設計 | Accepted |
| [004](004-signal-a-no-volume-requirement.md) | 死亡叉信號 A 不設量能要求 | Accepted |
| [005](005-stop-loss-continuous-volume-lookback.md) | Type 1 止損回掃採用連續性而非全段最低點 | Accepted |
| [006](006-method-c-tracking-extension-override.md) | Method C：追蹤階段延伸超過門檻後允許更換基準K棒 | Accepted |
