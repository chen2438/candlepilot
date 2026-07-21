import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BacktestDecisionLog, CadenceSelector, formatDailyLossPercent, LoginScreen } from "./App";
import type { BacktestDecisionPage } from "./types";

afterEach(cleanup);

it("formats the active daily loss fraction as a percentage", () => {
  expect(formatDailyLossPercent("0.05")).toBe("5.0%");
  expect(formatDailyLossPercent("0.075")).toBe("7.5%");
});

describe("CadenceSelector", () => {
  it("shows one selected cadence and replaces it with the clicked cadence", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <CadenceSelector
        active="15m"
        supported={["5m", "15m", "30m", "1h", "4h"]}
        disabled={false}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByRole("button", { name: "15m" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getAllByRole("button", { pressed: true })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "30m" }));
    expect(onSelect).toHaveBeenCalledWith("30m");

    rerender(
      <CadenceSelector
        active="30m"
        supported={["5m", "15m", "30m", "1h", "4h"]}
        disabled={false}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByRole("button", { name: "30m" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getAllByRole("button", { pressed: true })).toHaveLength(1);
  });
});

describe("LoginScreen", () => {
  it("submits credentials and enters the authenticated console", async () => {
    const onAuthenticated = vi.fn();
    const request = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ enabled: true, authenticated: true, username: "operator" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<LoginScreen onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct horse battery staple" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(onAuthenticated).toHaveBeenCalledWith({
      enabled: true,
      authenticated: true,
      username: "operator",
    }));
    expect(request).toHaveBeenCalledWith("/api/auth/login", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ username: "operator", password: "correct horse battery staple" }),
    }));
    request.mockRestore();
  });

  it("keeps the login form visible after rejected credentials", async () => {
    const request = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid username or password" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<LoginScreen onAuthenticated={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect((await screen.findByRole("alert")).textContent).toContain("invalid username or password");
    request.mockRestore();
  });
});

describe("BacktestDecisionLog", () => {
  it("shows pagination progress and requests the next page", () => {
    const onLoadMore = vi.fn();
    const page: BacktestDecisionPage = {
      items: [{
        id: 1,
        provider: "local-rule",
        decided_at: "2026-07-20T12:00:00Z",
        symbol: "BTCUSDT",
        cadence: "5m",
        outcome: "hold",
        action: "HOLD",
        confidence: 0.5,
        rationale: "没有入场信号",
        detail: null,
        attempt_started_at: [],
        fill: null,
      }],
      total: 101,
      has_more: true,
      next_after_id: 1,
    };

    render(
      <BacktestDecisionLog
        page={page}
        localTimeZone="Europe/London"
        loadingMore={false}
        onLoadMore={onLoadMore}
      />,
    );

    expect(screen.getByText("已加载 1 / 101 条决策")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(onLoadMore).toHaveBeenCalledOnce();
  });
});
