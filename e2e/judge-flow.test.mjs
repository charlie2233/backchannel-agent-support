import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  CAPTURE_FILES,
  CHROME_LAUNCH_OPTIONS,
  CaptureContractError,
  SERVER_ENV_ALLOWLIST,
  VIEWPORTS,
  assertCaptureSafeDom,
  assertCompletedProvenance,
  assertDeclinedProvenance,
  assertFinalBuild,
  assertPendingProvenance,
  assertResetResponse,
  assertSessionRecoveryId,
  assertTerminalViewportVisibility,
  captureFailureCode,
  createDecisionPayload,
  createServerEnvironment,
  isRecoveryCreationResponse,
  measureTerminalViewportGeometry,
  parsePngDimensions,
  resetSession,
  terminalHeadingAnchorTop,
} from "./judge-flow.mjs";

const digest = `sha256:${"a".repeat(64)}`;
const recoveryId = "11111111-2222-4333-8444-555555555555";

function pendingSnapshot(overrides = {}) {
  return {
    recoveryId,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    status: "pending_approval",
    currentStep: 3,
    currentStepSummary: "Exact remedy requires approval.",
    createdAt: "2026-07-20T00:00:00Z",
    updatedAt: "2026-07-20T00:00:01Z",
    rootTraceId: "trace_sdk_stub_contract",
    modelIds: [],
    sdkVersion: "0.8.1",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: "b".repeat(64),
    pendingApproval: {
      remedyId: "remedy-contract",
      remedyDigest: digest,
      terms: {
        bookingId: "booking-contract",
        action: "replace_room",
        replacement: { fromRoomType: "standard", toRoomType: "suite" },
        stay: { checkIn: "2026-08-01", checkOut: "2026-08-03" },
        currency: "USD",
      },
      costDeltaMinor: 0,
      changedFields: ["roomType"],
      providerCommitments: ["Replacement held until consent expiry"],
      expiry: "2026-07-20T00:10:00Z",
      hardConstraintSatisfied: true,
      delegatedAuthoritySatisfied: true,
      toolCallId: "call-contract",
      executionStarted: false,
    },
    ...overrides,
  };
}

function terminalSnapshot(status) {
  return {
    ...pendingSnapshot(),
    status,
    currentStep: 5,
    currentStepSummary: status === "completed" ? "Receipt sealed." : "Closed safely.",
    pendingApproval: null,
  };
}

function completedReceipt(overrides = {}) {
  return {
    recoveryId,
    executionMode: "sdk_stub",
    status: "completed",
    simulated: true,
    providerExecution: true,
    modelIds: [],
    rootTraceId: "trace_sdk_stub_contract",
    sdkVersion: "0.8.1",
    protocolVersion: "backchannel.approval.v1",
    agentGraphVersion: "backchannel.hotel-agent.v1",
    promptToolSchemaHash: "b".repeat(64),
    boundary: "Demo adapter only. No OpenAI model call or real booking.",
    providerResult: "Demo replacement recorded.",
    authorizationSource: "Exact user approval",
    verificationResults: ["Receipt sealed."],
    decision: "approved",
    decisionRemedyDigest: digest,
    executionCount: 1,
    providerDispatchStarted: true,
    exactInterruptionRejected: false,
    permissionRevoked: true,
    scopeClosed: true,
    approvedRemedyDigest: digest,
    quotaEvidence: null,
    ...overrides,
  };
}

function declinedReceipt(overrides = {}) {
  return {
    ...completedReceipt(),
    status: "closed_without_action",
    providerExecution: false,
    providerResult: "Provider dispatch did not begin.",
    authorizationSource: "User declined exact remedy",
    decision: "declined",
    executionCount: 0,
    providerDispatchStarted: false,
    exactInterruptionRejected: true,
    approvedRemedyDigest: null,
    ...overrides,
  };
}

test("pins the Chrome capture matrix and six final evidence filenames", () => {
  assert.deepEqual(VIEWPORTS, [
    { name: "desktop", width: 1440, height: 1024 },
    { name: "mobile", width: 390, height: 844 },
  ]);
  assert.deepEqual(CHROME_LAUNCH_OPTIONS, { channel: "chrome", headless: true });
  assert.deepEqual(CAPTURE_FILES, [
    "desktop-consent.png",
    "desktop-completed.png",
    "desktop-declined.png",
    "mobile-consent.png",
    "mobile-completed.png",
    "mobile-declined.png",
  ]);
});

test("accepts only the exact 201 recovery creation response contract", () => {
  const origin = "http://127.0.0.1:43123";
  const response = (status, path = "/api/recoveries", method = "POST") => ({
    url: () => `${origin}${path}`,
    request: () => ({ method: () => method }),
    status: () => status,
  });

  assert.equal(isRecoveryCreationResponse(response(201), origin), true);
  assert.equal(isRecoveryCreationResponse(response(200), origin), false);
  assert.equal(isRecoveryCreationResponse(response(201, "/health"), origin), false);
  assert.equal(isRecoveryCreationResponse(response(201, "/api/recoveries", "GET"), origin), false);
  assert.equal(
    isRecoveryCreationResponse(response(201), "http://127.0.0.1:43124"),
    false,
  );
});

test("requires the session recovery UUID to be valid and match the creation response", () => {
  assert.equal(assertSessionRecoveryId(recoveryId, recoveryId), recoveryId);
  for (const stored of [
    null,
    "",
    "not-a-uuid",
    "11111111-2222-4333-8444-55555555555Z",
    "22222222-2222-4333-8444-555555555555",
  ]) {
    assert.throws(
      () => assertSessionRecoveryId(stored, recoveryId),
      /capture_session_recovery/,
    );
  }
});

test("publishes only allowlisted static failure codes", () => {
  assert.equal(
    captureFailureCode(new CaptureContractError("capture_chrome_missing")),
    "capture_chrome_missing",
  );
  assert.equal(
    captureFailureCode(new CaptureContractError("capture_pending_contract")),
    "capture_pending_contract",
  );
  for (const resetCode of [
    "capture_reset_status",
    "capture_reset_body",
    "capture_reset_identity",
  ]) {
    assert.equal(captureFailureCode(new CaptureContractError(resetCode)), resetCode);
  }
  assert.equal(
    captureFailureCode(new CaptureContractError("capture_terminal_viewport")),
    "capture_terminal_viewport",
  );
  const tokenShapedDetail = ["sk", "-", "Z".repeat(32)].join("");
  assert.equal(captureFailureCode(new Error(tokenShapedDetail)), "capture_unexpected");
  assert.equal(
    captureFailureCode(
      new CaptureContractError(["capture", "detail", tokenShapedDetail].join("_")),
    ),
    "capture_unexpected",
  );
});

test("requires the built final bundle and creates only an allowlisted keyless server environment", () => {
  const root = mkdtempSync(join(tmpdir(), "backchannel-final-build-contract-"));
  try {
    assert.throws(() => assertFinalBuild(root), /capture_final_build_missing/);
    mkdirSync(join(root, "assets"));
    writeFileSync(join(root, "index.html"), "<!doctype html>");
    assert.doesNotThrow(() => assertFinalBuild(root));

    const credentialName = ["OPENAI", "API", "KEY"].join("_");
    const environment = createServerEnvironment({
      baseEnvironment: {
        PATH: "/usr/bin:/bin",
        LANG: "en_US.UTF-8",
        HOME: "/must-not-be-forwarded",
        [credentialName]: "must-not-be-forwarded",
        RANDOM_SECRET: "must-not-be-forwarded",
      },
      databasePath: join(root, "capture.sqlite3"),
      frontendDistPath: root,
      identityHmacSecret: "generated-never-logged-secret-value",
      port: 43123,
    });

    assert.deepEqual(Object.keys(environment).sort(), [...SERVER_ENV_ALLOWLIST].sort());
    assert.equal(environment.BACKCHANNEL_DEPLOYED, "false");
    assert.equal(environment.BACKCHANNEL_DEMO_RESET_ENABLED, "true");
    assert.equal(environment.BACKCHANNEL_FRONTEND_DIST_PATH, root);
    assert.equal(environment.BACKCHANNEL_DB_PATH, join(root, "capture.sqlite3"));
    assert.equal(environment.PORT, "43123");
    assert.equal(environment.PATH, "/usr/bin:/bin");
    assert.equal(credentialName in environment, false);
    assert.equal("HOME" in environment, false);
    assert.equal("RANDOM_SECRET" in environment, false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("accepts only the exact session reset acknowledgement", () => {
  assert.doesNotThrow(() => assertResetResponse({ reset: true }));
  for (const value of [
    { reset: false },
    { reset: true, count: 1 },
    { ok: true },
    null,
    "reset",
  ]) {
    assert.throws(() => assertResetResponse(value), /capture_reset_body/);
  }
});

async function exerciseReset({ status, body }, events = []) {
  const originalFetch = globalThis.fetch;
  const originalSessionStorage = globalThis.sessionStorage;
  let observedRequest = null;
  globalThis.fetch = async (path, options) => {
    events.push("fetch");
    observedRequest = { path, options };
    return {
      status,
      async json() {
        events.push("json");
        return body;
      },
    };
  };
  globalThis.sessionStorage = {
    clear() {
      events.push("clear");
    },
  };
  const page = {
    async evaluate(callback, argument) {
      return await callback(argument);
    },
  };
  try {
    const result = await resetSession(page);
    return { events, observedRequest, result };
  } finally {
    globalThis.fetch = originalFetch;
    if (originalSessionStorage === undefined) {
      delete globalThis.sessionStorage;
    } else {
      globalThis.sessionStorage = originalSessionStorage;
    }
  }
}

test("reset sends exact JSON and clears session storage only after acknowledgement", async () => {
  const { events, observedRequest } = await exerciseReset({
    status: 200,
    body: { reset: true },
  });

  assert.deepEqual(observedRequest, {
    path: "/api/demo/reset",
    options: {
      method: "POST",
      body: "{}",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
    },
  });
  assert.deepEqual(events, ["fetch", "json", "clear"]);
});

test("reset status and body failures are redacted and never clear session storage", async () => {
  const statusEvents = [];
  await assert.rejects(
    () => exerciseReset({ status: 415, body: { reset: true } }, statusEvents),
    /capture_reset_status/,
  );
  assert.deepEqual(statusEvents, ["fetch", "json"]);
  const bodyEvents = [];
  await assert.rejects(
    () => exerciseReset({ status: 200, body: { reset: false } }, bodyEvents),
    /capture_reset_body/,
  );
  assert.deepEqual(bodyEvents, ["fetch", "json"]);
});

test("builds an exact decision payload from the pending approval", () => {
  assert.deepEqual(
    createDecisionPayload("approve", pendingSnapshot(), "decision-contract"),
    {
      decision: "approve",
      clientDecisionId: "decision-contract",
      remedyId: "remedy-contract",
      remedyDigest: digest,
      toolCallId: "call-contract",
    },
  );
  assert.throws(
    () => createDecisionPayload("approve", terminalSnapshot("completed"), "decision"),
    /capture_pending_contract/,
  );
});

test("accepts truthful pending evidence and rejects every provenance drift", () => {
  const health = {
    backend: "stub",
    liveReady: false,
    sdkStubReady: true,
    providerBoundary: "demo_adapter_only",
  };
  const visibleText = "SDK stub Approve exact remedy Execution has not begun.";
  assert.doesNotThrow(() =>
    assertPendingProvenance({ health, snapshot: pendingSnapshot(), visibleText }),
  );

  const invalidCases = [
    { health: { ...health, liveReady: true }, snapshot: pendingSnapshot(), visibleText },
    {
      health,
      snapshot: pendingSnapshot({ executionMode: "openai_live" }),
      visibleText,
    },
    {
      health,
      snapshot: pendingSnapshot({ modelIds: ["gpt-5.6"] }),
      visibleText,
    },
    {
      health,
      snapshot: pendingSnapshot({
        pendingApproval: { ...pendingSnapshot().pendingApproval, executionStarted: true },
      }),
      visibleText,
    },
    { health, snapshot: pendingSnapshot(), visibleText: `${visibleText} OpenAI live` },
  ];
  for (const candidate of invalidCases) {
    assert.throws(() => assertPendingProvenance(candidate), /capture_pending_contract/);
  }
});

test("accepts only decision-bound completed provenance", () => {
  const pending = pendingSnapshot();
  const decision = {
    clientDecisionId: "decision-approved",
    recoveryId,
    decision: "approve",
    status: "completed",
    approvedRemedyDigest: digest,
    executionStarted: true,
  };
  const visibleText = "SDK stub Completed receipt None — no model call";
  assert.doesNotThrow(() =>
    assertCompletedProvenance({
      pending,
      decision,
      snapshot: terminalSnapshot("completed"),
      receipt: completedReceipt(),
      visibleText,
    }),
  );

  const invalidReceipts = [
    completedReceipt({ recoveryId: "other" }),
    completedReceipt({ simulated: false }),
    completedReceipt({ modelIds: ["gpt-5.6"] }),
    completedReceipt({ providerExecution: false }),
    completedReceipt({ providerDispatchStarted: false }),
    completedReceipt({ executionCount: 2 }),
    completedReceipt({ permissionRevoked: false }),
    completedReceipt({ scopeClosed: false }),
    completedReceipt({ approvedRemedyDigest: `sha256:${"c".repeat(64)}` }),
  ];
  for (const receipt of invalidReceipts) {
    assert.throws(
      () =>
        assertCompletedProvenance({
          pending,
          decision,
          snapshot: terminalSnapshot("completed"),
          receipt,
          visibleText,
        }),
      /capture_completed_contract/,
    );
  }
  for (const terminalText of [
    "Completed receipt None — no model call",
    `${visibleText} OpenAI live`,
    `${visibleText} GPT-5.6 agents`,
  ]) {
    assert.throws(
      () =>
        assertCompletedProvenance({
          pending,
          decision,
          snapshot: terminalSnapshot("completed"),
          receipt: completedReceipt(),
          visibleText: terminalText,
        }),
      /capture_completed_contract/,
    );
  }
});

test("accepts only zero-dispatch declined provenance", () => {
  const pending = pendingSnapshot();
  const decision = {
    clientDecisionId: "decision-declined",
    recoveryId,
    decision: "decline",
    status: "closed_without_action",
    decisionRemedyDigest: digest,
    executionStarted: false,
  };
  const visibleText =
    "SDK stub Closed without action Provider dispatch did not begin. executionCount = 0";
  assert.doesNotThrow(() =>
    assertDeclinedProvenance({
      pending,
      decision,
      snapshot: terminalSnapshot("closed_without_action"),
      receipt: declinedReceipt(),
      visibleText,
    }),
  );

  const invalidReceipts = [
    declinedReceipt({ status: "outcome_unknown" }),
    declinedReceipt({ simulated: false }),
    declinedReceipt({ providerExecution: true }),
    declinedReceipt({ providerDispatchStarted: true }),
    declinedReceipt({ executionCount: 1 }),
    declinedReceipt({ exactInterruptionRejected: false }),
    declinedReceipt({ permissionRevoked: false }),
    declinedReceipt({ scopeClosed: false }),
    declinedReceipt({ approvedRemedyDigest: digest }),
  ];
  for (const receipt of invalidReceipts) {
    assert.throws(
      () =>
        assertDeclinedProvenance({
          pending,
          decision,
          snapshot: terminalSnapshot("closed_without_action"),
          receipt,
          visibleText,
        }),
      /capture_declined_contract/,
    );
  }
  for (const terminalText of [
    "Closed without action Provider dispatch did not begin. executionCount = 0",
    `${visibleText} OpenAI live`,
    `${visibleText} GPT-5.6 agents`,
  ]) {
    assert.throws(
      () =>
        assertDeclinedProvenance({
          pending,
          decision,
          snapshot: terminalSnapshot("closed_without_action"),
          receipt: declinedReceipt(),
          visibleText: terminalText,
        }),
      /capture_declined_contract/,
    );
  }
});

test("forbidden DOM detection never includes the matched credential or state value", () => {
  assert.doesNotThrow(() =>
    assertCaptureSafeDom("<main>SDK stub</main>", "SDK stub receipt"),
  );
  const credentialName = ["OPENAI", "API", "KEY"].join("_");
  const signingName = ["BACKCHANNEL", "IDENTITY", "HMAC", "SECRET"].join("_");
  const sensitiveCases = [
    [credentialName, "super-sensitive-value"].join("="),
    ["Author", "ization: ", "Bearer ", "super-sensitive-token-value"].join(""),
    ["sk", "-", "super-sensitive-token-value-with-padding"].join(""),
    ['"state_', 'json":"super-sensitive-state"'].join(""),
    [signingName, "super-sensitive-secret"].join("="),
    ["Set", "-Cookie: super-sensitive-cookie"].join(""),
  ];
  for (const sensitive of sensitiveCases) {
    assert.throws(
      () => assertCaptureSafeDom(`<main>${sensitive}</main>`, sensitive),
      (error) => {
        assert.equal(error instanceof Error, true);
        assert.equal(error.message, "capture_forbidden_dom_marker");
        assert.equal(error.message.includes("super-sensitive"), false);
        return true;
      },
    );
  }
});

test("parses PNG dimensions without a decoder and rejects malformed evidence", () => {
  const png = Buffer.alloc(24);
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).copy(png, 0);
  png.write("IHDR", 12, "ascii");
  png.writeUInt32BE(1440, 16);
  png.writeUInt32BE(1024, 20);
  assert.deepEqual(parsePngDimensions(png), { width: 1440, height: 1024 });

  assert.throws(() => parsePngDimensions(Buffer.alloc(24)), /capture_png_contract/);
  assert.throws(() => parsePngDimensions(png.subarray(0, 20)), /capture_png_contract/);
});

test("accepts visible unobscured terminal heading and verdict rectangles", () => {
  assert.equal(terminalHeadingAnchorTop({ width: 390, height: 844 }), 74);
  assert.equal(terminalHeadingAnchorTop({ width: 1440, height: 1024 }), null);
  assert.doesNotThrow(() =>
    assertTerminalViewportVisibility({
      viewport: { width: 390, height: 844 },
      headingBlockRect: {
        left: 18,
        top: 74,
        right: 260,
        bottom: 106,
        width: 242,
        height: 32,
      },
      verdictRect: {
        left: 18,
        top: 144,
        right: 372,
        bottom: 224,
        width: 354,
        height: 80,
      },
      headingBlockUnobscuredSamples: [true, true, true, true, true],
      verdictUnobscuredSamples: [true, true, true, true, true],
      headingAnchorTop: 74,
    }),
  );
});

test("rejects a mobile terminal heading when its eyebrow begins under the sticky bar", () => {
  assert.throws(
    () =>
      assertTerminalViewportVisibility({
        viewport: { width: 390, height: 844 },
        headingRect: {
          left: 18,
          top: 74,
          right: 260,
          bottom: 106,
          width: 242,
          height: 32,
        },
        headingBlockRect: {
          left: 18,
          top: 42,
          right: 372,
          bottom: 132,
          width: 354,
          height: 90,
        },
        verdictRect: {
          left: 18,
          top: 144,
          right: 372,
          bottom: 224,
          width: 354,
          height: 80,
        },
        headingBlockUnobscuredSamples: [true, true, true, true, true],
        verdictUnobscuredSamples: [true, true, true, true, true],
        headingAnchorTop: 74,
      }),
    /capture_terminal_viewport/,
  );
});

test("rejects offscreen, zero-size, and obscured terminal evidence", () => {
  const base = {
    viewport: { width: 1440, height: 1024 },
    headingBlockRect: {
      left: 1040,
      top: 120,
      right: 1300,
      bottom: 156,
      width: 260,
      height: 36,
    },
    verdictRect: {
      left: 1040,
      top: 172,
      right: 1400,
      bottom: 252,
      width: 360,
      height: 80,
    },
    headingBlockUnobscuredSamples: [true, true, true, true, true],
    verdictUnobscuredSamples: [true, true, true, true, true],
    headingAnchorTop: null,
  };
  const invalidCases = [
    {
      ...base,
      headingBlockRect: {
        ...base.headingBlockRect,
        top: 1100,
        bottom: 1136,
      },
    },
    {
      ...base,
      verdictRect: { ...base.verdictRect, width: 0, right: 1040 },
    },
    {
      ...base,
      headingBlockRect: {
        ...base.headingBlockRect,
        left: -12,
        right: 248,
      },
    },
    {
      ...base,
      verdictRect: { ...base.verdictRect, bottom: 1040, height: 868 },
    },
    {
      ...base,
      headingBlockUnobscuredSamples: [true, true, true, false, true],
    },
    {
      ...base,
      verdictUnobscuredSamples: [true, false, true, true, true],
    },
  ];
  for (const candidate of invalidCases) {
    assert.throws(
      () => assertTerminalViewportVisibility(candidate),
      /capture_terminal_viewport/,
    );
  }
});

test("rejects a terminal element when one sampled corner is obscured", () => {
  const ownedChild = {};
  const foreignOverlay = {};
  const element = {
    contains: (candidate) => candidate === ownedChild,
    getBoundingClientRect: () => ({
      left: 18,
      top: 74,
      right: 260,
      bottom: 106,
      width: 242,
      height: 32,
    }),
  };
  const geometry = measureTerminalViewportGeometry(element, {
    view: { innerWidth: 390, innerHeight: 844 },
    documentApi: {
      elementFromPoint: (x, y) =>
        x === 19 && y === 105 ? foreignOverlay : ownedChild,
    },
  });
  assert.deepEqual(geometry.unobscuredSamples, [true, true, true, false, true]);
  assert.throws(
    () =>
      assertTerminalViewportVisibility({
        viewport: { width: 390, height: 844 },
        headingBlockRect: geometry.rect,
        verdictRect: {
          left: 18,
          top: 144,
          right: 372,
          bottom: 224,
          width: 354,
          height: 80,
        },
        headingBlockUnobscuredSamples: geometry.unobscuredSamples,
        verdictUnobscuredSamples: [true, true, true, true, true],
        headingAnchorTop: 74,
      }),
    (error) => {
      assert.equal(error.message, "capture_terminal_viewport");
      assert.equal(error.message.includes("overlay"), false);
      return true;
    },
  );
});
