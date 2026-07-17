export interface CustomProvider {
  id: string;
  base_url: string;
  model: string;
  reasoning_effort: string;
  wire_api: string;
  pricing: string;
  require_api_key: boolean;
  extra_header_names: string[];
  api_key_configured: boolean;
  api_key_masked: string;
}

export interface CustomProvidersPayload {
  providers: CustomProvider[];
  max_providers: number;
  wire_apis: string[];
  pricing_options: string[];
}

export interface SettingsField {
  key: string;
  label: string;
  kind: "text" | "int" | "number" | "bool" | "enum" | "json" | "secret";
  options: string[];
  placeholder: string;
  description: string;
  secret: boolean;
  configured: boolean;
  value: string | null;
  masked: string | null;
}

export interface SettingsPayload {
  path: string;
  sections: Array<{ title: string; fields: SettingsField[] }>;
}

export interface EngineStatus {
  running: boolean;
  emergency_locked: boolean;
  emergency_locked_until: string | null;
  selected_provider: string | null;
  backup_provider: string | null;
  provider_chain: string[];
  active_provider: string | null;
  provider_routes: ProviderRouteStatus[];
  active_cadences: string[];
  supported_cadences: string[];
  run_limits: { max_run_seconds: number | null; max_run_cost_usd: number | null };
  auto_stop_reason: string | null;
  route_failure_count: number;
  route_failure_limit: number;
  candidates_per_cycle: number | null;
  max_candidates_per_cycle: number;
  candidate_count: number;
  universe_refreshed_at: string | null;
  user_stream: {
    enabled: boolean;
    running: boolean;
    event_count: number;
    last_event_at: string | null;
    reconnect_count: number;
    dropped_event_count: number;
    last_error: string | null;
  };
}

export interface ProviderRouteStatus {
  provider: string;
  priority: number;
  state: "active" | "cooldown" | "standby";
  consecutive_failures: number;
  cooldown_until: string | null;
  last_error: string | null;
  last_failed_at: string | null;
  last_success_at: string | null;
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
  pricing: string | null;
  pricing_options: string[];
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

export interface DecisionEvent {
  id: number;
  provider: string;
  model: string | null;
  provenance: {
    reasoning_effort?: string | null;
    [key: string]: unknown;
  };
  failover: null | {
    route_position: number;
    continues: boolean;
    error: string | null;
  };
  intent: {
    symbol: string;
    cadence: string;
    action: string;
    confidence: number;
    leverage: number;
    risk_fraction: string;
    order_type: string;
    entry_price: string | null;
    stop_loss: string | null;
    take_profit: string | null;
    rationale: string;
  };
  duration_ms: number;
  outcome: "hold" | "approved" | "rejected" | "analysis_only" | "executed" | "execution_failed";
  risk: null | {
    id: number;
    accepted: boolean;
    reason: string;
    decision: { max_quantity: string | null };
    created_at: string;
  };
  execution: null | {
    id: number;
    inference_id: number;
    client_order_id: string | null;
    status: "SUCCEEDED" | "FAILED" | "RESCUED" | "UNKNOWN";
    stage: "ENTRY" | "PROTECTION" | "RESCUE" | "COMPLETE";
    message: string;
    exchange_error_code: number | null;
    estimated_loss_usdt: string | null;
    entry_report: null | {
      client_order_id: string;
      status: string;
      filled_quantity: string;
      average_price: string | null;
      message: string;
    };
    rescue_report: null | {
      client_order_id: string;
      status: string;
      filled_quantity: string;
      average_price: string | null;
      message: string;
    };
    created_at: string;
  };
  created_at: string;
}

export interface DecisionDetail extends DecisionEvent {
  audit_status: "complete" | "partial" | "unavailable";
  input: {
    market: Record<string, unknown>;
    portfolio: Record<string, unknown>;
  } | null;
  prompt: string | null;
  raw_output: string;
  usage: {
    input_tokens?: number;
    cached_input_tokens?: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
    [key: string]: unknown;
  };
  equivalent_cost_usd: number | null;
}

export interface AccountPortfolio {
  source: "binance-testnet";
  initial_equity: string | null;
  cash: string;
  equity: string;
  available_balance: string;
  daily_pnl: string | null;
  unrealized_pnl: string;
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
  protection_source?: "exchange" | "missing" | "unknown";
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

export interface RunSessionMetrics {
  state: "none" | "running" | "completed";
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number;
  call_count: number;
  error_count: number;
  input_tokens: number;
  cached_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  priced_call_count: number;
  cost_complete: boolean;
  equivalent_cost_usd: number | null;
  average_duration_ms: number;
  average_tokens: number;
  average_cost_usd: number | null;
}

export interface TestnetAccountStatus {
  enabled: boolean;
  active: boolean;
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

export interface BacktestEstimate {
  decisions_per_model: number;
  total_calls: number;
  estimated_seconds: number;
  estimated_hours: number;
  max_hours: number;
  within_limit: boolean;
}

export interface BacktestTrade {
  symbol: string;
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
}

export interface BacktestResult {
  initial_equity: string;
  final_equity: string;
  gross_price_pnl?: string;
  net_pnl?: string;
  total_return: string;
  max_drawdown: string;
  win_rate: string;
  profit_factor: string | null;
  trade_count: number;
  total_fees: string;
  total_funding: string;
  run_end_trade_count?: number;
  cancelled_pending_orders?: number;
  symbol_results?: Array<{
    symbol: string;
    gross_price_pnl: string;
    net_pnl: string;
    contribution_return: string;
    trade_count: number;
    total_fees: string;
    total_funding: string;
  }>;
  trades?: BacktestTrade[];
}

export interface BacktestModelRun {
  provider: string;
  model: string | null;
  reasoning_effort: string | null;
  config_recorded: boolean;
  decisions_done: number;
  decisions_total: number;
  calls_failed: number;
  usage: {
    call_count: number;
    priced_call_count: number;
    input_tokens: number;
    cached_input_tokens: number;
    cache_creation_input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    equivalent_cost_usd: number | null;
    duration_ms_total: number;
    average_duration_ms: number;
  };
  progress: number;
  error: string | null;
  result: BacktestResult | null;
}

export interface BacktestRun {
  id: number;
  status: "running" | "completed" | "unreliable" | "failed" | "cancelled";
  error: string | null;
  spec: {
    symbols: string[];
    cadences: string[];
    start: string;
    end: string;
    providers: string[];
    provider_configs?: Record<string, { model: string | null; reasoning_effort: string | null }>;
    use_recorded_book?: boolean;
    timeout_seconds?: number | null;
    timeout_source?: "explicit" | "provider_config";
    estimate: { decisions_per_model: number; total_calls: number; estimated_hours: number };
  };
  created_at: string;
  ended_at: string | null;
  models: BacktestModelRun[];
}

export interface CollectorStatus {
  running: boolean;
  symbols: string[];
  capture_count: number;
  error_count: number;
  last_capture_at: string | null;
  last_error: string | null;
  interval_seconds: number;
  max_symbols: number;
  recorded: Array<{
    symbol: string;
    capture_count: number;
    first_capture_at: string;
    last_capture_at: string;
  }>;
}

export interface ProbeCall {
  seconds: number;
  ok: boolean;
  error: string | null;
}

export interface ProviderProbe {
  provider: string;
  error: string | null;
  failures: number;
  done: boolean;
  in_flight_seconds: number | null;
  calls: ProbeCall[];
  slowest_ok_seconds: number | null;
  suggested_timeout_seconds: number | null;
}

export interface ProbeStatus {
  running: boolean;
  decisions: number;
  ceiling_seconds: number;
  providers: ProviderProbe[];
}

export interface BacktestDecision {
  id: number;
  provider: string;
  decided_at: string;
  symbol: string;
  cadence: string;
  outcome: "traded" | "pending" | "rejected" | "hold" | "no_snapshot" | "call_failed";
  action: string | null;
  confidence: number | null;
  rationale: string | null;
  detail: string | null;
  fill: null | {
    status: "NEW" | "FILLED";
    price: string;
    quantity: string;
    side: string;
    leverage: number;
    stop_loss: string | null;
    take_profit: string | null;
  };
}
