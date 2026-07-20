import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { recoveryScenarios } from "../fixtures/recoveries";
import { ScenarioRail } from "./ScenarioRail";

afterEach(cleanup);

describe("ScenarioRail action gating", () => {
  it("disables desktop scenario buttons until selection is allowed", () => {
    const onSelect = vi.fn();
    const view = render(
      <ScenarioRail
        activeId="hotel"
        disabled
        onSelect={onSelect}
        scenarios={recoveryScenarios}
      />,
    );

    const quota = screen.getByRole("button", { name: /API quota recovery/i });
    expect(quota).toBeDisabled();
    fireEvent.click(quota);
    expect(onSelect).not.toHaveBeenCalled();

    view.rerender(
      <ScenarioRail
        activeId="hotel"
        onSelect={onSelect}
        scenarios={recoveryScenarios}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /API quota recovery/i }));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith("api-quota");
  });

  it("disables the mobile selector until selection is allowed", () => {
    const onSelect = vi.fn();
    const view = render(
      <ScenarioRail
        activeId="hotel"
        disabled
        mobile
        onSelect={onSelect}
        scenarios={recoveryScenarios}
      />,
    );

    const selector = screen.getByRole("combobox", { name: "Scenario" });
    expect(selector).toBeDisabled();
    fireEvent.change(selector, { target: { value: "api-quota" } });
    expect(onSelect).not.toHaveBeenCalled();

    view.rerender(
      <ScenarioRail
        activeId="hotel"
        mobile
        onSelect={onSelect}
        scenarios={recoveryScenarios}
      />,
    );
    fireEvent.change(screen.getByRole("combobox", { name: "Scenario" }), {
      target: { value: "api-quota" },
    });
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith("api-quota");
  });
});
