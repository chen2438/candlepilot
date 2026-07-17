import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AccountPortfolio,
  AccountPosition,
  BacktestDecision,
  BacktestEstimate,
  BacktestResult,
  BacktestRun,
  Candidate,
  CollectorStatus,
  ProbeStatus,
  DecisionEvent,
  DecisionDetail,
  EngineStatus,
  OrderRecord,
  ProviderHealth,
  ProviderMetric,
  ProviderMetricsResponse,
  CustomProvider,
  CustomProvidersPayload,
  RunSessionMetrics,
  SettingsField,
  SettingsPayload,
  TestnetAccountStatus,
} from "./types";

const EXIT_REASON: Record<string, string> = {
  stop_loss: "止损",
  take_profit: "止盈",
  model_exit: "模型退出",
  run_end: "回测收尾",
};

function signedUsdt(value: number) {
  // Four decimals keep the displayed components reconcilable; rounding each
  // leg to cents can make two correct decimals look one cent apart.
  return `${value > 0 ? "+" : ""}${value.toFixed(4)} USDT`;
}

type SymbolBreakdown = {
  symbol: string;
  grossPnl: number;
  fees: number;
  funding: number;
  netPnl: number;
  tradeCount: number;
  contributionReturn: number;
};

function backtestSymbolBreakdown(result: BacktestResult): SymbolBreakdown[] {
  if (result.symbol_results) {
    return result.symbol_results.map((item) => ({
      symbol: item.symbol,
      grossPnl: Number(item.gross_price_pnl),
      fees: Number(item.total_fees),
      funding: Number(item.total_funding),
      netPnl: Number(item.net_pnl),
      tradeCount: item.trade_count,
      contributionReturn: Number(item.contribution_return),
    }));
  }
  const initialEquity = Number(result.initial_equity);
  const grouped = new Map<string, SymbolBreakdown>();
  for (const trade of result.trades ?? []) {
    const current = grouped.get(trade.symbol) ?? {
      symbol: trade.symbol, grossPnl: 0, fees: 0, funding: 0,
      netPnl: 0, tradeCount: 0, contributionReturn: 0,
    };
    const net = Number(trade.net_pnl);
    const fees = Number(trade.fees);
    const funding = Number(trade.funding);
    current.netPnl += net;
    current.fees += fees;
    current.funding += funding;
    current.grossPnl += net + fees + funding;
    current.tradeCount += 1;
    current.contributionReturn = initialEquity ? current.netPnl / initialEquity : 0;
    grouped.set(trade.symbol, current);
  }
  return [...grouped.values()].sort((left, right) => left.symbol.localeCompare(right.symbol));
}

function BacktestResultDetail({ result }: { result: BacktestResult | null }) {
  if (!result) return <div className="backtest-result-empty">正在读取收益明细；未完成的运行会在结束后生成。</div>;
  const netPnl = result.net_pnl === undefined
    ? Number(result.final_equity) - Number(result.initial_equity)
    : Number(result.net_pnl);
  const fees = Number(result.total_fees);
  const fundingCost = Number(result.total_funding);
  const grossPnl = result.gross_price_pnl === undefined
    ? netPnl + fees + fundingCost
    : Number(result.gross_price_pnl);
  const forcedCloses = result.run_end_trade_count
    ?? result.trades?.filter((trade) => trade.exit_reason === "run_end").length;
  const symbolBreakdown = backtestSymbolBreakdown(result);

  return (
    <section className="backtest-result-detail">
      <div className="backtest-result-heading">
        <strong>收益构成</strong>
        <small>净盈亏 = 价格盈亏（已含滑点） − 手续费 − 资金费成本</small>
      </div>
      <div className="backtest-pnl-grid">
        <span><small>初始权益</small><strong>{Number(result.initial_equity).toFixed(2)} USDT</strong></span>
        <span data-tooltip="所有已平仓交易按成交价计算的盈亏；成交价已经包含入场与退出滑点。">
          <small>价格盈亏（毛）</small><strong className={grossPnl >= 0 ? "positive" : "negative"}>{signedUsdt(grossPnl)}</strong>
        </span>
        <span data-tooltip="开仓和退出两侧手续费的合计；这里显示它对权益的实际影响。">
          <small>手续费影响</small><strong className="negative">{signedUsdt(-fees)}</strong>
        </span>
        <span data-tooltip="资金费为正表示策略支付、为负表示策略收取；这里显示它对权益的实际影响。">
          <small>资金费影响</small><strong className={-fundingCost >= 0 ? "positive" : "negative"}>{signedUsdt(-fundingCost)}</strong>
        </span>
        <span data-tooltip="最终权益减初始权益；回测收尾后没有未实现盈亏。">
          <small>净盈亏</small><strong className={netPnl >= 0 ? "positive" : "negative"}>{signedUsdt(netPnl)}</strong>
        </span>
        <span><small>最终权益 / 总收益</small><strong>{Number(result.final_equity).toFixed(2)} USDT</strong><em>{(Number(result.total_return) * 100).toFixed(2)}%</em></span>
      </div>
      {symbolBreakdown.length > 0 && (
        <div className="backtest-symbols table-wrap">
          <div className="backtest-symbols-heading">
            <strong>按标的拆分</strong>
            <small>收益贡献使用共享初始权益作分母；各行净盈亏与贡献之和等于组合结果。</small>
          </div>
          <table>
            <thead><tr><th>标的</th><th>交易</th><th data-tooltip="该标的全部已平仓交易按成交价计算的盈亏，成交价已经包含滑点。">价格盈亏（毛）</th><th>手续费影响</th><th>资金费影响</th><th>净盈亏</th><th data-tooltip="该标的净盈亏除以整个组合的初始权益；这是组合收益贡献，不是为该标的虚拟分配一份本金后的独立收益率。">收益贡献</th></tr></thead>
            <tbody>{symbolBreakdown.map((item) => (
              <tr key={item.symbol}>
                <td><strong>{item.symbol}</strong></td>
                <td>{item.tradeCount}</td>
                <td className={item.grossPnl >= 0 ? "positive" : "negative"}>{signedUsdt(item.grossPnl)}</td>
                <td className="negative">{signedUsdt(-item.fees)}</td>
                <td className={-item.funding >= 0 ? "positive" : "negative"}>{signedUsdt(-item.funding)}</td>
                <td className={item.netPnl >= 0 ? "positive" : "negative"}>{signedUsdt(item.netPnl)}</td>
                <td className={item.contributionReturn >= 0 ? "positive" : "negative"}>{(item.contributionReturn * 100).toFixed(2)}%</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="backtest-closeout">
        <strong>收尾处理</strong>
        <span>
          {forcedCloses === undefined
            ? "旧记录未保存强制平仓计数"
            : `按最后可用价格强制平仓 ${forcedCloses} 笔（含退出滑点与手续费）`}
        </span>
        <span>
          {result.cancelled_pending_orders === undefined
            ? "旧记录未保存挂单撤销计数"
            : `撤销未成交挂单 ${result.cancelled_pending_orders} 笔（不产生盈亏）`}
        </span>
      </div>
      {result.trades && result.trades.length > 0 && (
        <div className="backtest-trades table-wrap">
          <table>
            <thead><tr><th>交易</th><th>入场 → 出场</th><th>价格盈亏</th><th>手续费</th><th>资金费影响</th><th>净盈亏</th><th>退出</th></tr></thead>
            <tbody>{result.trades.map((trade, index) => {
              const tradeNet = Number(trade.net_pnl);
              const tradeFees = Number(trade.fees);
              const tradeFunding = Number(trade.funding);
              const tradeGross = tradeNet + tradeFees + tradeFunding;
              return <tr key={`${trade.symbol}-${trade.entry_time}-${index}`}>
                <td><strong>{trade.symbol} · {trade.side}</strong><small>{trade.quantity}</small></td>
                <td>{Number(trade.entry_price).toFixed(4)} → {Number(trade.exit_price).toFixed(4)}</td>
                <td className={tradeGross >= 0 ? "positive" : "negative"}>{signedUsdt(tradeGross)}</td>
                <td className="negative">{signedUsdt(-tradeFees)}</td>
                <td className={-tradeFunding >= 0 ? "positive" : "negative"}>{signedUsdt(-tradeFunding)}</td>
                <td className={tradeNet >= 0 ? "positive" : "negative"}>{signedUsdt(tradeNet)}</td>
                <td>{EXIT_REASON[trade.exit_reason] ?? trade.exit_reason}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

const emptyStatus: EngineStatus = {
  running: false,
  emergency_locked: false,
  emergency_locked_until: null,
  selected_provider: null,
  backup_provider: null,
  provider_chain: [],
  active_provider: null,
  provider_routes: [],
  active_cadences: ["5m", "15m", "30m"],
  run_limits: { max_run_seconds: null, max_run_cost_usd: null },
  auto_stop_reason: null,
  route_exhausted_since: null,
  supported_cadences: ["5m", "15m", "30m"],
  candidates_per_cycle: 5,
  max_candidates_per_cycle: 20,
  candidate_count: 0,
  universe_refreshed_at: null,
  user_stream: {
    enabled: false,
    running: false,
    event_count: 0,
    last_event_at: null,
    reconnect_count: 0,
    dropped_event_count: 0,
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
  { key: "user_events", label: "测试网事件", hint: "用户数据流" },
  { key: "alerts", label: "告警历史", hint: "" },
  { key: "backtests", label: "回测记录", hint: "运行、逐模型结果与每条决策" },
  // Last, and worded plainly: the others are records of things that happened,
  // this one is data that can never be obtained again.
  { key: "book_captures", label: "盘口采集", hint: "币安不提供历史盘口，删掉无法重录" },
  { key: "market_cache", label: "行情缓存", hint: "Parquet" },
  { key: "pricing_cache", label: "定价缓存", hint: "models.dev" },
];

type TabKey = "overview" | "account" | "backtest" | "operations" | "data" | "settings";

const TABS: Array<{ key: TabKey; label: string; meta: string }> = [
  { key: "overview", label: "总览", meta: "引擎 · 接入 · 候选 · 决策" },
  { key: "account", label: "账户", meta: "持仓 · 订单" },
  { key: "backtest", label: "回测", meta: "历史模式 · 多模型对比" },
  { key: "operations", label: "运维", meta: "模型 · 测试网" },
  { key: "data", label: "数据", meta: "删除历史数据" },
  { key: "settings", label: "设置", meta: "编辑本地 .env" },
];

const METRIC_DEFINITIONS: Record<string, string> = {
  "候选标的": "最近一次全市场扫描后进入动态候选池的 USDT 永续合约数量；候选池最多保留 20 个，不等于每个周期实际送入模型的数量。",
  "最大杠杆": "硬风控允许模型请求的最高杠杆倍数；实际交易可以更低，不能由模型突破。",
  "日亏熔断": "当日净亏损达到当日起始权益的 8% 时，硬风控拒绝新增风险仓位。",
  "权益": "账户现金或钱包余额加上按最新标记价计算的未实现盈亏。",
  "可用余额": "扣除当前保证金占用后，仍可用于新订单保证金的账户余额。",
  "占用保证金": "当前非零持仓占用的保证金合计，由交易所返回。",
  "持仓数": "当前数量非零的单向净仓标的数量。",
  "调用量": "过去 24 小时写入本地推理审计的该 Provider 调用记录数，包括失败并降级的记录。",
  "平均延迟": "过去 24 小时该 Provider 单次模型调用耗时的算术平均值。",
  "P95 延迟": "过去 24 小时调用耗时的第 95 百分位；约 95% 的调用不超过该值。",
  "错误率": "过去 24 小时带 Provider 错误标记的调用数除以调用总数。",
  "钱包余额": "币安测试网账户的钱包余额，不包含当前未实现盈亏。",
  "未实现盈亏": "全部未平仓头寸按最新标记价计算的浮动盈亏合计。",
  "当日盈亏": "从当日 UTC 00:00 起的已实现盈亏、手续费和资金费，加上当前未实现盈亏；硬风控用它判断 8% 日亏熔断。",
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

const UNIVERSE_COLLAPSED_ROWS = 5;

const CUSTOM_PROVIDER_PREFIX = "openai-compatible:";

// Extra custom endpoints are named "openai-compatible:<id>"; the id is what
// distinguishes them for the user.
function customProviderId(name: string): string | null {
  return name.startsWith(CUSTOM_PROVIDER_PREFIX)
    ? name.slice(CUSTOM_PROVIDER_PREFIX.length)
    : null;
}

function providerLabel(name: string): string {
  if (name === "codex-auth") return "Codex Auth";
  if (name === "claude-code-auth") return "Claude Code Auth";
  if (name === "openai-compatible") return "Custom API";
  const id = customProviderId(name);
  if (id) return `Custom API · ${id}`;
  return name;
}

function modelConfigSummary(model: string | null, effort: string | null, recorded = true): string {
  if (!recorded) return "旧记录未保存模型配置";
  return `${model ?? "Provider 默认模型"} · ${effort ? `推理 ${effort}` : "默认推理强度"}`;
}

function providerIcon(name: string): string {
  if (name === "codex-auth") return "CX";
  if (name === "claude-code-auth") return "CC";
  if (name === "openai-compatible" || customProviderId(name)) return "API";
  return "AI";
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

const DECISION_PAGE_SIZE = 50;

type DecisionFilter = "all" | DecisionEvent["outcome"];

function decisionQueryUrl(filter: DecisionFilter, beforeId?: number): string {
  const params = new URLSearchParams({ limit: String(DECISION_PAGE_SIZE) });
  // Filtering happens server-side over the whole table. Filtering the loaded
  // page in the browser instead would answer "show me every rejection" with
  // only the rejections that happen to be in the newest 50 rows.
  if (filter !== "all") params.set("outcome", filter);
  if (beforeId !== undefined) params.set("before_id", String(beforeId));
  return `/api/decision-events?${params}`;
}

export default function App() {
  const [tab, setTab] = useState<TabKey>("overview");
  const [status, setStatus] = useState<EngineStatus>(emptyStatus);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [decisions, setDecisions] = useState<DecisionEvent[]>([]);
  const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>("all");
  const [decisionsExhausted, setDecisionsExhausted] = useState(false);
  const decisionFilterRef = useRef(decisionFilter);
  decisionFilterRef.current = decisionFilter;
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
  const [configDraft, setConfigDraft] = useState<Record<string, {
    model: string;
    effort: string;
    custom: boolean;
    pricing: string;
  }>>({});
  const [historySelected, setHistorySelected] = useState<Record<string, boolean>>({});
  const [historyConfirm, setHistoryConfirm] = useState(false);
  const [historyResult, setHistoryResult] = useState<string | null>(null);
  const [candidateDraft, setCandidateDraft] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; text: string }>>({});
  const [universeExpanded, setUniverseExpanded] = useState(false);
  const [limitDraft, setLimitDraft] = useState<{ minutes: string; budget: string } | null>(null);

  const applyProviderConfig = useCallback(async (
    name: string,
    draft: { model: string; effort: string; pricing?: string },
  ) => {
    setBusy("provider-config");
    setError(null);
    try {
      const next = await api<ProviderHealth[]>("/api/providers/config", {
        method: "POST",
        body: JSON.stringify({
          name,
          model: draft.model,
          reasoning_effort: draft.effort || null,
          ...(draft.pricing === undefined ? {} : { pricing: draft.pricing || null }),
        }),
      });
      setProviders(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const testProvider = useCallback(async (
    name: string,
    draft?: { model: string; effort: string; pricing?: string },
  ) => {
    setBusy(`test-${name}`);
    setError(null);
    setTestResult((current) => ({ ...current, [name]: { ok: false, text: "测试中…" } }));
    try {
      if (draft) {
        const next = await api<ProviderHealth[]>("/api/providers/config", {
          method: "POST",
          body: JSON.stringify({
            name,
            model: draft.model,
            reasoning_effort: draft.effort || null,
            ...(draft.pricing === undefined ? {} : { pricing: draft.pricing || null }),
          }),
        });
        setProviders(next);
      }
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

  // Limits are sent as a pair: the engine treats null as "unbounded", so an
  // empty box clears that dimension rather than leaving a stale limit behind.
  const applyRunLimits = useCallback(async (minutes: string, budget: string) => {
    const parse = (raw: string, scale = 1) => {
      const trimmed = raw.trim();
      if (!trimmed) return null;
      const value = Number(trimmed) * scale;
      return Number.isFinite(value) && value > 0 ? value : null;
    };
    setBusy("run-limits");
    setError(null);
    try {
      setStatus(
        await api<EngineStatus>("/api/run-limits", {
          method: "POST",
          body: JSON.stringify({
            max_run_seconds: parse(minutes, 60),
            max_run_cost_usd: parse(budget),
          }),
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const refresh = useCallback(async () => {
    const [nextStatus, nextProviders, nextCandidates] = await Promise.all([
      api<EngineStatus>("/api/status"),
      api<ProviderHealth[]>("/api/providers"),
      api<Candidate[]>("/api/universe"),
    ]);
    setStatus(nextStatus);
    setProviders(nextProviders);
    setCandidates(nextCandidates);
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

  // The live tail is merged, not replaced: paging older decisions in would
  // otherwise be wiped by the next push two seconds later. The audit log is
  // append-only, but an event does change after it is written -- risk and
  // execution rows land later -- so newer copies win by id.
  const mergeDecisions = useCallback((incoming: DecisionEvent[]) => {
    setDecisions((current) => {
      const byId = new Map(current.map((event) => [event.id, event]));
      for (const event of incoming) byId.set(event.id, event);
      return [...byId.values()].sort((left, right) => right.id - left.id);
    });
  }, []);

  const refreshDecisions = useCallback(async () => {
    mergeDecisions(await api<DecisionEvent[]>(decisionQueryUrl(decisionFilter)));
  }, [decisionFilter, mergeDecisions]);
  const refreshDecisionsRef = useRef(refreshDecisions);
  refreshDecisionsRef.current = refreshDecisions;

  const loadOlderDecisions = useCallback(async () => {
    const oldest = decisions.at(-1);
    const older = await api<DecisionEvent[]>(
      decisionQueryUrl(decisionFilter, oldest ? oldest.id : undefined),
    );
    setDecisionsExhausted(!older.length);
    mergeDecisions(older);
  }, [decisions, decisionFilter, mergeDecisions]);

  // A filtered list is a query result over the whole table, so switching the
  // filter drops the previous page: it was answering a different question.
  useEffect(() => {
    setDecisions([]);
    setDecisionsExhausted(false);
    refreshDecisions().catch(() => undefined);
  }, [refreshDecisions]);

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
      if (decisionFilterRef.current !== "all") return;
      refreshDecisionsRef.current().catch(() => undefined);
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
        // The push is the newest rows unfiltered, so it cannot be merged into a
        // filtered list -- doing so would sprinkle in decisions that do not
        // match what the user asked for. Filtering pauses the live tail.
        if (message.type === "decisions" && decisionFilterRef.current === "all") {
          mergeDecisions(message.data);
        }
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
  }, [refresh, refreshAccount, mergeDecisions, refreshOperations, refreshRunSession]);

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
          <strong>币安测试网 · 真实撮合</strong>
          <small>{status.user_stream.running ? `账户流实时 · ${status.user_stream.event_count} 事件` : "账户流待启动 · REST 行情"}</small>
        </div>
        <div className="live-state">
          <span className={`dot ${socketOnline ? "online" : ""}`} />
          {socketOnline ? "CONSOLE ONLINE" : "CONSOLE OFFLINE"}
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
        {status.auto_stop_reason && !status.running && <div className="lock-banner">引擎已自动停止：{status.auto_stop_reason}。持仓保持不变（测试网仍由交易所侧止盈止损保护）；确认后可重新启动。</div>}
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
            <div className="cadence-select" title={status.running ? "运行时锁定" : "到达任一上限即自动优雅停止；留空表示不限"}>
              <span>运行上限（留空=不限）</span>
              <div className="limit-row">
                <label>
                  <input
                    type="number"
                    min={1}
                    step={1}
                    placeholder="分钟"
                    value={limitDraft ? limitDraft.minutes : status.run_limits.max_run_seconds ? String(Math.round(status.run_limits.max_run_seconds / 60)) : ""}
                    disabled={busy !== null || status.running}
                    onFocus={(event) => event.target.select()}
                    onChange={(event) => setLimitDraft({
                      minutes: event.target.value,
                      budget: limitDraft ? limitDraft.budget : status.run_limits.max_run_cost_usd ? String(status.run_limits.max_run_cost_usd) : "",
                    })}
                  />
                  <small>分钟</small>
                </label>
                <label>
                  <input
                    type="number"
                    min={0}
                    step="0.01"
                    placeholder="预算"
                    value={limitDraft ? limitDraft.budget : status.run_limits.max_run_cost_usd ? String(status.run_limits.max_run_cost_usd) : ""}
                    disabled={busy !== null || status.running}
                    onFocus={(event) => event.target.select()}
                    onChange={(event) => setLimitDraft({
                      minutes: limitDraft ? limitDraft.minutes : status.run_limits.max_run_seconds ? String(Math.round(status.run_limits.max_run_seconds / 60)) : "",
                      budget: event.target.value,
                    })}
                  />
                  <small>$ 等效</small>
                </label>
                <button
                  className="text-button"
                  disabled={busy !== null || status.running || limitDraft === null}
                  onClick={async () => {
                    if (!limitDraft) return;
                    await applyRunLimits(limitDraft.minutes, limitDraft.budget);
                    setLimitDraft(null);
                  }}
                >{busy === "run-limits" ? "…" : "应用"}</button>
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
            <datalist id="runtime-pricing-providers">
              {[...new Set(providers.flatMap((provider) => provider.pricing_options))]
                .map((option) => <option key={option} value={option} />)}
            </datalist>
            <div className="provider-list">
              {providers.map((provider) => {
                const routeIndex = status.provider_chain.indexOf(provider.provider);
                const options = provider.model_options ?? [];
                const model = configDraft[provider.provider]?.model ?? provider.model ?? "";
                const effort = configDraft[provider.provider]?.effort ?? provider.reasoning_effort ?? "";
                const customProvider = customProviderId(provider.provider) !== null;
                const pricing = configDraft[provider.provider]?.pricing ?? provider.pricing ?? "";
                const custom = configDraft[provider.provider]?.custom ?? (model !== "" && !options.includes(model));
                const draft = { model, effort, custom, pricing };
                const dirty = model !== (provider.model ?? "")
                  || effort !== (provider.reasoning_effort ?? "")
                  || (customProvider && pricing !== (provider.pricing ?? ""));
                const update = (next: Partial<typeof draft>) =>
                  setConfigDraft((current) => ({ ...current, [provider.provider]: { ...draft, ...next } }));
                return <div
                  key={provider.provider}
                  className={`provider-card ${routeIndex >= 0 ? "selected" : ""}`}
                >
                  <button
                    className="provider-card-main"
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
                  <div className={`provider-card-config ${customProvider ? "has-pricing" : ""}`}>
                    <label>
                      <span>模型</span>
                      <div className="config-model-cell">
                        <select
                          value={custom ? "__custom__" : model}
                          disabled={status.running}
                          onChange={(event) => event.target.value === "__custom__" ? update({ custom: true }) : update({ model: event.target.value, custom: false })}
                        >
                          <option value="">默认模型</option>
                          {options.map((option) => <option key={option} value={option}>{option}</option>)}
                          <option value="__custom__">自定义…</option>
                        </select>
                        {custom && <input
                          className="config-model-custom"
                          placeholder="输入模型名"
                          value={model}
                          disabled={status.running}
                          onChange={(event) => update({ model: event.target.value })}
                        />}
                      </div>
                    </label>
                    <label>
                      <span>推理强度</span>
                      <select value={effort} disabled={status.running} onChange={(event) => update({ effort: event.target.value })}>
                        <option value="">默认强度</option>
                        {provider.reasoning_effort_options.map((option) => <option key={option} value={option}>{option}</option>)}
                      </select>
                    </label>
                    {customProvider && <label data-tooltip="models.dev 的厂商 ID，决定按谁的价折算等效成本。留空则成本未知，运行预算不会按该端点触发；永久配置请在设置页保存。">
                      <span>计费厂商</span>
                      <input
                        list="runtime-pricing-providers"
                        value={pricing}
                        placeholder="如 xai · 留空不计成本"
                        disabled={status.running}
                        onChange={(event) => update({ pricing: event.target.value })}
                      />
                    </label>}
                    <div className="provider-card-actions">
                      <button className="text-button" disabled={status.running || busy !== null || !dirty}
                        onClick={() => applyProviderConfig(provider.provider, {
                          model,
                          effort,
                          ...(customProvider ? { pricing } : {}),
                        })}>应用</button>
                      <button className="text-button" disabled={status.running || busy !== null}
                        title={dirty
                          ? "应用当前模型与推理强度后立即发起真实调用"
                          : provider.authenticated
                            ? "用当前配置发起一次真实调用"
                            : "发起真实调用并查看当前配置不可用的具体原因"}
                        onClick={() => testProvider(
                          provider.provider,
                          dirty ? {
                            model,
                            effort,
                            ...(customProvider ? { pricing } : {}),
                          } : undefined,
                        )}>
                        {busy === `test-${provider.provider}`
                          ? "测试中…"
                          : dirty ? "应用并测试" : "测试"}
                      </button>
                    </div>
                    {testResult[provider.provider] && <span className={`config-test-result ${testResult[provider.provider].ok ? "ok" : "err"}`}>
                      {testResult[provider.provider].text}
                    </span>}
                  </div>
                </div>;
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
                  {(universeExpanded ? candidates : candidates.slice(0, UNIVERSE_COLLAPSED_ROWS)).map((candidate) => (
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
            {candidates.length > UNIVERSE_COLLAPSED_ROWS && (
              <button
                className="universe-toggle"
                aria-expanded={universeExpanded}
                onClick={() => setUniverseExpanded((current) => !current)}
              >
                {universeExpanded
                  ? `收起，只看前 ${UNIVERSE_COLLAPSED_ROWS} 个`
                  : `展开全部 ${candidates.length} 个（还有 ${candidates.length - UNIVERSE_COLLAPSED_ROWS} 个）`}
              </button>
            )}
          </article>

          <DecisionPanel
            decisions={decisions}
            filter={decisionFilter}
            onFilter={setDecisionFilter}
            onLoadOlder={loadOlderDecisions}
            exhausted={decisionsExhausted}
          />
        </section>
        </>)}


        {tab === "backtest" && (
        <section className="grid">
          <BacktestPanel providers={providers} engineRunning={status.running} />
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

        {tab === "settings" && (
        <section className="grid">
          <SettingsPanel busy={busy} setBusy={setBusy} setError={setError} />
        </section>
        )}
      </main>
      <footer><span>CANDLEPILOT / GPL-3.0</span><span>LOCALHOST ONLY · NO LIVE MONEY</span></footer>
    </div>
  );
}

function RestartPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const restart = useCallback(async () => {
    setBusy("restart");
    setError(null);
    setNote("正在重启后端…");
    try {
      await api<{ restarting: boolean }>("/api/restart", { method: "POST" });
    } catch (reason) {
      setBusy(null);
      setConfirming(false);
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
      return;
    }
    // The process is replaced, so poll until the new one answers, then reload
    // to pick up the fresh state.
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      try {
        const response = await fetch("/api/health/live", { cache: "no-store" });
        if (response.ok) {
          setNote("后端已重启，正在刷新…");
          window.location.reload();
          return;
        }
      } catch {
        // Expected while the old process is gone and the new one is binding.
      }
    }
    setBusy(null);
    setConfirming(false);
    setNote(null);
    setError("后端在 30 秒内没有恢复，请检查启动它的终端。");
  }, [setBusy, setError]);

  return (
    <div className="settings-section">
      <h4 className="account-subhead">重启后端</h4>
      <div className="settings-actions">
        {!confirming ? (
          <button className="compact" disabled={busy !== null} onClick={() => setConfirming(true)}>
            重启后端
          </button>
        ) : (
          <>
            <span className="history-warn">确认重启？引擎必须已停止；重启期间页面会短暂断开。</span>
            <button className="compact" disabled={busy !== null} onClick={restart}>
              {busy === "restart" ? "重启中…" : "确认重启"}
            </button>
            <button className="text-button" disabled={busy !== null} onClick={() => setConfirming(false)}>
              取消
            </button>
          </>
        )}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        用当前 .env 重新启动后端进程，让上面保存的设置生效。引擎运行中会被拒绝；
        由 .env 注入的旧值会被清掉，但你在 shell 里 export 的变量仍然优先。
      </small>
    </div>
  );
}

type ProviderDraft = CustomProvider & { api_key: string | null };

function CustomProvidersPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [payload, setPayload] = useState<CustomProvidersPayload | null>(null);
  const [drafts, setDrafts] = useState<ProviderDraft[] | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [revealedKeys, setRevealedKeys] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const next = await api<CustomProvidersPayload>("/api/custom-providers");
      setPayload(next);
      setDrafts(null);
      setRevealedKeys({});
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [setError]);

  useEffect(() => { load(); }, [load]);

  // api_key null means "leave the stored key alone" — the console never holds it.
  const rows: ProviderDraft[] =
    drafts ?? (payload?.providers ?? []).map((p) => ({ ...p, api_key: null }));
  const dirty = drafts !== null;

  const update = (index: number, patch: Partial<ProviderDraft>) =>
    setDrafts(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  const revealKey = useCallback(async (providerId: string) => {
    setBusy(`reveal-key-${providerId}`);
    setError(null);
    try {
      const result = await api<{ api_key: string }>(
        `/api/custom-providers/${encodeURIComponent(providerId)}/api-key`,
      );
      setRevealedKeys((current) => ({ ...current, [providerId]: result.api_key }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [setBusy, setError]);

  const hideKey = (providerId: string) => setRevealedKeys((current) => {
    const next = { ...current };
    delete next[providerId];
    return next;
  });

  const save = useCallback(async () => {
    setBusy("custom-providers");
    setError(null);
    setSaved(null);
    try {
      const next = await api<CustomProvidersPayload>("/api/custom-providers", {
        method: "POST",
        body: JSON.stringify({
          providers: rows.map((row) => ({
            id: row.id.trim(),
            base_url: row.base_url.trim(),
            model: row.model.trim() || null,
            reasoning_effort: row.reasoning_effort.trim() || null,
            wire_api: row.wire_api,
            pricing: row.pricing.trim() || null,
            require_api_key: row.require_api_key,
            ...(row.api_key === null ? {} : { api_key: row.api_key }),
          })),
        }),
      });
      setPayload(next);
      setDrafts(null);
      setRevealedKeys({});
      setSaved(`已保存 ${next.providers.length} 个端点，重启后生效`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [rows, setBusy, setError]);

  if (!payload) return null;
  const full = rows.length >= payload.max_providers;

  return (
    <div className="settings-section">
      <h4 className="account-subhead">Custom API 端点（{rows.length}/{payload.max_providers}）</h4>
      <datalist id="models-dev-providers">
        {payload.pricing_options.map((option) => <option key={option} value={option} />)}
      </datalist>
      {!rows.length && <div className="empty cards">还没有自定义端点。点「新增端点」接入任意 OpenAI 兼容服务。</div>}
      {rows.map((row, index) => (
        <div className="endpoint-card" key={index}>
          <div className="endpoint-grid">
            <label><span>ID</span>
              <input value={row.id} placeholder="main" disabled={busy !== null}
                onChange={(e) => update(index, { id: e.target.value })} />
            </label>
            <label className="endpoint-wide"><span>Base URL</span>
              <input value={row.base_url} placeholder="https://api.example/v1" disabled={busy !== null}
                onChange={(e) => update(index, { base_url: e.target.value })} />
            </label>
            <label><span>模型</span>
              <input value={row.model} placeholder="gpt-4o" disabled={busy !== null}
                onChange={(e) => update(index, { model: e.target.value })} />
            </label>
            <label><span>API Key</span>
              <input
                type={row.api_key === null && revealedKeys[row.id] !== undefined ? "text" : "password"}
                value={row.api_key ?? revealedKeys[row.id] ?? ""}
                readOnly={row.api_key === null && revealedKeys[row.id] !== undefined}
                placeholder={row.api_key_configured ? `已配置（${row.api_key_masked}）· 留空不变` : "未配置"}
                disabled={busy !== null}
                onChange={(e) => update(index, { api_key: e.target.value })}
              />
            </label>
            <label><span>协议</span>
              <select value={row.wire_api} disabled={busy !== null}
                onChange={(e) => update(index, { wire_api: e.target.value })}>
                {payload.wire_apis.map((w) => <option key={w} value={w}>{w}</option>)}
              </select>
            </label>
            <label data-tooltip="models.dev 的厂商 ID，决定按谁的价折算等效成本。同一模型常被多家转售且价格不同，无法从模型名或地址推断，只能指定。留空则成本显示「—」，且预算自动停止对该端点不生效。">
              <span>计费厂商</span>
              <input
                list="models-dev-providers"
                value={row.pricing}
                placeholder="models.dev 厂商 ID，如 xai · 留空不计成本"
                disabled={busy !== null}
                onChange={(e) => update(index, { pricing: e.target.value })}
              />
            </label>
            <label><span>推理强度</span>
              <select value={row.reasoning_effort} disabled={busy !== null}
                onChange={(e) => update(index, { reasoning_effort: e.target.value })}>
                {["", "low", "medium", "high", "xhigh"].map((o) => (
                  <option key={o} value={o}>{o || "（默认）"}</option>
                ))}
              </select>
            </label>
            <label className="endpoint-check">
              <input type="checkbox" checked={row.require_api_key} disabled={busy !== null}
                onChange={(e) => update(index, { require_api_key: e.target.checked })} />
              <span>需要 API Key</span>
            </label>
            <div className="endpoint-actions">
              {row.api_key_configured && row.api_key === null && (
                revealedKeys[row.id] !== undefined
                  ? <button className="text-button" disabled={busy !== null}
                    onClick={() => hideKey(row.id)}>隐藏密钥</button>
                  : <button className="text-button" disabled={busy !== null}
                    onClick={() => void revealKey(row.id)}>
                    {busy === `reveal-key-${row.id}` ? "读取中…" : "显示密钥"}
                  </button>
              )}
              {row.api_key_configured && row.api_key === null && (
                <button className="text-button" disabled={busy !== null}
                  onClick={() => { hideKey(row.id); update(index, { api_key: "" }); }}>清除密钥</button>
              )}
              {row.api_key !== null && (
                <button className="text-button" disabled={busy !== null}
                  onClick={() => update(index, { api_key: null })}>取消改密钥</button>
              )}
              <button className="text-button danger-text" disabled={busy !== null}
                onClick={() => setDrafts(rows.filter((_, i) => i !== index))}>删除端点</button>
            </div>
          </div>
          {row.extra_header_names.length > 0 && (
            <small className="settings-hint">
              自定义请求头（保留不变）：{row.extra_header_names.join("、")}
            </small>
          )}
        </div>
      ))}
      <div className="settings-actions">
        <button
          className="compact"
          disabled={busy !== null || full}
          title={full ? `最多 ${payload.max_providers} 个` : ""}
          onClick={() => setDrafts([...rows, {
            id: "", base_url: "", model: "", reasoning_effort: "", wire_api: "chat-completions", pricing: "",
            require_api_key: true, extra_header_names: [], api_key_configured: false,
            api_key_masked: "", api_key: "",
          }])}
        >新增端点</button>
        <button className="compact" disabled={busy !== null || !dirty} onClick={save}>
          {busy === "custom-providers" ? "保存中…" : "保存端点"}
        </button>
        <button className="text-button" disabled={busy !== null || !dirty}
          onClick={() => { setDrafts(null); setSaved(null); }}>放弃改动</button>
        {saved && <span className="settings-saved">{saved}</span>}
      </div>
    </div>
  );
}

function SettingsPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [payload, setPayload] = useState<SettingsPayload | null>(null);
  // Only edited keys are tracked, so an untouched secret is never written back
  // as its own mask.
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPayload(await api<SettingsPayload>("/api/settings"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [setError]);

  useEffect(() => { load(); }, [load]);

  const dirty = Object.keys(draft).length;

  const save = useCallback(async () => {
    setBusy("settings");
    setError(null);
    setSaved(null);
    try {
      const next = await api<SettingsPayload>("/api/settings", {
        method: "POST",
        body: JSON.stringify({ values: draft }),
      });
      setPayload(next);
      setDraft({});
      setSaved(`已保存 ${dirty} 项到 ${next.path}，重启后生效`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [draft, dirty, setBusy, setError]);

  if (!payload) return <article className="panel settings-panel"><PanelTitle code="09" title="设置" meta="编辑本地 .env" /><div className="empty cards">读取中…</div></article>;

  const shown = (field: SettingsField) =>
    draft[field.key] ?? (field.secret ? "" : field.value ?? "");

  return (
    <article className="panel settings-panel">
      <PanelTitle code="09" title="设置" meta="写入本地 .env · 重启后生效" />
      <p className="settings-note">
        保存只写入 <code>{payload.path}</code>，<strong>不会改变正在运行的进程</strong>；重启后生效。
        密钥只写不读：现有值仅显示掩码尾号，留空表示保持不变。shell 里 export 的同名变量在运行时优先级更高。
      </p>
      <RestartPanel busy={busy} setBusy={setBusy} setError={setError} />
      <CustomProvidersPanel busy={busy} setBusy={setBusy} setError={setError} />
      {payload.sections.map((section) => (
        <div className="settings-section" key={section.title}>
          <h4 className="account-subhead">{section.title}</h4>
          {section.fields.map((field) => (
            <div className="settings-row" key={field.key}>
              <span className="settings-label">
                <strong>{field.label}</strong>
                <small>{field.key}</small>
              </span>
              <div className="settings-input">
                {field.kind === "enum" ? (
                  <select
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  >
                    {(field.options.includes("") ? field.options : ["", ...field.options]).map((option) => (
                      <option key={option} value={option}>{option || "（默认）"}</option>
                    ))}
                  </select>
                ) : field.kind === "bool" ? (
                  <select
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  >
                    <option value="">（默认）</option>
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                ) : field.kind === "json" ? (
                  <textarea
                    rows={3}
                    placeholder={field.secret && field.configured ? `已配置（${field.masked}）· 留空保持不变` : field.placeholder}
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  />
                ) : (
                  <input
                    type={field.secret ? "password" : field.kind === "int" || field.kind === "number" ? "number" : "text"}
                    placeholder={field.secret && field.configured ? `已配置（${field.masked}）· 留空保持不变` : field.placeholder}
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  />
                )}
                {field.description && <small className="settings-hint">{field.description}</small>}
              </div>
              <span className={`settings-state ${field.configured ? "on" : ""}`}>
                {field.secret
                  ? field.configured ? `已配置 ${field.masked}` : "未配置"
                  : field.configured ? "已设置" : "默认"}
              </span>
            </div>
          ))}
        </div>
      ))}
      <div className="settings-actions">
        <button className="compact" disabled={busy !== null || !dirty} onClick={save}>
          {busy === "settings" ? "保存中…" : dirty ? `保存 ${dirty} 项改动` : "无改动"}
        </button>
        <button className="text-button" disabled={busy !== null || !dirty} onClick={() => { setDraft({}); setSaved(null); }}>放弃改动</button>
        {saved && <span className="settings-saved">{saved}</span>}
      </div>
    </article>
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

function backtestElapsed(run: BacktestRun): string {
  const end = run.ended_at ? new Date(run.ended_at).getTime() : Date.now();
  const start = new Date(run.created_at).getTime();
  return formatDuration(Math.max(0, Math.floor((end - start) / 1000)));
}

function formatAverageDecision(milliseconds: number | undefined): string {
  if (!milliseconds) return "—";
  const seconds = milliseconds / 1000;
  return seconds < 60 ? `${seconds.toFixed(2)}s` : formatDuration(Math.round(seconds));
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

const BACKTEST_VS_LIVE: Array<{ aspect: string; live: string; real: string; plain: string }> = [
  {
    aspect: "下单",
    live: "真实签名下单到币安测试网，交易所撮合、交易所侧括号单",
    real: "本地仿真：下一根 K 线开盘价成交 + 滑点，不发任何订单",
    plain: "同左",
  },
  {
    aspect: "订单流",
    live: "20 档盘口失衡、成交流水失衡、基差、持仓量",
    real: "全部在场——采集器当时录下来的",
    plain: "全部缺失。币安不提供历史盘口，无法重建。Prompt 已告知模型，不因缺流而否决形态",
  },
  {
    aspect: "价差",
    live: "真实买一卖一",
    real: "采集时的真实买一卖一",
    plain: "无盘口即无价差（bid = ask = mark）。编一个价差会美化每笔成交",
  },
  {
    aspect: "标的",
    live: "全市场动态扫描，每分钟轮换",
    real: "你指定标的池，且必须是采集器录过的",
    plain: "你指定标的池——历史上的价差/24h ticker 快照不存在，选币无法忠实重放",
  },
  {
    aspect: "K 线特征",
    live: "5m/15m/30m 全套 + 日线结构位",
    real: "同一套 FeaturePipeline，同构",
    plain: "同左",
  },
  {
    aspect: "风控",
    live: "AggressiveRiskPolicy",
    real: "同一个——日亏熔断、仓位上限、tick 对齐全部生效",
    plain: "同左",
  },
];

function CollectorPanel({ status, onChange }: { status: CollectorStatus | null; onChange: () => void }) {
  const [symbols, setSymbols] = useState("BTCUSDT");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (path: string, body?: object) => {
    setBusy(true); setError(null);
    try {
      await api(path, { method: "POST", ...(body ? { body: JSON.stringify(body) } : {}) });
      onChange();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(false); }
  };

  if (!status) return null;
  return (
    <div className="collector">
      <div className="collector-head">
        <span className="eyebrow">盘口采集</span>
        <span className={`dot ${status.running ? "online" : ""}`} />
        <strong>{status.running ? `采集中 · ${status.symbols.join(" ")}` : "未运行"}</strong>
        <small>
          币安不提供历史盘口，所以订单流只能在它发生时录下来。每 {status.interval_seconds / 60} 分钟采一次
          （覆盖 5m/15m/30m 的全部决策时刻）。不调模型、不下单。
        </small>
      </div>
      <div className="collector-actions">
        <input
          value={symbols}
          placeholder={`逗号分隔，最多 ${status.max_symbols} 个`}
          disabled={busy || status.running}
          onChange={(e) => setSymbols(e.target.value)}
        />
        {status.running
          ? <button className="ghost" disabled={busy} onClick={() => void act("/api/collector/stop")}>停止采集</button>
          : <button disabled={busy} onClick={() => void act("/api/collector/start", {
              symbols: symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
            })}>开始采集</button>}
        {status.error_count > 0 && <small className="negative">{status.error_count} 次采集失败</small>}
      </div>
      {error && <div className="error-text">{error}</div>}
      {status.recorded.length > 0 && (
        <div className="collector-recorded">
          {status.recorded.map((item) => (
            <span key={item.symbol}>
              <strong>{item.symbol.replace("USDT", "")}</strong>
              {item.capture_count} 条
              <small>{formatLocalDateTime(new Date(item.first_capture_at))} 起</small>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function BacktestPanel({ providers, engineRunning }: { providers: ProviderHealth[]; engineRunning: boolean }) {
  const [form, setForm] = useState(() => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    return {
      symbols: "BTCUSDT",
      cadences: ["5m"] as string[],
      start: formatLocalDateTime(yesterday),
      end: formatLocalDateTime(today),
      providers: [] as string[],
      initialEquity: "10000",
      feeRate: "0.0005",
      slippage: "0.0005",
    };
  });
  const [estimate, setEstimate] = useState<BacktestEstimate | null>(null);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [collector, setCollector] = useState<CollectorStatus | null>(null);
  const [useRecordedBook, setUseRecordedBook] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [probe, setProbe] = useState<ProbeStatus | null>(null);
  const [timeout, setTimeoutSeconds] = useState("");
  const [openDecisions, setOpenDecisions] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<BacktestDecision[] | null>(null);
  const [detailResult, setDetailResult] = useState<BacktestResult | null>(null);
  const localTimeZone = useMemo(() => localTimeZoneLabel(), []);

  const body = useCallback(() => ({
    symbols: form.symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
    cadences: form.cadences,
    start: parseLocalDateTime(form.start).toISOString(),
    end: parseLocalDateTime(form.end).toISOString(),
    providers: form.providers,
    use_recorded_book: useRecordedBook,
    ...(timeout.trim() ? { timeout_seconds: Number(timeout) } : {}),
    config: {
      initial_equity: form.initialEquity,
      fee_rate: form.feeRate,
      slippage_fraction: form.slippage,
    },
  }), [form, useRecordedBook, timeout]);

  const refreshCollector = useCallback(async () => {
    try {
      setCollector(await api<CollectorStatus>("/api/collector"));
    } catch { /* the collector panel is not worth an error banner */ }
  }, []);

  useEffect(() => { void refreshCollector(); }, [refreshCollector]);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await api<BacktestRun[]>("/api/backtests?limit=10"));
    } catch { /* the list is not worth an error banner */ }
  }, []);

  useEffect(() => { void refreshRuns(); }, [refreshRuns]);

  // Poll only while something is unfinished, so an idle console stays quiet.
  useEffect(() => {
    if (!runs.some((run) => run.status === "running")) return;
    const timer = window.setInterval(() => void refreshRuns(), 3000);
    return () => window.clearInterval(timer);
  }, [runs, refreshRuns]);

  // The estimate is stale the moment the spec changes; showing an old one
  // beside a new window is worse than showing none.
  useEffect(() => { setEstimate(null); }, [form]);

  const refreshProbe = useCallback(async () => {
    try {
      setProbe(await api<ProbeStatus>("/api/backtests/probe"));
    } catch { /* the probe panel is not worth an error banner */ }
  }, []);

  useEffect(() => { void refreshProbe(); }, [refreshProbe]);

  // Poll only while the probe is in flight. Fast, because the elapsed counter
  // is the only thing that moves while an endpoint thinks -- and whether it is
  // moving is the entire question a waiting user has.
  useEffect(() => {
    if (!probe?.running) return;
    const timer = window.setInterval(() => void refreshProbe(), 1000);
    return () => window.clearInterval(timer);
  }, [probe, refreshProbe]);

  const cancelProbe = async () => {
    try {
      await api("/api/backtests/probe/cancel", { method: "POST" });
      await refreshProbe();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const startProbe = async () => {
    setBusy("probe"); setError(null);
    try {
      await api("/api/backtests/probe", { method: "POST", body: JSON.stringify(body()) });
      await refreshProbe();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(null); }
  };

  const runEstimate = async () => {
    setBusy("estimate"); setError(null);
    try {
      setEstimate(await api<BacktestEstimate>("/api/backtests/estimate", {
        method: "POST", body: JSON.stringify(body()),
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(null); }
  };

  const start = async () => {
    setBusy("start"); setError(null);
    try {
      await api<{ id: number }>("/api/backtests", { method: "POST", body: JSON.stringify(body()) });
      await refreshRuns();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(null); }
  };

  const toggleDecisions = async (runId: number, provider: string) => {
    const key = `${runId}-${provider}`;
    if (openDecisions === key) {
      setOpenDecisions(null);
      setDetailResult(null);
      return;
    }
    // Clear first: showing the previous model's decisions under a new header
    // while the fetch lands is worse than showing nothing.
    setOpenDecisions(key);
    setDecisions(null);
    setDetailResult(null);
    try {
      const [loadedDecisions, detailedRun] = await Promise.all([
        api<BacktestDecision[]>(
          `/api/backtests/${runId}/decisions?provider=${encodeURIComponent(provider)}`,
        ),
        api<BacktestRun>(`/api/backtests/${runId}`),
      ]);
      setDecisions(loadedDecisions);
      setDetailResult(
        detailedRun.models.find((model) => model.provider === provider)?.result ?? null,
      );
    } catch (reason) {
      setDecisions([]);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  // Keep an expanded running model current. A row becomes visible only after
  // its LLM call has completed and the server has persisted the whole decision.
  useEffect(() => {
    if (!openDecisions) return;
    const active = runs.find((run) =>
      run.models.some((model) => `${run.id}-${model.provider}` === openDecisions),
    );
    if (!active) return;
    const provider = active.models.find(
      (model) => `${active.id}-${model.provider}` === openDecisions,
    )?.provider;
    if (!provider) return;
    const refresh = async () => {
      try {
        const [loadedDecisions, detailedRun] = await Promise.all([
          api<BacktestDecision[]>(
            `/api/backtests/${active.id}/decisions?provider=${encodeURIComponent(provider)}`,
          ),
          api<BacktestRun>(`/api/backtests/${active.id}`),
        ]);
        setDecisions(loadedDecisions);
        setDetailResult(
          detailedRun.models.find((model) => model.provider === provider)?.result ?? null,
        );
      } catch { /* progress polling will surface terminal run errors */ }
    };
    void refresh();
    if (active.status !== "running") return;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [openDecisions, runs]);

  const cancel = async (id: number) => {
    try {
      await api(`/api/backtests/${id}/cancel`, { method: "POST" });
      await refreshRuns();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const toggle = (key: "cadences" | "providers", value: string) =>
    setForm((current) => ({
      ...current,
      [key]: current[key].includes(value)
        ? current[key].filter((item) => item !== value)
        : [...current[key], value],
    }));

  return (
    <article className="panel backtest-panel">
      <PanelTitle code="09" title="回测" meta="历史模式 · 多模型对比" />

      <CollectorPanel status={collector} onChange={() => void refreshCollector()} />

      <div className="backtest-note">
        <strong>回测不下单。</strong>它用历史行情重放同一套决策与风控，只有撮合是仿真的。
        <button className="text-button" onClick={() => setShowDiff((value) => !value)}>
          {showDiff ? "收起差异" : "与实盘的差异"}
        </button>
      </div>
      {showDiff && (
        <div className="table-wrap backtest-diff">
          <table>
            <thead><tr><th></th><th>实盘（测试网）</th><th>真实回测</th><th>普通回测</th></tr></thead>
            <tbody>
              {BACKTEST_VS_LIVE.map((row) => (
                <tr key={row.aspect}>
                  <td><strong>{row.aspect}</strong></td>
                  <td>{row.live}</td>
                  <td>{row.real}</td>
                  <td>{row.plain}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <section className="backtest-setup">
        <div className="backtest-section-head">
          <div><span>01</span><strong>回测参数</strong></div>
          <small>YYYY/MM/DD HH:mm · 本地时区 <strong>{localTimeZone}</strong></small>
        </div>
        <div className="backtest-form">
        <label><span>标的（逗号分隔，最多 5 个）</span>
          <input value={form.symbols} disabled={busy !== null}
            onChange={(e) => setForm({ ...form, symbols: e.target.value })} />
        </label>
        <label><span>起（本地时间）</span>
          <input type="text" value={form.start} placeholder="YYYY/MM/DD HH:mm"
            inputMode="numeric" disabled={busy !== null}
            onChange={(e) => setForm({ ...form, start: e.target.value })} />
        </label>
        <label><span>止（本地时间 · 最长 3 天）</span>
          <input type="text" value={form.end} placeholder="YYYY/MM/DD HH:mm"
            inputMode="numeric" disabled={busy !== null}
            onChange={(e) => setForm({ ...form, end: e.target.value })} />
        </label>
        <label><span>初始权益</span>
          <input value={form.initialEquity} disabled={busy !== null}
            onChange={(e) => setForm({ ...form, initialEquity: e.target.value })} />
        </label>
        </div>

        <div className="backtest-picks">
        <div>
          <span className="eyebrow">周期（每多选一个，耗时增加一份）</span>
          <div className="chips">
            {["5m", "15m", "30m"].map((cadence) => (
              <button key={cadence} className={form.cadences.includes(cadence) ? "active" : ""}
                disabled={busy !== null} onClick={() => toggle("cadences", cadence)}>{cadence}</button>
            ))}
          </div>
        </div>
        <div>
          <span className="eyebrow">对比的模型（最多 4 个 · 并行跑，不叠加耗时）</span>
          <div className="chips">
            {providers.map((provider) => (
              <button key={provider.provider} className={form.providers.includes(provider.provider) ? "active" : ""}
                disabled={busy !== null || !provider.available}
                onClick={() => toggle("providers", provider.provider)}>
                <span>{providerLabel(provider.provider)}</span>
                <small>{modelConfigSummary(provider.model, provider.reasoning_effort)}</small>
              </button>
            ))}
          </div>
        </div>
        </div>
      </section>

      <div className="backtest-preflight-grid">
        <label className="backtest-real">
          <input type="checkbox" checked={useRecordedBook} disabled={busy !== null}
            onChange={(e) => setUseRecordedBook(e.target.checked)} />
          <span>真实回测</span>
          <small>
            用采集器录下的盘口，payload 与实盘完全同构。要求窗口内<strong>每个</strong>决策时刻都有记录——
            覆盖不全会被拒绝并告诉你缺多少，因为一半决策有订单流、一半没有，
            等于把两个策略平均成一个不提及此事的数字。
          </small>
        </label>

        <div className="probe">
        <div className="probe-head">
          <strong>试跑 {probe?.decisions ?? 3} 次决策</strong>
          <button
            className="compact"
            disabled={busy !== null || !form.providers.length || engineRunning || probe?.running}
            onClick={() => void startProbe()}
          >{probe?.running ? "试跑中…" : "开始试跑"}</button>
          {probe?.running && (
            <button className="text-button danger-text" onClick={() => void cancelProbe()}>
              停止试跑
            </button>
          )}
          <small>
            用这个窗口的真实 payload 调每个模型 {probe?.decisions ?? 3} 次，量出它实际要多久。
            试跑期间超时放宽到 {probe?.ceiling_seconds ?? 180}s——用当前超时去试只会复现超时，
            量不出模型真正需要的时间。这几次是真实调用，会真实计费。
          </small>
        </div>
        {probe?.providers.map((item) => (
          <div className="probe-row" key={item.provider}>
            <span className="probe-name">{providerLabel(item.provider)}</span>
            <span className="probe-calls">
              {/* One slot per call: landed, in flight, or still queued. A row
                  that only appears when it is done cannot be told from a hang. */}
              {Array.from({ length: probe.decisions }, (_, index) => {
                const call = item.calls[index];
                if (call) {
                  return <b key={index} className={call.ok ? "" : "negative"} title={call.error ?? ""}>
                    {call.ok ? `${call.seconds}s` : "失败"}
                  </b>;
                }
                if (index === item.calls.length && item.in_flight_seconds !== null) {
                  return <b key={index} className="probe-waiting">
                    {item.in_flight_seconds}s…
                  </b>;
                }
                return <b key={index} className="probe-queued">·</b>;
              })}
            </span>
            {item.error && <span className="negative">{item.error}</span>}
            {!item.done && !item.error && (
              <small className="probe-progress">
                第 {Math.min(item.calls.length + 1, probe.decisions)}/{probe.decisions} 次
                {item.in_flight_seconds !== null
                  ? `已等 ${item.in_flight_seconds}s（上限 ${probe.ceiling_seconds}s）`
                  : "准备中"}
              </small>
            )}
            {item.done && !item.error && item.suggested_timeout_seconds !== null && (
              <button
                className="text-button"
                onClick={() => setTimeoutSeconds(String(item.suggested_timeout_seconds))}
              >建议 {item.suggested_timeout_seconds}s · 点击采用</button>
            )}
            {/* Only when every call actually ran and failed. A probe that was
                cut short has no calls to have failed, and saying they did would
                blame the endpoint for something the user did. */}
            {item.done && !item.error && item.failures === probe.decisions && (
              <span className="negative">
                {probe.decisions} 次全部失败——这个端点跑不动这次回测，调大超时也没用
              </span>
            )}
          </div>
        ))}
        <label className="probe-timeout">
          <span>本次回测超时（秒）</span>
          <input
            type="number" min={1} placeholder="留空=用设置里的默认值"
            value={timeout} disabled={busy !== null}
            onChange={(event) => setTimeoutSeconds(event.target.value)}
          />
        </label>
        </div>
      </div>

      {estimate && (
        <div className={`backtest-estimate ${estimate.within_limit ? "" : "over"}`}>
          <span>每模型 <strong>{estimate.decisions_per_model}</strong> 次决策</span>
          <span>共 <strong>{estimate.total_calls}</strong> 次模型调用</span>
          <span>预计 <strong>{estimate.estimated_hours}</strong> 小时
            <small>按本机实测延迟 · 上限 {estimate.max_hours}h</small></span>
          {!estimate.within_limit && <span className="negative">超出耗时上限，请缩短窗口</span>}
        </div>
      )}

      <div className="backtest-actions">
        <button className="ghost" disabled={busy !== null || !form.providers.length} onClick={() => void runEstimate()}>
          {busy === "estimate" ? "估算中…" : "先估算耗时"}
        </button>
        <button className="primary" disabled={busy !== null || !form.providers.length || engineRunning} onClick={() => void start()}>
          {busy === "start" ? "启动中…" : "开始回测"}
        </button>
        {engineRunning && <small className="backtest-blocked">引擎运行中无法回测——两者共用同一个模型且调用严格串行，并发会让实盘快照超时被误判否决。请先停止引擎。</small>}
      </div>
      {error && <div className="error-text">{error}</div>}

      <div className="backtest-results-head">
        <div><span>02</span><strong>运行记录</strong></div>
        <small>最近 {runs.length} 次 · 点击模型展开收益与决策明细</small>
      </div>
      <div className="table-wrap backtest-runs">
        <table>
          <thead><tr><th>#</th><th>窗口</th><th>模型</th><th>进度</th>
            <th data-tooltip="该模型已成功返回的决策调用平均耗时；不含历史行情读取、撮合和数据库写入。">平均决策</th>
            <th data-tooltip="已完成模型调用返回的总 Token；运行中随 3 秒轮询更新。">Token</th>
            <th data-tooltip="按 Provider 返回成本或所选计费厂商价格折算；有任一调用无法定价时显示未知。">成本</th>
            <th>收益</th><th>胜率</th><th>回撤</th><th>交易</th><th></th></tr></thead>
          <tbody>
            {runs.flatMap((run) => run.models.map((model, index) => (
              <tr key={`${run.id}-${model.provider}`}>
                {index === 0 && <td rowSpan={run.models.length}><strong>{run.id}</strong><small className={`run-status ${run.status}`}>{RUN_STATUS[run.status]}</small><small data-tooltip="任务从创建到结束的墙钟耗时；运行中随列表轮询继续计时。">耗时 {backtestElapsed(run)}</small></td>}
                {index === 0 && <td rowSpan={run.models.length}>
                  <small className="run-window">
                    <span>{run.spec.symbols.join(" ")}</span>
                    <span>{run.spec.cadences.join(" ")}</span>
                    <span><b>开始</b>{formatLocalDateTime(new Date(run.spec.start))}</span>
                    <span><b>结束</b>{formatLocalDateTime(new Date(run.spec.end))}</span>
                  </small>
                  {run.spec.use_recorded_book
                    ? <small className="run-real">真实回测 · 含订单流</small>
                    : <small>普通回测 · 无订单流</small>}
                  {run.spec.timeout_seconds
                    ? <small>超时 {run.spec.timeout_seconds}s</small>
                    : null}
                </td>}
                <td>
                  <button
                    className="run-expand"
                    onClick={() => void toggleDecisions(run.id, model.provider)}
                    title="展开收益构成、收尾处理与逐条决策"
                  >
                    {openDecisions === `${run.id}-${model.provider}` ? "▾" : "▸"}
                    <span>
                      {providerLabel(model.provider)}
                      <small>{modelConfigSummary(model.model, model.reasoning_effort, model.config_recorded)}</small>
                    </span>
                  </button>
                </td>
                <td>{Math.round(model.progress * 100)}%
                  {model.calls_failed > 0 && <small className="negative">{model.calls_failed} 次失败</small>}</td>
                <td>{formatAverageDecision(model.usage?.average_duration_ms)}</td>
                <td>
                  {(model.usage?.total_tokens ?? 0).toLocaleString("zh-CN")}
                </td>
                <td>
                  {model.usage?.equivalent_cost_usd === null || model.usage?.equivalent_cost_usd === undefined
                    ? "—"
                    : `$${model.usage.equivalent_cost_usd.toFixed(6)}`}
                </td>
                <td className={model.result && Number(model.result.total_return) >= 0 ? "positive" : "negative"}>
                  {model.result ? `${(Number(model.result.total_return) * 100).toFixed(2)}%` : "—"}</td>
                <td>{model.result ? `${(Number(model.result.win_rate) * 100).toFixed(0)}%` : "—"}</td>
                <td>{model.result ? `${(Number(model.result.max_drawdown) * 100).toFixed(2)}%` : "—"}</td>
                <td>{model.result ? model.result.trade_count : "—"}</td>
                {index === 0 && <td rowSpan={run.models.length}>
                  {run.status === "running" && <button className="text-button danger-text" onClick={() => void cancel(run.id)}>取消</button>}
                  {run.status === "unreliable" && <small className="negative" title={run.error ?? ""}>丢了太多决策</small>}
                  {run.status === "failed" && run.error && <small className="negative" title={run.error}>失败</small>}
                </td>}
              </tr>
            )).concat(
              openDecisions?.startsWith(`${run.id}-`)
                ? [
                  <tr key={`${run.id}-decisions`} className="run-decisions">
                    <td colSpan={12}>
                      <BacktestResultDetail result={detailResult} />
                      {decisions === null
                        ? <span className="empty">读取中…</span>
                        : !decisions.length
                          ? <span className="empty">这个模型还没有决策记录。</span>
                          : <table className="decision-log">
                            <thead><tr><th>时间</th><th>标的</th><th>结果</th><th>动作</th><th>置信</th><th>说明</th></tr></thead>
                            <tbody>
                              {decisions.map((item) => (
                                <tr key={item.id}>
                                  <td>{formatLocalDateTime(new Date(item.decided_at))}</td>
                                  <td>{item.symbol} · {item.cadence}</td>
                                  <td><span className={`decision-outcome ${DECISION_OUTCOME_CLASS[item.outcome]}`}>
                                    {BACKTEST_OUTCOME[item.outcome]}</span></td>
                                  <td>{item.action ?? "—"}
                                    {item.fill && <small>{item.fill.side} @ {item.fill.price} × {item.fill.quantity}</small>}</td>
                                  <td>{item.confidence !== null ? `${Math.round(item.confidence * 100)}%` : "—"}</td>
                                  <td className="decision-why">
                                    {item.detail && <small className="negative">{item.detail}</small>}
                                    {item.rationale}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>}
                    </td>
                  </tr>,
                ]
                : [],
            ))}
            {!runs.length && <tr><td colSpan={9} className="empty">还没有回测。选好标的、窗口和模型后先估算耗时。</td></tr>}
          </tbody>
        </table>
      </div>
    </article>
  );
}

// Zero trades has four different causes; these are the words that tell them
// apart, so they say what happened rather than grading it.
const BACKTEST_OUTCOME: Record<BacktestDecision["outcome"], string> = {
  traded: "已成交",
  pending: "限价挂单",
  rejected: "风控否决",
  hold: "持有",
  no_snapshot: "无快照",
  call_failed: "调用失败",
};

// Reuse the live panel's colours: approved/rejected/analysis_only already mean
// exactly these three things on the overview tab.
const DECISION_OUTCOME_CLASS: Record<BacktestDecision["outcome"], string> = {
  traded: "approved",
  pending: "analysis_only",
  rejected: "rejected",
  hold: "analysis_only",
  no_snapshot: "analysis_only",
  call_failed: "rejected",
};

const RUN_STATUS: Record<BacktestRun["status"], string> = {
  running: "进行中",
  completed: "已完成",
  unreliable: "不可信",
  failed: "失败",
  cancelled: "已取消",
};

function formatLocalDateTime(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${String(date.getFullYear()).padStart(4, "0")}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseLocalDateTime(value: string): Date {
  const match = /^(\d{4})\/(\d{2})\/(\d{2}) (\d{2}):(\d{2})$/.exec(value.trim());
  if (!match) throw new Error("日期时间格式必须为 YYYY/MM/DD HH:mm（浏览器本地时间）");
  const [, yearText, monthText, dayText, hourText, minuteText] = match;
  const [year, month, day, hour, minute] = [
    yearText, monthText, dayText, hourText, minuteText,
  ].map(Number);
  const date = new Date(year, month - 1, day, hour, minute, 0, 0);
  if (
    date.getFullYear() !== year
    || date.getMonth() !== month - 1
    || date.getDate() !== day
    || date.getHours() !== hour
    || date.getMinutes() !== minute
  ) {
    throw new Error(`无效的本地日期时间：${value}`);
  }
  return date;
}

function localTimeZoneLabel(date = new Date()): string {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "系统本地时区";
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const absolute = Math.abs(offset);
  const hours = String(Math.floor(absolute / 60)).padStart(2, "0");
  const minutes = String(absolute % 60).padStart(2, "0");
  return `${zone} · UTC${sign}${hours}:${minutes}`;
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

const DECISION_FILTERS: Array<{ key: DecisionFilter; label: string }> = [
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

function DecisionPanel({
  decisions,
  filter,
  onFilter,
  onLoadOlder,
  exhausted,
}: {
  decisions: DecisionEvent[];
  filter: DecisionFilter;
  onFilter: (next: DecisionFilter) => void;
  onLoadOlder: () => Promise<void>;
  exhausted: boolean;
}) {
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [details, setDetails] = useState<Record<number, DecisionDetail>>({});
  const [detailLoading, setDetailLoading] = useState<number | null>(null);
  const [detailErrors, setDetailErrors] = useState<Record<number, string>>({});
  const [copied, setCopied] = useState<string | null>(null);
  const visible = decisions;

  const loadOlder = async () => {
    setLoadingOlder(true);
    try {
      await onLoadOlder();
    } finally {
      setLoadingOlder(false);
    }
  };

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
            onClick={() => onFilter(item.key)}
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
      <div className="decision-more">
        {exhausted
          ? <span className="decision-more-note">已到最早一条记录。</span>
          : <button className="text-button" disabled={loadingOlder} onClick={() => void loadOlder()}>
              {loadingOlder ? "加载中…" : "加载更早"}
            </button>}
        {filter !== "all" && (
          <span className="decision-more-note" data-tooltip="实时推送的是未筛选的最新记录，混入当前列表会掺进不符合筛选条件的决策。">
            筛选中 · 实时更新已暂停
          </span>
        )}
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
  const displayedPnl = portfolio?.daily_pnl ?? null;
  return (
    <article className="panel account-panel">
      <PanelTitle
        code="06"
        title="账户与订单"
        meta="币安测试网账户 · 只读"
      />
      <div className="account-metrics">
        <Metric label="权益" value={portfolio ? money(portfolio.equity) : "—"} suffix="" />
        <Metric label="可用余额" value={portfolio ? money(portfolio.available_balance) : "—"} suffix="" />
        <div
          className="metric"
          data-tooltip={METRIC_DEFINITIONS["当日盈亏"]}
        ><span>当日盈亏</span><strong className={Number(displayedPnl ?? 0) >= 0 ? "positive" : "negative"}>{displayedPnl === null ? "—" : money(displayedPnl)}</strong></div>
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
                <td>{position.stop_loss === null && position.take_profit === null
                  ? position.protection_source === "exchange" ? "交易所侧"
                    : position.protection_source === "missing" ? "缺失"
                      : position.protection_source === "unknown" ? "待确认" : "—"
                  : <span className="protection">
                      <span>止损 <strong>{position.stop_loss === null ? "缺失" : Number(position.stop_loss).toFixed(4)}</strong></span>
                      <span>止盈 <strong>{position.take_profit === null ? "缺失" : Number(position.take_profit).toFixed(4)}</strong></span>
                    </span>}</td>
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
