<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import CoinCard from '../components/CoinCard.vue'
import Pagination from '../components/Pagination.vue'
import { useWebSocket } from '../composables/useWebSocket'
import type {
  LongBreakoutCoin, DeathCrossCoin, FibonacciCoin,
  PaginatedResponse, HealthResponse,
} from '../types'

type Strategy = 'long_breakout' | 'death_cross' | 'fibonacci_long' | 'fibonacci_short'

const strategies: { key: Strategy; label: string }[] = [
  { key: 'long_breakout', label: 'Long Breakout' },
  { key: 'death_cross', label: 'Death Cross' },
  { key: 'fibonacci_long', label: 'Fib Long' },
  { key: 'fibonacci_short', label: 'Fib Short' },
]

const phaseOptions: Record<Strategy, { value: string; label: string }[]> = {
  long_breakout: [
    { value: 'ALL', label: '全部' },
    { value: 'TRACKING', label: 'Tracking' },
    { value: 'READY', label: 'Ready' },
  ],
  death_cross: [
    { value: 'ALL', label: '全部' },
    { value: 'WATCHING', label: 'Watching' },
    { value: 'ALERT', label: 'Alert' },
  ],
  fibonacci_long: [{ value: 'ALL', label: '全部' }],
  fibonacci_short: [{ value: 'ALL', label: '全部' }],
}

const activeStrategy = ref<Strategy>('long_breakout')
const activePhase = ref('ALL')
const page = ref(1)
const perPage = ref(25)
const health = ref<HealthResponse | null>(null)

const items = ref<(LongBreakoutCoin | DeathCrossCoin | FibonacciCoin)[]>([])
const total = ref(0)
const loading = ref(false)

watch(activeStrategy, () => {
  activePhase.value = 'ALL'
  page.value = 1
})
watch([activePhase], () => { page.value = 1 })

function apiPath(s: Strategy) {
  if (s === 'fibonacci_long') return '/api/strategies/fibonacci?direction=long'
  if (s === 'fibonacci_short') return '/api/strategies/fibonacci?direction=short'
  return `/api/strategies/${s}`
}

async function fetchData() {
  loading.value = true
  try {
    const s = activeStrategy.value
    const base = apiPath(s)
    const sep = base.includes('?') ? '&' : '?'
    const url = `${base}${sep}phase=${activePhase.value}&page=${page.value}&per_page=${perPage.value}`
    const res = await fetch(url)
    const data: PaginatedResponse<LongBreakoutCoin | DeathCrossCoin | FibonacciCoin> = await res.json()
    items.value = data.items
    total.value = data.total
  } catch (err) {
    console.error(err)
  } finally {
    loading.value = false
  }
}

async function fetchHealth() {
  try {
    const res = await fetch('/api/health')
    health.value = await res.json()
  } catch {}
}

watch([activeStrategy, activePhase, page, perPage], fetchData)

let dataTimer: ReturnType<typeof setInterval> | null = null
let healthTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  fetchData()
  fetchHealth()
  dataTimer = setInterval(fetchData, 5000)
  healthTimer = setInterval(fetchHealth, 10000)
})

onUnmounted(() => {
  if (dataTimer) clearInterval(dataTimer)
  if (healthTimer) clearInterval(healthTimer)
})

useWebSocket(() => {
  fetchData()
  fetchHealth()
})

const badgeCounts = computed(() => {
  if (!health.value) return {} as Record<Strategy, number>
  return {
    long_breakout: health.value.long_breakout_tracking + health.value.long_breakout_ready,
    death_cross: health.value.death_cross_watching + health.value.death_cross_alert,
    fibonacci_long: health.value.fibonacci_symbols,
    fibonacci_short: health.value.fibonacci_symbols,
  }
})
</script>

<template>
  <div class="min-h-screen bg-gray-950 text-gray-100">
    <!-- Top bar -->
    <header class="bg-gray-900 border-b border-gray-800 px-4 py-3 flex flex-wrap items-center gap-3">
      <span class="font-bold text-indigo-400 text-lg mr-2">Binance Monitor</span>
      <template v-if="health">
        <span class="text-xs text-gray-500">{{ health.total_symbols }} 幣種</span>
      </template>
      <div class="ml-auto text-xs text-gray-600" v-if="loading">更新中...</div>
    </header>

    <main class="max-w-screen-xl mx-auto px-4 py-4">
      <!-- Strategy Tabs -->
      <div class="flex flex-wrap gap-1 mb-4 bg-gray-900 rounded-lg p-1 w-fit">
        <button
          v-for="s in strategies"
          :key="s.key"
          @click="activeStrategy = s.key"
          :class="[
            'px-4 py-1.5 rounded-md text-sm font-medium transition-colors',
            activeStrategy === s.key
              ? 'bg-indigo-600 text-white'
              : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
          ]"
        >
          {{ s.label }}
          <span
            v-if="badgeCounts[s.key]"
            class="ml-1.5 inline-flex items-center justify-center w-5 h-5 text-xs rounded-full"
            :class="activeStrategy === s.key ? 'bg-indigo-400/30 text-indigo-100' : 'bg-gray-700 text-gray-400'"
          >
            {{ badgeCounts[s.key] }}
          </span>
        </button>
      </div>

      <!-- Phase + Stats bar -->
      <div class="flex flex-wrap items-center gap-3 mb-4">
        <div class="flex gap-1">
          <button
            v-for="opt in phaseOptions[activeStrategy]"
            :key="opt.value"
            @click="activePhase = opt.value"
            :class="[
              'px-3 py-1 rounded text-xs font-medium border transition-colors',
              activePhase === opt.value
                ? 'bg-indigo-600 border-indigo-600 text-white'
                : 'bg-gray-900 border-gray-700 text-gray-400 hover:border-gray-600'
            ]"
          >
            {{ opt.label }}
          </button>
        </div>

        <!-- Health summary chips -->
        <div v-if="health" class="flex flex-wrap gap-2 text-xs ml-auto">
          <template v-if="activeStrategy === 'long_breakout'">
            <span class="px-2 py-0.5 bg-blue-500/10 text-blue-400 rounded">
              Tracking {{ health.long_breakout_tracking }}
            </span>
            <span class="px-2 py-0.5 bg-green-500/10 text-green-400 rounded">
              Ready {{ health.long_breakout_ready }}
            </span>
          </template>
          <template v-else-if="activeStrategy === 'death_cross'">
            <span class="px-2 py-0.5 bg-yellow-500/10 text-yellow-400 rounded">
              Watching {{ health.death_cross_watching }}
            </span>
            <span class="px-2 py-0.5 bg-red-500/10 text-red-400 rounded">
              Alert {{ health.death_cross_alert }}
            </span>
          </template>
          <template v-else>
            <span class="px-2 py-0.5 bg-purple-500/10 text-purple-400 rounded">
              Fib {{ health.fibonacci_symbols }}
            </span>
          </template>
        </div>
      </div>

      <!-- Grid -->
      <div
        v-if="items.length"
        class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4"
      >
        <CoinCard
          v-for="coin in items"
          :key="coin.symbol"
          :strategy="activeStrategy"
          :coin="coin"
        />
      </div>
      <div
        v-else-if="!loading"
        class="text-center text-gray-600 py-20 text-sm"
      >
        目前無符合條件的幣種
      </div>
      <div v-else class="text-center text-gray-600 py-20 text-sm">載入中...</div>

      <!-- Pagination -->
      <Pagination
        v-if="total > 0"
        :page="page"
        :per-page="perPage"
        :total="total"
        @update:page="page = $event"
        @update:per-page="perPage = $event"
      />
    </main>
  </div>
</template>
