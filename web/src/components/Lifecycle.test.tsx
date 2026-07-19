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

  it("labels uncertain execution as unknown instead of recorded", () => {
    render(<Lifecycle scenario={terminalScenario("outcome_unknown")} />);
    const items = within(screen.getByRole("list", { name: "Recovery lifecycle" }))
      .getAllByRole("listitem");

    expect(items[3]).toHaveTextContent("Declined");
    expect(items[4]).toHaveTextContent("Unknown");
    expect(items[4]).not.toHaveTextContent("Recorded");
    expect(items[5]).toHaveTextContent("Outcome unknown");
  });
});
