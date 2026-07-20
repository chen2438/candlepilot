import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CadenceSelector } from "./App";

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
