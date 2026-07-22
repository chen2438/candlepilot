import { type FormEvent, type ReactNode, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type {
  AccountPortfolio,
  AccountPosition,
  ManualCloseResult,
  LiveRunPerformance,
  BacktestDecision,
  BacktestDecisionPage,
  BacktestEstimate,
  BacktestResult,
  BacktestRun,
  Candidate,
  ProbeStatus,
  ReplayableFormalRun,
  DecisionEvent,
  DecisionDetail,
  EngineStatus,
  TradeFillRecord,
  ProviderHealth,
  CodexAuthSession,
  CodexUsageSnapshot,
  ProviderTestResult,
  ProviderMetric,
  ProviderMetricsResponse,
  CustomProvider,
  CustomProvidersPayload,
  RunSessionMetrics,
  SettingsField,
  SettingsPayload,
  BackupInventory,
  LogMaintenanceStatus,
  WebUpdateStatus,
  WebUpdateCheck,
  StructureGateSummary,
  TestnetAccountStatus,
  TrailingStopEvent,
  PartialTakeProfitEvent,
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

function BacktestResultDetail({ result }: { result: BacktestResult | null }) {
  if (!result) return <div className="backtest-result-empty">正在读取收益明细；未完成的运行会在结束后生成。</div>;
  const netPnl = Number(result.net_pnl);
  const fees = Number(result.total_fees);
  const fundingCost = Number(result.total_funding);
  const grossPnl = Number(result.gross_price_pnl);
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
          按最后可用价格强制平仓 {result.run_end_trade_count} 笔（含退出滑点与手续费）
        </span>
        <span>
          撤销未成交挂单 {result.cancelled_pending_orders} 笔（不产生盈亏）
        </span>
      </div>
      {result.trades.length > 0 && (
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

const DECISION_CADENCES = ["5m", "15m", "30m", "1h", "4h"];

export function CadenceSelector({
  active,
  supported,
  disabled,
  onSelect,
}: {
  active: string;
  supported: string[];
  disabled: boolean;
  onSelect: (cadence: string) => void;
}) {
  return (
    <div className="cadence-select" title={disabled ? "运行时锁定" : "选择唯一的分析周期；每次仍会读取完整多周期特征"}>
      <span>分析周期</span>
      <div className="cadence-chips">
        {supported.map((cadence) => (
          <button
            key={cadence}
            className={`cadence-chip ${active === cadence ? "on" : ""}`}
            disabled={disabled}
            aria-pressed={active === cadence}
            onClick={() => onSelect(cadence)}
          >{cadence}</button>
        ))}
      </div>
    </div>
  );
}

export function ProviderChoiceButton({
  name,
  selected,
  disabled,
  onSelect,
  children,
}: {
  name: string;
  selected: boolean;
  disabled: boolean;
  onSelect: (name: string) => void;
  children: ReactNode;
}) {
  return <button
    className="provider-card-main"
    disabled={disabled || selected}
    aria-pressed={selected}
    onClick={() => onSelect(name)}
    title={selected ? "当前运行 Provider" : "选择为唯一运行 Provider；当前不可用也可预先配置"}
  >{children}</button>;
}

const emptyStatus: EngineStatus = {
  running: false,
  emergency_locked: false,
  emergency_locked_until: null,
  provider_chain: [],
  active_provider: null,
  live_run_id: null,
  provider_routes: [],
  active_cadences: ["15m"],
  run_limits: { max_run_seconds: null, max_run_cost_usd: null },
  risk_limits: { daily_loss_fraction: "0.05" },
  decision_timeout_seconds: null,
  startup_probe: null,
  auto_stop_reason: null,
  route_failure_count: 0,
  route_failure_limit: 3,
  rescue_count: 0,
  rescue_limit: 3,
  supported_cadences: DECISION_CADENCES,
  candidates_per_cycle: 5,
  max_candidates_per_cycle: 20,
  candidate_count: 0,
  venue_excluded_symbols: [],
  universe_refreshed_at: null,
  scheduler: {
    current_cycle: null,
    current_cycles: [],
    last_cycle: null,
    last_error: null,
    universe_last_error: null,
    guard_last_error: null,
  },
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
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event("candlepilot:unauthorized"));
    }
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

type AuthStatus = {
  enabled: boolean;
  authenticated: boolean;
  username: string | null;
  expires_at?: number | null;
};

export function LoginScreen({ onAuthenticated }: { onAuthenticated: (status: AuthStatus) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const body = await response.json().catch(() => ({ detail: response.statusText }));
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      onAuthenticated(body as AuthStatus);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-shell">
      <section className="login-card panel">
        <div className="brand login-brand">
          <span className="brand-mark"><i /><i /><i /></span>
          <div><strong>CANDLEPILOT</strong><small>SECURE OPERATOR CONSOLE</small></div>
        </div>
        <div className="login-copy">
          <span className="eyebrow">REMOTE ACCESS</span>
          <h1>登录控制台</h1>
          <p>请输入 VPS 安装时设置的管理员凭据。登录状态仅保存在受保护的浏览器 Cookie 中。</p>
        </div>
        <form className="login-form" name="candlepilot-login" method="post" action="/api/auth/login" autoComplete="on" onSubmit={submit}>
          <label htmlFor="candlepilot-username"><span>用户名</span><input id="candlepilot-username" name="username" type="text" autoComplete="username" autoCapitalize="none" spellCheck={false} value={username} onChange={(event) => setUsername(event.target.value)} required /></label>
          <label htmlFor="candlepilot-password"><span>密码</span><input id="candlepilot-password" name="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button type="submit" disabled={busy}>{busy ? "验证中…" : "登录"}</button>
        </form>
        <small className="login-security">仅通过 HTTPS 远程登录；核对安装输出的证书指纹后，再接受首次访问的证书警告。</small>
      </section>
    </main>
  );
}

const HISTORY_CATEGORIES: Array<{ key: string; label: string; hint: string }> = [
  { key: "inferences", label: "模型调用与决策", hint: "AI 分析 / 最近决策" },
  { key: "risk_decisions", label: "风控决策", hint: "风险事件" },
  { key: "executions", label: "订单与成交", hint: "" },
  { key: "user_events", label: "测试网事件", hint: "用户数据流" },
  { key: "alerts", label: "告警历史", hint: "" },
  { key: "trailing_stops", label: "移动止损记录", hint: "影子候选与实盘改单" },
  { key: "partial_take_profits", label: "部分止盈实验", hint: "1R 分批止盈与保本影子成交" },
  { key: "backtests", label: "回测记录", hint: "运行、逐模型结果与每条决策" },
  // Kept as a cleanup-only category for databases created by older versions.
  { key: "book_captures", label: "旧盘口采集数据", hint: "手动采集已停用；仅用于清理遗留记录" },
  { key: "market_cache", label: "行情缓存", hint: "Parquet" },
  { key: "pricing_cache", label: "定价缓存", hint: "models.dev" },
];

type TabKey = "overview" | "account" | "backtest" | "operations" | "data" | "settings";

const TABS: Array<{ key: TabKey; label: string; meta: string }> = [
  { key: "overview", label: "总览", meta: "引擎 · 接入 · 候选 · 决策" },
  { key: "account", label: "账户", meta: "持仓 · 订单" },
  { key: "backtest", label: "回测", meta: "历史模式 · 多模型对比" },
  { key: "operations", label: "运维", meta: "模型调用指标" },
  { key: "data", label: "数据", meta: "删除历史数据" },
  { key: "settings", label: "设置", meta: "编辑本地 .env" },
];

const METRIC_DEFINITIONS: Record<string, string> = {
  "候选标的": "最近一次全市场扫描后进入动态候选池的 USDT 永续合约数量；候选池最多保留 20 个，不等于每个周期实际送入模型的数量。",
  "最大杠杆": "硬风控允许模型请求的最高杠杆倍数；实际交易可以更低，不能由模型突破。",
  "24h亏损熔断": "滚动过去 24 小时的净亏损达到设置比例时，硬风控拒绝开仓和加仓；减仓和平仓不受影响。",
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
  "过去24h盈亏": "从当前时刻往前 24 小时的已实现盈亏、手续费和资金费，加上当前未实现盈亏；硬风控用它判断 5% 的 24h 亏损熔断。",
  "总收益": "回测结束权益相对初始权益的累计变化比例，包含模型交易产生的费用和资金费影响。",
  "最大回撤": "回测权益曲线从任一历史峰值到后续低点的最大跌幅。",
  "Sharpe": "回测周期收益的年化平均值除以样本标准差，未扣无风险利率；值越高代表单位总波动收益越高。",
  "Sortino": "回测周期收益的年化平均值除以下行偏差；只惩罚负收益波动。",
  "换手": "回测全部成交名义价值合计除以初始权益。",
};

const RISK_DEFINITIONS: Record<string, string> = {
  "候选标的": METRIC_DEFINITIONS["候选标的"],
  "最大杠杆": METRIC_DEFINITIONS["最大杠杆"],
  "24h亏损熔断": METRIC_DEFINITIONS["24h亏损熔断"],
  "单笔风险": "单次开仓或加仓在止损触发时允许承担的计划亏损上限，为当前权益的 1%，并在定量时计入手续费、盘口与保守滑点。",
  "组合止损风险": "全部未平仓头寸按当前交易所保护价计算的计划止损风险合计，不得超过当前权益的 4%；缺少可核验止损时拒绝新增风险。",
  "最低盈亏比": "开仓与加仓按入场、止损和止盈的价格距离计算原始盈亏比，必须大于 1.3:1；手续费和滑点不参与该比例，减仓和平仓不受此限制。",
  "保证金占用": "全部仓位占用保证金不得超过账户权益的 80%。",
  "单标的保证金": "每个标的的初始保证金不得超过账户权益的 10%，增仓也计入同一上限。",
  "持仓模式": "每个标的使用逐仓保证金并维持单向净仓，不同时持有双向仓位。",
};

const STRUCTURE_CHECK_LABELS: Record<string, string> = {
  metadata: "计划字段",
  anchor: "结构锚点",
  extension: "追价距离",
  alignment: "周期同向",
  trigger: "入场触发",
  invalidation: "失效与止损",
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
  if (name === "local-rule") return "本地规则";
  if (name === "codex-auth") return "Codex Auth";
  if (name === "claude-code-auth") return "Claude Code Auth";
  const id = customProviderId(name);
  if (id) return `Custom API · ${id}`;
  return name;
}

export function codexAuthSourceLabel(source: string | null | undefined): string {
  if (source === "chatgpt-app") return "ChatGPT App";
  if (source === "codex-cli") return "Codex CLI";
  return "未检测";
}

export function codexProviderIdentity(provider: Pick<
  ProviderHealth,
  "auth_source" | "account_email" | "version" | "detail"
>): string {
  return [
    codexAuthSourceLabel(provider.auth_source),
    provider.account_email,
    provider.version ?? provider.detail,
  ].filter(Boolean).join(" · ");
}

export function CodexAuthSourceSelect({
  disabled,
  onChange,
  options,
  value,
}: {
  disabled: boolean;
  onChange: (source: string) => void;
  options: string[];
  value: string;
}) {
  return <select
    aria-label="Codex 接入来源"
    value={value}
    disabled={disabled}
    onChange={(event) => onChange(event.target.value)}
  >
    {options.length === 0 && <option value="">未检测到可用来源</option>}
    {options.map((source) => (
      <option key={source} value={source}>{codexAuthSourceLabel(source)}</option>
    ))}
  </select>;
}

export function CodexCliAuthControls({
  authenticated,
  busy,
  disabled,
  onCancel,
  onLogin,
  onLogout,
  onRefreshUsage,
  session,
  usage,
  usageLoading,
}: {
  authenticated: boolean;
  busy: boolean;
  disabled: boolean;
  onCancel: () => void;
  onLogin: () => void;
  onLogout: () => void;
  onRefreshUsage: () => void;
  session: CodexAuthSession | null;
  usage: CodexUsageSnapshot | null;
  usageLoading: boolean;
}) {
  const [confirmLogout, setConfirmLogout] = useState(false);
  const active = session?.state === "starting" || session?.state === "pending";
  const message = !authenticated && session?.state === "succeeded"
    ? "登录已失效，请重新登录"
    : session?.message ?? (authenticated ? "当前登录有效" : "当前未登录");
  useEffect(() => {
    if (!authenticated) setConfirmLogout(false);
  }, [authenticated]);
  return <div className="codex-cli-auth">
    <div className="codex-cli-auth-head">
      <span>
        <strong>Codex CLI 登录</strong>
        <small>{message}</small>
      </span>
      <div>
        {active
          ? <button className="text-button" disabled={busy || disabled} onClick={onCancel}>取消登录</button>
          : authenticated
            ? confirmLogout
              ? <>
                <button className="text-button danger" disabled={busy || disabled} onClick={onLogout}>确认登出</button>
                <button className="text-button" disabled={busy} onClick={() => setConfirmLogout(false)}>保留登录</button>
              </>
              : <button className="text-button" disabled={busy || disabled} onClick={() => setConfirmLogout(true)}>登出</button>
            : <button className="text-button" disabled={busy || disabled || session?.available === false} onClick={onLogin}>
              {busy ? "处理中…" : "登录"}
            </button>}
      </div>
    </div>
    {active && <div className="codex-device-auth" role="status">
      {session?.verification_uri && <a href={session.verification_uri} target="_blank" rel="noreferrer">
        打开 Codex 授权页面
      </a>}
      {session?.user_code && <code aria-label="Codex 一次性代码">{session.user_code}</code>}
      <small>在授权页面输入一次性代码；代码仅用于本次登录。</small>
    </div>}
    {authenticated && <div className="codex-usage" aria-label="Codex 剩余额度">
      <div className="codex-usage-head">
        <span><strong>Codex 剩余额度</strong><small>{usage?.message ?? "尚未查询"}</small></span>
        <button className="text-button" disabled={usageLoading || disabled} onClick={onRefreshUsage}>
          {usageLoading ? "查询中…" : "刷新额度"}
        </button>
      </div>
      {usage?.available && usage.buckets.flatMap((bucket) => bucket.windows.map((window) => (
        <div className="codex-usage-row" key={`${bucket.limit_id ?? "default"}-${window.kind}`}>
          <span>
            <strong>{[codexPlanLabel(bucket.plan_type), codexWindowLabel(window.window_duration_minutes, bucket.limit_name)]
              .filter(Boolean).join(" · ")}</strong>
            <small>{window.resets_at
              ? `${new Date(window.resets_at).toLocaleString("zh-CN", { hour12: false })} 重置`
              : "Codex 未提供重置时间"}</small>
          </span>
          <b>{window.remaining_percent}%</b>
        </div>
      ))) }
    </div>}
  </div>;
}

function codexPlanLabel(plan: string | null): string | null {
  if (!plan) return null;
  return ({ team: "Team", business: "Business", pro: "Pro", plus: "Plus", free: "Free" } as Record<string, string>)[plan]
    ?? plan;
}

export function codexWindowLabel(minutes: number | null, name: string | null): string {
  if (minutes !== null && minutes >= 28 * 1440 && minutes <= 31 * 1440) return "月额度";
  if (minutes === 7 * 1440) return "周额度";
  if (minutes === 1440) return "日额度";
  if (minutes !== null && minutes % 1440 === 0) return `${minutes / 1440} 天额度`;
  if (minutes !== null && minutes % 60 === 0) return `${minutes / 60} 小时额度`;
  if (minutes !== null) return `${minutes} 分钟额度`;
  return name?.trim() || "额度窗口";
}

function modelConfigSummary(model: string | null, effort: string | null): string {
  return `${model ?? "Provider 默认模型"} · ${effort ? `推理 ${effort}` : "默认推理强度"}`;
}

function providerIcon(name: string): string {
  if (name === "local-rule") return "FX";
  if (name === "codex-auth") return "CX";
  if (name === "claude-code-auth") return "CC";
  if (customProviderId(name)) return "API";
  return "AI";
}

function inferenceConfigLabel(decision: DecisionEvent): string {
  const model = decision.model ?? "默认模型";
  const provenance = decision.provenance;
  if (!Object.prototype.hasOwnProperty.call(provenance, "reasoning_effort")) {
    return `${model} · 推理强度未记录`;
  }
  return `${model} · ${provenance.reasoning_effort || "默认推理强度"}`;
}

function percent(value: string): string {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

const DECISION_RUN_PAGE_SIZE = 10;

type DecisionFilter = "all" | DecisionEvent["outcome"];

export function decisionQueryUrl(filter: DecisionFilter, beforeRunId?: number): string {
  const params = new URLSearchParams({ run_limit: String(DECISION_RUN_PAGE_SIZE) });
  // Filtering happens server-side over the whole table. Filtering the loaded
  // page in the browser instead would answer "show me every rejection" with
  // only the rejections that happen to be in the newest 10 runs.
  if (filter !== "all") params.set("outcome", filter);
  if (beforeRunId !== undefined) params.set("before_run_id", String(beforeRunId));
  return `/api/decision-events?${params}`;
}

function decisionRunCount(events: DecisionEvent[]): number {
  return new Set(events.map((event) => event.live_run_id).filter((runId) => runId !== null)).size;
}

export function LiveRunActionButtons({
  busy,
  running,
  emergencyLocked,
  probeReady,
  onProbe,
  onProbeAndStart,
  onRunOnce,
  onStart,
  onStop,
  onEmergencyStop,
}: {
  busy: string | null;
  running: boolean;
  emergencyLocked: boolean;
  probeReady: boolean;
  onProbe: () => void;
  onProbeAndStart: () => void;
  onRunOnce: () => void;
  onStart: () => void;
  onStop: () => void;
  onEmergencyStop: () => void;
}) {
  return <>
    <button
      disabled={busy !== null || running || emergencyLocked}
      onClick={onProbe}
    >{busy === "probe" ? "真实批量试跑…" : "试跑"}</button>
    <button
      className="primary"
      disabled={busy !== null || running || emergencyLocked}
      onClick={onProbeAndStart}
    >{busy === "probe-and-start" ? "试跑并启动中…" : "试跑并启动"}</button>
    <button
      disabled={busy !== null || running || emergencyLocked}
      onClick={onRunOnce}
    >{busy === "run-once" ? "运行一次中…" : "运行一次"}</button>
    <button
      className="primary"
      disabled={busy !== null || running || emergencyLocked || !probeReady}
      onClick={onStart}
    >{busy === "start" ? "启动中…" : "启动"}</button>
    <button disabled={busy !== null || !running} onClick={onStop}>优雅停止</button>
    <button
      className="danger"
      disabled={busy !== null && busy !== "run-once"}
      onClick={onEmergencyStop}
    >紧急熔断</button>
  </>;
}

export function EmergencyLockBanner({
  lockedUntil,
  busy,
  onClear,
}: {
  lockedUntil: string | null;
  busy: boolean;
  onClear: () => void;
}) {
  return <div className="lock-banner">
    <span>
      紧急锁定已生效
      {lockedUntil
        ? `，自动解锁时间：${new Date(lockedUntil).toLocaleString("zh-CN", { hour12: false })}`
        : ""}
      。解除前会检查测试网账户无持仓且无挂单。
    </span>
    <button disabled={busy} onClick={onClear}>
      {busy ? "安全检查中…" : "检查并解除锁定"}
    </button>
  </div>;
}

export function LiveCycleStatus({
  cycle,
}: {
  cycle: EngineStatus["scheduler"]["current_cycles"][number];
}) {
  let detail: string;
  if (cycle.stage === "preparing") {
    detail = `${cycle.total} 个标的 · 准备合约规则`;
  } else if (cycle.stage === "market_snapshot") {
    detail = `${cycle.symbol ?? `${cycle.total} 个标的`} · 采集行情`;
  } else if (cycle.stage === "portfolio") {
    detail = `${cycle.total} 个标的 · 读取账户状态`;
  } else if (cycle.stage === "batch_decision") {
    detail = `${cycle.total} 个标的 · 批量分析中`;
  } else {
    detail = `${cycle.symbol ?? `${cycle.total} 个标的`} · ${cycle.stage}`;
  }
  return <div className="live-cycle-strip">
    当前 {cycle.cadence} 周期 · {detail}
  </div>;
}

export function DecisionTiming({ decision }: { decision: DecisionEvent }) {
  const batchSeconds = (decision.duration_ms / 1000).toFixed(2);
  const decisionDurationMs = decision.decision_duration_ms ?? decision.duration_ms;
  const decisionSeconds = (decisionDurationMs / 1000).toFixed(2);
  const hasMaterialPostProcessing = decisionDurationMs - decision.duration_ms >= 100;
  return <span
    className="signal-time"
    data-tooltip="批次耗时是同周期全部标的共享的一次模型调用，不应按决策条数相加；整笔耗时还包含该标的后续风控与执行。"
  >
    {new Date(decision.created_at).toLocaleTimeString("zh-CN", { hour12: false })}
    <small>批次耗时 {batchSeconds}s</small>
    {hasMaterialPostProcessing && <small>整笔耗时 {decisionSeconds}s</small>}
  </span>;
}

export function StartupProbeCompletedSummary({
  probe,
  ready,
}: {
  probe: NonNullable<EngineStatus["startup_probe"]>;
  ready: boolean;
}) {
  return <div className="live-probe-summary">
    <div>最近试跑：{startupProbeSymbolSummary(probe)} · 批量分析 {probe.slowest_seconds}s
      · 负载 {((probe.aggregate_utilization ?? 0) * 100).toFixed(1)}%</div>
    <StartupProbeProviderResults probe={probe} />
    {!ready && <small>{probe.consumed
      ? "该试跑已用于一次运行，请重新试跑"
      : "参数已变化，请重新试跑"}</small>}
  </div>;
}

export function startupProbeSymbolSummary(
  probe: NonNullable<EngineStatus["startup_probe"]>,
): string {
  const candidateCount = probe.candidate_symbol_count;
  const extraPositionCount = probe.extra_position_symbol_count;
  if (candidateCount === undefined || extraPositionCount === undefined) {
    return `${probe.analysis_symbol_count} 个分析标的`;
  }
  const prefix = extraPositionCount > 0
    ? `${candidateCount} 个候选 + ${extraPositionCount} 个额外持仓 = `
    : `${candidateCount} 个候选 = `;
  return `${prefix}${probe.analysis_symbol_count} 个分析标的`;
}

export function StartupProbeRunningSummary({
  probe,
}: {
  probe: NonNullable<EngineStatus["startup_probe"]>;
}) {
  return <div className="live-probe-summary live-probe-running">
    <div>
      正式批量试跑：已完成 {probe.completed_providers}/{probe.provider_count} 个 Provider
      · <span title={probe.probe_symbols.join("、")}>
        {startupProbeSymbolSummary(probe)} · {probe.probe_cadence}
      </span>
    </div>
    <div className="live-probe-track" aria-label={`已完成 ${probe.completed_providers}/${probe.provider_count} 个 Provider`}>
      <span style={{ width: `${probe.completed_providers / probe.provider_count * 100}%` }} />
    </div>
    <StartupProbeProviderResults probe={probe} />
  </div>;
}

function StartupProbeProviderResults({
  probe,
}: {
  probe: NonNullable<EngineStatus["startup_probe"]>;
}) {
  return <div className="live-probe-provider-results">
    {Object.entries(probe.provider_results).map(([name, result]) => {
      if (result.status !== "completed") {
        return <div className="live-probe-provider" key={name}>
          <strong>{providerLabel(name)}</strong><small>等待结果</small>
        </div>;
      }
      const actions = Object.entries(result.actions ?? {})
        .map(([action, count]) => `${action} × ${count}`)
        .join(" · ");
      const tokens = result.total_tokens == null
        ? "Token 未报告"
        : `Token ${result.total_tokens.toLocaleString()}（未缓存 ${result.input_tokens?.toLocaleString() ?? 0} · 缓存 ${result.cached_input_tokens?.toLocaleString() ?? 0} · 输出 ${result.output_tokens?.toLocaleString() ?? 0}）`;
      const cost = result.equivalent_cost_usd == null
        ? "成本未知"
        : `成本 $${result.equivalent_cost_usd.toFixed(6)}`;
      return <div className="live-probe-provider" key={name}>
        <div><strong>{providerLabel(name)}</strong>
          <small>{result.model ?? "默认模型"}{result.reasoning_effort ? ` · ${result.reasoning_effort}` : ""}</small>
        </div>
        <div>{result.duration_seconds}s · {actions || "无意图"}</div>
        <small>{tokens} · {cost}</small>
        <details>
          <summary>查看 {result.intents?.length ?? 0} 条意图</summary>
          <div className="live-probe-intents">
            {(result.intents ?? []).map((intent) => <span key={intent.symbol}>
              {intent.symbol} · {intent.action} · {(intent.confidence * 100).toFixed(0)}%
            </span>)}
          </div>
        </details>
      </div>;
    })}
  </div>;
}

function ConsoleApp({ auth, onLogout }: { auth: AuthStatus; onLogout: () => void }) {
  const [tab, setTab] = useState<TabKey>("overview");
  const [status, setStatus] = useState<EngineStatus>(emptyStatus);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [codexAuthSession, setCodexAuthSession] = useState<CodexAuthSession | null>(null);
  const [codexUsage, setCodexUsage] = useState<CodexUsageSnapshot | null>(null);
  const [codexUsageLoading, setCodexUsageLoading] = useState(false);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [decisions, setDecisions] = useState<DecisionEvent[]>([]);
  const [liveRunPerformance, setLiveRunPerformance] = useState<LiveRunPerformance[]>([]);
  const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>("all");
  const [decisionsExhausted, setDecisionsExhausted] = useState(false);
  const decisionFilterRef = useRef(decisionFilter);
  decisionFilterRef.current = decisionFilter;
  const [portfolio, setPortfolio] = useState<AccountPortfolio | null>(null);
  const [positions, setPositions] = useState<AccountPosition[]>([]);
  const [fills, setFills] = useState<TradeFillRecord[]>([]);
  const [providerMetrics, setProviderMetrics] = useState<ProviderMetric[]>([]);
  const [runSession, setRunSession] = useState<RunSessionMetrics>(emptyRunSession);
  const [structureGateSummary, setStructureGateSummary] = useState<StructureGateSummary | null>(null);
  const [testnetStatus, setTestnetStatus] = useState<TestnetAccountStatus | null>(null);
  const [trailingStopEvents, setTrailingStopEvents] = useState<TrailingStopEvent[]>([]);
  const [trailingStopError, setTrailingStopError] = useState<string | null>(null);
  const [partialTakeProfitEvents, setPartialTakeProfitEvents] = useState<PartialTakeProfitEvent[]>([]);
  const [partialTakeProfitError, setPartialTakeProfitError] = useState<string | null>(null);
  const [operationsError, setOperationsError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [socketOnline, setSocketOnline] = useState(false);
  const [configDraft, setConfigDraft] = useState<Record<string, {
    model: string;
    effort: string;
    custom: boolean;
    pricing: string;
    authSource: string;
  }>>({});
  const [historySelected, setHistorySelected] = useState<Record<string, boolean>>({});
  const [historyConfirm, setHistoryConfirm] = useState(false);
  const [historyResult, setHistoryResult] = useState<string | null>(null);
  const [candidateDraft, setCandidateDraft] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; text: string }>>({});
  const [universeExpanded, setUniverseExpanded] = useState(false);
  const [limitDraft, setLimitDraft] = useState<{ minutes: string; budget: string } | null>(null);
  const [decisionTimeoutDraft, setDecisionTimeoutDraft] = useState<string | null>(null);
  const [overviewPanelExpanded, setOverviewPanelExpanded] = useState({
    providers: true,
    risk: true,
    universe: true,
  });

  const refreshProviders = useCallback(async () => {
    setProviders(await api<ProviderHealth[]>("/api/providers"));
  }, []);

  const refreshCodexUsage = useCallback(async () => {
    setCodexUsageLoading(true);
    try {
      setCodexUsage(await api<CodexUsageSnapshot>("/api/providers/codex-auth/usage"));
    } catch (reason) {
      setCodexUsage({
        available: false,
        buckets: [],
        checked_at: new Date().toISOString(),
        message: reason instanceof Error ? reason.message : "额度查询失败",
      });
    } finally {
      setCodexUsageLoading(false);
    }
  }, []);

  const codexCliAuthenticated = providers.some((provider) => provider.provider === "codex-auth"
    && provider.auth_source === "codex-cli" && provider.authenticated);
  useEffect(() => {
    if (codexCliAuthenticated) void refreshCodexUsage();
    else setCodexUsage(null);
  }, [codexCliAuthenticated, refreshCodexUsage]);

  const runCodexAuthAction = useCallback(async (
    action: "login" | "cancel" | "logout",
  ) => {
    setBusy(`codex-auth-${action}`);
    setError(null);
    const endpoint = action === "login"
      ? "/api/providers/codex-auth/login"
      : action === "cancel"
        ? "/api/providers/codex-auth/login/cancel"
        : "/api/providers/codex-auth/logout";
    try {
      const session = await api<CodexAuthSession>(endpoint, { method: "POST" });
      setCodexAuthSession(session);
      if (action !== "login") await refreshProviders();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [refreshProviders]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const session = await api<CodexAuthSession>("/api/providers/codex-auth/session");
        if (stopped) return;
        setCodexAuthSession(session);
        if (session.state === "starting" || session.state === "pending") {
          timer = window.setTimeout(poll, 1000);
        } else if (session.state === "succeeded") {
          await refreshProviders();
        }
      } catch {
        // The normal console error handling covers authenticated API failures.
      }
    };
    void poll();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [codexAuthSession?.state, refreshProviders]);

  const applyProviderConfig = useCallback(async (
    name: string,
    draft: { model: string; effort: string; pricing?: string; authSource?: string },
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
          ...(draft.authSource === undefined ? {} : { auth_source: draft.authSource }),
        }),
      });
      setProviders(next);
      setStatus((current) => ({
        ...current,
        startup_probe: current.startup_probe
          ? {
            ...current.startup_probe,
            ready: false,
            invalidated_reason: "provider settings changed",
          }
          : null,
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const testProvider = useCallback(async (
    name: string,
    draft?: { model: string; effort: string; pricing?: string; authSource?: string },
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
            ...(draft.authSource === undefined ? {} : { auth_source: draft.authSource }),
          }),
        });
        setProviders(next);
        setStatus((current) => ({
          ...current,
          startup_probe: current.startup_probe
            ? {
              ...current.startup_probe,
              ready: false,
              invalidated_reason: "provider settings changed",
            }
            : null,
        }));
      }
      const result = await api<ProviderTestResult>(
        "/api/providers/test",
        { method: "POST", body: JSON.stringify({ name }) },
      );
      const seconds = (result.duration_ms / 1000).toFixed(1);
      const tokens = result.usage?.tokens_reported
        ? `${result.usage.total_tokens.toLocaleString("zh-CN")} Token`
        : "Token 未报告";
      const cost = result.usage?.equivalent_cost_usd == null
        ? "成本未知"
        : `成本 $${result.usage.equivalent_cost_usd.toFixed(6)}`;
      const text = result.ok
        ? `✓ ${result.model ?? "默认模型"} · ${seconds}s · ${result.action} · ${tokens} · ${cost}`
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

  const selectProvider = useCallback((name: string) => {
    if (status.provider_chain[0] !== name) void changeProviderChain([name]);
  }, [status.provider_chain, changeProviderChain]);

  const selectCadence = useCallback(async (cadence: string) => {
    if (status.active_cadences[0] === cadence) return;
    setBusy("cadences");
    setError(null);
    try {
      setStatus(await api<EngineStatus>("/api/cadences", { method: "POST", body: JSON.stringify({ cadences: [cadence] }) }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [status.active_cadences]);

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
    const [nextPortfolio, nextPositions, nextFills, nextTestnetStatus] = await Promise.all([
      api<AccountPortfolio>("/api/account/portfolio"),
      api<AccountPosition[]>("/api/account/positions"),
      api<TradeFillRecord[]>("/api/fills?limit=50"),
      api<TestnetAccountStatus>("/api/testnet/account-status"),
    ]);
    setPortfolio(nextPortfolio);
    setPositions(nextPositions);
    setFills(nextFills);
    setTestnetStatus(nextTestnetStatus);
  }, []);

  const refreshTrailingStops = useCallback(async () => {
    try {
      const response = await api<{ events: TrailingStopEvent[] }>(
        "/api/trailing-stops/history?limit=100",
      );
      setTrailingStopEvents(response.events);
      setTrailingStopError(null);
    } catch (reason) {
      setTrailingStopError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  const refreshPartialTakeProfits = useCallback(async () => {
    try {
      const response = await api<{ events: PartialTakeProfitEvent[] }>(
        "/api/partial-take-profits/history?limit=100",
      );
      setPartialTakeProfitEvents(response.events);
      setPartialTakeProfitError(null);
    } catch (reason) {
      setPartialTakeProfitError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  const closeAccountPosition = useCallback(async (symbol: string): Promise<boolean> => {
    setBusy(`position-close-${symbol}`);
    setError(null);
    try {
      await api<ManualCloseResult>("/api/account/positions/close", {
        method: "POST",
        body: JSON.stringify({ symbol }),
      });
      await refreshAccount();
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      try {
        await refreshAccount();
      } catch {
        // Preserve the action error; the normal account poll will retry shortly.
      }
      return false;
    } finally {
      setBusy(null);
    }
  }, [refreshAccount]);

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
    const latest = await api<DecisionEvent[]>(decisionQueryUrl(decisionFilter));
    setDecisionsExhausted(decisionRunCount(latest) < DECISION_RUN_PAGE_SIZE);
    mergeDecisions(latest);
  }, [decisionFilter, mergeDecisions]);
  const refreshLiveRunPerformance = useCallback(async () => {
    setLiveRunPerformance(await api<LiveRunPerformance[]>("/api/live-runs/performance?limit=100"));
  }, []);
  const refreshDecisionsRef = useRef(refreshDecisions);
  refreshDecisionsRef.current = refreshDecisions;

  const loadOlderDecisions = useCallback(async () => {
    const runIds = decisions
      .map((decision) => decision.live_run_id)
      .filter((runId): runId is number => runId !== null);
    const oldestRunId = runIds.length ? Math.min(...runIds) : undefined;
    const older = await api<DecisionEvent[]>(
      decisionQueryUrl(decisionFilter, oldestRunId),
    );
    setDecisionsExhausted(decisionRunCount(older) < DECISION_RUN_PAGE_SIZE);
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
    try {
      const metrics = await api<ProviderMetricsResponse>("/api/metrics/providers?hours=24");
      setProviderMetrics(metrics.providers);
      setOperationsError(null);
    } catch (reason) {
      setOperationsError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  const refreshRunSession = useCallback(async () => {
    setRunSession(await api<RunSessionMetrics>("/api/metrics/run-session"));
  }, []);

  const refreshStructureGateSummary = useCallback(async () => {
    setStructureGateSummary(await api<StructureGateSummary>("/api/structure-gate/summary"));
  }, []);

  useEffect(() => {
    refresh().catch((reason: Error) => setError(reason.message));
    refreshAccount().catch((reason: Error) => setError(reason.message));
    refreshTrailingStops().catch(() => undefined);
    refreshPartialTakeProfits().catch(() => undefined);
    refreshLiveRunPerformance().catch(() => undefined);
    refreshOperations().catch(() => undefined);
    refreshRunSession().catch(() => undefined);
    refreshStructureGateSummary().catch(() => undefined);
    const account = window.setInterval(() => {
      refreshAccount().catch(() => undefined);
      refreshTrailingStops().catch(() => undefined);
      refreshPartialTakeProfits().catch(() => undefined);
      refreshLiveRunPerformance().catch(() => undefined);
      refreshOperations().catch(() => undefined);
      refreshStructureGateSummary().catch(() => undefined);
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
  }, [refresh, refreshAccount, refreshTrailingStops, refreshPartialTakeProfits, mergeDecisions, refreshLiveRunPerformance, refreshOperations, refreshRunSession, refreshStructureGateSummary]);

  const act = useCallback(async (name: string, path: string, body?: unknown) => {
    setBusy(name);
    setError(null);
    try {
      const next = await api<EngineStatus>(path, {
        method: "POST",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      setStatus(next);
      if (path.startsWith("/api/engine/")) {
        await Promise.all([refreshRunSession(), refreshLiveRunPerformance()]);
      }
      if (path === "/api/engine/run-once") {
        await Promise.all([refreshDecisions(), refreshAccount()]);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [refreshAccount, refreshDecisions, refreshLiveRunPerformance, refreshRunSession]);

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

  const venueExcludedSymbols = status.venue_excluded_symbols;
  const selectedExternalProvider = useMemo(
    () => status.provider_chain
      .map((name) => providers.find((provider) => provider.provider === name))
      .find((provider) => provider?.capabilities.external_inference),
    [providers, status.provider_chain],
  );
  const displayedDecisionTimeout = decisionTimeoutDraft
    ?? String(status.decision_timeout_seconds ?? selectedExternalProvider?.timeout_seconds ?? 60);
  const requestedDecisionTimeout = selectedExternalProvider
    ? Number(displayedDecisionTimeout)
    : null;
  const probeReady = Boolean(
    status.startup_probe?.ready
    && !status.startup_probe.consumed
    && (
      !selectedExternalProvider
      || status.startup_probe.timeout_seconds === requestedDecisionTimeout
    ),
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
          <span>{socketOnline ? "FRONTEND ONLINE" : "FRONTEND OFFLINE"}</span>
          {auth.enabled && <button className="logout-button" onClick={onLogout} title={`当前用户：${auth.username ?? "—"}`}>退出</button>}
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
        {status.running && status.rescue_count > 0 && <div className="lock-banner">本次运行已紧急回补 {status.rescue_count} / {status.rescue_limit} 次；达到上限后将自动停止，当前持仓继续由交易所侧止盈止损保护。</div>}
        {status.emergency_locked && <EmergencyLockBanner
          lockedUntil={status.emergency_locked_until}
          busy={busy === "clear-emergency-lock"}
          onClear={() => void act(
            "clear-emergency-lock",
            "/api/engine/clear-emergency-lock",
          )}
        />}

        {tab === "overview" && (<>
        <section className="hero panel">
          <div>
            <p className="eyebrow">AUTONOMOUS DESK / 本地前端</p>
            <h1>系统{status.running ? "运行中" : "已停机"}</h1>
            <p className="hero-copy">
              外部模型或本地规则负责提出交易意图，确定性风控拥有最终否决权。当前不支持真钱实盘。
            </p>
          </div>
          <div className="controls">
            <CadenceSelector
              active={status.active_cadences[0] ?? "15m"}
              supported={status.supported_cadences}
              disabled={busy !== null || status.running}
              onSelect={(cadence) => void selectCadence(cadence)}
            />
            <div className="cadence-select" title={status.running ? "运行时锁定" : "设置动态候选池前 N 名；已有持仓会去重后额外加入分析"}>
              <span className="range-head">
                每周期候选标的数
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
            <div className="cadence-select" title="覆盖整次外部模型调用的绝对截止时间；持续启动前需试跑，运行一次无需试跑">
              <span>正式决策硬超时</span>
              <div className="limit-row live-timeout-row">
                <label>
                  <input
                    type="number"
                    min={1}
                    max={600}
                    step={1}
                    value={displayedDecisionTimeout}
                    disabled={busy !== null || status.running || !selectedExternalProvider}
                    onFocus={(event) => event.target.select()}
                    onChange={(event) => setDecisionTimeoutDraft(event.target.value)}
                  />
                  <small>秒</small>
                </label>
                <small>{selectedExternalProvider
                  ? "持续启动前需试跑；运行一次直接分析并交易，无需试跑"
                  : "本地规则无外部调用超时；运行一次无需试跑"}</small>
              </div>
              {(busy === "probe" || busy === "probe-and-start") && !status.startup_probe && <div className="live-probe-summary">
                正在读取真实行情与测试网账户…
              </div>}
              {status.startup_probe?.running && <StartupProbeRunningSummary probe={status.startup_probe} />}
              {status.startup_probe && !status.startup_probe.running && status.startup_probe.slowest_seconds !== undefined
                && <StartupProbeCompletedSummary probe={status.startup_probe} ready={probeReady} />}
            </div>
            <LiveRunActionButtons
              busy={busy}
              running={status.running}
              emergencyLocked={status.emergency_locked}
              probeReady={probeReady}
              onProbe={() => {
                const timeout = Number(displayedDecisionTimeout);
                if (selectedExternalProvider && (!Number.isFinite(timeout) || timeout <= 0)) {
                  setError("请输入有效的正式决策硬超时秒数");
                  return;
                }
                void act("probe", "/api/engine/probe", {
                  timeout_seconds: selectedExternalProvider ? timeout : null,
                });
              }}
              onProbeAndStart={() => {
                const timeout = Number(displayedDecisionTimeout);
                if (selectedExternalProvider && (!Number.isFinite(timeout) || timeout <= 0)) {
                  setError("请输入有效的正式决策硬超时秒数");
                  return;
                }
                void act("probe-and-start", "/api/engine/probe-and-start", {
                  timeout_seconds: selectedExternalProvider ? timeout : null,
                });
              }}
              onRunOnce={() => {
                const timeout = Number(displayedDecisionTimeout);
                if (selectedExternalProvider && (!Number.isFinite(timeout) || timeout <= 0)) {
                  setError("请输入有效的正式决策硬超时秒数");
                  return;
                }
                void act("run-once", "/api/engine/run-once", {
                  timeout_seconds: selectedExternalProvider ? timeout : null,
                });
              }}
              onStart={() => void act("start", "/api/engine/start", {
                timeout_seconds: selectedExternalProvider ? requestedDecisionTimeout : null,
              })}
              onStop={() => void act("stop", "/api/engine/stop")}
              onEmergencyStop={() => void act("kill", "/api/engine/emergency-stop")}
            />
          </div>
          {status.running && status.scheduler.current_cycles.map((cycle) =>
            <LiveCycleStatus cycle={cycle} key={cycle.cadence} />)}
          {status.scheduler.last_error && <div className="live-cycle-error">
            最近调度错误：{status.scheduler.last_error}
          </div>}
        </section>

        <RunUsage session={runSession} />

        <section className="grid overview-grid">
          <div className="overview-column">
            <CollapsiblePanel
              className="provider-panel"
              code="01"
              title="模型接入"
              meta="单一 Provider"
              expanded={overviewPanelExpanded.providers}
              onExpandedChange={(expanded) => setOverviewPanelExpanded((current) => ({
                ...current,
                providers: expanded,
              }))}
            >
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
                const codexProvider = provider.provider === "codex-auth";
                const configurable = provider.capabilities.configurable_model;
                const pricing = configDraft[provider.provider]?.pricing ?? provider.pricing ?? "";
                const authSource = configDraft[provider.provider]?.authSource
                  ?? provider.auth_source
                  ?? "";
                const custom = configDraft[provider.provider]?.custom ?? (model !== "" && !options.includes(model));
                const draft = { model, effort, custom, pricing, authSource };
                const authSourceDirty = codexProvider
                  && authSource !== (provider.auth_source ?? "");
                const dirty = configurable && (model !== (provider.model ?? "")
                  || effort !== (provider.reasoning_effort ?? "")
                  || (customProvider && pricing !== (provider.pricing ?? ""))
                  || authSourceDirty);
                const update = (next: Partial<typeof draft>) =>
                  setConfigDraft((current) => ({ ...current, [provider.provider]: { ...draft, ...next } }));
                return <div
                  key={provider.provider}
                  className={`provider-card ${routeIndex >= 0 ? "selected" : ""}`}
                >
                  <ProviderChoiceButton
                    name={provider.provider}
                    selected={routeIndex === 0}
                    disabled={status.running || busy !== null}
                    onSelect={selectProvider}
                  >
                    <span className={`provider-icon ${provider.authenticated ? "ready" : ""}`}>
                      {providerIcon(provider.provider)}
                    </span>
                    <span className="provider-text">
                      <strong>{providerLabel(provider.provider)}</strong>
                      <small>{codexProvider
                        ? codexProviderIdentity(provider)
                        : provider.version ?? provider.detail}</small>
                    </span>
                    <span className={`status-pill ${provider.authenticated ? "ok" : "off"}`}>
                      {routeIndex === 0 ? "已选择 · " : ""}{provider.authenticated ? "READY" : provider.available ? "LOGIN" : "MISSING"}
                    </span>
                  </ProviderChoiceButton>
                  {!configurable ? <div className="provider-card-config local-provider-config">
                    <div>
                      <strong>{provider.model}</strong>
                      <small>只使用现有多周期 K 线特征 · 本地确定性计算 · 0 Token / 0 成本</small>
                    </div>
                    <button className="text-button" disabled={status.running || busy !== null}
                      onClick={() => testProvider(provider.provider)}>
                      {busy === `test-${provider.provider}` ? "测试中…" : "测试"}
                    </button>
                    {testResult[provider.provider] && <span className={`config-test-result ${testResult[provider.provider].ok ? "ok" : "err"}`}>
                      {testResult[provider.provider].text}
                    </span>}
                  </div> : <div className={`provider-card-config ${customProvider ? "has-pricing" : codexProvider ? "has-auth-source" : ""}`}>
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
                    {codexProvider && <label>
                      <span>接入来源</span>
                      <CodexAuthSourceSelect
                        value={authSource}
                        disabled={status.running}
                        options={provider.auth_source_options ?? []}
                        onChange={(source) => update({ authSource: source })}
                      />
                    </label>}
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
                          ...(authSourceDirty ? { authSource } : {}),
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
                            ...(authSourceDirty ? { authSource } : {}),
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
                  </div>}
                  {codexProvider && authSource === "codex-cli" && !authSourceDirty && <CodexCliAuthControls
                    authenticated={provider.authenticated}
                    busy={busy?.startsWith("codex-auth-") ?? false}
                    disabled={status.running || busy !== null}
                    session={codexAuthSession}
                    onLogin={() => void runCodexAuthAction("login")}
                    onCancel={() => void runCodexAuthAction("cancel")}
                    onLogout={() => void runCodexAuthAction("logout")}
                    onRefreshUsage={() => void refreshCodexUsage()}
                    usage={codexUsage}
                    usageLoading={codexUsageLoading}
                  />}
                </div>;
              })}
            </div>
            </CollapsiblePanel>
          </div>

          <div className="overview-column">
            <CollapsiblePanel
              className="risk-panel"
              code="02"
              title="硬风控边界"
              meta="不可由模型修改"
              expanded={overviewPanelExpanded.risk}
              onExpandedChange={(expanded) => setOverviewPanelExpanded((current) => ({
                ...current,
                risk: expanded,
              }))}
            >
            <div className="risk-grid">
              <RiskItem label="候选标的" value={`${status.candidate_count} / 20`} detail="动态候选池" />
              <RiskItem label="最大杠杆" value="10×" detail="模型不可突破" />
              <RiskItem label="24h亏损熔断" value={formatDailyLossPercent(status.risk_limits.daily_loss_fraction)} detail="窗口起始权益" />
              <RiskItem label="单笔风险" value="1.0%" detail="权益上限" />
              <RiskItem label="组合止损风险" value="4.0%" detail="权益上限" />
              <RiskItem label="最低盈亏比" value="> 1.3:1" detail="原始值" />
              <RiskItem label="保证金占用" value="80%" detail="组合上限" />
              <RiskItem label="单标的保证金" value="10%" detail="权益上限" />
              <RiskItem label="持仓模式" value="逐仓" detail="单向净仓" />
            </div>
            <div className="risk-line"><span style={{ width: "80%" }} /></div>
            <p>模型提示词不包含盈亏比要求；所有开仓必须包含交易所侧止损和止盈，并通过组合风险、原始盈亏比、精度、陈旧行情和强平缓冲检查。</p>
            <StructureGateSummaryCard summary={structureGateSummary} />
            </CollapsiblePanel>

            <CollapsiblePanel
              className="universe-panel"
              code="03"
              title="动态候选池"
              meta={venueExcludedSymbols.length
                ? `测试网可交易 · 已过滤 ${venueExcludedSymbols.length}`
                : "测试网可交易"}
              expanded={overviewPanelExpanded.universe}
              onExpandedChange={(expanded) => setOverviewPanelExpanded((current) => ({
                ...current,
                universe: expanded,
              }))}
            >
            <div className="universe-actions">
              <button className="compact" disabled={busy !== null} onClick={refreshUniverse}>{busy === "universe" ? "扫描中…" : "刷新全市场"}</button>
            </div>
            {venueExcludedSymbols.length > 0 && <p className="universe-filter-note" title={venueExcludedSymbols.join(", ")}>
              已在模型调用前排除测试网未开放的生产行情标的：{venueExcludedSymbols.slice(0, 5).map((symbol) => symbol.replace("USDT", "")).join("、")}
              {venueExcludedSymbols.length > 5 ? ` 等 ${venueExcludedSymbols.length} 个` : ""}
            </p>}
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
            </CollapsiblePanel>
          </div>

          <DecisionPanel
            decisions={decisions}
            liveRunPerformance={liveRunPerformance}
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
            fills={fills}
            testnetStatus={testnetStatus}
            engineRunning={status.running}
            busy={busy}
            onClosePosition={closeAccountPosition}
          />
          <TrailingStopPanel
            status={status.scheduler.trailing_stop ?? null}
            events={trailingStopEvents}
            error={trailingStopError}
          />
          <PartialTakeProfitPanel
            status={status.scheduler.partial_take_profit ?? null}
            events={partialTakeProfitEvents}
            error={partialTakeProfitError}
          />
        </section>
        )}

        {tab === "operations" && (
        <section className="grid">
          <OperationsPanel
            providerMetrics={providerMetrics}
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
                disabled={busy !== null || status.running}
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
                  disabled={busy !== null || status.running || !Object.values(historySelected).some(Boolean)}
                  onClick={() => setHistoryConfirm(true)}
                >清除所选</button>
              ) : (
                <>
                  <span className="history-warn">确认删除所选数据？此操作不可恢复。</span>
                  <button className="danger" disabled={busy !== null || status.running} onClick={clearHistory}>{busy === "history-clear" ? "删除中…" : "确认删除"}</button>
                  <button className="text-button" disabled={busy !== null} onClick={() => setHistoryConfirm(false)}>取消</button>
                </>
              )}
              {status.running && <span className="history-warn">正式决策运行中，停止后才能删除历史。</span>}
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
      <footer><span>CANDLEPILOT / GPL-3.0</span><span>{auth.enabled ? "AUTHENTICATED CONSOLE" : "LOCALHOST ONLY"} · NO LIVE MONEY</span></footer>
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    fetch("/api/auth/status", { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<AuthStatus>;
      })
      .then((status) => { if (active) setAuth(status); })
      .catch((reason) => { if (active) setAuthError(reason instanceof Error ? reason.message : String(reason)); });
    const unauthorized = () => setAuth((current) => current ? { ...current, authenticated: false, username: null } : current);
    window.addEventListener("candlepilot:unauthorized", unauthorized);
    return () => {
      active = false;
      window.removeEventListener("candlepilot:unauthorized", unauthorized);
    };
  }, []);

  const logout = useCallback(async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      setAuth((current) => current ? { ...current, authenticated: false, username: null } : current);
    }
  }, []);

  if (authError) return <main className="login-shell"><section className="login-card panel"><h1>控制台不可用</h1><p role="alert">无法读取认证状态：{authError}</p></section></main>;
  if (auth === null) return <main className="login-shell"><section className="login-card panel"><p>正在检查登录状态…</p></section></main>;
  if (!auth.authenticated) return <LoginScreen onAuthenticated={setAuth} />;
  return <ConsoleApp auth={auth} onLogout={logout} />;
}

export function WebUpdatePanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [status, setStatus] = useState<WebUpdateStatus | null>(null);
  const [checkResult, setCheckResult] = useState<WebUpdateCheck | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    const next = await api<WebUpdateStatus>("/api/update/status");
    setStatus(next);
    return next;
  }, []);

  useEffect(() => {
    refreshStatus().catch(() => undefined);
  }, [refreshStatus]);

  const check = useCallback(async () => {
    setBusy("update-check");
    setError(null);
    setConfirming(false);
    setNote("正在检查远端版本…");
    try {
      const next = await api<WebUpdateCheck>("/api/update/check", { method: "POST" });
      setCheckResult(next);
      setNote(null);
    } catch (reason) {
      setCheckResult(null);
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [setBusy, setError]);

  const update = useCallback(async () => {
    setBusy("update");
    setError(null);
    setNote("正在启动安全更新…");
    try {
      await api<{ started: boolean }>("/api/update", { method: "POST" });
    } catch (reason) {
      setBusy(null);
      setConfirming(false);
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
      return;
    }

    // The dedicated update service deliberately takes this backend offline.
    // Keep polling through the expected disconnect and read the root worker's
    // persisted terminal result after the updated or rolled-back service returns.
    for (let attempt = 0; attempt < 1500; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      try {
        const next = await refreshStatus();
        if (next.phase === "running" || next.phase === "idle") {
          setNote("更新中：正在备份、安装依赖、构建并执行健康检查…");
          continue;
        }
        if (next.phase === "completed") {
          setNote(`${next.message}，正在载入新版本…`);
          window.location.reload();
          return;
        }
        if (next.phase === "failed") {
          setBusy(null);
          setConfirming(false);
          setNote(null);
          setError(next.message);
          return;
        }
      } catch {
        setNote("更新中：后端暂时离线，等待服务恢复…");
      }
    }
    setBusy(null);
    setConfirming(false);
    setNote(null);
    setError("更新在 50 分钟内没有返回结果，请检查 candlepilot-update.service 日志。");
  }, [refreshStatus, setBusy, setError]);

  const commitRange = status?.from_commit && status.current_commit
    ? `${status.from_commit.slice(0, 12)} → ${status.current_commit.slice(0, 12)}`
    : null;

  return (
    <div className="settings-section web-update-section">
      <h4 className="account-subhead">软件更新</h4>
      <div className="settings-actions">
        <button
          className="compact"
          disabled={busy !== null || status === null || !status.supported || status.phase === "running"}
          onClick={check}
        >
          {busy === "update-check" ? "检查中…" : "检查更新"}
        </button>
        {!confirming ? (
          <button
            className="compact"
            disabled={busy !== null || status?.phase === "running" || !checkResult?.update_available}
            onClick={() => setConfirming(true)}
          >
            安装更新
          </button>
        ) : (
          <>
            <span className="history-warn">
              确认更新？必须先停止引擎、回测和试跑。服务会短暂离线；更新失败将自动回滚。
            </span>
            <button className="compact" disabled={busy !== null} onClick={update}>
              {busy === "update" ? "更新中…" : "确认更新"}
            </button>
            <button className="text-button" disabled={busy !== null} onClick={() => setConfirming(false)}>
              取消
            </button>
          </>
        )}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        {status?.supported
          ? "调用 VPS 安装器的安全原地更新：仅接受 main 快进，保留 .env、数据库、行情、TLS 和模型登录；更新前备份，失败自动回滚。"
          : status?.message ?? "正在检查 VPS 更新能力…"}
      </small>
      {checkResult && (
        <div className={`update-check-result ${checkResult.update_available ? "available" : "current"}`}>
          <strong>{checkResult.message}</strong>
          <span>{checkResult.branch}</span>
          <span>{checkResult.current_commit.slice(0, 12)} → {checkResult.latest_commit.slice(0, 12)}</span>
          <span>{formatLocalDateTime(new Date(checkResult.checked_at))}</span>
        </div>
      )}
      {status && status.phase !== "idle" && (
        <div className={`update-result ${status.phase}`}>
          <strong>{status.message}</strong>
          {commitRange && <span>{commitRange}</span>}
          {status.backup && <span>备份：{status.backup}</span>}
          {status.finished_at && <span>{formatLocalDateTime(new Date(status.finished_at))}</span>}
        </div>
      )}
    </div>
  );
}

function formatStorageSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${unit}`;
}

export function BackupPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [inventory, setInventory] = useState<BackupInventory | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const next = await api<BackupInventory>("/api/backups");
    setInventory(next);
    return next;
  }, []);

  useEffect(() => {
    load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [load, setError]);

  const waitForResult = useCallback(async () => {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const next = await load();
      if (next.status.phase === "completed") return next;
      if (next.status.phase === "failed") throw new Error(next.status.message);
    }
    throw new Error("备份维护在 60 秒内没有返回结果，请检查更新服务日志。");
  }, [load]);

  const runAction = useCallback(async (path: string, busyKey: string) => {
    setWorking(true);
    setBusy(busyKey);
    setError(null);
    setNote("备份维护已排队…");
    try {
      await api<{ queued: boolean }>(path, { method: "POST" });
      const next = await waitForResult();
      const reclaimed = next.status.reclaimed_bytes;
      setNote(reclaimed === null
        ? next.status.message
        : `${next.status.message}，释放 ${formatStorageSize(reclaimed)}`);
      setConfirming(null);
    } catch (reason) {
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setWorking(false);
      setBusy(null);
    }
  }, [setBusy, setError, waitForResult]);

  const blocked = busy !== null || working || inventory?.status.phase === "running";
  const total = inventory?.backups.reduce((sum, backup) => sum + backup.size_bytes, 0) ?? 0;

  return (
    <div className="settings-section web-update-section backup-section">
      <h4 className="account-subhead">服务器备份</h4>
      <div className="settings-actions">
        <button
          className="compact"
          disabled={blocked || inventory === null || !inventory.supported}
          onClick={() => runAction("/api/backups/refresh", "backup-refresh")}
        >
          {working && busy === "backup-refresh" ? "刷新中…" : "刷新备份清单"}
        </button>
        {inventory?.backups.length ? (
          <span className="settings-saved">{inventory.backups.length} 份 · {formatStorageSize(total)}</span>
        ) : null}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        {inventory?.supported
          ? "只列出安装器创建的标准备份。最新一份始终保留；删除由受限的 root 维护服务执行且不可恢复。"
          : inventory?.status.message ?? "正在读取 VPS 备份清单…"}
      </small>
      {inventory?.backups.length === 0 && inventory.supported && (
        <div className="empty backup-empty">清单为空；刷新后显示现有备份。</div>
      )}
      {inventory && inventory.backups.length > 0 && (
        <div className="backup-list">
          {inventory.backups.map((backup) => (
            <div className="backup-row" key={backup.id}>
              <div>
                <strong>{formatLocalDateTime(new Date(backup.created_at))}</strong>
                <small>{backup.id} · {formatStorageSize(backup.size_bytes)}{backup.source_commit ? ` · ${backup.source_commit.slice(0, 12)}` : ""}</small>
              </div>
              <div className="backup-actions">
                {backup.protected ? (
                  <span className="backup-protected">最新 · 保留</span>
                ) : confirming === backup.id ? (
                  <>
                    <span className="history-warn">确认永久删除这份备份？</span>
                    <button className="compact danger" disabled={blocked} onClick={() => runAction(`/api/backups/${backup.id}/delete`, "backup-delete")}>确认删除</button>
                    <button className="text-button" disabled={blocked} onClick={() => setConfirming(null)}>取消</button>
                  </>
                ) : (
                  <button className="text-button danger-text" disabled={blocked} onClick={() => setConfirming(backup.id)}>删除</button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function LogMaintenancePanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [status, setStatus] = useState<LogMaintenanceStatus | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const next = await api<LogMaintenanceStatus>("/api/logs");
    setStatus(next);
    return next;
  }, []);

  useEffect(() => {
    load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [load, setError]);

  const clearLogs = useCallback(async () => {
    setBusy("clear-logs");
    setError(null);
    setNote("日志清理已排队…");
    try {
      await api<{ queued: boolean }>("/api/logs/clear", { method: "POST" });
      for (let attempt = 0; attempt < 180; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        let next: LogMaintenanceStatus;
        try {
          next = await load();
        } catch {
          setNote("服务正在切换专用日志，等待恢复…");
          continue;
        }
        if (next.phase === "running" || next.phase === "idle") {
          setNote("正在隔离并清理 CandlePilot 日志，服务可能短暂重连…");
          continue;
        }
        if (next.phase === "failed") throw new Error(next.message);
        const sizes = next.before_bytes !== null && next.after_bytes !== null
          ? `（${formatStorageSize(next.before_bytes)} → ${formatStorageSize(next.after_bytes)}）`
          : "";
        setNote(`${next.message}${sizes}`);
        setConfirming(false);
        return;
      }
      throw new Error("日志清理在 90 秒内没有返回结果，请检查更新服务日志。");
    } catch (reason) {
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [load, setBusy, setError]);

  const blocked = busy !== null || status === null || !status.supported || status.phase === "running";
  return (
    <div className="settings-section web-update-section">
      <h4 className="account-subhead">CandlePilot 日志</h4>
      <div className="settings-actions">
        {!confirming ? (
          <button className="compact danger" disabled={blocked} onClick={() => setConfirming(true)}>
            清除日志
          </button>
        ) : (
          <>
            <span className="history-warn">确认永久清除 CandlePilot 专用日志？活动任务必须已停止，首次启用隔离时服务会短暂重启。</span>
            <button className="compact danger" disabled={busy !== null} onClick={clearLogs}>确认清除</button>
            <button className="text-button" disabled={busy !== null} onClick={() => setConfirming(false)}>取消</button>
          </>
        )}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        {status?.supported
          ? "只清理 CandlePilot 的独立 systemd journal，不影响 SSH、Nginx 或其他服务；删除不可恢复。"
          : status?.message ?? "正在读取 VPS 日志管理状态…"}
      </small>
      {status?.finished_at && status.phase !== "running" && (
        <div className={`update-result ${status.phase}`}>
          <strong>{status.message}</strong>
          {status.before_bytes !== null && status.after_bytes !== null && (
            <span>{formatStorageSize(status.before_bytes)} → {formatStorageSize(status.after_bytes)}</span>
          )}
          <span>{formatLocalDateTime(new Date(status.finished_at))}</span>
        </div>
      )}
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
            <span className="history-warn">确认重启？引擎、回测、探测和调度任务必须已停止；重启期间页面会短暂断开。</span>
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
        用当前 .env 重新启动后端进程，让上面保存的设置生效。引擎、回测、探测、采集或调度任务运行中会被拒绝；
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

  // api_key null means "leave the stored key alone" — the frontend never holds it.
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
                {["", "low", "medium", "high", "xhigh", "max"].map((o) => (
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
      <WebUpdatePanel busy={busy} setBusy={setBusy} setError={setError} />
      <BackupPanel busy={busy} setBusy={setBusy} setError={setError} />
      <LogMaintenancePanel busy={busy} setBusy={setBusy} setError={setError} />
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

function formatEstimatedDuration(seconds: number): string {
  const totalMinutes = Math.ceil(seconds / 60);
  if (totalMinutes <= 60) return `${totalMinutes} 分钟`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours} 小时 ${minutes} 分钟`;
}

function backtestHeadline(run: BacktestRun, model: BacktestRun["models"][number]) {
  return model.result ?? (run.status === "running" ? model.live_result : null);
}

function BacktestRemaining({ run }: { run: BacktestRun }) {
  if (run.status !== "running") return null;
  const active = run.models.filter((model) => model.progress < 1);
  if (!active.length) return <small className="run-timing">正在收尾</small>;
  if (active.some((model) => model.remaining_seconds === null)) {
    return <small className="run-timing">剩余时间推算中</small>;
  }
  const seconds = Math.max(...active.map((model) => model.remaining_seconds ?? 0));
  return <small className="run-timing" data-tooltip="多个模型并行回测，因此整轮剩余时间取尚未完成模型中的最慢值。">
    剩余约 {formatEstimatedDuration(seconds)}
  </small>;
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

export function BacktestSymbolList({ symbols }: { symbols: string[] }) {
  return <span className="run-symbols" role="list" aria-label={`回测标的：${symbols.join("、")}`}>
    {symbols.map((symbol) => <i key={symbol} role="listitem">{symbol}</i>)}
  </span>;
}

export function RunUsage({ session }: { session: RunSessionMetrics }) {
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
        <span data-tooltip="Provider 报告的未缓存输入 Token；缓存命中与缓存写入分别计入右侧两项，因此该值可能很小。">未缓存输入<strong>{session.input_tokens.toLocaleString()}</strong></span>
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

const BACKTEST_VS_LIVE: Array<{ aspect: string; live: string; replay: string; plain: string }> = [
  {
    aspect: "下单",
    live: "真实签名下单到币安测试网，交易所撮合、交易所侧括号单",
    replay: "本地仿真：下一根 K 线开盘价成交 + 滑点，不发任何订单",
    plain: "同左",
  },
  {
    aspect: "订单流",
    live: "20 档盘口失衡、成交流水失衡、基差、持仓量",
    replay: "全部在场——正式引擎实际送入决策的完整快照",
    plain: "全部缺失。币安不提供历史盘口，无法重建。Prompt 已告知模型，不因缺流而否决形态",
  },
  {
    aspect: "价差",
    live: "真实买一卖一",
    replay: "原正式决策快照中的真实买一卖一",
    plain: "无盘口即无价差（bid = ask = mark）。编一个价差会美化每笔成交",
  },
  {
    aspect: "标的",
    live: "全市场动态扫描，每分钟轮换",
    replay: "原正式运行实际分析的动态候选与已有持仓",
    plain: "你指定标的池——历史上的价差/24h ticker 快照不存在，选币无法忠实重放",
  },
  {
    aspect: "K 线特征",
    live: "5m/15m/30m/1h/4h 全套 + 日线结构位",
    replay: "直接复用正式运行保存的精确特征",
    plain: "同左",
  },
  {
    aspect: "风控",
    live: "AggressiveRiskPolicy",
    replay: "同一个——24h亏损熔断、止损风险与保证金上限、tick 对齐全部生效",
    plain: "同左",
  },
];

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
  const [formalRuns, setFormalRuns] = useState<ReplayableFormalRun[]>([]);
  const [replayLiveRunId, setReplayLiveRunId] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [probe, setProbe] = useState<ProbeStatus | null>(null);
  const [autoEstimatePending, setAutoEstimatePending] = useState(false);
  const [timeout, setTimeoutSeconds] = useState("");
  const [openDecisions, setOpenDecisions] = useState<string | null>(null);
  const [decisionPage, setDecisionPage] = useState<BacktestDecisionPage | null>(null);
  const [decisionsLoadingMore, setDecisionsLoadingMore] = useState(false);
  const [detailResult, setDetailResult] = useState<BacktestResult | null>(null);
  const decisionRequestKey = useRef<string | null>(null);
  const decisionPageRef = useRef<BacktestDecisionPage | null>(null);
  const restoredEstimateKey = useRef<string | null>(null);
  const localEstimateKey = useRef<string | null>(null);
  const localTimeZone = useMemo(() => localTimeZoneLabel(), []);
  useEffect(() => { decisionPageRef.current = decisionPage; }, [decisionPage]);
  const configuredTimeouts = useMemo(() => [
    ...new Set(
      form.providers.map((name) => {
        const provider = providers.find((item) => item.provider === name);
        return provider?.capabilities.external_inference ? provider.timeout_seconds : undefined;
      })
        .filter((seconds): seconds is number => seconds !== undefined),
    ),
  ], [form.providers, providers]);
  const providersRequiringProbe = useMemo(() => form.providers.filter((name) =>
    providers.find((item) => item.provider === name)?.capabilities.requires_backtest_probe,
  ), [form.providers, providers]);
  const timeoutPlaceholder = configuredTimeouts.length === 1
    ? `留空默认 ${configuredTimeouts[0]}s`
    : configuredTimeouts.length > 1
      ? "各模型默认值不同，请填写"
      : "留空使用 Provider 默认值";

  const body = useCallback(() => ({
    symbols: form.symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
    cadences: form.cadences,
    start: parseLocalDateTime(form.start).toISOString(),
    end: parseLocalDateTime(form.end).toISOString(),
    providers: form.providers,
    ...(replayLiveRunId ? { replay_live_run_id: Number(replayLiveRunId) } : {}),
    ...(timeout.trim() ? { timeout_seconds: Number(timeout) } : {}),
    config: {
      initial_equity: form.initialEquity,
      fee_rate: form.feeRate,
      slippage_fraction: form.slippage,
    },
  }), [form, replayLiveRunId, timeout]);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await api<BacktestRun[]>("/api/backtests?limit=10"));
    } catch { /* the list is not worth an error banner */ }
  }, []);

  useEffect(() => { void refreshRuns(); }, [refreshRuns]);

  useEffect(() => {
    api<ReplayableFormalRun[]>("/api/backtests/formal-runs?limit=50")
      .then(setFormalRuns)
      .catch(() => undefined);
  }, []);

  // Poll only while something is unfinished, so an idle frontend stays quiet.
  useEffect(() => {
    if (!runs.some((run) => run.status === "running")) return;
    const timer = window.setInterval(() => void refreshRuns(), 3000);
    return () => window.clearInterval(timer);
  }, [runs, refreshRuns]);

  // The estimate is stale the moment the spec changes; showing an old one
  // beside a new window is worse than showing none.
  useEffect(() => {
    setEstimate(null);
    setAutoEstimatePending(false);
  }, [form, replayLiveRunId]);

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
      setAutoEstimatePending(false);
      await api("/api/backtests/probe/cancel", { method: "POST" });
      await refreshProbe();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const startProbe = async () => {
    setBusy("probe"); setError(null);
    // Clear the previous completed probe before the POST yields. Otherwise the
    // pending flag can render against stale successful rows and request an
    // estimate while the server has already cleared them for the new probe.
    setProbe(null);
    setAutoEstimatePending(false);
    restoredEstimateKey.current = null;
    try {
      await api("/api/backtests/probe", { method: "POST", body: JSON.stringify(body()) });
      await refreshProbe();
      setAutoEstimatePending(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(null); }
  };

  const runEstimate = useCallback(async (surfaceError = true) => {
    setBusy("estimate");
    if (surfaceError) setError(null);
    try {
      setEstimate(await api<BacktestEstimate>("/api/backtests/estimate", {
        method: "POST", body: JSON.stringify(body()),
      }));
      setError(null);
    } catch (reason) {
      if (surfaceError) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally { setBusy(null); }
  }, [body]);

  // A successful probe already has every latency sample needed by the
  // estimate. Requiring a second click adds no choice, so complete the
  // preflight automatically. Parameter edits clear the pending flag above,
  // preventing a completed old probe from being applied to a new spec.
  useEffect(() => {
    if (!autoEstimatePending || !probe || probe.running) return;
    setAutoEstimatePending(false);
    const complete = probe.providers.length > 0 && probe.providers.every((item) =>
      item.done
      && item.error === null
      && item.failures === 0
      && item.calls.length === probe.decisions
    );
    if (complete) void runEstimate(true);
  }, [autoEstimatePending, probe, runEstimate]);

  // Probe samples live in the backend process while the estimate card is UI
  // state. After a refresh, recover the card when the completed providers and
  // current request still match. A stale probe is expected and rejected by
  // the API, so recovery failures stay silent; the request fingerprint keeps
  // them from being retried on every render.
  useEffect(() => {
    if (autoEstimatePending || estimate || !probe || probe.running || !form.providers.length) {
      return;
    }
    const complete = probe.providers.length === providersRequiringProbe.length
      && probe.providers.every((item) =>
        providersRequiringProbe.includes(item.provider)
        && item.done
        && item.error === null
        && item.failures === 0
        && item.calls.length === probe.decisions
      );
    if (!complete) return;
    let requestKey: string;
    try {
      requestKey = JSON.stringify({
        request: body(),
        samples: probe.providers.map((item) => ({
          provider: item.provider,
          calls: item.calls.map((call) => [call.seconds, call.ok]),
        })),
      });
    } catch {
      return;
    }
    if (restoredEstimateKey.current === requestKey) return;
    restoredEstimateKey.current = requestKey;
    void runEstimate(false);
  }, [autoEstimatePending, body, estimate, form.providers, probe, providersRequiringProbe, runEstimate]);

  // A deterministic local provider has no network latency to sample. Estimate
  // it directly whenever the current form contains only providers that declare
  // probing unnecessary.
  useEffect(() => {
    if (!form.providers.length || providersRequiringProbe.length || estimate || busy !== null) {
      return;
    }
    let requestKey: string;
    try {
      requestKey = JSON.stringify(body());
    } catch {
      return;
    }
    if (localEstimateKey.current === requestKey) return;
    localEstimateKey.current = requestKey;
    void runEstimate(false);
  }, [body, busy, estimate, form.providers.length, providersRequiringProbe.length, runEstimate]);

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
      decisionRequestKey.current = null;
      setOpenDecisions(null);
      setDecisionPage(null);
      setDetailResult(null);
      return;
    }
    // Clear first: showing the previous model's decisions under a new header
    // while the fetch lands is worse than showing nothing.
    decisionRequestKey.current = key;
    setOpenDecisions(key);
    setDecisionPage(null);
    setDetailResult(null);
    try {
      const [loadedDecisions, detailedRun] = await Promise.all([
        api<BacktestDecisionPage>(
          `/api/backtests/${runId}/decisions?provider=${encodeURIComponent(provider)}`,
        ),
        api<BacktestRun>(`/api/backtests/${runId}`),
      ]);
      if (decisionRequestKey.current !== key) return;
      setDecisionPage(loadedDecisions);
      setDetailResult(
        detailedRun.models.find((model) => model.provider === provider)?.result ?? null,
      );
    } catch (reason) {
      if (decisionRequestKey.current !== key) return;
      setDecisionPage({ items: [], total: 0, has_more: false, next_after_id: null });
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const loadMoreBacktestDecisions = async () => {
    if (!openDecisions || !decisionPage?.has_more || decisionPage.next_after_id === null) return;
    const active = runs.find((run) =>
      run.models.some((model) => `${run.id}-${model.provider}` === openDecisions),
    );
    const provider = active?.models.find(
      (model) => `${active.id}-${model.provider}` === openDecisions,
    )?.provider;
    if (!active || !provider) return;
    const key = openDecisions;
    setDecisionsLoadingMore(true);
    try {
      const loaded = await api<BacktestDecisionPage>(
        `/api/backtests/${active.id}/decisions?provider=${encodeURIComponent(provider)}&after_id=${decisionPage.next_after_id}`,
      );
      if (decisionRequestKey.current !== key) return;
      setDecisionPage((current) => {
        const merged = mergeBacktestDecisionPages(current, loaded);
        decisionPageRef.current = merged;
        return merged;
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDecisionsLoadingMore(false);
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
    if (!provider || !decisionPageRef.current || active.status !== "running") return;
    const key = openDecisions;
    const refresh = async () => {
      try {
        const afterId = decisionPageRef.current?.items.at(-1)?.id ?? 0;
        const [loadedDecisions, detailedRun] = await Promise.all([
          api<BacktestDecisionPage>(
            `/api/backtests/${active.id}/decisions?provider=${encodeURIComponent(provider)}&after_id=${afterId}`,
          ),
          api<BacktestRun>(`/api/backtests/${active.id}`),
        ]);
        if (decisionRequestKey.current !== key) return;
        setDecisionPage((current) => {
          const merged = mergeBacktestDecisionPages(current, loadedDecisions);
          decisionPageRef.current = merged;
          return merged;
        });
        setDetailResult(
          detailedRun.models.find((model) => model.provider === provider)?.result ?? null,
        );
      } catch { /* progress polling will surface terminal run errors */ }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [decisionPage !== null, openDecisions, runs]);

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
      <PanelTitle code="05" title="回测" meta="历史模式 · 多模型对比" />

      <div className="backtest-note">
        <strong>回测不下单。</strong>它用历史行情重放同一套决策与风控，只有撮合是仿真的。
        <button className="text-button" onClick={() => setShowDiff((value) => !value)}>
          {showDiff ? "收起差异" : "与实盘的差异"}
        </button>
      </div>
      {showDiff && (
        <div className="table-wrap backtest-diff">
          <table>
            <thead><tr><th></th><th>正式运行（测试网）</th><th>正式运行回放</th><th>普通历史回测</th></tr></thead>
            <tbody>
              {BACKTEST_VS_LIVE.map((row) => (
                <tr key={row.aspect}>
                  <td><strong>{row.aspect}</strong></td>
                  <td>{row.live}</td>
                  <td>{row.replay}</td>
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
        <label className="replay-source">
          <span>数据来源</span>
          <select
            value={replayLiveRunId}
            disabled={busy !== null}
            onChange={(event) => {
              setReplayLiveRunId(event.target.value);
            }}
          >
            <option value="">自选历史窗口</option>
            {formalRuns.map((run) => (
              <option key={run.id} value={run.id}>
                正式运行 #{run.id} · {formatLocalDateTime(new Date(run.started_at))} · {run.snapshot_count} 条
              </option>
            ))}
          </select>
          <small>{replayLiveRunId
            ? "自动使用该次正式运行实际送入模型的行情、合约规则和真实起始账户；标的、周期、时间与初始权益由记录决定。之后仓位按回测模型自己的成交演化。"
            : "按下方标的、时间和周期从历史行情构建回测。"}</small>
        </label>
        <div className="backtest-form">
        <label><span>标的（逗号分隔，最多 5 个）</span>
          <input value={form.symbols} disabled={busy !== null || Boolean(replayLiveRunId)}
            onChange={(e) => setForm({ ...form, symbols: e.target.value })} />
        </label>
        <label><span>起（本地时间）</span>
          <input type="text" value={form.start} placeholder="YYYY/MM/DD HH:mm"
            inputMode="numeric" disabled={busy !== null || Boolean(replayLiveRunId)}
            onChange={(e) => setForm({ ...form, start: e.target.value })} />
        </label>
        <label><span>止（本地时间 · 最长 31 天，约 1 个月）</span>
          <input type="text" value={form.end} placeholder="YYYY/MM/DD HH:mm"
            inputMode="numeric" disabled={busy !== null || Boolean(replayLiveRunId)}
            onChange={(e) => setForm({ ...form, end: e.target.value })} />
        </label>
        <label><span>初始权益</span>
          <input value={form.initialEquity} disabled={busy !== null || Boolean(replayLiveRunId)}
            onChange={(e) => setForm({ ...form, initialEquity: e.target.value })} />
        </label>
        </div>

        <div className="backtest-picks">
        <div>
          <span className="eyebrow">周期（每多选一个，耗时增加一份）</span>
          <div className="chips">
            {DECISION_CADENCES.map((cadence) => (
              <button key={cadence} className={form.cadences.includes(cadence) ? "active" : ""}
                disabled={busy !== null || Boolean(replayLiveRunId)} onClick={() => toggle("cadences", cadence)}>{cadence}</button>
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
                <small>{provider.capabilities.external_inference
                  ? modelConfigSummary(provider.model, provider.reasoning_effort)
                  : `${provider.model} · 本地确定性`}</small>
              </button>
            ))}
          </div>
        </div>
        </div>
      </section>

      <div className="probe">
        <div className="probe-head">
          <strong>{providersRequiringProbe.length
            ? `试跑 ${probe?.decisions ?? 5} 次决策`
            : "本地策略无需试跑"}</strong>
          {providersRequiringProbe.length > 0 && <button
            className="compact"
            disabled={busy !== null || !form.providers.length || engineRunning || probe?.running}
            onClick={() => void startProbe()}
          >{probe?.running ? "试跑中…" : "开始试跑"}</button>}
          {probe?.running && (
            <button className="text-button danger-text" onClick={() => void cancelProbe()}>
              停止试跑
            </button>
          )}
          <small>{providersRequiringProbe.length ? <>
            用这个窗口的真实 payload 调每个外部模型 {probe?.decisions ?? 5} 次，量出它实际要多久。
            试跑期间超时放宽到 {probe?.ceiling_seconds ?? 180}s——用当前超时去试只会复现超时，
            量不出模型真正需要的时间。5 次全部成功后会自动估算耗时；修改参数后需要重新试跑。
            这几次是真实调用，会真实计费。
          </> : <>本地规则直接计算现有特征，没有网络超时、Token 或调用成本；参数变化后会自动重新估算。</>}</small>
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
            {item.done && !item.error && item.failures === 0
              && item.suggested_timeout_seconds !== null && (
              <button
                className="text-button"
                onClick={() => setTimeoutSeconds(String(item.suggested_timeout_seconds))}
              >建议 {item.suggested_timeout_seconds}s · 点击采用</button>
            )}
            {item.done && !item.error && item.failures > 0
              && item.failures < probe.decisions && (
              <span className="negative">
                {item.failures}/{probe.decisions} 次失败——估算和回测要求重新取得 5 次完整成功
              </span>
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
        {providersRequiringProbe.length > 0 && <label className="probe-timeout">
          <span>本次回测超时（秒）</span>
          <input
            type="number" min={1} placeholder={timeoutPlaceholder} title={timeoutPlaceholder}
            value={timeout} disabled={busy !== null}
            onChange={(event) => setTimeoutSeconds(event.target.value)}
          />
        </label>}
      </div>

      {estimate && (
        <div className={`backtest-estimate ${estimate.within_limit ? "" : "over"}`}>
          <span>每模型 <strong>{estimate.decisions_per_model}</strong> 次决策
            {estimate.calls_per_model !== estimate.decisions_per_model
              && <small> · {estimate.calls_per_model} 次批量调用</small>}
          </span>
          <span>共 <strong>{estimate.total_calls}</strong> 次 Provider 调用</span>
          <span>预计 <strong>{formatEstimatedDuration(estimate.estimated_seconds)}</strong>
            <small>
              按{estimate.latency_source === "local_deterministic" ? "本地计算基线" : "本次试跑平均决策最慢的模型"} {providerLabel(estimate.slowest_provider)}：平均
              {estimate.seconds_per_call}s · 上限 {estimate.max_hours}h
            </small></span>
          {!estimate.within_limit && <span className="negative">超出耗时上限，请缩短窗口</span>}
        </div>
      )}

      <div className="backtest-actions">
        {busy === "estimate" && <small className="backtest-blocked">试跑完成，正在自动估算耗时…</small>}
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
          <colgroup>
            <col className="run-col-id" />
            <col className="run-col-window" />
            <col className="run-col-model" />
            <col className="run-col-progress" />
            <col className="run-col-average" />
            <col className="run-col-token" />
            <col className="run-col-cost" />
            <col className="run-col-return" />
            <col className="run-col-win" />
            <col className="run-col-drawdown" />
            <col className="run-col-trades" />
            <col className="run-col-action" />
          </colgroup>
          <thead><tr><th>#</th><th>窗口</th><th>模型</th><th>进度</th>
            <th data-tooltip="该模型已成功返回的决策调用平均耗时；不含历史行情读取、撮合和数据库写入。">平均决策</th>
            <th data-tooltip="已完成模型调用返回的总 Token；运行中随 3 秒轮询更新。">Token</th>
            <th data-tooltip="按 Provider 返回成本或所选计费厂商价格折算；有任一调用无法定价时显示未知。">成本</th>
            <th>收益</th><th>胜率</th><th>回撤</th><th>交易</th><th></th></tr></thead>
          <tbody>
            {runs.flatMap((run) => run.models.map((model, index) => {
              const headline = backtestHeadline(run, model);
              const live = !model.result && headline !== null;
              return <tr key={`${run.id}-${model.provider}`}>
                {index === 0 && <td rowSpan={run.models.length} className="run-identity-cell">
                  <div className="run-identity">
                    <strong>#{run.id}</strong>
                    <small className={`run-status ${run.status}`}>{RUN_STATUS[run.status]}</small>
                  </div>
                </td>}
                {index === 0 && <td rowSpan={run.models.length}>
                  <small className="run-window">
                    <BacktestSymbolList symbols={run.spec.symbols} />
                    <span>{run.spec.cadences.join(" ")}</span>
                    <span><b>开始</b>{formatLocalDateTime(new Date(run.spec.start))}</span>
                    <span><b>结束</b>{formatLocalDateTime(new Date(run.spec.end))}</span>
                    {run.spec.requested_end && <span className="negative">
                      Provider 失效提前结束 · 原计划 {formatLocalDateTime(new Date(run.spec.requested_end))}
                    </span>}
                  </small>
                  {run.spec.replay_live_run_id
                    ? <small className="run-real">正式运行回放 #{run.spec.replay_live_run_id} · 精确决策数据</small>
                    : run.spec.use_recorded_book
                      ? <small className="run-real">旧真实回测记录 · 含订单流</small>
                      : <small>普通回测 · 无订单流</small>}
                  {run.spec.timeout_seconds
                    !== null
                    ? <small>
                        超时 {run.spec.timeout_seconds}s · {run.spec.timeout_source === "provider_config"
                          ? "继承配置"
                          : "本次指定"}
                      </small>
                    : <small>本地规则 · 超时不适用</small>}
                  <small className="run-timing" data-tooltip="任务从创建到结束的墙钟耗时；运行中随列表轮询继续计时。">
                    耗时 {backtestElapsed(run)}
                  </small>
                  <BacktestRemaining run={run} />
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
                      <small>{modelConfigSummary(model.model, model.reasoning_effort)}</small>
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
                <td className={headline && Number(headline.total_return) >= 0 ? "positive" : "negative"}
                  data-tooltip={live && model.live_result
                    ? `实时收益包含当前未平仓头寸按最新历史 mark 计算的未实现盈亏（${money(model.live_result.unrealized_pnl)}）。`
                    : undefined}>
                  {headline ? `${(Number(headline.total_return) * 100).toFixed(2)}%` : "—"}
                  {live && <small>实时 · 含未实现</small>}
                </td>
                <td data-tooltip={live ? "实时胜率只统计已经平仓的交易。" : undefined}>
                  {headline ? `${(Number(headline.win_rate) * 100).toFixed(0)}%` : "—"}</td>
                <td data-tooltip={live ? "截至当前逐决策权益曲线的最大回撤。" : undefined}>
                  {headline ? `${(Number(headline.max_drawdown) * 100).toFixed(2)}%` : "—"}</td>
                <td data-tooltip={live ? "实时交易数只统计已经平仓的交易。" : undefined}>
                  {headline ? headline.trade_count : "—"}</td>
                {index === 0 && <td rowSpan={run.models.length}>
                  {run.status === "running" && <button className="text-button danger-text" onClick={() => void cancel(run.id)}>取消</button>}
                  {run.status === "failed" && run.error && <small className="negative" title={run.error}>失败</small>}
                </td>}
              </tr>;
            }).concat(
              openDecisions?.startsWith(`${run.id}-`)
                ? [
                  <tr key={`${run.id}-decisions`} className="run-decisions">
                    <td colSpan={12}>
                      <BacktestResultDetail result={detailResult} />
                      <BacktestDecisionLog
                        page={decisionPage}
                        localTimeZone={localTimeZone}
                        loadingMore={decisionsLoadingMore}
                        onLoadMore={loadMoreBacktestDecisions}
                      />
                    </td>
                  </tr>,
                ]
                : [],
            ))}
            {!runs.length && <tr><td colSpan={12} className="empty">还没有回测。选好标的、窗口和决策 Provider；外部模型试跑后、本地规则直接自动估算耗时。</td></tr>}
          </tbody>
        </table>
      </div>
    </article>
  );
}

function mergeBacktestDecisionPages(
  current: BacktestDecisionPage | null,
  incoming: BacktestDecisionPage,
): BacktestDecisionPage {
  if (!current) return incoming;
  const known = new Set(current.items.map((item) => item.id));
  const items = [
    ...current.items,
    ...incoming.items.filter((item) => !known.has(item.id)),
  ];
  const hasMore = items.length < incoming.total;
  return {
    ...incoming,
    items,
    has_more: hasMore,
    next_after_id: hasMore ? items.at(-1)?.id ?? null : null,
  };
}

export function BacktestDecisionLog({
  page,
  localTimeZone,
  loadingMore,
  onLoadMore,
}: {
  page: BacktestDecisionPage | null;
  localTimeZone: string;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  if (page === null) return <span className="empty">读取中…</span>;
  if (!page.items.length) return <span className="empty">这个模型还没有决策记录。</span>;
  return <>
    <table className="decision-log">
      <thead><tr><th>历史时刻</th><th data-tooltip={`模型请求实际从本机发出的墙钟时间；按 ${localTimeZone} 显示。`}>实际调用</th><th>标的</th><th>结果</th><th>动作</th><th>置信</th><th>说明</th></tr></thead>
      <tbody>
        {page.items.map((item) => (
          <tr key={item.id}>
            <td>{formatLocalDateTime(new Date(item.decided_at))}</td>
            <td className="decision-call-times">
              {item.attempt_started_at.length
                ? <>
                  <span>首次 · {formatLocalDateTimeSeconds(new Date(item.attempt_started_at[0]))}</span>
                  {item.attempt_started_at.slice(1).map((startedAt, retry) => (
                    <small key={`${startedAt}-${retry}`}>重试 {retry + 1} · {formatLocalDateTimeSeconds(new Date(startedAt))}</small>
                  ))}
                  {item.attempt_started_at.length > 1
                    && <em>共重试 {item.attempt_started_at.length - 1} 次</em>}
                </>
                : <span>未调用模型</span>}
            </td>
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
    </table>
    <div className="decision-more">
      <span className="decision-more-note">已加载 {page.items.length} / {page.total} 条决策</span>
      {page.has_more && <button className="text-button" disabled={loadingMore} onClick={onLoadMore}>
        {loadingMore ? "加载中…" : "加载更多"}
      </button>}
    </div>
  </>;
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
  failed: "失败",
  cancelled: "已取消",
};

function formatLocalDateTime(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${String(date.getFullYear()).padStart(4, "0")}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatLocalDateTimeSeconds(date: Date): string {
  const seconds = String(date.getSeconds()).padStart(2, "0");
  return `${formatLocalDateTime(date)}:${seconds}`;
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

export function CollapsiblePanel({
  code,
  title,
  meta,
  className = "",
  expanded,
  onExpandedChange,
  children,
}: {
  code: string;
  title: string;
  meta: string;
  className?: string;
  expanded: boolean;
  onExpandedChange: (expanded: boolean) => void;
  children: ReactNode;
}) {
  const contentId = useId();
  return <article className={`panel collapsible-panel ${className}`}>
    <button
      type="button"
      className="collapsible-panel-toggle"
      aria-expanded={expanded}
      aria-controls={contentId}
      onClick={() => onExpandedChange(!expanded)}
    >
      <PanelTitle code={code} title={title} meta={meta} />
      <span className="collapsible-panel-icon" aria-hidden="true">{expanded ? "−" : "+"}</span>
    </button>
    {expanded && <div className="collapsible-panel-content" id={contentId}>{children}</div>}
  </article>;
}

function RiskItem({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="risk-item" data-tooltip={RISK_DEFINITIONS[label]}><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>;
}

export function formatDailyLossPercent(fraction: string): string {
  return `${(Number(fraction) * 100).toFixed(1)}%`;
}

export function StructureGateSummaryCard({ summary }: { summary: StructureGateSummary | null }) {
  const modeLabel = summary?.mode === "enforce" ? "强制" : summary?.mode === "off" ? "已关闭" : "SHADOW";
  if (!summary || summary.sample_size === 0) {
    return <div className="structure-gate-summary empty">
      <div><strong>结构门槛 · {modeLabel}</strong><span>等待开仓样本</span></div>
      <small>{summary?.mode === "enforce"
        ? "当前会拒绝未通过项；产生经过其他实时硬风控的开仓或加仓后开始统计。"
        : summary?.mode === "off"
          ? "当前未执行结构评估。"
          : "只观察，不改变订单；产生经过其他实时硬风控的开仓或加仓后开始统计。"}</small>
    </div>;
  }
  return <div className="structure-gate-summary">
    <div>
      <strong>结构门槛 · {modeLabel}</strong>
      <span>{summary.passed}/{summary.sample_size} 全项通过 · {(summary.pass_rate! * 100).toFixed(0)}%</span>
    </div>
    <ul>
      {summary.checks.map((check) => <li key={check.key}>
        <span>{STRUCTURE_CHECK_LABELS[check.key] ?? check.key}</span>
        <strong>{(check.pass_rate * 100).toFixed(0)}%</strong>
        <small>{check.passed}/{check.evaluated}</small>
      </li>)}
    </ul>
    <small>统计最近 {summary.scanned} 条风控记录中的 {summary.sample_size} 个结构评估；{summary.mode === "enforce"
      ? "当前未通过项会被拒绝。"
      : summary.mode === "off" ? "当前门槛已关闭，显示的是历史结果。" : "当前结果不参与拦截。"}</small>
  </div>;
}

function money(value: string): string {
  return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

const DECISION_FILTERS: Array<{ key: DecisionFilter; label: string }> = [
  { key: "all", label: "全部" },
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

function decisionOutcomeLabel(decision: DecisionEvent): string {
  if (decision.risk?.decision.pending_entry) return "等待触发";
  return decision.failover ? "Provider 调用失败" : OUTCOME_LABELS[decision.outcome];
}

function pendingExpiryLabel(decision: DecisionEvent): string {
  const expiresAt = decision.risk?.decision.pending_expires_at;
  return expiresAt ? formatLocalDateTimeSeconds(new Date(expiresAt)) : "—";
}

function intentPrice(value: string | null): string {
  return value === null ? "—" : Number(value).toFixed(4);
}

export function intentRewardRiskRatio(intent: DecisionEvent["intent"]): number | null {
  if (intent.entry_price === null || intent.stop_loss === null || intent.take_profit === null) {
    return null;
  }
  const entry = Number(intent.entry_price);
  const stopLoss = Number(intent.stop_loss);
  const takeProfit = Number(intent.take_profit);
  if (![entry, stopLoss, takeProfit].every(Number.isFinite)) return null;

  const direction = intent.action === "OPEN_SHORT" ? -1
    : intent.action === "OPEN_LONG" ? 1
      : takeProfit > entry && stopLoss < entry ? 1
        : takeProfit < entry && stopLoss > entry ? -1 : 0;
  const reward = (takeProfit - entry) * direction;
  const risk = (entry - stopLoss) * direction;
  return direction !== 0 && reward > 0 && risk > 0 ? reward / risk : null;
}

function intentRewardRiskLabel(intent: DecisionEvent["intent"]): string {
  const ratio = intentRewardRiskRatio(intent);
  return ratio === null ? "—" : `${ratio.toFixed(2)} : 1`;
}

function preTradeRewardRiskLabel(decision: DecisionEvent): string {
  const rawValue = decision.risk?.decision.pre_trade_reward_risk_ratio;
  if (rawValue == null) return "—";
  const value = Number(rawValue);
  return Number.isFinite(value) ? `${value.toFixed(4)} : 1` : "—";
}

function executionPrice(value: string | null | undefined): string {
  return value == null ? "—" : Number(value).toFixed(4);
}

function executionLoss(value: string | null | undefined): string {
  return value == null
    ? "—"
    : `$${Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`;
}

function executionSizing(decision: DecisionEvent): {
  margin: number;
  notional: number;
} | null {
  if (decision.outcome !== "executed" || decision.execution?.entry_report == null) return null;
  const quantity = Number(decision.execution.entry_report.filled_quantity);
  const price = Number(decision.execution.entry_report.average_price);
  const leverage = Number(decision.intent.leverage);
  if (![quantity, price, leverage].every(Number.isFinite) || quantity <= 0 || price <= 0 || leverage <= 0) {
    return null;
  }
  const notional = quantity * price;
  return { notional, margin: notional / leverage };
}

function ExecutionSizing({ decision }: { decision: DecisionEvent }) {
  const sizing = executionSizing(decision);
  if (sizing === null) return null;
  return <>
    <span data-tooltip="按实际入场成交数量 × 实际成交均价计算。">成交额<strong>{money(String(sizing.notional))} USDT</strong></span>
    <span data-tooltip="按实际成交额 ÷ 本次杠杆计算的初始保证金估算值；不包含手续费。">保证金<strong>{money(String(sizing.margin))} USDT</strong></span>
  </>;
}

function positionProtectionMetrics(position: AccountPosition): {
  takeProfitPercent: number | null;
  stopLossPercent: number | null;
  riskRewardRatio: number | null;
} {
  const entry = Number(position.average_price);
  if (!Number.isFinite(entry) || entry <= 0) {
    return { takeProfitPercent: null, stopLossPercent: null, riskRewardRatio: null };
  }
  const direction = position.side === "SHORT" ? -1 : 1;
  const takeProfit = position.take_profit === null ? null : Number(position.take_profit);
  const stopLoss = position.stop_loss === null ? null : Number(position.stop_loss);
  const takeProfitPercent = takeProfit !== null && Number.isFinite(takeProfit)
    ? direction * ((takeProfit - entry) / entry) * 100
    : null;
  const stopLossPercent = stopLoss !== null && Number.isFinite(stopLoss)
    ? direction * ((stopLoss - entry) / entry) * 100
    : null;
  const riskRewardRatio = takeProfitPercent !== null
    && stopLossPercent !== null
    && takeProfitPercent > 0
    && stopLossPercent < 0
    ? takeProfitPercent / Math.abs(stopLossPercent)
    : null;
  return { takeProfitPercent, stopLossPercent, riskRewardRatio };
}

function signedPositionPercent(value: number | null): string {
  if (value === null) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

const LIVE_RUN_STATUS: Record<NonNullable<DecisionEvent["live_run"]>["status"], string> = {
  running: "运行中",
  stopped: "已停止",
  auto_stopped: "自动停止",
  emergency_stopped: "紧急停止",
  interrupted: "进程中断",
};

function groupDecisionEvents(decisions: DecisionEvent[]) {
  return decisions.reduce<Array<{
    key: string;
    run: NonNullable<DecisionEvent["live_run"]>;
    decisions: DecisionEvent[];
  }>>((groups, decision) => {
    if (decision.live_run_id === null || decision.live_run === null) return groups;
    const key = `run-${decision.live_run_id}`;
    const last = groups.at(-1);
    if (last?.key === key) {
      last.decisions.push(decision);
    } else {
      groups.push({ key, run: decision.live_run, decisions: [decision] });
    }
    return groups;
  }, []);
}

function DecisionRunHeader({
  run,
  decisionCount,
  performance,
}: {
  run: NonNullable<DecisionEvent["live_run"]>;
  decisionCount: number;
  performance: LiveRunPerformance | undefined;
}) {
  const config = [
    run.config.cadences?.join(" / "),
    run.config.provider_chain?.map(providerLabel).join(" → "),
  ].filter(Boolean).join(" · ");
  return <summary className={`decision-run-header ${run.status}`}>
    <span className="decision-run-primary">
      <strong>
        正式运行 #{run.id} · {LIVE_RUN_STATUS[run.status]}
        <em className="decision-run-version">版本 {run.config.software_version ?? "未记录"}</em>
      </strong>
      <small>{formatLocalDateTimeSeconds(new Date(run.started_at))}{run.ended_at ? ` → ${formatLocalDateTimeSeconds(new Date(run.ended_at))}` : " → 现在"}</small>
    </span>
    <span className="decision-run-summary">
      <span className="decision-run-performance">
        <span data-tooltip="价格已实现 + 未实现 - 可归属手续费；资金费无法可靠归属到单次运行时不混入总额。">
          交易净盈亏<strong className={performance?.total_pnl !== null && Number(performance?.total_pnl) < 0 ? "negative" : "positive"}>
            {performance?.total_pnl === null || performance === undefined
              ? "—"
              : `${Number(performance.total_pnl) > 0 ? "+" : ""}${money(performance.total_pnl)} USDT`}
          </strong>
        </span>
        <span data-tooltip="价格毛利是交易所已实现盈亏；手续费只汇总 USDT 且可归属的入场/退出成交；资金费未知时明确显示未知。">
          拆分<strong>{performance === undefined
            ? "—"
            : `价格 ${money(performance.gross_price_pnl)} · 未实现 ${money(performance.unrealized_pnl)} · 手续费 ${performance.commission_complete ? money(performance.commissions) : `${money(performance.commissions)}+未知`} · 资金费 ${performance.funding_complete && performance.funding_pnl !== null ? money(performance.funding_pnl) : "未知"}`}</strong>
        </span>
        <span data-tooltip="盈利平仓笔数除以该运行已完成的平仓笔数；运行停止后的手动平仓仍按仓位归属计入，没有平仓时显示 —。">
          已平仓胜率<strong>{performance?.win_rate === null || performance === undefined
            ? "—"
            : `${(Number(performance.win_rate) * 100).toFixed(0)}% (${performance.wins}/${performance.closed_trades})`}</strong>
        </span>
        <span data-tooltip="当前交易所账户中由该运行开仓且尚有剩余数量的标的数；同一标的多次开仓或加仓只计一个，停止后手动平仓会实时减少。">
          未平仓<strong>{performance?.open_position_count ?? "—"}</strong>
        </span>
      </span>
      <small className="decision-run-config">{[config, `${decisionCount} 条决策`].filter(Boolean).join(" · ")}</small>
      {run.stop_reason && <small className="decision-run-stop">停止原因：{run.stop_reason}</small>}
    </span>
    <i className="decision-run-toggle" aria-hidden="true" />
  </summary>;
}

function DecisionRunGroup({
  run,
  decisionCount,
  performance,
  children,
}: {
  run: NonNullable<DecisionEvent["live_run"]>;
  decisionCount: number;
  performance: LiveRunPerformance | undefined;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(run.status === "running");
  return <details
    className="decision-run-group"
    open={open}
    onToggle={(event) => setOpen(event.currentTarget.open)}
  >
    <DecisionRunHeader decisionCount={decisionCount} performance={performance} run={run} />
    {children}
  </details>;
}

export function DecisionPanel({
  decisions,
  liveRunPerformance,
  filter,
  onFilter,
  onLoadOlder,
  exhausted,
}: {
  decisions: DecisionEvent[];
  liveRunPerformance: LiveRunPerformance[];
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
  const performanceByRun = new Map(
    liveRunPerformance.map((performance) => [performance.live_run_id, performance]),
  );

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
        {groupDecisionEvents(visible).map((group) => (
          <DecisionRunGroup
            decisionCount={group.decisions.length}
            key={group.key}
            performance={performanceByRun.get(group.run.id)}
            run={group.run}
          >
            {group.decisions.map((decision) => (
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
              {decision.failover ? <span className="signal-confidence residual" data-tooltip="所选 Provider 的调用失败记录；系统只会重试同一 Provider。">
                失败<small>{decision.failover.continues ? "继续重试" : "重试耗尽"}</small>
              </span> : <span
                className={`signal-confidence ${decision.intent.action === "HOLD" ? "residual" : ""}`}
                data-tooltip={decision.intent.action === "HOLD"
                  ? "HOLD 时表示当前快照仍残留的交易机会强度，不是盈利概率，也不代表模型输出可靠性。"
                  : "模型对该非 HOLD 方向在当前快照下具备可执行交易优势的估计；不是盈利概率，且不能绕过硬风控。"}
              >
                {Math.round(decision.intent.confidence * 100)}%
                <small>{decision.intent.action === "HOLD" ? "机会强度" : "执行置信度"}</small>
              </span>}
              <span className={`decision-outcome ${decision.risk?.decision.pending_entry ? "pending" : decision.outcome}`}>
                {decisionOutcomeLabel(decision)}
                {decision.outcome === "execution_failed" && decision.execution?.estimated_loss_usdt != null
                  ? <small>损失 {executionLoss(decision.execution.estimated_loss_usdt)}</small>
                  : null}
              </span>
              <DecisionTiming decision={decision} />
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
                  <span data-tooltip="仅按 AI 返回的入场价、止损和止盈计算，不含交易所 tick 对齐或最新行情。">AI 原始盈亏比<strong>{intentRewardRiskLabel(decision.intent)}</strong></span>
                  <span data-tooltip="硬风控按下单前刷新行情得到的实际入场基准，以及对齐交易所精度后的止损和止盈计算；这是最低 1.3:1 边界真正校验的数值。">下单前盈亏比<strong>{preTradeRewardRiskLabel(decision)}</strong></span>
                  <span data-tooltip="后端硬风控根据止损风险、保证金上限和交易所数量规则计算的最终允许下单数量。">最终下单数量<strong>{decision.risk?.decision.max_quantity ?? "—"}</strong></span>
                  {decision.intent.decision_framework === "structure-v1" ? <>
                    <span>结构形态<strong>{decision.intent.setup_type ?? "—"}</strong></span>
                    <span>结构锚点<strong>{decision.intent.anchor_timeframe ?? "—"} · {intentPrice(decision.intent.anchor_price ?? null)}</strong></span>
                    <span>入场触发<strong>{decision.intent.trigger_type ?? "—"} · {intentPrice(decision.intent.trigger_price ?? null)}</strong></span>
                    <span>失效依据<strong>{decision.intent.invalidation_type ?? "—"} · {intentPrice(decision.intent.invalidation_level ?? null)}</strong></span>
                    <span>目标依据<strong>{decision.intent.target_type ?? "—"}</strong></span>
                  </> : null}
                  {decision.risk?.decision.pending_expires_at ? <span data-tooltip="本地待触发意图不会预先提交到交易所；截止前每次检查都会重新获取行情与账户并完整复跑硬风控。过期后该时间仍保留用于审计。">意图有效至<strong>{pendingExpiryLabel(decision)}</strong></span> : null}
                  {decision.risk?.decision.take_profit_reentry_assessment ? <span data-tooltip="纯影子评估，不会否决或修改订单；用于比较止盈后等待 15、30、60 分钟是否改善后续净收益。">止盈后重入 · SHADOW<strong>{Math.floor(decision.risk.decision.take_profit_reentry_assessment.elapsed_seconds / 60)} 分钟 · 会被 {decision.risk.decision.take_profit_reentry_assessment.would_block_minutes.join("/")} 分钟窗口拦截</strong></span> : null}
                </div>
                {decision.risk?.decision.structure_assessment && (
                  <div className={`structure-assessment ${decision.risk.decision.structure_assessment.passed ? "passed" : "failed"}`}>
                    <div>
                      <strong>结构入场门槛 · {decision.risk.decision.structure_assessment.mode === "shadow" ? "SHADOW" : "强制"}</strong>
                      <span>{decision.risk.decision.structure_assessment.passed ? "全部通过" : "存在未通过项"}</span>
                    </div>
                    <ul>
                      {decision.risk.decision.structure_assessment.checks.map((check) => (
                        <li className={check.passed ? "passed" : "failed"} key={check.key}>
                          <i>{check.passed ? "✓" : "×"}</i>
                          <span><strong>{check.key}</strong><small>{check.detail}</small></span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <div className={`decision-reason ${decision.risk?.decision.pending_entry ? "pending" : decision.outcome}`}>
                  <strong>{decision.risk?.decision.pending_entry ? "本地待触发" : decision.failover ? "Provider 调用失败" : decision.risk?.accepted ? "风控放行" : OUTCOME_LABELS[decision.outcome]}</strong>
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
                      <ExecutionSizing decision={decision} />
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
          </DecisionRunGroup>
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
        <span>{detail.provider} · {detail.model ?? "CLI 默认"} · 批次耗时 {(detail.duration_ms / 1000).toFixed(2)}s</span>
      </div>
      <div className="analysis-usage-grid">
        <span>未缓存输入<strong>{Number(usage.input_tokens ?? 0).toLocaleString("zh-CN")}</strong></span>
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
          <details className="analysis-block" key={block.key}>
            <summary>
              <strong><i aria-hidden="true" />{block.title}</strong>
              {block.value !== null && (
                <button onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  void onCopy(block.key, block.value ?? "");
                }}>
                  {copied === block.key ? "已复制" : "复制"}
                </button>
              )}
            </summary>
            {block.value === null
              ? <p>{missingAuditMessage}</p>
              : <pre>{block.value}</pre>}
          </details>
        ))}
      </div>
    </section>
  );
}

const TRAILING_STATUS_LABELS: Record<TrailingStopEvent["status"], string> = {
  shadow: "影子候选",
  simulated_filled: "模拟成交",
  applied: "已应用",
  missed: "已错过",
  failed: "失败",
};

export function TrailingStopPanel({
  status,
  events,
  error,
}: {
  status: NonNullable<EngineStatus["scheduler"]["trailing_stop"]> | null;
  events: TrailingStopEvent[];
  error: string | null;
}) {
  const mode = status?.mode ?? "off";
  return <article className="panel trailing-panel">
    <PanelTitle
      code="06B"
      title="移动止损观测"
      meta={`${mode.toUpperCase()} · ${mode === "shadow" ? `只记录，不改单 · 模拟成交 ${status?.simulated_fills ?? 0}` : mode === "live" ? "交易所止损生效" : "已关闭"}`}
    />
    <p className="trailing-note">
      Shadow 同时计算多组参数，候选价只写入本地审计；Live 始终只运行明确的单一策略，避免多组候选争抢交易所止损。
    </p>
    <div className="trailing-strategies">
      {(status?.strategies ?? []).map((strategy) => {
        const profileEvents = events.filter(
          (item) => item.event.profile_id === strategy.profile_id,
        );
        const latest = profileEvents[0];
        return <div className="trailing-strategy" key={strategy.profile_id}>
          <span>{strategy.profile_id}</span>
          <strong>{profileEvents.length}</strong>
          <small>
            {latest
              ? `${latest.symbol.replace("USDT", "")} · 候选 ${executionPrice(latest.event.candidate_stop)}`
              : `+${strategy.activation_r}R 激活 · 回撤 ${strategy.distance_r}R`}
          </small>
        </div>;
      })}
      {!status?.strategies.length && <div className="empty cards">移动止损已关闭</div>}
    </div>
    {error && <div className="operations-error">移动止损记录暂不可用：{error}</div>}
    <div className="table-wrap trailing-table">
      <table>
        <thead><tr><th>时间</th><th>策略</th><th>标的</th><th>入场 / 标记</th><th>原始 / 当前止损</th><th>候选止损</th><th>结果</th></tr></thead>
        <tbody>
          {events.map((item) => <tr key={item.id}>
            <td><small>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</small></td>
            <td><strong>{item.event.profile_id ?? "系统"}</strong><small>{item.mode.toUpperCase()}</small></td>
            <td><strong>{item.symbol.replace("USDT", "")}</strong><small className={item.event.side === "LONG" ? "positive" : "negative"}>{item.event.side === "LONG" ? "多仓" : "空仓"}</small></td>
            <td>{executionPrice(item.event.entry_price)} / {executionPrice(item.event.mark_price)}</td>
            <td>{executionPrice(item.event.original_stop)} / {executionPrice(item.event.previous_stop)}</td>
            <td className="accent">{executionPrice(item.event.candidate_stop)}{item.event.simulated_fill_price && <small>观察 {executionPrice(item.event.simulated_fill_price)}</small>}</td>
            <td><span className={`trailing-result ${item.status}`}>{TRAILING_STATUS_LABELS[item.status]}</span>{item.event.detail && <small title={item.event.detail}>{item.event.detail}</small>}</td>
          </tr>)}
          {!events.length && <tr><td colSpan={7} className="empty">尚无候选记录；持仓达到最早激活阈值后会自动显示。</td></tr>}
        </tbody>
      </table>
    </div>
  </article>;
}

const PARTIAL_TAKE_PROFIT_STATUS_LABELS: Record<PartialTakeProfitEvent["status"], string> = {
  partial_simulated_filled: "部分止盈成交",
  breakeven_simulated_filled: "剩余保本成交",
  position_closed: "随实仓结束",
  unviable: "数量不可执行",
};

export function PartialTakeProfitPanel({
  status,
  events,
  error,
}: {
  status: NonNullable<EngineStatus["scheduler"]["partial_take_profit"]> | null;
  events: PartialTakeProfitEvent[];
  error: string | null;
}) {
  const strategies = status?.strategies ?? [
    { profile_id: "1R / 25% + BE", target_r: "1", fraction: "0.25", move_remainder_to_breakeven: true },
    { profile_id: "1R / 50% + BE", target_r: "1", fraction: "0.50", move_remainder_to_breakeven: true },
  ];
  return <article className="panel account-wide trailing-panel">
    <PanelTitle
      code="06C"
      title="部分止盈影子实验"
      meta={`SHADOW · 只记录，不改单 · 当前仓位：部分成交 ${status?.partial_fills ?? 0} · 保本成交 ${status?.breakeven_fills ?? 0}`}
    />
    <p className="trailing-note">
      同时比较 1R 止盈 25% 与 50%，剩余仓位按入场价模拟保本止损；数量先按交易所市价步长向下取整，结果不含手续费与资金费。
    </p>
    {error && <div className="operations-error">部分止盈记录暂不可用：{error}</div>}
    <div className="trailing-strategies">
      {strategies.map((strategy) => {
        const fills = events.filter((item) => item.event.profile_id === strategy.profile_id);
        return <div className="trailing-strategy" key={strategy.profile_id}>
          <span>{strategy.profile_id}</span>
          <strong>{fills.length}</strong>
          <small>+{strategy.target_r}R 触发 · 止盈 {(Number(strategy.fraction) * 100).toFixed(0)}% · 余仓保本</small>
        </div>;
      })}
    </div>
    <div className="table-wrap trailing-table">
      <table>
        <thead><tr><th>时间</th><th>策略</th><th>标的</th><th>入场 / 观察</th><th>目标 / 模拟成交</th><th>数量</th><th>模拟毛利</th><th>结果</th></tr></thead>
        <tbody>
          {events.map((item) => <tr key={item.id}>
            <td><small>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</small></td>
            <td><strong>{item.event.profile_id}</strong><small>SHADOW</small></td>
            <td><strong>{item.symbol.replace("USDT", "")}</strong><small className={item.event.side === "LONG" ? "positive" : "negative"}>{item.event.side === "LONG" ? "多仓" : "空仓"}</small></td>
            <td>{executionPrice(item.event.entry_price)} / {executionPrice(item.event.observed_mark_price)}</td>
            <td className="accent">{executionPrice(item.event.target_price)} / {executionPrice(item.event.simulated_fill_price)}</td>
            <td>{item.event.fill_quantity ?? "—"}<small>余 {item.event.remaining_quantity ?? "—"}</small></td>
            <td className={Number(item.event.strategy_gross_pnl ?? item.event.fill_gross_pnl ?? 0) >= 0 ? "positive" : "negative"}>
              {item.event.strategy_gross_pnl ?? item.event.fill_gross_pnl ?? "—"}
              <small>USDT · 未扣费</small>
            </td>
            <td><span className={`trailing-result ${item.status}`}>{PARTIAL_TAKE_PROFIT_STATUS_LABELS[item.status]}</span>{item.event.detail && <small title={item.event.detail}>{item.event.detail}</small>}</td>
          </tr>)}
          {!events.length && <tr><td colSpan={8} className="empty">尚无部分止盈记录；持仓首次达到 1R 后会自动显示。</td></tr>}
        </tbody>
      </table>
    </div>
  </article>;
}

export function AccountPanel({
  portfolio,
  positions,
  fills,
  testnetStatus,
  engineRunning,
  busy,
  onClosePosition,
}: {
  portfolio: AccountPortfolio | null;
  positions: AccountPosition[];
  fills: TradeFillRecord[];
  testnetStatus: TestnetAccountStatus | null;
  engineRunning: boolean;
  busy: string | null;
  onClosePosition: (symbol: string) => Promise<boolean>;
}) {
  const [confirmCloseSymbol, setConfirmCloseSymbol] = useState<string | null>(null);
  const displayedPnl = portfolio?.pnl_24h ?? null;
  const reconciliation = testnetStatus?.reconciliation;
  const testnetSafe = reconciliation !== null
    && reconciliation !== undefined
    && reconciliation.unprotected_symbols.length === 0;
  return (
    <article className="panel account-panel">
      <PanelTitle
        code="06"
        title="账户与订单"
        meta="币安测试网账户 · 可手动平仓"
      />
      <div className="account-testnet-status">
        <div className="testnet-heading">
          <div>
            <strong>{testnetStatus?.enabled ? "账户已配置" : "未配置"}</strong>
            <small>{testnetStatus?.active ? "当前交易模式" : "当前未启用测试网交易模式"}</small>
          </div>
          <span className={`status-pill ${testnetStatus?.enabled ? "ok" : "off"}`}>
            {testnetStatus?.enabled ? "TESTNET" : "DISABLED"}
          </span>
        </div>
        <div className="testnet-checks">
          <span><i className={testnetStatus?.user_stream.running ? "ok" : ""} />用户流 {testnetStatus?.user_stream.running ? "在线" : "离线"}</span>
          <span><i className={testnetSafe ? "ok" : ""} />启动对账 {reconciliation ? testnetSafe ? "安全" : "有未保护仓位" : "尚未执行"}</span>
          <span><i className={testnetStatus?.account?.can_trade ? "ok" : ""} />可用保证金 {testnetStatus?.account?.can_trade ? "就绪" : "不足"}</span>
        </div>
      </div>
      <div className="account-metrics">
        <Metric label="钱包余额" value={portfolio ? money(portfolio.cash) : "—"} suffix="" />
        <Metric label="权益" value={portfolio ? money(portfolio.equity) : "—"} suffix="" />
        <Metric label="可用余额" value={portfolio ? money(portfolio.available_balance) : "—"} suffix="" />
        <div
          className="metric"
          data-tooltip={METRIC_DEFINITIONS["过去24h盈亏"]}
        ><span>过去24h盈亏</span><strong className={Number(displayedPnl ?? 0) >= 0 ? "positive" : "negative"}>{displayedPnl === null ? "—" : money(displayedPnl)}</strong></div>
        <Metric label="占用保证金" value={portfolio ? money(portfolio.margin_used) : "—"} suffix="" />
        <Metric label="持仓数" value={portfolio ? String(portfolio.open_positions) : "—"} suffix="" />
      </div>

      <h4 className="account-subhead">持仓</h4>
      <p className="position-close-note">
        市价平仓按交易所当前数量提交 reduce-only 委托；成交并确认仓位归零后，仅撤销 CandlePilot 的保护单。运行中请先停止引擎。
      </p>
      <div className="table-wrap account-table">
        <table>
          <thead><tr><th>标的</th><th>方向 / 杠杆</th><th>持仓价值 / 保证金</th><th>均价 / 标记价</th><th data-tooltip="百分比是保证金回报率：未实现盈亏 ÷ 当前初始保证金 × 100%。它会随杠杆放大，不是标记价相对均价的价格涨跌幅。">未实现盈亏</th><th data-tooltip="按交易所持仓均价与当前实际止损、止盈价格计算，不包含手续费或成交滑点。">原始盈亏比</th><th data-tooltip="百分比是止损价相对持仓均价的方向化价格距离：多单为 (止损价 − 均价) ÷ 均价，空单方向相反。它不乘杠杆，不是保证金回报率。">止损</th><th data-tooltip="百分比是止盈价相对持仓均价的方向化价格距离：多单为 (止盈价 − 均价) ÷ 均价，空单方向相反。它不乘杠杆，不是保证金回报率。">止盈</th><th>操作</th></tr></thead>
          <tbody>
            {positions.map((position) => {
              const protectionMetrics = positionProtectionMetrics(position);
              const unrealizedReturnPercent = Number(position.margin_used) > 0
                ? (Number(position.unrealized_pnl) / Number(position.margin_used)) * 100
                : null;
              const protectionFallback = position.protection_source === "exchange" ? "交易所侧"
                : position.protection_source === "missing" ? "缺失"
                  : position.protection_source === "unknown" ? "待确认" : "—";
              return <tr key={position.symbol}>
                <td><strong>{position.symbol.replace("USDT", "")}</strong></td>
                <td><span className="position-inline-pair"><span className={position.side === "LONG" ? "positive" : "negative"}>{position.side}</span><i>/</i><span>{position.leverage}×</span></span></td>
                <td><span className="position-inline-pair"><span>{money(position.notional)}</span><i>/</i><span>{money(position.margin_used)} USDT</span></span></td>
                <td><span className="position-inline-pair"><span>{Number(position.average_price).toFixed(4)}</span><i>/</i><span>{Number(position.mark_price).toFixed(4)}</span></span></td>
                <td className={Number(position.unrealized_pnl) >= 0 ? "positive" : "negative"}>
                  <span className="pnl-with-return"><span>{money(position.unrealized_pnl)}</span><em>{signedPositionPercent(unrealizedReturnPercent)}</em></span>
                </td>
                <td>{protectionMetrics.riskRewardRatio === null
                  ? "—"
                  : `${protectionMetrics.riskRewardRatio.toFixed(2)} : 1`}</td>
                <td>{position.stop_loss === null
                  ? protectionFallback
                  : <span className="position-protection"><span>{Number(position.stop_loss).toFixed(4)}</span><em className="negative">{signedPositionPercent(protectionMetrics.stopLossPercent)}</em></span>}</td>
                <td>{position.take_profit === null
                  ? protectionFallback
                  : <span className="position-protection"><span>{Number(position.take_profit).toFixed(4)}</span><em className="positive">{signedPositionPercent(protectionMetrics.takeProfitPercent)}</em></span>}</td>
                <td className="position-close-cell">
                  {confirmCloseSymbol === position.symbol
                    ? <span className="position-close-confirm">
                        <small>确认全部平仓？</small>
                        <button
                          className="position-close-danger"
                          disabled={busy !== null || engineRunning}
                          onClick={async () => {
                            if (await onClosePosition(position.symbol)) setConfirmCloseSymbol(null);
                          }}
                        >{busy === `position-close-${position.symbol}` ? "平仓中…" : "确认"}</button>
                        <button
                          className="text-button"
                          disabled={busy !== null}
                          onClick={() => setConfirmCloseSymbol(null)}
                        >取消</button>
                      </span>
                    : <button
                        className="position-close-button"
                        disabled={busy !== null || engineRunning}
                        title={engineRunning ? "请先停止交易引擎" : `市价平掉全部 ${position.symbol} 持仓`}
                        onClick={() => setConfirmCloseSymbol(position.symbol)}
                      >市价平仓</button>}
                </td>
              </tr>;
            })}
            {!positions.length && <tr><td colSpan={9} className="empty">当前无持仓。</td></tr>}
          </tbody>
        </table>
      </div>

      <h4 className="account-subhead">成交明细</h4>
      <div className="table-wrap account-table">
        <table>
          <thead><tr><th>时间</th><th>标的</th><th>方向</th><th>用途</th><th data-tooltip="该笔实际成交数量 × 实际成交均价得到的 USDT 名义价值。它不是保证金、账户扣款或盈亏；初始保证金通常约为成交额 ÷ 杠杆。">成交额（USDT）</th><th>成交价</th><th data-tooltip="已实现盈亏及其相对于该笔平仓所对应开仓保证金的回报率；无法可靠追溯开仓保证金时回报率显示为「—」。">已实现盈亏 / 回报率</th><th>关联开仓</th><th>订单号</th></tr></thead>
          <tbody>
            {fills.map((fill) => (
              <tr key={`${fill.source}-${fill.id}`}>
                <td><small>{new Date(fill.created_at).toLocaleString("zh-CN", { hour12: false })}</small></td>
                <td>{fill.symbol.replace("USDT", "")}</td>
                <td className={fill.side === "BUY" ? "fill-buy" : fill.side === "SELL" ? "fill-sell" : ""}>
                  {fillDirectionLabel(fill)}
                </td>
                <td><span className={`fill-purpose ${displayedFillPurpose(fill)}`}>{fillPurposeLabel(displayedFillPurpose(fill))}</span></td>
                <td>{fill.notional_usdt === null ? "—" : `${money(fill.notional_usdt)} USDT`}</td>
                <td>{fill.report.average_price === null ? "—" : Number(fill.report.average_price).toFixed(4)}</td>
                <td className={fill.realized_pnl !== null && Number(fill.realized_pnl) < 0 ? "fill-pnl negative" : "fill-pnl"}>
                  {fill.realized_pnl === null || !fill.reduce_only ? "—" : <span className="pnl-with-return">
                    <span>{Number(fill.realized_pnl).toFixed(4)} USDT</span>
                    <em>{signedPositionPercent(fill.realized_return_percent === null ? null : Number(fill.realized_return_percent))}</em>
                  </span>}
                </td>
                <td><small title={fill.related_client_order_id ?? undefined}>{shortOrderId(fill.related_client_order_id)}</small></td>
                <td><small title={fill.client_order_id}>{shortOrderId(fill.client_order_id)}</small></td>
              </tr>
            ))}
            {!fills.length && <tr><td colSpan={9} className="empty">尚无成交记录。</td></tr>}
          </tbody>
        </table>
      </div>

    </article>
  );
}

function fillPurposeLabel(purpose: TradeFillRecord["purpose"]): string {
  return {
    entry: "开仓 / 加仓",
    stop_loss: "止损平仓",
    take_profit: "止盈平仓",
    manual_close: "手动平仓",
    rescue_close: "紧急回补",
    model_close: "模型平仓",
    model_reduce: "模型减仓",
    other_close: "其他平仓",
  }[purpose];
}

function displayedFillPurpose(fill: TradeFillRecord): TradeFillRecord["purpose"] {
  return fill.reduce_only && fill.purpose === "entry" ? "other_close" : fill.purpose;
}

export function fillDirectionLabel(
  fill: Pick<TradeFillRecord, "side" | "reduce_only">,
): "开多" | "开空" | "平多" | "平空" | "—" {
  if (fill.side === null) return "—";
  if (fill.reduce_only) return fill.side === "SELL" ? "平多" : "平空";
  return fill.side === "BUY" ? "开多" : "开空";
}

function shortOrderId(clientOrderId: string | null): string {
  if (!clientOrderId) return "—";
  return clientOrderId.length > 15 ? `…${clientOrderId.slice(-12)}` : clientOrderId;
}

function OperationsPanel({
  providerMetrics,
  operationsError,
}: {
  providerMetrics: ProviderMetric[];
  operationsError: string | null;
}) {
  return (
    <article className="panel operations-panel">
      <PanelTitle code="07" title="模型运维" meta="24 小时调用窗口 · 只读" />
      {operationsError && <div className="operations-error">模型运维数据暂不可用：{operationsError}</div>}
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
                  <span data-tooltip={metric.cost_complete ? "过去 24 小时全部调用按公开 API 单价或 Provider 返回成本折算的总成本；订阅 Auth 的实际账单可能不同。" : `仅 ${metric.priced_call_count}/${metric.call_count} 次调用可定价，因此不展示不完整的总成本。`}>
                    等效成本
                    <strong>{metric.cost_usd_total === null ? "—" : `$${metric.cost_usd_total.toFixed(4)}`}</strong>
                    {!metric.cost_complete && <small>{metric.priced_call_count}/{metric.call_count} 可定价</small>}
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
              等效成本为按 API 标准计价的折算估算（Claude 用 CLI 自带成本，Codex 用 models.dev 逐 token 折算）；订阅计划实际不按次计费。窗口内存在无法定价的调用时显示「—」，不把部分小计冒充总成本。
            </small>
          )}
        </section>

      </div>
    </article>
  );
}
