import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountPanel, fillDirectionLabel, TrailingStopPanel } from "./App";
import type { AccountPosition } from "./types";

afterEach(cleanup);

const position: AccountPosition = {
  symbol: "BTCUSDT",
  side: "LONG",
  quantity: "0.015",
  average_price: "64797",
  mark_price: "64800",
  leverage: 2,
  unrealized_pnl: "24.3",
  notional: "972",
  margin_used: "486",
  stop_loss: "64000",
  take_profit: "65500",
  protection_source: "exchange",
};

describe("AccountPanel manual close", () => {
  it("labels fills by position effect instead of exchange buy or sell side", () => {
    expect(fillDirectionLabel({ side: "BUY", reduce_only: false })).toBe("开多");
    expect(fillDirectionLabel({ side: "SELL", reduce_only: false })).toBe("开空");
    expect(fillDirectionLabel({ side: "SELL", reduce_only: true })).toBe("平多");
    expect(fillDirectionLabel({ side: "BUY", reduce_only: true })).toBe("平空");
    expect(fillDirectionLabel({ side: null, reduce_only: false })).toBe("—");
  });

  it("labels the account result as a rolling 24-hour metric", () => {
    render(
      <AccountPanel
        portfolio={{
          source: "binance-testnet",
          initial_equity: null,
          cash: "1000",
          equity: "990",
          available_balance: "900",
          pnl_24h: "-10",
          unrealized_pnl: "-2",
          open_positions: 0,
          margin_used: "0",
        }}
        positions={[]}
        fills={[]}
        testnetStatus={null}
        engineRunning={false}
        busy={null}
        onClosePosition={vi.fn(async () => true)}
      />,
    );

    const metric = screen.getByText("过去24h盈亏").closest("div");
    expect(metric?.textContent).toContain("-10.00");
    expect(metric?.getAttribute("data-tooltip")).toContain("当前时刻往前 24 小时");
  });

  it("shows position return on margin and USDT fill notional with realized return", () => {
    render(
      <AccountPanel
        portfolio={null}
        positions={[position]}
        fills={[{
          id: 1,
          source: "exchange_user_stream",
          client_order_id: "cp-entry-sl",
          related_client_order_id: "cp-entry",
          symbol: "BTCUSDT",
          side: "SELL",
          purpose: "entry",
          reduce_only: true,
          realized_pnl: "-10",
          notional_usdt: "200",
          realized_pnl_margin_usdt: "100",
          realized_return_percent: "-10",
          status: "FILLED",
          report: {
            filled_quantity: "2",
            average_price: "100",
            message: "filled",
          },
          created_at: "2026-07-19T15:00:00Z",
        }]}
        testnetStatus={null}
        engineRunning={false}
        busy={null}
        onClosePosition={vi.fn(async () => true)}
      />,
    );

    expect(screen.getByText("+5.00%")).toBeTruthy();
    expect(screen.getByText("200.00 USDT")).toBeTruthy();
    expect(screen.getByText("平多")).toBeTruthy();
    expect(screen.getByText("其他平仓")).toBeTruthy();
    expect(screen.getByText("-10.00%")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "成交额（USDT）" }).getAttribute("data-tooltip"))
      .toContain("不是保证金、账户扣款或盈亏");
    expect(screen.getByRole("columnheader", { name: "原始盈亏比" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "未实现盈亏" }).getAttribute("data-tooltip"))
      .toContain("保证金回报率");
    expect(screen.getByRole("columnheader", { name: "止损" }).getAttribute("data-tooltip"))
      .toContain("不乘杠杆");
    expect(screen.getByRole("columnheader", { name: "止盈" }).getAttribute("data-tooltip"))
      .toContain("不乘杠杆");
  });

  it("requires an explicit confirmation before closing the whole position", async () => {
    const user = userEvent.setup();
    const closePosition = vi.fn(async () => true);

    render(
      <AccountPanel
        portfolio={null}
        positions={[position]}
        fills={[]}
        testnetStatus={null}
        engineRunning={false}
        busy={null}
        onClosePosition={closePosition}
      />,
    );

    await user.click(screen.getByRole("button", { name: "市价平仓" }));
    expect(screen.getByText("确认全部平仓？")).toBeTruthy();
    expect(closePosition).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "确认" }));
    expect(closePosition).toHaveBeenCalledOnce();
    expect(closePosition).toHaveBeenCalledWith("BTCUSDT");
    expect(screen.getByRole("button", { name: "市价平仓" })).toBeTruthy();
  });

  it("keeps manual close disabled while the engine is running", () => {
    render(
      <AccountPanel
        portfolio={null}
        positions={[position]}
        fills={[]}
        testnetStatus={null}
        engineRunning
        busy={null}
        onClosePosition={vi.fn(async () => true)}
      />,
    );

    const button = screen.getByRole("button", { name: "市价平仓" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe("请先停止交易引擎");
  });
});

describe("TrailingStopPanel", () => {
  it("shows every shadow strategy and its recent candidate without a raw API", () => {
    render(<TrailingStopPanel
      status={{
        mode: "shadow",
        strategies: [
          { profile_id: "0.5R / 0.5R", activation_r: "0.5", distance_r: "0.5" },
          { profile_id: "2R / 1R", activation_r: "2", distance_r: "1" },
        ],
        managed_positions: 1,
        active_positions: 1,
        active_strategies: 1,
        simulated_fills: 1,
        last_event: null,
      }}
      events={[{
        id: 7,
        symbol: "BTCUSDT",
        mode: "shadow",
        status: "simulated_filled",
        event: {
          side: "LONG",
          quantity: "0.01",
          entry_price: "65000",
          mark_price: "65500",
          original_stop: "64000",
          best_mark: "65500",
          previous_stop: "64000",
          candidate_stop: "65000",
          simulated_fill_price: "64990",
          profile_id: "0.5R / 0.5R",
          activation_r: "0.5",
          distance_r: "0.5",
          detail: "",
        },
        created_at: "2026-07-21T07:00:00Z",
      }]}
      error={null}
    />);

    expect(screen.getByText("移动止损观测")).toBeTruthy();
    expect(screen.getAllByText("0.5R / 0.5R").length).toBeGreaterThan(1);
    expect(screen.getByText("2R / 1R")).toBeTruthy();
    expect(screen.getByText("65000.0000")).toBeTruthy();
    expect(screen.getByText("模拟成交")).toBeTruthy();
    expect(screen.getByText(/观察 64990\.0000/)).toBeTruthy();
    expect(screen.getByText(/只记录，不改单/)).toBeTruthy();
  });
});
