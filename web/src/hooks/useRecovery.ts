import { useEffect, useReducer } from "react";

import {
  getReceipt as getReceiptFromServer,
  getRecovery as getRecoveryFromServer,
  HttpStatusError,
} from "../api/client";
import {
  connectRecoveryEvents,
  type RecoveryEvent,
  type RecoveryEventHandlers,
} from "../api/events";
import {
  isTerminalRecoveryStatus,
  type RecoveryReceipt,
  type RecoverySnapshot,
} from "../domain/recovery";

export interface RecoveryState {
  snapshot: RecoverySnapshot | null;
  receipt: RecoveryReceipt | null;
  events: ReadonlyArray<RecoveryEvent>;
  lastSeq: number;
  loading: boolean;
  error: string | null;
  errorStatus: number | null;
  errorPhase: RecoveryErrorPhase | null;
}

export type RecoveryErrorPhase = "initial" | "terminal" | "events";

export const initialRecoveryState: RecoveryState = {
  snapshot: null,
  receipt: null,
  events: [],
  lastSeq: 0,
  loading: false,
  error: null,
  errorStatus: null,
  errorPhase: null,
};

type RecoveryAction =
  | { type: "loading" }
  | { type: "snapshotLoaded"; snapshot: RecoverySnapshot }
  | {
      type: "terminalLoaded";
      snapshot: RecoverySnapshot;
      receipt: RecoveryReceipt;
    }
  | { type: "eventReceived"; event: RecoveryEvent }
  | {
      type: "failed";
      message: string;
      status: number | null;
      phase: RecoveryErrorPhase;
    }
  | { type: "reset" };

export function recoveryReducer(
  state: RecoveryState,
  action: RecoveryAction,
): RecoveryState {
  switch (action.type) {
    case "loading":
      return {
        ...state,
        loading: true,
        error: null,
        errorStatus: null,
        errorPhase: null,
      };
    case "snapshotLoaded":
      return {
        ...state,
        snapshot: action.snapshot,
        receipt: null,
        loading: false,
        error: null,
        errorStatus: null,
        errorPhase: null,
      };
    case "terminalLoaded":
      return {
        ...state,
        snapshot: action.snapshot,
        receipt: action.receipt,
        loading: false,
        error: null,
        errorStatus: null,
        errorPhase: null,
      };
    case "eventReceived": {
      if (state.events.some(({ seq }) => seq === action.event.seq)) {
        return state;
      }
      const events = [...state.events, action.event].sort(
        (left, right) => left.seq - right.seq,
      );
      return {
        ...state,
        events,
        lastSeq: Math.max(state.lastSeq, action.event.seq),
        error: state.errorPhase === "initial" ? state.error : null,
        errorStatus:
          state.errorPhase === "initial" ? state.errorStatus : null,
        errorPhase:
          state.errorPhase === "initial" ? state.errorPhase : null,
      };
    }
    case "failed":
      if (state.errorPhase === "initial" && action.phase !== "initial") {
        return { ...state, loading: false };
      }
      return {
        ...state,
        loading: false,
        error: action.message,
        errorStatus: action.status,
        errorPhase: action.phase,
      };
    case "reset":
      return initialRecoveryState;
  }
}

interface UseRecoveryOptions {
  getRecovery?: typeof getRecoveryFromServer;
  getReceipt?: typeof getReceiptFromServer;
  connectEvents?: (
    recoveryId: string,
    handlers: RecoveryEventHandlers,
  ) => () => void;
  initialSnapshot?: RecoverySnapshot | null;
  terminalRetryDelayMs?: number;
}

function terminalPairIsConsistent(
  recoveryId: string,
  snapshot: RecoverySnapshot,
  receipt: RecoveryReceipt,
): boolean {
  return (
    snapshot.recoveryId === recoveryId &&
    receipt.recoveryId === recoveryId &&
    isTerminalRecoveryStatus(snapshot.status) &&
    snapshot.status === receipt.status
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Recovery could not be loaded";
}

function errorStatus(error: unknown): number | null {
  return error instanceof HttpStatusError ? error.status : null;
}

export function useRecovery(
  recoveryId: string | null,
  options: UseRecoveryOptions = {},
): RecoveryState {
  const [state, dispatch] = useReducer(recoveryReducer, initialRecoveryState);
  const {
    getRecovery = getRecoveryFromServer,
    getReceipt = getReceiptFromServer,
    connectEvents = connectRecoveryEvents,
    initialSnapshot = null,
    terminalRetryDelayMs = 250,
  } = options;

  useEffect(() => {
    if (recoveryId === null) {
      dispatch({ type: "reset" });
      return;
    }

    const activeRecoveryId = recoveryId;
    const controller = new AbortController();
    let disposed = false;
    let terminalRefreshStarted = false;
    let terminalRetryTimer: ReturnType<typeof setTimeout> | null = null;
    dispatch({ type: "reset" });

    const fail = (error: unknown, phase: RecoveryErrorPhase) => {
      if (disposed || (error instanceof DOMException && error.name === "AbortError")) {
        return;
      }
      dispatch({
        type: "failed",
        message: errorMessage(error),
        status: errorStatus(error),
        phase,
      });
    };

    const commitTerminal = (
      snapshot: RecoverySnapshot,
      receipt: RecoveryReceipt,
    ) => {
      if (disposed) {
        return;
      }
      if (!terminalPairIsConsistent(activeRecoveryId, snapshot, receipt)) {
        fail(
          new Error("Terminal snapshot and receipt did not describe one recovery outcome"),
          "terminal",
        );
        return;
      }
      dispatch({ type: "terminalLoaded", snapshot, receipt });
    };

    const scheduleTerminalRetry = (error: unknown) => {
      fail(error, "terminal");
      terminalRefreshStarted = false;
      if (disposed || terminalRetryTimer !== null) {
        return;
      }
      terminalRetryTimer = setTimeout(() => {
        terminalRetryTimer = null;
        refreshTerminal();
      }, terminalRetryDelayMs);
    };

    const loadReceiptForSnapshot = (snapshot: RecoverySnapshot) => {
      terminalRefreshStarted = true;
      void getReceipt(activeRecoveryId, controller.signal)
        .then((receipt) => commitTerminal(snapshot, receipt))
        .catch(scheduleTerminalRetry);
    };

    function refreshTerminal() {
      if (terminalRefreshStarted || disposed) {
        return;
      }
      terminalRefreshStarted = true;
      void Promise.all([
        getRecovery(activeRecoveryId, controller.signal),
        getReceipt(activeRecoveryId, controller.signal),
      ])
        .then(([snapshot, receipt]) => commitTerminal(snapshot, receipt))
        .catch(scheduleTerminalRetry);
    }

    const acceptInitialSnapshot = (snapshot: RecoverySnapshot) => {
      if (disposed || terminalRefreshStarted) {
        return;
      }
      if (snapshot.recoveryId !== activeRecoveryId) {
        fail(new Error("Recovery snapshot did not belong to the active recovery"), "initial");
        return;
      }
      if (isTerminalRecoveryStatus(snapshot.status)) {
        loadReceiptForSnapshot(snapshot);
        return;
      }
      dispatch({ type: "snapshotLoaded", snapshot });
    };

    if (initialSnapshot !== null) {
      acceptInitialSnapshot(initialSnapshot);
    } else {
      dispatch({ type: "loading" });
      void getRecovery(activeRecoveryId, controller.signal)
        .then(acceptInitialSnapshot)
        .catch((error: unknown) => fail(error, "initial"));
    }

    const disconnect = connectEvents(activeRecoveryId, {
      onEvent: (event) => {
        if (disposed) {
          return;
        }
        dispatch({ type: "eventReceived", event });
        if (event.terminal) {
          refreshTerminal();
        }
      },
      onError: (error) => fail(error, "events"),
    });

    return () => {
      disposed = true;
      controller.abort();
      if (terminalRetryTimer !== null) {
        clearTimeout(terminalRetryTimer);
      }
      disconnect();
    };
  }, [
    connectEvents,
    getReceipt,
    getRecovery,
    initialSnapshot,
    recoveryId,
    terminalRetryDelayMs,
  ]);

  return state;
}
