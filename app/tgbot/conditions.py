import time
import asyncio
from binance import Client
from ..setting.config import OI_THRESHOLD, PRICE_THRESHOLD, VOLUME_THRESHOLD
from ..setting.models import symbol_state, oi_history, price_history

def _pick_reference_point(hist, now: float, window_sec: int):
    """從 (timestamp, value) 的歷史序列中挑選最接近目標窗口的參考點。

    會挑選「時間 <= now - window_sec」的最後一筆作為參考點；若找不到（資料不足），
    則退回使用 hist[0]。

    Args:
        hist: 由 (timestamp, value) 組成的序列（通常是 deque）。
        now: 目前時間（秒）。
        window_sec: 希望回溯的時間窗（秒）。

    Returns:
        (ref_t, ref_v): 參考點時間與值。
    """
    target_t = now - window_sec
    ref_t = None
    ref_v = None
    for t, v in reversed(hist):
        if t <= target_t:
            ref_t, ref_v = t, v
            break
    if ref_t is None:
        ref_t, ref_v = hist[0]
    return ref_t, ref_v


def _calc_volume_stats_15m_from_1m(vol_deque, *, window_len: int = 15, baseline_len: int = 48 * 60):
    """計算 15 分鐘成交量與 48 小時 baseline 的平均 15 分鐘成交量。

    此函式是自動告警與手動檢查共用的成交量計算邏輯，避免兩邊修改不同步。

    規則：
    - current_vol：最近 window_len 筆（1m）成交量總和
    - baseline：排除最後 window_len 後，取前 baseline_len 筆（1m）
    - avg_vol：baseline 的「1m 平均」換算成「window_len 分鐘平均」

    Args:
        vol_deque: 1m quoteVolume 的序列（deque）。
        window_len: 視窗長度（預設 15）。
        baseline_len: baseline 長度（預設 48 小時 = 2880）。

    Returns:
        (current_vol, avg_vol, vol_ratio):
            current_vol: 最近 15 分鐘成交量
            avg_vol: 過去 48 小時平均 15 分鐘成交量
            vol_ratio: current/avg（若 avg<=0 則為 0）
    """
    if vol_deque is None:
        return 0, 0, 0
    if len(vol_deque) < window_len + baseline_len:
        return 0, 0, 0

    vol_list = list(vol_deque)
    current_vol = sum(vol_list[-window_len:])
    baseline = vol_list[-(baseline_len + window_len):-window_len]
    avg_vol = (sum(baseline) / len(baseline) * window_len) if baseline else 0
    vol_ratio = (current_vol / avg_vol) if avg_vol > 0 else 0
    return current_vol, avg_vol, vol_ratio


def _format_alert_reason(*, vol_ratio: float, price_pct, oi_pct):
    """統一組合告警原因文字。

    Args:
        vol_ratio: 成交量倍數（current/avg）
        price_pct: 價格變化百分比
        oi_pct: OI 變化百分比

    Returns:
        str: 觸發原因文字（含換行）。
    """
    return (
        f"成交量暴增 {vol_ratio or 0:.1f}×\n"
        f"價格異動 {price_pct or 0:+.2f}%\n"
        f"持倉變化 {oi_pct or 0:+.1f}%"
    )


def _build_alert_data(*, vol_ratio: float, price_pct, oi_pct):
    """統一建立告警資料結構（供 send_alert 與手動檢查共用）。"""
    return {
        "price_pct": price_pct,
        "oi_pct": oi_pct,
        "reason": [_format_alert_reason(vol_ratio=vol_ratio, price_pct=price_pct, oi_pct=oi_pct)],
    }

async def check_oi_condition(symbol, now):
    """檢查持倉量（OI）是否符合 1 小時變化門檻。

    使用 `oi_history[symbol]` 的資料，找出最接近 1 小時前的參考 OI 後計算變化百分比。

    Returns:
        (met, pct):
            met: 是否超過 `OI_THRESHOLD`
            pct: OI 變化百分比（資料不足則為 None）
    """
    # 確認歷史資料是否足夠
    hist = oi_history[symbol]
    if len(hist) < 2: return False, None
    # 確認幣種最新持倉量是否有資料
    cur = symbol_state[symbol]["last_oi"]
    if cur <= 0: return False, None
    # 確認是否符合1小時監測
    old_t, old_oi = _pick_reference_point(hist, now, 3600)
    if old_oi <= 0:
        return False, None
    pct = (cur - old_oi) / old_oi * 100
    if now - old_t < 3600: return False, pct
    return pct > OI_THRESHOLD, pct

async def check_price_condition(symbol, now):
    """檢查價格是否符合 15 分鐘變化門檻。

    使用 `price_history[symbol]` 的資料，找出最接近 15 分鐘前的參考價格後計算漲跌幅。

    Returns:
        (met, pct):
            met: 是否超過 `PRICE_THRESHOLD`
            pct: 價格變化百分比（資料不足則為 None）
    """
    # 確認歷史資料是否足夠
    hist = price_history[symbol]
    if len(hist) < 2: return False, None
    # 確認幣種最新價格是否有資料
    cur = symbol_state[symbol]["last_price"]
    if cur <= 0: return False, None
    # 確認是否符合15分鐘監測
    old_t, old_p = _pick_reference_point(hist, now, 900)
    if old_p <= 0:
        return False, None
    pct = (cur - old_p) / old_p * 100
    if now - old_t < 900: return False, pct
    return pct > PRICE_THRESHOLD, pct

async def check_kline_overfulfil(symbol, kline):
    """判斷指定 K 線週期是否呈現 EMA 多頭排列且價格站上關鍵均線。

    Args:
        symbol: 幣種
        kline: 週期字串（例如 "1h" / "4h"）

    Returns:
        True/None: 符合條件回 True，否則回 None。
    """
    ema15 = symbol_state[symbol][f"ema_{kline}"][15]
    ema30 = symbol_state[symbol][f"ema_{kline}"][30]
    ema45 = symbol_state[symbol][f"ema_{kline}"][45]
    ema60 = symbol_state[symbol][f"ema_{kline}"][60]
    cur = symbol_state[symbol]["last_price"]
    if ema15 is not None and ema30 is not None and ema45 is not None and ema60 is not None and ema15 > ema30 > ema45 > ema60 and cur > ema45:
        return True

async def check_conditions(client, sym):
    """自動告警用的條件檢查。

    觸發邏輯：
    1) 成交量條件：最近 15 分鐘成交量 > 過去 48 小時平均 15 分鐘成交量 * VOLUME_THRESHOLD
    2) 價格條件：15 分鐘漲跌幅 > PRICE_THRESHOLD

    Returns:
        dict 或 None：若觸發則回傳告警資料（給 telegram_bot 發送），否則回 None。
    """
    state = symbol_state[sym]
    if state["last_price"] is None or state["last_oi"] is None:
        return None
    if time.time() - state["monitor_start"] < 60:
        return None
    # if not await check_kline_overfulfil(sym, "1h"):
    #     return None

    now = time.time()
    vol_ratio = 0
    try:
        current_vol, avg_vol, vol_ratio = _calc_volume_stats_15m_from_1m(state.get("volume_1m"))
        if avg_vol <= 0:
            return None
        if avg_vol > 0 and current_vol > avg_vol * VOLUME_THRESHOLD:
            oi_met, oi_pct = await check_oi_condition(sym, now)
            price_met, price_pct = await check_price_condition(sym, now)
            # kline_4h = await check_kline_overfulfil(sym, "4h")

            reasons = []

            if price_met:
                return _build_alert_data(vol_ratio=vol_ratio, price_pct=price_pct, oi_pct=oi_pct)
            else: 
                return None
    except:
        return None

# 手動檢查條件（帶詳細日誌）
async def check_conditions_manual(client, sym):
    """手動檢查條件，返回詳細日誌。

    此函式會輸出每個步驟的計算結果，方便你用 Telegram `/c` 指令確認現在的判斷依據。

    Returns:
        (result, logs):
            result: dict 或 None
            logs: list[str] 的詳細過程
    """
    logs = []
    state = symbol_state[sym]
    
    # 1. 基本檢查
    logs.append(f"🔍 檢查 {sym} 的條件...")
    
    if state["last_price"] is None or state["last_oi"] is None:
        logs.append("❌ 基本檢查失敗：缺少價格或持倉量資料")
        return None, logs
    
    monitor_time = time.time() - state["monitor_start"]
    if monitor_time < 60:
        logs.append(f"❌ 監控時間不足：{monitor_time:.1f}秒 < 60秒")
        return None, logs
    
    logs.append(f"✅ 基本檢查通過：監控時間 {monitor_time:.1f}秒")
    
    now = time.time()
    vol_ratio = 0
    
    try:
        # 2. 成交量檢查
        logs.append("📊 檢查成交量條件...")
        window_len = 15  # 15分鐘（每筆 1m）
        baseline_len = 48 * 60  # 48小時（每筆 1m）
        if len(state["volume_1m"]) < window_len + baseline_len:
            logs.append(f"❌ 成交量資料不足：{len(state['volume_1m'])} < {window_len + baseline_len}")
            return None, logs
        current_vol, avg_vol, vol_ratio = _calc_volume_stats_15m_from_1m(
            state["volume_1m"],
            window_len=window_len,
            baseline_len=baseline_len,
        )
        
        logs.append(f"📊 當前成交量：{current_vol:,.0f}")
        logs.append(f"📊 平均成交量：{avg_vol:,.0f}")
        logs.append(f"📊 成交量閥值：{VOLUME_THRESHOLD}倍")
        
        if avg_vol <= 0:
            logs.append("❌ 平均成交量為 0，無法比較")
            return None, logs
            
        required_vol = avg_vol * VOLUME_THRESHOLD
        
        logs.append(f"📊 成交量倍數：{vol_ratio:.2f}倍")
        logs.append(f"📊 需要成交量：{required_vol:,.0f}")
        
        if current_vol <= required_vol:
            logs.append(f"❌ 成交量不足：{current_vol:,.0f} <= {required_vol:,.0f}")
            return None, logs
            
        logs.append(f"✅ 成交量條件通過：{vol_ratio:.2f}倍")
        
        # 3. 持倉量檢查
        logs.append("📊 檢查持倉量條件...")
        oi_met, oi_pct = await check_oi_condition(sym, now)
        
        if oi_pct is not None:
            logs.append(f"📊 持倉量變化：{oi_pct:+.2f}%")
            logs.append(f"📊 持倉量閥值：{OI_THRESHOLD}%")
            if oi_met:
                logs.append(f"✅ 持倉量條件通過")
            else:
                logs.append(f"❌ 持倉量條件未通過")
        else:
            logs.append("❌ 持倉量資料不足")
            
        # 4. 價格檢查
        logs.append("📈 檢查價格條件...")
        price_met, price_pct = await check_price_condition(sym, now)
        
        if price_pct is not None:
            logs.append(f"📈 價格變化：{price_pct:+.2f}%")
            logs.append(f"📈 價格閥值：{PRICE_THRESHOLD}%")
            if price_met:
                logs.append(f"✅ 價格條件通過")
            else:
                logs.append(f"❌ 價格條件未通過")
        else:
            logs.append("❌ 價格資料不足")
            
        # 5. 最終判斷
        if price_met:
            logs.append("✅ 所有條件通過，會觸發告警！")
            result = _build_alert_data(vol_ratio=vol_ratio, price_pct=price_pct, oi_pct=oi_pct)
            return result, logs
        else:
            logs.append("❌ 價格條件未通過，不會觸發告警")
            return None, logs
            
    except Exception as e:
        logs.append(f"❌ 檢查過程發生錯誤：{str(e)}")
        return None, logs
