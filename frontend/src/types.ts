export interface EngineStatus {
  mode: string;
  running: boolean;
  emergency_locked: boolean;
  selected_provider: string | null;
  candidate_count: number;
  universe_refreshed_at: string | null;
}

export interface ProviderHealth {
  provider: string;
  available: boolean;
  authenticated: boolean;
  executable: string | null;
  version: string | null;
  detail: string;
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

