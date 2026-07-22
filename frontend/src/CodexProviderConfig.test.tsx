import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodexAuthSourceSelect, CodexCliAuthControls, codexProviderIdentity } from "./App";

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
      session={null}
    />);
    await user.click(screen.getByRole("button", { name: "登出" }));
    expect(onLogout).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "确认登出" }));
    expect(onLogout).toHaveBeenCalledOnce();
  });
});
