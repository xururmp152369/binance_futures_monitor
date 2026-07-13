<script setup lang="ts">
import { Dialog, DialogPanel, DialogTitle, TransitionRoot, TransitionChild } from '@headlessui/vue'

const props = defineProps<{
  symbol: string
  open: boolean
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

function tvSymbol(symbol: string) {
  return `BINANCE:${symbol}.P`
}

function tvUrl(symbol: string) {
  const params = new URLSearchParams({
    symbol: tvSymbol(symbol),
    interval: '60',
    theme: 'dark',
    style: '1',
    locale: 'en',
    toolbar_bg: '#1a1a2e',
    hide_top_toolbar: '0',
    hide_side_toolbar: '0',
    allow_symbol_change: '0',
    studies: 'MAExp@tv-basicstudies',
  })
  return `https://s.tradingview.com/widgetembed/?${params.toString()}`
}

function binanceUrl(symbol: string) {
  return `https://www.binance.com/en/futures/${symbol}`
}
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
              <iframe
                :src="tvUrl(symbol)"
                :key="symbol"
                class="w-full h-full border-0"
                allowtransparency="true"
                frameborder="0"
                scrolling="no"
              />
            </div>
          </DialogPanel>
        </TransitionChild>
      </div>
    </Dialog>
  </TransitionRoot>
</template>
