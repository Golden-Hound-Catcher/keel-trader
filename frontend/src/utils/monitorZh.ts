/**
 * Chinese-first Monitor UX helpers (labels / blocker phrases / status hero).
 * Display-only — does not change store/API semantics.
 */

/** Known arming / first_live blocker codes → short 中文. */
const BLOCKER_ZH: Record<string, string> = {
  insufficient_shadow_markout_sample: '影子样本不足',
  probe_win_rate_net_roundtrip_below_threshold: '近探净胜率偏低',
  avg_net_roundtrip_markout_bps_below_threshold: '净回报偏低',
  full_gate_win_rate_net_roundtrip_below_threshold: '满门净胜率偏低',
  insufficient_post_e31_full_gate_sample: '满门(新队列)样本不足',
  'no recent shadow_fill rehearsal': '缺少近期影子排练',
  'OKX keys not configured': '未配置 OKX 密钥',
  'max_notional_per_instrument must be > 0': '单品种名义上限须 > 0',
  'max_daily_loss_usdt must be > 0': '日损上限须 > 0',
  'live_max_notional_per_instrument must be > 0': '实盘名义上限须 > 0',
  'live_max_contracts_per_instrument must be > 0': '实盘张数上限须 > 0',
}

/** Prefix / substring patterns for dynamic blocker strings. */
const BLOCKER_PATTERNS: Array<{ test: RegExp; zh: string }> = [
  { test: /^okx_environment must be/i, zh: 'OKX 环境须为 live 或 demo' },
  { test: /okx_environment=demo/i, zh: '当前是模拟盘，切实盘需改环境' },
  { test: /okx_capability/i, zh: 'OKX 能力未就绪' },
  { test: /shadow_fill rehearsal/i, zh: '缺少近期影子排练' },
  { test: /markout/i, zh: '标记收益未达标' },
  { test: /win_rate/i, zh: '胜率未达标' },
  { test: /kill/i, zh: '杀开关相关' },
]

export function blockerToZh(code: string): string {
  const raw = String(code || '').trim()
  if (!raw) return '—'
  if (BLOCKER_ZH[raw]) return BLOCKER_ZH[raw]
  for (const { test, zh } of BLOCKER_PATTERNS) {
    if (test.test(raw)) return zh
  }
  // Soften snake_case codes into short readable Chinese-ish fallback
  if (/^[a-z0-9_]+$/i.test(raw) && raw.includes('_')) {
    return raw.replace(/_/g, ' ')
  }
  return raw
}

export function economicEvidenceZh(code: string | null | undefined): string {
  const c = String(code || 'none').toLowerCase()
  switch (c) {
    case 'none':
      return '无'
    case 'probe':
      return '近探'
    case 'full_gate':
      return '满门'
    case 'mixed':
      return '混合'
    case 'stale_pre_e31':
      return '旧样本(pre_e31)'
    case 'shadow_non_probe':
      return '影子非探'
    case 'insufficient':
      return '不足'
    default:
      return c
  }
}

export type StatusHeroTone = 'rose' | 'violet' | 'amber' | 'emerald' | 'sky' | 'zinc'

export interface StatusHeroInput {
  killSwitch: boolean
  shadowMode: boolean
  nearProbe: boolean
  environment: string
  policy: string
  armingReady: boolean
  armingBlockers: string[]
  firstLiveAllowed: boolean
  firstLiveBlockers: string[]
  economicPassed: boolean | null
  economicEnabled: boolean
  workerStale: boolean
}

export interface StatusHeroModel {
  headline: string
  tone: StatusHeroTone
  lines: Array<{ label: string; value: string; emphasize?: boolean }>
  primaryBlocker: string | null
  canArmLabel: string
}

function primaryBlockerOf(input: StatusHeroInput): string | null {
  const pool = [
    ...(input.armingBlockers || []),
    ...(input.firstLiveBlockers || []),
  ]
  if (!pool.length) return null
  return blockerToZh(pool[0])
}

export function envZh(raw: unknown): string {
  const e = String(raw || '').trim().toLowerCase()
  if (e === 'demo') return '模拟盘'
  if (e === 'live') return '实盘'
  if (e === 'paper') return '本地模拟'
  return String(raw || '—') || '—'
}

export function policyZh(raw: unknown): string {
  const p = String(raw || '').trim().toLowerCase()
  if (p === 'llm') return 'LLM'
  if (p === 'rule' || p === 'rules') return '规则'
  if (p === 'stub') return '占位'
  return String(raw || '—') || '—'
}

export function exchangeModeZh(raw: unknown): string {
  const m = String(raw || '').trim().toLowerCase()
  if (m.includes('okx')) return '欧易'
  if (m.includes('paper')) return '本地模拟'
  return String(raw || '—') || '—'
}

export function marketSourceZh(raw: unknown): string {
  const s = String(raw || '').trim().toLowerCase()
  if (s === 'okx_public' || s === 'okx') return '欧易行情'
  if (s === 'synthetic' || s.startsWith('synthetic')) return '合成行情'
  if (s === 'ledger') return '账本'
  if (s === 'mixed') return '混合'
  return String(raw || '—') || '—'
}

export function actionZh(action: string | undefined | null): string {
  const a = String(action || '').toUpperCase()
  if (a === 'BUY_LONG') return '做多'
  if (a === 'SELL_SHORT') return '做空'
  if (a === 'WAIT') return '观望'
  if (a === 'CLOSE' || a === 'CLOSE_LONG' || a === 'CLOSE_SHORT') return '平仓'
  if (a === 'OPEN') return '开仓'
  if (a === 'BUY') return '买入'
  if (a === 'SELL') return '卖出'
  return action || '—'
}

export function sideZh(side: string | undefined | null): string {
  const s = String(side || '').toLowerCase()
  if (s === 'long') return '多'
  if (s === 'short') return '空'
  if (s === 'net') return '净'
  return side || '—'
}

const MISSING_GATE_ZH: Record<string, string> = {
  volume_ok: '量能',
  rsi_ok: 'RSI',
  macd_ok: 'MACD',
  trend_ok: '趋势',
  rr_ok: '盈亏比',
  risk_ok: '风控',
  atr_ok: 'ATR',
  alignment_ok: '多周期同向',
  edge_ok: '边际不足',
}

export function missingGateZh(raw: unknown): string {
  const g = String(raw || '').trim()
  if (!g) return ''
  if (MISSING_GATE_ZH[g]) return MISSING_GATE_ZH[g]
  return g.replace(/_ok$/, '').replace(/_/g, ' ')
}

export function radarNearestLabel(nearest: string | null | undefined, action: string): string {
  const a = (action || '').toUpperCase()
  if (a === 'BUY_LONG') return '已做多'
  if (a === 'SELL_SHORT') return '已做空'
  const n = (nearest || 'none').toLowerCase()
  if (n === 'long') return '近多'
  if (n === 'short') return '近空'
  return '观望'
}

export function fmtConfidence(v: unknown): string {
  const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
  if (!Number.isFinite(n)) return '—'
  const pct = n <= 1 ? n * 100 : n
  return `${pct.toFixed(0)}%`
}

/** System / infra errors → 中文；模型论述原文保留。 */
export function humanizeError(raw: unknown): string {
  const s = String(raw || '').replace(/\s+/g, ' ').trim()
  if (!s) return ''
  if (/empty content|content was null/i.test(s)) return '模型返回空正文'
  if (/JSON parse error/i.test(s)) return '模型输出不是 JSON，本轮已改 WAIT'
  if (/\b429\b|rate-limited|rate limit/i.test(s)) return 'LLM 限流，下一轮会重试'
  if (/JSON parse error|Expecting value: line/i.test(s)) return '模型返回无法解析的 JSON'
  if (/llm unavailable/i.test(s)) return '模型暂时不可用'
  if (/简易模式|acctLv=1/i.test(s)) return '欧易账户仍是简易模式，无法下永续'
  if (/All operations failed/i.test(s)) return '欧易拒单（账户模式或字段）'
  if (/'NoneType' object has no attribute 'strip'/i.test(s)) return '模型返回格式无法解析'
  if (/Risk:reward .+ below minimum/i.test(s)) return '盈亏比未达下限'
  if (s.length > 96) return `${s.slice(0, 93)}…`
  return s
}

export function displayReason(raw: unknown): string {
  return humanizeError(raw) || '—'
}

/**
 * One calm answer to "are we trading, rehearsing, or frozen?".
 * Demo with kill off is live-sim trading — not an "arming" story.
 */
export function buildStatusHero(input: StatusHeroInput): StatusHeroModel {
  const env = envZh(input.environment)
  const policy = policyZh(input.policy)
  const primary = primaryBlockerOf(input)
  const tradingNow = !input.killSwitch && !input.shadowMode
  const envKey = (input.environment || '').toLowerCase()

  let headline = '运行中'
  let tone: StatusHeroTone = 'zinc'
  let orderLine = '待命'

  if (input.workerStale) {
    headline = '调度可能停滞'
    tone = 'amber'
    orderLine = '周期过久未完成'
  } else if (input.killSwitch) {
    headline = '熔断中 · 不会下单'
    tone = 'rose'
    orderLine = '杀开关开启'
  } else if (input.shadowMode) {
    headline = '影子排练 · 不会真实下单'
    tone = 'violet'
    orderLine = input.nearProbe ? '只记影子成交（含近探）' : '只记影子成交'
  } else if (envKey === 'demo') {
    headline = '模拟盘交易中'
    tone = 'emerald'
    orderLine = '会向欧易模拟盘下单'
  } else if (envKey === 'live') {
    headline = '实盘交易中'
    tone = 'rose'
    orderLine = '会向欧易实盘下单'
  } else if (envKey === 'paper') {
    headline = '本地模拟中'
    tone = 'sky'
    orderLine = '不连接交易所'
  } else {
    headline = '运行中'
    tone = 'emerald'
    orderLine = '请核对环境'
  }

  const lines: StatusHeroModel['lines'] = [
    { label: '盘口', value: env },
    { label: '策略', value: policy },
    { label: '下单', value: orderLine, emphasize: tradingNow || input.killSwitch },
  ]
  if (input.killSwitch) {
    lines.push({ label: '熔断', value: '开', emphasize: true })
  }
  if (input.shadowMode) {
    lines.push({ label: '影子', value: input.nearProbe ? '开 · 近探' : '开', emphasize: true })
  }
  if (!tradingNow && primary) {
    lines.push({ label: '阻断', value: primary, emphasize: true })
  }

  const canArmLabel = tradingNow
    ? (envKey === 'demo' ? '模拟盘已在交易' : '已在交易')
    : input.armingReady
      ? '清单已绿，仍未开盘'
      : '暂未开盘'

  return {
    headline,
    tone,
    lines,
    primaryBlocker: tradingNow ? null : primary,
    canArmLabel,
  }
}

export const HERO_TONE_CLASS: Record<StatusHeroTone, string> = {
  rose: 'border-rose-500/40 bg-rose-500/10',
  violet: 'border-violet-500/40 bg-violet-500/10',
  amber: 'border-amber-500/40 bg-amber-500/10',
  emerald: 'border-emerald-500/40 bg-emerald-500/10',
  sky: 'border-sky-500/40 bg-sky-500/10',
  zinc: 'border-[#1A2232] bg-[#0D121B]',
}

export const HERO_HEADLINE_CLASS: Record<StatusHeroTone, string> = {
  rose: 'text-rose-300',
  violet: 'text-violet-200',
  amber: 'text-amber-300',
  emerald: 'text-emerald-300',
  sky: 'text-sky-300',
  zinc: 'text-white',
}
