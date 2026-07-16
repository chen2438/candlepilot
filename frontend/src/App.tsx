import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import type {
  AccountPortfolio,
  AccountPosition,
  BacktestRun,
  Candidate,
  DecisionEvent,
  DecisionDetail,
  EngineStatus,
  OrderRecord,
  ProviderHealth,
  ProviderMetric,
  ProviderMetricsResponse,
  RunSessionMetrics,
  TestnetAccountStatus,
} from "./types";

const emptyStatus: EngineStatus = {
  mode: "paper-production-data",
  running: false,
  emergency_locked: false,
  emergency_locked_until: null,
  selected_provider: null,
  backup_provider: null,
  provider_chain: [],
  active_provider: null,
  provider_routes: [],
  active_cadences: ["5m", "15m", "30m"],
  supported_cadences: ["5m", "15m", "30m"],
  candidates_per_cycle: 5,
  max_candidates_per_cycle: 20,
  candidate_count: 0,
  universe_refreshed_at: null,
  market_stream: {
    enabled: false,
    running: false,
    symbol_count: 0,
    event_count: 0,
    backfill_count: 0,
    last_backfill_at: null,
    last_error: null,
  },
};

const emptyRunSession: RunSessionMetrics = {
  state: "none",
  started_at: null,
  ended_at: null,
  duration_seconds: 0,
  call_count: 0,
  error_count: 0,
  input_tokens: 0,
  cached_input_tokens: 0,
  cache_creation_input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  priced_call_count: 0,
  cost_complete: true,
  equivalent_cost_usd: 0,
  average_duration_ms: 0,
  average_tokens: 0,
  average_cost_usd: 0,
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

const HISTORY_CATEGORIES: Array<{ key: string; label: string; hint: string }> = [
  { key: "inferences", label: "模型调用与决策", hint: "AI 分析 / 最近决策" },
  { key: "risk_decisions", label: "风控决策", hint: "风险事件" },
  { key: "executions", label: "订单与成交", hint: "" },
  { key: "backtests", label: "回测记录", hint: "" },
  { key: "user_events", label: "测试网事件", hint: "用户数据流" },
  { key: "alerts", label: "告警历史", hint: "" },
  { key: "market_cache", label: "行情缓存", hint: "Parquet" },
  { key: "pricing_cache", label: "定价缓存", hint: "models.dev" },
];

type TabKey = "overview" | "account" | "backtest" | "operations" | "data";

const TABS: Array<{ key: TabKey; label: string; meta: string }> = [
  { key: "overview", label: "总览", meta: "引擎 · 接入 · 候选 · 决策" },
  { key: "account", label: "账户", meta: "持仓 · 订单" },
  { key: "backtest", label: "回测", meta: "重放已审计决策" },
  { key: "operations", label: "运维", meta: "模型 · 测试网" },
  { key: "data", label: "数据", meta: "删除历史数据" },
];

const METRIC_DEFINITIONS: Record<string, string> = {
  "候选标的": "最近一次全市场扫描后进入动态候选池的 USDT 永续合约数量；候选池最多保留 20 个，不等于每个周期实际送入模型的数量。",
  "最大杠杆": "硬风控允许模型请求的最高杠杆倍数；实际交易可以更低，不能由模型突破。",
  "日亏熔断": "当日净亏损达到当日起始权益的 8% 时，硬风控拒绝新增风险仓位。",
  "权益": "账户现金或钱包余额加上按最新标记价计算的未实现盈亏。",
  "可用余额": "扣除当前保证金占用后，仍可用于新订单保证金的账户余额。",
  "占用保证金": "当前非零持仓占用的保证金合计；模拟账户按名义价值除以杠杆估算。",
  "持仓数": "当前数量非零的单向净仓标的数量。",
  "调用量": "过去 24 小时写入本地推理审计的该 Provider 调用记录数，包括失败并降级的记录。",
  "平均延迟": "过去 24 小时该 Provider 单次模型调用耗时的算术平均值。",
  "P95 延迟": "过去 24 小时调用耗时的第 95 百分位；约 95% 的调用不超过该值。",
  "错误率": "过去 24 小时带 Provider 错误标记的调用数除以调用总数。",
  "钱包余额": "币安测试网账户的钱包余额，不包含当前未实现盈亏。",
  "未实现盈亏": "全部未平仓头寸按最新标记价计算的浮动盈亏合计。",
  "总收益": "回测结束权益相对初始权益的累计变化比例，包含模型交易产生的费用和资金费影响。",
  "最大回撤": "回测权益曲线从任一历史峰值到后续低点的最大跌幅。",
  "Sharpe": "回测周期收益的年化平均值除以样本标准差，未扣无风险利率；值越高代表单位总波动收益越高。",
  "Sortino": "回测周期收益的年化平均值除以下行偏差；只惩罚负收益波动。",
  "换手": "回测全部成交名义价值合计除以初始权益。",
};

const RISK_DEFINITIONS: Record<string, string> = {
  "单笔风险": "单次开仓或加仓在止损触发时允许承担的计划亏损上限，为当前权益的 2%，并在定量时计入费用与保守滑点。",
  "并发仓位": "整个组合同时允许持有的非零净仓标的上限为 8 个。",
  "保证金占用": "全部仓位占用保证金不得超过账户权益的 60%。",
  "持仓模式": "每个标的使用逐仓保证金并维持单向净仓，不同时持有双向仓位。",
};

const CANDIDATE_DEFINITIONS = {
  score: "候选综合评分：24h 成交额 35% + 价差流动性 30% + 24h 波动 20% + 趋势绝对强度 15%，均在入选成交额池内归一化。",
  volumeRank: "通过上市时间、数据完整性和价差过滤后，按 24h USDT 成交额排序的名次；评分池最多取前 50 名。",
  spread: "最新卖一价与买一价之差除以中间价，以基点 bp 表示；1 bp = 0.01%。",
  volatility: "币安 24h 最高价与最低价之差除以最新价。",
  trend: "币安 24h 价格涨跌幅；正值表示上涨，负值表示下跌。",
};

function providerLabel(name: string): string {
  if (name === "codex-auth") return "Codex Auth";
  if (name === "claude-code-auth") return "Claude Code Auth";
  if (name === "openai-compatible") return "Custom API";
  return name;
}

function providerIcon(name: string): string {
  if (name === "codex-auth") return "CX";
  if (name === "claude-code-auth") return "CC";
  if (name === "openai-compatible") return "API";
  return "AI";
}

function providerConfigLabel(name: string): string {
  if (name === "codex-auth") return "Codex";
  if (name === "claude-code-auth") return "Claude";
  if (name === "openai-compatible") return "Custom API";
  return name;
}

function inferenceConfigLabel(decision: DecisionEvent): string {
  const model = decision.model ?? "默认模型";
  const provenance = decision.provenance;
  if (!Object.prototype.hasOwnProperty.call(provenance, "reasoning_effort")) {
    return `${model} · 推理强度未记录`;
  }
  return `${model} · ${provenance.reasoning_effort ? `推理 ${provenance.reasoning_effort}` : "默认推理强度"}`;
}

function percent(value: string): string {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function localDateTime(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function initialReplayForm() {
  const end = new Date();
  end.setSeconds(0, 0);
  const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
  return {
    symbol: "BTCUSDT",
    cadence: "5m",
    start: localDateTime(start),
    end: localDateTime(end),
    initialEquity: "10000",
    feeRate: "0.0005",
    slippage: "0.0005",
  };
}

export default function App() {
  const [tab, setTab] = useState<TabKey>("overview");
  const [status, setStatus] = useState<EngineStatus>(emptyStatus);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [decisions, setDecisions] = useState<DecisionEvent[]>([]);
  const [backtests, setBacktests] = useState<BacktestRun[]>([]);
  const [selectedBacktest, setSelectedBacktest] = useState<BacktestRun | null>(null);
  const [portfolio, setPortfolio] = useState<AccountPortfolio | null>(null);
  const [positions, setPositions] = useState<AccountPosition[]>([]);
  const [orders, setOrders] = useState<OrderRecord[]>([]);
  const [providerMetrics, setProviderMetrics] = useState<ProviderMetric[]>([]);
  const [runSession, setRunSession] = useState<RunSessionMetrics>(emptyRunSession);
  const [testnetStatus, setTestnetStatus] = useState<TestnetAccountStatus | null>(null);
  const [operationsError, setOperationsError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [socketOnline, setSocketOnline] = useState(false);
  const [replayForm, setReplayForm] = useState(initialReplayForm);
  const [configDraft, setConfigDraft] = useState<Record<string, { model: string; effort: string; custom: boolean }>>({});
  const [historySelected, setHistorySelected] = useState<Record<string, boolean>>({});
  const [historyConfirm, setHistoryConfirm] = useState(false);
  const [historyResult, setHistoryResult] = useState<string | null>(null);
  const [candidateDraft, setCandidateDraft] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; text: string }>>({});

  const applyProviderConfig = useCallback(async (name: string, draft: { model: string; effort: string }) => {
    setBusy("provider-config");
    setError(null);
    try {
      const next = await api<ProviderHealth[]>("/api/providers/config", {
        method: "POST",
        body: JSON.stringify({ name, model: draft.model, reasoning_effort: draft.effort || null }),
      });
      setProviders(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const testProvider = useCallback(async (name: string) => {
    setBusy(`test-${name}`);
    setError(null);
    setTestResult((current) => ({ ...current, [name]: { ok: false, text: "测试中…" } }));
    try {
      const result = await api<{ ok: boolean; model: string | null; action?: string; duration_ms: number; detail?: string }>(
        "/api/providers/test",
        { method: "POST", body: JSON.stringify({ name }) },
      );
      const seconds = (result.duration_ms / 1000).toFixed(1);
      const text = result.ok
        ? `✓ ${result.model ?? "默认模型"} · ${seconds}s · ${result.action}`
        : `✗ ${result.detail ?? "调用失败"}`;
      setTestResult((current) => ({ ...current, [name]: { ok: result.ok, text } }));
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : String(reason);
      setTestResult((current) => ({ ...current, [name]: { ok: false, text: `✗ ${detail}` } }));
    } finally {
      setBusy(null);
    }
  }, []);

  const changeProviderChain = useCallback(async (chain: string[]) => {
    if (!chain.length) return;
    setBusy("provider-route");
    setError(null);
    try {
      setStatus(await api<EngineStatus>("/api/providers/select", {
        method: "POST",
        body: JSON.stringify({ providers: chain }),
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const toggleProviderRoute = useCallback((name: string) => {
    const chain = status.provider_chain;
    const next = chain.includes(name)
      ? chain.filter((provider) => provider !== name)
      : [...chain, name];
    if (next.length) void changeProviderChain(next);
  }, [status.provider_chain, changeProviderChain]);

  const moveProviderRoute = useCallback((index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= status.provider_chain.length) return;
    const next = [...status.provider_chain];
    [next[index], next[target]] = [next[target], next[index]];
    void changeProviderChain(next);
  }, [status.provider_chain, changeProviderChain]);

  const toggleCadence = useCallback(async (cadence: string, active: string[], supported: string[]) => {
    const next = active.includes(cadence) ? active.filter((c) => c !== cadence) : [...active, cadence];
    if (!next.length) return; // keep at least one cadence
    const ordered = supported.filter((c) => next.includes(c));
    setBusy("cadences");
    setError(null);
    try {
      setStatus(await api<EngineStatus>("/api/cadences", { method: "POST", body: JSON.stringify({ cadences: ordered }) }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const changeCandidatesPerCycle = useCallback(async (value: number, max: number) => {
    const clamped = Math.max(1, Math.min(max, Math.round(value)));
    setBusy("candidates-per-cycle");
    setError(null);
    try {
      setStatus(
        await api<EngineStatus>("/api/candidates-per-cycle", {
          method: "POST",
          body: JSON.stringify({ candidates_per_cycle: clamped }),
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  // The slider and the number box share candidateDraft (a string so the box can
  // be cleared and retyped). Editing only updates the draft locally; the release
  // (slider pointer/key up, or box blur/Enter) commits one clamped request, so
  // sweeping or typing does not spam the API.
  const commitCandidates = useCallback(() => {
    setCandidateDraft((draft) => {
      if (draft === null || draft.trim() === "") return null; // nothing valid; revert
      const parsed = Math.round(Number(draft));
      if (!Number.isFinite(parsed)) return null;
      const clamped = Math.max(1, Math.min(status.max_candidates_per_cycle, parsed));
      if (clamped !== status.candidates_per_cycle) {
        changeCandidatesPerCycle(clamped, status.max_candidates_per_cycle);
      }
      return String(clamped);
    });
  }, [status.candidates_per_cycle, status.max_candidates_per_cycle, changeCandidatesPerCycle]);

  // Clear the draft once the server confirms it, avoiding a value flicker.
  useEffect(() => {
    if (candidateDraft !== null && Number(candidateDraft) === status.candidates_per_cycle) {
      setCandidateDraft(null);
    }
  }, [candidateDraft, status.candidates_per_cycle]);

  const refresh = useCallback(async () => {
    const [nextStatus, nextProviders, nextCandidates, nextDecisions, nextBacktests] = await Promise.all([
      api<EngineStatus>("/api/status"),
      api<ProviderHealth[]>("/api/providers"),
      api<Candidate[]>("/api/universe"),
      api<DecisionEvent[]>("/api/decision-events?limit=50"),
      api<BacktestRun[]>("/api/backtests?limit=10"),
    ]);
    setStatus(nextStatus);
    setProviders(nextProviders);
    setCandidates(nextCandidates);
    setDecisions(nextDecisions);
    setBacktests(nextBacktests);
  }, []);

  const refreshAccount = useCallback(async () => {
    const [nextPortfolio, nextPositions, nextOrders] = await Promise.all([
      api<AccountPortfolio>("/api/account/portfolio"),
      api<AccountPosition[]>("/api/account/positions"),
      api<OrderRecord[]>("/api/orders?limit=25"),
    ]);
    setPortfolio(nextPortfolio);
    setPositions(nextPositions);
    setOrders(nextOrders);
  }, []);

  const refreshDecisions = useCallback(async () => {
    setDecisions(await api<DecisionEvent[]>("/api/decision-events?limit=50"));
  }, []);

  const refreshOperations = useCallback(async () => {
    const [metrics, testnet] = await Promise.allSettled([
      api<ProviderMetricsResponse>("/api/metrics/providers?hours=24"),
      api<TestnetAccountStatus>("/api/testnet/account-status"),
    ]);
    if (metrics.status === "fulfilled") setProviderMetrics(metrics.value.providers);
    if (testnet.status === "fulfilled") setTestnetStatus(testnet.value);
    const failures = [metrics, testnet]
      .filter((result): result is PromiseRejectedResult => result.status === "rejected")
      .map((result) => result.reason instanceof Error ? result.reason.message : String(result.reason));
    setOperationsError(failures.length ? failures.join("；") : null);
  }, []);

  const refreshRunSession = useCallback(async () => {
    setRunSession(await api<RunSessionMetrics>("/api/metrics/run-session"));
  }, []);

  useEffect(() => {
    refresh().catch((reason: Error) => setError(reason.message));
    refreshAccount().catch((reason: Error) => setError(reason.message));
    refreshOperations().catch(() => undefined);
    refreshRunSession().catch(() => undefined);
    const account = window.setInterval(() => {
      refreshAccount().catch(() => undefined);
      refreshOperations().catch(() => undefined);
      api<EngineStatus>("/api/status").then(setStatus).catch(() => undefined);
    }, 5000);
    const decisionFallback = window.setInterval(() => {
      refreshDecisions().catch(() => undefined);
    }, 15000);
    const runUsage = window.setInterval(() => {
      refreshRunSession().catch(() => undefined);
    }, 2000);
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let disposed = false;
    const connect = () => {
      socket = new WebSocket(`${protocol}://${window.location.host}/ws/events`);
      socket.onopen = () => setSocketOnline(true);
      socket.onclose = () => {
        setSocketOnline(false);
        if (!disposed) reconnectTimer = window.setTimeout(connect, 2000);
      };
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data) as
          | { type: "status"; data: EngineStatus }
          | { type: "decisions"; data: DecisionEvent[] };
        if (message.type === "status") setStatus(message.data);
        if (message.type === "decisions") setDecisions(message.data);
      };
    };
    connect();
    return () => {
      disposed = true;
      window.clearInterval(account);
      window.clearInterval(decisionFallback);
      window.clearInterval(runUsage);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [refresh, refreshAccount, refreshDecisions, refreshOperations, refreshRunSession]);

  const act = useCallback(async (name: string, path: string, body?: unknown) => {
    setBusy(name);
    setError(null);
    try {
      const next = await api<EngineStatus>(path, {
        method: "POST",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      setStatus(next);
      if (path.startsWith("/api/engine/")) await refreshRunSession();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [refreshRunSession]);

  const refreshUniverse = useCallback(async () => {
    setBusy("universe");
    setError(null);
    try {
      const next = await api<Candidate[]>("/api/universe/refresh", { method: "POST" });
      setCandidates(next);
      setStatus(await api<EngineStatus>("/api/status"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const runCachedReplay = useCallback(async (event: FormEvent) => {
    event.preventDefault();
    setBusy("backtest");
    setError(null);
    try {
      const start = new Date(replayForm.start);
      const end = new Date(replayForm.end);
      if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || end <= start) {
        throw new Error("回测结束时间必须晚于开始时间");
      }
      const created = await api<BacktestRun>("/api/backtests/replay", {
        method: "POST",
        body: JSON.stringify({
          symbol: replayForm.symbol.trim().toUpperCase(),
          cadence: replayForm.cadence,
          start: start.toISOString(),
          end: end.toISOString(),
          config: {
            initial_equity: replayForm.initialEquity,
            fee_rate: replayForm.feeRate,
            slippage_fraction: replayForm.slippage,
          },
        }),
      });
      setBacktests((current) => [created, ...current.filter((run) => run.id !== created.id)]);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message.includes("no cached LLM decisions") ? "该区间没有可重放的已审计 LLM 决策" : message);
    } finally {
      setBusy(null);
    }
  }, [replayForm]);

  const clearHistory = useCallback(async () => {
    const categories = Object.entries(historySelected).filter(([, on]) => on).map(([key]) => key);
    if (!categories.length) return;
    setBusy("history-clear");
    setError(null);
    try {
      const res = await api<{ cleared: Record<string, number> }>("/api/history/clear", {
        method: "POST",
        body: JSON.stringify({ categories }),
      });
      setHistoryResult(Object.entries(res.cleared).map(([key, count]) => `${key}: ${count}`).join(" · "));
      setHistorySelected({});
      setHistoryConfirm(false);
      await refresh();
      await refreshAccount();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [historySelected, refresh, refreshAccount]);

  const openBacktest = useCallback(async (id: number) => {
    setBusy("backtest-detail");
    setError(null);
    try {
      setSelectedBacktest(await api<BacktestRun>(`/api/backtests/${id}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const activeProvider = useMemo(
    () => providers.find((provider) => provider.provider === status.active_provider),
    [providers, status.active_provider],
  );
  const allHistorySelected = HISTORY_CATEGORIES.every(
    (category) => historySelected[category.key],
  );

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><i /><i /><i /></span>
          <div><strong>CANDLEPILOT</strong><small>PERPETUAL INTELLIGENCE</small></div>
        </div>
        <div className="environment">
          <span className="eyebrow">环境</span>
          <strong>{status.mode === "paper-production-data" ? "生产行情 · 模拟成交" : status.mode}</strong>
          <small>{status.market_stream.running ? `币安实时 · ${status.market_stream.symbol_count} 标的 · ${status.market_stream.event_count} 事件 · ${status.market_stream.backfill_count} 回补` : status.market_stream.enabled ? "币安实时流待启动" : "REST 行情"}</small>
        </div>
        <div className="live-state">
          <span className={`dot ${socketOnline ? "online" : ""}`} />
          {socketOnline ? status.market_stream.running ? "BINANCE MARKET LIVE" : "LOCAL STREAM ONLINE" : "STREAM OFFLINE"}
        </div>
      </header>

      <nav className="tabnav">
        <div className="tabnav-inner">
          {TABS.map((item) => (
            <button
              key={item.key}
              className={tab === item.key ? "active" : ""}
              onClick={() => setTab(item.key)}
            >
              <strong>{item.label}</strong>
              <small>{item.meta}</small>
            </button>
          ))}
        </div>
      </nav>

      <main>
        {error && <div className="error-banner"><b>操作失败</b><span>{error}</span><button onClick={() => setError(null)}>×</button></div>}
        {status.emergency_locked && <div className="lock-banner">紧急锁定已生效{status.emergency_locked_until ? `，自动解锁时间：${new Date(status.emergency_locked_until).toLocaleString("zh-CN", { hour12: false })}` : ""}。检查账户状态后也可手动解除。</div>}

        {tab === "overview" && (<>
        <section className="hero panel">
          <div>
            <p className="eyebrow">AUTONOMOUS DESK / 本地控制台</p>
            <h1>系统{status.running ? "运行中" : "已停机"}</h1>
            <p className="hero-copy">
              LLM 负责提出交易意图，确定性风控拥有最终否决权。当前不支持真钱实盘。
            </p>
          </div>
          <div className="hero-metrics">
            <Metric label="候选标的" value={String(status.candidate_count)} suffix="/ 20" />
            <Metric label="最大杠杆" value="10" suffix="×" />
            <Metric label="日亏熔断" value="8.0" suffix="%" />
          </div>
          <div className="controls">
            <div className="cadence-select" title={status.running ? "运行时锁定" : "选择要分析的决策周期"}>
              <span>分析周期</span>
              <div className="cadence-chips">
                {status.supported_cadences.map((cadence) => (
                  <button
                    key={cadence}
                    className={`cadence-chip ${status.active_cadences.includes(cadence) ? "on" : ""}`}
                    disabled={busy !== null || status.running}
                    onClick={() => toggleCadence(cadence, status.active_cadences, status.supported_cadences)}
                  >{cadence}</button>
                ))}
              </div>
            </div>
            <div className="cadence-select" title={status.running ? "运行时锁定" : "拖动滑条或直接输入每个周期分析候选池前 N 个标的"}>
              <span className="range-head">
                每周期标的数
                <input
                  type="number"
                  className="range-input"
                  min={1}
                  max={status.max_candidates_per_cycle}
                  step={1}
                  value={candidateDraft ?? String(status.candidates_per_cycle ?? 5)}
                  disabled={busy !== null || status.running}
                  onFocus={(event) => event.target.select()}
                  onChange={(event) => setCandidateDraft(event.target.value)}
                  onBlur={commitCandidates}
                  onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }}
                />
              </span>
              <div className="range-row">
                <input
                  type="range"
                  className="range"
                  min={1}
                  max={status.max_candidates_per_cycle}
                  step={1}
                  value={Number(candidateDraft ?? status.candidates_per_cycle ?? 5) || 1}
                  disabled={busy !== null || status.running}
                  onChange={(event) => setCandidateDraft(event.target.value)}
                  onPointerUp={commitCandidates}
                  onKeyUp={commitCandidates}
                />
                <div className="range-scale"><span>1</span><span>{status.max_candidates_per_cycle}</span></div>
              </div>
            </div>
            <button
              className="primary"
              disabled={busy !== null || status.running || status.emergency_locked}
              onClick={() => act("start", "/api/engine/start")}
            >{busy === "start" ? "启动中…" : "启动引擎"}</button>
            <button disabled={busy !== null || !status.running} onClick={() => act("stop", "/api/engine/stop")}>优雅停止</button>
            <button className="danger" disabled={busy !== null} onClick={() => act("kill", "/api/engine/emergency-stop")}>紧急熔断</button>
          </div>
        </section>

        <RunUsage session={runSession} />

        <section className="grid">
          <article className="panel provider-panel">
            <PanelTitle code="01" title="模型接入" meta="手动路由" />
            <div className="provider-list">
              {providers.map((provider) => {
                const routeIndex = status.provider_chain.indexOf(provider.provider);
                return <button
                  key={provider.provider}
                  className={`provider-card ${routeIndex >= 0 ? "selected" : ""}`}
                  disabled={status.running || busy !== null || (routeIndex === 0 && status.provider_chain.length === 1)}
                  onClick={() => toggleProviderRoute(provider.provider)}
                  title={routeIndex >= 0 ? "点击从路由中移除" : "点击加入路由末尾；当前不可用也可预先配置"}
                >
                  <span className={`provider-icon ${provider.authenticated ? "ready" : ""}`}>
                    {providerIcon(provider.provider)}
                  </span>
                  <span className="provider-text">
                    <strong>{providerLabel(provider.provider)}</strong>
                    <small>{provider.version ?? provider.detail}</small>
                  </span>
                  <span className={`status-pill ${provider.authenticated ? "ok" : "off"}`}>
                    {routeIndex >= 0 ? `#${routeIndex + 1} · ` : ""}{provider.authenticated ? "READY" : provider.available ? "LOGIN" : "MISSING"}
                  </span>
                </button>
              })}
            </div>
            <div className="provider-route">
              <div className="provider-config-title"><span>主备顺序</span><small>{status.running ? "运行时锁定" : "失败后冷却 60 秒并自动恢复"}</small></div>
              {status.provider_chain.map((name, index) => {
                const route = status.provider_routes.find((item) => item.provider === name);
                const health = providers.find((item) => item.provider === name);
                const state = route?.state === "active" ? "承载中" : route?.state === "cooldown" ? "冷却" : health?.authenticated ? "待命" : "不可用";
                return <div className={`provider-route-row ${route?.state ?? "standby"}`} key={name}>
                  <strong>{index + 1}</strong>
                  <span>{providerLabel(name)}<small>{state}{route?.last_error ? ` · ${route.last_error}` : ""}</small></span>
                  <button disabled={status.running || busy !== null || index === 0} onClick={() => moveProviderRoute(index, -1)} title="提高优先级">↑</button>
                  <button disabled={status.running || busy !== null || index === status.provider_chain.length - 1} onClick={() => moveProviderRoute(index, 1)} title="降低优先级">↓</button>
                  <button disabled={status.running || busy !== null || status.provider_chain.length === 1} onClick={() => toggleProviderRoute(name)} title="移出路由">×</button>
                </div>;
              })}
            </div>
            <div className="provider-foot">
              <span>实际承载</span><strong>{activeProvider ? providerLabel(activeProvider.provider) : status.running ? "等待可用 Provider" : "引擎未运行"}</strong>
            </div>
            <div className="provider-config">
              <div className="provider-config-title"><span>模型与推理强度</span><small>{status.running ? "运行时锁定" : "留空=Provider 默认"}</small></div>
              {providers.map((provider) => {
                const options = provider.model_options ?? [];
                const model = configDraft[provider.provider]?.model ?? provider.model ?? "";
                const effort = configDraft[provider.provider]?.effort ?? provider.reasoning_effort ?? "";
                const custom = configDraft[provider.provider]?.custom ?? (model !== "" && !options.includes(model));
                const draft = { model, effort, custom };
                const dirty = model !== (provider.model ?? "") || effort !== (provider.reasoning_effort ?? "");
                const update = (next: Partial<typeof draft>) =>
                  setConfigDraft((current) => ({ ...current, [provider.provider]: { ...draft, ...next } }));
                return (
                  <div className="provider-config-row" key={provider.provider}>
                    <span className="config-name">{providerConfigLabel(provider.provider)}</span>
                    <div className="config-model-cell">
                      <select
                        value={custom ? "__custom__" : model}
                        disabled={status.running}
                        onChange={(event) => event.target.value === "__custom__" ? update({ custom: true }) : update({ model: event.target.value, custom: false })}
                      >
                        <option value="">默认模型</option>
                        {options.map((option) => (
                          <option key={option} value={option}>{option}</option>
                        ))}
                        <option value="__custom__">自定义…</option>
                      </select>
                      {custom && (
                        <input
                          className="config-model-custom"
                          placeholder="输入模型名"
                          value={model}
                          disabled={status.running}
                          onChange={(event) => update({ model: event.target.value })}
                        />
                      )}
                    </div>
                    <select
                      value={effort}
                      disabled={status.running}
                      onChange={(event) => update({ effort: event.target.value })}
                    >
                      <option value="">默认强度</option>
                      {provider.reasoning_effort_options.map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                    <button
                      className="text-button"
                      disabled={status.running || busy !== null || !dirty}
                      onClick={() => applyProviderConfig(provider.provider, { model, effort })}
                    >应用</button>
                    <button
                      className="text-button"
                      disabled={status.running || busy !== null || dirty || !provider.authenticated}
                      title={dirty ? "请先应用配置再测试" : "用当前配置发起一次真实调用"}
                      onClick={() => testProvider(provider.provider)}
                    >{busy === `test-${provider.provider}` ? "测试中…" : "测试"}</button>
                    {testResult[provider.provider] && (
                      <span className={`config-test-result ${testResult[provider.provider].ok ? "ok" : "err"}`}>
                        {testResult[provider.provider].text}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </article>

          <article className="panel risk-panel">
            <PanelTitle code="02" title="硬风控边界" meta="不可由模型修改" />
            <div className="risk-grid">
              <RiskItem label="单笔风险" value="2.0%" detail="权益上限" />
              <RiskItem label="并发仓位" value="8" detail="全组合" />
              <RiskItem label="保证金占用" value="60%" detail="组合上限" />
              <RiskItem label="持仓模式" value="逐仓" detail="单向净仓" />
            </div>
            <div className="risk-line"><span style={{ width: "60%" }} /></div>
            <p>所有开仓必须包含交易所侧止损，并通过精度、陈旧行情和强平缓冲检查。</p>
          </article>

          <article className="panel universe-panel">
            <PanelTitle code="03" title="动态候选池" meta="USDT 永续" />
            <button className="compact" disabled={busy !== null} onClick={refreshUniverse}>{busy === "universe" ? "扫描中…" : "刷新全市场"}</button>
            <div className="table-wrap">
              <table>
                <thead><tr><th>标的</th><th data-tooltip={CANDIDATE_DEFINITIONS.score}>评分</th><th data-tooltip={CANDIDATE_DEFINITIONS.volumeRank}>成交额排名</th><th data-tooltip={CANDIDATE_DEFINITIONS.spread}>价差</th><th data-tooltip={CANDIDATE_DEFINITIONS.volatility}>24h 波动</th><th data-tooltip={CANDIDATE_DEFINITIONS.trend}>趋势</th></tr></thead>
                <tbody>
                  {candidates.map((candidate) => (
                    <tr key={candidate.symbol}>
                      <td><strong>{candidate.symbol.replace("USDT", "")}</strong><small>/USDT PERP</small></td>
                      <td className="accent">{Number(candidate.score).toFixed(3)}</td>
                      <td>#{candidate.volume_rank}</td>
                      <td>{Number(candidate.spread_bps).toFixed(2)} bp</td>
                      <td>{percent(candidate.volatility)}</td>
                      <td className={Number(candidate.trend_strength) >= 0 ? "positive" : "negative"}>{percent(candidate.trend_strength)}</td>
                    </tr>
                  ))}
                  {!candidates.length && <tr><td colSpan={6} className="empty">尚未扫描市场</td></tr>}
                </tbody>
              </table>
            </div>
          </article>

          <DecisionPanel decisions={decisions} />
        </section>
        </>)}

        {tab === "backtest" && (
        <section className="grid">
          <article className="panel backtest-panel">
            <PanelTitle code="05" title="回测运行" meta="事件驱动 · 下一根 K 线成交" />
            <form className="backtest-form" onSubmit={runCachedReplay}>
              <label><span>标的</span><input required pattern="[A-Za-z0-9]+USDT" value={replayForm.symbol} onChange={(event) => setReplayForm({ ...replayForm, symbol: event.target.value })} /></label>
              <label><span>周期</span><select value={replayForm.cadence} onChange={(event) => setReplayForm({ ...replayForm, cadence: event.target.value })}><option value="1m">1m</option><option value="5m">5m</option><option value="15m">15m</option><option value="30m">30m</option></select></label>
              <label><span>开始时间</span><input required type="datetime-local" value={replayForm.start} onChange={(event) => setReplayForm({ ...replayForm, start: event.target.value })} /></label>
              <label><span>结束时间</span><input required type="datetime-local" value={replayForm.end} onChange={(event) => setReplayForm({ ...replayForm, end: event.target.value })} /></label>
              <label><span>初始权益</span><input required min="1" step="any" type="number" value={replayForm.initialEquity} onChange={(event) => setReplayForm({ ...replayForm, initialEquity: event.target.value })} /></label>
              <label><span>手续费率</span><input required min="0" max="1" step="any" type="number" value={replayForm.feeRate} onChange={(event) => setReplayForm({ ...replayForm, feeRate: event.target.value })} /></label>
              <label><span>滑点比例</span><input required min="0" max="1" step="any" type="number" value={replayForm.slippage} onChange={(event) => setReplayForm({ ...replayForm, slippage: event.target.value })} /></label>
              <button className="compact" disabled={busy !== null} type="submit">{busy === "backtest" ? "重放中…" : "重放缓存决策"}</button>
              <small>仅使用已审计的 LLM 决策，不会在回测时产生新的模型调用。</small>
            </form>
            <div className="table-wrap">
              <table>
                <thead><tr><th>运行</th><th>标的</th><th>周期</th><th>总收益</th><th>最大回撤</th><th>胜率</th><th>交易数</th><th>时间</th><th /></tr></thead>
                <tbody>
                  {backtests.map((run) => (
                    <tr key={run.id}>
                      <td>#{run.id}{run.result.replay && <small>{run.result.replay.decision_count} 决策</small>}</td>
                      <td><strong>{run.symbol}</strong></td>
                      <td>{run.cadence}</td>
                      <td className={Number(run.result.total_return) >= 0 ? "positive" : "negative"}>{percent(run.result.total_return)}</td>
                      <td className="negative">{percent(run.result.max_drawdown)}</td>
                      <td>{percent(run.result.win_rate)}</td>
                      <td>{run.result.trade_count ?? run.result.trades?.length ?? 0}</td>
                      <td>{new Date(run.created_at).toLocaleString("zh-CN", { hour12: false })}</td>
                      <td><button className="text-button" disabled={busy !== null} onClick={() => openBacktest(run.id)}>详情</button></td>
                    </tr>
                  ))}
                  {!backtests.length && <tr><td colSpan={9} className="empty">尚无回测运行。请使用上方表单重放已审计的历史决策。</td></tr>}
                </tbody>
              </table>
            </div>
            {selectedBacktest && (
              <BacktestDetail run={selectedBacktest} onClose={() => setSelectedBacktest(null)} />
            )}
          </article>
        </section>
        )}

        {tab === "account" && (
        <section className="grid">
          <AccountPanel
            portfolio={portfolio}
            positions={positions}
            orders={orders}
          />
        </section>
        )}

        {tab === "operations" && (
        <section className="grid">
          <OperationsPanel
            providerMetrics={providerMetrics}
            testnetStatus={testnetStatus}
            operationsError={operationsError}
          />
        </section>
        )}

        {tab === "data" && (
        <section className="grid">
          <article className="panel history-panel">
            <PanelTitle code="08" title="数据管理" meta="删除历史数据 · 不可恢复" />
            <div className="history-grid">
              {HISTORY_CATEGORIES.map((category) => (
                <label className="history-item" key={category.key}>
                  <input
                    type="checkbox"
                    checked={!!historySelected[category.key]}
                    onChange={(event) => { setHistoryConfirm(false); setHistorySelected((current) => ({ ...current, [category.key]: event.target.checked })); }}
                  />
                  <span><strong>{category.label}</strong>{category.hint && <small>{category.hint}</small>}</span>
                </label>
              ))}
            </div>
            <div className="history-actions">
              <button
                className="history-select-all"
                disabled={busy !== null}
                onClick={() => {
                  setHistoryConfirm(false);
                  setHistorySelected(
                    allHistorySelected
                      ? {}
                      : Object.fromEntries(
                          HISTORY_CATEGORIES.map((category) => [category.key, true]),
                        ),
                  );
                }}
              >{allHistorySelected ? "取消全选" : "全选"}</button>
              {!historyConfirm ? (
                <button
                  className="danger"
                  disabled={busy !== null || !Object.values(historySelected).some(Boolean)}
                  onClick={() => setHistoryConfirm(true)}
                >清除所选</button>
              ) : (
                <>
                  <span className="history-warn">确认删除所选数据？此操作不可恢复。</span>
                  <button className="danger" disabled={busy !== null} onClick={clearHistory}>{busy === "history-clear" ? "删除中…" : "确认删除"}</button>
                  <button className="text-button" disabled={busy !== null} onClick={() => setHistoryConfirm(false)}>取消</button>
                </>
              )}
              {historyResult && <span className="history-result">已删除 → {historyResult}</span>}
            </div>
          </article>
        </section>
        )}
      </main>
      <footer><span>CANDLEPILOT / GPL-3.0</span><span>LOCALHOST ONLY · NO LIVE MONEY</span></footer>
    </div>
  );
}

function Metric({ label, value, suffix }: { label: string; value: string; suffix: string }) {
  return <div className="metric" data-tooltip={METRIC_DEFINITIONS[label]}><span>{label}</span><strong>{value}<small>{suffix}</small></strong></div>;
}

function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours) return `${hours}h ${minutes}m ${rest}s`;
  if (minutes) return `${minutes}m ${rest}s`;
  return `${rest}s`;
}

function RunUsage({ session }: { session: RunSessionMetrics }) {
  const active = session.state === "running";
  const title = active ? "本次运行用量" : session.state === "completed" ? "上次运行用量" : "运行用量";
  const cost = session.equivalent_cost_usd === null
    ? "—"
    : `$${session.equivalent_cost_usd.toFixed(6)}`;
  const averageCost = session.average_cost_usd === null
    ? "—"
    : `$${session.average_cost_usd.toFixed(6)}`;
  return (
    <section className={`run-usage panel ${active ? "active" : ""}`}>
      <div className="run-usage-heading">
        <div>
          <span className={`run-state ${active ? "live" : ""}`}>
            {active ? "LIVE" : session.state === "completed" ? "STOPPED" : "IDLE"}
          </span>
          <strong>{title}</strong>
        </div>
        <small>
          {session.state === "none"
            ? "启动引擎后开始统计"
            : `${formatDuration(session.duration_seconds)} · ${session.call_count} 次调用${session.error_count ? ` · ${session.error_count} 次错误` : ""}`}
        </small>
      </div>
      <div className="run-usage-metrics">
        <span data-tooltip="本次或上次引擎运行中，Provider 报告的非缓存输入 Token 合计。">输入 Token<strong>{session.input_tokens.toLocaleString()}</strong></span>
        <span data-tooltip="本次或上次运行中从 Provider 提示词缓存读取并复用的输入 Token 合计。">缓存输入<strong>{session.cached_input_tokens.toLocaleString()}</strong></span>
        <span data-tooltip="本次或上次运行中新写入 Provider 提示词缓存的输入 Token 合计；并非所有 Provider 都报告此项。">缓存写入<strong>{session.cache_creation_input_tokens.toLocaleString()}</strong></span>
        <span data-tooltip="本次或上次运行中 Provider 报告的输出 Token 合计；是否包含内部思考 Token 取决于 Provider 的计量口径。">输出 Token<strong>{session.output_tokens.toLocaleString()}</strong></span>
        <span data-tooltip="本次或上次运行中各调用经统一审计后的总 Token 合计，包含 Provider 报告的缓存相关用量。">总 Token<strong>{session.total_tokens.toLocaleString()}</strong></span>
        <span data-tooltip={session.cost_complete ? "按各模型公开 API 单价或 Provider 返回成本折算的本次运行总成本；订阅 Auth 的实际账单可能不同。" : `仅 ${session.priced_call_count}/${session.call_count} 次调用可定价，因此不展示不完整的总成本。`}>
          等效成本<strong>{cost}</strong>
          {!session.cost_complete && <small>{session.priced_call_count}/{session.call_count} 可定价</small>}
        </span>
        <span data-tooltip="本次或上次运行内所有模型调用耗时的算术平均值，不使用引擎总运行时长计算。">平均调用耗时<strong>{(session.average_duration_ms / 1000).toFixed(2)}s</strong></span>
        <span data-tooltip="本次或上次运行的总 Token 除以模型调用次数。">平均 Token<strong>{session.average_tokens.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}</strong></span>
        <span data-tooltip={session.cost_complete ? "本次运行完整等效成本除以模型调用次数；订阅 Auth 的实际账单可能不同。" : "存在无法定价的调用，因此不计算可能误导的完整平均成本。"}>
          平均成本<strong>{averageCost}</strong>
        </span>
      </div>
      <p>等效成本按可用的 API 单价或 Provider 返回成本折算，订阅 Auth 的实际账单可能不同。</p>
    </section>
  );
}

function PanelTitle({ code, title, meta }: { code: string; title: string; meta: string }) {
  return <div className="panel-title"><span>{code}</span><h2>{title}</h2><small>{meta}</small></div>;
}

function RiskItem({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="risk-item" data-tooltip={RISK_DEFINITIONS[label]}><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>;
}

function money(value: string): string {
  return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

const DECISION_FILTERS: Array<{ key: "all" | DecisionEvent["outcome"]; label: string }> = [
  { key: "all", label: "全部" },
  { key: "approved", label: "风控放行" },
  { key: "executed", label: "下单成功" },
  { key: "execution_failed", label: "下单失败" },
  { key: "rejected", label: "风控否决" },
  { key: "hold", label: "HOLD" },
  { key: "analysis_only", label: "仅推理" },
];

const OUTCOME_LABELS: Record<DecisionEvent["outcome"], string> = {
  approved: "风控放行",
  executed: "下单成功",
  execution_failed: "下单失败",
  rejected: "风控否决",
  hold: "无需下单",
  analysis_only: "仅推理",
};

function intentPrice(value: string | null): string {
  return value === null ? "—" : Number(value).toFixed(4);
}

function executionPrice(value: string | null | undefined): string {
  return value == null ? "—" : Number(value).toFixed(4);
}

function executionLoss(value: string | null | undefined): string {
  return value == null
    ? "—"
    : `$${Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`;
}

function DecisionPanel({ decisions }: { decisions: DecisionEvent[] }) {
  const [filter, setFilter] = useState<"all" | DecisionEvent["outcome"]>("all");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [details, setDetails] = useState<Record<number, DecisionDetail>>({});
  const [detailLoading, setDetailLoading] = useState<number | null>(null);
  const [detailErrors, setDetailErrors] = useState<Record<number, string>>({});
  const [copied, setCopied] = useState<string | null>(null);
  const visible = filter === "all"
    ? decisions
    : decisions.filter((decision) => decision.outcome === filter);

  const toggleDecision = async (decision: DecisionEvent) => {
    if (expanded === decision.id) {
      setExpanded(null);
      return;
    }
    setExpanded(decision.id);
    if (details[decision.id] || detailLoading === decision.id) return;
    setDetailLoading(decision.id);
    setDetailErrors((current) => {
      const next = { ...current };
      delete next[decision.id];
      return next;
    });
    try {
      const detail = await api<DecisionDetail>(`/api/decision-events/${decision.id}`);
      setDetails((current) => ({ ...current, [decision.id]: detail }));
    } catch (reason) {
      setDetailErrors((current) => ({
        ...current,
        [decision.id]: reason instanceof Error ? reason.message : String(reason),
      }));
    } finally {
      setDetailLoading((current) => current === decision.id ? null : current);
    }
  };

  const copyDetail = async (key: string, value: string) => {
    try {
      await copyToClipboard(value);
      setCopied(key);
      window.setTimeout(() => setCopied((current) => current === key ? null : current), 1500);
    } catch {
      setCopied(null);
    }
  };

  return (
    <article className="panel signals-panel">
      <PanelTitle code="04" title="决策与风控" meta="LLM 意图 → 硬风控" />
      <div className="decision-filters" aria-label="决策筛选">
        {DECISION_FILTERS.map((item) => (
          <button
            className={filter === item.key ? "active" : ""}
            key={item.key}
            onClick={() => setFilter(item.key)}
          >{item.label}</button>
        ))}
      </div>
      <div className="signal-list">
        {visible.map((decision) => (
          <div className={`decision-event ${expanded === decision.id ? "expanded" : ""}`} key={decision.id}>
            <button
              className="signal decision-row"
              aria-expanded={expanded === decision.id}
              onClick={() => void toggleDecision(decision)}
            >
              <span className={`action ${decision.failover ? "provider-fail" : decision.intent.action.toLowerCase()}`}>
                {decision.failover ? "PROVIDER_FAIL" : decision.intent.action}
              </span>
              <span className="signal-symbol">
                <strong>{decision.intent.symbol}</strong>
                <small>{decision.intent.cadence} · {providerLabel(decision.provider)} · {inferenceConfigLabel(decision)}</small>
              </span>
              {decision.failover ? <span className="signal-confidence residual" data-tooltip="该 Provider 调用失败时在有序路由中的位置。">
                #{decision.failover.route_position}<small>{decision.failover.continues ? "继续切换" : "路由耗尽"}</small>
              </span> : <span
                className={`signal-confidence ${decision.intent.action === "HOLD" ? "residual" : ""}`}
                data-tooltip={decision.intent.action === "HOLD"
                  ? "HOLD 时表示当前快照仍残留的交易机会强度，不是盈利概率，也不代表模型输出可靠性。"
                  : "模型对该非 HOLD 方向在当前快照下具备可执行交易优势的估计；不是盈利概率，且不能绕过硬风控。"}
              >
                {Math.round(decision.intent.confidence * 100)}%
                <small>{decision.intent.action === "HOLD" ? "机会强度" : "执行置信度"}</small>
              </span>}
              <span className={`decision-outcome ${decision.outcome}`}>
                {decision.failover ? "故障切换" : OUTCOME_LABELS[decision.outcome]}
                {decision.outcome === "execution_failed" && decision.execution?.estimated_loss_usdt != null
                  ? <small>损失 {executionLoss(decision.execution.estimated_loss_usdt)}</small>
                  : null}
              </span>
              <span className="signal-time">{new Date(decision.created_at).toLocaleTimeString("zh-CN", { hour12: false })}</span>
              <span className="decision-chevron">{expanded === decision.id ? "−" : "+"}</span>
            </button>
            {expanded === decision.id && (
              <div className="decision-detail">
                <p>{decision.intent.rationale}</p>
                <div className="decision-detail-grid">
                  <span>模型<strong>{decision.model ?? "CLI 默认"}</strong></span>
                  <span>杠杆<strong>{decision.intent.leverage}×</strong></span>
                  <span>风险比例<strong>{percent(decision.intent.risk_fraction)}</strong></span>
                  <span>订单类型<strong>{decision.intent.order_type}</strong></span>
                  <span>入场价<strong>{intentPrice(decision.intent.entry_price)}</strong></span>
                  <span>止损<strong>{intentPrice(decision.intent.stop_loss)}</strong></span>
                  <span>止盈<strong>{intentPrice(decision.intent.take_profit)}</strong></span>
                  <span>风控数量<strong>{decision.risk?.decision.max_quantity ?? "—"}</strong></span>
                </div>
                <div className={`decision-reason ${decision.outcome}`}>
                  <strong>{decision.failover ? "故障切换" : decision.risk?.accepted ? "风控放行" : OUTCOME_LABELS[decision.outcome]}</strong>
                  <span>{decision.failover?.error ?? decision.risk?.reason ?? "该记录只有模型推理，未进入实时硬风控流程。"}</span>
                </div>
                {decision.execution && (
                  <div className={`execution-result ${decision.outcome}`}>
                    <div className="execution-result-heading">
                      <strong>{OUTCOME_LABELS[decision.outcome]}</strong>
                      <span>{decision.execution.message}</span>
                    </div>
                    <div className="execution-result-grid">
                      <span>执行状态<strong>{decision.execution.status}</strong></span>
                      <span>失败阶段<strong>{decision.execution.stage === "COMPLETE" ? "—" : decision.execution.stage}</strong></span>
                      <span>交易所错误<strong>{decision.execution.exchange_error_code ?? "—"}</strong></span>
                      <span>客户端订单号<strong>{decision.execution.client_order_id ?? "—"}</strong></span>
                      <span>入场成交<strong>{decision.execution.entry_report
                        ? `${decision.execution.entry_report.filled_quantity} @ ${executionPrice(decision.execution.entry_report.average_price)}`
                        : "—"}</strong></span>
                      <span>紧急回补<strong>{decision.execution.rescue_report
                        ? `${decision.execution.rescue_report.filled_quantity} @ ${executionPrice(decision.execution.rescue_report.average_price)}`
                        : "—"}</strong></span>
                      <span data-tooltip="保护单或下单失败后，入场与紧急回补之间的不利价差乘以已回补数量；仅在成交价可确认时计算，不含手续费。">失败损失（估算）<strong>{executionLoss(decision.execution.estimated_loss_usdt)}</strong></span>
                    </div>
                  </div>
                )}
                <AnalysisDetail
                  copied={copied}
                  detail={details[decision.id]}
                  error={detailErrors[decision.id]}
                  loading={detailLoading === decision.id}
                  onCopy={copyDetail}
                />
              </div>
            )}
          </div>
        ))}
        {!visible.length && <div className="empty cards">当前筛选条件下没有决策记录。</div>}
      </div>
    </article>
  );
}

async function copyToClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall through for embedded browsers that deny the async clipboard API.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

function AnalysisDetail({
  copied,
  detail,
  error,
  loading,
  onCopy,
}: {
  copied: string | null;
  detail?: DecisionDetail;
  error?: string;
  loading: boolean;
  onCopy: (key: string, value: string) => Promise<void>;
}) {
  if (loading) return <div className="analysis-detail-state">正在加载 AI 分析详情…</div>;
  if (error) return <div className="analysis-detail-state error">详情加载失败：{error}</div>;
  if (!detail) return null;

  const inputText = detail.input === null ? null : JSON.stringify(detail.input, null, 2);
  const usage = detail.usage;
  const cachedTokens = Number(
    usage.cached_input_tokens ?? usage.cache_read_input_tokens ?? 0,
  );
  const blocks = [
    { key: `input-${detail.id}`, title: "结构化输入", value: inputText },
    { key: `prompt-${detail.id}`, title: "实际 Prompt", value: detail.prompt },
    {
      key: `output-${detail.id}`,
      title: usage.error && detail.audit_status !== "complete"
        ? "Provider 错误信息"
        : "模型原始输出",
      value: detail.raw_output,
    },
  ];
  const missingAuditMessage = detail.audit_status === "unavailable"
    ? "该记录创建于输入审计启用前，无法补回当时的精确输入。"
    : "该记录只保存了部分输入审计；缺失内容无法补回。";

  return (
    <section className="analysis-detail">
      <div className="analysis-detail-heading">
        <div><strong>AI 分析详情</strong><small>逐次审计 · 本地保存</small></div>
        <span>{detail.provider} · {detail.model ?? "CLI 默认"} · {(detail.duration_ms / 1000).toFixed(2)}s</span>
      </div>
      <div className="analysis-usage-grid">
        <span>输入 Token<strong>{Number(usage.input_tokens ?? 0).toLocaleString("zh-CN")}</strong></span>
        <span>缓存输入<strong>{cachedTokens.toLocaleString("zh-CN")}</strong></span>
        <span>输出 Token<strong>{Number(usage.output_tokens ?? 0).toLocaleString("zh-CN")}</strong></span>
        <span>总 Token<strong>{Number(usage.total_tokens ?? 0).toLocaleString("zh-CN")}</strong></span>
        <span>等效成本<strong>{detail.equivalent_cost_usd === null ? "—" : `$${detail.equivalent_cost_usd.toFixed(6)}`}</strong></span>
      </div>
      {typeof usage.error_message === "string" && (
        <div className="analysis-detail-state error">Provider 调用失败：{usage.error_message}</div>
      )}
      <div className="analysis-blocks">
        {blocks.map((block) => (
          <div className="analysis-block" key={block.key}>
            <div>
              <strong>{block.title}</strong>
              {block.value !== null && (
                <button onClick={() => void onCopy(block.key, block.value ?? "")}>
                  {copied === block.key ? "已复制" : "复制"}
                </button>
              )}
            </div>
            {block.value === null
              ? <p>{missingAuditMessage}</p>
              : <pre>{block.value}</pre>}
          </div>
        ))}
      </div>
    </section>
  );
}

function AccountPanel({
  portfolio,
  positions,
  orders,
}: {
  portfolio: AccountPortfolio | null;
  positions: AccountPosition[];
  orders: OrderRecord[];
}) {
  const isTestnet = portfolio?.source === "binance-testnet";
  const displayedPnl = portfolio
    ? isTestnet ? portfolio.unrealized_pnl : portfolio.daily_pnl
    : null;
  return (
    <article className="panel account-panel">
      <PanelTitle
        code="06"
        title="账户与订单"
        meta={`${isTestnet ? "币安测试网账户" : "模拟账户"} · 只读`}
      />
      <div className="account-metrics">
        <Metric label="权益" value={portfolio ? money(portfolio.equity) : "—"} suffix="" />
        <Metric label="可用余额" value={portfolio ? money(portfolio.available_balance) : "—"} suffix="" />
        <div
          className="metric"
          data-tooltip={isTestnet
            ? METRIC_DEFINITIONS["未实现盈亏"]
            : "模拟账户当前权益相对本次运行起始权益的变化额，用于判断日亏熔断。"}
        ><span>{isTestnet ? "未实现盈亏" : "当日盈亏"}</span><strong className={Number(displayedPnl ?? 0) >= 0 ? "positive" : "negative"}>{displayedPnl === null ? "—" : money(displayedPnl)}</strong></div>
        <Metric label="占用保证金" value={portfolio ? money(portfolio.margin_used) : "—"} suffix="" />
        <Metric label="持仓数" value={portfolio ? String(portfolio.open_positions) : "—"} suffix="" />
      </div>

      <h4 className="account-subhead">持仓</h4>
      <div className="table-wrap account-table">
        <table>
          <thead><tr><th>标的</th><th>方向</th><th>数量</th><th>均价</th><th>标记价</th><th>杠杆</th><th>未实现盈亏</th><th>保护</th></tr></thead>
          <tbody>
            {positions.map((position) => (
              <tr key={position.symbol}>
                <td><strong>{position.symbol.replace("USDT", "")}</strong></td>
                <td className={position.side === "LONG" ? "positive" : "negative"}>{position.side}</td>
                <td>{Number(position.quantity).toFixed(4)}</td>
                <td>{Number(position.average_price).toFixed(4)}</td>
                <td>{Number(position.mark_price).toFixed(4)}</td>
                <td>{position.leverage}×</td>
                <td className={Number(position.unrealized_pnl) >= 0 ? "positive" : "negative"}>{money(position.unrealized_pnl)}</td>
                <td>{position.stop_loss === null
                  ? position.protection_source === "exchange" ? "交易所侧"
                    : position.protection_source === "missing" ? "缺失"
                      : position.protection_source === "unknown" ? "待确认" : "—"
                  : Number(position.stop_loss).toFixed(4)}</td>
              </tr>
            ))}
            {!positions.length && <tr><td colSpan={8} className="empty">当前无持仓。</td></tr>}
          </tbody>
        </table>
      </div>

      <h4 className="account-subhead">订单与成交</h4>
      <div className="table-wrap account-table">
        <table>
          <thead><tr><th>订单号</th><th>标的</th><th>状态</th><th>成交量</th><th>成交价</th><th>时间</th></tr></thead>
          <tbody>
            {orders.map((order) => (
              <tr key={order.id}>
                <td><small>{order.client_order_id}</small></td>
                <td>{order.symbol.replace("USDT", "")}</td>
                <td><span className={`order-status ${order.status.toLowerCase()}`}>{order.status}</span></td>
                <td>{Number(order.report.filled_quantity).toFixed(4)}</td>
                <td>{order.report.average_price === null ? "—" : Number(order.report.average_price).toFixed(4)}</td>
                <td><small>{new Date(order.created_at).toLocaleString("zh-CN", { hour12: false })}</small></td>
              </tr>
            ))}
            {!orders.length && <tr><td colSpan={6} className="empty">尚无订单记录。</td></tr>}
          </tbody>
        </table>
      </div>

    </article>
  );
}

function OperationsPanel({
  providerMetrics,
  testnetStatus,
  operationsError,
}: {
  providerMetrics: ProviderMetric[];
  testnetStatus: TestnetAccountStatus | null;
  operationsError: string | null;
}) {
  const reconciliation = testnetStatus?.reconciliation;
  const testnetSafe = reconciliation !== null
    && reconciliation !== undefined
    && reconciliation.unprotected_symbols.length === 0;
  return (
    <article className="panel operations-panel">
      <PanelTitle code="07" title="模型与测试网" meta="24 小时运维窗口 · 只读" />
      {operationsError && <div className="operations-error">部分运维数据暂不可用：{operationsError}</div>}
      <div className="operations-grid">
        <section>
          <h4 className="account-subhead">模型调用</h4>
          <div className="provider-metrics-list">
            {providerMetrics.map((metric) => (
              <div className="provider-metric-card" key={metric.provider}>
                <div className="provider-metric-heading">
                  <strong>{providerLabel(metric.provider)}</strong>
                  <span className={`status-pill ${metric.error_count === 0 ? "ok" : "off"}`}>
                    {metric.error_count === 0 ? "HEALTHY" : `${metric.error_count} ERROR`}
                  </span>
                </div>
                <div className="provider-metric-values">
                  <Metric label="调用量" value={String(metric.call_count)} suffix="" />
                  <Metric label="平均延迟" value={(metric.average_duration_ms / 1000).toFixed(2)} suffix="s" />
                  <Metric label="P95 延迟" value={(metric.p95_duration_ms / 1000).toFixed(2)} suffix="s" />
                  <Metric label="错误率" value={(metric.error_rate * 100).toFixed(1)} suffix="%" />
                </div>
                <div className="provider-metric-usage">
                  <span data-tooltip="过去 24 小时该 Provider 全部审计调用的总 Token 合计。">Token 用量<strong>{metric.tokens_total.toLocaleString("zh-CN")}</strong></span>
                  <span data-tooltip="过去 24 小时可定价调用的等效成本合计；无法定价的调用不计入，订阅 Auth 的实际账单可能不同。">
                    等效成本
                    <strong>{metric.cost_usd_total === null ? "—" : `$${metric.cost_usd_total.toFixed(4)}`}</strong>
                  </span>
                </div>
                <small className="metric-models">
                  {Object.entries(metric.models).map(([model, count]) => `${model} × ${count}`).join(" · ")}
                </small>
              </div>
            ))}
            {!providerMetrics.length && <div className="empty cards">过去 24 小时没有模型调用。</div>}
          </div>
          {providerMetrics.length > 0 && (
            <small className="usage-note">
              等效成本为按 API 标准计价的折算估算（Claude 用 CLI 自带成本，Codex 用 models.dev 逐 token 折算）；订阅计划实际不按次计费。无法定价的模型显示「—」。
            </small>
          )}
        </section>

        <section className="testnet-summary">
          <h4 className="account-subhead">币安测试网</h4>
          <div className="testnet-heading">
            <div>
              <strong>{testnetStatus?.enabled ? "账户已配置" : "未配置"}</strong>
              <small>{testnetStatus?.active ? "当前交易模式" : "当前未启用测试网交易模式"}</small>
            </div>
            <span className={`status-pill ${testnetStatus?.enabled ? "ok" : "off"}`}>
              {testnetStatus?.enabled ? "TESTNET" : "DISABLED"}
            </span>
          </div>
          <div className="testnet-balances">
            <Metric label="钱包余额" value={testnetStatus?.account ? money(testnetStatus.account.total_wallet_balance) : "—"} suffix="" />
            <Metric label="可用余额" value={testnetStatus?.account ? money(testnetStatus.account.available_balance) : "—"} suffix="" />
            <Metric label="未实现盈亏" value={testnetStatus?.account ? money(testnetStatus.account.total_unrealized_profit) : "—"} suffix="" />
          </div>
          <div className="testnet-checks">
            <span><i className={testnetStatus?.user_stream.running ? "ok" : ""} />用户流 {testnetStatus?.user_stream.running ? "在线" : "离线"}</span>
            <span><i className={testnetSafe ? "ok" : ""} />启动对账 {reconciliation ? testnetSafe ? "安全" : "有未保护仓位" : "尚未执行"}</span>
            <span><i className={testnetStatus?.account?.can_trade ? "ok" : ""} />可用保证金 {testnetStatus?.account?.can_trade ? "就绪" : "不足"}</span>
          </div>
          {testnetStatus?.positions.length ? (
            <div className="table-wrap testnet-positions">
              <table>
                <thead><tr><th>持仓</th><th>数量</th><th>标记价</th><th>杠杆</th><th>未实现盈亏</th></tr></thead>
                <tbody>{testnetStatus.positions.map((position) => (
                  <tr key={position.symbol}>
                    <td>{position.symbol}</td>
                    <td>{Number(position.position_amount).toFixed(4)}</td>
                    <td>{Number(position.mark_price).toFixed(4)}</td>
                    <td>{position.leverage}×</td>
                    <td className={Number(position.unrealized_profit) >= 0 ? "positive" : "negative"}>{money(position.unrealized_profit)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : <div className="empty testnet-empty">测试网当前无持仓。</div>}
        </section>
      </div>
    </article>
  );
}

function EquityChart({ points }: { points: Array<{ timestamp: string; equity: string }> }) {
  if (points.length < 2) return <div className="empty chart-empty">权益点不足，无法绘制曲线。</div>;
  const width = 900;
  const height = 190;
  const values = points.map((point) => Number(point.equity));
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum || 1;
  const coordinates = values.map((value, index) => {
    const x = (index / (values.length - 1)) * width;
    const y = height - 12 - ((value - minimum) / range) * (height - 24);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return (
    <div className="equity-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="回测权益曲线">
        <line x1="0" y1={height - 12} x2={width} y2={height - 12} />
        <polyline points={coordinates} />
      </svg>
      <div><span>{minimum.toFixed(2)}</span><strong>{values.at(-1)?.toFixed(2)}</strong><span>{maximum.toFixed(2)}</span></div>
    </div>
  );
}

const GROUP_LABELS: Record<string, string> = {
  side: "方向",
  exit_reason: "退出原因",
  regime: "市场状态",
};

const REGIME_LABELS: Record<string, string> = {
  trend_up: "上升趋势",
  trend_down: "下降趋势",
  range: "震荡",
  high_volatility: "高波动",
  unknown: "未知",
};

type GroupStats = BacktestRun["result"]["grouped_stats"];

function GroupedStatsPanel({ grouped }: { grouped: GroupStats }) {
  const groups = Object.entries(grouped).filter(([, buckets]) => Object.keys(buckets).length > 0);
  if (!groups.length) return null;
  return (
    <div className="grouped-stats">
      {groups.map(([groupName, buckets]) => (
        <div className="grouped-stats-block" key={groupName}>
          <h4>{GROUP_LABELS[groupName] ?? groupName}</h4>
          <div className="table-wrap">
            <table>
              <thead><tr><th>分组</th><th>笔数</th><th>胜率</th><th>净盈亏</th><th>盈亏因子</th></tr></thead>
              <tbody>
                {Object.entries(buckets).map(([bucket, stats]) => (
                  <tr key={bucket}>
                    <td>{groupName === "regime" ? (REGIME_LABELS[bucket] ?? bucket) : bucket}</td>
                    <td>{stats.trade_count}</td>
                    <td>{(Number(stats.win_rate) * 100).toFixed(1)}%</td>
                    <td className={Number(stats.net_pnl) >= 0 ? "positive" : "negative"}>{Number(stats.net_pnl).toFixed(2)}</td>
                    <td>{stats.profit_factor === null ? "—" : Number(stats.profit_factor).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

function BacktestDetail({ run, onClose }: { run: BacktestRun; onClose: () => void }) {
  const trades = run.result.trades ?? [];
  const curve = run.result.equity_curve ?? [];
  return (
    <section className="backtest-detail" aria-label={`回测 #${run.id} 详情`}>
      <div className="detail-heading">
        <div><span className="eyebrow">RUN #{run.id} / {run.symbol} / {run.cadence}</span><h3>回测详情</h3></div>
        <button className="text-button" onClick={onClose}>关闭</button>
      </div>
      <div className="detail-metrics">
        <Metric label="总收益" value={(Number(run.result.total_return) * 100).toFixed(2)} suffix="%" />
        <Metric label="最大回撤" value={(Number(run.result.max_drawdown) * 100).toFixed(2)} suffix="%" />
        <Metric label="Sharpe" value={run.result.sharpe_ratio === null ? "—" : Number(run.result.sharpe_ratio).toFixed(2)} suffix="" />
        <Metric label="Sortino" value={run.result.sortino_ratio === null ? "—" : Number(run.result.sortino_ratio).toFixed(2)} suffix="" />
        <Metric label="换手" value={Number(run.result.turnover).toFixed(2)} suffix="×" />
      </div>
      <EquityChart points={curve} />
      <GroupedStatsPanel grouped={run.result.grouped_stats} />
      <div className="table-wrap detail-trades">
        <table>
          <thead><tr><th>方向</th><th>数量</th><th>入场</th><th>出场</th><th>净盈亏</th><th>费用</th><th>原因</th></tr></thead>
          <tbody>
            {trades.map((trade, index) => (
              <tr key={`${trade.entry_time}-${index}`}>
                <td className={trade.side === "LONG" ? "positive" : "negative"}>{trade.side}</td>
                <td>{Number(trade.quantity).toFixed(4)}</td>
                <td>{Number(trade.entry_price).toFixed(4)}<small>{new Date(trade.entry_time).toLocaleString("zh-CN", { hour12: false })}</small></td>
                <td>{Number(trade.exit_price).toFixed(4)}<small>{new Date(trade.exit_time).toLocaleString("zh-CN", { hour12: false })}</small></td>
                <td className={Number(trade.net_pnl) >= 0 ? "positive" : "negative"}>{Number(trade.net_pnl).toFixed(2)}</td>
                <td>{Number(trade.fees).toFixed(2)}</td>
                <td>{trade.exit_reason}</td>
              </tr>
            ))}
            {!trades.length && <tr><td colSpan={7} className="empty">该运行没有成交记录。</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}
