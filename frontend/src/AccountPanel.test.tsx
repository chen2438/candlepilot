import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountPanel } from "./App";
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
          purpose: "stop_loss",
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
    expect(screen.getByText("-10.00%")).toBeTruthy();
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
