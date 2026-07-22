import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BacktestDecisionLog,
  BacktestSymbolList,
  CadenceSelector,
  formatDailyLossPercent,
  LoginScreen,
  ProviderChoiceButton,
  WebUpdatePanel,
} from "./App";
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

describe("ProviderChoiceButton", () => {
  it("selects one Provider and locks the current selection", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <ProviderChoiceButton name="codex-auth" selected={false} disabled={false} onSelect={onSelect}>
        Codex Auth
      </ProviderChoiceButton>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Codex Auth" }));
    expect(onSelect).toHaveBeenCalledWith("codex-auth");

    rerender(
      <ProviderChoiceButton name="codex-auth" selected disabled={false} onSelect={onSelect}>
        Codex Auth
      </ProviderChoiceButton>,
    );
    expect(screen.getByRole("button", { name: "Codex Auth" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Codex Auth" }).hasAttribute("disabled")).toBe(true);
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

    const username = screen.getByLabelText("用户名") as HTMLInputElement;
    const password = screen.getByLabelText("密码") as HTMLInputElement;
    const form = username.form;
    expect(username.name).toBe("username");
    expect(username.autocomplete).toBe("username");
    expect(password.name).toBe("password");
    expect(password.autocomplete).toBe("current-password");
    expect(form?.name).toBe("candlepilot-login");
    expect(form?.method).toBe("post");
    expect(form?.getAttribute("action")).toBe("/api/auth/login");

    fireEvent.change(username, { target: { value: "operator" } });
    fireEvent.change(password, { target: { value: "correct horse battery staple" } });
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
    expect(password.value).toBe("correct horse battery staple");
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

describe("WebUpdatePanel", () => {
  it("checks first and only then enables the separately confirmed install", async () => {
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.endsWith("/check") ? {
        supported: true,
        checked_at: "2026-07-22T10:00:00Z",
        branch: "main",
        current_commit: "a".repeat(40),
        latest_commit: "b".repeat(40),
        update_available: true,
        message: "发现可安装的新版本",
      } : {
        supported: true,
        phase: "idle",
        message: "ready",
        started_at: null,
        finished_at: null,
        from_commit: null,
        current_commit: null,
        backup: null,
      };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    render(<WebUpdatePanel busy={null} setBusy={vi.fn()} setError={vi.fn()} />);

    const check = await screen.findByRole("button", { name: "检查更新" });
    const install = screen.getByRole("button", { name: "安装更新" });
    await waitFor(() => expect(check.hasAttribute("disabled")).toBe(false));
    expect(install.hasAttribute("disabled")).toBe(true);
    fireEvent.click(check);
    await waitFor(() => expect(install.hasAttribute("disabled")).toBe(false));
    expect(screen.getByText("发现可安装的新版本")).toBeTruthy();
    fireEvent.click(install);

    expect(screen.getByText(/服务会短暂离线/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "确认更新" })).toBeTruthy();
    expect(request).toHaveBeenCalledWith("/api/update/status", expect.anything());
    expect(request).toHaveBeenCalledWith("/api/update/check", expect.objectContaining({ method: "POST" }));
    request.mockRestore();
  });

  it("keeps installation disabled when the current commit is latest", async () => {
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const isCheck = String(input).endsWith("/check");
      return new Response(JSON.stringify(isCheck ? {
        supported: true,
        checked_at: "2026-07-22T10:00:00Z",
        branch: "main",
        current_commit: "a".repeat(40),
        latest_commit: "a".repeat(40),
        update_available: false,
        message: "当前已是最新版本",
      } : {
        supported: true,
        phase: "idle",
        message: "ready",
        started_at: null,
        finished_at: null,
        from_commit: null,
        current_commit: null,
        backup: null,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    render(<WebUpdatePanel busy={null} setBusy={vi.fn()} setError={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "检查更新" }));
    expect(await screen.findByText("当前已是最新版本")).toBeTruthy();
    expect(screen.getByRole("button", { name: "安装更新" }).hasAttribute("disabled")).toBe(true);
    request.mockRestore();
  });

  it("explains why web update is unavailable without the root helper", async () => {
    const request = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        supported: false,
        phase: "idle",
        message: "网页更新仅在通过 VPS 安装器部署更新助手后可用",
        started_at: null,
        finished_at: null,
        from_commit: null,
        current_commit: null,
        backup: null,
      }), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    render(<WebUpdatePanel busy={null} setBusy={vi.fn()} setError={vi.fn()} />);

    expect(await screen.findByText(/部署更新助手后可用/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "检查更新" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "安装更新" }).hasAttribute("disabled")).toBe(true);
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

describe("BacktestSymbolList", () => {
  it("renders long symbol sets as separate wrapping items", () => {
    const symbols = ["BTCUSDT", "ETHUSDT", "BANKUSDT", "DEXEUSDT", "ESPORTSUSDT"];

    render(<BacktestSymbolList symbols={symbols} />);

    const list = screen.getByRole("list", { name: /回测标的/ });
    expect(list.classList.contains("run-symbols")).toBe(true);
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual(symbols);
  });
});
