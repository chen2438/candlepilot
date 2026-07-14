export interface EngineStatus {
  mode: string;
  running: boolean;
  emergency_locked: boolean;
  emergency_locked_until: string | null;
  selected_provider: string | null;
  backup_provider: string | null;
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
    trades: unknown[];
    replay?: {
      source: string;
      decision_count: number;
      start: string;
      end: string;
    };
  };
  created_at: string;
}
