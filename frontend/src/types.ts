export interface EngineStatus {
  mode: string;
  running: boolean;
  emergency_locked: boolean;
  emergency_locked_until: string | null;
  selected_provider: string | null;
  backup_provider: string | null;
  active_cadences: string[];
  supported_cadences: string[];
  candidates_per_cycle: number | null;
  max_candidates_per_cycle: number;
  candidate_count: number;
  universe_refreshed_at: string | null;
  market_stream: {
    enabled: boolean;
    running: boolean;
    symbol_count: number;
    event_count: number;
    backfill_count: number;
    last_backfill_at: string | null;
    last_error: string | null;
  };
}

export interface ProviderHealth {
  provider: string;
  available: boolean;
  authenticated: boolean;
  executable: string | null;
  version: string | null;
  detail: string;
  model: string | null;
  reasoning_effort: string | null;
  reasoning_effort_options: string[];
  model_options: string[];
  capabilities: {
    subscription_auth: boolean;
    structured_output: boolean;
    tools_disabled: boolean;
    cancellable: boolean;
    max_concurrency: number;
  };
}

export interface Candidate {
  symbol: string;
  score: string;
  volume_rank: number;
  spread_bps: string;
  volatility: string;
  trend_strength: string;
}

export interface Signal {
  id: number;
  provider: string;
  model: string | null;
  intent: {
    symbol: string;
    cadence: string;
    action: string;
    confidence: number;
    leverage: number;
    rationale: string;
  };
  duration_ms: number;
  created_at: string;
}

export interface AccountPortfolio {
  mode: string;
  initial_equity: string;
  cash: string;
  equity: string;
  available_balance: string;
  daily_pnl: string;
  open_positions: number;
  margin_used: string;
}

export interface AccountPosition {
  symbol: string;
  side: string;
  quantity: string;
  average_price: string;
  mark_price: string;
  leverage: number;
  unrealized_pnl: string;
  notional: string;
  margin_used: string;
  stop_loss: string | null;
  take_profit: string | null;
}

export interface OrderRecord {
  id: number;
  client_order_id: string;
  symbol: string;
  status: string;
  report: {
    filled_quantity: string;
    average_price: string | null;
    message: string;
  };
  created_at: string;
}

export interface RiskEvent {
  id: number;
  inference_id: number | null;
  symbol: string;
  accepted: boolean;
  reason: string;
  decision: { max_quantity: string | null };
  created_at: string;
}

export interface ProviderMetric {
  provider: string;
  call_count: number;
  error_count: number;
  error_rate: number;
  average_duration_ms: number;
  p95_duration_ms: number;
  models: Record<string, number>;
  tokens_total: number;
  cost_usd_total: number | null;
  last_call_at: string;
}

export interface ProviderMetricsResponse {
  window_hours: number;
  providers: ProviderMetric[];
}

export interface TestnetAccountStatus {
  enabled: boolean;
  active: boolean;
  mode: string;
  account: null | {
    can_trade: boolean;
    total_wallet_balance: string;
    total_margin_balance: string;
    available_balance: string;
    total_unrealized_profit: string;
    total_initial_margin: string;
  };
  positions: Array<{
    symbol: string;
    position_amount: string;
    entry_price: string;
    mark_price: string;
    unrealized_profit: string;
    leverage: number;
    isolated: boolean;
  }>;
  reconciliation: null | {
    position_symbols: string[];
    open_order_count: number;
    unprotected_symbols: string[];
  };
  user_stream: {
    enabled: boolean;
    running: boolean;
    event_count: number;
    last_event_at: string | null;
    reconnect_count: number;
    dropped_event_count: number;
    last_error: string | null;
  };
  fetched_at: string | null;
}

export interface BacktestRun {
  id: number;
  symbol: string;
  cadence: string;
  result: {
    initial_equity: string;
    final_equity: string;
    total_return: string;
    max_drawdown: string;
    win_rate: string;
    sharpe_ratio: string | null;
    sortino_ratio: string | null;
    payoff_ratio: string | null;
    turnover: string;
    exposure_fraction: string;
    grouped_stats: Record<string, Record<string, {
      trade_count: number;
      win_rate: string;
      net_pnl: string;
      average_net_pnl: string;
      profit_factor: string | null;
    }>>;
    profit_factor: string | null;
    total_fees: string;
    total_funding: string;
    trade_count?: number;
    trades?: Array<{
      side: string;
      quantity: string;
      entry_time: string;
      entry_price: string;
      exit_time: string;
      exit_price: string;
      net_pnl: string;
      fees: string;
      funding: string;
      exit_reason: string;
    }>;
    equity_curve?: Array<{ timestamp: string; equity: string }>;
    replay?: {
      source: string;
      decision_count: number;
      start: string;
      end: string;
    };
  };
  created_at: string;
}
