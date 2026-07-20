import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TechnicalEvidence } from "./TechnicalEvidence";

afterEach(cleanup);

function disclosure(): HTMLDetailsElement {
  const element = screen.getByText("Technical evidence").closest("details");
  if (!(element instanceof HTMLDetailsElement)) {
    throw new Error("Technical evidence disclosure is missing");
  }
  return element;
}

describe("TechnicalEvidence breakpoint defaults", () => {
  it("moves from the mobile collapsed default to the desktop expanded default", () => {
    const view = render(
      <TechnicalEvidence expanded={false} preferenceScope="mobile">
        Exact evidence
      </TechnicalEvidence>,
    );
    expect(disclosure()).not.toHaveAttribute("open");

    view.rerender(
      <TechnicalEvidence expanded preferenceScope="desktop">
        Exact evidence
      </TechnicalEvidence>,
    );
    expect(disclosure()).toHaveAttribute("open");
  });

  it("preserves deliberate toggles separately for mobile and desktop", () => {
    const view = render(
      <TechnicalEvidence expanded={false} preferenceScope="mobile">
        Exact evidence
      </TechnicalEvidence>,
    );
    fireEvent.click(screen.getByText("Technical evidence"));
    expect(disclosure()).toHaveAttribute("open");

    view.rerender(
      <TechnicalEvidence expanded preferenceScope="desktop">
        Exact evidence
      </TechnicalEvidence>,
    );
    expect(disclosure()).toHaveAttribute("open");
    fireEvent.click(screen.getByText("Technical evidence"));
    expect(disclosure()).not.toHaveAttribute("open");

    view.rerender(
      <TechnicalEvidence expanded={false} preferenceScope="mobile">
        Exact evidence
      </TechnicalEvidence>,
    );
    expect(disclosure()).toHaveAttribute("open");
    view.rerender(
      <TechnicalEvidence expanded preferenceScope="desktop">
        Exact evidence
      </TechnicalEvidence>,
    );
    expect(disclosure()).not.toHaveAttribute("open");
  });

  it("toggles from Enter and Space while retaining summary focus", () => {
    render(
      <TechnicalEvidence expanded={false} preferenceScope="mobile">
        Exact evidence
      </TechnicalEvidence>,
    );
    const summary = screen.getByText("Technical evidence").closest("summary");
    if (!(summary instanceof HTMLElement)) {
      throw new Error("Technical evidence summary is missing");
    }
    summary.focus();

    fireEvent.keyDown(summary, { key: "Enter" });
    expect(disclosure()).toHaveAttribute("open");
    expect(summary).toHaveFocus();

    fireEvent.keyDown(summary, { key: " " });
    expect(disclosure()).not.toHaveAttribute("open");
    expect(summary).toHaveFocus();
  });
});
