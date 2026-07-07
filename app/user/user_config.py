import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from cryptography.fernet import Fernet

_ACCOUNTS_DIR = Path(__file__).parent.parent.parent / "data" / "accounts"
_CONFIGS_DIR  = Path(__file__).parent.parent.parent / "data" / "configs"

SESSION_DURATION = 30 * 86400  # 30 天（秒）


# ─── Fernet 加解密 ────────────────────────────────────────────────────────────

def _get_fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY 未設定，請見 .env 說明")
    return Fernet(key.encode() if isinstance(key, str) else key)


# ─── 密碼雜湊 ─────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"sha256:{salt}:{h}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split(":", 2)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False


# ─── 帳號檔 CRUD ──────────────────────────────────────────────────────────────

def _account_path(account_name: str) -> Path:
    return _ACCOUNTS_DIR / f"{account_name}.json"


def _load_account(account_name: str) -> dict | None:
    p = _account_path(account_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_account(account_name: str, data: dict) -> None:
    _ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    _account_path(account_name).write_text(
        json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8"
    )


def get_account_by_chat_id(tg_chat_id: int) -> tuple[str, dict] | None:
    """找出與此 TG chat_id 綁定的帳號，回傳 (account_name, acc_data) 或 None。"""
    if not _ACCOUNTS_DIR.exists():
        return None
    for p in _ACCOUNTS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("tg_chat_id") == tg_chat_id:
                return p.stem, data
        except Exception:
            continue
    return None


# ─── Session ─────────────────────────────────────────────────────────────────

def is_session_valid(acc: dict) -> bool:
    if acc.get("permanent"):
        return True
    exp = acc.get("session_expires_at")
    return bool(exp and time.time() < exp)


def refresh_session(account_name: str, acc: dict) -> None:
    """每次有效互動後延長 session 到期時間。永久帳號不更新。"""
    if acc.get("permanent"):
        return
    acc["session_expires_at"] = time.time() + SESSION_DURATION
    _save_account(account_name, acc)


def _fmt_ts(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ─── 帳號操作 ─────────────────────────────────────────────────────────────────

_ADMIN_CODE = "lccadmin"


def register_account(account_name: str, password: str, admin_code: str = "") -> tuple[bool, str]:
    """建立新帳號，回傳 (成功, 訊息)。admin_code == _ADMIN_CODE 時帳號永久有效。"""
    if not account_name.isalnum() or len(account_name) < 3:
        return False, "帳號需為 3 字元以上英數字組合"
    if len(password) < 6:
        return False, "密碼需至少 6 字元"
    if _load_account(account_name):
        return False, "帳號已存在，請換一個帳號名稱"
    permanent = admin_code == _ADMIN_CODE
    _save_account(account_name, {
        "password_hash": _hash_password(password),
        "tg_chat_id": None,
        "session_expires_at": None,
        "permanent": permanent,
    })
    msg = "帳號建立成功（永久有效）" if permanent else "帳號建立成功"
    return True, msg


def login_account(account_name: str, password: str, tg_chat_id: int) -> tuple[bool, str]:
    """驗證密碼並綁定 TG 帳號，回傳 (成功, 訊息)。"""
    acc = _load_account(account_name)
    if not acc:
        return False, "帳號不存在"
    if not _verify_password(password, acc["password_hash"]):
        return False, "密碼錯誤"
    acc["tg_chat_id"] = tg_chat_id
    if acc.get("permanent"):
        acc["session_expires_at"] = None
        _save_account(account_name, acc)
        return True, "登入成功，此帳號 Session 永久有效"
    expires = time.time() + SESSION_DURATION
    acc["session_expires_at"] = expires
    _save_account(account_name, acc)
    return True, f"登入成功，Session 有效期至 {_fmt_ts(expires)}"


def logout_account(tg_chat_id: int) -> tuple[bool, str]:
    """登出並停用自動開單，回傳 (成功, 訊息)。"""
    result = get_account_by_chat_id(tg_chat_id)
    if not result:
        return False, "你目前未登入"
    account_name, acc = result
    cfg = get_user_config(account_name)
    if cfg and cfg.get("ENABLED"):
        cfg["ENABLED"] = False
        save_user_config(account_name, cfg)
    acc["tg_chat_id"] = None
    acc["session_expires_at"] = None
    _save_account(account_name, acc)
    return True, "已登出，自動開單已停止"


# ─── 設定檔 CRUD（加密） ──────────────────────────────────────────────────────

def get_user_config(account_name: str) -> dict | None:
    """讀取並解密使用者設定，失敗回傳 None。"""
    p = _CONFIGS_DIR / f"{account_name}.json"
    if not p.exists():
        return None
    try:
        cfg = json.loads(_get_fernet().decrypt(p.read_bytes()).decode("utf-8"))
    except Exception:
        return None
    # 舊設定 ORDER_LIMIT → LONG_ORDER_LIMIT 自動 migration
    if "ORDER_LIMIT" in cfg and "LONG_ORDER_LIMIT" not in cfg:
        cfg["LONG_ORDER_LIMIT"] = cfg.pop("ORDER_LIMIT")
        save_user_config(account_name, cfg)
    # 舊設定 TP_STRATEGY → LONG_TP_STRATEGY 自動 migration
    if "TP_STRATEGY" in cfg and "LONG_TP_STRATEGY" not in cfg:
        cfg["LONG_TP_STRATEGY"] = cfg.pop("TP_STRATEGY")
        save_user_config(account_name, cfg)
    # 舊策略代號 TYPE1 → long_breakout（涵蓋三個策略欄位）
    for _field in ("STRATEGY", "PRD_STRATEGY", "DEV_STRATEGY"):
        _s = cfg.get(_field, [])
        if "TYPE1" in _s:
            cfg[_field] = ["long_breakout" if x == "TYPE1" else x for x in _s]
            save_user_config(account_name, cfg)
    # 舊設定 STRATEGY + ORDER_MODE → PRD_STRATEGY / DEV_STRATEGY 自動 migration
    if "STRATEGY" in cfg and "PRD_STRATEGY" not in cfg and "DEV_STRATEGY" not in cfg:
        _strategy   = cfg.pop("STRATEGY")
        _order_mode = cfg.pop("ORDER_MODE", "DEV")
        if _order_mode == "PRD":
            cfg["PRD_STRATEGY"] = _strategy
        else:
            cfg["DEV_STRATEGY"] = _strategy
        save_user_config(account_name, cfg)
    return cfg


def save_user_config(account_name: str, config: dict) -> None:
    """加密並寫入使用者設定。"""
    _CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    encrypted = _get_fernet().encrypt(json.dumps(config, ensure_ascii=False).encode("utf-8"))
    (_CONFIGS_DIR / f"{account_name}.json").write_bytes(encrypted)


def get_all_trading_configs() -> list[tuple[str, dict]]:
    """回傳所有帳號的 (account_name, config)，供自動開單遍歷使用。"""
    result = []
    if not _CONFIGS_DIR.exists():
        return result
    for p in _CONFIGS_DIR.glob("*.json"):
        try:
            cfg = get_user_config(p.stem)
            if cfg is not None:
                result.append((p.stem, cfg))
        except Exception:
            continue
    return result


def get_all_trading_configs_with_chat_id() -> list[tuple[str, int | None, dict]]:
    """回傳所有帳號的 (account_name, tg_chat_id, config)，供開單結果通知使用。"""
    result = []
    if not _CONFIGS_DIR.exists():
        return result
    for p in _CONFIGS_DIR.glob("*.json"):
        try:
            cfg = get_user_config(p.stem)
            if cfg is not None:
                acc = _load_account(p.stem)
                chat_id = acc.get("tg_chat_id") if acc else None
                result.append((p.stem, chat_id, cfg))
        except Exception:
            continue
    return result


def get_all_registered_chat_ids() -> list[int]:
    """回傳所有已綁定 TG 帳號的 chat_id，供策略告警廣播使用。"""
    result = []
    if not _ACCOUNTS_DIR.exists():
        return result
    for p in _ACCOUNTS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            cid = data.get("tg_chat_id")
            if cid:
                result.append(cid)
        except Exception:
            continue
    return result


# ─── Session 過期掃描（由 periodic_screen 呼叫） ─────────────────────────────

async def check_expired_sessions(bot) -> None:
    """掃描所有帳號，對 session 已過期且 ENABLED=True 的帳號停用並通知。"""
    if not _ACCOUNTS_DIR.exists():
        return
    for p in _ACCOUNTS_DIR.glob("*.json"):
        try:
            account_name = p.stem
            acc = json.loads(p.read_text(encoding="utf-8"))
            chat_id = acc.get("tg_chat_id")
            if not chat_id or acc.get("permanent"):
                continue
            exp = acc.get("session_expires_at")
            if not exp or time.time() < exp:
                continue
            cfg = get_user_config(account_name)
            if not (cfg and cfg.get("ENABLED")):
                continue
            cfg["ENABLED"] = False
            save_user_config(account_name, cfg)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ *登入已過期，自動開單已停止*\n\n"
                        "若要重新啟動，請先重新登入：\n"
                        "`/login <帳號> <密碼>`\n\n"
                        "登入後再傳送以下訊息重新開啟自動開單：\n"
                        '`{"ENABLED": true}`'
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        except Exception:
            continue


# ─── 設定範本 ─────────────────────────────────────────────────────────────────

_VALID_STRATEGIES = {"long_breakout", "death_cross_short", "fibonacci_long", "fibonacci_short"}

CONFIG_TEMPLATE_TEXT = """\
📋 *個人設定說明*

請複製下方 JSON，填寫後直接傳給我：

*欄位說明：*
• `EXCHANGE`：交易所（`"binance"` 或 `"bingx"`，預設 `"binance"`）
• `API_KEY` / `SECRET_KEY`：模擬帳戶 API 金鑰（Binance Testnet / BingX Virtual Trading，DEV\\_STRATEGY 有值時必填）
• `PRD_API_KEY` / `PRD_SECRET_KEY`：正式帳戶 API 金鑰（PRD\\_STRATEGY 有值時必填）
• `PRD_STRATEGY`：觸發*正式自動開單*的策略（填 `[]` 停用正式下單）
  ‣ `"long_breakout"`：多頭盤整突破（4h 帶量拉漲 → 盤整 → 15m 帶量突破做多）
  ‣ `"death_cross_short"`：死亡叉制空（日線 EMA50<EMA200 格局 → 日線跌破 EMA200 → 1H 拒絕/吞噬做空）
  ‣ `"fibonacci_long"`：Fibonacci 多頭（底底高形態 + Fib 1.73 影線確認）
  ‣ `"fibonacci_short"`：Fibonacci 空頭（頂頂低形態 + Fib 1.73 影線確認）
• `DEV_STRATEGY`：觸發*模擬自動開單*的策略（填 `[]` 停用模擬下單，有效值同上）
• `NOTIFY_STRATEGY`：接收*訊號通知*的策略（不想接收填 `[]`，有效值同上）
• `RISK_TYPE`：風險計算方式
  ‣ `0`：固定投入金額（RISK\\_AMOUNT × 槓桿 USDT）
  ‣ `1`：固定損失金額（依止損比例換算手數）
• `RISK_AMOUNT`：投入或損失金額（USDT）
• `RISK_LEVERAGE`：槓桿倍數
• `MARGIN_TYPE`：保證金模式，`"CROSSED"`（全倉）或 `"ISOLATED"`（逐倉）
• `LONG_TP_STRATEGY`：多頭止盈策略，至少 1 組、至多 3 組，PERCENT 總計不超過 100
  ‣ `RR_RATIO`：止盈盈虧比（1 = 1R，1.5 = 1.5R）
  ‣ `PERCENT`：達到該盈虧比時平倉的部位比例 (%)，最後一組會自動全數平倉
• `SHORT_TP_STRATEGY`：空頭止盈策略（選填，格式同上；不填則沿用多頭止盈策略）
• `LONG_ORDER_LIMIT`：同時持有多單部位數上限
• `SHORT_ORDER_LIMIT`：同時持有空單部位數上限
• `ADD_SAME_SYMBOL`：同幣種已有倉位時，是否再次開單（加倉）
• `SYMBOL_BLACKLIST`：不自動開單的幣種清單，空陣列表示不限制
• `ENABLED`：是否啟用自動開單

```
{
    "EXCHANGE": "binance",
    "API_KEY": "",
    "SECRET_KEY": "",
    "PRD_API_KEY": "",
    "PRD_SECRET_KEY": "",
    "PRD_STRATEGY": [],
    "DEV_STRATEGY": ["long_breakout"],
    "NOTIFY_STRATEGY": ["long_breakout", "death_cross_short"],
    "RISK_TYPE": 0,
    "RISK_AMOUNT": 0.1,
    "RISK_LEVERAGE": 20,
    "MARGIN_TYPE": "CROSSED",
    "LONG_TP_STRATEGY": [
        { "RR_RATIO": 1.5, "PERCENT": 50 }
    ],
    "SHORT_TP_STRATEGY": [
        { "RR_RATIO": 1.5, "PERCENT": 50 }
    ],
    "LONG_ORDER_LIMIT": 10,
    "SHORT_ORDER_LIMIT": 5,
    "ADD_SAME_SYMBOL": false,
    "SYMBOL_BLACKLIST": [],
    "ENABLED": true
}
```\
"""


def validate_config(data: dict) -> tuple[bool, list[str]]:
    """驗證使用者設定 dict，回傳 (是否通過, 錯誤訊息列表)。"""
    errors = []

    prd_strategy = data.get("PRD_STRATEGY", [])
    dev_strategy = data.get("DEV_STRATEGY", [])

    if prd_strategy:
        for key in ("PRD_API_KEY", "PRD_SECRET_KEY"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                errors.append(f"`{key}` 使用正式策略（PRD_STRATEGY）時必須填寫")

    if dev_strategy:
        for key in ("API_KEY", "SECRET_KEY"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                errors.append(f"`{key}` 使用模擬策略（DEV_STRATEGY）時必須填寫")

    for field in ("PRD_STRATEGY", "DEV_STRATEGY"):
        val = data.get(field)
        if val is not None:
            if not isinstance(val, list):
                errors.append(f"`{field}` 必須為陣列，如 [\"long_breakout\"] 或 []")
            elif invalid := set(val) - _VALID_STRATEGIES:
                invalid_str = ", ".join(f"`{v}`" for v in sorted(invalid))
                errors.append(f"`{field}` 包含無效值：{invalid_str}，只接受 `long_breakout`、`death_cross_short`、`fibonacci_long`、`fibonacci_short`")

    notify_strategy = data.get("NOTIFY_STRATEGY")
    if notify_strategy is not None:
        if not isinstance(notify_strategy, list):
            errors.append("`NOTIFY_STRATEGY` 必須為陣列，如 [\"long_breakout\"] 或 []")
        elif invalid_ns := set(notify_strategy) - _VALID_STRATEGIES:
            invalid_ns_str = ", ".join(f"`{v}`" for v in sorted(invalid_ns))
            errors.append(f"`NOTIFY_STRATEGY` 包含無效值：{invalid_ns_str}，只接受 `long_breakout`、`death_cross_short`、`fibonacci_long`、`fibonacci_short`")

    if data.get("RISK_TYPE") not in (0, 1):
        errors.append("`RISK_TYPE` 必須為 0（固定金額）或 1（固定損失）")

    risk_amount = data.get("RISK_AMOUNT")
    if not isinstance(risk_amount, (int, float)) or risk_amount <= 0:
        errors.append("`RISK_AMOUNT` 必須為正數")

    leverage = data.get("RISK_LEVERAGE")
    if not isinstance(leverage, int) or leverage <= 0:
        errors.append("`RISK_LEVERAGE` 必須為正整數")

    margin_type = data.get("MARGIN_TYPE", "CROSSED")
    if margin_type not in ("CROSSED", "ISOLATED"):
        errors.append("`MARGIN_TYPE` 必須為 \"CROSSED\"（全倉）或 \"ISOLATED\"（逐倉）")

    def _validate_tp(field: str) -> None:
        tp = data.get(field)
        if tp is None:
            return  # 選填欄位，不填則 fallback 到 TP_STRATEGY
        if not isinstance(tp, list) or len(tp) < 1:
            errors.append(f"`{field}` 必須包含至少 1 組止盈設定")
            return
        total_pct = 0
        for i, entry in enumerate(tp, 1):
            rr = entry.get("RR_RATIO")
            pct = entry.get("PERCENT")
            if not isinstance(rr, (int, float)) or rr <= 0:
                errors.append(f"`{field}[{i}].RR_RATIO` 必須為正數")
            if not isinstance(pct, (int, float)) or pct <= 0:
                errors.append(f"`{field}[{i}].PERCENT` 必須為正數")
            else:
                total_pct += pct
        if total_pct > 100:
            errors.append(f"`{field}` PERCENT 總和不可超過 100（目前：{total_pct}）")

    tp = data.get("LONG_TP_STRATEGY")
    if not isinstance(tp, list) or len(tp) < 1:
        errors.append("`LONG_TP_STRATEGY` 必須包含至少 1 組止盈設定")
    else:
        _validate_tp("LONG_TP_STRATEGY")

    if data.get("SHORT_TP_STRATEGY") is not None:
        _validate_tp("SHORT_TP_STRATEGY")

    long_order_limit = data.get("LONG_ORDER_LIMIT")
    if not isinstance(long_order_limit, int) or long_order_limit <= 0:
        errors.append("`LONG_ORDER_LIMIT` 必須為正整數")

    short_order_limit = data.get("SHORT_ORDER_LIMIT")
    if short_order_limit is not None:
        if not isinstance(short_order_limit, int) or short_order_limit <= 0:
            errors.append("`SHORT_ORDER_LIMIT` 必須為正整數")

    if not isinstance(data.get("ADD_SAME_SYMBOL"), bool):
        errors.append("`ADD_SAME_SYMBOL` 必須為 true 或 false")

    blacklist = data.get("SYMBOL_BLACKLIST")
    if not isinstance(blacklist, list) or not all(isinstance(s, str) for s in blacklist):
        errors.append("`SYMBOL_BLACKLIST` 必須為字串陣列，如 [] 或 [\"BTCUSDT\"]")

    exchange = data.get("EXCHANGE", "binance")
    if exchange not in ("binance", "bingx"):
        errors.append("`EXCHANGE` 必須為 `\"binance\"` 或 `\"bingx\"`")

    if not isinstance(data.get("ENABLED"), bool):
        errors.append("`ENABLED` 必須為 true 或 false")

    return (len(errors) == 0, errors)
