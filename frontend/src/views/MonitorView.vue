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

function factorSourceLabel(source: string | undefined): string {
  if (!source) return '—'
  if (source === 'okx_public') return 'live'
  if (source === 'ledger') return 'ledger'
  return source
}

function factorSourceClass(source: string | undefined): string {
  if (source === 'okx_public') return 'bg-cyan-500/15 text-cyan-400 border-cyan-500/40'
  if (source === 'ledger') return 'bg-zinc-500/10 text-[#A8B3C7] border-zinc-500/30'
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
  const instCount = Array.isArray(c?.instruments) ? c!.instruments.length : store.watchlist.length
  const maxPos = c?.max_positions ?? '—'
  const kill = c?.kill_switch ?? store.status?.kill_switch ?? false
  const notify = c?.notify_configured
  const policy = c?.decision_policy || store.status?.decision_policy || '—'
  const intervalSec = cycleIntervalSeconds.value
  return {
    env,
    mode,
    instCount,
    maxPos,
    kill: kill ? 'ON' : 'off',
    notify: notify == null ? '—' : notify ? 'yes' : 'no',
    policy,
    cycle: formatCycleIntervalLabel(intervalSec),
    cycleTitle: `Trader cycle interval ${intervalSec}s; stale threshold uses max(2×interval, interval+300) = ${workerStaleThreshold.value}s`,
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
              <div class="text-[11px] font-mono text-[#707E94] mt-1">
                {{ store.dailyPnl?.date || '—' }} · {{ store.dailyPnl?.source || 'ledger' }}
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
                <div>OKX: {{ store.status?.credentials?.okx ? 'yes' : 'no' }}</div>
                <div>LLM: {{ store.status?.credentials?.llm ? 'yes' : 'no' }}</div>
              </div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1 truncate" :title="store.status?.ledger_db">
                {{ store.status?.mode || '—' }}
              </div>
            </div>
          </div>

          <div class="bg-[#0D121B] border border-[#1A2232] rounded-xl px-4 py-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] font-mono">
            <div class="flex items-center gap-1.5 text-[#707E94]">
              <Settings2 class="w-3.5 h-3.5 text-cyan-400" />
              <span class="text-white font-bold">Config</span>
            </div>
            <span class="text-[#A8B3C7]">env <span class="text-white">{{ configStrip.env }}</span></span>
            <span class="text-[#A8B3C7]">mode <span class="text-white">{{ configStrip.mode }}</span></span>
            <span class="text-[#A8B3C7]">policy <span class="text-white">{{ configStrip.policy }}</span></span>
            <span class="text-[#A8B3C7]">instruments <span class="text-white">{{ configStrip.instCount }}</span></span>
            <span class="text-[#A8B3C7]">max_pos <span class="text-white">{{ configStrip.maxPos }}</span></span>
            <span class="text-[#A8B3C7]">kill <span :class="killSwitchOn ? 'text-rose-400' : 'text-white'">{{ configStrip.kill }}</span></span>
            <span class="text-[#A8B3C7]">notify <span class="text-white">{{ configStrip.notify }}</span></span>
            <span class="text-[#A8B3C7]" :title="configStrip.cycleTitle">周期 <span class="text-white">{{ configStrip.cycle }}</span></span>
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
                  <td class="py-2 pr-3">{{ fmt(d.confidence, 2) }}</td>
                  <td class="py-2 pr-3">{{ fmt(d.entry_price) }}</td>
                  <td class="py-2 text-zinc-400 max-w-md truncate" :title="d.reason">{{ d.reason || '—' }}</td>
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
                      :class="factorSourceClass(row.f?.source)"
                    >{{ factorSourceLabel(row.f?.source) }}</span>
                  </td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.price) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.rsi_14) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.ema_9) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.ema_21) }}</td>
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
