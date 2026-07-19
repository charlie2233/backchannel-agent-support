import "@testing-library/jest-dom/vitest";

if (typeof globalThis.EventSource === "undefined") {
  class QuietEventSource {
    onmessage: ((event: MessageEvent<string>) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    close() {}
  }

  Object.defineProperty(globalThis, "EventSource", {
    configurable: true,
    value: QuietEventSource,
  });
}
