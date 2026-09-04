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
  type KeelConfig,
  type KeelDailyPnl,
} from '../types/keel'

export const useMonitorStore = defineStore('monitor', () => {
  const { keelFetch } = useKeelApi()

  const health = ref<KeelHealth | null>(null)
  const status = ref<KeelStatus | null>(null)
  const config = ref<KeelConfig | null>(null)
  const dailyPnl = ref<KeelDailyPnl | null>(null)
  const balance = ref<KeelBalance | null>(null)
  const positions = ref<KeelPosition[]>([])
  const positionsSource = ref<string>('')
  const decisions = ref<KeelDecision[]>([])
  /** Decisions tab rows when inst filter is set; Overview always uses unfiltered `decisions`. */
  const decisionsFiltered = ref<KeelDecision[]>([])
  const trades = ref<KeelTrade[]>([])
  /** Trades tab rows when inst filter is set; Overview stays on unfiltered `trades` if shown. */
  const tradesFiltered = ref<KeelTrade[]>([])
  const events = ref<Array<Record<string, unknown>>>([])
  /** Events tab rows when filters are set; Overview «Recent events» uses unfiltered `events`. */
  const eventsFiltered = ref<Array<Record<string, unknown>>>([])
  const factors = ref<Record<string, KeelFactors>>({})
  /** When true, factors fetch uses ?live=1 (OKX public candles); else ledger snapshots. */
  const factorsLive = ref(false)
  /** Per-instrument factor fetch in-flight (does not block global poll). */
  const factorLoading = ref<Record<string, boolean>>({})
  /** Per-instrument factor fetch error message (stale data kept on failure). */
  const factorErrors = ref<Record<string, string>>({})
  /** Positions tab filter; empty = All. Client-side filter over loaded positions. */
  const positionInstFilter = ref('')
  /** Decisions tab filter; empty = All. Passed as GET /decisions?inst_id= when set. */
  const decisionInstFilter = ref('')
  /** Trades tab filter; empty = All. Passed as GET /trades?inst_id= when set. */
  const tradeInstFilter = ref('')
  /** Events tab instrument filter; empty = All. Passed as GET /events?inst_id= when set. */
  const eventInstFilter = ref('')
  /** Events tab type filter; empty = All. Passed as GET /events?event_type= when set. */
  const eventTypeFilter = ref('')
  const watchlist = ref<string[]>([...KEEL_DEFAULT_INSTRUMENTS])

  const loading = ref(false)
  const isRefreshing = ref(false)
  const error = ref<string | null>(null)
  const lastUpdated = ref<Date | null>(null)
  const isConnected = ref(false)
  const pollingTimer = ref<ReturnType<typeof setInterval> | null>(null)
  const activeTab = ref<'overview' | 'positions' | 'decisions' | 'trades' | 'events' | 'factors'>('overview')

  const positionCount = computed(() => positions.value.length)
  /** Positions rows after client-side instrument filter (empty filter = all). */
  const filteredPositions = computed(() => {
    const f = positionInstFilter.value
    if (!f) return positions.value
    return positions.value.filter((p) => p.inst_id === f)
  })
  /** Decisions tab binding: filtered when inst filter set, else unfiltered. */
  const decisionsTabRows = computed(() =>
    decisionInstFilter.value ? decisionsFiltered.value : decisions.value,
  )
  /** Trades tab binding: filtered when inst filter set, else unfiltered. */
  const tradesTabRows = computed(() =>
    tradeInstFilter.value ? tradesFiltered.value : trades.value,
  )
  /** Events tab binding: filtered when any event filter set, else unfiltered. */
  const eventsTabRows = computed(() =>
    eventInstFilter.value || eventTypeFilter.value ? eventsFiltered.value : events.value,
  )
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
        keelFetch<KeelConfig>('/api/v1/config'),
        keelFetch<KeelDailyPnl>('/api/v1/pnl/daily'),
        keelFetch<KeelBalance>('/api/v1/balance'),
        keelFetch<{ count: number; positions: KeelPosition[]; source: string }>('/api/v1/positions'),
        keelFetch<{ count: number; decisions: KeelDecision[] }>('/api/v1/decisions?limit=50'),
        keelFetch<{ count: number; trades: KeelTrade[] }>('/api/v1/trades?limit=50'),
        keelFetch<{ count: number; events: Array<Record<string, unknown>> }>(
          '/api/v1/events?limit=50',
        ),
      ])

      const [h, st, cfg, pnl, bal, pos, dec, tr, ev] = results

      if (h.status === 'fulfilled') health.value = h.value
      else errors.push(`health: ${h.reason?.message || h.reason}`)

      if (st.status === 'fulfilled') status.value = st.value
      else errors.push(`status: ${st.reason?.message || st.reason}`)

      if (cfg.status === 'fulfilled') {
        config.value = cfg.value
        if (Array.isArray(cfg.value.instruments) && cfg.value.instruments.length) {
          watchlist.value = [...cfg.value.instruments]
        }
      } else {
        errors.push(`config: ${cfg.reason?.message || cfg.reason}`)
      }

      if (pnl.status === 'fulfilled') dailyPnl.value = pnl.value
      else errors.push(`pnl/daily: ${pnl.reason?.message || pnl.reason}`)

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

      // Refresh tab-only filtered lists without touching Overview unfiltered refs
      const filteredRefresh = await Promise.allSettled([
        decisionInstFilter.value ? refreshDecisionsFiltered() : Promise.resolve(),
        tradeInstFilter.value ? refreshTradesFiltered() : Promise.resolve(),
        (eventInstFilter.value || eventTypeFilter.value) ? refreshEventsFiltered() : Promise.resolve(),
      ])
      const filteredLabels = ['decisions(filter)', 'trades(filter)', 'events(filter)'] as const
      for (let i = 0; i < filteredRefresh.length; i++) {
        const r = filteredRefresh[i]
        if (r.status === 'rejected') {
          const msg = (r.reason as { message?: string })?.message || String(r.reason)
          errors.push(`${filteredLabels[i]}: ${msg}`)
        }
      }

      // Factors per instrument (best-effort; per-inst loading/error; do not block poll)
      await fetchFactors()

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

  async function fetchFactors() {
    const ids = watchlist.value
    const liveQ = factorsLive.value ? '?live=1' : ''
    const loadingNext: Record<string, boolean> = { ...factorLoading.value }
    for (const id of ids) loadingNext[id] = true
    factorLoading.value = loadingNext

    const factorEntries = await Promise.allSettled(
      ids.map((instId) =>
        keelFetch<KeelFactors>(
          `/api/v1/factors/${encodeURIComponent(instId)}${liveQ}`,
        ).then((f) => [instId, f] as const),
      ),
    )

    const nextFactors: Record<string, KeelFactors> = { ...factors.value }
    const nextErrors: Record<string, string> = { ...factorErrors.value }
    const nextLoading: Record<string, boolean> = { ...factorLoading.value }
    for (let i = 0; i < factorEntries.length; i++) {
      const instId = ids[i]
      const r = factorEntries[i]
      nextLoading[instId] = false
      if (r.status === 'fulfilled') {
        const [id, f] = r.value
        nextFactors[id] = f
        delete nextErrors[id]
      } else {
        const msg = (r.reason as { message?: string })?.message || String(r.reason)
        nextErrors[instId] = msg
        // keep stale factor row on failure
      }
    }
    factors.value = nextFactors
    factorErrors.value = nextErrors
    factorLoading.value = nextLoading
  }

  function buildEventsUrl(filtered: boolean): string {
    const params = new URLSearchParams()
    params.set('limit', '50')
    if (filtered) {
      if (eventTypeFilter.value) params.set('event_type', eventTypeFilter.value)
      if (eventInstFilter.value) params.set('inst_id', eventInstFilter.value)
    }
    return `/api/v1/events?${params.toString()}`
  }

  async function refreshDecisionsFiltered() {
    const url = `/api/v1/decisions?limit=50&inst_id=${encodeURIComponent(decisionInstFilter.value)}`
    const dec = await keelFetch<{ count: number; decisions: KeelDecision[] }>(url)
    decisionsFiltered.value = dec.decisions || []
  }

  async function refreshTradesFiltered() {
    const url = `/api/v1/trades?limit=50&inst_id=${encodeURIComponent(tradeInstFilter.value)}`
    const tr = await keelFetch<{ count: number; trades: KeelTrade[] }>(url)
    tradesFiltered.value = tr.trades || []
  }

  async function refreshEventsFiltered() {
    const ev = await keelFetch<{ count: number; events: Array<Record<string, unknown>> }>(
      buildEventsUrl(true),
    )
    eventsFiltered.value = ev.events || []
  }

  /** Tab filter change: update filtered list only; never touch Overview `decisions`. */
  async function fetchDecisionsOnly() {
    if (!decisionInstFilter.value) {
      decisionsFiltered.value = []
      return
    }
    await refreshDecisionsFiltered()
  }

  /** Tab filter change: update filtered list only; never touch Overview `trades`. */
  async function fetchTradesOnly() {
    if (!tradeInstFilter.value) {
      tradesFiltered.value = []
      return
    }
    await refreshTradesFiltered()
  }

  /** Tab filter change: update filtered list only; never touch Overview `events`. */
  async function fetchEventsOnly() {
    if (!eventInstFilter.value && !eventTypeFilter.value) {
      eventsFiltered.value = []
      return
    }
    await refreshEventsFiltered()
  }

  function setEventInstFilter(instId: string) {
    if (eventInstFilter.value === instId) return
    eventInstFilter.value = instId
    void fetchEventsOnly().catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err)
      error.value = `events: ${msg}`
    })
  }

  function setEventTypeFilter(eventType: string) {
    if (eventTypeFilter.value === eventType) return
    eventTypeFilter.value = eventType
    void fetchEventsOnly().catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err)
      error.value = `events: ${msg}`
    })
  }

  function setFactorsLive(live: boolean) {
    if (factorsLive.value === live) return
    factorsLive.value = live
    void fetchFactors()
  }

  function setPositionInstFilter(instId: string) {
    if (positionInstFilter.value === instId) return
    positionInstFilter.value = instId
  }

  function setDecisionInstFilter(instId: string) {
    if (decisionInstFilter.value === instId) return
    decisionInstFilter.value = instId
    void fetchDecisionsOnly().catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err)
      error.value = `decisions: ${msg}`
    })
  }

  function setTradeInstFilter(instId: string) {
    if (tradeInstFilter.value === instId) return
    tradeInstFilter.value = instId
    void fetchTradesOnly().catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err)
      error.value = `trades: ${msg}`
    })
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
    config,
    dailyPnl,
    balance,
    positions,
    positionsSource,
    filteredPositions,
    decisions,
    decisionsFiltered,
    decisionsTabRows,
    trades,
    tradesFiltered,
    tradesTabRows,
    events,
    eventsFiltered,
    eventsTabRows,
    factors,
    factorsLive,
    factorLoading,
    factorErrors,
    positionInstFilter,
    decisionInstFilter,
    tradeInstFilter,
    eventInstFilter,
    eventTypeFilter,
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
    fetchFactors,
    setFactorsLive,
    setPositionInstFilter,
    setDecisionInstFilter,
    setTradeInstFilter,
    setEventInstFilter,
    setEventTypeFilter,
    fetchEventsOnly,
    startPolling,
    stopPolling,
  }
})
