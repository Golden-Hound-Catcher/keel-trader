/**
 * Keel monitor store (Phase U1).
 * Aggregates /health + /api/v1/{status,balance,positions,decisions,trades,events,factors/*}.
 * Never reads data/*.json or legacy r20 private routes.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useKeelApi } from '../composables/useKeelApi'
import {
  KEEL_DEFAULT_INSTRUMENTS,
  type KeelHealth,
  type KeelStatus,
  type KeelBalance,
  type KeelPosition,
  type KeelDecision,
  type KeelTrade,
  type KeelFactors,
} from '../types/keel'

export const useMonitorStore = defineStore('monitor', () => {
  const { keelFetch } = useKeelApi()

  const health = ref<KeelHealth | null>(null)
  const status = ref<KeelStatus | null>(null)
  const balance = ref<KeelBalance | null>(null)
  const positions = ref<KeelPosition[]>([])
  const positionsSource = ref<string>('')
  const decisions = ref<KeelDecision[]>([])
  const trades = ref<KeelTrade[]>([])
  const events = ref<Array<Record<string, unknown>>>([])
  const factors = ref<Record<string, KeelFactors>>({})
  const watchlist = ref<string[]>([...KEEL_DEFAULT_INSTRUMENTS])

  const loading = ref(false)
  const isRefreshing = ref(false)
  const error = ref<string | null>(null)
  const lastUpdated = ref<Date | null>(null)
  const isConnected = ref(false)
  const pollingTimer = ref<ReturnType<typeof setInterval> | null>(null)
  const activeTab = ref<'overview' | 'positions' | 'decisions' | 'trades' | 'events' | 'factors'>('overview')

  const positionCount = computed(() => positions.value.length)
  const uptimeLabel = computed(() => {
    const s = status.value?.uptime_seconds ?? 0
    if (s < 60) return `${s}s`
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    return `${h}h ${m}m`
  })

  async function fetchAll(silent = false) {
    if (!silent) isRefreshing.value = true
    loading.value = !silent && !lastUpdated.value
    const errors: string[] = []

    try {
      const results = await Promise.allSettled([
        keelFetch<KeelHealth>('/health'),
        keelFetch<KeelStatus>('/api/v1/status'),
        keelFetch<KeelBalance>('/api/v1/balance'),
        keelFetch<{ count: number; positions: KeelPosition[]; source: string }>('/api/v1/positions'),
        keelFetch<{ count: number; decisions: KeelDecision[] }>('/api/v1/decisions?limit=50'),
        keelFetch<{ count: number; trades: KeelTrade[] }>('/api/v1/trades?limit=50'),
        keelFetch<{ count: number; events: Array<Record<string, unknown>> }>('/api/v1/events?limit=50'),
      ])

      const [h, st, bal, pos, dec, tr, ev] = results

      if (h.status === 'fulfilled') health.value = h.value
      else errors.push(`health: ${h.reason?.message || h.reason}`)

      if (st.status === 'fulfilled') status.value = st.value
      else errors.push(`status: ${st.reason?.message || st.reason}`)

      if (bal.status === 'fulfilled') balance.value = bal.value
      else errors.push(`balance: ${bal.reason?.message || bal.reason}`)

      if (pos.status === 'fulfilled') {
        positions.value = pos.value.positions || []
        positionsSource.value = pos.value.source || ''
      } else {
        errors.push(`positions: ${pos.reason?.message || pos.reason}`)
      }

      if (dec.status === 'fulfilled') decisions.value = dec.value.decisions || []
      else errors.push(`decisions: ${dec.reason?.message || dec.reason}`)

      if (tr.status === 'fulfilled') trades.value = tr.value.trades || []
      else errors.push(`trades: ${tr.reason?.message || tr.reason}`)

      if (ev.status === 'fulfilled') events.value = ev.value.events || []
      else errors.push(`events: ${ev.reason?.message || ev.reason}`)

      // Factors per instrument (best-effort; ledger may be empty)
      const factorEntries = await Promise.allSettled(
        watchlist.value.map((instId) =>
          keelFetch<KeelFactors>(`/api/v1/factors/${encodeURIComponent(instId)}`).then(
            (f) => [instId, f] as const,
          ),
        ),
      )
      const nextFactors: Record<string, KeelFactors> = { ...factors.value }
      for (const r of factorEntries) {
        if (r.status === 'fulfilled') {
          const [id, f] = r.value
          nextFactors[id] = f
        }
      }
      factors.value = nextFactors

      // Connected if core health+status work
      isConnected.value = h.status === 'fulfilled' && st.status === 'fulfilled'
      lastUpdated.value = new Date()
      error.value = errors.length ? errors.join(' | ') : null
    } catch (err: any) {
      console.error('[MonitorStore] fetch failed:', err)
      error.value = err.message || 'Failed to fetch Keel API'
      isConnected.value = false
    } finally {
      loading.value = false
      if (!silent) {
        setTimeout(() => {
          isRefreshing.value = false
        }, 400)
      }
    }
  }

  function startPolling(intervalMs = 5000) {
    stopPolling()
    fetchAll(false)
    pollingTimer.value = setInterval(() => fetchAll(true), intervalMs)
  }

  function stopPolling() {
    if (pollingTimer.value) {
      clearInterval(pollingTimer.value)
      pollingTimer.value = null
    }
  }

  return {
    health,
    status,
    balance,
    positions,
    positionsSource,
    decisions,
    trades,
    events,
    factors,
    watchlist,
    loading,
    isRefreshing,
    error,
    lastUpdated,
    isConnected,
    activeTab,
    positionCount,
    uptimeLabel,
    fetchAll,
    startPolling,
    stopPolling,
  }
})
