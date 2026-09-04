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
  })),
)

const lastCycle = computed(() => store.status?.last_cycle ?? null)
const lastCycleActions = computed(() => {
  const counts = lastCycle.value?.decision_counts || {}
  return Object.entries(counts)
    .map(([k, v]) => `${k}:${v}`)
    .join(' · ')
})

/** last_cycle.risk_denies is a count (int); schema has no reason list. */
const riskDeniesCount = computed(() => {
  const raw = lastCycle.value?.risk_denies
  const n = typeof raw === 'number' ? raw : Number(raw ?? 0)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
})
const riskDeniesWarn = computed(() => riskDeniesCount.value > 0)

/** Read-only: armed via KEEL_KILL_SWITCH (status API); no admin toggle. */
const killSwitchOn = computed(() => Boolean(store.status?.kill_switch))
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

          <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
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
                <LayoutGrid class="w-4 h-4 text-blue-400" />
                Positions
              </div>
              <div class="text-2xl font-black font-mono text-white">{{ store.positionCount }}</div>
              <div class="text-[11px] font-mono text-[#707E94] mt-1">
                {{ store.positionsSource || '—' }}
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
              <div>
                <div class="text-[#707E94]">Risk denies</div>
                <div class="mt-0.5">
                  <span
                    class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-bold border tabular-nums"
                    :class="riskDeniesWarn
                      ? 'bg-amber-500/15 text-amber-400 border-amber-500/40'
                      : 'bg-zinc-500/10 text-[#707E94] border-zinc-500/20'"
                    :title="riskDeniesWarn
                      ? `${riskDeniesCount} instrument(s) denied by risk gates this cycle`
                      : 'No risk-gate denies this cycle'"
                  >
                    {{ riskDeniesCount }}
                  </span>
                </div>
              </div>
              <div>
                <div class="text-[#707E94]">Errors</div>
                <div :class="(lastCycle.errors?.length || 0) ? 'text-rose-400' : 'text-white'">
                  {{ lastCycle.errors?.length ?? 0 }}
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
          <h2 class="text-sm font-mono font-bold text-white mb-3">
            Positions
            <span class="text-[#707E94] font-normal">({{ store.positionCount }} · {{ store.positionsSource || '—' }})</span>
          </h2>
          <div v-if="!store.positions.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            No open positions
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
                <tr v-for="p in store.positions" :key="p.inst_id + p.side" class="hover:bg-[#121824]/50">
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
          <h2 class="text-sm font-mono font-bold text-white mb-3">Decisions (ledger)</h2>
          <div v-if="!store.decisions.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            Empty — worker has not written decisions yet
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
                <tr v-for="d in store.decisions" :key="String(d.id)">
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
          <h2 class="text-sm font-mono font-bold text-white mb-3">Trades (ledger)</h2>
          <div v-if="!store.trades.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            No trades recorded
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
                <tr v-for="t in store.trades" :key="String(t.id)">
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
          <h2 class="text-sm font-mono font-bold text-white mb-3">Ledger events</h2>
          <div v-if="!store.events.length" class="py-10 text-center text-xs font-mono text-[#707E94] border border-dashed border-[#1A2232] rounded-lg">
            No events
          </div>
          <ul v-else class="space-y-2 max-h-[32rem] overflow-y-auto">
            <li
              v-for="(e, i) in store.events"
              :key="i"
              class="text-xs font-mono bg-[#080B10] border border-[#1A2232] rounded-lg px-3 py-2 text-[#A8B3C7]"
            >
              <pre class="whitespace-pre-wrap break-all m-0">{{ JSON.stringify(e, null, 2) }}</pre>
            </li>
          </ul>
        </div>

        <!-- FACTORS -->
        <div v-show="store.activeTab === 'factors'" class="bg-[#0D121B] border border-[#1A2232] rounded-xl p-4">
          <h2 class="text-sm font-mono font-bold text-white mb-3">
            Factors
            <span class="text-[#707E94] font-normal">/api/v1/factors/&#123;inst_id&#125;</span>
          </h2>
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
                  <th class="pb-2">Trend</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[#1A2232]/50">
                <tr v-for="row in factorRows" :key="row.instId">
                  <td class="py-2 pr-3 text-white font-bold">{{ row.instId }}</td>
                  <td class="py-2 pr-3 text-[#707E94]">{{ row.f?.source || '—' }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.price) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.rsi_14) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.ema_9) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.ema_21) }}</td>
                  <td class="py-2 pr-3">{{ fmt(row.f?.macd?.histogram) }}</td>
                  <td class="py-2">{{ row.f?.trend_15m || '—' }}</td>
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
