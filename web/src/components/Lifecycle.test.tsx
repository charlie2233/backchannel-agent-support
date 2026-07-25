import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { lifecycleSteps, type RecoveryScenario } from "../domain/recovery";
import { recoveryScenarios } from "../fixtures/recoveries";
import { Lifecycle } from "./Lifecycle";

afterEach(cleanup);

function terminalScenario(
  status: "closed_without_action" | "outcome_unknown",
): RecoveryScenario {
  return {
    ...recoveryScenarios[0],
    status,
    currentStep: 5,
    currentStepSummary: status === "closed_without_action" ? "Closed." : "Unknown.",
  };
}

describe("Lifecycle terminal truth", () => {
  it.each([
    { phase: "idle", label: "Not started" },
    { phase: "awaiting", label: "Awaiting server evidence" },
  ] as const)("renders a neutral $phase lifecycle without a current step", ({ phase, label }) => {
    render(<Lifecycle phase={phase} scenario={recoveryScenarios[0]} />);
    const lifecycle = screen.getByRole("list", { name: "Recovery lifecycle" });

    expect(within(lifecycle).queryByRole("listitem", { current: "step" })).not.toBeInTheDocument();
    expect(screen.getByText(label, { selector: ".step-count" })).toBeVisible();
    expect(screen.queryByText("Step 1 of 6")).not.toBeInTheDocument();
  });

  it("keeps the six-step timeline compact while exposing one readable current-step detail", () => {
    const scenario = recoveryScenarios[0];
    render(<Lifecycle scenario={scenario} />);

    const detail = screen.getByRole("note", { name: "Current step detail" });
    expect(detail).toHaveTextContent(lifecycleSteps[scenario.currentStep]);
    expect(detail).toHaveTextContent(
      scenario.lifecycleDetails[lifecycleSteps[scenario.currentStep]],
    );
  });

  it("labels declined authorization, skipped execution, and closure without recording execution", () => {
    render(<Lifecycle scenario={terminalScenario("closed_without_action")} />);
    const items = within(screen.getByRole("list", { name: "Recovery lifecycle" }))
      .getAllByRole("listitem");

    expect(items[3]).toHaveTextContent("Declined");
    expect(items[4]).toHaveTextContent("Not run");
    expect(items[4]).not.toHaveTextContent("Recorded");
    expect(items[5]).toHaveTextContent("Closed");
  });

  it("labels uncertain authorization generically and execution as unknown", () => {
    render(<Lifecycle scenario={terminalScenario("outcome_unknown")} />);
    const items = within(screen.getByRole("list", { name: "Recovery lifecycle" }))
      .getAllByRole("listitem");

    expect(items[3]).toHaveTextContent("Claimed");
    expect(items[3]).not.toHaveTextContent("Declined");
    expect(items[4]).toHaveTextContent("Unknown");
    expect(items[4]).not.toHaveTextContent("Recorded");
    expect(items[5]).toHaveTextContent("Outcome unknown");
  });
});
