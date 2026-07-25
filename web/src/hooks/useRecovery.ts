import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import {
  getReceipt as getReceiptFromServer,
  getRecovery as getRecoveryFromServer,
  HttpStatusError,
  PublicApiError,
} from "../api/client";
import {
  openRecoveryEventStream,
  RecoveryEventStreamContractError,
  type RecoveryEvent,
  type RecoveryEventStreamHandlers,
  type RecoveryEventStreamRequest,
  type RecoveryEventStreamResult,
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
  | { type: "eventsOpened" }
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
        error: state.errorPhase === "events" ? state.error : null,
        errorStatus:
          state.errorPhase === "events" ? state.errorStatus : null,
        errorPhase:
          state.errorPhase === "events" ? state.errorPhase : null,
      };
    case "snapshotLoaded":
      return {
        ...state,
        snapshot: action.snapshot,
        receipt: null,
        loading: false,
        error: state.errorPhase === "events" ? state.error : null,
        errorStatus:
          state.errorPhase === "events" ? state.errorStatus : null,
        errorPhase:
          state.errorPhase === "events" ? state.errorPhase : null,
      };
    case "terminalLoaded":
      return {
        ...state,
        snapshot: action.snapshot,
        receipt: action.receipt,
        loading: false,
        error: state.errorPhase === "events" ? state.error : null,
        errorStatus:
          state.errorPhase === "events" ? state.errorStatus : null,
        errorPhase:
          state.errorPhase === "events" ? state.errorPhase : null,
      };
    case "eventReceived": {
      if (action.event.seq !== state.lastSeq + 1) {
        return state;
      }
      return {
        ...state,
        events: [...state.events, action.event],
        lastSeq: action.event.seq,
      };
    }
    case "eventsOpened":
      if (state.errorPhase !== "events") {
        return state;
      }
      return {
        ...state,
        error: null,
        errorStatus: null,
        errorPhase: null,
      };
    case "failed":
      if (
        (state.errorPhase === "initial" && action.phase !== "initial") ||
        (state.errorPhase === "terminal" && action.phase === "events")
      ) {
        return { ...state, loading: false };
      }
      return {
        ...state,
        loading: action.phase === "events" ? state.loading : false,
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
  openEvents?: (
    request: RecoveryEventStreamRequest,
    handlers: RecoveryEventStreamHandlers,
  ) => Promise<RecoveryEventStreamResult>;
  initialSnapshot?: RecoverySnapshot | null;
  terminalRetryDelayMs?: number;
  eventReconnectDelayMs?: number;
  eventReconnectAttempts?: number;
}

function terminalPairIsConsistent(
  recoveryId: string,
  snapshot: RecoverySnapshot,
  receipt: RecoveryReceipt,
): boolean {
  const snapshotModelIds = snapshot.modelIds;
  const provenanceIsConsistent =
    snapshot.rootTraceId !== undefined &&
    receipt.rootTraceId !== undefined &&
    snapshot.rootTraceId === receipt.rootTraceId &&
    Array.isArray(snapshotModelIds) &&
    snapshotModelIds.length === receipt.modelIds.length &&
    snapshotModelIds.every(
      (modelId, index) => modelId === receipt.modelIds[index],
    ) &&
    snapshot.sdkVersion !== undefined &&
    receipt.sdkVersion !== undefined &&
    snapshot.sdkVersion === receipt.sdkVersion &&
    snapshot.protocolVersion !== undefined &&
    receipt.protocolVersion !== undefined &&
    snapshot.protocolVersion === receipt.protocolVersion &&
    snapshot.agentGraphVersion !== undefined &&
    receipt.agentGraphVersion !== undefined &&
    snapshot.agentGraphVersion === receipt.agentGraphVersion &&
    snapshot.promptToolSchemaHash !== undefined &&
    receipt.promptToolSchemaHash !== undefined &&
    snapshot.promptToolSchemaHash === receipt.promptToolSchemaHash;
  const baseIsConsistent =
    snapshot.recoveryId === recoveryId &&
    receipt.recoveryId === recoveryId &&
    isTerminalRecoveryStatus(snapshot.status) &&
    snapshot.status === receipt.status &&
    snapshot.executionMode === receipt.executionMode &&
    provenanceIsConsistent;
  if (!baseIsConsistent) {
    return false;
  }
  const quotaEvidence = receipt.quotaEvidence;
  const quotaScenario = snapshot.scenarioId === "api-quota";
  if (quotaScenario !== (quotaEvidence !== null)) {
    return false;
  }
  if (!quotaScenario || quotaEvidence === null) {
    return true;
  }
  if (
    snapshot.status !== "completed" ||
    snapshot.currentStep !== 5 ||
    snapshot.pendingApproval !== null ||
    snapshot.executionMode === "openai_live"
  ) {
    return false;
  }
  if (snapshot.executionMode === "sdk_stub") {
    return (
      quotaEvidence.source === "sdk_simulator" &&
      snapshot.rootTraceId === null &&
      Array.isArray(snapshot.modelIds) &&
      snapshot.modelIds.length === 0 &&
      typeof snapshot.sdkVersion === "string" &&
      snapshot.sdkVersion.length > 0 &&
      snapshot.protocolVersion === "backchannel.quota.v1" &&
      snapshot.agentGraphVersion === "backchannel.quota-agent.v1" &&
      typeof snapshot.promptToolSchemaHash === "string" &&
      /^[0-9a-f]{64}$/.test(snapshot.promptToolSchemaHash)
    );
  }
  return (
    quotaEvidence.source === "recorded_fixture" &&
    snapshot.rootTraceId === null &&
    Array.isArray(snapshot.modelIds) &&
    snapshot.modelIds.length === 0 &&
    snapshot.sdkVersion === null &&
    snapshot.protocolVersion === null &&
    snapshot.agentGraphVersion === null &&
    snapshot.promptToolSchemaHash === null
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

interface EventRetryState {
  eventsRetryAvailable: boolean;
  eventsRetryAfterSeconds: number | null;
  eventsRetrying: boolean;
}

const initialEventRetryState: EventRetryState = {
  eventsRetryAvailable: false,
  eventsRetryAfterSeconds: null,
  eventsRetrying: false,
};

export interface UseRecoveryResult extends RecoveryState, EventRetryState {
  retryEvents: () => void;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function useRecovery(
  recoveryId: string | null,
  options: UseRecoveryOptions = {},
): UseRecoveryResult {
  const [state, dispatch] = useReducer(recoveryReducer, initialRecoveryState);
  const [eventRetryState, setEventRetryState] = useState<EventRetryState>(
    initialEventRetryState,
  );
  const eventRetryStateRef = useRef(eventRetryState);
  const retryEventsRef = useRef<() => void>(() => undefined);
  const retryEvents = useCallback(() => retryEventsRef.current(), []);
  const {
    getRecovery = getRecoveryFromServer,
    getReceipt = getReceiptFromServer,
    openEvents = openRecoveryEventStream,
    initialSnapshot = null,
    terminalRetryDelayMs = 250,
    eventReconnectDelayMs = 250,
    eventReconnectAttempts = 3,
  } = options;
  const boundedReconnectAttempts =
    Number.isInteger(eventReconnectAttempts) &&
    eventReconnectAttempts >= 0
      ? Math.min(eventReconnectAttempts, 5)
      : 3;
  const boundedReconnectDelayMs =
    Number.isFinite(eventReconnectDelayMs) && eventReconnectDelayMs >= 0
      ? Math.min(eventReconnectDelayMs, 30_000)
      : 250;

  useEffect(() => {
    const updateEventRetryState = (next: EventRetryState) => {
      eventRetryStateRef.current = next;
      setEventRetryState(next);
    };
    updateEventRetryState(initialEventRetryState);
    retryEventsRef.current = () => undefined;
    if (recoveryId === null) {
      dispatch({ type: "reset" });
      return;
    }

    const activeRecoveryId = recoveryId;
    let disposed = false;
    let authoritativeGeneration = 0;
    let authoritativeController = new AbortController();
    let terminalRefreshStarted = false;
    let terminalRetryTimer: ReturnType<typeof setTimeout> | null = null;
    let streamGeneration = 0;
    let streamController: AbortController | null = null;
    let streamRetryTimer: ReturnType<typeof setTimeout> | null = null;
    let streamRetryCount = 0;
    let durableCursor = 0;
    let durableLastEvent: RecoveryEvent | null = null;
    let retryEligibilityGeneration = 0;
    let retryEligibilityTimer: ReturnType<typeof setTimeout> | null = null;
    dispatch({ type: "reset" });

    const fail = (error: unknown, phase: RecoveryErrorPhase) => {
      if (disposed || isAbortError(error)) {
        return;
      }
      dispatch({
        type: "failed",
        message: errorMessage(error),
        status: errorStatus(error),
        phase,
      });
    };

    const clearRetryEligibility = () => {
      retryEligibilityGeneration += 1;
      if (retryEligibilityTimer !== null) {
        clearTimeout(retryEligibilityTimer);
        retryEligibilityTimer = null;
      }
    };

    const restoreManualEventRetry = (
      controller: AbortController,
      generation: number,
    ) => {
      if (
        disposed ||
        controller !== authoritativeController ||
        generation !== authoritativeGeneration ||
        controller.signal.aborted
      ) {
        return;
      }
      updateEventRetryState({
        eventsRetryAvailable: true,
        eventsRetryAfterSeconds: null,
        eventsRetrying: false,
      });
    };

    const commitTerminal = (
      snapshot: RecoverySnapshot,
      receipt: RecoveryReceipt,
    ) => {
      if (disposed) {
        return false;
      }
      if (!terminalPairIsConsistent(activeRecoveryId, snapshot, receipt)) {
        fail(
          new Error(
            "Terminal snapshot and receipt did not describe one recovery outcome",
          ),
          "terminal",
        );
        return false;
      }
      dispatch({ type: "terminalLoaded", snapshot, receipt });
      return true;
    };

    const scheduleTerminalRetry = (
      error: unknown,
      controller: AbortController,
      generation: number,
    ) => {
      if (
        disposed ||
        controller !== authoritativeController ||
        generation !== authoritativeGeneration
      ) {
        return;
      }
      fail(error, "terminal");
      terminalRefreshStarted = false;
      if (
        disposed ||
        isAbortError(error) ||
        terminalRetryTimer !== null
      ) {
        return;
      }
      terminalRetryTimer = setTimeout(() => {
        terminalRetryTimer = null;
        refreshTerminal();
      }, terminalRetryDelayMs);
    };

    const loadReceiptForSnapshot = (snapshot: RecoverySnapshot) => {
      terminalRefreshStarted = true;
      const controller = authoritativeController;
      const generation = authoritativeGeneration;
      void getReceipt(activeRecoveryId, controller.signal)
        .then((receipt) => {
          if (
            disposed ||
            controller !== authoritativeController ||
            generation !== authoritativeGeneration
          ) {
            return;
          }
          commitTerminal(snapshot, receipt);
        })
        .catch((error: unknown) =>
          scheduleTerminalRetry(error, controller, generation),
        );
    };

    function refreshTerminal() {
      if (terminalRefreshStarted || disposed) {
        return;
      }
      terminalRefreshStarted = true;
      const controller = authoritativeController;
      const generation = authoritativeGeneration;
      void Promise.all([
        getRecovery(activeRecoveryId, controller.signal),
        getReceipt(activeRecoveryId, controller.signal),
      ])
        .then(([snapshot, receipt]) => {
          if (
            disposed ||
            controller !== authoritativeController ||
            generation !== authoritativeGeneration
          ) {
            return;
          }
          commitTerminal(snapshot, receipt);
        })
        .catch((error: unknown) =>
          scheduleTerminalRetry(error, controller, generation),
        );
    }

    const acceptInitialSnapshot = (
      snapshot: RecoverySnapshot,
      generation: number,
    ) => {
      if (
        disposed ||
        generation !== authoritativeGeneration ||
        terminalRefreshStarted
      ) {
        return;
      }
      if (snapshot.recoveryId !== activeRecoveryId) {
        fail(
          new Error("Recovery snapshot did not belong to the active recovery"),
          "initial",
        );
        return;
      }
      if (isTerminalRecoveryStatus(snapshot.status)) {
        loadReceiptForSnapshot(snapshot);
        return;
      }
      dispatch({ type: "snapshotLoaded", snapshot });
    };

    if (initialSnapshot !== null) {
      acceptInitialSnapshot(initialSnapshot, authoritativeGeneration);
    } else {
      dispatch({ type: "loading" });
      const generation = authoritativeGeneration;
      const controller = authoritativeController;
      void getRecovery(activeRecoveryId, controller.signal)
        .then((snapshot) => acceptInitialSnapshot(snapshot, generation))
        .catch((error: unknown) => {
          if (
            !disposed &&
            controller === authoritativeController &&
            generation === authoritativeGeneration &&
            !terminalRefreshStarted
          ) {
            fail(error, "initial");
          }
        });
    }

    const stopStream = () => {
      streamGeneration += 1;
      if (streamRetryTimer !== null) {
        clearTimeout(streamRetryTimer);
        streamRetryTimer = null;
      }
      const controller = streamController;
      streamController = null;
      controller?.abort();
    };

    const exposeEventFailure = (error: Error) => {
      fail(error, "events");
      clearRetryEligibility();
      const retryAfterSeconds =
        error instanceof PublicApiError &&
        error.code === "stream_capacity_reached" &&
        error.retryAfterSeconds !== null
          ? error.retryAfterSeconds
          : null;
      if (retryAfterSeconds === null) {
        updateEventRetryState({
          eventsRetryAvailable: true,
          eventsRetryAfterSeconds: null,
          eventsRetrying: false,
        });
        return;
      }
      updateEventRetryState({
        eventsRetryAvailable: false,
        eventsRetryAfterSeconds: retryAfterSeconds,
        eventsRetrying: false,
      });
      const eligibilityGeneration = retryEligibilityGeneration;
      retryEligibilityTimer = setTimeout(() => {
        retryEligibilityTimer = null;
        if (
          disposed ||
          eligibilityGeneration !== retryEligibilityGeneration
        ) {
          return;
        }
        updateEventRetryState({
          eventsRetryAvailable: true,
          eventsRetryAfterSeconds: null,
          eventsRetrying: false,
        });
      }, retryAfterSeconds * 1_000);
    };

    const scheduleStreamReconnect = () => {
      if (disposed || streamRetryTimer !== null) {
        return;
      }
      if (streamRetryCount >= boundedReconnectAttempts) {
        exposeEventFailure(
          new Error("Recovery event stream disconnected."),
        );
        return;
      }
      const expectedGeneration = streamGeneration;
      streamRetryTimer = setTimeout(() => {
        streamRetryTimer = null;
        if (
          disposed ||
          expectedGeneration !== streamGeneration
        ) {
          return;
        }
        streamRetryCount += 1;
        startStream();
      }, boundedReconnectDelayMs);
    };

    function startStream() {
      if (disposed) {
        return;
      }
      streamGeneration += 1;
      const generation = streamGeneration;
      const previousController = streamController;
      const controller = new AbortController();
      streamController = controller;
      previousController?.abort();
      const request: RecoveryEventStreamRequest = {
        recoveryId: activeRecoveryId,
        afterSeq: durableCursor,
        lastEvent: durableLastEvent,
        signal: controller.signal,
      };
      void openEvents(request, {
        onOpen: () => {
          if (
            disposed ||
            generation !== streamGeneration ||
            controller.signal.aborted
          ) {
            return;
          }
          clearRetryEligibility();
          updateEventRetryState(initialEventRetryState);
          dispatch({ type: "eventsOpened" });
        },
        onEvent: (event) => {
          if (
            disposed ||
            generation !== streamGeneration ||
            controller.signal.aborted ||
            event.recoveryId !== activeRecoveryId ||
            event.seq !== durableCursor + 1
          ) {
            return;
          }
          durableCursor = event.seq;
          durableLastEvent = event;
          streamRetryCount = 0;
          dispatch({ type: "eventReceived", event });
          if (event.terminal) {
            refreshTerminal();
          }
        },
      })
        .then((result) => {
          if (
            disposed ||
            generation !== streamGeneration ||
            controller.signal.aborted
          ) {
            return;
          }
          streamController = null;
          if (
            result.lastSeq !== durableCursor ||
            result.lastEvent?.seq !== durableLastEvent?.seq
          ) {
            exposeEventFailure(new RecoveryEventStreamContractError());
            return;
          }
          if (result.kind === "terminal") {
            refreshTerminal();
            return;
          }
          scheduleStreamReconnect();
        })
        .catch((error: unknown) => {
          if (
            disposed ||
            generation !== streamGeneration ||
            controller.signal.aborted ||
            isAbortError(error)
          ) {
            return;
          }
          streamController = null;
          if (
            error instanceof PublicApiError ||
            error instanceof RecoveryEventStreamContractError
          ) {
            exposeEventFailure(error);
            return;
          }
          scheduleStreamReconnect();
        });
    }

    retryEventsRef.current = () => {
      const retryState = eventRetryStateRef.current;
      if (
        disposed ||
        !retryState.eventsRetryAvailable ||
        retryState.eventsRetrying
      ) {
        return;
      }
      updateEventRetryState({
        eventsRetryAvailable: false,
        eventsRetryAfterSeconds: null,
        eventsRetrying: true,
      });
      clearRetryEligibility();
      stopStream();
      authoritativeGeneration += 1;
      authoritativeController.abort();
      authoritativeController = new AbortController();
      terminalRefreshStarted = false;
      if (terminalRetryTimer !== null) {
        clearTimeout(terminalRetryTimer);
        terminalRetryTimer = null;
      }
      const generation = authoritativeGeneration;
      const controller = authoritativeController;
      void (async () => {
        let refreshedSnapshot: RecoverySnapshot;
        try {
          refreshedSnapshot = await getRecovery(
            activeRecoveryId,
            controller.signal,
          );
        } catch (error) {
          if (
            !disposed &&
            generation === authoritativeGeneration
          ) {
            restoreManualEventRetry(controller, generation);
          }
          return;
        }
        if (
          disposed ||
          generation !== authoritativeGeneration ||
          controller.signal.aborted
        ) {
          return;
        }
        if (refreshedSnapshot.recoveryId !== activeRecoveryId) {
          restoreManualEventRetry(controller, generation);
          return;
        }
        if (isTerminalRecoveryStatus(refreshedSnapshot.status)) {
          let refreshedReceipt: RecoveryReceipt;
          try {
            refreshedReceipt = await getReceipt(
              activeRecoveryId,
              controller.signal,
            );
          } catch (error) {
            if (
              !disposed &&
              generation === authoritativeGeneration
            ) {
              restoreManualEventRetry(controller, generation);
            }
            return;
          }
          if (
            disposed ||
            generation !== authoritativeGeneration ||
            controller.signal.aborted
          ) {
            return;
          }
          if (
            !terminalPairIsConsistent(
              activeRecoveryId,
              refreshedSnapshot,
              refreshedReceipt,
            )
          ) {
            restoreManualEventRetry(controller, generation);
            return;
          }
          commitTerminal(refreshedSnapshot, refreshedReceipt);
          terminalRefreshStarted = true;
        } else {
          dispatch({
            type: "snapshotLoaded",
            snapshot: refreshedSnapshot,
          });
        }
        if (
          disposed ||
          generation !== authoritativeGeneration ||
          controller.signal.aborted
        ) {
          return;
        }
        streamRetryCount = 0;
        startStream();
      })();
    };

    startStream();

    return () => {
      disposed = true;
      retryEventsRef.current = () => undefined;
      clearRetryEligibility();
      streamGeneration += 1;
      if (streamRetryTimer !== null) {
        clearTimeout(streamRetryTimer);
      }
      const currentStreamController = streamController;
      streamController = null;
      currentStreamController?.abort();
      authoritativeGeneration += 1;
      authoritativeController.abort();
      if (terminalRetryTimer !== null) {
        clearTimeout(terminalRetryTimer);
      }
    };
  }, [
    boundedReconnectAttempts,
    boundedReconnectDelayMs,
    getReceipt,
    getRecovery,
    initialSnapshot,
    openEvents,
    recoveryId,
    terminalRetryDelayMs,
  ]);

  return {
    ...state,
    ...eventRetryState,
    retryEvents,
  };
}
