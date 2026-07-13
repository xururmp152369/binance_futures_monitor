<script setup lang="ts">
import { ref } from 'vue'
import ChartDialog from './ChartDialog.vue'
import type { LongBreakoutCoin, DeathCrossCoin, FibonacciCoin } from '../types'

const props = defineProps<{
  strategy: 'long_breakout' | 'death_cross' | 'fibonacci_long' | 'fibonacci_short'
  coin: LongBreakoutCoin | DeathCrossCoin | FibonacciCoin
}>()

const chartOpen = ref(false)

function fmt(v: number | null | undefined, decimals = 2) {
  if (v == null) return '—'
  return v.toFixed(decimals)
}

function fmtPrice(v: number | null | undefined) {
  if (v == null) return '—'
  return v >= 1000
    ? v.toLocaleString('en-US', { maximumFractionDigits: 2 })
    : v.toFixed(v >= 1 ? 2 : 6)
}

function fmtTime(ts: number | null | undefined) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${mm}/${dd} ${hh}:${mi}`
}

function phaseBadgeClass(phase: string) {
  const map: Record<string, string> = {
    tracking: 'bg-blue-500/20 text-blue-400',
    ready: 'bg-green-500/20 text-green-400',
    WATCHING: 'bg-yellow-500/20 text-yellow-400',
    ALERT: 'bg-red-500/20 text-red-400',
  }
  return map[phase] ?? 'bg-gray-700 text-gray-400'
}

function phaseLabel(phase: string) {
  const map: Record<string, string> = {
    tracking: 'TRACKING',
    ready: 'READY',
    WATCHING: 'WATCHING',
    ALERT: 'ALERT',
  }
  return map[phase] ?? phase.toUpperCase()
}

const lb = () => props.coin as LongBreakoutCoin
const dc = () => props.coin as DeathCrossCoin
const fib = () => props.coin as FibonacciCoin

function tvMiniUrl(symbol: string) {
  const params = new URLSearchParams({
    symbol: `BINANCE:${symbol}.P`,
    interval: '60',
    theme: 'dark',
    hide_top_toolbar: '1',
    hide_side_toolbar: '1',
    allow_symbol_change: '0',
    save_image: '0',
    toolbar_bg: '#111827',
  })
  return `https://s.tradingview.com/widgetembed/?${params.toString()}`
}
</script>

<template>
  <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden hover:border-gray-700 transition-colors flex flex-col">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 pt-3 pb-1">
      <span class="font-bold text-white tracking-wide">{{ coin.symbol }}</span>
      <span
        v-if="strategy !== 'fibonacci_long' && strategy !== 'fibonacci_short'"
        :class="['text-xs px-2 py-0.5 rounded-full font-medium', phaseBadgeClass((coin as LongBreakoutCoin | DeathCrossCoin).phase)]"
      >
        {{ phaseLabel((coin as LongBreakoutCoin | DeathCrossCoin).phase) }}
      </span>
    </div>

    <!-- Mini TradingView thumbnail -->
    <div class="mx-3 my-2 rounded overflow-hidden bg-gray-800" style="height: 80px;">
      <iframe
        :src="tvMiniUrl(coin.symbol)"
        :key="coin.symbol"
        class="w-full border-0 pointer-events-none"
        style="height: 160px; margin-top: -80px;"
        scrolling="no"
        frameborder="0"
      />
    </div>

    <!-- Metrics -->
    <div class="px-4 pb-3 flex-1 space-y-1 text-xs text-gray-400">

      <!-- Long Breakout -->
      <template v-if="strategy === 'long_breakout'">
        <div class="grid grid-cols-2 gap-x-4 gap-y-0.5">
          <div>觸發時間 <span class="text-gray-200">{{ fmtTime(lb().trigger_time_ts) }}</span></div>
          <div>漲幅 <span class="text-green-400">+{{ fmt(lb().gain_pct) }}%</span></div>
          <div>量能 <span class="text-gray-200">{{ fmt(lb().volume_ratio) }}x</span></div>
          <div>Taker 買 <span class="text-gray-200">{{ fmt((lb().taker_buy_ratio ?? 0) * 100) }}%</span></div>
          <div>盤整 <span class="text-gray-200">{{ lb().consolidation_hours ?? '—' }}h</span></div>
          <div v-if="lb().is_method_b" class="text-purple-400">Method B</div>
          <div v-else class="opacity-0">—</div>
        </div>
        <div class="border-t border-gray-800 pt-1 grid grid-cols-2 gap-x-4">
          <div>底部 <span class="text-gray-200">{{ fmtPrice(lb().consolidation_low) }}</span></div>
          <div>頂部 <span class="text-gray-200">{{ fmtPrice(lb().consolidation_high) }}</span></div>
          <div>現價 <span class="text-gray-200">{{ fmtPrice(lb().current_price) }}</span></div>
          <div>
            距頂
            <span :class="(lb().distance_from_top_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'">
              {{ lb().distance_from_top_pct != null ? (lb().distance_from_top_pct! >= 0 ? '+' : '') + fmt(lb().distance_from_top_pct) + '%' : '—' }}
            </span>
          </div>
        </div>
      </template>

      <!-- Death Cross -->
      <template v-else-if="strategy === 'death_cross'">
        <div class="grid grid-cols-2 gap-x-4 gap-y-0.5">
          <div>T0 時間 <span class="text-gray-200">{{ fmtTime(dc().alert_time_ts) }}</span></div>
          <div>T0 收盤 <span class="text-gray-200">{{ fmtPrice(dc().close_t0) }}</span></div>
          <div>已過 <span class="text-gray-200">{{ dc().alert_elapsed_hours ?? '—' }}h</span></div>
          <div>窗口 <span class="text-gray-200">{{ dc().alert_window_hours }}h</span></div>
          <div>進場 <span class="text-gray-200">{{ dc().entry_count }}/{{ dc().max_entries }}</span></div>
          <div>現價 <span class="text-gray-200">{{ fmtPrice(dc().current_price) }}</span></div>
        </div>
        <div v-if="dc().alert_time_ts" class="mt-1">
          <div class="w-full bg-gray-800 rounded-full h-1.5">
            <div
              class="bg-red-500 h-1.5 rounded-full"
              :style="`width: ${Math.min(100, ((dc().alert_elapsed_hours ?? 0) / dc().alert_window_hours) * 100)}%`"
            />
          </div>
          <div class="text-right text-gray-500 mt-0.5">
            剩餘 {{ Math.max(0, dc().alert_window_hours - (dc().alert_elapsed_hours ?? 0)).toFixed(1) }}h
          </div>
        </div>
      </template>

      <!-- Fibonacci Long -->
      <template v-else-if="strategy === 'fibonacci_long'">
        <div class="grid grid-cols-1 gap-y-0.5">
          <div>多單 bar9 <span class="text-gray-200">{{ fib().long_reset_bar_time ? fmtTime(fib().long_reset_bar_time / 1000) : '—' }}</span></div>
          <div>現價 <span class="text-gray-200">{{ fmtPrice(fib().current_price) }}</span></div>
        </div>
      </template>

      <!-- Fibonacci Short -->
      <template v-else>
        <div class="grid grid-cols-1 gap-y-0.5">
          <div>空單 bar9 <span class="text-gray-200">{{ fib().short_reset_bar_time ? fmtTime(fib().short_reset_bar_time / 1000) : '—' }}</span></div>
          <div>現價 <span class="text-gray-200">{{ fmtPrice(fib().current_price) }}</span></div>
        </div>
      </template>
    </div>

    <!-- Actions -->
    <div class="px-4 pb-3 flex gap-2">
      <button
        @click="chartOpen = true"
        class="flex-1 text-xs py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded transition-colors"
      >
        開啟線圖
      </button>
      <a
        :href="`https://www.binance.com/en/futures/${coin.symbol}`"
        target="_blank"
        rel="noopener"
        class="text-xs px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-yellow-400 rounded border border-gray-700 transition-colors"
      >
        Binance
      </a>
    </div>
  </div>

  <ChartDialog :symbol="coin.symbol" :open="chartOpen" @close="chartOpen = false" />
</template>
