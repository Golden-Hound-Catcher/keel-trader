/** Keel API response types (Phase U1 — read-only control plane). */

export interface KeelHealth {
  status: string
  service: string
  version: string
  timestamp: number
  environment: string
}

export interface KeelRiskDenyReason {
  gate: string
  reason?: string
}

export interface KeelCycleError {
  inst_id?: string | null
  error: string
}

export interface KeelLastCycle {
  timestamp: number
  mode: string
  adapter?: string
  policy?: string
  instruments?: number
  decision_counts?: Record<string, number>
  /** Count of instruments denied by risk gates this cycle. */
  risk_denies?: number
  /** Capped list of deny gate/reason pairs (see backend RISK_DENY_REASONS_CAP). */
  risk_deny_reasons?: KeelRiskDenyReason[]
  /** Full count of non-risk instrument errors this cycle (list may be capped). */
  error_count?: number
  /** Capped per-instrument non-risk errors (see backend CYCLE_ERRORS_CAP). */
  errors?: KeelCycleError[]
  policy_success?: boolean | null
  duration_ms?: number
  /** Candle quality aggregate: okx_public | synthetic | mixed | unknown */
  market_source?: string | null
  /** Q3.5: near-probe skips in this cycle (status annotation). */
  probe_skips?: number
  by_skip_reason?: Record<string, number>
  top_skip_reason?: string | null
  last_probe_skip_reason?: string | null
  /** R6: per-instrument multi-TF trends from last cycle (soft-fail if absent). */
  instrument_trends?: Array<{
    inst_id: string
    trend_15m?: string | null
    trend_1h?: string | null
    trend_4h?: string | null
  }>
}

/** S1 economic arming gate summary (read-only; never clears kill-switch). */
export interface KeelArmingEconomicInstrument {
  fill_count?: number
  probe_count?: number
  sample_count?: number
  probe_sample_count?: number
  avg_net_roundtrip_markout_bps?: number | null
  win_rate_net_roundtrip?: number | null
  probe_win_rate_net_roundtrip?: number | null
  probe_avg_net_roundtrip_markout_bps?: number | null
  by_skip_reason?: Record<string, number>
}

export interface KeelArmingEconomic {
  enabled?: boolean
  hours?: number
  horizon_seconds?: number
  fill_count?: number
  probe_count?: number
  sample_count?: number
  probe_sample_count?: number
  probe_win_rate_net_roundtrip?: number | null
  win_rate_net_roundtrip?: number | null
  avg_net_roundtrip_markout_bps?: number | null
  min_fills?: number
  min_probe_fills?: number
  min_markout_sample?: number
  min_probe_win_rate_net_rt?: number
  min_avg_net_rt_bps?: number
  fills_ok?: boolean
  sample_ok?: boolean
  passed?: boolean
  by_skip_reason?: Record<string, number>
  /** Diagnostic per-instrument snapshots; overall gate stays aggregate. */
  by_instrument?: Record<string, KeelArmingEconomicInstrument>
  note?: string
}

export interface KeelArmingStatus {
  ready_to_arm: boolean
  kill_switch: boolean
  capability: string
  blockers: string[]
  warnings: string[]
  /** S1: economic acceptance gates summary when evaluated. */
  economic?: KeelArmingEconomic | null
}

/** S2 first-live Stage T gate checklist (read-only; never clears kill). */
export interface KeelFirstLiveStatus {
  allowed_now: boolean
  kill_switch: boolean
  shadow_mode: boolean
  shadow_near_probe: boolean
  capability: string
  ready_to_arm: boolean
  blockers: string[]
  economic?: KeelArmingEconomic | null
  suggested_live_caps?: {
    live_max_notional_per_instrument?: number
    live_max_contracts_per_instrument?: number
    env_keys?: string[]
  }
  human_steps: string[]
  note?: string
}

export interface KeelStatus {
  version: string
  mode: string
  uptime_seconds: number
  environment: string
  credentials: {
    okx: boolean
    llm: boolean
  }
  ledger_db: string
  kill_switch: boolean
  /** Q1: shadow execution — ledger fills without place_order (KEEL_SHADOW_MODE). */
  shadow_mode?: boolean
  /** Q3.1: near-signal shadow probe armed (KEEL_SHADOW_NEAR_PROBE; kill+shadow only). */
  shadow_near_probe?: boolean
  /** Q3.1: per-instrument probe cooldown seconds. */
  shadow_near_probe_cooldown_seconds?: number
  /** Q3.4: fee-aware edge hurdle mode (round_trip|open). */
  shadow_near_probe_edge_mode?: string
  shadow_near_probe_min_edge_bps?: number | null
  shadow_near_probe_hurdle_bps?: number | null
  /** Active decision policy name (rule|stub|llm) from build_decision_policy. */
  decision_policy: string
  last_cycle?: KeelLastCycle | null
  /** Seconds since last_cycle.timestamp; null if missing/unparsable. */
  seconds_since_last_cycle?: number | null
  /** True when lag exceeds interval-based stale threshold (same as /ready). */
  worker_stale?: boolean
  /** Q1: OKX key capability — none|paper|read|trade|error (no order placement). */
  okx_capability?: string
  okx_capability_detail?: string | null
  /** Q1: read-only arming checklist (never writes kill-switch). */
  arming?: KeelArmingStatus | null
  /** S2: first-live checklist (allowed_now false while kill on / econ fail). */
  first_live?: KeelFirstLiveStatus | null
}

export interface KeelConfig {
  environment: string
  max_positions: number
  max_daily_loss: number
  max_asset_margin: number
  max_notional_per_instrument?: number
  max_contracts_per_instrument?: number
  /** First-live tighter caps (KEEL_LIVE_MAX_*). */
  live_max_notional_per_instrument?: number
  live_max_contracts_per_instrument?: number
  /** Caps risk gates apply right now (live tighter when env=live and not shadow). */
  effective_max_notional_per_instrument?: number
  effective_max_contracts_per_instrument?: number
  llm_model: string
  kill_switch: boolean
  /** Q1: shadow execution flag (same as status.shadow_mode). */
  shadow_mode?: boolean
  /** Q3.1: near-signal shadow probe (same as status.shadow_near_probe). */
  shadow_near_probe?: boolean
  shadow_near_probe_cooldown_seconds?: number
  shadow_near_probe_max_missing?: number
  /** Q3.4: fee-aware edge hurdle (same as status). */
  shadow_near_probe_edge_mode?: string
  shadow_near_probe_min_edge_bps?: number | null
  shadow_near_probe_hurdle_bps?: number | null
  /** Active decision policy name (same as status.decision_policy). */
  decision_policy: string
  instruments: string[]
  notify_configured: boolean
  notify_alerts_only?: boolean
  notify_format?: string
  exchange_mode: string
  /** Trader cycle interval in seconds (KEEL_CYCLE_INTERVAL_SECONDS / observe preset). */
  cycle_interval_seconds: number
  /** Q0 observe cadence preset: default|fast|slow when KEEL_OBSERVE_PRESET set. */
  observe_preset?: string | null
  scheduler_jobs?: string[]
  /** Q1: OKX key capability (same as status.okx_capability). */
  okx_capability?: string
  okx_capability_detail?: string | null
}

export interface KeelDailyPnl {
  date: string
  realized_pnl: number
  source: string
}

export interface KeelPosition {
  inst_id: string
  side: string
  size: number
  avg_price: number
  mark_price: number
  upl: number | null
  upl_ratio: number
  leverage: number
  margin: number
}

export interface KeelPositionsResponse {
  count: number
  positions: KeelPosition[]
  source: string
}

export interface KeelBalance {
  total_equity: number
  available: number
  cash: number
  unrealized_pnl: number
  margin_usage_pct: number
  source: string
}

export interface KeelDecision {
  id: number | string
  timestamp: string | number
  inst_id: string
  action: string
  confidence: number
  entry_price?: number
  take_profit?: number
  stop_loss?: number
  reason?: string
  calculus_data?: Record<string, unknown>
  /** Q0 near-signal gate diagnostics (also under calculus_data.signal_diag). */
  signal_diag?: Record<string, unknown> | null
  policy_name?: string
  prompt_modules?: string[] | null
}


export interface KeelNearestSignalItem {
  inst_id: string
  action: string
  timestamp: string | number
  nearest?: string | null
  missing?: string[]
  rsi_14?: number | null
  trend_15m?: string | null
  trend_1h?: string | null
  trend_4h?: string | null
  volume_ratio?: number | null
  ema_9?: number | null
  ema_21?: number | null
  macd_histogram?: number | null
}

export interface KeelNearestSignalsSummary {
  waiting: number
  long_nearest: number
  short_nearest: number
  fired_long: number
  fired_short: number
}

/** Soft-fail Overview radar card from GET /api/v1/signals/nearest. */
export interface KeelNearestSignals {
  hours: number
  count: number
  summary: KeelNearestSignalsSummary
  signals: KeelNearestSignalItem[]
}

export interface KeelDecisionStats {
  hours: number
  decision_count: number
  by_action: Record<string, number>
  by_policy: Record<string, number>
  wait_rate: number
  risk_deny_events: number
  cycle_count: number
  avg_cycle_duration_ms: number | null
  /** Filter echo: okx_public | synthetic | any */
  market_source?: string
}

/** Soft-fail Overview shadow rehearsal stats from GET /api/v1/stats/shadow. */
export interface KeelShadowMarkoutAction {
  sample_count: number
  avg_markout_bps?: number | null
  median_markout_bps?: number | null
  win_rate?: number | null
  avg_net_open_markout_bps?: number | null
  median_net_open_markout_bps?: number | null
  win_rate_net_open?: number | null
  avg_net_roundtrip_markout_bps?: number | null
  median_net_roundtrip_markout_bps?: number | null
  win_rate_net_roundtrip?: number | null
}

export interface KeelShadowMarkoutHorizon {
  horizon_seconds: number
  sample_count: number
  skipped: number
  /** Gross mid markout (fee-unaware). */
  avg_markout_bps?: number | null
  median_markout_bps?: number | null
  win_rate?: number | null
  avg_net_open_markout_bps?: number | null
  median_net_open_markout_bps?: number | null
  win_rate_net_open?: number | null
  avg_net_roundtrip_markout_bps?: number | null
  median_net_roundtrip_markout_bps?: number | null
  win_rate_net_roundtrip?: number | null
  probe_sample_count?: number
  probe_avg_markout_bps?: number | null
  probe_median_markout_bps?: number | null
  probe_win_rate?: number | null
  probe_avg_net_open_markout_bps?: number | null
  probe_median_net_open_markout_bps?: number | null
  probe_win_rate_net_open?: number | null
  probe_avg_net_roundtrip_markout_bps?: number | null
  probe_median_net_roundtrip_markout_bps?: number | null
  probe_win_rate_net_roundtrip?: number | null
  funding_applied_count?: number
  by_action?: Record<string, KeelShadowMarkoutAction>
}

export interface KeelShadowMarkout {
  price_source?: string
  horizons: KeelShadowMarkoutHorizon[]
}

/** Q3.3 OKX-official fee model on /stats/shadow*. */
export interface KeelShadowFeeModel {
  source?: string
  inst_type?: string
  margin?: string
  level?: string | null
  maker_bps?: number
  taker_bps?: number
  role?: string
  open_fee_bps?: number
  round_trip_fee_bps?: number
  funding_note?: string
  funding_applied?: boolean
}

/** Q3.5 near-probe skip aggregates (durable shadow_near_probe_skip events). */
export interface KeelProbeSkips {
  count: number
  by_skip_reason?: Record<string, number>
  top_skip_reason?: string | null
  last_reason?: string | null
  last_timestamp?: number | null
}

export interface KeelShadowInstrumentMarkout300 {
  sample_count?: number
  probe_sample_count?: number
  avg_net_roundtrip_markout_bps?: number | null
  win_rate_net_roundtrip?: number | null
  probe_avg_net_roundtrip_markout_bps?: number | null
  probe_win_rate_net_roundtrip?: number | null
}

export interface KeelShadowInstrumentStats {
  count: number
  probe_count?: number
  by_action?: Record<string, number>
  by_skip_reason?: Record<string, number>
  markout_300s?: KeelShadowInstrumentMarkout300 | null
}

export interface KeelShadowStats {
  hours: number
  count: number
  by_action: Record<string, number>
  by_policy?: Record<string, number>
  /** Q3 near-probe fills in lookback. */
  probe_count?: number
  last_timestamp?: number | null
  /** Q3.5 skip counts (hours-filterable). */
  probe_skips?: KeelProbeSkips | null
  by_skip_reason?: Record<string, number>
  /** Q3.3 fee model (soft-fail if older API). */
  fee_model?: KeelShadowFeeModel | null
  /** Q3.2 offline markout nest (soft-fail if older API). */
  markout?: KeelShadowMarkout | null
  /** Stage R/S: per-instrument breakdown (soft-fail if absent). */
  by_instrument?: Record<string, KeelShadowInstrumentStats>
}

/** Soft-fail Overview quality scorecard from GET /api/v1/stats/quality. */
export interface KeelQualityShadow {
  count: number
  by_action: Record<string, number>
  by_policy?: Record<string, number>
  /** Q3 near-probe fills in lookback. */
  probe_count?: number
  last_timestamp?: number | null
}

export interface KeelQualityInstrumentStats {
  decision_count: number
  wait_rate: number
  near_signal_rate: number
  by_action?: Record<string, number>
  market_source?: Record<string, number>
}

export interface KeelQualityStats {
  hours: number
  market_source: Record<string, number>
  decision_count: number
  wait_rate: number
  by_action: Record<string, number>
  near_signal_rate: number
  shadow: KeelQualityShadow
  cycle_count: number
  avg_cycle_duration_ms: number | null
  /** Stage R/S: per-instrument breakdown (soft-fail if absent). */
  by_instrument?: Record<string, KeelQualityInstrumentStats>
}

export interface KeelDecisionsResponse {
  count: number
  decisions: KeelDecision[]
}

export interface KeelTrade {
  id: number | string
  timestamp: string | number
  inst_id: string
  action: string
  direction?: string
  size?: number
  price?: number
  pnl?: number
  strategy_tag?: string
  reason?: string
  metadata?: Record<string, unknown>
}

export interface KeelTradesResponse {
  count: number
  trades: KeelTrade[]
}

export interface KeelEventsResponse {
  count: number
  events: Array<Record<string, unknown>>
}

export interface KeelFactors {
  inst_id: string
  /** ledger = worker snapshot; okx_public = live candles (?live=1) */
  source: 'ledger' | 'okx_public' | string
  timestamp?: string | number
  price?: number
  ema_9?: number
  ema_21?: number
  ema_55?: number
  rsi_14?: number
  rsi_7?: number
  atr_14?: number
  macd?: {
    line?: number
    signal?: number
    histogram?: number
  }
  trend_15m?: string
  trend_1h?: string
  trend_4h?: string
  volume_ratio?: number
  bollinger?: Record<string, number>
  candle_count?: number
  /** Worker snapshot quality (okx_public / synthetic / synthetic_fallback:…); live = okx_public */
  data_quality_reason?: string | null
}

/** Default watchlist — mirrors keel.domain.instruments.DEFAULT_CRYPTO_INSTRUMENTS */
export const KEEL_DEFAULT_INSTRUMENTS = [
  'BTC-USDT-SWAP',
  'ETH-USDT-SWAP',
  'SOL-USDT-SWAP',
  'DOGE-USDT-SWAP',
  'SUI-USDT-SWAP',
  'LINK-USDT-SWAP',
] as const
