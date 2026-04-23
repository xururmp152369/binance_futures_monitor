import json
from pathlib import Path

_DATA_DIR = Path(__file__).parent.parent.parent / "data" / "users"

CONFIG_TEMPLATE_TEXT = """\
📋 *個人設定說明*

請複製下方 JSON，填寫後直接傳給我：

*欄位說明：*
• `API_KEY` / `SECRET_KEY`：Binance API 金鑰
• `STRATEGY`：觸發自動開單的策略，可填 "TYPE1"（帶量突破）、"TYPE2"（均線反彈），或兩者皆填
• `RISK_TYPE`：風險計算方式
  ‣ `0`：固定投入金額（RISK\\_AMOUNT × 槓桿 USDT）
  ‣ `1`：固定損失金額（依止損比例換算手數，手數 = RISK\\_AMOUNT / (入場價 × 止損比例)）
• `RISK_AMOUNT`：投入或損失金額（USDT）
• `RISK_LEVERAGE`：槓桿倍數
• `TP_STRATEGY`：止盈策略，至少 1 組、至多 3 組，PERCENT 總計不超過 100
  ‣ `RR_RATIO`：止盈盈虧比（1 = 1R，1.5 = 1.5R）
  ‣ `PERCENT`：達到該盈虧比時平倉的部位比例 (%)，剩餘部位由使用者自行決定
• `ORDER_LIMIT`：同時持有部位數上限
• `ADD_SAME_SYMBOL`：同幣種已有倉位時，是否再次開單（加倉）
• `SYMBOL_BLACKLIST`：不自動開單的幣種清單，空陣列表示不限制
• `ENABLED`：是否啟用自動開單

```
{
    "API_KEY": "",
    "SECRET_KEY": "",
    "STRATEGY": ["TYPE1", "TYPE2"],
    "RISK_TYPE": 0,
    "RISK_AMOUNT": 0.1,
    "RISK_LEVERAGE": 20,
    "TP_STRATEGY": [
        { "RR_RATIO": 1.5, "PERCENT": 50 }
    ],
    "ORDER_LIMIT": 10,
    "ADD_SAME_SYMBOL": false,
    "SYMBOL_BLACKLIST": [],
    "ENABLED": true
}
```\
"""

_VALID_STRATEGIES = {"TYPE1", "TYPE2"}


def validate_config(data: dict) -> tuple[bool, list[str]]:
    """驗證使用者設定 dict，回傳 (是否通過, 錯誤訊息列表)。"""
    errors = []

    for key in ("API_KEY", "SECRET_KEY"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f"`{key}` 必須為非空字串")

    strategy = data.get("STRATEGY")
    if not isinstance(strategy, list) or not strategy:
        errors.append("`STRATEGY` 必須為非空陣列，如 [\"TYPE1\", \"TYPE2\"]")
    elif invalid := set(strategy) - _VALID_STRATEGIES:
        errors.append(f"`STRATEGY` 包含無效值：{sorted(invalid)}，只接受 TYPE1 / TYPE2")

    if data.get("RISK_TYPE") not in (0, 1):
        errors.append("`RISK_TYPE` 必須為 0（固定金額）或 1（固定損失）")

    risk_amount = data.get("RISK_AMOUNT")
    if not isinstance(risk_amount, (int, float)) or risk_amount <= 0:
        errors.append("`RISK_AMOUNT` 必須為正數")

    leverage = data.get("RISK_LEVERAGE")
    if not isinstance(leverage, int) or leverage <= 0:
        errors.append("`RISK_LEVERAGE` 必須為正整數")

    tp = data.get("TP_STRATEGY")
    if not isinstance(tp, list) or not (1 <= len(tp) <= 3):
        errors.append("`TP_STRATEGY` 必須包含 1 ~ 3 組止盈設定")
    else:
        total_pct = 0
        for i, entry in enumerate(tp, 1):
            rr = entry.get("RR_RATIO")
            pct = entry.get("PERCENT")
            if not isinstance(rr, (int, float)) or rr <= 0:
                errors.append(f"`TP_STRATEGY[{i}].RR_RATIO` 必須為正數")
            if not isinstance(pct, (int, float)) or pct <= 0:
                errors.append(f"`TP_STRATEGY[{i}].PERCENT` 必須為正數")
            else:
                total_pct += pct
        if total_pct > 100:
            errors.append(f"`TP_STRATEGY` PERCENT 總和不可超過 100（目前：{total_pct}）")

    order_limit = data.get("ORDER_LIMIT")
    if not isinstance(order_limit, int) or order_limit <= 0:
        errors.append("`ORDER_LIMIT` 必須為正整數")

    if not isinstance(data.get("ADD_SAME_SYMBOL"), bool):
        errors.append("`ADD_SAME_SYMBOL` 必須為 true 或 false")

    blacklist = data.get("SYMBOL_BLACKLIST")
    if not isinstance(blacklist, list) or not all(isinstance(s, str) for s in blacklist):
        errors.append("`SYMBOL_BLACKLIST` 必須為字串陣列，如 [] 或 [\"BTCUSDT\"]")

    if not isinstance(data.get("ENABLED"), bool):
        errors.append("`ENABLED` 必須為 true 或 false")

    return (len(errors) == 0, errors)


def get_user_config(user_id: int) -> dict | None:
    """讀取使用者設定，不存在則回傳 None。"""
    path = _DATA_DIR / f"{user_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_user_config(user_id: int, config: dict) -> None:
    """將使用者設定寫入 data/users/{user_id}.json。"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _DATA_DIR / f"{user_id}.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
