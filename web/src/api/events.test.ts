import { afterEach, describe, expect, it, vi } from "vitest";

import { PublicApiError } from "./client";
import {
  openRecoveryEventStream,
  RecoveryEventStreamContractError,
  type RecoveryEvent,
} from "./events";

const recoveryId = "11111111-2222-4333-8444-555555555555";

function recoveryEvent(
  seq: number,
  {
    terminal = false,
    summary = `event ${seq}`,
  }: { terminal?: boolean; summary?: string } = {},
): RecoveryEvent {
  return {
    recoveryId,
    seq,
    type: terminal ? "recovery.completed" : "recovery.progress",
    terminal,
    data: { summary },
    createdAt: "2026-07-24T12:00:00Z",
  };
}

function frame(event: RecoveryEvent, lineEnding = "\n"): string {
  return [
    `id: ${event.seq}`,
    `data: ${JSON.stringify(event)}`,
    "",
    "",
  ].join(lineEnding);
}

function byteStreamResponse(
  chunks: ReadonlyArray<Uint8Array>,
  {
    contentType = "text/event-stream; charset=utf-8",
    close = true,
    onCancel,
    status = 200,
  }: {
    contentType?: string;
    close?: boolean;
    onCancel?: () => void;
    status?: number;
  } = {},
): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(chunk);
        }
        if (close) {
          controller.close();
        }
      },
      cancel() {
        onCancel?.();
      },
    }),
    {
      status,
      headers: { "Content-Type": contentType },
    },
  );
}

function streamResponse(
  chunks: ReadonlyArray<string>,
  options: Parameters<typeof byteStreamResponse>[1] = {},
): Response {
  const encoder = new TextEncoder();
  return byteStreamResponse(
    chunks.map((chunk) => encoder.encode(chunk)),
    options,
  );
}

function capacityResponse(): Response {
  return new Response(
    JSON.stringify({
      error: {
        code: "stream_capacity_reached",
        message: "The event stream is currently at capacity.",
        requestId: "req_11111111111111111111111111111111",
        recoveryId,
        retryAfterSeconds: 1,
        fallback: null,
      },
    }),
    {
      status: 429,
      headers: {
        "Content-Type": "application/json",
        "Retry-After": "1",
      },
    },
  );
}

function open(
  response: Response | Promise<Response>,
  {
    afterSeq = 0,
    lastEvent = null,
    signal = new AbortController().signal,
    onOpen = vi.fn(),
    onEvent = vi.fn(),
  }: {
    afterSeq?: number;
    lastEvent?: RecoveryEvent | null;
    signal?: AbortSignal;
    onOpen?: () => void;
    onEvent?: (event: RecoveryEvent) => void;
  } = {},
) {
  const fetchImpl = vi.fn().mockResolvedValue(response);
  return {
    fetchImpl,
    result: openRecoveryEventStream(
      { recoveryId, afterSeq, lastEvent, signal, fetchImpl },
      { onOpen, onEvent },
    ),
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("one-attempt fetch recovery event transport", () => {
  it("requests same-origin SSE and parses split CRLF, comments, multiline data, and UTF-8 bytes", async () => {
    const terminal = recoveryEvent(1, {
      terminal: true,
      summary: "Récovery ✓",
    });
    const serialized = JSON.stringify(terminal).replace(
      `,"seq":${terminal.seq}`,
      `,\r\ndata: "seq":${terminal.seq}`,
    );
    const encoded = new TextEncoder().encode(
      `: heartbeat\r\nid: 1\r\ndata: ${serialized}\r\n\r\n`,
    );
    const checkmarkStart = encoded.findIndex((byte) => byte === 0xe2);
    const chunks = [
      encoded.slice(0, 12),
      encoded.slice(12, checkmarkStart + 1),
      encoded.slice(checkmarkStart + 1, checkmarkStart + 2),
      encoded.slice(checkmarkStart + 2, encoded.length - 3),
      encoded.slice(encoded.length - 3, encoded.length - 2),
      encoded.slice(encoded.length - 2),
    ];
    const onOpen = vi.fn();
    const onEvent = vi.fn();
    const { fetchImpl, result } = open(byteStreamResponse(chunks), {
      onOpen,
      onEvent,
    });

    await expect(result).resolves.toMatchObject({
      kind: "terminal",
      lastSeq: 1,
      lastEvent: terminal,
    });
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/recoveries/${recoveryId}/events`);
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("Accept")).toBe("text/event-stream");
    expect(new Headers(init.headers).has("Last-Event-ID")).toBe(false);
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onEvent).toHaveBeenCalledOnce();
    expect(onEvent).toHaveBeenCalledWith(terminal);
  });

  it.each([
    [
      "non-200 status",
      () =>
        new Response(null, {
          status: 204,
          headers: { "Content-Type": "text/event-stream" },
        }),
    ],
    [
      "wrong MIME",
      () => streamResponse([], { contentType: "application/json" }),
    ],
    [
      "missing body",
      () =>
        new Response(null, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
    ],
  ])("rejects a %s as a redacted contract failure", async (_label, response) => {
    const { result } = open(response());

    await expect(result).rejects.toMatchObject({
      name: "RecoveryEventStreamContractError",
      message: "The server returned an unexpected response.",
    });
  });

  it("returns clean nonterminal EOF as a reconnectable disconnect", async () => {
    const first = recoveryEvent(1);
    const { result } = open(streamResponse([frame(first)]));

    await expect(result).resolves.toEqual({
      kind: "eof",
      lastSeq: 1,
      lastEvent: first,
    });
  });

  it("sends the retained cursor and ignores only an exact replay duplicate", async () => {
    const first = recoveryEvent(1);
    const terminal = recoveryEvent(2, { terminal: true });
    const onEvent = vi.fn();
    const { fetchImpl, result } = open(
      streamResponse([frame(first), frame(terminal)]),
      { afterSeq: 1, lastEvent: first, onEvent },
    );

    await expect(result).resolves.toMatchObject({
      kind: "terminal",
      lastSeq: 2,
      lastEvent: terminal,
    });
    expect(
      new Headers((fetchImpl.mock.calls[0]?.[1] as RequestInit).headers).get(
        "Last-Event-ID",
      ),
    ).toBe("1");
    expect(onEvent.mock.calls.map(([event]) => event)).toEqual([terminal]);
  });

  it.each([
    [
      "conflicting duplicate",
      recoveryEvent(1),
      [frame(recoveryEvent(1, { summary: "substituted" }))],
    ],
    ["regression", recoveryEvent(2), [frame(recoveryEvent(1))]],
    ["initial gap", null, [frame(recoveryEvent(2))]],
    [
      "later gap",
      null,
      [frame(recoveryEvent(1)), frame(recoveryEvent(3))],
    ],
    [
      "frame/payload sequence mismatch",
      null,
      [
        [
          "id: 2",
          `data: ${JSON.stringify(recoveryEvent(1))}`,
          "",
          "",
        ].join("\n"),
      ],
    ],
    [
      "recovery substitution",
      null,
      [
        frame({
          ...recoveryEvent(1),
          recoveryId: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        }),
      ],
    ],
  ])("rejects %s before dispatch", async (label, lastEvent, chunks) => {
    const onEvent = vi.fn();
    const afterSeq = lastEvent?.seq ?? 0;
    const { result } = open(streamResponse(chunks), {
      afterSeq,
      lastEvent,
      onEvent,
    });

    await expect(result).rejects.toBeInstanceOf(
      RecoveryEventStreamContractError,
    );
    expect(onEvent).toHaveBeenCalledTimes(label === "later gap" ? 1 : 0);
  });

  it.each([
    ["line", `id: ${"1".repeat(16_385)}\n\n`],
    [
      "frame",
      `${Array.from({ length: 65 }, () => `x: ${"a".repeat(1_020)}`).join("\n")}\n\n`,
    ],
    ["buffer", `data: ${"a".repeat(131_073)}`],
  ])("bounds oversized %s bytes", async (_label, payload) => {
    const { result } = open(streamResponse([payload]));

    await expect(result).rejects.toBeInstanceOf(
      RecoveryEventStreamContractError,
    );
  });

  it("rejects an unterminated overlong line without consuming or rescanning the remaining tiny chunks", async () => {
    const encoder = new TextEncoder();
    const byte = encoder.encode("a");
    let pulls = 0;
    const response = new Response(
      new ReadableStream<Uint8Array>({
        pull(controller) {
          pulls += 1;
          if (pulls <= 20_000) {
            controller.enqueue(byte);
            return;
          }
          controller.close();
        },
      }),
      {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      },
    );
    const { result } = open(response);

    await expect(result).rejects.toBeInstanceOf(
      RecoveryEventStreamContractError,
    );
    expect(pulls).toBeLessThanOrEqual(16_386);
  });

  it("rejects malformed UTF-8 without leaking decoder details", async () => {
    const { result } = open(
      byteStreamResponse([
        new TextEncoder().encode("id: 1\ndata: "),
        new Uint8Array([0xc3, 0x28]),
        new TextEncoder().encode("\n\n"),
      ]),
    );

    await expect(result).rejects.toMatchObject({
      name: "RecoveryEventStreamContractError",
      message: "The server returned an unexpected response.",
    });
  });

  it("returns the endpoint-only typed capacity failure without retrying", async () => {
    const { fetchImpl, result } = open(capacityResponse());

    await expect(result).rejects.toMatchObject({
      code: "stream_capacity_reached",
      status: 429,
      recoveryId,
      retryAfterSeconds: 1,
    });
    await expect(result.catch((error: unknown) => error)).resolves.toBeInstanceOf(
      PublicApiError,
    );
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it.each([
    {
      label: "wrong success MIME",
      response: (onCancel: () => void) =>
        streamResponse(["not SSE"], {
          close: false,
          contentType: "application/json",
          onCancel,
        }),
    },
    {
      label: "non-JSON HTTP failure",
      response: (onCancel: () => void) =>
        streamResponse(["private upstream response"], {
          close: false,
          contentType: "text/plain",
          status: 503,
          onCancel,
        }),
    },
  ])("cancels the response body on a pre-reader $label", async ({ response }) => {
    const onCancel = vi.fn();
    const rawResponse = response(onCancel);
    const body = rawResponse.body;
    expect(body).not.toBeNull();
    const cancelSpy = vi.spyOn(body as ReadableStream<Uint8Array>, "cancel");
    const { result } = open(rawResponse);

    await expect(result).rejects.toMatchObject({
      message: expect.not.stringContaining("upstream"),
    });
    expect(cancelSpy).toHaveBeenCalledOnce();
    await expect(cancelSpy.mock.results[0]?.value).resolves.toBeUndefined();
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("aborts the owned request when response reader acquisition fails", async () => {
    const onCancel = vi.fn();
    const response = streamResponse([], { close: false, onCancel });
    const lockedReader = response.body?.getReader();
    expect(lockedReader).toBeDefined();
    const { fetchImpl, result } = open(response);

    await expect(result).rejects.toBeInstanceOf(
      RecoveryEventStreamContractError,
    );
    const requestSignal = (
      fetchImpl.mock.calls[0]?.[1] as RequestInit
    ).signal as AbortSignal;
    expect(requestSignal.aborted).toBe(true);

    await lockedReader?.cancel();
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("cancels the reader on terminal and treats abort as normal cleanup", async () => {
    const onCancel = vi.fn();
    const terminal = recoveryEvent(1, { terminal: true });
    const terminalAttempt = open(
      streamResponse([frame(terminal)], { close: false, onCancel }),
    );
    await expect(terminalAttempt.result).resolves.toMatchObject({
      kind: "terminal",
    });
    expect(onCancel).toHaveBeenCalledOnce();

    const controller = new AbortController();
    const abortedAttempt = open(
      new Promise<Response>(() => undefined),
      { signal: controller.signal },
    );
    controller.abort();
    await expect(abortedAttempt.result).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("suppresses late open/event callbacks after abort", async () => {
    let resolveFetch: ((response: Response) => void) | undefined;
    const response = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    const controller = new AbortController();
    const onOpen = vi.fn();
    const onEvent = vi.fn();
    const attempt = open(response, {
      signal: controller.signal,
      onOpen,
      onEvent,
    });

    controller.abort();
    resolveFetch?.(
      streamResponse([frame(recoveryEvent(1, { terminal: true }))]),
    );

    await expect(attempt.result).rejects.toMatchObject({ name: "AbortError" });
    expect(onOpen).not.toHaveBeenCalled();
    expect(onEvent).not.toHaveBeenCalled();
  });
});
