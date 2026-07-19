import type { RecoveryEvent } from "../api/events";

interface EventLedgerProps {
  events: ReadonlyArray<RecoveryEvent>;
  error?: string | null;
  mobile: boolean;
  rootTraceId: string | null;
}

function formatUtc(timestamp: string): string {
  return new Date(timestamp).toISOString().replace("T", " ").replace(".000Z", " UTC");
}

function publicSummary(event: RecoveryEvent): string {
  return typeof event.data.summary === "string"
    ? event.data.summary
    : "No public summary supplied.";
}

function eventLabel(type: string): string {
  const words = type.replace(/[._-]+/g, " ");
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`;
}

export function EventLedger({ events, error = null, mobile, rootTraceId }: EventLedgerProps) {
  const orderedEvents = [...events].sort((left, right) => left.seq - right.seq);
  const latest = orderedEvents.at(-1);

  return (
    <section className="event-ledger" aria-labelledby="event-ledger-heading">
      <div className="event-ledger-heading">
        <h2 id="event-ledger-heading">Event ledger</h2>
        {rootTraceId === null ? null : (
          <p>
            Root trace ID <code>{rootTraceId}</code>
          </p>
        )}
      </div>

      {orderedEvents.length === 0 ? (
        <p className="empty-evidence">No server events have been received for this recovery.</p>
      ) : mobile ? (
        <ol className="event-ledger-list" aria-label="Authoritative server event ledger">
          {orderedEvents.map((event) => (
            <li key={event.seq}>
              <div>
                <strong>{eventLabel(event.type)}</strong>
                <time dateTime={event.createdAt}>{formatUtc(event.createdAt)}</time>
              </div>
              <p>{publicSummary(event)}</p>
              <span>{event.terminal ? "Terminal evidence" : `Sequence ${event.seq}`}</span>
            </li>
          ))}
        </ol>
      ) : (
        <div className="event-table-wrap">
          <table>
            <caption>Authoritative server event ledger</caption>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Time (UTC)</th>
                <th scope="col">Event</th>
                <th scope="col">Details</th>
                <th scope="col">Record</th>
              </tr>
            </thead>
            <tbody>
              {orderedEvents.map((event) => (
                <tr key={event.seq}>
                  <td>{event.seq}</td>
                  <td><time dateTime={event.createdAt}>{formatUtc(event.createdAt)}</time></td>
                  <td>{eventLabel(event.type)}</td>
                  <td>{publicSummary(event)}</td>
                  <td>{event.terminal ? "Terminal" : "Recorded"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {latest === undefined ? null : (
        <p className="sr-only" aria-live="polite" aria-atomic="true">
          Latest event: {publicSummary(latest)}
        </p>
      )}
      {error === null ? null : (
        <p className="stream-status" role="status">
          {error}. The browser will keep trying to reconnect.
        </p>
      )}
    </section>
  );
}
