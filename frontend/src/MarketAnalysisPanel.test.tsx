import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarketAnalysisPanel } from "./App";
import type { MarketAnalysisRecord } from "./types";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const record: MarketAnalysisRecord = {
  id: 7,
  symbol: "BTCUSDT",
  status: "succeeded",
  provider: "codex-auth",
  model: "gpt-5.6-sol",
  reasoning_effort: "medium",
  prompt_version: "market-analysis-v1",
  data_version: "kansoku-compatible-crypto-v1",
  result: {
    direction: "long",
    summary: "15m structure holds above the latest swing.",
    anchor: { timeframe: "15m", time: "2026-07-22T10:00:00Z", price: 100, reason: "confirmed swing" },
    scenarios: [
      { name: "continuation", probability: 60, trigger: "close above 101", expected_path: "test T1", invalidation: "close below 98" },
      { name: "range", probability: 40, trigger: "remain below 101", expected_path: "rotate", invalidation: "break range" },
    ],
    range_plan: null,
    entry_plan: {
      entry: 101,
      stop: 98,
      target1: 104,
      target2: 108,
      stop_structure: "below 15m swing",
      entry_trigger: "15m close then 5m retest",
      management: "T1 reduce half; remainder toward breakeven.",
    },
    reward_risk: { target1: 1, target2: 2.3333 },
    key_evidence: ["EMA alignment", "flow confirms"],
    missing_data_impact: ["news risk is unknown"],
  },
  usage: { total_tokens: 1234 },
  duration_ms: 12000,
  error: null,
  created_at: "2026-07-22T10:01:00Z",
  completed_at: "2026-07-22T10:01:12Z",
  outcome: null,
  outcome_updated_at: null,
  input: {
    as_of: "2026-07-22T10:00:05Z",
    timeframes: Object.fromEntries((["5m", "15m", "1h"] as const).map((timeframe) => [timeframe, {
      bars: [
        { time: "2026-07-22T09:45:00Z", open: 99, high: 101, low: 98, close: 100, volume: 10, quote_volume: 1000 },
        { time: "2026-07-22T10:00:00Z", open: 100, high: 102, low: 99, close: 101, volume: 12, quote_volume: 1200 },
      ],
      summary: {},
    }])) as NonNullable<MarketAnalysisRecord["input"]>["timeframes"],
    unavailable_inputs: { news: "no source" },
  },
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("MarketAnalysisPanel", () => {
  it("starts an analysis and renders the frozen three-timeframe plan", async () => {
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/market-analyses?limit=30") return response([]);
      if (path === "/api/market-analyses" && init?.method === "POST") return response({ id: 7, status: "pending" }, 202);
      if (path === "/api/market-analyses/7") return response(record);
      throw new Error(`unexpected request: ${path}`);
    });
    render(<MarketAnalysisPanel engineRunning={false} provider="codex-auth" />);

    await waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/market-analyses?limit=30",
      expect.any(Object),
    ));
    fireEvent.click(screen.getByRole("button", { name: "开始分析" }));

    await screen.findByText("偏多");
    expect(screen.getByText("101")).toBeTruthy();
    expect(screen.getByText("104")).toBeTruthy();
    expect(screen.getByText("108")).toBeTruthy();
    expect(screen.getByText("news risk is unknown")).toBeTruthy();
    expect(screen.getByRole("img", { name: "BTCUSDT 15m 冻结 K 线" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "1h" }));
    expect(screen.getByRole("img", { name: "BTCUSDT 1h 冻结 K 线" })).toBeTruthy();
  });

  it("blocks starting while the formal engine is running", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response([]));
    render(<MarketAnalysisPanel engineRunning provider="codex-auth" />);
    expect(screen.getByRole("button", { name: "开始分析" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/正式引擎运行中/)).toBeTruthy();
  });
});
