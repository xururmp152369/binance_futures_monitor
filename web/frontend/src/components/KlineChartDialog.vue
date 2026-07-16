<script setup lang="ts">
import { ref, watch, nextTick, onUnmounted } from 'vue'
import { Dialog, DialogPanel, DialogTitle, TransitionRoot, TransitionChild } from '@headlessui/vue'
import { createChart, CrosshairMode, LineStyle } from 'lightweight-charts'
import type {
  IChartApi, ISeriesApi, IPriceLine,
  UTCTimestamp, CandlestickData, LineData,
} from 'lightweight-charts'

const props = defineProps<{
  symbol: string
  open: boolean
  currentPrice?: number | null
  pumpCandleTime?: number | null
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const chartRef = ref<HTMLElement | null>(null)
const loading = ref(false)
const error = ref('')
const interval = ref('4h')

const INTERVALS = ['15m', '1h', '4h', '1d'] as const

const EMA_CONFIGS = [
  { period: 15,  color: '#f97316' },
  { period: 30,  color: '#3b82f6' },
  { period: 45,  color: '#a855f7' },
  { period: 60,  color: '#ef4444' },
  { period: 200, color: '#22c55e' },
] as const

interface Kline {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

interface OhlcInfo {
  time: number
  open: number
  high: number
  low: number
  close: number
  change: number
  changePct: number
}

const ohlcInfo = ref<OhlcInfo | null>(null)
const emaInfo = ref<Record<number, number | null>>({ 15: null, 30: null, 45: null, 60: null, 200: null })

let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let emaSeries: { period: number; series: ISeriesApi<'Line'> }[] = []
let emaPriceLines: Map<number, IPriceLine> = new Map()
let emaLastValues: Map<number, number> = new Map()
let allCandles: Kline[] = []
let lastCandle: Kline | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

function snapTo4h(ts: number): number {
  return Math.floor(ts / (4 * 3600)) * (4 * 3600)
}

function fmtLocalTime(ts: number): string {
  const d = new Date(ts * 1000)
  const yyyy = d.getFullYear()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '—'
  if (Math.abs(v) >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  return v.toFixed(Math.abs(v) >= 1 ? 2 : 6)
}

function fmtChange(v: number): string {
  const sign = v >= 0 ? '+' : '-'
  const abs = Math.abs(v)
  if (abs >= 1000) return sign + abs.toLocaleString('en-US', { maximumFractionDigits: 2 })
  return sign + abs.toFixed(abs >= 1 ? 2 : 6)
}

function calcEma(candles: Kline[], period: number): { time: UTCTimestamp; value: number }[] {
  if (candles.length < period) return []
  const k = 2 / (period + 1)
  const result: { time: UTCTimestamp; value: number }[] = []
  let ema = candles.slice(0, period).reduce((s, c) => s + c.close, 0) / period
  result.push({ time: candles[period - 1].time as UTCTimestamp, value: ema })
  for (let i = period; i < candles.length; i++) {
    ema = candles[i].close * k + ema * (1 - k)
    result.push({ time: candles[i].time as UTCTimestamp, value: ema })
  }
  return result
}

function clearChart() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null }
  candleSeries = null
  emaSeries = []
  emaPriceLines.clear()
  emaLastValues.clear()
  allCandles = []
  lastCandle = null
  ohlcInfo.value = null
  emaInfo.value = { 15: null, 30: null, 45: null, 60: null, 200: null }
  if (chart) { chart.remove(); chart = null }
}

async function refreshLastCandle() {
  if (!candleSeries || !lastCandle) return
  try {
    const res = await fetch(`/api/chart/klines?symbol=${props.symbol}&interval=${interval.value}&limit=1`)
    if (!res.ok) return
    const latest: Kline[] = await res.json()
    if (!latest.length) return
    const current = latest[0]
    if (current.time === lastCandle.time) {
      // Same candle — update close/high/low
      candleSeries.update({
        time: current.time as UTCTimestamp,
        open: current.open,
        high: current.high,
        low: current.low,
        close: current.close,
      })
      lastCandle = { ...lastCandle, ...current }
    } else {
      // New candle opened — full refresh to recalculate EMA
      await initChart()
    }
  } catch { /* network error, skip */ }
}

function applyLegend(candle: Kline) {
  const idx = allCandles.findIndex(c => c.time === candle.time)
  const prevClose = idx > 0 ? allCandles[idx - 1].close : candle.open
  ohlcInfo.value = {
    time: candle.time,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
    change: candle.close - prevClose,
    changePct: (candle.close - prevClose) / prevClose * 100,
  }
  for (const [period, val] of emaLastValues) {
    emaInfo.value[period] = val
  }
}

async function initChart() {
  if (!chartRef.value) return
  clearChart()
  loading.value = true
  error.value = ''

  const iv = interval.value
  const limit = iv === '1d' ? 365 : 500
  try {
    const res = await fetch(`/api/chart/klines?symbol=${props.symbol}&interval=${iv}&limit=${limit}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    allCandles = await res.json()
  } catch {
    error.value = '載入 K 線失敗'
    loading.value = false
    return
  }
  if (!chartRef.value || !allCandles.length) { loading.value = false; return }

  chart = createChart(chartRef.value, {
    autoSize: true,
    layout: { background: { color: '#0f172a' }, textColor: '#94a3b8' },
    grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: '#475569', labelBackgroundColor: '#1e293b' },
      horzLine: { color: '#475569', labelBackgroundColor: '#1e293b' },
    },
    rightPriceScale: { borderColor: '#334155' },
    timeScale: {
      borderColor: '#334155',
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time: number) => {
        const d = new Date(time * 1000)
        const mm = String(d.getMonth() + 1).padStart(2, '0')
        const dd = String(d.getDate()).padStart(2, '0')
        const hh = String(d.getHours()).padStart(2, '0')
        const mi = String(d.getMinutes()).padStart(2, '0')
        return iv === '1d' ? `${mm}/${dd}` : `${mm}/${dd} ${hh}:${mi}`
      },
    },
    localization: { timeFormatter: fmtLocalTime },
  })

  // 1d 日線與 4H 邊界不對齊，跳過起漲標記
  const pumpTime = (props.pumpCandleTime && iv !== '1d')
    ? snapTo4h(props.pumpCandleTime)
    : null

  // Candlestick series
  candleSeries = chart.addCandlestickSeries({
    upColor: '#22c55e',
    downColor: '#ef4444',
    borderVisible: false,
    wickUpColor: '#22c55e',
    wickDownColor: '#ef4444',
  })
  candleSeries.setData(allCandles.map(c => ({
    time: c.time as UTCTimestamp,
    open: c.open, high: c.high, low: c.low, close: c.close,
    ...(pumpTime && c.time === pumpTime
      ? { color: '#ffffff', borderColor: '#ffffff', wickColor: '#ffffff' }
      : {}),
  })))

  if (pumpTime) {
    candleSeries.setMarkers([{
      time: pumpTime as UTCTimestamp,
      position: 'belowBar',
      color: '#ffffff',
      shape: 'arrowUp',
      text: '起漲K',
      size: 2,
    }])
  }

  // Volume histogram (bottom 25% of pane)
  const volSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  })
  volSeries.priceScale().applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } })
  volSeries.setData(allCandles.map(c => ({
    time: c.time as UTCTimestamp,
    value: c.volume,
    color: c.close >= c.open ? '#22c55e55' : '#ef444455',
  })))

  // EMA lines + right-axis price labels (lineVisible: false shows only axis label, no horizontal line)
  for (const { period, color } of EMA_CONFIGS) {
    const emaData = calcEma(allCandles, period)
    const s = chart.addLineSeries({
      color,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })
    s.setData(emaData)
    emaSeries.push({ period, series: s })

    if (emaData.length > 0) {
      const lastVal = emaData[emaData.length - 1].value
      emaLastValues.set(period, lastVal)
      const pl = s.createPriceLine({
        price: lastVal,
        color,
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        lineVisible: false,       // hide horizontal line — keep only the axis label
        axisLabelVisible: true,
        title: `${period}`,
      })
      emaPriceLines.set(period, pl)
    }
  }

  lastCandle = allCandles[allCandles.length - 1]

  // Crosshair: update OHLC legend + EMA axis labels on hover
  chart.subscribeCrosshairMove((param) => {
    if (!param.point || !param.time || !candleSeries) {
      // Cursor left chart — restore last-candle values
      for (const [period, lastVal] of emaLastValues) {
        emaPriceLines.get(period)?.applyOptions({ price: lastVal })
        emaInfo.value[period] = lastVal
      }
      if (lastCandle) applyLegend(lastCandle)
      return
    }

    const cd = param.seriesData.get(candleSeries) as CandlestickData | undefined
    if (cd) {
      const t = cd.time as number
      const idx = allCandles.findIndex(c => c.time === t)
      const prevClose = idx > 0 ? allCandles[idx - 1].close : cd.open
      ohlcInfo.value = {
        time: t,
        open: cd.open, high: cd.high, low: cd.low, close: cd.close,
        change: cd.close - prevClose,
        changePct: (cd.close - prevClose) / prevClose * 100,
      }
    }

    for (const { period, series } of emaSeries) {
      const ld = param.seriesData.get(series) as LineData | undefined
      if (ld != null) {
        emaPriceLines.get(period)?.applyOptions({ price: ld.value })
        emaInfo.value[period] = ld.value
      }
    }
  })

  applyLegend(lastCandle)
  chart.timeScale().fitContent()
  loading.value = false

  // Self-contained refresh: poll every 5 s regardless of parent data
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = setInterval(refreshLastCandle, 5000)
}

// Live price update — update last candle close in real time
watch(() => props.currentPrice, (price) => {
  if (!price || !candleSeries || !lastCandle) return
  const updated = {
    time: lastCandle.time as UTCTimestamp,
    open: lastCandle.open,
    high: Math.max(lastCandle.high, price),
    low: Math.min(lastCandle.low, price),
    close: price,
  }
  candleSeries.update(updated)
  lastCandle = { ...lastCandle, ...updated }
})

watch([() => props.open, interval], async ([isOpen]) => {
  if (!isOpen) { clearChart(); return }
  await nextTick()
  await initChart()
})

onUnmounted(clearChart)
</script>

<template>
  <TransitionRoot :show="open" as="template">
    <Dialog @close="emit('close')" class="relative z-50">
      <TransitionChild
        enter="ease-out duration-200" enter-from="opacity-0" enter-to="opacity-100"
        leave="ease-in duration-150" leave-from="opacity-100" leave-to="opacity-0"
      >
        <div class="fixed inset-0 bg-black/70" />
      </TransitionChild>

      <div class="fixed inset-0 flex items-center justify-center p-4">
        <TransitionChild
          enter="ease-out duration-200" enter-from="opacity-0 scale-95" enter-to="opacity-100 scale-100"
          leave="ease-in duration-150" leave-from="opacity-100 scale-100" leave-to="opacity-0 scale-95"
        >
          <DialogPanel class="w-[90vw] h-[85vh] bg-[#0f172a] rounded-xl shadow-2xl flex flex-col overflow-hidden border border-gray-800">

            <!-- Header: symbol + interval switcher + actions -->
            <div class="flex items-center justify-between px-3 py-2 border-b border-gray-800 shrink-0">
              <DialogTitle class="text-sm font-bold text-white tracking-wide">{{ symbol }}</DialogTitle>
              <div class="flex items-center gap-2">
                <div class="flex gap-1">
                  <button
                    v-for="iv in INTERVALS" :key="iv"
                    @click="interval = iv"
                    :class="[
                      'text-xs px-2 py-0.5 rounded font-medium transition-colors',
                      interval === iv
                        ? 'bg-indigo-600 text-white'
                        : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200',
                    ]"
                  >{{ iv }}</button>
                </div>
                <a
                  :href="`https://www.binance.com/en/futures/${symbol}`"
                  target="_blank" rel="noopener"
                  class="text-xs px-2 py-0.5 bg-yellow-500/20 text-yellow-400 rounded hover:bg-yellow-500/30 transition-colors"
                >Binance ↗</a>
                <button @click="emit('close')" class="text-gray-400 hover:text-white text-xl leading-none">×</button>
              </div>
            </div>

            <!-- OHLC + EMA legend (updates on crosshair move) -->
            <div class="px-3 py-1.5 border-b border-gray-800 bg-gray-900/40 shrink-0 min-h-[3.25rem]">
              <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs font-mono">
                <span class="text-gray-500">{{ ohlcInfo ? fmtLocalTime(ohlcInfo.time) : '' }}</span>
                <template v-if="ohlcInfo">
                  <span class="text-gray-400">O<span class="text-gray-100 ml-0.5">{{ fmtPrice(ohlcInfo.open) }}</span></span>
                  <span class="text-gray-400">H<span class="text-green-400 ml-0.5">{{ fmtPrice(ohlcInfo.high) }}</span></span>
                  <span class="text-gray-400">L<span class="text-red-400 ml-0.5">{{ fmtPrice(ohlcInfo.low) }}</span></span>
                  <span class="text-gray-400">C<span class="text-gray-100 ml-0.5">{{ fmtPrice(ohlcInfo.close) }}</span></span>
                  <span :class="ohlcInfo.change >= 0 ? 'text-green-400' : 'text-red-400'">
                    {{ fmtChange(ohlcInfo.change) }} ({{ ohlcInfo.changePct >= 0 ? '+' : '' }}{{ ohlcInfo.changePct.toFixed(2) }}%)
                  </span>
                </template>
              </div>
              <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 mt-0.5 text-xs font-mono">
                <span
                  v-for="cfg in EMA_CONFIGS" :key="cfg.period"
                  :style="`color: ${cfg.color}`"
                >
                  EMA{{ cfg.period }}
                  <span class="opacity-90">{{ emaInfo[cfg.period] != null ? fmtPrice(emaInfo[cfg.period]) : '—' }}</span>
                </span>
              </div>
            </div>

            <!-- Chart canvas -->
            <div class="flex-1 min-h-0 relative">
              <div v-if="loading" class="absolute inset-0 flex items-center justify-center text-gray-500 text-sm">載入中...</div>
              <div v-else-if="error" class="absolute inset-0 flex items-center justify-center text-red-400 text-sm">{{ error }}</div>
              <div ref="chartRef" class="w-full h-full" />
            </div>

          </DialogPanel>
        </TransitionChild>
      </div>
    </Dialog>
  </TransitionRoot>
</template>
