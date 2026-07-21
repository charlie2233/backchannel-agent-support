import { useCallback, useEffect, useRef, useState } from "react";

import { getHealth } from "../api/client";
import type { HealthStatus } from "../domain/runtime";

export type RuntimeHealthPhase = "checking" | "retrying" | "ready" | "unavailable";

interface RuntimeHealthState {
  health: HealthStatus | null;
  phase: RuntimeHealthPhase;
}

export interface RuntimeHealthResult extends RuntimeHealthState {
  retry: () => void;
}

const ATTEMPT_TIMEOUT_MS = 3_000;
const RETRY_BACKOFF_MS = [500, 1_000] as const;

export function useRuntimeHealth(): RuntimeHealthResult {
  const [state, setState] = useState<RuntimeHealthState>({
    health: null,
    phase: "checking",
  });
  const phaseRef = useRef<RuntimeHealthPhase>("checking");
  const generationRef = useRef(0);
  const inFlightRef = useRef(false);
  const requestControllerRef = useRef<AbortController | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setOwnedState = useCallback(
    (generation: number, next: RuntimeHealthState): boolean => {
      if (generationRef.current !== generation) return false;
      phaseRef.current = next.phase;
      setState(next);
      return true;
    },
    [],
  );

  const startGeneration = useCallback((initialPhase: "checking" | "retrying") => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    const generation = ++generationRef.current;
    setOwnedState(generation, { health: null, phase: initialPhase });

    const runAttempt = (attempt: number) => {
      if (generationRef.current !== generation) return;
      const controller = new AbortController();
      requestControllerRef.current = controller;

      let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
      const timeout = new Promise<never>((_resolve, reject) => {
        timeoutHandle = setTimeout(() => {
          controller.abort();
          reject(new DOMException("Runtime health request timed out", "AbortError"));
        }, ATTEMPT_TIMEOUT_MS);
        timeoutRef.current = timeoutHandle;
      });

      void Promise.race([getHealth(controller.signal), timeout])
        .then((health) => {
          if (timeoutHandle !== null) clearTimeout(timeoutHandle);
          if (timeoutRef.current === timeoutHandle) timeoutRef.current = null;
          if (generationRef.current !== generation) return;
          requestControllerRef.current = null;
          inFlightRef.current = false;
          setOwnedState(generation, { health, phase: "ready" });
        })
        .catch(() => {
          if (timeoutHandle !== null) clearTimeout(timeoutHandle);
          if (timeoutRef.current === timeoutHandle) timeoutRef.current = null;
          if (generationRef.current !== generation) return;
          requestControllerRef.current = null;

          if (attempt >= 3) {
            inFlightRef.current = false;
            setOwnedState(generation, { health: null, phase: "unavailable" });
            return;
          }

          setOwnedState(generation, { health: null, phase: "retrying" });
          const backoffHandle = setTimeout(() => {
            if (backoffRef.current === backoffHandle) backoffRef.current = null;
            runAttempt(attempt + 1);
          }, RETRY_BACKOFF_MS[attempt - 1]);
          backoffRef.current = backoffHandle;
        });
    };

    runAttempt(1);
  }, [setOwnedState]);

  const retry = useCallback(() => {
    if (phaseRef.current !== "unavailable" || inFlightRef.current) return;
    startGeneration("retrying");
  }, [startGeneration]);

  useEffect(() => {
    startGeneration("checking");
    return () => {
      generationRef.current += 1;
      inFlightRef.current = false;
      requestControllerRef.current?.abort();
      requestControllerRef.current = null;
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
      if (backoffRef.current !== null) clearTimeout(backoffRef.current);
      backoffRef.current = null;
    };
  }, [startGeneration]);

  return { ...state, retry };
}
