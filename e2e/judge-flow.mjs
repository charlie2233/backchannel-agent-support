import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  statSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";
import {
  CaptureManifestError,
  buildCaptureManifest,
  captureManifestFailureCode,
  installCaptureEvidence,
  prepareCaptureSource,
} from "./capture-manifest.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const ROOT = resolve(dirname(MODULE_PATH), "..");
const FINAL_ASSET_DIRECTORY = join(ROOT, "docs", "assets", "final");
const FINAL_BUILD_DIRECTORY = join(ROOT, "web", "dist");
const SERVER_COMMAND = [".venv/bin/python", "scripts/start.py"];
const STARTUP_TIMEOUT_MS = 20_000;
const UI_TIMEOUT_MS = 20_000;
const SHUTDOWN_TIMEOUT_MS = 5_000;
const ACTIVE_HOTEL_RECOVERY_KEY = "backchannel.hotelRecovery.v1";
const IDENTITY_HMAC_ENV_NAME = "BACKCHANNEL_IDENTITY_HMAC_SECRET";
const MOBILE_TERMINAL_MAX_WIDTH = 759;
const MOBILE_STICKY_TOP_BAR_HEIGHT = 58;
const TERMINAL_HEADING_GAP = 16;
const VIEWPORT_EDGE_TOLERANCE = 0.5;

export const VIEWPORTS = Object.freeze([
  Object.freeze({ name: "desktop", width: 1440, height: 1024 }),
  Object.freeze({ name: "mobile", width: 390, height: 844 }),
]);

export const CHROME_LAUNCH_OPTIONS = Object.freeze({
  channel: "chrome",
  headless: true,
});

export const CAPTURE_FILES = Object.freeze([
  "desktop-consent.png",
  "desktop-completed.png",
  "desktop-declined.png",
  "mobile-consent.png",
  "mobile-completed.png",
  "mobile-declined.png",
]);

export const SERVER_ENV_ALLOWLIST = Object.freeze([
  "PATH",
  "LANG",
  "LC_ALL",
  "TMPDIR",
  "PYTHONUNBUFFERED",
  "PORT",
  "BACKCHANNEL_DB_PATH",
  "BACKCHANNEL_FRONTEND_DIST_PATH",
  "BACKCHANNEL_DEMO_RESET_ENABLED",
  "BACKCHANNEL_DEPLOYED",
  "BACKCHANNEL_IDENTITY_HMAC_SECRET",
]);

const FORBIDDEN_DOM_MARKERS = Object.freeze([
  /OPENAI_API_KEY/i,
  /BACKCHANNEL_IDENTITY_HMAC_SECRET/i,
  /Authorization\s*[:=]\s*Bearer\b/i,
  /\bsk-[A-Za-z0-9_-]{8,}/,
  /["']?(?:state_json|sdkState|serializedState)["']?\s*[:=]/i,
  /\bSet-Cookie\s*:/i,
  /\bsession_hash\b/i,
  /\bip_hash\b/i,
]);

export class CaptureContractError extends Error {
  constructor(code) {
    super(code);
    this.name = "CaptureContractError";
  }
}

const PUBLIC_CAPTURE_FAILURE_CODES = new Set([
  "capture_approve_response",
  "capture_chrome_missing",
  "capture_completed_contract",
  "capture_creation_response",
  "capture_decline_response",
  "capture_declined_contract",
  "capture_environment_contract",
  "capture_external_origin",
  "capture_filename_contract",
  "capture_final_build_missing",
  "capture_forbidden_dom_marker",
  "capture_health_api",
  "capture_pending_contract",
  "capture_png_contract",
  "capture_png_dimensions",
  "capture_port",
  "capture_python_missing",
  "capture_receipt_api",
  "capture_reset_body",
  "capture_reset_identity",
  "capture_reset_status",
  "capture_server_start",
  "capture_server_start_timeout",
  "capture_session_recovery",
  "capture_snapshot_api",
  "capture_terminal_viewport",
]);

function requireContract(condition, code) {
  if (!condition) {
    throw new CaptureContractError(code);
  }
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function hasNoModels(snapshot) {
  return Array.isArray(snapshot?.modelIds) && snapshot.modelIds.length === 0;
}

function includesAll(text, fragments) {
  return fragments.every((fragment) => text.includes(fragment));
}

export function assertFinalBuild(frontendDistPath) {
  const indexPath = join(frontendDistPath, "index.html");
  const assetsPath = join(frontendDistPath, "assets");
  requireContract(
    existsSync(indexPath) &&
      statSync(indexPath).isFile() &&
      existsSync(assetsPath) &&
      statSync(assetsPath).isDirectory(),
    "capture_final_build_missing",
  );
}

export function createServerEnvironment({
  baseEnvironment,
  databasePath,
  frontendDistPath,
  identityHmacSecret,
  port,
}) {
  const environment = {
    PATH: baseEnvironment.PATH ?? "/usr/bin:/bin",
    LANG: baseEnvironment.LANG ?? "C.UTF-8",
    LC_ALL: baseEnvironment.LC_ALL ?? "C.UTF-8",
    TMPDIR: baseEnvironment.TMPDIR ?? tmpdir(),
    PYTHONUNBUFFERED: "1",
    PORT: String(port),
    BACKCHANNEL_DB_PATH: databasePath,
    BACKCHANNEL_FRONTEND_DIST_PATH: frontendDistPath,
    BACKCHANNEL_DEMO_RESET_ENABLED: "true",
    BACKCHANNEL_DEPLOYED: "false",
  };
  environment[IDENTITY_HMAC_ENV_NAME] = identityHmacSecret;
  requireContract(
    Object.keys(environment).every((key) => SERVER_ENV_ALLOWLIST.includes(key)) &&
      Object.keys(environment).length === SERVER_ENV_ALLOWLIST.length,
    "capture_environment_contract",
  );
  requireContract(
    typeof identityHmacSecret === "string" &&
      Buffer.byteLength(identityHmacSecret, "utf8") >= 32,
    "capture_environment_contract",
  );
  requireContract(
    !Object.hasOwn(environment, "OPENAI_API_KEY"),
    "capture_environment_contract",
  );
  return environment;
}

export function assertSessionRecoveryId(storedRecoveryId, responseRecoveryId) {
  const recoveryIdPattern =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  requireContract(
    typeof storedRecoveryId === "string" &&
      recoveryIdPattern.test(storedRecoveryId) &&
      typeof responseRecoveryId === "string" &&
      recoveryIdPattern.test(responseRecoveryId) &&
      storedRecoveryId === responseRecoveryId,
    "capture_session_recovery",
  );
  return storedRecoveryId;
}

export function assertResetResponse(value) {
  requireContract(
    value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      Object.keys(value).length === 1 &&
      value.reset === true,
    "capture_reset_body",
  );
}

export function createDecisionPayload(action, snapshot, clientDecisionId) {
  const approval = snapshot?.pendingApproval;
  requireContract(
    snapshot?.status === "pending_approval" &&
      snapshot?.executionMode === "sdk_stub" &&
      approval !== null &&
      typeof approval === "object" &&
      approval.executionStarted === false,
    "capture_pending_contract",
  );
  requireContract(
    action === "approve" || action === "decline",
    "capture_pending_contract",
  );
  return {
    decision: action,
    clientDecisionId,
    remedyId: approval.remedyId,
    remedyDigest: approval.remedyDigest,
    toolCallId: approval.toolCallId,
  };
}

export function assertPendingProvenance({ health, snapshot, visibleText }) {
  const approval = snapshot?.pendingApproval;
  requireContract(
    health?.backend === "stub" &&
      health.liveReady === false &&
      health.sdkStubReady === true &&
      health.providerBoundary === "demo_adapter_only" &&
      snapshot?.scenarioId === "hotel" &&
      snapshot.executionMode === "sdk_stub" &&
      snapshot.status === "pending_approval" &&
      approval !== null &&
      typeof approval === "object" &&
      approval.executionStarted === false &&
      hasNoModels(snapshot) &&
      includesAll(visibleText, [
        "SDK stub",
        "Approve exact remedy",
        "Execution has not begun.",
      ]) &&
      !visibleText.includes("GPT-5.6 agents") &&
      !visibleText.includes("OpenAI live"),
    "capture_pending_contract",
  );
}

function assertTerminalIdentity(pending, snapshot, receipt, expectedStatus, code) {
  requireContract(
    pending?.recoveryId === snapshot?.recoveryId &&
      snapshot.recoveryId === receipt?.recoveryId &&
      pending.executionMode === "sdk_stub" &&
      snapshot.executionMode === pending.executionMode &&
      receipt.executionMode === pending.executionMode &&
      snapshot.status === expectedStatus &&
      receipt.status === expectedStatus &&
      snapshot.pendingApproval === null &&
      hasNoModels(pending) &&
      hasNoModels(snapshot) &&
      Array.isArray(receipt.modelIds) &&
      receipt.modelIds.length === 0 &&
      pending.rootTraceId === snapshot.rootTraceId &&
      snapshot.rootTraceId === receipt.rootTraceId &&
      sameJson(pending.modelIds, snapshot.modelIds) &&
      sameJson(snapshot.modelIds, receipt.modelIds),
    code,
  );
}

export function assertCompletedProvenance({
  pending,
  decision,
  snapshot,
  receipt,
  visibleText,
}) {
  const digest = pending?.pendingApproval?.remedyDigest;
  assertTerminalIdentity(
    pending,
    snapshot,
    receipt,
    "completed",
    "capture_completed_contract",
  );
  requireContract(
    decision?.recoveryId === pending.recoveryId &&
      decision.decision === "approve" &&
      decision.status === "completed" &&
      decision.executionStarted === true &&
      decision.approvedRemedyDigest === digest &&
      receipt.simulated === true &&
      receipt.decision === "approved" &&
      receipt.decisionRemedyDigest === digest &&
      receipt.approvedRemedyDigest === digest &&
      receipt.providerExecution === true &&
      receipt.providerDispatchStarted === true &&
      receipt.executionCount === 1 &&
      receipt.permissionRevoked === true &&
      receipt.scopeClosed === true &&
      includesAll(visibleText, [
        "SDK stub",
        "Completed receipt",
        "None — no model call",
      ]) &&
      !visibleText.includes("OpenAI live") &&
      !visibleText.includes("GPT-5.6 agents"),
    "capture_completed_contract",
  );
}

export function assertDeclinedProvenance({
  pending,
  decision,
  snapshot,
  receipt,
  visibleText,
}) {
  const digest = pending?.pendingApproval?.remedyDigest;
  assertTerminalIdentity(
    pending,
    snapshot,
    receipt,
    "closed_without_action",
    "capture_declined_contract",
  );
  requireContract(
    decision?.recoveryId === pending.recoveryId &&
      decision.decision === "decline" &&
      decision.status === "closed_without_action" &&
      decision.executionStarted === false &&
      decision.decisionRemedyDigest === digest &&
      receipt.simulated === true &&
      receipt.decision === "declined" &&
      receipt.decisionRemedyDigest === digest &&
      receipt.approvedRemedyDigest === null &&
      receipt.providerExecution === false &&
      receipt.providerDispatchStarted === false &&
      receipt.executionCount === 0 &&
      receipt.exactInterruptionRejected === true &&
      receipt.permissionRevoked === true &&
      receipt.scopeClosed === true &&
      receipt.providerResult === "Provider dispatch did not begin." &&
      includesAll(visibleText, [
        "SDK stub",
        "Closed without action",
        "Provider dispatch did not begin.",
        "executionCount = 0",
      ]) &&
      !visibleText.includes("OpenAI live") &&
      !visibleText.includes("GPT-5.6 agents"),
    "capture_declined_contract",
  );
}

export function assertCaptureSafeDom(outerHtml, visibleText) {
  const candidate = `${String(outerHtml)}\n${String(visibleText)}`;
  requireContract(
    FORBIDDEN_DOM_MARKERS.every((pattern) => !pattern.test(candidate)),
    "capture_forbidden_dom_marker",
  );
}

export function parsePngDimensions(buffer) {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  requireContract(
    Buffer.isBuffer(buffer) &&
      buffer.length >= 24 &&
      buffer.subarray(0, 8).equals(signature) &&
      buffer.subarray(12, 16).toString("ascii") === "IHDR",
    "capture_png_contract",
  );
  const width = buffer.readUInt32BE(16);
  const height = buffer.readUInt32BE(20);
  requireContract(width > 0 && height > 0, "capture_png_contract");
  return { width, height };
}

function rectFullyWithinViewport(rect, viewport) {
  const values = [
    rect?.left,
    rect?.top,
    rect?.right,
    rect?.bottom,
    rect?.width,
    rect?.height,
    viewport?.width,
    viewport?.height,
  ];
  return (
    values.every((value) => typeof value === "number" && Number.isFinite(value)) &&
    viewport.width > 0 &&
    viewport.height > 0 &&
    rect.width > 0 &&
    rect.height > 0 &&
    rect.right > rect.left &&
    rect.bottom > rect.top &&
    rect.left >= -VIEWPORT_EDGE_TOLERANCE &&
    rect.top >= -VIEWPORT_EDGE_TOLERANCE &&
    rect.right <= viewport.width + VIEWPORT_EDGE_TOLERANCE &&
    rect.bottom <= viewport.height + VIEWPORT_EDGE_TOLERANCE
  );
}

export function terminalHeadingAnchorTop(viewport) {
  return viewport.width <= MOBILE_TERMINAL_MAX_WIDTH
    ? MOBILE_STICKY_TOP_BAR_HEIGHT + TERMINAL_HEADING_GAP
    : null;
}

export function assertTerminalViewportVisibility({
  viewport,
  headingRect,
  verdictRect,
  headingUnobscuredSamples,
  verdictUnobscuredSamples,
  headingAnchorTop = null,
}) {
  const unionRect = {
    left: Math.min(headingRect?.left, verdictRect?.left),
    top: Math.min(headingRect?.top, verdictRect?.top),
    right: Math.max(headingRect?.right, verdictRect?.right),
    bottom: Math.max(headingRect?.bottom, verdictRect?.bottom),
  };
  unionRect.width = unionRect.right - unionRect.left;
  unionRect.height = unionRect.bottom - unionRect.top;
  const anchored =
    headingAnchorTop === null ||
    (typeof headingAnchorTop === "number" &&
      Number.isFinite(headingAnchorTop) &&
      Math.abs(headingRect?.top - headingAnchorTop) <= 1.5);
  const samplesAreUnobscured = (samples) =>
    Array.isArray(samples) &&
    samples.length === 5 &&
    samples.every((sample) => sample === true);
  requireContract(
    rectFullyWithinViewport(headingRect, viewport) &&
      rectFullyWithinViewport(verdictRect, viewport) &&
      rectFullyWithinViewport(unionRect, viewport) &&
      samplesAreUnobscured(headingUnobscuredSamples) &&
      samplesAreUnobscured(verdictUnobscuredSamples) &&
      anchored,
    "capture_terminal_viewport",
  );
}

async function reserveLoopbackPort() {
  return await new Promise((resolvePort, rejectPort) => {
    const server = createServer();
    server.unref();
    server.once("error", () => rejectPort(new CaptureContractError("capture_port")));
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address !== null ? address.port : 0;
      server.close((error) => {
        if (error || port === 0) {
          rejectPort(new CaptureContractError("capture_port"));
          return;
        }
        resolvePort(port);
      });
    });
  });
}

async function waitForHealth(origin, ownedProcess) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (ownedProcess.exitCode !== null || ownedProcess.signalCode !== null) {
      throw new CaptureContractError("capture_server_start");
    }
    try {
      const response = await fetch(`${origin}/health`, {
        signal: AbortSignal.timeout(800),
      });
      if (response.ok) {
        const health = await response.json();
        if (
          health?.backend === "stub" &&
          health.liveReady === false &&
          health.sdkStubReady === true &&
          health.providerBoundary === "demo_adapter_only"
        ) {
          return health;
        }
      }
    } catch {
      // A bounded retry is expected while the owned process starts listening.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new CaptureContractError("capture_server_start_timeout");
}

async function waitForExit(ownedProcess, timeoutMs) {
  if (ownedProcess.exitCode !== null || ownedProcess.signalCode !== null) {
    return true;
  }
  return await new Promise((resolveExit) => {
    const timer = setTimeout(() => {
      ownedProcess.off("exit", onExit);
      resolveExit(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timer);
      resolveExit(true);
    };
    ownedProcess.once("exit", onExit);
  });
}

async function stopOwnedServer(ownedProcess) {
  if (ownedProcess === null || ownedProcess.exitCode !== null || ownedProcess.signalCode !== null) {
    return;
  }
  ownedProcess.kill("SIGTERM");
  if (await waitForExit(ownedProcess, SHUTDOWN_TIMEOUT_MS)) {
    return;
  }
  ownedProcess.kill("SIGKILL");
  await waitForExit(ownedProcess, SHUTDOWN_TIMEOUT_MS);
}

async function responseJson(response, code) {
  requireContract(response.ok(), code);
  try {
    return await response.json();
  } catch {
    throw new CaptureContractError(code);
  }
}

function submittedDecisionPayload(response, pending, action, code) {
  let submitted = null;
  try {
    submitted = response.request().postDataJSON();
  } catch {
    throw new CaptureContractError(code);
  }
  requireContract(
    submitted !== null &&
      typeof submitted === "object" &&
      !Array.isArray(submitted) &&
      typeof submitted.clientDecisionId === "string" &&
      submitted.clientDecisionId.length > 0,
    code,
  );
  const expected = createDecisionPayload(
    action,
    pending,
    submitted.clientDecisionId,
  );
  requireContract(sameJson(submitted, expected), code);
  return submitted;
}

async function browserApiJson(page, path, options = undefined) {
  return await page.evaluate(
    async ({ requestPath, requestOptions }) => {
      try {
        const response = await fetch(requestPath, {
          ...requestOptions,
          credentials: "same-origin",
          headers: requestOptions?.body
            ? { "Content-Type": "application/json" }
            : undefined,
        });
        let body = null;
        try {
          body = await response.json();
        } catch {
          body = null;
        }
        return { status: response.status, body };
      } catch {
        return { status: 0, body: null };
      }
    },
    { requestPath: path, requestOptions: options },
  );
}

async function getSessionOwnedSnapshot(page, recoveryId) {
  const result = await browserApiJson(
    page,
    `/api/recoveries/${encodeURIComponent(recoveryId)}`,
  );
  requireContract(result.status === 200, "capture_snapshot_api");
  return result.body;
}

async function getBrowserHealth(page) {
  const result = await browserApiJson(page, "/health");
  requireContract(result.status === 200, "capture_health_api");
  return result.body;
}

async function getSessionOwnedReceipt(page, recoveryId) {
  const result = await browserApiJson(
    page,
    `/api/recoveries/${encodeURIComponent(recoveryId)}/receipt`,
  );
  requireContract(result.status === 200, "capture_receipt_api");
  return result.body;
}

export async function resetSession(page) {
  const result = await browserApiJson(page, "/api/demo/reset", {
    method: "POST",
    body: "{}",
  });
  requireContract(result.status === 200, "capture_reset_status");
  assertResetResponse(result.body);
  await page.evaluate(() => sessionStorage.clear());
}

async function readSessionRecoveryId(page, responseRecoveryId) {
  const storedRecoveryId = await page.evaluate(async () => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const candidate = sessionStorage.getItem("backchannel.hotelRecovery.v1");
      if (candidate !== null) {
        return candidate;
      }
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 25));
    }
    return null;
  });
  requireContract(
    ACTIVE_HOTEL_RECOVERY_KEY === "backchannel.hotelRecovery.v1",
    "capture_session_recovery",
  );
  return assertSessionRecoveryId(storedRecoveryId, responseRecoveryId);
}

async function settleForEvidence(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolveFrames) => {
      requestAnimationFrame(() => requestAnimationFrame(resolveFrames));
    });
  });
}

export function measureTerminalViewportGeometry(element, environment = undefined) {
    const view = environment?.view ?? window;
    const documentApi = environment?.documentApi ?? document;
    const rect = element.getBoundingClientRect();
    const visibleLeft = Math.max(0, rect.left);
    const visibleTop = Math.max(0, rect.top);
    const visibleRight = Math.min(view.innerWidth, rect.right);
    const visibleBottom = Math.min(view.innerHeight, rect.bottom);
    let unobscuredSamples = [];
    if (visibleRight > visibleLeft && visibleBottom > visibleTop) {
      const inset = 1;
      const left = Math.min(visibleRight, visibleLeft + inset);
      const top = Math.min(visibleBottom, visibleTop + inset);
      const right = Math.max(visibleLeft, visibleRight - inset);
      const bottom = Math.max(visibleTop, visibleBottom - inset);
      const centerX = visibleLeft + (visibleRight - visibleLeft) / 2;
      const centerY = visibleTop + (visibleBottom - visibleTop) / 2;
      unobscuredSamples = [
        [left, top],
        [right, top],
        [centerX, centerY],
        [left, bottom],
        [right, bottom],
      ].map(([x, y]) => {
        const hit = documentApi.elementFromPoint(x, y);
        return hit !== null && (hit === element || element.contains(hit));
      });
    }
    return {
      rect: {
        left: rect.left,
        top: rect.top,
        right: rect.right,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
      },
      unobscuredSamples,
    };
}

async function viewportGeometry(locator) {
  return await locator.evaluate(measureTerminalViewportGeometry);
}

async function positionTerminalEvidence(
  page,
  inspector,
  headingName,
  viewport,
) {
  const heading = inspector.getByRole("heading", { name: headingName });
  const verdict =
    headingName === "Completed receipt"
      ? inspector.locator(".inspector-heading > p").last()
      : inspector.locator(".receipt-verdict").first();
  await heading.waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
  await verdict.waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
  const headingAnchorTop = terminalHeadingAnchorTop(viewport);
  if (headingAnchorTop === null) {
    await heading.evaluate((element) =>
      element.scrollIntoView({ block: "center", inline: "nearest" }),
    );
  } else {
    await heading.evaluate((element, anchorTop) => {
      const rect = element.getBoundingClientRect();
      window.scrollTo({
        top: Math.max(0, window.scrollY + rect.top - anchorTop),
        behavior: "auto",
      });
    }, headingAnchorTop);
  }
  const verdictBlock = headingAnchorTop === null ? "center" : "nearest";
  await verdict.evaluate(
    (element, block) =>
      element.scrollIntoView({ block, inline: "nearest" }),
    verdictBlock,
  );
  await settleForEvidence(page);
  requireContract(
    sameJson(page.viewportSize(), {
      width: viewport.width,
      height: viewport.height,
    }),
    "capture_terminal_viewport",
  );
  const [headingGeometry, verdictGeometry] = await Promise.all([
    viewportGeometry(heading),
    viewportGeometry(verdict),
  ]);
  assertTerminalViewportVisibility({
    viewport,
    headingRect: headingGeometry.rect,
    verdictRect: verdictGeometry.rect,
    headingUnobscuredSamples: headingGeometry.unobscuredSamples,
    verdictUnobscuredSamples: verdictGeometry.unobscuredSamples,
    headingAnchorTop,
  });
}

async function pageEvidence(page) {
  const [outerHtml, visibleText] = await Promise.all([
    page.locator("html").evaluate((element) => element.outerHTML),
    page.locator("body").innerText(),
  ]);
  assertCaptureSafeDom(outerHtml, visibleText);
  return { outerHtml, visibleText };
}

async function captureViewport(page, viewport, suffix, captureDirectory) {
  await settleForEvidence(page);
  await pageEvidence(page);
  const captureName = `${viewport.name}-${suffix}.png`;
  requireContract(CAPTURE_FILES.includes(captureName), "capture_filename_contract");
  const capturePath = join(captureDirectory, captureName);
  const png = await page.screenshot({
    path: capturePath,
    fullPage: false,
    animations: "disabled",
  });
  const dimensions = parsePngDimensions(png);
  requireContract(
    dimensions.width === viewport.width && dimensions.height === viewport.height,
    "capture_png_dimensions",
  );
}

async function openPendingConsent(page, viewport) {
  if (viewport.name === "mobile") {
    const review = page.getByRole("link", { name: "Review exact remedy" });
    await review.waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
    await review.click();
    await page
      .getByRole("dialog", { name: "Approve exact remedy" })
      .waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
    return;
  }
  await page
    .getByRole("complementary", { name: "Approve exact remedy" })
    .waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
}

export function isRecoveryCreationResponse(response, origin) {
  try {
    const url = new URL(response.url());
    return (
      url.origin === origin &&
      url.pathname === "/api/recoveries" &&
      response.request().method() === "POST" &&
      response.status() === 201
    );
  } catch {
    return false;
  }
}

function recoveryResponsePredicate(origin) {
  return (response) => isRecoveryCreationResponse(response, origin);
}

async function loadFreshPending(page, origin, reload = false) {
  const responsePromise = page.waitForResponse(recoveryResponsePredicate(origin), {
    timeout: UI_TIMEOUT_MS,
  });
  if (reload) {
    await page.reload({ waitUntil: "domcontentloaded" });
  } else {
    await page.goto(origin, { waitUntil: "domcontentloaded" });
  }
  const response = await responsePromise;
  const created = await responseJson(response, "capture_creation_response");
  requireContract(
    typeof created?.recoveryId === "string" && created.status === "pending_approval",
    "capture_creation_response",
  );
  await page
    .getByRole("heading", { name: "Hotel booking recovery" })
    .waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
  const recoveryId = await readSessionRecoveryId(page, created.recoveryId);
  return { created, recoveryId };
}

async function captureOneViewport(
  browser,
  origin,
  startupHealth,
  viewport,
  captureDirectory,
) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: 1,
    locale: "en-US",
    timezoneId: "UTC",
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  let externalOriginSeen = false;

  await page.route("**/*", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.origin !== origin) {
      externalOriginSeen = true;
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });

  try {
    const pendingCreation = await loadFreshPending(page, origin);
    await openPendingConsent(page, viewport);
    const browserHealth = await getBrowserHealth(page);
    requireContract(
      sameJson(browserHealth, startupHealth),
      "capture_health_api",
    );
    const pending = await getSessionOwnedSnapshot(page, pendingCreation.recoveryId);
    const pendingEvidence = await pageEvidence(page);
    assertPendingProvenance({
      health: browserHealth,
      snapshot: pending,
      visibleText: pendingEvidence.visibleText,
    });
    requireContract(!externalOriginSeen, "capture_external_origin");
    await captureViewport(page, viewport, "consent", captureDirectory);

    const approveResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname ===
          `/api/recoveries/${pending.recoveryId}/decisions`,
      { timeout: UI_TIMEOUT_MS },
    );
    await page.getByRole("button", { name: "Approve remedy" }).click();
    const approveResponse = await approveResponsePromise;
    const approveRequest = submittedDecisionPayload(
      approveResponse,
      pending,
      "approve",
      "capture_approve_response",
    );
    const approveDecision = await responseJson(
      approveResponse,
      "capture_approve_response",
    );
    requireContract(
      approveDecision.clientDecisionId === approveRequest.clientDecisionId,
      "capture_approve_response",
    );
    const completedInspector = page.getByRole("complementary", {
      name: "Completed receipt",
    });
    await completedInspector.waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
    const completedSnapshot = await getSessionOwnedSnapshot(page, pending.recoveryId);
    const completedReceipt = await getSessionOwnedReceipt(page, pending.recoveryId);
    await positionTerminalEvidence(
      page,
      completedInspector,
      "Completed receipt",
      viewport,
    );
    const completedEvidence = await pageEvidence(page);
    assertCompletedProvenance({
      pending,
      decision: approveDecision,
      snapshot: completedSnapshot,
      receipt: completedReceipt,
      visibleText: completedEvidence.visibleText,
    });
    requireContract(!externalOriginSeen, "capture_external_origin");
    await captureViewport(page, viewport, "completed", captureDirectory);

    await resetSession(page);
    const declinedPendingCreation = await loadFreshPending(page, origin, true);
    requireContract(
      declinedPendingCreation.recoveryId !== pending.recoveryId,
      "capture_reset_identity",
    );
    await openPendingConsent(page, viewport);
    const declinedPending = await getSessionOwnedSnapshot(
      page,
      declinedPendingCreation.recoveryId,
    );
    const declinedPendingEvidence = await pageEvidence(page);
    assertPendingProvenance({
      health: browserHealth,
      snapshot: declinedPending,
      visibleText: declinedPendingEvidence.visibleText,
    });

    const declineResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname ===
          `/api/recoveries/${declinedPending.recoveryId}/decisions`,
      { timeout: UI_TIMEOUT_MS },
    );
    await page.getByRole("button", { name: "Decline" }).click();
    const declineResponse = await declineResponsePromise;
    const declineRequest = submittedDecisionPayload(
      declineResponse,
      declinedPending,
      "decline",
      "capture_decline_response",
    );
    const declineDecision = await responseJson(
      declineResponse,
      "capture_decline_response",
    );
    requireContract(
      declineDecision.clientDecisionId === declineRequest.clientDecisionId,
      "capture_decline_response",
    );
    const declinedInspector = page.getByRole("complementary", {
      name: "Closed without action",
    });
    await declinedInspector.waitFor({ state: "visible", timeout: UI_TIMEOUT_MS });
    const declinedSnapshot = await getSessionOwnedSnapshot(
      page,
      declinedPending.recoveryId,
    );
    const declinedReceipt = await getSessionOwnedReceipt(
      page,
      declinedPending.recoveryId,
    );
    await positionTerminalEvidence(
      page,
      declinedInspector,
      "Closed without action",
      viewport,
    );
    const declinedEvidence = await pageEvidence(page);
    assertDeclinedProvenance({
      pending: declinedPending,
      decision: declineDecision,
      snapshot: declinedSnapshot,
      receipt: declinedReceipt,
      visibleText: declinedEvidence.visibleText,
    });
    requireContract(!externalOriginSeen, "capture_external_origin");
    await captureViewport(page, viewport, "declined", captureDirectory);

    requireContract(!externalOriginSeen, "capture_external_origin");
  } finally {
    await context.close();
  }
}

async function runCapture() {
  const captureSource = prepareCaptureSource(ROOT);
  assertFinalBuild(FINAL_BUILD_DIRECTORY);
  requireContract(
    existsSync(join(ROOT, SERVER_COMMAND[0])),
    "capture_python_missing",
  );
  const tempRoot = mkdtempSync(join(tmpdir(), "backchannel-judge-capture-"));
  let ownedProcess = null;
  let browser = null;

  try {
    const captureDirectory = join(tempRoot, "captures");
    mkdirSync(captureDirectory);
    const databasePath = join(tempRoot, "capture.sqlite3");
    const port = await reserveLoopbackPort();
    const origin = new URL("http://127.0.0.1");
    origin.port = String(port);
    const originUrl = origin.origin;
    const identityHmacSecret = randomBytes(48).toString("hex");
    const environment = createServerEnvironment({
      baseEnvironment: process.env,
      databasePath,
      frontendDistPath: FINAL_BUILD_DIRECTORY,
      identityHmacSecret,
      port,
    });
    ownedProcess = spawn(join(ROOT, SERVER_COMMAND[0]), [SERVER_COMMAND[1]], {
      cwd: ROOT,
      env: environment,
      stdio: "ignore",
    });
    ownedProcess.once("error", () => {
      // waitForHealth observes the owned process failure without printing internals.
    });
    const health = await waitForHealth(originUrl, ownedProcess);
    try {
      browser = await chromium.launch(CHROME_LAUNCH_OPTIONS);
    } catch {
      throw new CaptureContractError("capture_chrome_missing");
    }
    const browserVersion = browser.version();
    for (const viewport of VIEWPORTS) {
      await captureOneViewport(
        browser,
        originUrl,
        health,
        viewport,
        captureDirectory,
      );
    }
    for (const captureName of CAPTURE_FILES) {
      const dimensions = parsePngDimensions(
        readFileSync(join(captureDirectory, captureName)),
      );
      const viewport = VIEWPORTS.find((candidate) =>
        captureName.startsWith(`${candidate.name}-`),
      );
      requireContract(
        viewport !== undefined &&
          dimensions.width === viewport.width &&
          dimensions.height === viewport.height,
        "capture_png_dimensions",
      );
    }
    const manifest = buildCaptureManifest({
      artifactDirectory: captureDirectory,
      browserVersion,
      runtimeInput: captureSource.runtimeInput,
      sourceCommit: captureSource.sourceCommit,
    });
    installCaptureEvidence({
      captureDirectory,
      finalDirectory: FINAL_ASSET_DIRECTORY,
      manifest,
      root: ROOT,
    });
  } finally {
    try {
      if (browser !== null) {
        await browser.close();
      }
    } finally {
      try {
        await stopOwnedServer(ownedProcess);
      } finally {
        rmSync(tempRoot, { recursive: true, force: true });
      }
    }
  }
}

export function captureFailureCode(error) {
  if (error instanceof CaptureManifestError) {
    return captureManifestFailureCode(error);
  }
  if (
    error instanceof CaptureContractError &&
    PUBLIC_CAPTURE_FAILURE_CODES.has(error.message)
  ) {
    return error.message;
  }
  return "capture_unexpected";
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === MODULE_PATH) {
  runCapture().catch((error) => {
    process.stderr.write(`${captureFailureCode(error)}\n`);
    process.exitCode = 1;
  });
}
