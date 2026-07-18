import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import App from "./App";

describe("Backchannel console", () => {
  it("renders exactly two approved scenarios and the ordered lifecycle", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            backend: "stub",
            liveReady: false,
            providerBoundary: "demo_adapter_only",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(<App />);

    const scenarioList = screen.getByRole("list", { name: "Recovery scenarios" });
    expect(within(scenarioList).getAllByRole("listitem")).toHaveLength(2);
    expect(
      within(scenarioList).getByRole("button", { name: /Hotel booking recovery/i }),
    ).toBeInTheDocument();
    expect(
      within(scenarioList).getByRole("button", { name: /API quota recovery/i }),
    ).toBeInTheDocument();

    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });
    expect(within(lifecycle).getAllByRole("listitem")).toHaveLength(6);
    expect(within(lifecycle).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Detect"),
      expect.stringContaining("Prove"),
      expect.stringContaining("Negotiate"),
      expect.stringContaining("Authorize"),
      expect.stringContaining("Execute"),
      expect.stringContaining("Verify & seal"),
    ]);

    expect(await screen.findByText("Replay fixture")).toBeInTheDocument();
    expect(screen.queryByText(/GPT-5\.6 agents/i)).not.toBeInTheDocument();

    vi.unstubAllGlobals();
  });
});
