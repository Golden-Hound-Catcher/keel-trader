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
  last_cycle?: KeelLastCycle | null
  /** Seconds since last_cycle.timestamp; null if missing/unparsable. */
  seconds_since_last_cycle?: number | null
}

export interface KeelConfig {
  environment: string
  max_positions: number
  max_daily_loss: number
  max_asset_margin: number
  llm_model: string
  kill_switch: boolean
  instruments: string[]
  notify_configured: boolean
  exchange_mode: string
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
  upl: number
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
  source: string
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
  volume_ratio?: number
  bollinger?: Record<string, number>
  candle_count?: number
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
