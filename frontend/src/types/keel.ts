/** Keel API response types (Phase U1 — read-only control plane). */

export interface KeelHealth {
  status: string
  service: string
  version: string
  timestamp: number
  environment: string
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
}

export interface KeelConfig {
  environment: string
  max_positions: number
  max_daily_loss: number
  max_asset_margin: number
  llm_model: string
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
