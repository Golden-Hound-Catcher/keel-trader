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

/**
 * One calm answer to "can we trade / what are we waiting on?".
 */
export function buildStatusHero(input: StatusHeroInput): StatusHeroModel {
  const env = (input.environment || '—').toLowerCase()
  const policy = input.policy || '—'
  const primary = primaryBlockerOf(input)

  let headline = '运行中'
  let tone: StatusHeroTone = 'zinc'

  const waitingEcon =
    input.economicEnabled
    && input.economicPassed === false
    && (input.armingBlockers || []).some((b) =>
      /markout|win_rate|full_gate|sample|经济|insufficient/i.test(String(b)),
    )

  if (input.firstLiveAllowed && !input.killSwitch) {
    headline = '可开最小实盘'
    tone = 'emerald'
  } else if (input.workerStale) {
    headline = 'Worker 可能停滞'
    tone = 'amber'
  } else if (waitingEcon || (input.killSwitch && primary && /样本|胜率|回报|满门/.test(primary))) {
    headline = '等待经济样本'
    tone = 'amber'
  } else if (input.killSwitch && input.armingReady) {
    headline = '可武装 · 杀开关仍开'
    tone = 'sky'
  } else if (input.killSwitch && input.shadowMode) {
    headline = '观测中 · 不可实盘'
    tone = 'violet'
  } else if (input.killSwitch) {
    headline = '杀开关开启 · 不可实盘'
    tone = 'rose'
  } else if (input.shadowMode) {
    headline = '影子模式 · 无真实下单'
    tone = 'violet'
  } else {
    headline = '运行中 · 留意风控'
    tone = 'emerald'
  }

  const canArmLabel = input.armingReady ? '可以武装' : '暂不可武装'

  return {
    headline,
    tone,
    lines: [
      { label: '环境', value: env || '—' },
      { label: '策略', value: policy },
      {
        label: '杀开关',
        value: input.killSwitch ? '开（冻结实盘）' : '关',
        emphasize: input.killSwitch,
      },
      {
        label: '影子模式',
        value: input.shadowMode ? '开（只记影子成交）' : '关',
        emphasize: input.shadowMode,
      },
      {
        label: '近探',
        value: input.nearProbe ? '开（近信号排练）' : '关',
        emphasize: input.nearProbe,
      },
      { label: '可否武装', value: canArmLabel, emphasize: !input.armingReady },
      {
        label: '首要阻断',
        value: primary || (input.armingReady ? '无' : '—'),
        emphasize: Boolean(primary),
      },
    ],
    primaryBlocker: primary,
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
