<script setup lang="ts">
import { onMounted, onUnmounted, computed } from 'vue'
import { useMonitorStore } from '../stores/monitor'
import {
  Activity,
  Wallet,
  LayoutGrid,
  Brain,
  Receipt,
  ScrollText,
  LineChart,
  RefreshCw,
  Shield,
  Ban,
  AlertTriangle,
  TrendingUp,
  Clock,
  Settings2,
  Gauge,
  Waves,
  ClipboardCheck,
  Ghost,
  Crosshair,
} from 'lucide-vue-next'

const store = useMonitorStore()

onMounted(() => store.startPolling(5000))
onUnmounted(() => store.stopPolling())

function fmt(v: unknown, digits = 2): string {
  const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}

function fmtTs(v: unknown): string {
  if (v == null || v === '') return '—'
  if (typeof v === 'number') {
    const ms = v < 1e12 ? v * 1000 : v
    return new Date(ms).toLocaleString()
  }
  const d = new Date(String(v))
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString()
}

/** Q0 near-signal diagnostics from DecisionItem.signal_diag or calculus_data.signal_diag. */
function decisionSignalDiag(d: { signal_diag?: Record<string, unknown> | null; calculus_data?: Record<string, unknown> }): Record<string, unknown> | null {
  const top = d.signal_diag
  if (top && typeof top === 'object') return top
  const nested = d.calculus_data?.signal_diag
  if (nested && typeof nested === 'object') return nested as Record<string, unknown>
  return null
}

function nearSignalNearest(d: { action?: string; signal_diag?: Record<string, unknown> | null; calculus_data?: Record<string, unknown> }): string | null {
  if ((d.action || '').toUpperCase() !== 'WAIT') return null
  const diag = decisionSignalDiag(d)
  const n = diag?.nearest
  return typeof n === 'string' && n ? n : null
}

function nearSignalMissing(d: { action?: string; signal_diag?: Record<string, unknown> | null; calculus_data?: Record<string, unknown> }): string[] {
  if ((d.action || '').toUpperCase() !== 'WAIT') return []
  const diag = decisionSignalDiag(d)
  const raw = diag?.missing
  if (!Array.isArray(raw)) return []
  return raw.map((x) => String(x)).filter(Boolean).slice(0, 6)
}

const tabs = [
  { id: 'overview', label: 'Overview', icon: Activity },
  { id: 'positions', label: 'Positions', icon: LayoutGrid },
  { id: 'decisions', label: 'Decisions', icon: Brain },
  { id: 'trades', label: 'Trades', icon: Receipt },
  { id: 'events', label: 'Events', icon: ScrollText },
  { id: 'factors', label: 'Factors', icon: LineChart },
] as const

const equity = computed(() => fmt(store.balance?.total_equity))
const available = computed(() => fmt(store.balance?.available))
const upl = computed(() => fmt(store.balance?.unrealized_pnl))
const marginPct = computed(() => fmt(store.balance?.margin_usage_pct, 1))
const envLabel = computed(
  () => store.status?.environment || store.health?.environment || '—',
)
const lastUpdatedLabel = computed(() =>
  store.lastUpdated ? store.lastUpdated.toLocaleTimeString() : '—',
)

const factorRows = computed(() =>
  store.watchlist.map((id) => ({
    instId: id,
    f: store.factors[id],
    loading: Boolean(store.factorLoading[id]),
    error: store.factorErrors[id] || '',
  })),
)

function factorQualityShort(quality: string | null | undefined): string | null {
  if (!quality) return null
  const q = quality.toLowerCase()
  if (q === 'okx_public') return 'okx'
  if (q === 'synthetic' || q.startsWith('synthetic_fallback')) return 'synth'
  if (q.length > 10) return `${q.slice(0, 8)}…`
  return q
}

function factorSourceLabel(
  source: string | undefined,
  quality?: string | null,
): string {
  if (!source) return '—'
  if (source === 'okx_public') return 'live'
  if (source === 'ledger') {
    const short = factorQualityShort(quality)
    return short ? `ledger·${short}` : 'ledger'
  }
  return source
}

function factorSourceClass(
  source: string | undefined,
  quality?: string | null,
): string {
  if (source === 'okx_public') return 'bg-cyan-500/15 text-cyan-400 border-cyan-500/40'
  if (source === 'ledger') {
    const short = factorQualityShort(quality)
    if (short === 'okx') return 'bg-cyan-500/10 text-cyan-400/90 border-cyan-500/30'
    if (short === 'synth') return 'bg-amber-500/10 text-amber-400/90 border-amber-500/30'
    return 'bg-zinc-500/10 text-[#A8B3C7] border-zinc-500/30'
  }
  return 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'
}


function decisionMarketSource(d: { calculus_data?: Record<string, unknown> }): string | null {
  const ms = d.calculus_data?.market_source
  if (typeof ms !== 'string' || !ms.trim()) return null
  return ms.trim().toLowerCase()
}

function marketSourceLabel(src: string | null | undefined): string {
  if (!src) return '—'
  if (src === 'okx_public') return 'okx'
  if (src === 'synthetic') return 'synth'
  return src
}

function marketSourceClass(src: string | null | undefined): string {
  if (src === 'okx_public') return 'bg-cyan-500/15 text-cyan-400 border-cyan-500/40'
  if (src === 'synthetic') return 'bg-amber-500/15 text-amber-400 border-amber-500/40'
  if (src === 'mixed') return 'bg-violet-500/15 text-violet-300 border-violet-500/40'
  return 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'
}

const decisionFilterOptions = computed(() => [
  { value: '', label: 'All' },
  ...store.watchlist.map((id) => ({ value: id, label: id })),
])

const tradeFilterOptions = computed(() => [
  { value: '', label: 'All' },
  ...store.watchlist.map((id) => ({ value: id, label: id })),
])

const positionFilterOptions = computed(() => [
  { value: '', label: 'All' },
  ...store.watchlist.map((id) => ({ value: id, label: id })),
])

const eventInstFilterOptions = computed(() => [
  { value: '', label: 'All' },
  ...store.watchlist.map((id) => ({ value: id, label: id })),
])

/** Real event_type strings written by worker/orchestrator/ledger. */
const COMMON_EVENT_TYPES = [
  'worker_cycle_summary',
  'trader_cycle_complete',
  'paper_cycle_complete',
  'decision_invalid',
  'risk_gate_blocked',
  'order_failed',
  'order_resting',
  'order_filled',
  'order_accepted',
  'shadow_fill',
] as const

const eventTypeFilterOptions = computed(() => {
  const fromLoaded = new Set<string>()
  for (const e of store.events) {
    const t = e?.event_type
    if (typeof t === 'string' && t) fromLoaded.add(t)
  }
  const types = fromLoaded.size
    ? [...new Set([...COMMON_EVENT_TYPES, ...fromLoaded])].sort()
    : [...COMMON_EVENT_TYPES]
  return [{ value: '', label: 'All' }, ...types.map((t) => ({ value: t, label: t }))]
})

const eventsEmptyMessage = computed(() => {
  const inst = store.eventInstFilter
  const typ = store.eventTypeFilter
  if (!inst && !typ) return 'no events recorded'
  const parts: string[] = []
  if (typ) parts.push(`type ${typ}`)
  if (inst) parts.push(inst)
  return `no events for ${parts.join(' · ')}`
})

function onPositionFilterChange(ev: Event) {
  const el = ev.target as HTMLSelectElement
  store.setPositionInstFilter(el.value)
}

function onDecisionFilterChange(ev: Event) {
  const el = ev.target as HTMLSelectElement
  store.setDecisionInstFilter(el.value)
}

function onTradeFilterChange(ev: Event) {
  const el = ev.target as HTMLSelectElement
  store.setTradeInstFilter(el.value)
}

function onEventInstFilterChange(ev: Event) {
  const el = ev.target as HTMLSelectElement
  store.setEventInstFilter(el.value)
}

function onEventTypeFilterChange(ev: Event) {
  const el = ev.target as HTMLSelectElement
  store.setEventTypeFilter(el.value)
}

function onFactorsLiveChange(ev: Event) {
  const el = ev.target as HTMLInputElement
  store.setFactorsLive(el.checked)
}

const lastCycle = computed(() => store.status?.last_cycle ?? null)
const lastCycleActions = computed(() => {
  const counts = lastCycle.value?.decision_counts || {}
  return Object.entries(counts)
    .map(([k, v]) => `${k}:${v}`)
    .join(' · ')
})

const decisionStats = computed(() => store.decisionStats)
const decisionStatsWaitPct = computed(() => {
  const r = decisionStats.value?.wait_rate
  if (typeof r !== 'number' || !Number.isFinite(r)) return '—'
  return `${(r * 100).toFixed(0)}%`
})
const decisionStatsTopActions = computed(() => {
  const by = decisionStats.value?.by_action || {}
  return Object.entries(by)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([k, v]) => `${k}:${v}`)
    .join(' · ')
})
const decisionStatsByPolicy = computed(() => {
  const by = decisionStats.value?.by_policy || {}
  return Object.entries(by)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k || '(none)'}:${v}`)
    .join(' · ')
})

const nearestSignals = computed(() => store.nearestSignals)
const shadowStats = computed(() => store.shadowStats)
const nearestSummary = computed(() => nearestSignals.value?.summary ?? null)
const nearestFiredTotal = computed(() => {
  const s = nearestSummary.value
  if (!s) return 0
  return (s.fired_long || 0) + (s.fired_short || 0)
})
const shadowStatsByAction = computed(() => {
  const by = shadowStats.value?.by_action || {}
  return Object.entries(by)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}:${v}`)
    .join(' · ')
})

/** Q2.2 compact quality scorecard (soft-fail). */
const qualityStats = computed(() => store.qualityStats)
const qualityWaitPct = computed(() => {
  const r = qualityStats.value?.wait_rate
  if (typeof r !== 'number' || !Number.isFinite(r)) return '—'
  return `${(r * 100).toFixed(0)}%`
})
const qualityNearPct = computed(() => {
  const r = qualityStats.value?.near_signal_rate
  if (typeof r !== 'number' || !Number.isFinite(r)) return '—'
  return `${(r * 100).toFixed(0)}%`
})
const qualityOkxSharePct = computed(() => {
  const ms = qualityStats.value?.market_source
  if (!ms) return '—'
  const okx = Number(ms.okx_public || 0)
  const syn = Number(ms.synthetic || 0)
  const unk = Number(ms.unknown || 0)
  const total = okx + syn + unk
  if (!total) return '—'
  return `${((okx / total) * 100).toFixed(0)}%`
})
const qualityShadowCount = computed(() => {
  const n = qualityStats.value?.shadow?.count
  return typeof n === 'number' && Number.isFinite(n) ? n : null
})
const qualityProbeCount = computed(() => {
  const n = qualityStats.value?.shadow?.probe_count
  return typeof n === 'number' && Number.isFinite(n) ? n : null
})
const shadowProbeCount = computed(() => {
  const n = shadowStats.value?.probe_count
  return typeof n === 'number' && Number.isFinite(n) ? n : 0
})
/** Q3.5 soft chip: top near-probe skip reason in lookback (hours-filterable). */
const shadowProbeSkipChip = computed(() => {
  const skips = shadowStats.value?.probe_skips
  const by =
    (skips && skips.by_skip_reason) ||
    shadowStats.value?.by_skip_reason ||
    null
  if (!by || typeof by !== 'object') return null
  const entries = Object.entries(by).filter(
    ([, n]) => typeof n === 'number' && n > 0,
  ) as [string, number][]
  if (!entries.length) return null
  entries.sort((a, b) => b[1] - a[1])
  const [reason, count] = entries[0]
  const total =
    typeof skips?.count === 'number' && skips.count > 0
      ? skips.count
      : entries.reduce((s, [, n]) => s + n, 0)
  return { reason, count, total }
})
/** Q3.2/Q3.3 probe markout chip — prefer net roundtrip when present; soft-fail. */
const shadowProbeMarkoutChip = computed(() => {
  const horizons = shadowStats.value?.markout?.horizons
  if (!Array.isArray(horizons) || !horizons.length) return null
  const prefer = [300, 900, 60]
  let best = null
  for (const sec of prefer) {
    const h = horizons.find((x) => x && Number(x.horizon_seconds) === sec)
    if (h && typeof h.probe_sample_count === 'number' && h.probe_sample_count > 0) {
      best = h
      break
    }
  }
  if (!best) {
    best = horizons.find((x) => x && Number(x.probe_sample_count || 0) > 0) || null
  }
  if (!best) return null
  const useNet =
    typeof best.probe_win_rate_net_roundtrip === 'number' ||
    typeof best.probe_avg_net_roundtrip_markout_bps === 'number'
  const wr = useNet ? best.probe_win_rate_net_roundtrip : best.probe_win_rate
  const avg = useNet
    ? best.probe_avg_net_roundtrip_markout_bps
    : best.probe_avg_markout_bps
  const wrLabel =
    typeof wr === 'number' && Number.isFinite(wr) ? `${(wr * 100).toFixed(0)}%` : '—'
  const avgLabel =
    typeof avg === 'number' && Number.isFinite(avg)
      ? `${avg >= 0 ? '+' : ''}${avg.toFixed(1)}bps`
      : '—'
  const feeRole = shadowStats.value?.fee_model?.role
  const rtFee = shadowStats.value?.fee_model?.round_trip_fee_bps
  const netTag = useNet ? 'netRT' : 'gross'
  return {
    horizon: Number(best.horizon_seconds),
    samples: Number(best.probe_sample_count || 0),
    wrLabel,
    avgLabel,
    netTag,
    feeHint:
      useNet && typeof rtFee === 'number'
        ? `net RT (−${rtFee}bps ${feeRole || 'taker'}×2)`
        : 'gross mid',
  }
})
function radarNearestLabel(nearest: string | null | undefined, action: string): string {
  const a = (action || '').toUpperCase()
  if (a === 'BUY_LONG') return 'fired long'
  if (a === 'SELL_SHORT') return 'fired short'
  const n = (nearest || 'none').toLowerCase()
  if (n === 'long') return 'near long'
  if (n === 'short') return 'near short'
  return 'none'
}
function radarNearestClass(nearest: string | null | undefined, action: string): string {
  const a = (action || '').toUpperCase()
  if (a === 'BUY_LONG') return 'bg-emerald-500/20 text-emerald-300 border-emerald-500/50'
  if (a === 'SELL_SHORT') return 'bg-rose-500/20 text-rose-300 border-rose-500/50'
  const n = (nearest || '').toLowerCase()
  if (n === 'long') return 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40'
  if (n === 'short') return 'bg-rose-500/15 text-rose-400 border-rose-500/40'
  return 'bg-zinc-500/10 text-[#A8B3C7] border-zinc-500/30'
}
function radarMissing(s: { missing?: string[] | null }): string[] {
  const raw = s.missing
  if (!Array.isArray(raw)) return []
  return raw.map((x) => String(x)).filter(Boolean).slice(0, 3)
}


function modulesPreview(mods: string[] | null | undefined): string {
  if (!mods || !mods.length) return ''
  const s = mods.join(',')
  return s.length > 28 ? `${s.slice(0, 28)}…` : s
}

/** last_cycle.risk_denies count + optional capped risk_deny_reasons. */
const riskDeniesCount = computed(() => {
  const raw = lastCycle.value?.risk_denies
  const n = typeof raw === 'number' ? raw : Number(raw ?? 0)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
})
const riskDeniesWarn = computed(() => riskDeniesCount.value > 0)

function formatDenyReason(r: { gate?: string; reason?: string } | string): string {
  if (typeof r === 'string') return r
  const gate = (r.gate || '').trim()
  const reason = (r.reason || '').trim()
  if (gate && reason) return `${gate}: ${reason}`
  return gate || reason || 'deny'
}

const riskDenyReasonLines = computed(() => {
  const raw = lastCycle.value?.risk_deny_reasons
  if (!Array.isArray(raw) || !raw.length) return [] as string[]
  return raw.map((r) => formatDenyReason(r as { gate?: string; reason?: string } | string))
})

const riskDenyReasonsPreview = computed(() => {
  const lines = riskDenyReasonLines.value
  if (!lines.length) return ''
  const joined = lines.join(' · ')
  return joined.length > 96 ? `${joined.slice(0, 93)}…` : joined
})

const riskDenyReasonsTitle = computed(() => {
  const lines = riskDenyReasonLines.value
  if (!lines.length) return ''
  const extra = riskDeniesCount.value > lines.length
    ? `\n(+${riskDeniesCount.value - lines.length} more)`
    : ''
  return lines.join('\n') + extra
})

/** last_cycle.error_count (preferred) else errors.length + capped preview. */
const cycleErrorsCount = computed(() => {
  const rawCount = lastCycle.value?.error_count
  if (typeof rawCount === 'number' && Number.isFinite(rawCount) && rawCount >= 0) {
    return Math.floor(rawCount)
  }
  const raw = lastCycle.value?.errors
  return Array.isArray(raw) ? raw.length : 0
})
const cycleErrorsWarn = computed(() => cycleErrorsCount.value > 0)

function formatCycleError(e: { inst_id?: string | null; error?: string } | string): string {
  if (typeof e === 'string') return e
  const inst = (e.inst_id ?? '').toString().trim()
  const err = (e.error || '').trim()
  if (inst && err) return `${inst}: ${err}`
  return err || inst || 'error'
}

const cycleErrorLines = computed(() => {
  const raw = lastCycle.value?.errors
  if (!Array.isArray(raw) || !raw.length) return [] as string[]
  return raw.map((e) => formatCycleError(e as { inst_id?: string | null; error?: string } | string))
})

const cycleErrorsPreview = computed(() => {
  const lines = cycleErrorLines.value
  if (!lines.length) return ''
  const joined = lines.join(' · ')
  return joined.length > 96 ? `${joined.slice(0, 93)}…` : joined
})

const cycleErrorsTitle = computed(() => {
  const lines = cycleErrorLines.value
  if (!lines.length) return ''
  const extra = cycleErrorsCount.value > lines.length
    ? `\n(+${cycleErrorsCount.value - lines.length} more)`
    : ''
  return lines.join('\n') + extra
})

/** Read-only: armed via KEEL_KILL_SWITCH (status API); no admin toggle. */
const killSwitchOn = computed(() => Boolean(store.status?.kill_switch))

/** Read-only: KEEL_SHADOW_MODE — ledger shadow fills, no exchange place_order. */
const shadowModeOn = computed(() =>
  Boolean(store.status?.shadow_mode ?? store.config?.shadow_mode),
)

/** Read-only: KEEL_SHADOW_NEAR_PROBE — near-signal shadow rehearsal (kill+shadow only). */
const shadowNearProbeOn = computed(() =>
  Boolean(store.status?.shadow_near_probe ?? store.config?.shadow_near_probe),
)
const shadowNearProbeCooldown = computed(() => {
  const n =
    store.status?.shadow_near_probe_cooldown_seconds ??
    store.config?.shadow_near_probe_cooldown_seconds
  return typeof n === 'number' && Number.isFinite(n) ? n : null
})

/** Q1: OKX key capability badge (只读 / 可交易 / paper / 未知). */
const okxCapability = computed(() => {
  const raw = (store.status?.okx_capability || store.config?.okx_capability || '').toLowerCase()
  return raw || 'none'
})
const okxCapabilityLabel = computed(() => {
  switch (okxCapability.value) {
    case 'read':
      return '只读'
    case 'trade':
      return '可交易'
    case 'paper':
      return 'paper'
    case 'error':
      return '未知'
    case 'none':
    default:
      return '未知'
  }
})
const okxCapabilityClass = computed(() => {
  switch (okxCapability.value) {
    case 'read':
      return 'bg-amber-500/15 text-amber-400 border-amber-500/40'
    case 'trade':
      return 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40'
    case 'paper':
      return 'bg-cyan-500/15 text-cyan-400 border-cyan-500/40'
    case 'error':
      return 'bg-rose-500/15 text-rose-400 border-rose-500/40'
    default:
      return 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'
  }
})
const okxCapabilityTitle = computed(() => {
  const detail = store.status?.okx_capability_detail || store.config?.okx_capability_detail
  const level = okxCapability.value
  return detail ? `okx_capability=${level} · ${detail}` : `okx_capability=${level}`
})

/** Q1: read-only 实盘准入 checklist (never a kill-switch toggle). */
const arming = computed(() => store.status?.arming ?? null)
const armingReady = computed(() => Boolean(arming.value?.ready_to_arm))
const armingBlockers = computed(() => arming.value?.blockers ?? [])
const armingWarnings = computed(() => arming.value?.warnings ?? [])
const armingEconomic = computed(() => arming.value?.economic ?? null)
const armingEconBlockers = computed(() =>
  (armingBlockers.value || []).filter((b) =>
    b === 'insufficient_shadow_markout_sample'
    || b === 'probe_win_rate_net_roundtrip_below_threshold'
    || b === 'avg_net_roundtrip_markout_bps_below_threshold'
    || String(b).includes('markout')
    || String(b).includes('win_rate_net')
  )
)

const realizedPnl = computed(() => {
  const n = Number(store.dailyPnl?.realized_pnl ?? NaN)
  return Number.isFinite(n) ? n : null
})
const realizedPnlLabel = computed(() =>
  realizedPnl.value == null ? '—' : fmt(realizedPnl.value),
)

/** Loss budget usage vs config.max_daily_loss (0 when profit / unused). */
const maxDailyLoss = computed(() => {
  const n = Number(store.config?.max_daily_loss ?? NaN)
  return Number.isFinite(n) && n > 0 ? n : null
})
const riskBudgetUsage = computed(() => {
  if (realizedPnl.value == null || maxDailyLoss.value == null) return null
  if (realizedPnl.value >= 0) return 0
  return Math.min(1, Math.max(0, -realizedPnl.value / maxDailyLoss.value))
})
const riskBudgetPctLabel = computed(() => {
  if (riskBudgetUsage.value == null) return '—'
  if (riskBudgetUsage.value <= 0) return '未动用'
  return `${Math.round(riskBudgetUsage.value * 100)}%`
})
const riskBudgetWarn = computed(
  () => riskBudgetUsage.value != null && riskBudgetUsage.value >= 0.8,
)
const riskBudgetCritical = computed(
  () => riskBudgetUsage.value != null && riskBudgetUsage.value >= 1,
)

/** Remaining daily loss budget (USDT) when max_daily_loss applies. */
const dailyLossRemaining = computed(() => {
  if (maxDailyLoss.value == null || realizedPnl.value == null) return null
  if (realizedPnl.value >= 0) return maxDailyLoss.value
  return Math.max(0, maxDailyLoss.value + realizedPnl.value)
})
const dailyLossRemainingLabel = computed(() => {
  if (dailyLossRemaining.value == null) return ''
  return `距日损上限还剩 $${fmt(dailyLossRemaining.value)}`
})

/** Sum of positions[].upl (null/NaN → 0). */
const positionsFloatPnl = computed(() => {
  let sum = 0
  for (const pos of store.positions) {
    const n = Number(pos?.upl)
    if (Number.isFinite(n)) sum += n
  }
  return sum
})
const positionsFloatPnlLabel = computed(() => fmt(positionsFloatPnl.value))

/** Worker lag from status.seconds_since_last_cycle. */
const workerLagSeconds = computed(() => {
  const raw = store.status?.seconds_since_last_cycle
  if (raw == null) return null
  const n = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null
})

/** Same formula as keel.api.cycle_time.worker_stale_threshold_seconds. */
function workerStaleThresholdSeconds(intervalSeconds: number): number {
  const interval = Math.max(1, Math.floor(intervalSeconds))
  return Math.max(interval * 2, interval + 300)
}

const cycleIntervalSeconds = computed(() => {
  const raw = store.config?.cycle_interval_seconds
  const n = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 900
})

const workerStaleThreshold = computed(() =>
  workerStaleThresholdSeconds(cycleIntervalSeconds.value),
)

const workerLagStale = computed(() => {
  if (store.status?.worker_stale === true) return true
  if (store.status?.worker_stale === false) return false
  return (
    workerLagSeconds.value != null
    && workerLagSeconds.value > workerStaleThreshold.value
  )
})
const workerLagLabel = computed(() => {
  if (workerLagSeconds.value == null) return '尚无周期'
  return `${workerLagSeconds.value}秒前`
})

function formatCycleIntervalLabel(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds % 60 === 0) return `${seconds / 60}m`
  return `${seconds}s`
}

/** Overview banner copy when workerLagStale. */
const workerStaleBannerText = computed(() => {
  const lag =
    workerLagSeconds.value == null
      ? '上次周期未知'
      : `上次周期 ${workerLagSeconds.value} 秒前`
  const cycle = `周期约 ${formatCycleIntervalLabel(cycleIntervalSeconds.value)}`
  return `Worker 可能停滞 · ${lag} · ${cycle}`
})

const configStrip = computed(() => {
  const c = store.config
  const env = c?.environment || store.status?.environment || store.health?.environment || '—'
  const mode = c?.exchange_mode || '—'
  const instruments = Array.isArray(c?.instruments) && c!.instruments.length
    ? c!.instruments
    : store.watchlist
  const instCount = instruments.length
  const instListFull = instruments.join(', ') || '—'
  const instListShort = (() => {
    if (!instruments.length) return '—'
    const joined = instruments.join(', ')
    return joined.length > 42 ? `${joined.slice(0, 39)}…` : joined
  })()
  const maxPos = c?.max_positions ?? '—'
  const maxDaily = c?.max_daily_loss
  const maxNotional = c?.effective_max_notional_per_instrument ?? c?.max_notional_per_instrument
  const maxContracts = c?.effective_max_contracts_per_instrument ?? c?.max_contracts_per_instrument
  const liveMaxNotional = c?.live_max_notional_per_instrument
  const liveMaxContracts = c?.live_max_contracts_per_instrument
  const kill = c?.kill_switch ?? store.status?.kill_switch ?? false
  const shadow = c?.shadow_mode ?? store.status?.shadow_mode ?? false
  const nearProbe = c?.shadow_near_probe ?? store.status?.shadow_near_probe ?? false
  const nearProbeCd =
    c?.shadow_near_probe_cooldown_seconds ?? store.status?.shadow_near_probe_cooldown_seconds
  const isLiveEnv = String(env).toLowerCase() === 'live'
  const notify = c?.notify_configured
  const policy = c?.decision_policy || store.status?.decision_policy || '—'
  const intervalSec = cycleIntervalSeconds.value
  const presetRaw = c?.observe_preset
  const preset = typeof presetRaw === 'string' && presetRaw.trim() ? presetRaw.trim() : null
  return {
    env,
    mode,
    instCount,
    instListShort,
    instListFull,
    maxPos,
    maxDaily: maxDaily == null ? '—' : fmt(maxDaily),
    maxNotional: maxNotional == null ? '—' : fmt(maxNotional, 0),
    maxContracts: maxContracts == null ? '—' : String(maxContracts),
    liveMaxNotional: liveMaxNotional == null ? '—' : fmt(liveMaxNotional, 0),
    liveMaxContracts: liveMaxContracts == null ? '—' : String(liveMaxContracts),
    isLiveEnv,
    kill: kill ? 'ON' : 'off',
    shadow: shadow ? 'ON' : 'off',
    nearProbe: nearProbe ? 'ON' : 'off',
    nearProbeCd: nearProbeCd == null ? '—' : String(nearProbeCd),
    notify: notify == null ? '—' : notify ? 'yes' : 'no',
    policy,
    cycle: formatCycleIntervalLabel(intervalSec),
    cycleTitle: `Trader cycle interval ${intervalSec}s; stale threshold uses max(2×interval, interval+300) = ${workerStaleThreshold.value}s`,
    preset,
    presetTitle: preset
      ? `KEEL_OBSERVE_PRESET=${preset} → ${intervalSec}s (explicit KEEL_CYCLE_INTERVAL_SECONDS wins if set)`
      : '',
  }
})
</script>

<template>
  <div class="min-h-screen bg-[#080B10] text-[#F3F4F6] flex flex-col">
    <!-- Header -->
    <header class="sticky top-0 z-40 bg-[#0A0D14]/95 backdrop-blur-md border-b border-[#1A2232] px-4 py-2">
      <div class="max-w-[1400px] mx-auto flex items-center justify-between gap-3">
        <div class="flex items-center gap-3 min-w-0">
          <div class="w-8 h-8 rounded-lg bg-gradient-to-tr from-cyan-600 via-blue-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-cyan-500/20 ring-1 ring-white/20 shrink-0">
            <span class="text-white font-black text-sm">K</span>
          </div>
          <div class="min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <h1 class="font-extrabold text-sm tracking-wide text-white">Keel Trader</h1>
              <span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                MONITOR U1
              </span>
              <span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-zinc-500/10 text-zinc-400 border border-zinc-500/20 uppercase">
                {{ envLabel }}
              </span>
              <span
                v-if="killSwitchOn"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-extrabold bg-rose-500/15 text-rose-400 border border-rose-500/40 tracking-wide"
                title="KEEL_KILL_SWITCH armed — trading frozen (env-only)"
              >
                <Ban class="w-3 h-3" />
                KILL SWITCH ON
              </span>
              <span
                v-if="shadowModeOn"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-extrabold bg-violet-500/15 text-violet-300 border border-violet-500/40 tracking-wide"
                title="KEEL_SHADOW_MODE — decisions ledger shadow_fill; no exchange place_order (kill-switch still blocks real orders only)"
              >
                <Ghost class="w-3 h-3" />
                SHADOW MODE
              </span>
              <span
                v-if="shadowNearProbeOn"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-extrabold bg-fuchsia-500/15 text-fuchsia-300 border border-fuchsia-500/40 tracking-wide"
                :title="shadowNearProbeCooldown != null
                  ? `KEEL_SHADOW_NEAR_PROBE — near-signal → shadow_fill rehearsal; cooldown ${shadowNearProbeCooldown}s; never live orders`
                  : 'KEEL_SHADOW_NEAR_PROBE — near-signal → shadow_fill rehearsal; never live orders'"
              >
                <Crosshair class="w-3 h-3" />
                NEAR PROBE
              </span>
            </div>
            <p class="text-[10px] text-[#707E94] font-mono flex items-center gap-1.5">
              <span
                class="inline-block w-1.5 h-1.5 rounded-full"
                :class="store.isConnected ? 'bg-emerald-400' : 'bg-rose-500'"
              />
              <span>read-only · keel.api · {{ store.status?.version || store.health?.version || '…' }}</span>
              <span v-if="store.status">· up {{ store.uptimeLabel }}</span>
            </p>
          </div>
        </div>

        <div class="flex items-center gap-2 shrink-0">
          <div class="hidden sm:block text-[10px] font-mono text-[#707E94]">
            updated {{ lastUpdatedLabel }}
          </div>
          <button
            type="button"
            class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-[#1A2232] bg-[#0D121B] text-xs font-mono text-zinc-300 hover:text-white hover:border-cyan-500/40 transition cursor-pointer"
            :disabled="store.isRefreshing"
            @click="store.fetchAll(false)"
          >
            <RefreshCw class="w-3.5 h-3.5" :class="store.isRefreshing ? 'animate-spin' : ''" />
            Refresh
          </button>
        </div>
      </div>

      <!-- Tabs -->
      <nav class="max-w-[1400px] mx-auto mt-2 flex gap-1 overflow-x-auto pb-1">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          type="button"
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-mono font-bold whitespace-nowrap transition cursor-pointer"
          :class="
            store.activeTab === tab.id
              ? 'bg-[#1C2436] text-white border border-cyan-500/50'
              : 'text-[#707E94] hover:text-white border border-transparent'
          "
          @click="store.activeTab = tab.id"
        >
          <component :is="tab.icon" class="w-3.5 h-3.5" />
          {{ tab.label }}
        </button>
      </nav>
    </header>

    <main class="flex-1 max-w-[1400px] w-full mx-auto px-4 py-4 space-y-4">
      <!-- Error banner -->
      <div
        v-if="store.error"
        class="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs font-mono text-amber-200"
      >
        {{ store.error }}
      </div>

      <div v-if="store.loading" class="py-16 text-center text-sm font-mono text-[#707E94]">
        Loading Keel API…
      </div>

      <template v-else>
        <!-- OVERVIEW -->
        <div v-show="store.activeTab === 'overview'" class="space-y-4">
          <div
            v-if="killSwitchOn"
            class="rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 flex items-start gap-3"
            role="status"
            aria-live="polite"
          >
            <Ban class="w-5 h-5 text-rose-400 shrink-0 mt-0.5" />
            <div class="min-w-0">
              <div class="text-sm font-mono font-extrabold text-rose-300 tracking-wide">
                KILL SWITCH ON
              </div>
              <div class="text-xs font-mono text-rose-200/80 mt-0.5">
                交易已冻结 · risk gates deny all trading · env-only (KEEL_KILL_SWITCH) · no admin toggle
              </div>
            </div>
          </div>

          <div
            v-if="shadowModeOn"
            class="rounded-xl border border-violet-500/40 bg-violet-500/10 px-4 py-3 flex items-start gap-3"
            role="status"
            aria-live="polite"
          >
            <Ghost class="w-5 h-5 text-violet-300 shrink-0 mt-0.5" />
            <div class="min-w-0">
              <div class="text-sm font-mono font-extrabold text-violet-200 tracking-wide">
                SHADOW MODE ON
              </div>
              <div class="text-xs font-mono text-violet-100/80 mt-0.5">
                影子成交 · risk-pass decisions ledger shadow_fill · no OKX place_order · env-only (KEEL_SHADOW_MODE)
              </div>
            </div>
          </div>

          <div
            v-if="shadowNearProbeOn"
            class="rounded-xl border border-fuchsia-500/40 bg-fuchsia-500/10 px-4 py-3 flex items-start gap-3"
            role="status"
            aria-live="polite"
          >
            <Crosshair class="w-5 h-5 text-fuchsia-300 shrink-0 mt-0.5" />
            <div class="min-w-0">
              <div class="text-sm font-mono font-extrabold text-fuchsia-200 tracking-wide">
                NEAR PROBE ON
              </div>
              <div class="text-xs font-mono text-fuchsia-100/80 mt-0.5">
                近信号影子排练 · WAIT+near → shadow_fill · requires kill+shadow ·
                cooldown {{ shadowNearProbeCooldown ?? '—' }}s · env-only (KEEL_SHADOW_NEAR_PROBE)
              </div>
            </div>
          </div>

          <div
            v-if="workerLagStale"
            class="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 flex items-start gap-3"
            role="status"
            aria-live="polite"
          >
            <AlertTriangle class="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
            <div class="min-w-0">
              <div class="text-sm font-mono font-extrabold text-amber-300 tracking-wide">
                WORKER STALE
              </div>
              <div class="text-xs font-mono text-amber-200/80 mt-0.5">
                {{ workerStaleBannerText }}
              </div>
            </div>
          </div>

          <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono mb-2">
                <Wallet class="w-4 h-4 text-cyan-400" />
                Equity
              </div>
              <div class="text-2xl font-black font-mono text-white">${{ equity }}</div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1">
                avail ${{ available }} · src {{ store.balance?.source || '—' }}
              </div>
            </div>
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono mb-2">
                <Activity class="w-4 h-4 text-emerald-400" />
                Unrealized PnL
              </div>
              <div
                class="text-2xl font-black font-mono"
                :class="Number(store.balance?.unrealized_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'"
              >
                ${{ upl }}
              </div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1">margin {{ marginPct }}%</div>
            </div>
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono mb-2">
                <TrendingUp class="w-4 h-4 text-amber-400" />
                今日已实现盈亏
              </div>
              <div
                class="text-2xl font-black font-mono"
                :class="realizedPnl == null
                  ? 'text-[#707E94]'
                  : realizedPnl >= 0
                    ? 'text-emerald-400'
                    : 'text-rose-400'"
              >
                ${{ realizedPnlLabel }}
              </div>
              <div class="text-[11px] font-mono mt-1 space-y-0.5">
                <div v-if="dailyLossRemainingLabel" class="text-amber-200/90">
                  {{ dailyLossRemainingLabel }}
                </div>
                <div class="text-[#707E94]">
                  {{ store.dailyPnl?.date || '—' }} · {{ store.dailyPnl?.source || 'ledger' }}
                </div>
              </div>
            </div>
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono mb-2">
                <LayoutGrid class="w-4 h-4 text-blue-400" />
                Positions
              </div>
              <div class="text-2xl font-black font-mono text-white">{{ store.positionCount }}</div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1">
                {{ store.positionsSource || '—' }}
              </div>
            </div>
            <div
              class="bg-[#0D121B] border rounded-xl p-4"
              :class="riskBudgetCritical
                ? 'border-rose-500/50'
                : riskBudgetWarn
                  ? 'border-amber-500/40'
                  : 'border-[#1A2232]'"
            >
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono mb-2">
                <Gauge
                  class="w-4 h-4"
                  :class="riskBudgetCritical
                    ? 'text-rose-400'
                    : riskBudgetWarn
                      ? 'text-amber-400'
                      : 'text-violet-400'"
                />
                风控额度
              </div>
              <div
                class="text-2xl font-black font-mono"
                :class="riskBudgetCritical
                  ? 'text-rose-400'
                  : riskBudgetWarn
                    ? 'text-amber-400'
                    : 'text-white'"
              >
                {{ riskBudgetPctLabel }}
              </div>
              <div class="mt-2 h-1.5 rounded-full bg-[#1A2232] overflow-hidden">
                <div
                  class="h-full rounded-full transition-all"
                  :class="riskBudgetCritical
                    ? 'bg-rose-500'
                    : riskBudgetWarn
                      ? 'bg-amber-400'
                      : 'bg-violet-500'"
                  :style="{ width: `${Math.round((riskBudgetUsage ?? 0) * 100)}%` }"
                />
              </div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1">
                <template v-if="realizedPnl == null || maxDailyLoss == null">
                  已实现 vs max_daily_loss —
                </template>
                <template v-else-if="(riskBudgetUsage ?? 0) <= 0">
                  盈利/未亏 · budget ${{ fmt(maxDailyLoss) }}
                </template>
                <template v-else>
                  loss ${{ fmt(Math.abs(realizedPnl)) }} / ${{ fmt(maxDailyLoss) }}
                </template>
              </div>
            </div>
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono mb-2">
                <Waves class="w-4 h-4 text-sky-400" />
                持仓浮动盈亏
              </div>
              <div
                class="text-2xl font-black font-mono"
                :class="positionsFloatPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'"
              >
                ${{ positionsFloatPnlLabel }}
              </div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1">
                Σ positions.upl · {{ store.positionCount }} pos
              </div>
            </div>
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono mb-2">
                <Shield class="w-4 h-4 text-indigo-400" />
                Credentials
              </div>
              <div class="text-sm font-mono text-white space-y-1">
                <div class="flex items-center gap-2 flex-wrap">
                  <span>OKX: {{ store.status?.credentials?.okx ? 'yes' : 'no' }}</span>
                  <span
                    class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold border"
                    :class="okxCapabilityClass"
                    :title="okxCapabilityTitle"
                  >{{ okxCapabilityLabel }}</span>
                </div>
                <div>LLM: {{ store.status?.credentials?.llm ? 'yes' : 'no' }}</div>
              </div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1 truncate" :title="store.status?.ledger_db">
                {{ store.status?.mode || '—' }}
              </div>
            </div>
          </div>

          <div
            v-if="arming"
            class="bg-[#0D121B] border rounded-xl p-4"
            :class="armingReady ? 'border-emerald-500/40' : 'border-amber-500/40'"
            role="status"
            aria-live="polite"
          >
            <div class="flex flex-wrap items-start justify-between gap-2 mb-2">
              <div class="flex items-center gap-1.5 text-[#707E94] text-xs font-mono">
                <ClipboardCheck
                  class="w-4 h-4"
                  :class="armingReady ? 'text-emerald-400' : 'text-amber-400'"
                />
                <span class="text-white font-bold uppercase tracking-wide">实盘准入</span>
                <span class="text-[#707E94] font-normal normal-case">arming + economic gates · read-only</span>
              </div>
              <span
                class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono font-extrabold border tracking-wide"
                :class="armingReady
                  ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40'
                  : 'bg-amber-500/15 text-amber-400 border-amber-500/40'"
                :title="armingReady
                  ? 'Prerequisites met — still set KEEL_KILL_SWITCH=0 manually to arm'
                  : 'Not ready — resolve blockers before clearing kill-switch'"
              >
                {{ armingReady ? 'READY' : 'NOT READY' }}
              </span>
            </div>
            <div class="text-[11px] font-mono text-[#A8B3C7] mb-2">
              capability
              <span class="text-white">{{ arming.capability || '—' }}</span>
              · kill
              <span :class="arming.kill_switch ? 'text-rose-400' : 'text-emerald-400'">
                {{ arming.kill_switch ? 'ON' : 'off' }}
              </span>
              · never auto-clears env
            </div>
            <div v-if="armingBlockers.length" class="flex flex-wrap gap-1.5 mb-1.5">
              <span
                v-for="(b, i) in armingBlockers"
                :key="'ab-' + i"
                class="inline-flex max-w-full items-center px-1.5 py-0.5 rounded text-[10px] font-mono font-bold border bg-rose-500/10 text-rose-300 border-rose-500/30 truncate"
                :title="b"
              >{{ b }}</span>
            </div>
            <div v-if="armingWarnings.length" class="flex flex-wrap gap-1.5">
              <span
                v-for="(w, i) in armingWarnings"
                :key="'aw-' + i"
                class="inline-flex max-w-full items-center px-1.5 py-0.5 rounded text-[10px] font-mono border bg-amber-500/10 text-amber-200/90 border-amber-500/25 truncate"
                :title="w"
              >{{ w }}</span>
            </div>
            <div
              v-if="armingEconomic && armingEconomic.enabled !== false"
              class="mt-2 pt-2 border-t border-[#1A2232] text-[10px] font-mono text-[#A8B3C7] space-y-1"
            >
              <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span class="text-white font-bold uppercase tracking-wide">Economic</span>
                <span
                  class="inline-flex items-center px-1.5 py-0.5 rounded border text-[10px] font-extrabold"
                  :class="armingEconomic.passed
                    ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40'
                    : 'bg-rose-500/15 text-rose-300 border-rose-500/40'"
                  :title="armingEconomic.note || 'Shadow markout economic gates'"
                >{{ armingEconomic.passed ? 'PASS' : 'FAIL' }}</span>
                <span v-if="armingEconBlockers.length" class="text-rose-300 truncate" :title="armingEconBlockers.join('; ')">
                  {{ armingEconBlockers.join(' · ') }}
                </span>
              </div>
              <div class="flex flex-wrap gap-x-3 gap-y-0.5 text-[#707E94]">
                <span>fills <span class="text-white">{{ armingEconomic.fill_count ?? '—' }}</span>/<span>{{ armingEconomic.min_fills ?? 10 }}</span></span>
                <span>probe <span class="text-white">{{ armingEconomic.probe_count ?? '—' }}</span>/<span>{{ armingEconomic.min_probe_fills ?? 5 }}</span></span>
                <span>mk{{ armingEconomic.horizon_seconds ?? 300 }}s n=<span class="text-white">{{ armingEconomic.sample_count ?? '—' }}</span></span>
                <span>netRT wr <span class="text-white">{{
                  armingEconomic.probe_win_rate_net_roundtrip != null
                    ? Number(armingEconomic.probe_win_rate_net_roundtrip).toFixed(2)
                    : (armingEconomic.win_rate_net_roundtrip != null
                      ? Number(armingEconomic.win_rate_net_roundtrip).toFixed(2)
                      : '—')
                }}</span>≥{{ armingEconomic.min_probe_win_rate_net_rt ?? 0.55 }}</span>
                <span>avgNetRT <span class="text-white">{{
                  armingEconomic.avg_net_roundtrip_markout_bps != null
                    ? Number(armingEconomic.avg_net_roundtrip_markout_bps).toFixed(1)
                    : '—'
                }}</span>bps</span>
              </div>
            </div>
            <div
              v-if="armingReady && !armingBlockers.length"
              class="text-[10px] font-mono text-emerald-400/80 mt-1"
            >
              Checklist + economic gates green — rehearse with KEEL_SHADOW_MODE=1 (kill may stay ON; shadow still records), then set KEEL_KILL_SWITCH=0 manually (no UI toggle).
            </div>
          </div>

          <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl px-4 py-3 flex flex-col gap-2 text-[11px] font-mono">
            <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div class="flex items-center gap-1.5 text-[#707E94]">
              <Settings2 class="w-3.5 h-3.5 text-cyan-400" />
              <span class="text-white font-bold">Config</span>
            </div>
            <span class="text-[#A8B3C7]">env <span class="text-white">{{ configStrip.env }}</span></span>
            <span class="text-[#A8B3C7]">mode <span class="text-white">{{ configStrip.mode }}</span></span>
            <span class="text-[#A8B3C7]">policy <span class="text-white">{{ configStrip.policy }}</span></span>
            <span
              class="text-[#A8B3C7] max-w-[18rem] truncate"
              :title="configStrip.instListFull"
            >instruments <span class="text-white">{{ configStrip.instListShort }}</span>
              <span class="text-[#707E94]">({{ configStrip.instCount }})</span>
            </span>
            <span class="text-[#A8B3C7]">max_pos <span class="text-white">{{ configStrip.maxPos }}</span></span>
            <span class="text-[#A8B3C7]">max_daily_loss <span class="text-white">${{ configStrip.maxDaily }}</span></span>
            <span class="text-[#A8B3C7]">max_notional <span class="text-white">${{ configStrip.maxNotional }}</span></span>
            <span class="text-[#A8B3C7]">max_contracts <span class="text-white">{{ configStrip.maxContracts }}</span></span>
            <span
              v-if="configStrip.isLiveEnv"
              class="text-[#A8B3C7]"
              title="KEEL_LIVE_MAX_* first-live tighter caps (applied when env=live and not shadow)"
            >live_caps <span class="text-amber-300">${{ configStrip.liveMaxNotional }} / {{ configStrip.liveMaxContracts }} ct</span></span>
            <span class="text-[#A8B3C7]">kill <span :class="killSwitchOn ? 'text-rose-400' : 'text-white'">{{ configStrip.kill }}</span></span>
            <span class="text-[#A8B3C7]">shadow <span :class="shadowModeOn ? 'text-violet-300' : 'text-white'">{{ configStrip.shadow }}</span></span>
            <span
              class="text-[#A8B3C7]"
              :title="`KEEL_SHADOW_NEAR_PROBE · cooldown ${configStrip.nearProbeCd}s`"
            >near_probe <span :class="shadowNearProbeOn ? 'text-fuchsia-300' : 'text-white'">{{ configStrip.nearProbe }}</span>
              <span v-if="shadowNearProbeOn" class="text-[#707E94]">({{ configStrip.nearProbeCd }}s)</span>
            </span>
            <span class="text-[#A8B3C7]">notify <span class="text-white">{{ configStrip.notify }}</span></span>
            <span class="text-[#A8B3C7]" :title="configStrip.cycleTitle">周期 <span class="text-white">{{ configStrip.cycle }}</span></span>
            <span
              v-if="configStrip.preset"
              class="text-[#A8B3C7]"
              :title="configStrip.presetTitle"
            >preset <span class="text-cyan-400">{{ configStrip.preset }}</span></span>
            <span
              class="inline-flex items-center gap-1 ml-auto px-1.5 py-0.5 rounded text-[10px] font-bold border"
              :class="workerLagSeconds == null
                ? 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'
                : workerLagStale
                  ? 'bg-amber-500/15 text-amber-400 border-amber-500/40'
                  : 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'"
              :title="workerLagStale
                ? `Worker 可能停滞（>${workerStaleThreshold}s / ~2× cycle interval）`
                : 'Seconds since last worker cycle'"
            >
              <Clock class="w-3 h-3" />
              <template v-if="workerLagSeconds == null">尚无周期</template>
              <template v-else-if="workerLagStale">Worker 可能停滞 · {{ workerLagLabel }}</template>
              <template v-else>{{ workerLagLabel }}</template>
            </span>
            </div>
          </div>

          <div
            v-if="lastCycle"
            class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4"
          >
            <h2 class="text-xs font-mono font-bold text-white uppercase mb-2">Last worker cycle</h2>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs font-mono">
              <div>
                <div class="text-[#707E94]">When</div>
                <div class="text-white">{{ fmtTs(lastCycle.timestamp) }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Mode / adapter</div>
                <div class="text-white">{{ lastCycle.mode }} · {{ lastCycle.adapter || '—' }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Policy</div>
                <div class="text-white">{{ lastCycle.policy || '—' }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Instruments</div>
                <div class="text-white">{{ lastCycle.instruments ?? '—' }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Duration</div>
                <div class="text-white">{{ lastCycle.duration_ms != null ? `${lastCycle.duration_ms} ms` : '—' }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Market source</div>
                <div class="mt-0.5">
                  <span
                    class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold border"
                    :class="marketSourceClass(lastCycle.market_source)"
                    :title="lastCycle.market_source || 'unknown'"
                  >{{ marketSourceLabel(lastCycle.market_source) }}</span>
                </div>
              </div>
              <div class="md:col-span-2">
                <div class="text-[#707E94]">Decisions</div>
                <div class="text-cyan-400">{{ lastCycleActions || '—' }}</div>
              </div>
              <div class="md:col-span-2">
                <div class="text-[#707E94]">Risk denies</div>
                <div class="mt-0.5 flex flex-col gap-1 min-w-0">
                  <span
                    class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-bold border tabular-nums w-fit"
                    :class="riskDeniesWarn
                      ? 'bg-amber-500/15 text-amber-400 border-amber-500/40'
                      : 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'"
                    :title="riskDeniesWarn
                      ? `${riskDeniesCount} instrument(s) denied by risk gates this cycle`
                      : 'No risk-gate denies this cycle'"
                  >
                    {{ riskDeniesCount }}
                  </span>
                  <div
                    v-if="riskDenyReasonsPreview"
                    class="text-[10px] font-mono text-amber-400/80 truncate max-w-full"
                    :title="riskDenyReasonsTitle"
                  >
                    {{ riskDenyReasonsPreview }}
                  </div>
                </div>
              </div>
              <div class="md:col-span-2">
                <div class="text-[#707E94]">Errors</div>
                <div class="mt-0.5 flex flex-col gap-1 min-w-0">
                  <span
                    class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-bold border tabular-nums w-fit"
                    :class="cycleErrorsWarn
                      ? 'bg-rose-500/15 text-rose-400 border-rose-500/40'
                      : 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'"
                    :title="cycleErrorsWarn
                      ? `${cycleErrorsCount} instrument error(s) this cycle`
                      : 'No instrument errors this cycle'"
                  >
                    {{ cycleErrorsCount }}
                  </span>
                  <div
                    v-if="cycleErrorsPreview"
                    class="text-[10px] font-mono text-rose-400/80 truncate max-w-full"
                    :title="cycleErrorsTitle"
                  >
                    {{ cycleErrorsPreview }}
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div
            v-if="qualityStats"
            class="bg-[#0D121B] border border-[#1A2232] rounded-xl px-4 py-2.5 flex flex-wrap items-center gap-2"
            title="Observation quality scorecard (read-only)"
          >
            <span class="text-[10px] font-mono font-bold text-[#A8B3C7] uppercase tracking-wide mr-1">
              Quality
              <span class="text-[#707E94] font-normal normal-case">({{ qualityStats.hours }}h)</span>
            </span>
            <span
              class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-cyan-500/40 text-cyan-300"
              :title="`wait_rate among ${qualityStats.decision_count} decisions`"
            >wait {{ qualityWaitPct }}</span>
            <span
              class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-emerald-500/40 text-emerald-300"
              title="WAIT rows with signal_diag.nearest in {long,short}"
            >near {{ qualityNearPct }}</span>
            <span
              class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-violet-500/40 text-violet-300"
              :title="qualityStats.shadow?.last_timestamp
                ? `shadow_fill n=${qualityShadowCount} last @ ${fmtTs(qualityStats.shadow.last_timestamp)}`
                : `shadow_fill n=${qualityShadowCount ?? 0}`"
            >shadow {{ qualityShadowCount ?? 0 }}</span>
            <span
              class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-fuchsia-500/40 text-fuchsia-300"
              :title="`near-probe shadow_fill n=${qualityProbeCount ?? 0}`"
            >probe {{ qualityProbeCount ?? 0 }}</span>
            <span
              class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-sky-500/40 text-sky-300"
              :title="`okx_public ${qualityStats.market_source?.okx_public ?? 0} / synthetic ${qualityStats.market_source?.synthetic ?? 0} / unknown ${qualityStats.market_source?.unknown ?? 0}`"
            >okx {{ qualityOkxSharePct }}</span>
          </div>

          <div
            v-if="decisionStats"
            class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4"
          >
            <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
              <h2 class="text-xs font-mono font-bold text-white uppercase">
                Decision quality ({{ decisionStats.hours }}h)
              </h2>
              <span
                v-if="shadowStats"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-violet-500/40 text-violet-300"
                :title="shadowStats.last_timestamp
                  ? `last shadow_fill @ ${fmtTs(shadowStats.last_timestamp)}`
                  : 'no shadow_fill in lookback'"
              >shadow {{ shadowStats.count }}</span>
              <span
                v-if="shadowStats"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-fuchsia-500/40 text-fuchsia-300"
                title="near-probe shadow_fill count in lookback"
              >probe {{ shadowProbeCount }}</span>
              <span
                v-if="shadowProbeSkipChip"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-orange-500/40 text-orange-300"
                :title="`near-probe skips ${shadowProbeSkipChip.total} · top ${shadowProbeSkipChip.reason}=${shadowProbeSkipChip.count} (Q3.5)`"
              >skip {{ shadowProbeSkipChip.reason }} ×{{ shadowProbeSkipChip.count }}</span>
              <span
                v-if="shadowProbeMarkoutChip"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-amber-500/40 text-amber-300"
                :title="`probe markout @ ${shadowProbeMarkoutChip.horizon}s · n=${shadowProbeMarkoutChip.samples} · ${shadowProbeMarkoutChip.feeHint} · offline`"
              >mk {{ shadowProbeMarkoutChip.netTag }} {{ shadowProbeMarkoutChip.wrLabel }} / {{ shadowProbeMarkoutChip.avgLabel }}</span>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs font-mono">
              <div>
                <div class="text-[#707E94]">Decisions</div>
                <div class="text-white tabular-nums">{{ decisionStats.decision_count }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Wait rate</div>
                <div class="text-cyan-400 tabular-nums">{{ decisionStatsWaitPct }}</div>
              </div>
              <div class="md:col-span-2">
                <div class="text-[#707E94]">Top actions</div>
                <div class="text-white truncate" :title="decisionStatsTopActions">{{ decisionStatsTopActions || '—' }}</div>
              </div>
              <div class="md:col-span-2">
                <div class="text-[#707E94]">By policy</div>
                <div class="text-white truncate" :title="decisionStatsByPolicy">{{ decisionStatsByPolicy || '—' }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Cycles</div>
                <div class="text-white tabular-nums">{{ decisionStats.cycle_count }}</div>
              </div>
              <div>
                <div class="text-[#707E94]">Risk denies</div>
                <div class="text-white tabular-nums">{{ decisionStats.risk_deny_events }}</div>
              </div>
            </div>
          </div>

          <div
            v-if="nearestSignals"
            class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4"
          >
            <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
              <h2 class="text-xs font-mono font-bold text-white uppercase">
                近信号雷达
                <span class="text-[#707E94] font-normal normal-case">({{ nearestSignals.hours }}h)</span>
              </h2>
              <div class="flex flex-wrap items-center gap-2 text-[10px] font-mono">
                <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-zinc-500/30 text-[#A8B3C7]" title="WAIT">
                  WAIT <span class="text-white tabular-nums">{{ nearestSummary?.waiting ?? 0 }}</span>
                </span>
                <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-emerald-500/40 text-emerald-400" title="近多">
                  近多 <span class="tabular-nums">{{ nearestSummary?.long_nearest ?? 0 }}</span>
                </span>
                <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-rose-500/40 text-rose-400" title="近空">
                  近空 <span class="tabular-nums">{{ nearestSummary?.short_nearest ?? 0 }}</span>
                </span>
                <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-cyan-500/40 text-cyan-400" title="已触发 long+short">
                  已触发 <span class="tabular-nums">{{ nearestFiredTotal }}</span>
                </span>
              </div>
            </div>
            <div
              v-if="shadowStats"
              class="mb-3 text-[10px] font-mono text-[#A8B3C7] flex flex-wrap items-center gap-2"
            >
              <span class="text-violet-300">Shadow stats ({{ shadowStats.hours }}h)</span>
              <span class="text-white tabular-nums">n={{ shadowStats.count }}</span>
              <span class="text-fuchsia-300 tabular-nums">probe={{ shadowProbeCount }}</span>
              <span
                v-if="shadowProbeMarkoutChip"
                class="text-amber-300/90 tabular-nums"
                :title="`probe markout @ ${shadowProbeMarkoutChip.horizon}s · n=${shadowProbeMarkoutChip.samples} · ${shadowProbeMarkoutChip.feeHint} · win_rate / avg`"
              >probe mk {{ shadowProbeMarkoutChip.netTag }} {{ shadowProbeMarkoutChip.wrLabel }} / {{ shadowProbeMarkoutChip.avgLabel }} ({{ shadowProbeMarkoutChip.horizon }}s)</span>
              <span v-if="shadowStatsByAction" class="truncate" :title="shadowStatsByAction">{{ shadowStatsByAction }}</span>
              <span v-if="shadowStats.last_timestamp" class="text-[#707E94]">last {{ fmtTs(shadowStats.last_timestamp) }}</span>
              <span v-else class="text-[#707E94]">no fills</span>
            </div>
            <div v-if="!nearestSignals.signals.length" class="text-xs font-mono text-[#707E94] py-4 text-center border border-dashed border-[#1A2232] rounded-lg">
              No recent signal_diag decisions in lookback
            </div>
            <ul v-else class="space-y-2 max-h-72 overflow-y-auto">
              <li
                v-for="s in nearestSignals.signals"
                :key="s.inst_id"
                class="flex flex-wrap items-center gap-2 text-xs font-mono border-b border-[#1A2232]/60 pb-2"
              >
                <span class="text-white font-bold min-w-[8rem]">{{ s.inst_id }}</span>
                <span
                  class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold border"
                  :class="radarNearestClass(s.nearest, s.action)"
                  :title="`${s.action} · nearest ${s.nearest || '—'}`"
                >{{ radarNearestLabel(s.nearest, s.action) }}</span>
                <span
                  v-for="gate in radarMissing(s)"
                  :key="`${s.inst_id}-${gate}`"
                  class="inline-flex items-center px-1 py-0.5 rounded text-[10px] font-mono bg-amber-500/10 text-amber-400/90 border border-amber-500/30"
                  :title="`missing: ${gate}`"
                >{{ gate }}</span>
                <span class="ml-auto text-[10px] text-[#707E94]">{{ fmtTs(s.timestamp) }}</span>
              </li>
            </ul>
          </div>


          <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <h2 class="text-xs font-mono font-bold text-white uppercase mb-3">Recent decisions</h2>
              <div v-if="!store.decisions.length" class="text-xs font-mono text-[#707E94] py-6 text-center border border-dashed border-[#1A2232] rounded-lg">
                No ledger decisions yet — run <code class="text-cyan-400">python -m keel.worker --once</code>
              </div>
              <ul v-else class="space-y-2 max-h-72 overflow-y-auto">
                <li
                  v-for="d in store.decisions.slice(0, 8)"
                  :key="String(d.id)"
                  class="text-xs font-mono border-b border-[#1A2232]/60 pb-2"
                >
                  <div class="flex justify-between gap-2">
                    <span class="text-white font-bold">{{ d.inst_id }}</span>
                    <span class="text-cyan-400">{{ d.action }}</span>
                  </div>
                  <div class="text-[#707E94] flex justify-between gap-2 mt-0.5">
                    <span>conf {{ fmt(d.confidence, 2) }}</span>
                    <span>{{ fmtTs(d.timestamp) }}</span>
                  </div>
                </li>
              </ul>
            </div>
            <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
              <h2 class="text-xs font-mono font-bold text-white uppercase mb-3">Recent events</h2>
              <div v-if="!store.events.length" class="text-xs font-mono text-[#707E94] py-6 text-center border border-dashed border-[#1A2232] rounded-lg">
                No ledger events yet
              </div>
              <ul v-else class="space-y-2 max-h-72 overflow-y-auto">
                <li
                  v-for="(e, i) in store.events.slice(0, 8)"
                  :key="i"
                  class="text-xs font-mono border-b border-[#1A2232]/60 pb-2 text-[#A8B3C7]"
                >
                  <pre class="whitespace-pre-wrap break-all">{{ JSON.stringify(e) }}</pre>
                </li>
              </ul>
            </div>
          </div>
        </div>

        <!-- POSITIONS -->
        <div v-show="store.activeTab === 'positions'" class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
          <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 class="text-sm font-mono font-bold text-white">
              Positions
              <span class="text-[#707E94] font-normal">({{ store.filteredPositions.length }}/{{ store.positionCount }} · {{ store.positionsSource || '—' }})</span>
            </h2>
            <label class="flex items-center gap-2 text-xs font-mono text-[#A8B3C7]">
              <span class="text-[#707E94]">Instrument</span>
              <select
                class="bg-[#080B10] border border-[#1A2232] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:outline-none focus:border-cyan-500/50 cursor-pointer"
                :value="store.positionInstFilter"
                @change="onPositionFilterChange"
              >
                <option
                  v-for="opt in positionFilterOptions"
                  :key="opt.value || 'all'"
                  :value="opt.value"
                >{{ opt.label }}</option>
              </select>
            </label>
          </div>
          <div v-if="!store.positions.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            No open positions
          </div>
          <div
            v-else-if="!store.filteredPositions.length"
            class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg"
          >
            Empty — no positions for {{ store.positionInstFilter }}
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead>
                <tr class="text-[#707E94] border-b border-[#1A2232]">
                  <th class="pb-2 pr-3">Instrument</th>
                  <th class="pb-2 pr-3">Side</th>
                  <th class="pb-2 pr-3">Size</th>
                  <th class="pb-2 pr-3">Lev</th>
                  <th class="pb-2 pr-3">Avg</th>
                  <th class="pb-2 pr-3">Mark</th>
                  <th class="pb-2 pr-3">Margin</th>
                  <th class="pb-2 text-right">UPL</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[#1A2232]/50">
                <tr v-for="p in store.filteredPositions" :key="p.inst_id + p.side" class="hover:bg-[#121824]/50">
                  <td class="py-2.5 pr-3 text-white font-bold">{{ p.inst_id }}</td>
                  <td class="py-2.5 pr-3">
                    <span
                      class="px-1.5 py-0.5 rounded text-[10px] font-extrabold"
                      :class="p.side === 'long' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
                    >{{ p.side }}</span>
                  </td>
                  <td class="py-2.5 pr-3 text-zinc-300">{{ fmt(p.size, 4) }}</td>
                  <td class="py-2.5 pr-3 text-zinc-300">{{ fmt(p.leverage, 0) }}x</td>
                  <td class="py-2.5 pr-3 text-zinc-300">{{ fmt(p.avg_price) }}</td>
                  <td class="py-2.5 pr-3 text-white">{{ fmt(p.mark_price) }}</td>
                  <td class="py-2.5 pr-3 text-zinc-300">{{ fmt(p.margin) }}</td>
                  <td
                    class="py-2.5 text-right font-bold"
                    :class="Number(p.upl) >= 0 ? 'text-emerald-400' : 'text-rose-400'"
                  >
                    {{ fmt(p.upl) }}
                    <span class="text-[#707E94] font-normal">({{ fmt(p.upl_ratio, 2) }})</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- DECISIONS -->
        <div v-show="store.activeTab === 'decisions'" class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
          <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 class="text-sm font-mono font-bold text-white">
              Decisions (ledger)
              <span class="text-[#707E94] font-normal">({{ store.decisionsTabRows.length }})</span>
            </h2>
            <label class="flex items-center gap-2 text-xs font-mono text-[#A8B3C7]">
              <span class="text-[#707E94]">Instrument</span>
              <select
                class="bg-[#080B10] border border-[#1A2232] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:outline-none focus:border-cyan-500/50 cursor-pointer"
                :value="store.decisionInstFilter"
                @change="onDecisionFilterChange"
              >
                <option
                  v-for="opt in decisionFilterOptions"
                  :key="opt.value || 'all'"
                  :value="opt.value"
                >{{ opt.label }}</option>
              </select>
            </label>
          </div>
          <div v-if="!store.decisionsTabRows.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            Empty — {{ store.decisionInstFilter ? `no decisions for ${store.decisionInstFilter}` : 'worker has not written decisions yet' }}
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead>
                <tr class="text-[#707E94] border-b border-[#1A2232]">
                  <th class="pb-2 pr-3">Time</th>
                  <th class="pb-2 pr-3">Inst</th>
                  <th class="pb-2 pr-3">Action</th>
                  <th class="pb-2 pr-3">Policy</th>
                  <th class="pb-2 pr-3">Conf</th>
                  <th class="pb-2 pr-3">Entry</th>
                  <th class="pb-2">Reason</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[#1A2232]/50">
                <tr v-for="d in store.decisionsTabRows" :key="String(d.id)">
                  <td class="py-2 pr-3 text-[#707E94] whitespace-nowrap">{{ fmtTs(d.timestamp) }}</td>
                  <td class="py-2 pr-3 text-white">{{ d.inst_id }}</td>
                  <td class="py-2 pr-3 text-cyan-400">{{ d.action }}</td>
                  <td
                    class="py-2 pr-3 text-zinc-300 max-w-[9rem] truncate"
                    :title="[d.policy_name || '', modulesPreview(d.prompt_modules)].filter(Boolean).join(' · ')"
                  >{{ d.policy_name || '—' }}<span v-if="modulesPreview(d.prompt_modules)" class="text-[#707E94]"> · {{ modulesPreview(d.prompt_modules) }}</span></td>
                  <td class="py-2 pr-3">{{ fmt(d.confidence, 2) }}</td>
                  <td class="py-2 pr-3">{{ fmt(d.entry_price) }}</td>
                  <td class="py-2 text-zinc-400 max-w-lg whitespace-normal break-words" :title="d.reason">
                    <div class="line-clamp-2">{{ d.reason || '—' }}</div>
                    <div
                      v-if="decisionMarketSource(d) || nearSignalNearest(d)"
                      class="mt-1 flex flex-wrap items-center gap-1"
                    >
                      <span
                        v-if="decisionMarketSource(d)"
                        class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold border"
                        :class="marketSourceClass(decisionMarketSource(d))"
                        :title="`market_source ${decisionMarketSource(d)}`"
                      >{{ marketSourceLabel(decisionMarketSource(d)) }}</span>
                      <span
                        v-if="nearSignalNearest(d)"
                        class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold border"
                        :class="nearSignalNearest(d) === 'long'
                          ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40'
                          : nearSignalNearest(d) === 'short'
                            ? 'bg-rose-500/15 text-rose-400 border-rose-500/40'
                            : 'bg-zinc-500/10 text-[#A8B3C7] border-zinc-500/30'"
                        :title="`nearest ${nearSignalNearest(d)}`"
                      >near {{ nearSignalNearest(d) }}</span>
                      <span
                        v-for="gate in nearSignalMissing(d)"
                        :key="gate"
                        class="inline-flex items-center px-1 py-0.5 rounded text-[10px] font-mono bg-amber-500/10 text-amber-400/90 border border-amber-500/30"
                        :title="`missing: ${gate}`"
                      >{{ gate }}</span>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- TRADES -->
        <div v-show="store.activeTab === 'trades'" class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
          <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 class="text-sm font-mono font-bold text-white">
              Trades (ledger)
              <span class="text-[#707E94] font-normal">({{ store.tradesTabRows.length }})</span>
            </h2>
            <label class="flex items-center gap-2 text-xs font-mono text-[#A8B3C7]">
              <span class="text-[#707E94]">Instrument</span>
              <select
                class="bg-[#080B10] border border-[#1A2232] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:outline-none focus:border-cyan-500/50 cursor-pointer"
                :value="store.tradeInstFilter"
                @change="onTradeFilterChange"
              >
                <option
                  v-for="opt in tradeFilterOptions"
                  :key="opt.value || 'all'"
                  :value="opt.value"
                >{{ opt.label }}</option>
              </select>
            </label>
          </div>
          <div v-if="!store.tradesTabRows.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            Empty — {{ store.tradeInstFilter ? `no trades for ${store.tradeInstFilter}` : 'no trades recorded' }}
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead>
                <tr class="text-[#707E94] border-b border-[#1A2232]">
                  <th class="pb-2 pr-3">Time</th>
                  <th class="pb-2 pr-3">Inst</th>
                  <th class="pb-2 pr-3">Action</th>
                  <th class="pb-2 pr-3">Dir</th>
                  <th class="pb-2 pr-3">Size</th>
                  <th class="pb-2 pr-3">Price</th>
                  <th class="pb-2 text-right">PnL</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[#1A2232]/50">
                <tr v-for="t in store.tradesTabRows" :key="String(t.id)">
                  <td class="py-2 pr-3 text-[#707E94] whitespace-nowrap">{{ fmtTs(t.timestamp) }}</td>
                  <td class="py-2 pr-3 text-white">{{ t.inst_id }}</td>
                  <td class="py-2 pr-3 text-cyan-400">{{ t.action }}</td>
                  <td class="py-2 pr-3">{{ t.direction || '—' }}</td>
                  <td class="py-2 pr-3">{{ fmt(t.size, 4) }}</td>
                  <td class="py-2 pr-3">{{ fmt(t.price) }}</td>
                  <td
                    class="py-2 text-right font-bold"
                    :class="Number(t.pnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'"
                  >{{ fmt(t.pnl) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- EVENTS -->
        <div v-show="store.activeTab === 'events'" class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
          <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 class="text-sm font-mono font-bold text-white">
              Ledger events
              <span class="text-[#707E94] font-normal">({{ store.eventsTabRows.length }})</span>
            </h2>
            <div class="flex flex-wrap items-center gap-3">
              <label class="flex items-center gap-2 text-xs font-mono text-[#A8B3C7]">
                <span class="text-[#707E94]">Instrument</span>
                <select
                  class="bg-[#080B10] border border-[#1A2232] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:outline-none focus:border-cyan-500/50 cursor-pointer"
                  :value="store.eventInstFilter"
                  @change="onEventInstFilterChange"
                >
                  <option
                    v-for="opt in eventInstFilterOptions"
                    :key="opt.value || 'all'"
                    :value="opt.value"
                  >{{ opt.label }}</option>
                </select>
              </label>
              <label class="flex items-center gap-2 text-xs font-mono text-[#A8B3C7]">
                <span class="text-[#707E94]">Type</span>
                <select
                  class="bg-[#080B10] border border-[#1A2232] rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:outline-none focus:border-cyan-500/50 cursor-pointer"
                  :value="store.eventTypeFilter"
                  @change="onEventTypeFilterChange"
                >
                  <option
                    v-for="opt in eventTypeFilterOptions"
                    :key="opt.value || 'all'"
                    :value="opt.value"
                  >{{ opt.label }}</option>
                </select>
              </label>
            </div>
          </div>
          <div v-if="!store.eventsTabRows.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            Empty — {{ eventsEmptyMessage }}
          </div>
          <ul v-else class="space-y-2 max-h-[32rem] overflow-y-auto">
            <li
              v-for="(e, i) in store.eventsTabRows"
              :key="i"
              class="text-xs font-mono bg-[#080B10] border border-[#1A2232] rounded-lg px-3 py-2 text-[#A8B3C7]"
            >
              <pre class="whitespace-pre-wrap break-all m-0">{{ JSON.stringify(e, null, 2) }}</pre>
            </li>
          </ul>
        </div>

        <!-- FACTORS -->
        <div v-show="store.activeTab === 'factors'" class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
          <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 class="text-sm font-mono font-bold text-white">
              Factors
              <span class="text-[#707E94] font-normal">/api/v1/factors/&#123;inst_id&#125;</span>
            </h2>
            <label class="inline-flex items-center gap-2 text-xs font-mono text-[#A8B3C7] cursor-pointer select-none">
              <input
                type="checkbox"
                class="rounded border-[#1A2232] bg-[#080B10] text-cyan-500 focus:ring-cyan-500/40 cursor-pointer"
                :checked="store.factorsLive"
                @change="onFactorsLiveChange"
              />
              <span>实时蜡烛</span>
              <span class="text-[#707E94]">(live candles)</span>
            </label>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead>
                <tr class="text-[#707E94] border-b border-[#1A2232]">
                  <th class="pb-2 pr-3">Inst</th>
                  <th class="pb-2 pr-3">Src</th>
                  <th class="pb-2 pr-3">Price</th>
                  <th class="pb-2 pr-3">RSI14</th>
                  <th class="pb-2 pr-3">EMA9</th>
                  <th class="pb-2 pr-3">EMA21</th>
                  <th class="pb-2 pr-3">VolΔ</th>
                  <th class="pb-2 pr-3">MACD hist</th>
                  <th class="pb-2">Trend / status</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[#1A2232]/50">
                <tr v-for="row in factorRows" :key="row.instId">
                  <td class="py-2 pr-3 text-white font-bold">{{ row.instId }}</td>
                  <td class="py-2 pr-3">
                    <span
                      class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold border"
                      :class="factorSourceClass(row.f?.source, row.f?.data_quality_reason)"
                      :title="row.f?.data_quality_reason || row.f?.source || ''"
                    >{{ factorSourceLabel(row.f?.source, row.f?.data_quality_reason) }}</span>
                  </td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.price) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.rsi_14) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.ema_9) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.ema_21) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.volume_ratio, 2) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.macd?.histogram) }}</td>
                  <td class="py-2">
                    <span v-if="row.loading" class="text-cyan-400/80">loading…</span>
                    <span
                      v-else-if="row.error"
                      class="text-amber-400/90 truncate max-w-[14rem] inline-block align-bottom"
                      :title="row.error"
                    >err: {{ row.error }}</span>
                    <span v-else>{{ row.f?.trend_15m || '—' }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </main>

    <footer class="border-t border-[#1A2232] bg-[#0A0D14] py-3 text-center text-[10px] font-mono text-[#707E94]">
      Keel Trader · Phase U2 monitor · binds to /health + /api/v1/* only ·
      <a href="/docs" class="text-cyan-500/80 hover:text-cyan-400">API docs</a>
    </footer>
  </div>
</template>
