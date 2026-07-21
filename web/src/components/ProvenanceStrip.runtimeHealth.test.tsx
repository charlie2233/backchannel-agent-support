import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProvenanceStrip } from "./ProvenanceStrip";

afterEach(cleanup);

describe("ProvenanceStrip runtime-health states", () => {
  it("truthfully distinguishes the initial check from an automatic retry", () => {
    const { container, rerender } = render(
      <ProvenanceStrip presentation={null} healthPhase="checking" />,
    );

    expect(screen.getByText("Checking runtime")).toBeVisible();
    expect(container.querySelector("section")).toHaveAttribute("aria-live", "polite");
    expect(container.querySelector("section")).toHaveAttribute("aria-atomic", "true");
    expect(container.querySelector("section")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("button", { name: "Retry runtime check" })).not.toBeInTheDocument();

    rerender(<ProvenanceStrip presentation={null} healthPhase="retrying" />);

    expect(screen.getByText("Retrying runtime check")).toBeVisible();
    expect(
      screen.getByText(
        "The health endpoint could not be verified. Retrying before making any runtime claim.",
      ),
    ).toBeVisible();
    expect(container.querySelector("section")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("button", { name: "Retry runtime check" })).not.toBeInTheDocument();
  });

  it("offers one accessible manual retry only after automatic attempts are exhausted", () => {
    const onRetryRuntime = vi.fn();
    const { container } = render(
      <ProvenanceStrip
        presentation={null}
        healthPhase="unavailable"
        onRetryRuntime={onRetryRuntime}
      />,
    );

    expect(screen.getByText("Runtime unavailable")).toBeVisible();
    expect(
      screen.getByText(
        "The health endpoint could not be verified after repeated checks, so no runtime claim is shown.",
      ),
    ).toBeVisible();
    expect(container.querySelector("section")).toHaveAttribute("aria-busy", "false");
    expect(container.querySelector("section")).toHaveAttribute("aria-live", "polite");
    fireEvent.click(screen.getByRole("button", { name: "Retry runtime check" }));
    expect(onRetryRuntime).toHaveBeenCalledTimes(1);
  });
});
