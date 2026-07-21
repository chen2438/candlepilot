import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DecisionPanel,
  DecisionTiming,
  CollapsiblePanel,
  decisionQueryUrl,
  EmergencyLockBanner,
  intentRewardRiskRatio,
  LiveCycleStatus,
  LiveRunActionButtons,
  RunUsage,
  StartupProbeCompletedSummary,
  StartupProbeRunningSummary,
} from "./App";
import type { DecisionEvent, RunSessionMetrics } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const decision: DecisionEvent = {
  id: 1,
  live_run_id: 1,
  live_run: {
    id: 1,
    status: "running",
    config: { cadences: ["15m"], provider_chain: ["openai-compatible:openrouter"] },
    stop_reason: null,
    started_at: "2026-07-19T15:08:39Z",
    ended_at: null,
  },
  provider: "openai-compatible:openrouter",
  model: "tencent/hy3:free",
  provenance: { reasoning_effort: "low" },
  failover: null,
  intent: {
    symbol: "ETHUSDT",
    cadence: "15m",
    action: "OPEN_LONG",
    confidence: 0.6,
    leverage: 10,
    risk_fraction: "0.0049",
    order_type: "MARKET",
    entry_price: "1872.2400",
    stop_loss: "1863.0000",
    take_profit: "1888.0000",
    rationale: "Trend entry long.",
  },
  duration_ms: 50980,
  decision_duration_ms: 51250,
  outcome: "rejected",
  risk: {
    id: 1,
    accepted: false,
    reason: "pre-trade reward/risk ratio 1.2800:1 must be greater than 1.3:1",
    decision: {
      max_quantity: null,
      pre_trade_entry_price: "1871.5000",
      pre_trade_reward_risk_ratio: "1.2800",
    },
    created_at: "2026-07-19T15:16:52Z",
  },
  execution: null,
  created_at: "2026-07-19T15:16:52Z",
};

describe("DecisionPanel", () => {
  it("collapses and expands overview modules independently", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [tab, setTab] = useState<"overview" | "account">("overview");
      const [expanded, setExpanded] = useState(true);
      return <>
        <button onClick={() => setTab(tab === "overview" ? "account" : "overview")}>切换页面</button>
        {tab === "overview" && <CollapsiblePanel
          code="01"
          title="模型接入"
          meta="手动路由"
          expanded={expanded}
          onExpandedChange={setExpanded}
        >
          <div>模型配置内容</div>
        </CollapsiblePanel>}
      </>;
    }
    render(<Harness />);

    const toggle = screen.getByRole("button", { name: /01.*模型接入.*手动路由/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("模型配置内容")).toBeTruthy();

    await user.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("模型配置内容")).toBeNull();

    await user.click(screen.getByRole("button", { name: "切换页面" }));
    expect(screen.queryByRole("button", { name: /01.*模型接入.*手动路由/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "切换页面" }));

    const restoredToggle = screen.getByRole("button", { name: /01.*模型接入.*手动路由/ });
    expect(restoredToggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("模型配置内容")).toBeNull();

    await user.click(restoredToggle);
    expect(restoredToggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("模型配置内容")).toBeTruthy();
  });

  it("pages decision history by ten complete runs", () => {
    expect(decisionQueryUrl("all")).toBe("/api/decision-events?run_limit=10");
    expect(decisionQueryUrl("rejected", 21)).toBe(
      "/api/decision-events?run_limit=10&outcome=rejected&before_run_id=21",
    );
  });

  it("identifies shared batch inference separately from per-decision completion", () => {
    render(<DecisionTiming decision={decision} />);
    expect(screen.getByText("批次耗时 50.98s")).toBeTruthy();
    expect(screen.getByText("整笔耗时 51.25s")).toBeTruthy();
    expect(screen.getByText(/批次耗时/).closest("span")?.getAttribute("data-tooltip"))
      .toContain("不应按决策条数相加");
  });

  it("hides immaterial audit timing after a shared batch", () => {
    render(<DecisionTiming decision={{ ...decision, decision_duration_ms: 50990 }} />);
    expect(screen.getByText("批次耗时 50.98s")).toBeTruthy();
    expect(screen.queryByText(/整笔耗时/)).toBeNull();
  });

  it("describes a cadence batch instead of showing an empty current symbol", () => {
    render(<LiveCycleStatus cycle={{
      cadence: "5m",
      started_at: "2026-07-20T00:00:00Z",
      symbol: null,
      symbol_started_at: null,
      stage: "batch_decision",
      completed: 0,
      total: 11,
    }} />);
    expect(screen.getByText("当前 5m 周期 · 11 个标的 · 批量分析中")).toBeTruthy();
    expect(screen.queryByText(/准备中|batch_decision|0\/11/)).toBeNull();
  });

  it("labels provider input tokens as the uncached portion", () => {
    const session: RunSessionMetrics = {
      state: "running",
      started_at: "2026-07-20T00:00:00Z",
      ended_at: null,
      duration_seconds: 348,
      call_count: 11,
      error_count: 0,
      input_tokens: 4,
      cached_input_tokens: 45831,
      cache_creation_input_tokens: 35296,
      output_tokens: 9059,
      total_tokens: 90190,
      priced_call_count: 11,
      cost_complete: true,
      equivalent_cost_usd: 0.379836,
      average_duration_ms: 92110,
      average_tokens: 8199.1,
      average_cost_usd: 0.034531,
    };
    render(<RunUsage session={session} />);
    expect(screen.getByText("未缓存输入")).toBeTruthy();
    expect(screen.queryByText("输入 Token")).toBeNull();
    expect(screen.getByText("90,190")).toBeTruthy();
  });

  it("shows a running startup probe as a batch instead of its first symbol", () => {
    render(<StartupProbeRunningSummary probe={{
      running: true,
      ready: false,
      consumed: false,
      timeout_seconds: 100,
      provider_count: 1,
      completed_providers: 0,
      probe_symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
      candidate_symbol_count: 2,
      extra_position_symbol_count: 1,
      probe_cadence: "5m",
      provider_results: { "claude-code-auth": { status: "pending" } },
      analysis_symbol_count: 3,
      started_at: "2026-07-20T00:00:00Z",
    }} />);
    expect(screen.getByText(/2 个候选 \+ 1 个额外持仓 = 3 个分析标的 · 5m/)).toBeTruthy();
    expect(screen.queryByText("BTCUSDT 5m")).toBeNull();
    expect(screen.getByTitle("BTCUSDT、ETHUSDT、SOLUSDT")).toBeTruthy();
    expect(screen.getByText("等待结果")).toBeTruthy();
  });

  it("describes startup capacity as one shared symbol batch", () => {
    render(<StartupProbeCompletedSummary
      ready
      probe={{
        running: false,
        ready: true,
        consumed: false,
        timeout_seconds: 60,
        provider_count: 1,
        completed_providers: 1,
        probe_symbols: ["BTCUSDT", "ETHUSDT"],
        candidate_symbol_count: 2,
        extra_position_symbol_count: 0,
        probe_cadence: "15m",
        provider_results: {
          "openai-compatible:deepseek": {
            status: "completed",
            model: "deepseek-v4-pro",
            reasoning_effort: "high",
            duration_seconds: 42,
            actions: { HOLD: 1, OPEN_LONG: 1 },
            input_tokens: 12000,
            cached_input_tokens: 6000,
            output_tokens: 800,
            total_tokens: 12800,
            equivalent_cost_usd: 0.02,
            intents: [
              { symbol: "BTCUSDT", action: "HOLD", confidence: 0.4 },
              { symbol: "ETHUSDT", action: "OPEN_LONG", confidence: 0.7 },
            ],
          },
        },
        slowest_seconds: 42,
        analysis_symbol_count: 2,
        aggregate_utilization: 0.2,
        started_at: "2026-07-19T22:00:00Z",
      }}
    />);
    expect(screen.getByText(/2 个候选 = 2 个分析标的 · 批量分析 42s/)).toBeTruthy();
    expect(screen.queryByText(/× 2 标的/)).toBeNull();
    expect(screen.getByText(/42s · HOLD × 1 · OPEN_LONG × 1/)).toBeTruthy();
    expect(screen.getByText(/Token 12,800/)).toBeTruthy();
    expect(screen.getByText(/成本 \$0.020000/)).toBeTruthy();
    expect(screen.getByText("查看 2 条意图")).toBeTruthy();
  });

  it("allows one-shot trading without a probe but keeps continuous startup locked", async () => {
    const user = userEvent.setup();
    const onProbe = vi.fn();
    const onProbeAndStart = vi.fn();
    const onRunOnce = vi.fn();
    const onStart = vi.fn();
    const props = {
      busy: null,
      running: false,
      emergencyLocked: false,
      onProbe,
      onProbeAndStart,
      onRunOnce,
      onStart,
      onStop: vi.fn(),
      onEmergencyStop: vi.fn(),
    };
    const view = render(<LiveRunActionButtons {...props} probeReady={false} />);

    const probe = screen.getByRole("button", { name: "试跑" });
    const probeAndStart = screen.getByRole("button", { name: "试跑并启动" });
    const start = screen.getByRole("button", { name: "启动" });
    const runOnce = screen.getByRole("button", { name: "运行一次" });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    expect((probeAndStart as HTMLButtonElement).disabled).toBe(false);
    expect((runOnce as HTMLButtonElement).disabled).toBe(false);
    await user.click(probeAndStart);
    expect(onProbeAndStart).toHaveBeenCalledOnce();
    await user.click(runOnce);
    expect(onRunOnce).toHaveBeenCalledOnce();
    await user.click(probe);
    expect(onProbe).toHaveBeenCalledOnce();
    expect(onStart).not.toHaveBeenCalled();

    view.rerender(<LiveRunActionButtons {...props} probeReady />);
    expect((start as HTMLButtonElement).disabled).toBe(false);
    expect((runOnce as HTMLButtonElement).disabled).toBe(false);
    await user.click(start);
    expect(onStart).toHaveBeenCalledOnce();

    view.rerender(<LiveRunActionButtons {...props} busy="run-once" probeReady />);
    expect((screen.getByRole("button", { name: "紧急熔断" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("offers a safety-checked emergency lock release", async () => {
    const user = userEvent.setup();
    const onClear = vi.fn();
    const view = render(
      <EmergencyLockBanner
        lockedUntil="2026-07-21T00:00:00Z"
        busy={false}
        onClear={onClear}
      />,
    );

    expect(screen.getByText(/解除前会检查测试网账户无持仓且无挂单/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "检查并解除锁定" }));
    expect(onClear).toHaveBeenCalledOnce();

    view.rerender(
      <EmergencyLockBanner
        lockedUntil={null}
        busy
        onClear={onClear}
      />,
    );
    expect((screen.getByRole("button", { name: "安全检查中…" }) as HTMLButtonElement).disabled)
      .toBe(true);
  });

  it("does not offer the transient approved-only outcome as a filter", () => {
    render(
      <DecisionPanel
        decisions={[decision]}
        liveRunPerformance={[]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );

    expect(screen.queryByRole("button", { name: "风控放行" })).toBeNull();
    expect(screen.getByRole("button", { name: "下单成功" })).toBeTruthy();
    expect(screen.getByText("15m · Custom API · openrouter · tencent/hy3:free · low")).toBeTruthy();
  });

  it("shows the ratio calculated from the AI-returned prices when a decision is expanded", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      text: async () => "detail unavailable",
    })));
    const user = userEvent.setup();

    render(
      <DecisionPanel
        decisions={[decision]}
        liveRunPerformance={[]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );

    await user.click(screen.getByRole("button", { name: /OPEN_LONG/ }));

    expect(screen.getByText("AI 原始盈亏比")).toBeTruthy();
    expect(screen.getByText("1.71 : 1")).toBeTruthy();
    expect(screen.getByText("下单前盈亏比")).toBeTruthy();
    expect(screen.getByText("1.2800 : 1")).toBeTruthy();
    expect(screen.getByText("最终下单数量")).toBeTruthy();
    expect(screen.queryByText("风控数量")).toBeNull();
    expect(screen.queryByText("成交额")).toBeNull();
    expect(screen.queryByText("保证金")).toBeNull();
  });

  it("shows fill notional and estimated initial margin after a successful order", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      text: async () => "detail unavailable",
    })));
    const user = userEvent.setup();
    const executed: DecisionEvent = {
      ...decision,
      outcome: "executed",
      risk: {
        ...decision.risk!,
        accepted: true,
        reason: "accepted within hard risk limits",
        decision: { ...decision.risk!.decision, max_quantity: "2" },
      },
      execution: {
        id: 9,
        inference_id: 1,
        client_order_id: "cp-fixture",
        status: "SUCCEEDED",
        stage: "COMPLETE",
        message: "order accepted and required execution checks completed",
        exchange_error_code: null,
        estimated_loss_usdt: null,
        entry_report: {
          client_order_id: "cp-fixture",
          status: "FILLED",
          filled_quantity: "2",
          average_price: "150",
          message: "filled",
        },
        rescue_report: null,
        created_at: "2026-07-20T10:00:00Z",
      },
    };

    render(
      <DecisionPanel
        decisions={[executed]}
        liveRunPerformance={[]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );
    await user.click(screen.getByRole("button", { name: /OPEN_LONG/ }));

    expect(screen.getByText("成交额")).toBeTruthy();
    expect(screen.getByText("300.00 USDT")).toBeTruthy();
    expect(screen.getByText("保证金")).toBeTruthy();
    expect(screen.getByText("30.00 USDT")).toBeTruthy();
  });

  it("shows shadow structure checks without presenting them as a hard rejection", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      text: async () => "detail unavailable",
    })));
    const user = userEvent.setup();
    const shadow: DecisionEvent = {
      ...decision,
      risk: {
        ...decision.risk!,
        decision: {
          ...decision.risk!.decision,
          structure_assessment: {
            mode: "shadow",
            passed: false,
            checks: [
              { key: "alignment", passed: true, detail: "5m and 15m align" },
              { key: "extension", passed: false, detail: "2.2 ATR must be below 2" },
            ],
          },
        },
      },
    };

    render(
      <DecisionPanel
        decisions={[shadow]}
        liveRunPerformance={[]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );
    await user.click(screen.getByRole("button", { name: /OPEN_LONG/ }));

    expect(screen.getByText("结构入场门槛 · SHADOW")).toBeTruthy();
    expect(screen.getByText("存在未通过项")).toBeTruthy();
    expect(screen.getByText("2.2 ATR must be below 2")).toBeTruthy();
  });

  it("shows a local pending limit and its hard expiry", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      text: async () => "detail unavailable",
    })));
    const user = userEvent.setup();
    const pending: DecisionEvent = {
      ...decision,
      outcome: "approved",
      intent: { ...decision.intent, order_type: "LIMIT", ttl_seconds: 60 },
      risk: {
        ...decision.risk!,
        accepted: true,
        reason: "resting limit intent queued locally until trigger",
        decision: {
          ...decision.risk!.decision,
          pending_entry: true,
          pending_expires_at: "2026-07-20T10:30:00Z",
        },
      },
    };

    render(
      <DecisionPanel
        decisions={[pending]}
        liveRunPerformance={[]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );

    expect(screen.getByText("等待触发")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /OPEN_LONG/ }));
    expect(screen.getByText("本地待触发")).toBeTruthy();
    expect(screen.getByText("意图有效至")).toBeTruthy();
    expect(screen.getByText(/2026\/07\/20/)).toBeTruthy();
  });

  it("collapses each large AI audit block by default and expands it independently", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({
        ...decision,
        audit_status: "complete",
        input: { market: { symbol: "ETHUSDT" }, portfolio: { equity: "10000" } },
        prompt: "Choose one action.",
        raw_output: '{"action":"OPEN_LONG"}',
        usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
        equivalent_cost_usd: 0.001,
      }),
    })));
    const user = userEvent.setup();

    render(
      <DecisionPanel
        decisions={[decision]}
        liveRunPerformance={[]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );

    await user.click(screen.getByRole("button", { name: /OPEN_LONG/ }));
    await screen.findByText("AI 分析详情");
    const inputDetails = screen.getByText("结构化输入").closest("details");
    const promptDetails = screen.getByText("实际 Prompt").closest("details");
    const outputDetails = screen.getByText("模型原始输出").closest("details");

    expect(inputDetails?.open).toBe(false);
    expect(promptDetails?.open).toBe(false);
    expect(outputDetails?.open).toBe(false);
    await user.click(screen.getByText("结构化输入"));
    expect(inputDetails?.open).toBe(true);
    expect(promptDetails?.open).toBe(false);
    expect(outputDetails?.open).toBe(false);
  });

  it("expands running runs by default and collapses stopped runs independently", async () => {
    const user = userEvent.setup();
    const runningDecision: DecisionEvent = {
      ...decision,
      id: 2,
      live_run_id: 2,
      live_run: {
        id: 2,
        status: "running",
        config: { cadences: ["15m"], provider_chain: ["claude-code-auth"] },
        stop_reason: null,
        started_at: "2026-07-19T16:53:22Z",
        ended_at: null,
      },
      intent: { ...decision.intent, symbol: "BTCUSDT", action: "HOLD" },
      created_at: "2026-07-19T17:00:27Z",
    };
    const stoppedDecision: DecisionEvent = {
      ...decision,
      id: 3,
      live_run_id: 1,
      live_run: {
        id: 1,
        status: "stopped",
        config: { cadences: ["15m"], provider_chain: ["openai-compatible:openrouter"] },
        stop_reason: "stopped by user",
        started_at: "2026-07-19T15:08:39Z",
        ended_at: "2026-07-19T16:24:38Z",
      },
      intent: { ...decision.intent, symbol: "SOLUSDT", action: "HOLD" },
      created_at: "2026-07-19T16:20:58Z",
    };

    render(
      <DecisionPanel
        decisions={[runningDecision, stoppedDecision]}
        liveRunPerformance={[{
          live_run_id: 2,
          total_pnl: "12.5",
          realized_pnl: "10",
          unrealized_pnl: "2.5",
          wins: 2,
          closed_trades: 3,
          open_position_count: 2,
          win_rate: "0.6666666667",
          includes_unrealized: true,
          valued_at: "2026-07-19T17:00:27Z",
        }]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );

    const runningGroup = screen.getByText("正式运行 #2 · 运行中").closest("details");
    const stoppedHeader = screen.getByText("正式运行 #1 · 已停止");
    const stoppedGroup = stoppedHeader.closest("details");

    expect(runningGroup?.open).toBe(true);
    expect(stoppedGroup?.open).toBe(false);
    expect(screen.getAllByText(/1 条决策/)).toHaveLength(2);
    expect(screen.getByText("+12.50 USDT")).toBeTruthy();
    expect(screen.getAllByText("已平仓胜率")).toHaveLength(2);
    for (const label of screen.getAllByText("已平仓胜率")) {
      expect(label.getAttribute("data-tooltip")).toContain("已完成的平仓笔数");
    }
    expect(screen.getByText("67% (2/3)")).toBeTruthy();
    expect(screen.getAllByText("未平仓")).toHaveLength(2);
    expect(screen.getByText("2", { selector: ".decision-run-performance strong" })).toBeTruthy();
    expect(screen.getAllByText("批次耗时 50.98s")).toHaveLength(2);
    expect(screen.getAllByText("整笔耗时 51.25s")).toHaveLength(2);

    await user.click(stoppedHeader);

    expect(stoppedGroup?.open).toBe(true);
    expect(runningGroup?.open).toBe(true);
  });

  it("requires valid protective prices on opposite sides of entry", () => {
    expect(intentRewardRiskRatio({ ...decision.intent, take_profit: null })).toBeNull();
    expect(intentRewardRiskRatio({ ...decision.intent, stop_loss: "1880" })).toBeNull();
  });
});
