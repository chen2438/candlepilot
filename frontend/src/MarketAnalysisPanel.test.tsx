import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarketAnalysisPanel } from "./App";
import type { MarketAnalysisRecord, MarketAnalysisScheduleStatus } from "./types";

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
  prompt_version: "market-analysis-v2",
  data_version: "kansoku-compatible-crypto-v1",
  result: {
    direction: "long",
    summary: "15 分钟结构保持在最近摆动点上方。",
    anchor: { timeframe: "15m", time: "2026-07-22T10:00:00Z", price: 100, reason: "已确认摆动点" },
    scenarios: [
      { name: "延续上涨", probability: 60, trigger: "收盘站上 101", expected_path: "测试 T1", invalidation: "收盘跌破 98" },
      { name: "区间整理", probability: 40, trigger: "保持在 101 下方", expected_path: "区间轮动", invalidation: "离开区间" },
    ],
    range_plan: null,
    entry_plan: {
      entry: 101,
      stop: 98,
      target1: 104,
      target2: 108,
      stop_structure: "15 分钟摆动低点下方",
      entry_trigger: "15 分钟收盘确认后等待 5 分钟回踩",
      management: "T1 减仓一半，剩余仓位止损移向保本价。",
    },
    reward_risk: { target1: 1, target2: 2.3333 },
    key_evidence: ["EMA 排列一致", "资金流确认"],
    missing_data_impact: ["新闻风险未知"],
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

const rangeRecord: MarketAnalysisRecord = {
  ...record,
  id: 8,
  symbol: "ETHUSDT",
  result: {
    ...record.result!,
    direction: "neutral",
    range_plan: {
      low: 98,
      high: 102,
      tactic: "等待 15 分钟收盘确认离开区间后再重新评估。",
    },
    entry_plan: null,
    reward_risk: null,
  },
};

const shortRecord: MarketAnalysisRecord = {
  ...record,
  id: 9,
  symbol: "HYPEUSDT",
  result: {
    ...record.result!,
    direction: "short",
    summary: "15 分钟与 1 小时结构偏空。",
  },
};

const pendingRecord: MarketAnalysisRecord = {
  ...record,
  id: 10,
  symbol: "SOLUSDT",
  status: "pending",
  result: null,
  completed_at: null,
};

const activeRecord: MarketAnalysisRecord = {
  ...record,
  outcome: {
    status: "active",
    bars_observed: 3,
    entry_at: "2026-07-22T10:10:00Z",
    target1_at: null,
    resolved_at: null,
    detail: "计划已入场，尚未触及结构止损或 T1",
  },
  outcome_updated_at: "2026-07-22T10:20:00Z",
};

const stoppedRecord: MarketAnalysisRecord = {
  ...shortRecord,
  outcome: {
    status: "stopped",
    bars_observed: 4,
    entry_at: "2026-07-22T10:10:00Z",
    target1_at: null,
    resolved_at: "2026-07-22T10:25:00Z",
    detail: "计划已入场，随后触及结构止损",
  },
  outcome_updated_at: "2026-07-22T10:25:00Z",
};

const stoppedBeforeEntryRecord: MarketAnalysisRecord = {
  ...record,
  outcome: {
    status: "stopped_before_entry",
    bars_observed: 2,
    entry_at: null,
    target1_at: null,
    resolved_at: "2026-07-22T10:15:00Z",
    detail: "计划尚未入场，价格已先触及结构止损",
  },
  outcome_updated_at: "2026-07-22T10:20:00Z",
};

const target1BeforeEntryRecord: MarketAnalysisRecord = {
  ...shortRecord,
  outcome: {
    status: "target1_before_entry",
    bars_observed: 2,
    entry_at: null,
    target1_at: null,
    resolved_at: "2026-07-22T10:15:00Z",
    detail: "计划尚未入场，价格已先触及 T1",
  },
  outcome_updated_at: "2026-07-22T10:20:00Z",
};

const idleSchedule: MarketAnalysisScheduleStatus = {
  enabled: false,
  interval_minutes: 15,
  round_running: false,
  next_run_at: null,
  last_started_at: null,
  last_finished_at: null,
  last_error: null,
  last_result: null,
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
      if (path === "/api/market-analyses/schedule") return response(idleSchedule);
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
    expect(screen.getByRole("button", { name: "扫描并分析候选" }).hasAttribute("disabled")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "开始分析" }));

    await screen.findByRole("heading", { name: "偏多" });
    expect(screen.getByText("101")).toBeTruthy();
    expect(screen.getByText("104")).toBeTruthy();
    expect(screen.getByText("108")).toBeTruthy();
    expect(screen.getByText("新闻风险未知")).toBeTruthy();
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

  it("keeps a neutral range plan in its dedicated layout before scenarios", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/market-analyses/schedule") return response(idleSchedule);
      if (path === "/api/market-analyses?limit=30") return response([rangeRecord]);
      if (path === "/api/market-analyses/8") return response(rangeRecord);
      throw new Error(`unexpected request: ${path}`);
    });
    render(<MarketAnalysisPanel engineRunning={false} provider="codex-auth" />);

    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    fireEvent.click(await screen.findByRole("button", { name: /ETHUSDT/ }));
    const rangePlan = await screen.findByText("观望区间");
    expect(rangePlan.closest(".analysis-range-plan")).toBeTruthy();
    expect(rangePlan.closest(".range")).toBeNull();
    expect(screen.getByText("延续上涨")).toBeTruthy();
  });

  it("filters analysis history by directional, long, short, and all results", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/market-analyses/schedule") return response(idleSchedule);
      if (path === "/api/market-analyses?limit=30") return response([pendingRecord, rangeRecord, shortRecord, record]);
      if (path === "/api/market-analyses/8") return response(rangeRecord);
      throw new Error(`unexpected request: ${path}`);
    });
    render(<MarketAnalysisPanel engineRunning={false} provider="codex-auth" />);

    await screen.findByText("HYPEUSDT");
    expect(screen.getByText("BTCUSDT")).toBeTruthy();
    expect(screen.getByText("SOLUSDT")).toBeTruthy();
    expect(screen.queryByText("ETHUSDT")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "偏多" }));
    expect(screen.getByText("BTCUSDT")).toBeTruthy();
    expect(screen.queryByText("HYPEUSDT")).toBeNull();
    expect(screen.queryByText("SOLUSDT")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "偏空" }));
    expect(screen.getByText("HYPEUSDT")).toBeTruthy();
    expect(screen.queryByText("BTCUSDT")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(screen.getByText("ETHUSDT")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /ETHUSDT/ }));
    await screen.findByRole("heading", { name: "观望" });
    fireEvent.click(screen.getByRole("button", { name: "方向" }));
    expect(screen.queryByRole("heading", { name: "观望" })).toBeNull();
    expect(screen.getByText("选择一个标的开始")).toBeTruthy();
  });

  it("queues the formal engine candidate batch", async () => {
    const ethRecord = { ...record, id: 8, symbol: "ETHUSDT", status: "pending" as const, result: null };
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/market-analyses/schedule") return response(idleSchedule);
      if (path === "/api/market-analyses?limit=30") return response([]);
      if (path === "/api/market-analyses/batch" && init?.method === "POST") {
        return response({
          status: "pending",
          analyses: [
            { id: 8, symbol: "ETHUSDT" },
            { id: 9, symbol: "SOLUSDT" },
          ],
        }, 202);
      }
      if (path === "/api/market-analyses/8") return response(ethRecord);
      throw new Error(`unexpected request: ${path}`);
    });
    render(<MarketAnalysisPanel
      engineRunning={false}
      provider="codex-auth"
      candidateSymbols={["ETHUSDT", "SOLUSDT"]}
    />);

    fireEvent.click(await screen.findByRole("button", { name: "分析候选（2）" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/market-analyses/batch",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(screen.getByText("ETHUSDT · SOLUSDT")).toBeTruthy();
  });

  it("enables the 15-minute automatic analysis schedule", async () => {
    const enabledSchedule: MarketAnalysisScheduleStatus = {
      ...idleSchedule,
      enabled: true,
      next_run_at: "2026-07-22T10:15:00Z",
    };
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/market-analyses?limit=30") return response([]);
      if (path === "/api/market-analyses/schedule" && !init?.method) return response(idleSchedule);
      if (path === "/api/market-analyses/schedule/start" && init?.method === "POST") {
        return response(enabledSchedule);
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<MarketAnalysisPanel
      engineRunning={false}
      provider="codex-auth"
      candidateSymbols={["BTCUSDT", "ETHUSDT"]}
    />);

    fireEvent.click(await screen.findByRole("button", { name: "启动自动分析" }));

    expect(await screen.findByText("等待下一轮")).toBeTruthy();
    expect(screen.getByText(/下轮/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "停止自动分析" })).toBeTruthy();
    expect(request).toHaveBeenCalledWith(
      "/api/market-analyses/schedule/start",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("batch refreshes outcomes and shows each result in analysis history", async () => {
    let updated = false;
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/market-analyses/schedule") return response(idleSchedule);
      if (path === "/api/market-analyses?limit=30") {
        return response(updated ? [activeRecord, stoppedRecord] : [record, shortRecord]);
      }
      if (path === "/api/market-analyses/outcomes" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ analysis_ids: [7, 9] });
        updated = true;
        return response({ updated_ids: [7, 9], errors: [] });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<MarketAnalysisPanel engineRunning={false} provider="codex-auth" />);

    expect((await screen.findAllByText("尚未评估")).length).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "批量更新结果（2）" }));

    await screen.findByText("入场已触发");
    expect(screen.getByText("结构止损")).toBeTruthy();
    expect(request).toHaveBeenCalledWith(
      "/api/market-analyses/outcomes",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("shows pre-entry stop and T1 outcomes without implying an entry", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/market-analyses/schedule") return response(idleSchedule);
      if (path === "/api/market-analyses?limit=30") {
        return response([stoppedBeforeEntryRecord, target1BeforeEntryRecord]);
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<MarketAnalysisPanel engineRunning={false} provider="codex-auth" />);

    expect(await screen.findByText("未入场止损")).toBeTruthy();
    expect(screen.getByText("未入场 T1")).toBeTruthy();
  });
});
