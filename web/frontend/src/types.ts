export interface LongBreakoutCoin {
  symbol: string
  phase: string
  trigger_time_ts: number | null
  gain_pct: number | null
  volume_ratio: number | null
  taker_buy_ratio: number | null
  is_method_b: boolean
  consolidation_hours: number | null
  consolidation_low: number | null
  consolidation_high: number | null
  current_price: number | null
  distance_from_top_pct: number | null
}

export interface DeathCrossCoin {
  symbol: string
  phase: string
  alert_time_ts: number | null
  close_t0: number | null
  entry_count: number
  max_entries: number
  alert_elapsed_hours: number | null
  alert_window_hours: number
  current_price: number | null
}

export interface FibonacciCoin {
  symbol: string
  long_reset_bar_time: number
  short_reset_bar_time: number
  current_price: number | null
}

export interface PaginatedResponse<T> {
  total: number
  page: number
  per_page: number
  items: T[]
}

export interface HealthResponse {
  status: string
  total_symbols: number
  long_breakout_tracking: number
  long_breakout_ready: number
  death_cross_watching: number
  death_cross_alert: number
  fibonacci_symbols: number
}
