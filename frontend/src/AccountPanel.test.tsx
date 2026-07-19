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
  unrealized_pnl: "0.045",
  notional: "972",
  margin_used: "486",
  stop_loss: "64000",
  take_profit: "65500",
  protection_source: "exchange",
};

describe("AccountPanel manual close", () => {
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
