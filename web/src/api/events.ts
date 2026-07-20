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
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  close(): void;
}

const streamCapacityCode = "event_stream_capacity";
const streamCapacityMessage =
  "Event streaming is temporarily at capacity; retry is automatic.";

function hasExactKeys(value: Record<string, unknown>, keys: ReadonlyArray<string>): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function isUtcTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    (value.endsWith("Z") || value.endsWith("+00:00")) &&
    !Number.isNaN(Date.parse(value))
  );
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
    hasExactKeys(candidate, ["recoveryId", "seq", "type", "terminal", "data", "createdAt"]) &&
    typeof candidate.recoveryId === "string" &&
    Number.isInteger(candidate.seq) &&
    typeof candidate.seq === "number" &&
    candidate.seq > 0 &&
    typeof candidate.type === "string" &&
    typeof candidate.terminal === "boolean" &&
    typeof candidate.data === "object" &&
    candidate.data !== null &&
    !Array.isArray(candidate.data) &&
    isUtcTimestamp(candidate.createdAt)
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

function parseStreamCapacityControl(serialized: unknown): string {
  if (typeof serialized !== "string") {
    throw new Error("Stream capacity control did not match the public contract");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized) as unknown;
  } catch {
    throw new Error("Stream capacity control did not match the public contract");
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new Error("Stream capacity control did not match the public contract");
  }
  const candidate = parsed as Record<string, unknown>;
  if (
    !hasExactKeys(candidate, ["code", "message", "requestId"]) ||
    candidate.code !== streamCapacityCode ||
    candidate.message !== streamCapacityMessage ||
    typeof candidate.requestId !== "string" ||
    !/^[0-9a-f]{32}$/.test(candidate.requestId)
  ) {
    throw new Error("Stream capacity control did not match the public contract");
  }
  return streamCapacityMessage;
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
  let capacityRetryPending = false;
  const closeOnce = () => {
    if (closed) {
      return;
    }
    closed = true;
    source.removeEventListener("stream.capacity", onStreamCapacity);
    source.close();
  };
  const onStreamCapacity: EventListener = (event) => {
    if (closed) {
      return;
    }
    try {
      const message = parseStreamCapacityControl(
        event instanceof MessageEvent ? event.data : null,
      );
      capacityRetryPending = true;
      handlers.onError?.(new Error(message));
    } catch (error) {
      handlers.onError?.(
        error instanceof Error
          ? error
          : new Error("Stream capacity control did not match the public contract"),
      );
    }
  };
  source.addEventListener("stream.capacity", onStreamCapacity);
  source.onmessage = (message) => {
    if (closed) {
      return;
    }
    try {
      const event = parseRecoveryEvent(message.data);
      if (event.recoveryId !== recoveryId) {
        throw new Error("Recovery event did not belong to the active recovery");
      }
      capacityRetryPending = false;
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
    if (capacityRetryPending) {
      capacityRetryPending = false;
      return;
    }
    handlers.onError?.(new Error("Recovery event stream disconnected"));
  };
  return closeOnce;
}
