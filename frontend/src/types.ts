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

export interface StartupProbeProviderResult {
  status: "pending" | "completed";
  model?: string | null;
  reasoning_effort?: string | null;
  duration_seconds?: number;
  actions?: Record<string, number>;
  input_tokens?: number | null;
  cached_input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  equivalent_cost_usd?: number | null;
  intents?: Array<{ symbol: string; action: string; confidence: number }>;
}

export interface EngineStatus {
  running: boolean;
  emergency_locked: boolean;
  emergency_locked_until: string | null;
  provider_chain: string[];
  active_provider: string | null;
  live_run_id: number | null;
  provider_routes: ProviderRouteStatus[];
  active_cadences: string[];
  supported_cadences: string[];
  run_limits: { max_run_seconds: number | null; max_run_cost_usd: number | null };
  decision_timeout_seconds: number | null;
  startup_probe: {
    running: boolean;
    ready: boolean;
    consumed: boolean;
    timeout_seconds: number | null;
    provider_count: number;
    completed_providers: number;
    probe_symbols: string[];
    probe_cadence: string;
    provider_results: Record<string, StartupProbeProviderResult>;
    slowest_seconds?: number;
    analysis_symbol_count: number;
    projected_cycle_seconds?: number;
    aggregate_utilization?: number;
    max_safe_symbols?: number | null;
    started_at: string;
    checked_at?: string;
    error?: string;
    invalidated_reason?: string;
  } | null;
  auto_stop_reason: string | null;
  route_failure_count: number;
  route_failure_limit: number;
  rescue_count: number;
  rescue_limit: number;
  candidates_per_cycle: number | null;
  max_candidates_per_cycle: number;
  candidate_count: number;
  venue_excluded_symbols: string[];
  universe_refreshed_at: string | null;
  scheduler: {
    current_cycle: {
      cadence: string;
      started_at: string;
      symbol: string | null;
      symbol_started_at: string | null;
      stage: string;
      completed: number;
      total: number;
    } | null;
    current_cycles: Array<{
      cadence: string;
      started_at: string;
      symbol: string | null;
      symbol_started_at: string | null;
      stage: string;
      completed: number;
      total: number;
    }>;
    last_cycle: Record<string, unknown> | null;
    last_error: string | null;
    universe_last_error: string | null;
    guard_last_error: string | null;
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
  auth_source?: string | null;
  auth_source_options?: string[];
  account_email?: string | null;
  detail: string;
  model: string | null;
  reasoning_effort: string | null;
  timeout_seconds: number;
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
    external_inference: boolean;
    configurable_model: boolean;
    requires_backtest_probe: boolean;
    retryable: boolean;
    estimated_seconds_per_decision: number | null;
  };
}

export interface ProviderTestResult {
  ok: boolean;
  provider: string;
  model?: string | null;
  action?: string;
  duration_ms: number;
  detail?: string;
  usage?: {
    tokens_reported: boolean;
    input_tokens: number;
    cached_input_tokens: number;
    cache_creation_input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    equivalent_cost_usd: number | null;
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
  live_run_id: number | null;
  live_run: null | {
    id: number;
    status: "running" | "stopped" | "auto_stopped" | "emergency_stopped" | "interrupted";
    config: {
      provider_chain?: string[];
      cadences?: string[];
      candidates_per_cycle?: number;
      [key: string]: unknown;
    };
    stop_reason: string | null;
    started_at: string;
    ended_at: string | null;
  };
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
  decision_duration_ms?: number;
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

export interface LiveRunPerformance {
  live_run_id: number;
  total_pnl: string | null;
  realized_pnl: string;
  unrealized_pnl: string;
  wins: number;
  closed_trades: number;
  win_rate: string | null;
  includes_unrealized: boolean;
  valued_at: string | null;
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
  pnl_24h: string | null;
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

export interface ManualCloseResult {
  symbol: string;
  client_order_id: string;
  status: string;
  filled_quantity: string;
  average_price: string | null;
  timestamp: string;
}

export interface TradeFillRecord {
  id: number;
  source: "exchange_user_stream" | "exchange_rest_reconciliation" | "execution_audit";
  client_order_id: string;
  related_client_order_id: string | null;
  symbol: string;
  side: "BUY" | "SELL" | null;
  purpose: "entry" | "stop_loss" | "take_profit" | "manual_close" | "rescue_close" | "model_close" | "model_reduce" | "other_close";
  reduce_only: boolean;
  realized_pnl: string | null;
  notional_usdt: string | null;
  realized_pnl_margin_usdt: string | null;
  realized_return_percent: string | null;
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
  seconds_per_call: number;
  slowest_provider: string;
  latency_source: "probe_slowest_average" | "local_deterministic";
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
  gross_price_pnl: string;
  net_pnl: string;
  total_return: string;
  max_drawdown: string;
  win_rate: string;
  profit_factor: string | null;
  trade_count: number;
  total_fees: string;
  total_funding: string;
  run_end_trade_count: number;
  cancelled_pending_orders: number;
  symbol_results: Array<{
    symbol: string;
    gross_price_pnl: string;
    net_pnl: string;
    contribution_return: string;
    trade_count: number;
    total_fees: string;
    total_funding: string;
  }>;
  trades: BacktestTrade[];
}

export interface BacktestModelRun {
  provider: string;
  model: string | null;
  reasoning_effort: string | null;
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
  elapsed_seconds: number;
  remaining_seconds: number | null;
  live_result: {
    equity: string;
    unrealized_pnl: string;
    total_return: string;
    max_drawdown: string;
    win_rate: string;
    trade_count: number;
  } | null;
  error: string | null;
  result: BacktestResult | null;
}

export interface BacktestRun {
  id: number;
  status: "running" | "completed" | "failed" | "cancelled";
  error: string | null;
  spec: {
    symbols: string[];
    cadences: string[];
    start: string;
    end: string;
    requested_end?: string;
    providers: string[];
    provider_configs: Record<string, { model: string | null; reasoning_effort: string | null }>;
    use_recorded_book: boolean;
    timeout_seconds: number | null;
    timeout_source: "explicit" | "provider_config" | "not_applicable";
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
  average_ok_seconds: number | null;
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
  attempt_started_at: string[];
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
