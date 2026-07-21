import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HealthStatus } from "../domain/runtime";
import { useRuntimeHealth } from "./useRuntimeHealth";

const { getHealthMock } = vi.hoisted(() => ({
  getHealthMock: vi.fn<(signal?: AbortSignal) => Promise<HealthStatus>>(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  return { ...original, getHealth: getHealthMock };
});

const STUB_HEALTH: HealthStatus = {
  backend: "stub",
  liveReady: false,
  providerBoundary: "demo_adapter_only",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushMicrotasks() {
  await act(async () => {
    await Promise.resolve();
  });
}

afterEach(() => {
  cleanup();
  getHealthMock.mockReset();
  vi.useRealTimers();
});

describe("useRuntimeHealth", () => {
  it("uses exactly three attempts with 500ms and 1000ms capped backoffs", async () => {
    vi.useFakeTimers();
    getHealthMock
      .mockRejectedValueOnce(new Error("first"))
      .mockRejectedValueOnce(new Error("second"))
      .mockResolvedValueOnce(STUB_HEALTH);

    const { result } = renderHook(() => useRuntimeHealth());
    expect(result.current.phase).toBe("checking");
    act(() => {
      result.current.retry();
      result.current.retry();
    });
    expect(getHealthMock).toHaveBeenCalledTimes(1);

    await flushMicrotasks();
    expect(getHealthMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("retrying");
    act(() => {
      result.current.retry();
      result.current.retry();
    });
    expect(getHealthMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(499));
    expect(getHealthMock).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(getHealthMock).toHaveBeenCalledTimes(2);

    await act(async () => vi.advanceTimersByTimeAsync(999));
    expect(getHealthMock).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(getHealthMock).toHaveBeenCalledTimes(3);
    expect(result.current).toMatchObject({ phase: "ready", health: STUB_HEALTH });
    expect(getHealthMock.mock.calls.every(([signal]) => signal instanceof AbortSignal)).toBe(true);
  });

  it("aborts every 3s timed-out attempt and becomes unavailable after the third", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    getHealthMock.mockImplementation((signal) => {
      if (signal !== undefined) signals.push(signal);
      return new Promise<HealthStatus>(() => {});
    });

    const { result } = renderHook(() => useRuntimeHealth());
    expect(getHealthMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(signals[0]?.aborted).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(getHealthMock).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(signals[1]?.aborted).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(getHealthMock).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(3_000));

    expect(signals[2]?.aborted).toBe(true);
    expect(result.current).toMatchObject({ phase: "unavailable", health: null });
    expect(getHealthMock).toHaveBeenCalledTimes(3);
  });

  it("coalesces rapid manual retries and permits them only after exhaustion", async () => {
    vi.useFakeTimers();
    const manual = deferred<HealthStatus>();
    getHealthMock
      .mockRejectedValueOnce(new Error("one"))
      .mockRejectedValueOnce(new Error("two"))
      .mockRejectedValueOnce(new Error("three"))
      .mockReturnValueOnce(manual.promise);

    const { result } = renderHook(() => useRuntimeHealth());
    await act(async () => vi.advanceTimersByTimeAsync(1_500));
    expect(result.current.phase).toBe("unavailable");
    expect(getHealthMock).toHaveBeenCalledTimes(3);

    act(() => {
      result.current.retry();
      result.current.retry();
    });
    expect(getHealthMock).toHaveBeenCalledTimes(4);
    expect(result.current.phase).toBe("retrying");

    await act(async () => manual.resolve(STUB_HEALTH));
    expect(result.current).toMatchObject({ phase: "ready", health: STUB_HEALTH });
  });

  it("ignores a late settlement from an attempt that already timed out", async () => {
    vi.useFakeTimers();
    const first = deferred<HealthStatus>();
    const second = deferred<HealthStatus>();
    getHealthMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const { result } = renderHook(() => useRuntimeHealth());
    await act(async () => vi.advanceTimersByTimeAsync(3_500));
    expect(getHealthMock).toHaveBeenCalledTimes(2);

    await act(async () => second.resolve(STUB_HEALTH));
    expect(result.current).toMatchObject({ phase: "ready", health: STUB_HEALTH });

    await act(async () =>
      first.resolve({
        backend: "openai",
        liveReady: true,
        providerBoundary: "demo_adapter_only",
      }),
    );
    expect(result.current).toMatchObject({ phase: "ready", health: STUB_HEALTH });
  });

  it("cancels a queued backoff on cleanup", async () => {
    vi.useFakeTimers();
    getHealthMock.mockRejectedValue(new Error("offline"));
    const { unmount } = renderHook(() => useRuntimeHealth());
    await flushMicrotasks();
    expect(getHealthMock).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(getHealthMock).toHaveBeenCalledTimes(1);
  });

  it("aborts an active request on cleanup and starts an owned StrictMode generation", async () => {
    const first = deferred<HealthStatus>();
    const second = deferred<HealthStatus>();
    const signals: AbortSignal[] = [];
    getHealthMock
      .mockImplementationOnce((signal) => {
        if (signal !== undefined) signals.push(signal);
        return first.promise;
      })
      .mockImplementationOnce((signal) => {
        if (signal !== undefined) signals.push(signal);
        return second.promise;
      });

    const { result, unmount } = renderHook(() => useRuntimeHealth(), {
      reactStrictMode: true,
    });
    expect(getHealthMock).toHaveBeenCalledTimes(2);
    expect(signals[0]?.aborted).toBe(true);

    await act(async () => second.resolve(STUB_HEALTH));
    expect(result.current).toMatchObject({ phase: "ready", health: STUB_HEALTH });

    await act(async () => first.reject(new Error("stale")));
    expect(result.current).toMatchObject({ phase: "ready", health: STUB_HEALTH });

    unmount();
  });

  it("aborts an unresolved active request on unmount", () => {
    const signals: AbortSignal[] = [];
    getHealthMock.mockImplementation((signal) => {
      if (signal !== undefined) signals.push(signal);
      return new Promise<HealthStatus>(() => {});
    });

    const { unmount } = renderHook(() => useRuntimeHealth());
    unmount();

    expect(signals).toHaveLength(1);
    expect(signals[0]?.aborted).toBe(true);
  });
});
