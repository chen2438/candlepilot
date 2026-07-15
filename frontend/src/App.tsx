import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import type {
  AccountPortfolio,
  AccountPosition,
  BacktestRun,
  Candidate,
  EngineStatus,
  OrderRecord,
  ProviderHealth,
  ProviderMetric,
  ProviderMetricsResponse,
  RiskEvent,
  Signal,
  TestnetAccountStatus,
} from "./types";

const emptyStatus: EngineStatus = {
  mode: "paper-production-data",
  running: false,
  emergency_locked: false,
  emergency_locked_until: null,
  selected_provider: null,
  backup_provider: null,
  active_cadences: ["1m", "5m", "15m"],
  supported_cadences: ["1m", "5m", "15m"],
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
  { key: "overview", label: "总览", meta: "引擎 · 认证 · 候选 · 决策" },
  { key: "account", label: "账户", meta: "持仓 · 订单 · 风险" },
  { key: "backtest", label: "回测", meta: "重放已审计决策" },
  { key: "operations", label: "运维", meta: "模型 · 测试网" },
  { key: "data", label: "数据", meta: "删除历史数据" },
];

function providerLabel(name: string): string {
  if (name === "codex-auth") return "Codex Auth";
  if (name === "claude-code-auth") return "Claude Code Auth";
  return name;
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
  const [signals, setSignals] = useState<Signal[]>([]);
  const [backtests, setBacktests] = useState<BacktestRun[]>([]);
  const [selectedBacktest, setSelectedBacktest] = useState<BacktestRun | null>(null);
  const [portfolio, setPortfolio] = useState<AccountPortfolio | null>(null);
  const [positions, setPositions] = useState<AccountPosition[]>([]);
  const [orders, setOrders] = useState<OrderRecord[]>([]);
  const [riskEvents, setRiskEvents] = useState<RiskEvent[]>([]);
  const [providerMetrics, setProviderMetrics] = useState<ProviderMetric[]>([]);
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
  const [candidateDraft, setCandidateDraft] = useState<number | null>(null);

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

  // Slider drag updates candidateDraft locally; only the release (pointer/key
  // up) commits one request, so dragging across values does not spam the API.
  const commitCandidates = useCallback(() => {
    setCandidateDraft((draft) => {
      if (draft !== null && draft !== status.candidates_per_cycle) {
        changeCandidatesPerCycle(draft, status.max_candidates_per_cycle);
      }
      return draft;
    });
  }, [status.candidates_per_cycle, status.max_candidates_per_cycle, changeCandidatesPerCycle]);

  // Clear the draft once the server confirms it, avoiding a value flicker.
  useEffect(() => {
    if (candidateDraft !== null && candidateDraft === status.candidates_per_cycle) {
      setCandidateDraft(null);
    }
  }, [candidateDraft, status.candidates_per_cycle]);

  const refresh = useCallback(async () => {
    const [nextStatus, nextProviders, nextCandidates, nextSignals, nextBacktests] = await Promise.all([
      api<EngineStatus>("/api/status"),
      api<ProviderHealth[]>("/api/providers"),
      api<Candidate[]>("/api/universe"),
      api<Signal[]>("/api/signals?limit=20"),
      api<BacktestRun[]>("/api/backtests?limit=10"),
    ]);
    setStatus(nextStatus);
    setProviders(nextProviders);
    setCandidates(nextCandidates);
    setSignals(nextSignals);
    setBacktests(nextBacktests);
  }, []);

  const refreshAccount = useCallback(async () => {
    const [nextPortfolio, nextPositions, nextOrders, nextRisk] = await Promise.all([
      api<AccountPortfolio>("/api/account/portfolio"),
      api<AccountPosition[]>("/api/account/positions"),
      api<OrderRecord[]>("/api/orders?limit=25"),
      api<RiskEvent[]>("/api/risk-events?limit=25"),
    ]);
    setPortfolio(nextPortfolio);
    setPositions(nextPositions);
    setOrders(nextOrders);
    setRiskEvents(nextRisk);
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

  useEffect(() => {
    refresh().catch((reason: Error) => setError(reason.message));
    refreshAccount().catch((reason: Error) => setError(reason.message));
    refreshOperations().catch(() => undefined);
    const account = window.setInterval(() => {
      refreshAccount().catch(() => undefined);
      refreshOperations().catch(() => undefined);
    }, 5000);
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/events`);
    socket.onopen = () => setSocketOnline(true);
    socket.onclose = () => setSocketOnline(false);
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data) as { type: string; data: EngineStatus };
      if (message.type === "status") setStatus(message.data);
    };
    return () => {
      window.clearInterval(account);
      socket.close();
    };
  }, [refresh, refreshAccount, refreshOperations]);

  const act = useCallback(async (name: string, path: string, body?: unknown) => {
    setBusy(name);
    setError(null);
    try {
      const next = await api<EngineStatus>(path, {
        method: "POST",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      setStatus(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, []);

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
    () => providers.find((provider) => provider.provider === status.selected_provider),
    [providers, status.selected_provider],
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
            <div className="cadence-select" title={status.running ? "运行时锁定" : "拖动选择每个周期分析候选池前 N 个标的"}>
              <span>每周期标的数<b>{candidateDraft ?? status.candidates_per_cycle ?? 5}</b></span>
              <div className="range-row">
                <input
                  type="range"
                  className="range"
                  min={1}
                  max={status.max_candidates_per_cycle}
                  step={1}
                  value={candidateDraft ?? status.candidates_per_cycle ?? 5}
                  disabled={busy !== null || status.running}
                  onChange={(event) => setCandidateDraft(Number(event.target.value))}
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

        <section className="grid">
          <article className="panel provider-panel">
            <PanelTitle code="01" title="模型认证" meta="手动路由" />
            <div className="provider-list">
              {providers.map((provider) => (
                <button
                  key={provider.provider}
                  className={`provider-card ${status.selected_provider === provider.provider ? "selected" : ""}`}
                  disabled={!provider.available || !provider.authenticated || status.running}
                  onClick={() => act("provider", "/api/providers/select", { name: provider.provider })}
                >
                  <span className={`provider-icon ${provider.authenticated ? "ready" : ""}`}>
                    {provider.provider.startsWith("codex") ? "CX" : "CC"}
                  </span>
                  <span className="provider-text">
                    <strong>{providerLabel(provider.provider)}</strong>
                    <small>{provider.version ?? provider.detail}</small>
                  </span>
                  <span className={`status-pill ${provider.authenticated ? "ok" : "off"}`}>
                    {provider.authenticated ? "READY" : provider.available ? "LOGIN" : "MISSING"}
                  </span>
                </button>
              ))}
            </div>
            <div className="provider-foot">
              <span>当前路由</span><strong>{activeProvider ? providerLabel(activeProvider.provider) : "未选择"}</strong>
            </div>
            <div className="provider-config">
              <div className="provider-config-title"><span>模型与推理强度</span><small>{status.running ? "运行时锁定" : "留空=CLI 默认"}</small></div>
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
                    <span className="config-name">{provider.provider.startsWith("codex") ? "CX" : "CC"}</span>
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
                <thead><tr><th>标的</th><th>评分</th><th>成交额排名</th><th>价差</th><th>24h 波动</th><th>趋势</th></tr></thead>
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

          <article className="panel signals-panel">
            <PanelTitle code="04" title="最近决策" meta="完整审计" />
            <div className="signal-list">
              {signals.map((signal) => (
                <div className="signal" key={signal.id}>
                  <span className={`action ${signal.intent.action.toLowerCase()}`}>{signal.intent.action}</span>
                  <span className="signal-symbol"><strong>{signal.intent.symbol}</strong><small>{signal.intent.cadence} · {providerLabel(signal.provider)}</small></span>
                  <span className="signal-confidence">{Math.round(signal.intent.confidence * 100)}<small>% CONF</small></span>
                  <span className="signal-time">{new Date(signal.created_at).toLocaleTimeString("zh-CN", { hour12: false })}</span>
                </div>
              ))}
              {!signals.length && <div className="empty cards">启动引擎后，结构化交易意图会显示在这里。</div>}
            </div>
          </article>
        </section>
        </>)}

        {tab === "backtest" && (
        <section className="grid">
          <article className="panel backtest-panel">
            <PanelTitle code="05" title="回测运行" meta="事件驱动 · 下一根 K 线成交" />
            <form className="backtest-form" onSubmit={runCachedReplay}>
              <label><span>标的</span><input required pattern="[A-Za-z0-9]+USDT" value={replayForm.symbol} onChange={(event) => setReplayForm({ ...replayForm, symbol: event.target.value })} /></label>
              <label><span>周期</span><select value={replayForm.cadence} onChange={(event) => setReplayForm({ ...replayForm, cadence: event.target.value })}><option value="1m">1m</option><option value="5m">5m</option><option value="15m">15m</option></select></label>
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
            riskEvents={riskEvents}
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
  return <div className="metric"><span>{label}</span><strong>{value}<small>{suffix}</small></strong></div>;
}

function PanelTitle({ code, title, meta }: { code: string; title: string; meta: string }) {
  return <div className="panel-title"><span>{code}</span><h2>{title}</h2><small>{meta}</small></div>;
}

function RiskItem({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="risk-item"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>;
}

function money(value: string): string {
  return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function AccountPanel({
  portfolio,
  positions,
  orders,
  riskEvents,
}: {
  portfolio: AccountPortfolio | null;
  positions: AccountPosition[];
  orders: OrderRecord[];
  riskEvents: RiskEvent[];
}) {
  const dailyPnl = portfolio ? Number(portfolio.daily_pnl) : 0;
  return (
    <article className="panel account-panel">
      <PanelTitle code="06" title="账户与风险" meta="模拟账户 · 只读" />
      <div className="account-metrics">
        <Metric label="权益" value={portfolio ? money(portfolio.equity) : "—"} suffix="" />
        <Metric label="可用余额" value={portfolio ? money(portfolio.available_balance) : "—"} suffix="" />
        <div className="metric"><span>当日盈亏</span><strong className={dailyPnl >= 0 ? "positive" : "negative"}>{portfolio ? money(portfolio.daily_pnl) : "—"}</strong></div>
        <Metric label="占用保证金" value={portfolio ? money(portfolio.margin_used) : "—"} suffix="" />
        <Metric label="持仓数" value={portfolio ? String(portfolio.open_positions) : "—"} suffix="" />
      </div>

      <h4 className="account-subhead">持仓</h4>
      <div className="table-wrap account-table">
        <table>
          <thead><tr><th>标的</th><th>方向</th><th>数量</th><th>均价</th><th>标记价</th><th>杠杆</th><th>未实现盈亏</th><th>止损</th></tr></thead>
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
                <td>{position.stop_loss === null ? "—" : Number(position.stop_loss).toFixed(4)}</td>
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

      <h4 className="account-subhead">风险事件</h4>
      <div className="risk-events">
        {riskEvents.map((event) => (
          <div className="risk-event" key={event.id}>
            <span className={`status-pill ${event.accepted ? "ok" : "off"}`}>{event.accepted ? "放行" : "否决"}</span>
            <span className="risk-event-symbol"><strong>{event.symbol}</strong><small>{new Date(event.created_at).toLocaleString("zh-CN", { hour12: false })}</small></span>
            <span className="risk-event-reason">{event.reason}</span>
          </div>
        ))}
        {!riskEvents.length && <div className="empty cards">尚无风控决策记录。</div>}
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
                  <span>Token 用量<strong>{metric.tokens_total.toLocaleString("zh-CN")}</strong></span>
                  <span>
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
