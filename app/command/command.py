import json
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from ..setting.models import symbol_state, price_history, strategy_state, runtime_config
from ..strategy.state_machine import StrategyPhase
from ..extension.utils import reply_text_long
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
    """/register <帳號> <密碼> — 建立新帳號"""
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "用法：`/register <帳號> <密碼>`\n\n"
            "帳號：3 字元以上英數字\n密碼：6 字元以上",
            parse_mode="Markdown",
        )
        return
    account_name, password = args[0], args[1]
    try:
        await update.message.delete()
    except Exception:
        pass
    ok, msg = register_account(account_name, password)
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


async def my_config(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """/myconfig — 顯示目前個人設定摘要（需登入）"""
    account_name, acc = await _require_login(update)
    if not account_name:
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

    from datetime import datetime
    exp_str = datetime.fromtimestamp(acc["session_expires_at"]).strftime("%Y-%m-%d %H:%M")

    tp_lines = "\n".join(
        f"  {i}. {e['RR_RATIO']}R → 平倉 {e['PERCENT']}%"
        + (" (最後一筆，全數平倉)" if i == len(cfg.get("TP_STRATEGY", [])) else "")
        for i, e in enumerate(cfg.get("TP_STRATEGY", []), 1)
    )
    risk_label  = "固定投入金額" if cfg.get("RISK_TYPE") == 0 else "固定損失金額"
    blacklist   = cfg.get("SYMBOL_BLACKLIST") or []
    margin_type = cfg.get("MARGIN_TYPE", "CROSSED")

    text = (
        f"📄 *你的目前設定*\n\n"
        f"帳號：`{account_name}`\n"
        f"Session 到期：`{exp_str}`\n\n"
        f"API Key：`{_mask('API_KEY')}`\n"
        f"Secret Key：`{_mask('SECRET_KEY')}`\n\n"
        f"策略：`{'、'.join(cfg.get('STRATEGY', []))}`\n"
        f"風險模式：{risk_label}\n"
        f"投入/損失金額：`{cfg.get('RISK_AMOUNT')} USDT`\n"
        f"槓桿：`{cfg.get('RISK_LEVERAGE')}x`\n"
        f"保證金模式：`{margin_type}`\n\n"
        f"止盈策略：\n{tp_lines}\n\n"
        f"部位上限：`{cfg.get('ORDER_LIMIT')} 筆`\n"
        f"同幣種加倉：`{'是' if cfg.get('ADD_SAME_SYMBOL') else '否'}`\n"
        f"黑名單：{'、'.join(blacklist) if blacklist else '（無）'}\n"
        f"自動開單：`{'開啟' if cfg.get('ENABLED') else '關閉'}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


_ALL_CONFIG_KEYS = {
    "API_KEY", "SECRET_KEY", "STRATEGY", "RISK_TYPE", "RISK_AMOUNT",
    "RISK_LEVERAGE", "MARGIN_TYPE", "TP_STRATEGY", "ORDER_LIMIT",
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

    tp_lines = "\n".join(
        f"  {i}. {e['RR_RATIO']}R → 平倉 {e['PERCENT']}%"
        for i, e in enumerate(data.get("TP_STRATEGY", []), 1)
    )
    risk_label = "固定投入金額" if data.get("RISK_TYPE") == 0 else "固定損失金額"
    margin_type = data.get("MARGIN_TYPE", "CROSSED")

    await update.message.reply_text(
        f"✅ *設定已儲存！*\n\n"
        f"策略：`{'、'.join(data.get('STRATEGY', []))}`\n"
        f"風險模式：{risk_label}\n"
        f"投入/損失金額：`{data.get('RISK_AMOUNT')} USDT`\n"
        f"槓桿：`{data.get('RISK_LEVERAGE')}x`\n"
        f"保證金模式：`{margin_type}`\n\n"
        f"止盈策略：\n{tp_lines}\n\n"
        f"部位上限：`{data.get('ORDER_LIMIT')} 筆`\n"
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
        "/myconfig — 查看目前個人設定\n\n"
        "📊 行情查詢：\n"
        "/s <coin> — 搜尋幣種歷史資料，ex: btc\n"
        "/strategy <coin> — 查看策略狀態，ex: btc\n"
        "/config — 查看或修改執行期參數\n"
    )


async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/config 指令：查看或動態修改執行期參數。"""
    args = context.args

    if not args:
        lines = ["⚙️ *執行期參數一覽*\n"]
        descriptions = {
            "QUOTE_VOLUME":            "24h 最低成交量篩選 (USDT)",
            "PUMP_THRESHOLD":          "4h 拉漲偵測門檻 (%)",
            "CONSOLIDATION_MIN_HOURS": "最低盤整時數 (h)",
            "BREAKOUT_VOLUME_MULT":    "Type1 突破量能倍數",
            "EMA_TOUCH_THRESHOLD":     "Type2 EMA 觸碰容忍 (%)",
            "WICK_THRESHOLD":          "Type2 有效收針 (%)",
            "STRATEGY_RR_MIN":         "Type2 最低盈虧比",
            "STRATEGY_COOLDOWN":       "策略告警冷卻 (秒)",
        }
        for key, val in runtime_config.items():
            desc = descriptions.get(key, "")
            lines.append(f"`{key}` = `{val}` — {desc}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if args[0].lower() == "set" and len(args) >= 3:
        param = args[1].upper()
        raw_val = args[2]

        if param not in runtime_config:
            await update.message.reply_text(
                f"❌ 未知參數：`{param}`\n輸入 /config 查看所有可用參數",
                parse_mode="Markdown",
            )
            return

        try:
            old_val = runtime_config[param]
            new_val = int(float(raw_val)) if isinstance(old_val, int) else float(raw_val)
            runtime_config[param] = new_val
            await update.message.reply_text(
                f"✅ 已更新 `{param}`\n`{old_val}` → `{new_val}`",
                parse_mode="Markdown",
            )
        except ValueError:
            await update.message.reply_text(f"❌ 無效的數值：`{raw_val}`", parse_mode="Markdown")
        return

    await update.message.reply_text(
        "用法：\n/config\n/config set PARAM VALUE\n\n例如：/config set PUMP_THRESHOLD 8"
    )


async def strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/strategy 指令：查看指定幣種的策略狀態機當前狀態。"""
    args = context.args
    if not args:
        await update.message.reply_text("用法：\n/strategy btc\n查看指定幣種的策略狀態")
        return

    symbols = [arg.upper() if "USDT" in arg.upper() else f"{arg.upper()}USDT" for arg in args]

    for symbol in symbols:
        if symbol not in symbol_state:
            await update.message.reply_text(f"{symbol}：未監控的幣對")
            continue

        st = strategy_state.get(symbol)
        if not st:
            await update.message.reply_text(f"{symbol}：尚無策略狀態（IDLE）")
            continue

        phase = st.get("phase", StrategyPhase.IDLE)
        phase_label = {
            StrategyPhase.IDLE:     "💤 IDLE（閒置）",
            StrategyPhase.TRACKING: "👀 TRACKING（追蹤盤整）",
            StrategyPhase.READY:    "✅ READY（就緒，監控訊號）",
        }.get(phase, str(phase))

        lines = [f"📊 *{symbol} 策略狀態*", f"Phase：{phase_label}"]

        if phase != StrategyPhase.IDLE:
            pump_time = st.get("pump_candle_time")
            pump_time_str = (
                datetime.fromtimestamp(pump_time).strftime("%Y/%m/%d %H:%M")
                if pump_time else "N/A"
            )
            start_ts = st.get("consolidation_start_ts")
            elapsed_h = (datetime.now().timestamp() - start_ts) / 3600 if start_ts else 0

            lines += [
                f"拉漲時間：{pump_time_str}",
                f"已盤整：{elapsed_h:.1f} 小時",
                f"盤整底部：`{st.get('consolidation_low', 0):.6f}`",
                f"盤整頂部：`{st.get('consolidation_high', 0):.6f}`",
            ]

        last_ts = st.get("last_alert_ts", 0)
        last_type = st.get("last_signal_type")
        if last_ts and last_type:
            last_str = datetime.fromtimestamp(last_ts).strftime("%Y/%m/%d %H:%M")
            lines.append(f"上次訊號：{last_type} ({last_str})")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/s 指令：輸出指定幣種的價格歷史快照。"""
    args = context.args

    if not args:
        await update.message.reply_text("用法：\n/s btc eth\n輸入一個或多個幣對名稱")
        return

    symbols = [arg.upper() if "USDT" in arg.upper() else f"{arg.upper()}USDT" for arg in args]

    for symbol in symbols:
        price_hist = price_history.get(symbol, [])

        if not price_hist:
            await update.message.reply_text(f"{symbol}：無歷史資料")
            continue

        symbol_info = [f"{symbol}，已儲存的歷史訊息參考："]

        for i, (timestamp, price) in enumerate(price_hist, 1):
            time_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
            symbol_info.append(f"紀錄時間: {time_str}，紀錄價格: {price:.6f} (第{i}筆)")

        await reply_text_long(update.message, "\n".join(symbol_info))
