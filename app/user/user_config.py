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
        return json.loads(_get_fernet().decrypt(p.read_bytes()).decode("utf-8"))
    except Exception:
        return None


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

_VALID_STRATEGIES = {"TYPE1", "TYPE2"}

CONFIG_TEMPLATE_TEXT = """\
📋 *個人設定說明*

請複製下方 JSON，填寫後直接傳給我：

*欄位說明：*
• `API_KEY` / `SECRET_KEY`：模擬帳戶（Testnet）API 金鑰
• `PRD_API_KEY` / `PRD_SECRET_KEY`：正式帳戶 API 金鑰（ORDER\\_MODE=PRD 時必填）
• `ORDER_MODE`：下單環境，`"DEV"`（模擬，預設）或 `"PRD"`（正式）
• `STRATEGY`：觸發自動開單的策略，可填 "TYPE1"（帶量突破）、"TYPE2"（均線反彈），或兩者皆填
• `RISK_TYPE`：風險計算方式
  ‣ `0`：固定投入金額（RISK\\_AMOUNT × 槓桿 USDT）
  ‣ `1`：固定損失金額（依止損比例換算手數）
• `RISK_AMOUNT`：投入或損失金額（USDT）
• `RISK_LEVERAGE`：槓桿倍數
• `MARGIN_TYPE`：保證金模式，`"CROSSED"`（全倉）或 `"ISOLATED"`（逐倉）
• `TP_STRATEGY`：止盈策略，至少 1 組、至多 3 組，PERCENT 總計不超過 100
  ‣ `RR_RATIO`：止盈盈虧比（1 = 1R，1.5 = 1.5R）
  ‣ `PERCENT`：達到該盈虧比時平倉的部位比例 (%)，最後一組會自動全數平倉
• `ORDER_LIMIT`：同時持有部位數上限
• `ADD_SAME_SYMBOL`：同幣種已有倉位時，是否再次開單（加倉）
• `SYMBOL_BLACKLIST`：不自動開單的幣種清單，空陣列表示不限制
• `ENABLED`：是否啟用自動開單

```
{
    "API_KEY": "",
    "SECRET_KEY": "",
    "PRD_API_KEY": "",
    "PRD_SECRET_KEY": "",
    "ORDER_MODE": "DEV",
    "STRATEGY": ["TYPE1", "TYPE2"],
    "RISK_TYPE": 0,
    "RISK_AMOUNT": 0.1,
    "RISK_LEVERAGE": 20,
    "MARGIN_TYPE": "CROSSED",
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


def validate_config(data: dict) -> tuple[bool, list[str]]:
    """驗證使用者設定 dict，回傳 (是否通過, 錯誤訊息列表)。"""
    errors = []

    for key in ("API_KEY", "SECRET_KEY"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f"`{key}` 必須為非空字串")

    order_mode = data.get("ORDER_MODE", "DEV")
    if order_mode not in ("PRD", "DEV"):
        errors.append("`ORDER_MODE` 必須為 \"PRD\"（正式）或 \"DEV\"（模擬）")
    elif order_mode == "PRD":
        for key in ("PRD_API_KEY", "PRD_SECRET_KEY"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                errors.append(f"`{key}` 使用正式模式（ORDER_MODE=PRD）時必須填寫")

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

    margin_type = data.get("MARGIN_TYPE", "CROSSED")
    if margin_type not in ("CROSSED", "ISOLATED"):
        errors.append("`MARGIN_TYPE` 必須為 \"CROSSED\"（全倉）或 \"ISOLATED\"（逐倉）")

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
