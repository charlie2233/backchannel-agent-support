import { useEffect, useReducer } from "react";

import { getRecovery } from "../api/client";
import { connectRecoveryEvents, type RecoveryEvent } from "../api/events";
import type { RecoverySnapshot } from "../domain/recovery";

export interface RecoveryState {
  snapshot: RecoverySnapshot | null;
  events: ReadonlyArray<RecoveryEvent>;
  lastSeq: number;
  loading: boolean;
  error: string | null;
}

export interface RecoveryEventState {
  events: ReadonlyArray<RecoveryEvent>;
  error: string | null;
}

export const initialRecoveryState: RecoveryState = {
  snapshot: null,
  events: [],
  lastSeq: 0,
  loading: false,
  error: null,
};

type RecoveryAction =
  | { type: "loading" }
  | { type: "snapshotLoaded"; snapshot: RecoverySnapshot }
  | { type: "eventReceived"; event: RecoveryEvent }
  | { type: "failed"; message: string }
  | { type: "reset" };

export function recoveryReducer(
  state: RecoveryState,
  action: RecoveryAction,
): RecoveryState {
  switch (action.type) {
    case "loading":
      return { ...state, loading: true, error: null };
    case "snapshotLoaded":
      return { ...state, snapshot: action.snapshot, loading: false, error: null };
    case "eventReceived": {
      if (state.events.some(({ seq }) => seq === action.event.seq)) {
        return state;
      }
      const events = [...state.events, action.event].sort((left, right) => left.seq - right.seq);
      return {
        ...state,
        events,
        lastSeq: Math.max(state.lastSeq, action.event.seq),
        error: null,
      };
    }
    case "failed":
      return { ...state, loading: false, error: action.message };
    case "reset":
      return initialRecoveryState;
  }
}

export function useRecovery(recoveryId: string | null): RecoveryState {
  const [state, dispatch] = useReducer(recoveryReducer, initialRecoveryState);

  useEffect(() => {
    if (recoveryId === null) {
      dispatch({ type: "reset" });
      return;
    }

    const controller = new AbortController();
    dispatch({ type: "reset" });
    dispatch({ type: "loading" });

    void getRecovery(recoveryId, controller.signal)
      .then((snapshot) => dispatch({ type: "snapshotLoaded", snapshot }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        dispatch({
          type: "failed",
          message: error instanceof Error ? error.message : "Recovery could not be loaded",
        });
      });

    const disconnect = connectRecoveryEvents(recoveryId, {
      onEvent: (event) => dispatch({ type: "eventReceived", event }),
      onError: (error) => dispatch({ type: "failed", message: error.message }),
    });

    return () => {
      controller.abort();
      disconnect();
    };
  }, [recoveryId]);

  return state;
}

export function useRecoveryEvents(recoveryId: string | null): RecoveryEventState {
  const [state, dispatch] = useReducer(recoveryReducer, initialRecoveryState);

  useEffect(() => {
    dispatch({ type: "reset" });
    if (recoveryId === null) {
      return;
    }

    return connectRecoveryEvents(recoveryId, {
      onEvent: (event) => dispatch({ type: "eventReceived", event }),
      onError: (error) => dispatch({ type: "failed", message: error.message }),
    });
  }, [recoveryId]);

  return { events: state.events, error: state.error };
}
