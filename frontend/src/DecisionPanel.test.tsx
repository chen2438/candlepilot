import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DecisionPanel, intentRewardRiskRatio } from "./App";
import type { DecisionEvent } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const decision: DecisionEvent = {
  id: 1,
  live_run_id: null,
  live_run: null,
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
  outcome: "rejected",
  risk: {
    id: 1,
    accepted: false,
    reason: "effective reward/risk ratio is below the hard minimum 1.3:1",
    decision: { max_quantity: null },
    created_at: "2026-07-19T15:16:52Z",
  },
  execution: null,
  created_at: "2026-07-19T15:16:52Z",
};

describe("DecisionPanel intent reward/risk", () => {
  it("shows the ratio calculated from the AI-returned prices when a decision is expanded", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      text: async () => "detail unavailable",
    })));
    const user = userEvent.setup();

    render(
      <DecisionPanel
        decisions={[decision]}
        filter="all"
        onFilter={vi.fn()}
        onLoadOlder={vi.fn(async () => undefined)}
        exhausted
      />,
    );

    await user.click(screen.getByRole("button", { name: /OPEN_LONG/ }));

    expect(screen.getByText("AI 原始盈亏比")).toBeTruthy();
    expect(screen.getByText("1.71 : 1")).toBeTruthy();
  });

  it("requires valid protective prices on opposite sides of entry", () => {
    expect(intentRewardRiskRatio({ ...decision.intent, take_profit: null })).toBeNull();
    expect(intentRewardRiskRatio({ ...decision.intent, stop_loss: "1880" })).toBeNull();
  });
});
