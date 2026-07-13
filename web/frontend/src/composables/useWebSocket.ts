import { onMounted, onUnmounted } from 'vue'

export function useWebSocket(onMessage: (data: unknown) => void) {
  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let destroyed = false

  function connect() {
    if (destroyed) return
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${protocol}://${location.host}/ws`)

    ws.onmessage = (event) => {
      try {
        onMessage(JSON.parse(event.data))
      } catch {}
    }

    ws.onclose = () => {
      if (destroyed) return
      reconnectTimer = setTimeout(connect, 3000)
    }

    ws.onerror = () => {
      ws?.close()
    }
  }

  onMounted(connect)

  onUnmounted(() => {
    destroyed = true
    if (reconnectTimer) clearTimeout(reconnectTimer)
    ws?.close()
  })
}
