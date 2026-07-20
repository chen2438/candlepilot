import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CadenceSelector, LoginScreen } from "./App";

afterEach(cleanup);

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
