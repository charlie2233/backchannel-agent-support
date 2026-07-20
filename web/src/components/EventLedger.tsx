import type { RecoveryEvent } from "../api/events";

interface EventLedgerProps {
  events: ReadonlyArray<RecoveryEvent>;
  compact?: boolean;
}

function orderedEvents(events: ReadonlyArray<RecoveryEvent>): RecoveryEvent[] {
  return [...events].sort((left, right) => left.seq - right.seq);
}

function optionalEventString(event: RecoveryEvent, key: "phase" | "summary"): string | null {
  const value = event.data[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function sortedValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortedValue);
  }
  if (typeof value !== "object" || value === null) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, nested]) => [key, sortedValue(nested)]),
  );
}

function remainingEventData(event: RecoveryEvent): string | null {
  const remaining = Object.fromEntries(
    Object.entries(event.data).filter(([key]) => key !== "phase" && key !== "summary"),
  );
  return Object.keys(remaining).length === 0
    ? null
    : JSON.stringify(sortedValue(remaining));
}

function EventData({ event }: { event: RecoveryEvent }) {
  const data = remainingEventData(event);
  return data === null ? (
    <span>None</span>
  ) : (
    <details className="event-ledger__data">
      <summary>Event data</summary>
      <code>{data}</code>
    </details>
  );
}

export function EventLedger({ events, compact = false }: EventLedgerProps) {
  const ordered = orderedEvents(events);
  return (
    <section className="event-ledger" aria-labelledby="event-ledger-heading">
      <div className="section-heading event-ledger__heading">
        <div>
          <p className="eyebrow">Server-sent events</p>
          <h2 id="event-ledger-heading">Recovery event ledger</h2>
        </div>
      </div>
      {ordered.length === 0 ? (
        <p className="event-ledger__empty">No recovery events have been received.</p>
      ) : compact ? (
        <ol className="event-ledger__list" aria-label="Recovery events">
          {ordered.map((event) => (
            <li key={`${event.recoveryId}:${event.seq}`}>
              <dl>
                <div>
                  <dt>Sequence</dt>
                  <dd>{event.seq}</dd>
                </div>
                <div>
                  <dt>Event</dt>
                  <dd className="mono">{event.type}</dd>
                </div>
                <div>
                  <dt>Recorded at</dt>
                  <dd>
                    <time dateTime={event.createdAt}>{event.createdAt}</time>
                  </dd>
                </div>
                <div>
                  <dt>Phase</dt>
                  <dd>{optionalEventString(event, "phase") ?? "None"}</dd>
                </div>
                <div>
                  <dt>Summary</dt>
                  <dd>{optionalEventString(event, "summary") ?? "None"}</dd>
                </div>
                <div>
                  <dt>Terminal</dt>
                  <dd>{event.terminal ? "Yes" : "No"}</dd>
                </div>
                <div>
                  <dt>Event data</dt>
                  <dd className="mono"><EventData event={event} /></dd>
                </div>
              </dl>
            </li>
          ))}
        </ol>
      ) : (
        <div className="event-ledger__table-wrap">
          <table aria-label="Recovery events">
            <thead>
              <tr>
                <th scope="col">Sequence</th>
                <th scope="col">Event</th>
                <th scope="col">Recorded at</th>
                <th scope="col">Phase</th>
                <th scope="col">Summary</th>
                <th scope="col">Terminal</th>
                <th scope="col">Event data</th>
              </tr>
            </thead>
            <tbody>
              {ordered.map((event) => (
                <tr key={`${event.recoveryId}:${event.seq}`}>
                  <td>{event.seq}</td>
                  <td className="mono">{event.type}</td>
                  <td>
                    <time dateTime={event.createdAt}>{event.createdAt}</time>
                  </td>
                  <td>{optionalEventString(event, "phase") ?? "None"}</td>
                  <td>{optionalEventString(event, "summary") ?? "None"}</td>
                  <td>{event.terminal ? "Yes" : "No"}</td>
                  <td className="mono"><EventData event={event} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
