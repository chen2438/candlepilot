import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodexAuthSourceSelect, codexProviderIdentity } from "./App";

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
});
