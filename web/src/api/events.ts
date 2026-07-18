export interface RecoveryEvent {
  recoveryId: string;
  seq: number;
  type: string;
  terminal: boolean;
  data: Readonly<Record<string, unknown>>;
  createdAt: string;
}

interface EventSourceContract {
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceContract;

export interface RecoveryEventHandlers {
  onEvent: (event: RecoveryEvent) => void;
  onError?: (error: Error) => void;
}

function isRecoveryEvent(value: unknown): value is RecoveryEvent {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.recoveryId === "string" &&
    Number.isInteger(candidate.seq) &&
    typeof candidate.seq === "number" &&
    candidate.seq > 0 &&
    typeof candidate.type === "string" &&
    typeof candidate.terminal === "boolean" &&
    typeof candidate.data === "object" &&
    candidate.data !== null &&
    !Array.isArray(candidate.data) &&
    typeof candidate.createdAt === "string"
  );
}

export function parseRecoveryEvent(serialized: string): RecoveryEvent {
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized) as unknown;
  } catch {
    throw new Error("Recovery event was not valid JSON");
  }
  if (!isRecoveryEvent(parsed)) {
    throw new Error("Recovery event did not match the stream contract");
  }
  return parsed;
}

export function connectRecoveryEvents(
  recoveryId: string,
  handlers: RecoveryEventHandlers,
  createEventSource: EventSourceFactory = (url) => new EventSource(url),
): () => void {
  const source = createEventSource(
    `/api/recoveries/${encodeURIComponent(recoveryId)}/events`,
  );
  let closed = false;
  const closeOnce = () => {
    if (closed) {
      return;
    }
    closed = true;
    source.close();
  };
  source.onmessage = (message) => {
    if (closed) {
      return;
    }
    try {
      const event = parseRecoveryEvent(message.data);
      if (event.recoveryId !== recoveryId) {
        throw new Error("Recovery event did not belong to the active recovery");
      }
      handlers.onEvent(event);
      if (event.terminal) {
        closeOnce();
      }
    } catch (error) {
      handlers.onError?.(
        error instanceof Error ? error : new Error("Recovery event could not be read"),
      );
    }
  };
  source.onerror = () => {
    if (closed) {
      return;
    }
    handlers.onError?.(new Error("Recovery event stream disconnected"));
  };
  return closeOnce;
}
