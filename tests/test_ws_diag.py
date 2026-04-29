"""WebSocket 診斷腳本

此腳本不是自動化測試，需要手動執行（需要網路連線至 Binance）：

    python tests/test_ws_diag.py

測試兩種連線方式，各跑 30 秒：
  A. 直接 websockets.connect() → 繞過 python-binance，排除 BinanceSocketManager 問題
  B. BinanceSocketManager.futures_multiplex_socket() → 與正式程式相同路徑

診斷結論：
  A 有資料、B 沒有 → BinanceSocketManager 有問題，考慮改用原始 websockets
  A 也沒資料      → 網路問題或 Binance 端問題（確認能否連到 fstream.binance.com）
"""

import asyncio
import json
import time

TEST_SYMBOL = "btcusdt"
TEST_DURATION = 30  # 秒


# ── 方案 A：raw websockets ─────────────────────────────────────────────────────

async def test_raw_websockets():
    try:
        import websockets
    except ImportError:
        print("[A] websockets 未安裝，跳過")
        return

    url = (
        f"wss://fstream.binance.com/stream"
        f"?streams={TEST_SYMBOL}@markPrice/{TEST_SYMBOL}@kline_1m"
    )
    print(f"\n[A] raw websockets 連線中...\n    {url}")

    count = 0
    start = time.time()
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            print("[A] 連線成功，開始接收（30 秒）...")
            while time.time() - start < TEST_DURATION:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                    count += 1
                    stream = msg.get("stream", "?")
                    if count <= 5:
                        print(f"[A] #{count} stream={stream}")
                    elif count == 6:
                        print("[A] ... (後續省略，持續計數)")
                except asyncio.TimeoutError:
                    elapsed = int(time.time() - start)
                    print(f"[A] 5 秒無訊息（已過 {elapsed}s）")
    except Exception as e:
        print(f"[A] 連線失敗: {e}")

    print(f"[A] 結果：{TEST_DURATION} 秒內收到 {count} 筆訊息")
    return count


# ── 方案 B：BinanceSocketManager ──────────────────────────────────────────────

async def test_binance_socket_manager():
    try:
        from binance import AsyncClient, BinanceSocketManager
    except ImportError:
        print("[B] python-binance 未安裝，跳過")
        return

    print(f"\n[B] BinanceSocketManager 連線中...")

    count = 0
    start = time.time()
    try:
        client = await AsyncClient.create()
        bm = BinanceSocketManager(client, user_timeout=60)
        streams = [f"{TEST_SYMBOL}@markPrice", f"{TEST_SYMBOL}@kline_1m"]

        async with bm.futures_multiplex_socket(streams) as stream:
            print("[B] 連線成功，開始接收（30 秒）...")
            while time.time() - start < TEST_DURATION:
                try:
                    msg = await asyncio.wait_for(stream.recv(), timeout=5)
                    if not msg or "data" not in msg:
                        print(f"[B] 收到空/無效訊息: {msg}")
                        continue
                    count += 1
                    stream_name = msg.get("stream", "?")
                    if count <= 5:
                        print(f"[B] #{count} stream={stream_name}")
                    elif count == 6:
                        print("[B] ... (後續省略，持續計數)")
                except asyncio.TimeoutError:
                    elapsed = int(time.time() - start)
                    print(f"[B] 5 秒無訊息（已過 {elapsed}s）")
        await client.close_connection()
    except Exception as e:
        print(f"[B] 連線或接收失敗: {e}")

    print(f"[B] 結果：{TEST_DURATION} 秒內收到 {count} 筆訊息")
    return count


# ── 主程式 ────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Binance Futures WebSocket 診斷")
    print(f"測試幣種：{TEST_SYMBOL.upper()}，每段 {TEST_DURATION} 秒")
    print("=" * 60)

    count_a = await test_raw_websockets()
    count_b = await test_binance_socket_manager()

    print("\n" + "=" * 60)
    print("診斷結論：")
    if count_a and count_a > 0 and (count_b is None or count_b == 0):
        print("  ✓ A 有資料、B 沒有 → BinanceSocketManager 有問題")
        print("    建議：改用 raw websockets 方案")
    elif count_a and count_a > 0 and count_b and count_b > 0:
        print("  ✓ A 和 B 都有資料 → WebSocket 正常，問題在其他地方")
    elif (count_a is None or count_a == 0) and count_b and count_b > 0:
        print("  ✓ B 有資料、A 沒有 → 罕見情況，websockets 有問題")
    else:
        print("  ✗ A 和 B 都沒有資料 → 網路問題或 Binance 端問題")
        print("    請確認：1) 能否 ping fstream.binance.com  2) 是否需要代理")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
