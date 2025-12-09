import time
import asyncio
from binance import Client
from config import OI_THRESHOLD, PRICE_THRESHOLD, VOLUME_THRESHOLD
from models import symbol_state, oi_history, price_history

async def check_oi_condition(symbol, now):
    # 確認歷史資料是否足夠
    hist = oi_history[symbol]
    if len(hist) < 2: return False, None
    # 確認幣種最新持倉量是否有資料
    cur = symbol_state[symbol]["last_oi"]
    if cur <= 0: return False, None
    # 確認是否符合1小時監測
    old_t, old_oi = hist[0]
    pct = (cur - old_oi) / old_oi * 100
    if now - old_t < 3600 or old_oi <= 0: return False, pct
    return pct > OI_THRESHOLD, pct

async def check_price_condition(symbol, now):
    # 確認歷史資料是否足夠
    hist = price_history[symbol]
    if len(hist) < 2: return False, None
    # 確認幣種最新價格是否有資料
    cur = symbol_state[symbol]["last_price"]
    if cur <= 0: return False, None
    # 確認是否符合15分鐘監測
    old_t, old_p = hist[0]
    pct = (cur - old_p) / old_p * 100
    if now - old_t < 900 or old_p <= 0: return False, pct
    return pct > PRICE_THRESHOLD, pct

async def check_kline_overfulfil(symbol, kline):
    ema15 = symbol_state[symbol][f"ema_{kline}"][15]
    ema30 = symbol_state[symbol][f"ema_{kline}"][30]
    ema45 = symbol_state[symbol][f"ema_{kline}"][45]
    ema60 = symbol_state[symbol][f"ema_{kline}"][60]
    cur = symbol_state[symbol]["last_price"]
    if ema15 is not None and ema30 is not None and ema45 is not None and ema60 is not None and ema15 > ema30 > ema45 > ema60 and cur > ema45:
        return True

async def check_conditions(client, sym):
    state = symbol_state[sym]
    if state["last_price"] is None or state["last_oi"] is None:
        return None
    if time.time() - state["monitor_start"] < 60:
        return None
    if not await check_kline_overfulfil(sym, "1h"):
        return None

    now = time.time()
    vol_ratio = 0
    try:
        vol_deque = state["volume_5m"]
        if len(vol_deque) < 24:
            return None  # 資料不足，不觸發
        # 最近 1 小時（60 根）的總成交量
        current_vol = sum(list(vol_deque)[-60:])
        # 除了最近 1 小時以外的所有 1m K 成交量
        prev_volumes = list(vol_deque)[:-60]
        # 計算「前面所有資料」的平均「每小時」成交量
        # 每 60 根 1m K = 1 小時，所以總根數除以 60 = 有幾個完整小時
        prev_hour_count = max(1, len(prev_volumes) // 60)
        avg_vol = sum(prev_volumes) / prev_hour_count if prev_volumes else 1
        if avg_vol > 0 and current_vol > avg_vol * VOLUME_THRESHOLD:
            vol_ratio = current_vol / avg_vol
            oi_met, oi_pct = await check_oi_condition(sym, now)
            price_met, price_pct = await check_price_condition(sym, now)
            kline_4h = await check_kline_overfulfil(sym, "4h")

            reasons = []

            if price_met:
                reasons.append(f"成交量暴增 {vol_ratio or 0:.1f}×\n價格異動 {price_pct or 0:+.2f}%\n持倉變化 {oi_pct or 0:+.1f}%")
                if kline_4h:
                    reasons.append("4小時呈多頭趨勢")
                return {
                    "price_pct": price_pct,
                    "oi_pct": oi_pct,
                    "reason": reasons
                }
            else: 
                return None
    except:
        return None

# 手動檢查條件（帶詳細日誌）
async def check_conditions_manual(client, sym):
    """手動檢查條件，返回詳細日誌"""
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
        # 取倒數60筆
        current_vol = sum(list(state["volume_5m"])[-12:])
        # 取"撇除"倒數60筆
        prev_volumes = list(state["volume_5m"])[:-12]
        avg_vol = sum(prev_volumes) / len(prev_volumes) if prev_volumes else 1
        
        logs.append(f"📊 當前成交量：{current_vol:,.0f}")
        logs.append(f"📊 平均成交量：{avg_vol:,.0f}")
        logs.append(f"📊 成交量閥值：{VOLUME_THRESHOLD}倍")
        
        if avg_vol <= 0:
            logs.append("❌ 平均成交量為 0，無法比較")
            return None, logs
            
        vol_ratio = current_vol / avg_vol
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
            result = {
                "price_pct": price_pct,
                "oi_pct": oi_pct,
                "reason": [f"成交量暴增 {vol_ratio:.1f}×\n價格異動 {price_pct:+.2f}%\n持倉變化 {oi_pct or 0:+.1f}%"]
            }
            return result, logs
        else:
            logs.append("❌ 價格條件未通過，不會觸發告警")
            return None, logs
            
    except Exception as e:
        logs.append(f"❌ 檢查過程發生錯誤：{str(e)}")
        return None, logs
