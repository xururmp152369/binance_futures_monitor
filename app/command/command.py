import json
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from ..user.user_config import (
    CONFIG_TEMPLATE_TEXT,
    get_user_config,
    save_user_config,
    validate_config,
    get_account_by_chat_id,
    is_session_valid,
    register_account,
    login_account,
    logout_account,
)


# ─── 登入檢查 helper ──────────────────────────────────────────────────────────

async def _require_login(update: Update) -> tuple[str, dict] | tuple[None, None]:
    """檢查登入狀態與 session 有效性，通過則自動延長 session。"""
    chat_id = update.effective_user.id
    result = get_account_by_chat_id(chat_id)
    if not result:
        await update.effective_message.reply_text(
            "❌ 尚未登入。\n\n"
            "請先建立帳號：`/register <帳號> <密碼>`\n"
            "或登入已有帳號：`/login <帳號> <密碼>`",
            parse_mode="Markdown",
        )
        return None, None
    account_name, acc = result
    if not is_session_valid(acc):
        cfg = get_user_config(account_name)
        if cfg and cfg.get("ENABLED"):
            cfg["ENABLED"] = False
            save_user_config(account_name, cfg)
        await update.effective_message.reply_text(
            "⚠️ *登入已過期，自動開單已停止*\n\n"
            "請重新登入：`/login <帳號> <密碼>`",
            parse_mode="Markdown",
        )
        return None, None
    return account_name, acc


# ─── 帳號指令 ─────────────────────────────────────────────────────────────────

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/register <帳號> <密碼> 或 /register lccadmin <帳號> <密碼> — 建立新帳號"""
    args = context.args
    if len(args) == 3:
        admin_code, account_name, password = args[0], args[1], args[2]
    elif len(args) == 2:
        admin_code, account_name, password = "", args[0], args[1]
    else:
        await update.message.reply_text(
            "用法：`/register <帳號> <密碼>`\n\n"
            "帳號：3 字元以上英數字\n密碼：6 字元以上",
            parse_mode="Markdown",
        )
        return
    try:
        await update.message.delete()
    except Exception:
        pass
    ok, msg = register_account(account_name, password, admin_code)
    suffix = f"\n\n請用 `/login {account_name} <密碼>` 登入" if ok else ""
    await update.effective_chat.send_message(
        f"{'✅' if ok else '❌'} {msg}{suffix}",
        parse_mode="Markdown",
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/login <帳號> <密碼> — 登入並綁定此 TG 帳號"""
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("用法：`/login <帳號> <密碼>`", parse_mode="Markdown")
        return
    account_name, password = args[0], args[1]
    try:
        await update.message.delete()
    except Exception:
        pass
    ok, msg = login_account(account_name, password, update.effective_user.id)
    await update.effective_chat.send_message(
        f"{'✅' if ok else '❌'} {msg}",
        parse_mode="Markdown",
    )


async def logout(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """/logout — 登出並停用自動開單"""
    ok, msg = logout_account(update.effective_user.id)
    await update.message.reply_text(f"{'👋' if ok else '❌'} {msg}")


# ─── 個人設定指令 ─────────────────────────────────────────────────────────────

async def setup(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """/setup — 傳送個人設定範本（需登入）"""
    account_name, _ = await _require_login(update)
    if not account_name:
        return
    await update.message.reply_text(CONFIG_TEMPLATE_TEXT, parse_mode="Markdown")


async def my_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/myconfig — 顯示設定摘要；/myconfig FIELD VALUE 更新單一欄位（需登入）"""
    account_name, acc = await _require_login(update)
    if not account_name:
        return

    # 單欄位更新模式
    if context.args:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ 格式錯誤，請使用：`/myconfig 欄位名稱 值`\n例如：`/myconfig LONG_ORDER_LIMIT 5`",
                parse_mode="Markdown",
            )
            return
        field = context.args[0].upper()
        if field not in _ALL_CONFIG_KEYS:
            await update.message.reply_text(
                f"❌ 無法識別的欄位：`{field}`\n請輸入 /myconfig 查看可設定的欄位。",
                parse_mode="Markdown",
            )
            return
        value_str = " ".join(context.args[1:])
        try:
            value = json.loads(value_str)
        except (json.JSONDecodeError, ValueError):
            value = value_str
        existing = get_user_config(account_name)
        if existing is None:
            await update.message.reply_text(
                "❌ 尚無設定檔，請先用 /setup 取得範本並完整填寫後傳送。"
            )
            return
        updated = {**existing, field: value}
        ok, errors = validate_config(updated)
        if not ok:
            error_lines = "\n".join(f"• {e}" for e in errors)
            await update.message.reply_text(
                f"❌ 值不合法：\n\n{error_lines}",
                parse_mode="Markdown",
            )
            return
        save_user_config(account_name, updated)
        await update.message.reply_text(
            f"✅ `{field}` 已更新為 `{value}`\n\n輸入 /myconfig 查看完整設定。",
            parse_mode="Markdown",
        )
        return

    cfg = get_user_config(account_name)
    if cfg is None:
        await update.message.reply_text(
            "尚未設定，請使用 /setup 取得範本填寫後傳回。"
        )
        return

    def _mask(key: str) -> str:
        val = cfg.get(key, "")
        return val[:4] + "****" if len(val) > 4 else "****"

    exp_str = "永久有效" if acc.get("permanent") else datetime.fromtimestamp(acc["session_expires_at"]).strftime("%Y-%m-%d %H:%M")

    def _fmt_tp(tp_list: list, label: str) -> str:
        if not tp_list:
            return f"{label}：（未設定）"
        lines = "\n".join(
            f"  {i}. {e['RR_RATIO']}R → 平倉 {e['PERCENT']}%"
            + (" (最後一筆，全數平倉)" if i == len(tp_list) else "")
            for i, e in enumerate(tp_list, 1)
        )
        return f"{label}：\n{lines}"

    risk_label  = "固定投入金額" if cfg.get("RISK_TYPE") == 0 else "固定損失金額"
    blacklist   = cfg.get("SYMBOL_BLACKLIST") or []
    margin_type = cfg.get("MARGIN_TYPE", "CROSSED")
    order_mode  = cfg.get("ORDER_MODE", "DEV")
    tp_str = _fmt_tp(cfg.get("TP_STRATEGY", []), "止盈策略")

    text = (
        f"📄 *你的目前設定*\n\n"
        f"帳號：`{account_name}`\n"
        f"Session 到期：`{exp_str}`\n\n"
        f"API Key（模擬）：`{_mask('API_KEY')}`\n"
        f"Secret Key（模擬）：`{_mask('SECRET_KEY')}`\n"
        f"API Key（正式）：`{_mask('PRD_API_KEY')}`\n"
        f"Secret Key（正式）：`{_mask('PRD_SECRET_KEY')}`\n\n"
        f"下單模式：`{'正式 (PRD)' if order_mode == 'PRD' else '模擬 (DEV)'}`\n"
        f"策略：`{'、'.join(cfg.get('STRATEGY', []))}`\n"
        f"風險模式：{risk_label}\n"
        f"投入/損失金額：`{cfg.get('RISK_AMOUNT')} USDT`\n"
        f"槓桿：`{cfg.get('RISK_LEVERAGE')}x`\n"
        f"保證金模式：`{margin_type}`\n\n"
        f"{tp_str}\n\n"
        f"多單上限：`{cfg.get('LONG_ORDER_LIMIT')} 筆`\n"
        f"同幣種加倉：`{'是' if cfg.get('ADD_SAME_SYMBOL') else '否'}`\n"
        f"黑名單：{'、'.join(blacklist) if blacklist else '（無）'}\n"
        f"自動開單：`{'開啟' if cfg.get('ENABLED') else '關閉'}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


_ALL_CONFIG_KEYS = {
    "API_KEY", "SECRET_KEY", "PRD_API_KEY", "PRD_SECRET_KEY", "ORDER_MODE",
    "STRATEGY", "RISK_TYPE", "RISK_AMOUNT", "RISK_LEVERAGE", "MARGIN_TYPE",
    "TP_STRATEGY", "LONG_ORDER_LIMIT",
    "ADD_SAME_SYMBOL", "SYMBOL_BLACKLIST", "ENABLED",
}


async def handle_json_message(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """接收使用者傳送的 JSON 文字，驗證後儲存為個人設定（需登入）。

    支援部分更新：若 JSON 缺少欄位且已有現有設定，則 merge 後儲存。
    """
    text = (update.message.text or "").strip()
    if not text.startswith("{"):
        return

    account_name, _ = await _require_login(update)
    if not account_name:
        return

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        await update.message.reply_text(
            f"❌ JSON 格式錯誤，請確認格式後重新傳送。\n錯誤位置：{e}"
        )
        return

    is_partial = not _ALL_CONFIG_KEYS.issubset(data.keys())
    if is_partial:
        existing = get_user_config(account_name)
        if existing is None:
            await update.message.reply_text(
                "❌ 尚無設定檔，部分更新需先有完整設定。\n請用 /setup 取得範本填寫後傳送。"
            )
            return
        updated_keys = list(data.keys())
        data = {**existing, **data}

    ok, errors = validate_config(data)
    if not ok:
        error_lines = "\n".join(f"• {e}" for e in errors)
        await update.message.reply_text(
            f"❌ 設定有誤，請修正後重新傳送：\n\n{error_lines}",
            parse_mode="Markdown",
        )
        return

    save_user_config(account_name, data)

    if is_partial:
        keys_str = "、".join(f"`{k}`" for k in updated_keys)
        await update.message.reply_text(
            f"✅ *已更新以下欄位：*\n{keys_str}\n\n輸入 /myconfig 查看完整設定。",
            parse_mode="Markdown",
        )
        return

    def _fmt_tp_save(tp_list: list, label: str) -> str:
        if not tp_list:
            return f"{label}：（未設定）"
        lines = "\n".join(
            f"  {i}. {e['RR_RATIO']}R → 平倉 {e['PERCENT']}%"
            for i, e in enumerate(tp_list, 1)
        )
        return f"{label}：\n{lines}"

    risk_label  = "固定投入金額" if data.get("RISK_TYPE") == 0 else "固定損失金額"
    margin_type = data.get("MARGIN_TYPE", "CROSSED")
    tp_str = _fmt_tp_save(data.get("TP_STRATEGY", []), "止盈策略")

    await update.message.reply_text(
        f"✅ *設定已儲存！*\n\n"
        f"策略：`{'、'.join(data.get('STRATEGY', []))}`\n"
        f"風險模式：{risk_label}\n"
        f"投入/損失金額：`{data.get('RISK_AMOUNT')} USDT`\n"
        f"槓桿：`{data.get('RISK_LEVERAGE')}x`\n"
        f"保證金模式：`{margin_type}`\n\n"
        f"{tp_str}\n\n"
        f"多單上限：`{data.get('LONG_ORDER_LIMIT')} 筆`\n"
        f"自動開單：`{'開啟' if data.get('ENABLED') else '關閉'}`",
        parse_mode="Markdown",
    )


# ─── 系統指令 ─────────────────────────────────────────────────────────────────

async def command(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """/command 指令：回覆可用指令說明。"""
    user = update.effective_user
    await update.message.reply_text(
        f"嗨 {user.first_name}！\n\n"
        "📋 帳號管理：\n"
        "/register <帳號> <密碼> — 建立新帳號\n"
        "/login <帳號> <密碼> — 登入\n"
        "/logout — 登出\n\n"
        "⚙️ 個人設定（需登入）：\n"
        "/setup — 取得自動開單設定範本\n"
        "/myconfig — 查看目前個人設定\n"
        "/myconfig 欄位 值 — 更新單一欄位，例：/myconfig LONG_ORDER_LIMIT 5\n\n"
    )



