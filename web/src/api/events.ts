import { readRecoveryEventStreamFailure } from "./client";

export interface RecoveryEvent {
  recoveryId: string;
  seq: number;
  type: string;
  terminal: boolean;
  data: Readonly<Record<string, unknown>>;
  createdAt: string;
}

export interface RecoveryEventStreamHandlers {
  onOpen: () => void;
  onEvent: (event: RecoveryEvent) => void;
}

export interface RecoveryEventStreamRequest {
  recoveryId: string;
  afterSeq: number;
  lastEvent: RecoveryEvent | null;
  signal: AbortSignal;
  fetchImpl?: typeof fetch;
}

export type RecoveryEventStreamResult =
  | {
      kind: "eof";
      lastSeq: number;
      lastEvent: RecoveryEvent | null;
    }
  | {
      kind: "terminal";
      lastSeq: number;
      lastEvent: RecoveryEvent;
    };

export class RecoveryEventStreamContractError extends Error {
  constructor() {
    super("The server returned an unexpected response.");
    this.name = "RecoveryEventStreamContractError";
  }
}

const MAX_LINE_BYTES = 16_384;
const MAX_FRAME_BYTES = 65_536;
const MAX_BUFFER_BYTES = 131_072;
const CARRIAGE_RETURN = 0x0d;
const LINE_FEED = 0x0a;
const CR_BYTES = new Uint8Array([CARRIAGE_RETURN]);
const LF_BYTES = new Uint8Array([LINE_FEED]);
const CRLF_BYTES = new Uint8Array([CARRIAGE_RETURN, LINE_FEED]);
const EMPTY_BYTES = new Uint8Array();

function contractFailure(): RecoveryEventStreamContractError {
  return new RecoveryEventStreamContractError();
}

function abortFailure(): DOMException {
  return new DOMException("The operation was aborted.", "AbortError");
}

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) {
    throw abortFailure();
  }
}

function withAbort<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) {
    return Promise.reject(abortFailure());
  }
  return new Promise<T>((resolve, reject) => {
    const rejectForAbort = () => reject(abortFailure());
    signal.addEventListener("abort", rejectForAbort, { once: true });
    void promise.then(resolve, reject).finally(() => {
      signal.removeEventListener("abort", rejectForAbort);
    });
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: ReadonlyArray<string>,
): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return (
    actual.length === expected.length &&
    actual.every((key, index) => key === expected[index])
  );
}

function isRecoveryEvent(value: unknown): value is RecoveryEvent {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "recoveryId",
      "seq",
      "type",
      "terminal",
      "data",
      "createdAt",
    ])
  ) {
    return false;
  }
  return (
    typeof value.recoveryId === "string" &&
    Number.isSafeInteger(value.seq) &&
    typeof value.seq === "number" &&
    value.seq > 0 &&
    typeof value.type === "string" &&
    value.type.length > 0 &&
    typeof value.terminal === "boolean" &&
    isRecord(value.data) &&
    typeof value.createdAt === "string"
  );
}

export function parseRecoveryEvent(serialized: string): RecoveryEvent {
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized) as unknown;
  } catch {
    throw contractFailure();
  }
  if (!isRecoveryEvent(parsed)) {
    throw contractFailure();
  }
  return parsed;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map(
        (key) =>
          `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
      )
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function sameEvent(left: RecoveryEvent, right: RecoveryEvent): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

function validContentType(value: string | null): boolean {
  if (value === null) {
    return false;
  }
  const parts = value
    .split(";")
    .map((part) => part.trim().toLowerCase());
  return (
    parts[0] === "text/event-stream" &&
    (parts.length === 1 ||
      (parts.length === 2 && parts[1] === "charset=utf-8"))
  );
}

interface ParsedFrame {
  id: string | null;
  data: string;
}

class SseFrameParser {
  private readonly decoder = new TextDecoder("utf-8", { fatal: true });
  private readonly lineChunks: Uint8Array[] = [];
  private lineBytes = 0;
  private pendingCarriageReturn = false;
  private frameBytes = 0;
  private frameId: string | null = null;
  private readonly dataLines: string[] = [];

  constructor(private readonly onFrame: (frame: ParsedFrame) => boolean) {}

  push(chunk: Uint8Array, final = false): boolean {
    let index = 0;
    while (index < chunk.byteLength) {
      if (this.pendingCarriageReturn) {
        this.pendingCarriageReturn = false;
        if (chunk[index] === LINE_FEED) {
          index += 1;
          if (!this.completeLine(CRLF_BYTES)) {
            return false;
          }
          continue;
        }
        if (!this.completeLine(CR_BYTES)) {
          return false;
        }
      }

      let delimiterIndex = index;
      while (
        delimiterIndex < chunk.byteLength &&
        chunk[delimiterIndex] !== CARRIAGE_RETURN &&
        chunk[delimiterIndex] !== LINE_FEED
      ) {
        delimiterIndex += 1;
      }
      this.appendLineBytes(chunk.subarray(index, delimiterIndex));
      if (delimiterIndex === chunk.byteLength) {
        index = delimiterIndex;
        continue;
      }

      const delimiter = chunk[delimiterIndex];
      index = delimiterIndex + 1;
      if (delimiter === LINE_FEED) {
        if (!this.completeLine(LF_BYTES)) {
          return false;
        }
        continue;
      }
      if (index < chunk.byteLength) {
        if (chunk[index] === LINE_FEED) {
          index += 1;
          if (!this.completeLine(CRLF_BYTES)) {
            return false;
          }
        } else if (!this.completeLine(CR_BYTES)) {
          return false;
        }
        continue;
      }
      this.pendingCarriageReturn = true;
      if (this.lineBytes + 1 > MAX_BUFFER_BYTES) {
        throw contractFailure();
      }
    }

    if (final) {
      if (this.pendingCarriageReturn) {
        this.pendingCarriageReturn = false;
        if (!this.completeLine(CR_BYTES)) {
          return false;
        }
      }
      if (this.lineBytes > 0) {
        if (!this.completeLine(EMPTY_BYTES, true)) {
          return false;
        }
      } else {
        try {
          this.decoder.decode();
        } catch {
          throw contractFailure();
        }
      }
    }
    return true;
  }

  private appendLineBytes(bytes: Uint8Array): void {
    if (bytes.byteLength === 0) {
      return;
    }
    this.lineBytes += bytes.byteLength;
    if (
      this.lineBytes > MAX_LINE_BYTES ||
      this.lineBytes > MAX_BUFFER_BYTES ||
      this.frameBytes + this.lineBytes > MAX_FRAME_BYTES
    ) {
      throw contractFailure();
    }
    this.lineChunks.push(bytes);
  }

  private takeLineBytes(): Uint8Array {
    if (this.lineChunks.length === 0) {
      return EMPTY_BYTES;
    }
    if (this.lineChunks.length === 1) {
      return this.lineChunks[0] as Uint8Array;
    }
    const line = new Uint8Array(this.lineBytes);
    let offset = 0;
    for (const chunk of this.lineChunks) {
      line.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return line;
  }

  private completeLine(
    delimiter: Uint8Array,
    final = false,
  ): boolean {
    const rawLineBytes = this.lineBytes;
    if (
      this.frameBytes + rawLineBytes + delimiter.byteLength >
      MAX_FRAME_BYTES
    ) {
      throw contractFailure();
    }
    let line: string;
    try {
      line = this.decoder.decode(this.takeLineBytes(), {
        stream: !final,
      });
      if (delimiter.byteLength > 0) {
        this.decoder.decode(delimiter, { stream: true });
      }
    } catch {
      throw contractFailure();
    }
    this.lineChunks.length = 0;
    this.lineBytes = 0;
    if (final && delimiter.byteLength === 0 && line.length === 0) {
      return true;
    }
    if (!this.acceptLine(line, rawLineBytes, delimiter.byteLength)) {
      return false;
    }
    return true;
  }

  private acceptLine(
    line: string,
    lineBytes: number,
    delimiterBytes: number,
  ): boolean {
    if (lineBytes > MAX_LINE_BYTES) {
      throw contractFailure();
    }
    this.frameBytes += lineBytes + delimiterBytes;
    if (this.frameBytes > MAX_FRAME_BYTES) {
      throw contractFailure();
    }
    if (line === "") {
      const shouldContinue =
        this.dataLines.length === 0
          ? true
          : this.onFrame({
              id: this.frameId,
              data: this.dataLines.join("\n"),
            });
      this.frameBytes = 0;
      this.frameId = null;
      this.dataLines.length = 0;
      if (!shouldContinue) {
        return false;
      }
      return true;
    }
    if (line.startsWith(":")) {
      return true;
    }
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (field === "id") {
      if (value.includes("\u0000")) {
        throw contractFailure();
      }
      this.frameId = value;
    } else if (field === "data") {
      this.dataLines.push(value);
    }
    return true;
  }
}

function validateCursor(
  recoveryId: string,
  afterSeq: number,
  lastEvent: RecoveryEvent | null,
): void {
  if (
    !Number.isSafeInteger(afterSeq) ||
    afterSeq < 0 ||
    (afterSeq === 0 && lastEvent !== null) ||
    (afterSeq > 0 &&
      (lastEvent === null ||
        lastEvent.seq !== afterSeq ||
        lastEvent.recoveryId !== recoveryId))
  ) {
    throw contractFailure();
  }
}

async function cancelUnlockedBody(response: Response): Promise<void> {
  const body = response.body;
  if (body === null || body.locked) {
    return;
  }
  await body.cancel().catch(() => undefined);
}

export async function openRecoveryEventStream(
  request: RecoveryEventStreamRequest,
  handlers: RecoveryEventStreamHandlers,
): Promise<RecoveryEventStreamResult> {
  const {
    recoveryId,
    afterSeq,
    lastEvent: initialLastEvent,
    signal,
    fetchImpl = fetch,
  } = request;
  validateCursor(recoveryId, afterSeq, initialLastEvent);
  throwIfAborted(signal);

  const requestController = new AbortController();
  const abortOwnedRequest = () => requestController.abort();
  signal.addEventListener("abort", abortOwnedRequest, { once: true });
  if (signal.aborted) {
    requestController.abort();
  }

  try {
    const headers = new Headers({ Accept: "text/event-stream" });
    if (afterSeq > 0) {
      headers.set("Last-Event-ID", String(afterSeq));
    }
    const response = await withAbort(
      fetchImpl(
        `/api/recoveries/${encodeURIComponent(recoveryId)}/events`,
        {
          headers,
          credentials: "same-origin",
          signal: requestController.signal,
        },
      ),
      signal,
    );

    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      throwIfAborted(signal);
      if (response.status !== 200) {
        if (!response.ok) {
          throw await readRecoveryEventStreamFailure(
            response,
            recoveryId,
          );
        }
        throw contractFailure();
      }
      if (!validContentType(response.headers.get("Content-Type"))) {
        throw contractFailure();
      }
      try {
        reader = response.body?.getReader();
      } catch {
        throw contractFailure();
      }
      if (reader === undefined) {
        throw contractFailure();
      }
    } catch (error) {
      await cancelUnlockedBody(response);
      requestController.abort();
      throw error;
    }

    let cursor = afterSeq;
    let lastEvent = initialLastEvent;
    let terminalEvent: RecoveryEvent | null = null;
    const parser = new SseFrameParser((frame) => {
      throwIfAborted(signal);
      if (
        frame.id === null ||
        !/^[1-9][0-9]*$/.test(frame.id)
      ) {
        throw contractFailure();
      }
      const frameSequence = Number(frame.id);
      if (!Number.isSafeInteger(frameSequence)) {
        throw contractFailure();
      }
      const event = parseRecoveryEvent(frame.data);
      if (
        event.recoveryId !== recoveryId ||
        event.seq !== frameSequence
      ) {
        throw contractFailure();
      }
      if (event.seq === cursor) {
        if (lastEvent === null || !sameEvent(event, lastEvent)) {
          throw contractFailure();
        }
        return true;
      }
      if (event.seq !== cursor + 1) {
        throw contractFailure();
      }
      handlers.onEvent(event);
      throwIfAborted(signal);
      cursor = event.seq;
      lastEvent = event;
      if (event.terminal) {
        terminalEvent = event;
        return false;
      }
      return true;
    });
    const cancelForAbort = () => {
      void reader.cancel().catch(() => undefined);
    };
    signal.addEventListener("abort", cancelForAbort, { once: true });

    try {
      throwIfAborted(signal);
      handlers.onOpen();
      throwIfAborted(signal);
      while (terminalEvent === null) {
        const { done, value } = await withAbort(reader.read(), signal);
        throwIfAborted(signal);
        if (done) {
          try {
            parser.push(EMPTY_BYTES, true);
          } catch (error) {
            if (error instanceof RecoveryEventStreamContractError) {
              throw error;
            }
            throw contractFailure();
          }
          break;
        }
        try {
          if (!parser.push(value)) {
            break;
          }
        } catch (error) {
          if (error instanceof RecoveryEventStreamContractError) {
            throw error;
          }
          throw contractFailure();
        }
      }
      if (terminalEvent !== null) {
        return {
          kind: "terminal",
          lastSeq: cursor,
          lastEvent: terminalEvent,
        };
      }
      return { kind: "eof", lastSeq: cursor, lastEvent };
    } finally {
      signal.removeEventListener("abort", cancelForAbort);
      await reader.cancel().catch(() => undefined);
    }
  } finally {
    signal.removeEventListener("abort", abortOwnedRequest);
    requestController.abort();
  }
}
