<script lang="ts">
// Module-level: shared across all ChartDialog instances so tv.js loads only once
let scriptPromise: Promise<void> | null = null

function loadTvScript(): Promise<void> {
  if ((window as any).TradingView) return Promise.resolve()
  if (!scriptPromise) {
    scriptPromise = new Promise<void>((resolve, reject) => {
      const script = document.createElement('script')
      script.src = 'https://s3.tradingview.com/tv.js'
      script.onload = () => resolve()
      script.onerror = () => reject(new Error('tv.js failed to load'))
      document.head.appendChild(script)
    })
  }
  return scriptPromise
}
</script>

<script setup lang="ts">
import { ref, watch, nextTick, onUnmounted } from 'vue'
import { Dialog, DialogPanel, DialogTitle, TransitionRoot, TransitionChild } from '@headlessui/vue'

const props = defineProps<{
  symbol: string
  open: boolean
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const containerId = `tv_${Math.random().toString(36).slice(2, 9)}`
const chartRef = ref<HTMLElement | null>(null)
let widget: unknown = null

function binanceUrl(symbol: string) {
  return `https://www.binance.com/en/futures/${symbol}`
}

function clearWidget() {
  if (widget) {
    try { (widget as any).remove() } catch {}
    widget = null
  }
  if (chartRef.value) chartRef.value.innerHTML = ''
}

function initWidget() {
  if (!chartRef.value || !(window as any).TradingView) return
  clearWidget()
  const tv = (window as any).TradingView
  const wid = new tv.widget({
    container_id: containerId,
    symbol: `BINANCE:${props.symbol}.P`,
    interval: '240',
    theme: 'dark',
    style: '1',
    locale: 'en',
    toolbar_bg: '#1a1a2e',
    autosize: true,
    timezone: "Asia/Taipei",
    // 1. 為每一條 EMA 設定獨立的 uid
    studies: [
      { id: 'MAExp@tv-basicstudies', uid: "ema_15", inputs: { length: 15 } },
      { id: 'MAExp@tv-basicstudies', uid: "ema_30", inputs: { length: 30 } },
      { id: 'MAExp@tv-basicstudies', uid: "ema_45", inputs: { length: 45 } },
      { id: 'MAExp@tv-basicstudies', uid: "ema_60", inputs: { length: 60 } },
      { id: 'MAExp@tv-basicstudies', uid: "ema_200", inputs: { length: 200 } },
    ],
    // 2. 使用 studies_overrides 透過 uid 精準控制每條線的顏色
    studies_overrides: {
      "ema_15.plot.color": '#F97316',
      "ema_30.plot.color": '#3B82F6',
      "ema_45.plot.color": '#A855F7',
      "ema_60.plot.color": '#EF4444',
      "ema_200.plot.color":'#22C55E',
    }
  })
  console.log('TradingView widget initialized:', wid)
}

watch(() => props.open, async (isOpen) => {
  if (!isOpen) { clearWidget(); return }
  await nextTick()
  try {
    await loadTvScript()
    initWidget()
  } catch (err) {
    console.error('TradingView widget failed to load:', err)
  }
})

onUnmounted(clearWidget)
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
          <DialogPanel class="w-[90vw] h-[80vh] bg-gray-900 rounded-xl shadow-2xl flex flex-col overflow-hidden">
            <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700">
              <DialogTitle class="text-base font-semibold text-white">
                {{ symbol }} 線圖
              </DialogTitle>
              <div class="flex items-center gap-2">
                <a
                  :href="binanceUrl(symbol)"
                  target="_blank"
                  rel="noopener"
                  class="text-xs px-2 py-1 bg-yellow-500/20 text-yellow-400 rounded hover:bg-yellow-500/30 transition-colors"
                >
                  Binance ↗
                </a>
                <button
                  @click="emit('close')"
                  class="text-gray-400 hover:text-white transition-colors text-xl leading-none"
                >
                  ×
                </button>
              </div>
            </div>
            <div class="flex-1 min-h-0">
              <div :id="containerId" ref="chartRef" class="w-full h-full" />
            </div>
          </DialogPanel>
        </TransitionChild>
      </div>
    </Dialog>
  </TransitionRoot>
</template>
