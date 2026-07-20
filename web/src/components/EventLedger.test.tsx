import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RecoveryEvent } from "../api/events";
import { EventLedger } from "./EventLedger";

const events: RecoveryEvent[] = [
  {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    seq: 2,
    type: "approval.required",
    terminal: false,
    data: {
      phase: "Authorize",
      summary: "Exact remedy requires consent.",
      toolCallId: "call-server-742",
      executionStarted: false,
    },
    createdAt: "2026-07-18T20:00:02Z",
  },
  {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    seq: 1,
    type: "recovery.detected",
    terminal: false,
    data: { bookingId: "booking-server-742" },
    createdAt: "2026-07-18T20:00:01Z",
  },
];

afterEach(cleanup);

describe("EventLedger", () => {
  it("renders a truthful empty state without inventing activity", () => {
    render(<EventLedger events={[]} />);

    const ledger = screen.getByRole("region", { name: "Recovery event ledger" });
    expect(within(ledger).getByText("No recovery events have been received.")).toBeVisible();
    expect(within(ledger).queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders ordered stream fields and exact event data in a semantic table", () => {
    render(<EventLedger events={events} />);

    const table = screen.getByRole("table", { name: "Recovery events" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Sequence",
      "Event",
      "Recorded at",
      "Phase",
      "Summary",
      "Terminal",
      "Event data",
    ]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("recovery.detected");
    expect(rows[0]).toHaveTextContent('"bookingId":"booking-server-742"');
    expect(rows[1]).toHaveTextContent("approval.required");
    expect(rows[1]).toHaveTextContent('"executionStarted":false');
    expect(rows[1]).toHaveTextContent("Authorize");
    expect(rows[1]).toHaveTextContent("Exact remedy requires consent.");
    expect(rows[1]).toHaveTextContent("No");
    expect(rows[1]).not.toHaveTextContent('"phase"');
    expect(rows[1]).not.toHaveTextContent('"summary"');
    expect(within(table).queryByText("Actor")).not.toBeInTheDocument();
    expect(within(table).queryByText("Status")).not.toBeInTheDocument();
  });

  it("uses an ordered semantic list for the compact mobile presentation", () => {
    render(<EventLedger events={events} compact />);

    const list = screen.getByRole("list", { name: "Recovery events" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
