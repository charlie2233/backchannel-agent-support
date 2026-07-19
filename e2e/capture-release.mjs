async (page) => {
  const initialUrl = page.url();
  if (!initialUrl.startsWith("http://127.0.0.1:")) {
    throw new Error("Capture must begin on the loopback production app.");
  }
  const baseUrl = await page.evaluate(() => window.location.origin);
  const captureDir = "../../../docs/assets/final";

  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push("console error");
  });
  page.on("pageerror", () => browserErrors.push("page error"));

  const requireValue = (condition, message) => {
    if (!condition) throw new Error(message);
  };

  const poll = async (operation, message) => {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      const result = await operation();
      if (result) return result;
      await page.waitForTimeout(100);
    }
    throw new Error(message);
  };

  const apiJson = async (path) => {
    const response = await page.request.get(`${baseUrl}${path}`);
    requireValue(response.ok(), `API request failed for ${path}.`);
    return response.json();
  };

  const reset = async () => {
    await page.goto("about:blank");
    const response = await page.request.post(`${baseUrl}/api/demo/reset`);
    requireValue(response.ok(), "Demo reset was not enabled for the capture run.");
  };

  const startSdkRun = async () => {
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    const health = await apiJson("/health");
    requireValue(
      health.backend === "stub" &&
        health.liveReady === false &&
        health.providerBoundary === "demo_adapter_only",
      "Capture server health was not the expected keyless SDK boundary.",
    );

    const runButton = page.getByRole("button", { name: "Run SDK QA trace" });
    await runButton.waitFor({ state: "visible" });
    const responsePromise = page.waitForResponse((response) => {
      if (
        response.request().method() !== "POST" ||
        !response.url().endsWith("/api/recoveries")
      ) return false;
      return (response.request().postData() ?? "").includes('"sdk_stub"');
    });
    await runButton.click();
    const response = await responsePromise;
    requireValue(response.status() === 201, "SDK QA recovery did not start successfully.");
    const created = await response.json();
    requireValue(created.executionMode === "sdk_stub", "API provenance was not sdk_stub.");
    requireValue(created.modelIds.length === 0, "SDK QA unexpectedly claimed a model.");
    requireValue(
      typeof created.rootTraceId === "string" && created.rootTraceId.length > 0,
      "SDK QA root trace ID was missing.",
    );
    requireValue(created.status === "pending_approval", "SDK QA was not pending approval.");
    requireValue(created.pendingApproval?.executionStarted === false, "Execution began early.");

    const provenance = page.getByLabel("Runtime provenance");
    await provenance.getByText("SDK stub", { exact: true }).waitFor();
    requireValue(
      (await page.getByText("GPT-5.6 agents", { exact: true }).count()) === 0,
      "SDK UI falsely claimed GPT-5.6 agents.",
    );
    return created;
  };

  const authoritativeSnapshot = async (recoveryId, expectedStatus) => {
    return poll(async () => {
      const snapshot = await apiJson(`/api/recoveries/${recoveryId}`);
      if (snapshot.status !== expectedStatus) return null;
      requireValue(snapshot.executionMode === "sdk_stub", "Snapshot provenance drifted.");
      requireValue(snapshot.modelIds.length === 0, "Snapshot falsely claimed a model.");
      return snapshot;
    }, `Recovery did not reach ${expectedStatus}.`);
  };

  const authoritativeReceipt = async (recoveryId, expectedStatus) => {
    return poll(async () => {
      const response = await page.request.get(`${baseUrl}/api/recoveries/${recoveryId}/receipt`);
      if (response.status() === 404) return null;
      requireValue(response.ok(), "Receipt API failed.");
      const receipt = await response.json();
      if (receipt.status !== expectedStatus) return null;
      requireValue(receipt.executionMode === "sdk_stub", "Receipt provenance drifted.");
      requireValue(receipt.modelCall === false, "SDK receipt falsely claimed a model call.");
      requireValue(receipt.modelIds.length === 0, "SDK receipt falsely named a model.");
      return receipt;
    }, `Receipt did not reach ${expectedStatus}.`);
  };

  const openEvidenceOnMobile = async () => {
    if ((page.viewportSize()?.width ?? 0) >= 760) return;
    const trigger = page.getByRole("button", { name: /Review (exact remedy|recovery receipt)/ });
    await trigger.click();
    await page.getByRole("dialog").waitFor({ state: "visible" });
  };

  const capture = async (filename, expectedSize) => {
    const viewport = page.viewportSize();
    requireValue(
      viewport?.width === expectedSize.width && viewport?.height === expectedSize.height,
      `${filename} viewport drifted.`,
    );
    await page.evaluate(() => {
      window.scrollTo(0, 0);
      document.documentElement.scrollTo(0, 0);
      document.body.scrollTo(0, 0);
      for (const element of document.querySelectorAll(
        ".workspace, .evidence-inspector, .consent-sheet, .consent-sheet-body, .event-table-wrap",
      )) {
        element.scrollTo(0, 0);
      }
    });
    await page.waitForTimeout(50);
    await page.screenshot({
      path: `${captureDir}/${filename}`,
      animations: "disabled",
      fullPage: false,
    });
  };

  const verifyPending = async () => {
    await reset();
    const snapshot = await startSdkRun();
    await authoritativeSnapshot(snapshot.recoveryId, "pending_approval");
    await openEvidenceOnMobile();
    await page.getByRole("heading", { name: "Approve exact remedy" }).waitFor();
    await page.getByText("Execution has not begun.", { exact: true }).waitFor();
    return snapshot;
  };

  const verifyCompleted = async () => {
    const snapshot = await verifyPending();
    await page.getByRole("button", { name: "Approve remedy" }).click();
    await authoritativeSnapshot(snapshot.recoveryId, "completed");
    const receipt = await authoritativeReceipt(snapshot.recoveryId, "completed");
    requireValue(receipt.providerExecution === true, "Completed receipt omitted execution.");
    requireValue(receipt.approvalCount === 1, "Completed receipt approval count drifted.");
    requireValue(
      typeof receipt.approvedRemedyDigest === "string" &&
        receipt.approvedRemedyDigest.startsWith("sha256:"),
      "Completed receipt omitted the approved digest.",
    );
    for (const proof of [
      "Immediate pre-execution remedy digest matched the approved digest.",
      "Temporary provider-dispatch permission revoked after the approved execution.",
    ]) {
      requireValue(receipt.verificationResults.includes(proof), "Completed proof drifted.");
    }
    await page.getByRole("heading", { name: "Sealed receipt" }).waitFor();
    await page.getByText("Matched immediately before execution", { exact: true }).waitFor();
    await page.getByText("Revoked", { exact: true }).waitFor();
  };

  const verifyDeclined = async () => {
    const snapshot = await verifyPending();
    await page.getByRole("button", { name: "Decline" }).click();
    await authoritativeSnapshot(snapshot.recoveryId, "closed_without_action");
    const receipt = await authoritativeReceipt(snapshot.recoveryId, "closed_without_action");
    requireValue(receipt.providerExecution === false, "Decline receipt claimed execution.");
    requireValue(receipt.approvalCount === 0, "Decline receipt claimed approval.");
    requireValue(receipt.approvedRemedyDigest === null, "Decline receipt claimed a digest.");
    for (const proof of [
      "Execution count is zero.",
      "Provider dispatch did not begin.",
      "Temporary permission revoked.",
      "Cancellation receipt sealed.",
    ]) {
      requireValue(receipt.verificationResults.includes(proof), "Decline proof drifted.");
    }
    await page.getByRole("heading", { level: 3, name: "Closed without action" }).waitFor();
    await page.getByText("executionCount = 0", { exact: true }).waitFor();
    await page
      .locator(".verification-results")
      .getByText("Provider dispatch did not begin.", { exact: true })
      .waitFor();
  };

  const viewports = [
    { label: "desktop", width: 1440, height: 1024 },
    { label: "mobile", width: 390, height: 844 },
  ];
  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await verifyPending();
    await capture(`pending-${viewport.label}.png`, viewport);
    await verifyCompleted();
    await capture(`completed-${viewport.label}.png`, viewport);
    await verifyDeclined();
    await capture(`declined-${viewport.label}.png`, viewport);
  }

  requireValue(browserErrors.length === 0, "Browser console or page errors were observed.");
}
