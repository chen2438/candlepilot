import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodexAuthSourceSelect, CodexCliAuthControls, codexProviderIdentity, codexWindowLabel } from "./App";

afterEach(cleanup);

describe("Codex provider identity", () => {
  it("shows the selected source, account email, and CLI version", () => {
    expect(codexProviderIdentity({
      auth_source: "chatgpt-app",
      account_email: "trader@example.com",
      version: "codex-cli 1.2.3",
      detail: "Logged in using ChatGPT",
    })).toBe("ChatGPT App · trader@example.com · codex-cli 1.2.3");
  });

  it("lets the user select the standalone Codex CLI", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <CodexAuthSourceSelect
        value="chatgpt-app"
        options={["chatgpt-app", "codex-cli"]}
        disabled={false}
        onChange={onChange}
      />,
    );

    const select = screen.getByRole("combobox", { name: "Codex 接入来源" });
    expect(screen.getByRole("option", { name: "ChatGPT App" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Codex CLI" })).toBeTruthy();
    await user.selectOptions(select, "codex-cli");
    expect(onChange).toHaveBeenCalledWith("codex-cli");
  });

  it("shows device authorization and requires confirmation before logout", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const onLogin = vi.fn();
    const onLogout = vi.fn();
    const { rerender } = render(
      <CodexCliAuthControls
        authenticated={false}
        busy={false}
        disabled={false}
        onCancel={onCancel}
        onLogin={onLogin}
        onLogout={onLogout}
        onRefreshUsage={vi.fn()}
        usage={null}
        usageLoading={false}
        session={{
          available: true,
          state: "pending",
          verification_uri: "https://auth.openai.com/codex/device",
          user_code: "ABCD-EFGH",
          message: "请在授权页面输入一次性代码",
          started_at: "2026-07-22T12:00:00Z",
          finished_at: null,
        }}
      />,
    );

    expect(screen.getByRole("link", { name: "打开 Codex 授权页面" }).getAttribute("href"))
      .toBe("https://auth.openai.com/codex/device");
    expect(screen.getByLabelText("Codex 一次性代码").textContent).toBe("ABCD-EFGH");
    await user.click(screen.getByRole("button", { name: "取消登录" }));
    expect(onCancel).toHaveBeenCalledOnce();

    rerender(<CodexCliAuthControls
      authenticated={true}
      busy={false}
      disabled={false}
      onCancel={onCancel}
      onLogin={onLogin}
      onLogout={onLogout}
      onRefreshUsage={vi.fn()}
      usage={{
        available: true,
        buckets: [{
          limit_id: "codex",
          limit_name: "Codex",
          plan_type: "team",
          windows: [{
            kind: "primary",
            used_percent: 23,
            remaining_percent: 77,
            window_duration_minutes: 43200,
            resets_at: "2026-08-01T00:00:00Z",
          }],
        }],
        checked_at: "2026-07-22T12:00:00Z",
        message: "Codex 额度已刷新",
      }}
      usageLoading={false}
      session={null}
    />);
    expect(screen.getByText("Team · 月额度")).toBeTruthy();
    expect(screen.getByText("77%")).toBeTruthy();
    expect(screen.queryByText(/周额度/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "登出" }));
    expect(onLogout).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "确认登出" }));
    expect(onLogout).toHaveBeenCalledOnce();
  });

  it("labels only the window duration actually returned by Codex", () => {
    expect(codexWindowLabel(43200, "Codex")).toBe("月额度");
    expect(codexWindowLabel(null, "Team monthly")).toBe("Team monthly");
  });
});
