import { useCallback, useEffect, useMemo, useState } from "react";
import type { Candidate, EngineStatus, ProviderHealth, Signal } from "./types";

const emptyStatus: EngineStatus = {
  mode: "paper-production-data",
  running: false,
  emergency_locked: false,
  selected_provider: null,
  candidate_count: 0,
  universe_refreshed_at: null,
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

function providerLabel(name: string): string {
  if (name === "codex-auth") return "Codex Auth";
  if (name === "claude-code-auth") return "Claude Code Auth";
  return name;
}

function percent(value: string): string {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

export default function App() {
  const [status, setStatus] = useState<EngineStatus>(emptyStatus);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [socketOnline, setSocketOnline] = useState(false);

  const refresh = useCallback(async () => {
    const [nextStatus, nextProviders, nextCandidates, nextSignals] = await Promise.all([
      api<EngineStatus>("/api/status"),
      api<ProviderHealth[]>("/api/providers"),
      api<Candidate[]>("/api/universe"),
      api<Signal[]>("/api/signals?limit=20"),
    ]);
    setStatus(nextStatus);
    setProviders(nextProviders);
    setCandidates(nextCandidates);
    setSignals(nextSignals);
  }, []);

  useEffect(() => {
    refresh().catch((reason: Error) => setError(reason.message));
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/events`);
    socket.onopen = () => setSocketOnline(true);
    socket.onclose = () => setSocketOnline(false);
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data) as { type: string; data: EngineStatus };
      if (message.type === "status") setStatus(message.data);
    };
    return () => socket.close();
  }, [refresh]);

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
        </div>
        <div className="live-state">
          <span className={`dot ${socketOnline ? "online" : ""}`} />
          {socketOnline ? "LOCAL STREAM ONLINE" : "STREAM OFFLINE"}
        </div>
      </header>

      <main>
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
            <button
              className="primary"
              disabled={busy !== null || status.running || status.emergency_locked}
              onClick={() => act("start", "/api/engine/start")}
            >{busy === "start" ? "启动中…" : "启动引擎"}</button>
            <button disabled={busy !== null || !status.running} onClick={() => act("stop", "/api/engine/stop")}>优雅停止</button>
            <button className="danger" disabled={busy !== null} onClick={() => act("kill", "/api/engine/emergency-stop")}>紧急熔断</button>
          </div>
        </section>

        {error && <div className="error-banner"><b>操作失败</b><span>{error}</span><button onClick={() => setError(null)}>×</button></div>}
        {status.emergency_locked && <div className="lock-banner">紧急锁定已生效。检查账户状态后才能手动解除。</div>}

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

