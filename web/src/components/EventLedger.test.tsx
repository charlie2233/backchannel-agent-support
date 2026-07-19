import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RecoveryEvent } from "../api/events";
import { EventLedger } from "./EventLedger";

afterEach(cleanup);

const events: RecoveryEvent[] = [
  {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    seq: 2,
    type: "approval.requested",
    terminal: false,
    data: { phase: "Authorize", summary: "Human consent requested." },
    createdAt: "2026-07-19T12:00:02Z",
  },
  {
    recoveryId: "11111111-2222-4333-8444-555555555555",
    seq: 1,
    type: "recovery.detected",
    terminal: false,
    data: { phase: "Detect", summary: "Booking conflict detected." },
    createdAt: "2026-07-19T12:00:01Z",
  },
];

describe("EventLedger", () => {
  it("renders authoritative server order, UTC timestamps, and supplied summaries", () => {
    render(<EventLedger events={events} mobile={false} rootTraceId="qa_trace_0123456789abcdef0123456789abcdef" />);

    const table = screen.getByRole("table", { name: "Authoritative server event ledger" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("1");
    expect(rows[0]).toHaveTextContent("2026-07-19 12:00:01 UTC");
    expect(rows[0]).toHaveTextContent("Booking conflict detected.");
    expect(rows[1]).toHaveTextContent("Human consent requested.");
    expect(within(table).queryByRole("columnheader", { name: /actor/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Latest event: Human consent requested\./)).toHaveAttribute(
      "aria-live",
      "polite",
    );
  });

  it("uses a compact ordered ledger on mobile without duplicating the desktop table", () => {
    render(<EventLedger events={events} mobile rootTraceId={null} />);

    const list = screen.getByRole("list", { name: "Authoritative server event ledger" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
