import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import {
  CAPTURE_ARTIFACT_SPECS,
  CAPTURE_MANIFEST_FILENAME,
  CaptureManifestError,
  buildCaptureManifest,
  captureManifestFailureCode,
  computeRuntimeInput,
  installCaptureEvidence,
  prepareCaptureSource,
  verifyCaptureDirectory,
} from "./capture-manifest.mjs";

const SOURCE_COMMIT = "1".repeat(40);
const BROWSER_VERSION = "138.0.7204.183";
const ROOT_PACKAGE = Object.freeze({
  name: "backchannel-fixture",
  private: true,
  workspaces: ["web"],
  scripts: {
    build: "npm --workspace web run build",
    check: "canonical check wiring may change",
    "pretest:e2e": "npm run build",
    other: "node other.mjs",
  },
  engines: { node: ">=22.13 <23" },
  dependencies: { example: "1.0.0" },
});

function png(width, height, tail = "") {
  const bytes = Buffer.alloc(24 + Buffer.byteLength(tail));
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).copy(bytes);
  bytes.write("IHDR", 12, "ascii");
  bytes.writeUInt32BE(width, 16);
  bytes.writeUInt32BE(height, 20);
  bytes.write(tail, 24);
  return bytes;
}

function writeFixtureFile(root, path, bytes = path) {
  const absolutePath = join(root, path);
  mkdirSync(dirname(absolutePath), { recursive: true });
  writeFileSync(absolutePath, bytes);
}

function git(root, args) {
  return execFileSync("git", ["-C", root, ...args], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
}

function commitFixture(root, message) {
  git(root, ["add", "--all"]);
  git(root, [
    "-c",
    "user.name=Capture Contract",
    "-c",
    "user.email=capture-contract@example.invalid",
    "commit",
    "--quiet",
    "-m",
    message,
  ]);
  return git(root, ["rev-parse", "--verify", "HEAD^{commit}"]).trim();
}

function runtimeFixture(root) {
  const paths = [
    ".node-version",
    "e2e/capture-manifest.mjs",
    "e2e/judge-flow.mjs",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "scripts/start.py",
    "server/main.py",
    "uv.lock",
    "web/index.html",
    "web/package.json",
    "web/src/App.tsx",
    "web/tsconfig.json",
    "web/vite.config.ts",
  ];
  for (const path of paths) {
    const bytes =
      path === "package.json"
        ? `${JSON.stringify(ROOT_PACKAGE, null, 2)}\n`
        : path === ".node-version"
          ? "22.13.1\n"
          : path;
    writeFixtureFile(root, path, bytes);
  }
  return paths;
}

function artifactFixture(root) {
  const directory = join(root, "artifacts");
  mkdirSync(directory);
  for (const spec of CAPTURE_ARTIFACT_SPECS) {
    writeFileSync(join(directory, spec.filename), png(spec.width, spec.height));
  }
  return directory;
}

function sourceRepository({
  exists = true,
  ancestor = true,
  runtimeInput,
} = {}) {
  return {
    assertCommitExists(commit) {
      assert.equal(commit, SOURCE_COMMIT);
      if (!exists) {
        throw new CaptureManifestError("capture_manifest_source");
      }
    },
    assertAncestor(commit) {
      assert.equal(commit, SOURCE_COMMIT);
      if (!ancestor) {
        throw new CaptureManifestError("capture_manifest_source");
      }
    },
    runtimeInputAtCommit(commit) {
      assert.equal(commit, SOURCE_COMMIT);
      return runtimeInput;
    },
  };
}

function validFixture() {
  const root = mkdtempSync(join(tmpdir(), "capture-manifest-contract-"));
  const trackedPaths = runtimeFixture(root);
  const runtimeInput = computeRuntimeInput(root, { trackedPaths });
  const directory = artifactFixture(root);
  const manifest = buildCaptureManifest({
    artifactDirectory: directory,
    browserVersion: BROWSER_VERSION,
    runtimeInput,
    sourceCommit: SOURCE_COMMIT,
  });
  writeFileSync(
    join(directory, CAPTURE_MANIFEST_FILENAME),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  const verify = (overrides = {}) =>
    verifyCaptureDirectory({
      artifactDirectory: directory,
      repository: sourceRepository({ runtimeInput }),
      root,
      runtimeInputResolver: () => runtimeInput,
      ...overrides,
    });
  return { directory, manifest, root, runtimeInput, trackedPaths, verify };
}

function withFixture(callback) {
  const fixture = validFixture();
  try {
    return callback(fixture);
  } finally {
    rmSync(fixture.root, { recursive: true, force: true });
  }
}

test("pins the exact versioned secret-safe manifest matrix", () => {
  assert.deepEqual(CAPTURE_ARTIFACT_SPECS, [
    {
      filename: "desktop-consent.png",
      width: 1440,
      height: 1024,
      scenarioId: "hotel",
      executionMode: "sdk_stub",
      state: "consent",
    },
    {
      filename: "desktop-completed.png",
      width: 1440,
      height: 1024,
      scenarioId: "hotel",
      executionMode: "sdk_stub",
      state: "completed",
    },
    {
      filename: "desktop-declined.png",
      width: 1440,
      height: 1024,
      scenarioId: "hotel",
      executionMode: "sdk_stub",
      state: "declined",
    },
    {
      filename: "mobile-consent.png",
      width: 390,
      height: 844,
      scenarioId: "hotel",
      executionMode: "sdk_stub",
      state: "consent",
    },
    {
      filename: "mobile-completed.png",
      width: 390,
      height: 844,
      scenarioId: "hotel",
      executionMode: "sdk_stub",
      state: "completed",
    },
    {
      filename: "mobile-declined.png",
      width: 390,
      height: 844,
      scenarioId: "hotel",
      executionMode: "sdk_stub",
      state: "declined",
    },
  ]);
});

test("accepts a valid manifest and exact six-file evidence set", () => {
  withFixture(({ manifest, verify }) => {
    assert.deepEqual(verify(), manifest);
    assert.deepEqual(Object.keys(manifest), [
      "schemaVersion",
      "sourceCommit",
      "runtimeInput",
      "captureProfile",
      "browser",
      "environment",
      "artifacts",
    ]);
    assert.deepEqual(manifest.browser, {
      name: "Google Chrome",
      version: BROWSER_VERSION,
      channel: "chrome",
      headless: true,
    });
    assert.deepEqual(manifest.environment, {
      locale: "en-US",
      timezone: "UTC",
      reducedMotion: "reduce",
      colorScheme: "light",
    });
  });
});

test("rejects a changed PNG even when dimensions remain valid", () => {
  withFixture(({ directory, verify }) => {
    const artifact = CAPTURE_ARTIFACT_SPECS[0];
    writeFileSync(
      join(directory, artifact.filename),
      png(artifact.width, artifact.height, "changed"),
    );
    assert.throws(verify, /capture_manifest_artifact/);
  });
});

test("rejects missing and incorrect manifest artifact hashes", () => {
  withFixture(({ directory, manifest, verify }) => {
    const missing = structuredClone(manifest);
    delete missing.artifacts[0].sha256;
    writeFileSync(
      join(directory, CAPTURE_MANIFEST_FILENAME),
      JSON.stringify(missing),
    );
    assert.throws(verify, /capture_manifest_schema/);

    const incorrect = structuredClone(manifest);
    incorrect.artifacts[0].sha256 = "f".repeat(64);
    writeFileSync(
      join(directory, CAPTURE_MANIFEST_FILENAME),
      JSON.stringify(incorrect),
    );
    assert.throws(verify, /capture_manifest_artifact/);
  });
});

test("rejects runtime input byte and path drift", () => {
  withFixture(({ root, runtimeInput, trackedPaths, verify }) => {
    writeFileSync(join(root, "server/main.py"), "changed runtime");
    assert.throws(
      () =>
        verify({
          runtimeInputResolver: () => computeRuntimeInput(root, { trackedPaths }),
        }),
      /capture_manifest_runtime/,
    );
    assert.throws(
      () =>
        verify({
          runtimeInputResolver: () => ({
            ...runtimeInput,
            paths: [...runtimeInput.paths, "server/new.py"],
          }),
        }),
      /capture_manifest_runtime/,
    );
  });
});

test("strictly rejects malformed JSON and extra manifest fields/artifacts/files", () => {
  withFixture(({ directory, manifest, verify }) => {
    writeFileSync(join(directory, CAPTURE_MANIFEST_FILENAME), "{");
    assert.throws(verify, /capture_manifest_parse/);

    writeFileSync(
      join(directory, CAPTURE_MANIFEST_FILENAME),
      JSON.stringify({ ...manifest, generatedAt: "2026-07-24T00:00:00Z" }),
    );
    assert.throws(verify, /capture_manifest_schema/);

    writeFileSync(
      join(directory, CAPTURE_MANIFEST_FILENAME),
      JSON.stringify({
        ...manifest,
        artifacts: [
          ...manifest.artifacts,
          {
            ...manifest.artifacts[0],
            filename: "extra.png",
          },
        ],
      }),
    );
    assert.throws(verify, /capture_manifest_schema/);

    writeFileSync(
      join(directory, CAPTURE_MANIFEST_FILENAME),
      JSON.stringify(manifest),
    );
    writeFileSync(join(directory, "extra.txt"), "not evidence");
    assert.throws(verify, /capture_manifest_files/);
  });
});

test("runtime digest uses exact sorted tracked inputs and rejects unsafe inputs", () => {
  const root = mkdtempSync(join(tmpdir(), "capture-runtime-contract-"));
  try {
    const trackedPaths = runtimeFixture(root);
    const left = computeRuntimeInput(root, {
      trackedPaths: [...trackedPaths].reverse(),
    });
    const right = computeRuntimeInput(root, { trackedPaths });
    assert.deepEqual(left, right);
    assert.equal(left.algorithm, "sha256");
    assert.match(left.digest, /^[0-9a-f]{64}$/);
    assert.deepEqual(left.paths, [...trackedPaths].sort());
    assert.equal(left.paths.includes(".node-version"), true);
    assert.equal(left.paths.includes("package.json"), true);

    writeFileSync(join(root, ".node-version"), "22.14.0\n");
    assert.notEqual(
      computeRuntimeInput(root, { trackedPaths }).digest,
      left.digest,
    );
    writeFileSync(join(root, ".node-version"), "22.13.1\n");

    const packageWith = (mutate) => {
      const packageValue = structuredClone(ROOT_PACKAGE);
      mutate(packageValue);
      writeFileSync(join(root, "package.json"), JSON.stringify(packageValue));
      return computeRuntimeInput(root, { trackedPaths }).digest;
    };
    assert.equal(
      packageWith((packageValue) => {
        packageValue.scripts.check = "evidence-only check wiring";
      }),
      left.digest,
    );
    for (const mutate of [
      (packageValue) => {
        packageValue.scripts.build = "changed build";
      },
      (packageValue) => {
        packageValue.scripts["pretest:e2e"] = "changed pretest";
      },
      (packageValue) => {
        packageValue.engines.node = ">=23";
      },
      (packageValue) => {
        packageValue.dependencies.example = "2.0.0";
      },
      (packageValue) => {
        packageValue.scripts.other = "changed other script";
      },
    ]) {
      assert.notEqual(packageWith(mutate), left.digest);
    }
    writeFileSync(join(root, "package.json"), "{");
    assert.throws(
      () => computeRuntimeInput(root, { trackedPaths }),
      /capture_manifest_runtime/,
    );
    writeFileSync(
      join(root, "package.json"),
      `${JSON.stringify(ROOT_PACKAGE, null, 2)}\n`,
    );

    assert.throws(
      () =>
        computeRuntimeInput(root, {
          trackedPaths: trackedPaths.filter(
            (path) => !path.startsWith("server/"),
          ),
        }),
      /capture_manifest_runtime/,
    );

    const target = join(root, "server", "target.py");
    writeFileSync(target, "target");
    const linkedPath = join(root, "server", "linked.py");
    symlinkSync(target, linkedPath);
    assert.equal(lstatSync(linkedPath).isSymbolicLink(), true);
    assert.throws(
      () =>
        computeRuntimeInput(root, {
          trackedPaths: [...trackedPaths, "server/linked.py"],
        }),
      /capture_manifest_runtime/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("requires a clean worktree and proves HEAD before hashing runtime inputs", () => {
  const cleanCalls = [];
  let runtimeCalls = 0;
  const preparedRuntimeInput = {
    algorithm: "sha256",
    digest: "a".repeat(64),
    paths: ["server/main.py"],
  };
  const clean = prepareCaptureSource("/repo", {
    git(args) {
      cleanCalls.push(args);
      if (args[0] === "status") {
        return "";
      }
      if (args[0] === "rev-parse") {
        return `${SOURCE_COMMIT}\n`;
      }
      return "";
    },
    runtimeInputResolver() {
      runtimeCalls += 1;
      return preparedRuntimeInput;
    },
    repository: sourceRepository({ runtimeInput: preparedRuntimeInput }),
  });
  assert.deepEqual(clean, {
    sourceCommit: SOURCE_COMMIT,
    runtimeInput: preparedRuntimeInput,
  });
  assert.deepEqual(cleanCalls, [
    ["status", "--porcelain=v1", "--untracked-files=all", "-z"],
    ["rev-parse", "--verify", "HEAD^{commit}"],
    ["rev-parse", "--verify", "HEAD^{commit}"],
    ["status", "--porcelain=v1", "--untracked-files=all", "-z"],
  ]);
  assert.equal(runtimeCalls, 1);

  runtimeCalls = 0;
  assert.throws(
    () =>
      prepareCaptureSource("/repo", {
        git: (args) => (args[0] === "status" ? "?? untracked" : ""),
        runtimeInputResolver() {
          runtimeCalls += 1;
        },
      }),
    /capture_manifest_worktree/,
  );
  assert.equal(runtimeCalls, 0);

  let headReads = 0;
  assert.throws(
    () =>
      prepareCaptureSource("/repo", {
        git(args) {
          if (args[0] === "status") {
            return "";
          }
          headReads += 1;
          return `${headReads === 1 ? SOURCE_COMMIT : "2".repeat(40)}\n`;
        },
        runtimeInputResolver: () => preparedRuntimeInput,
        repository: sourceRepository({ runtimeInput: preparedRuntimeInput }),
      }),
    /capture_manifest_source/,
  );

  let statusReads = 0;
  assert.throws(
    () =>
      prepareCaptureSource("/repo", {
        git(args) {
          if (args[0] === "status") {
            statusReads += 1;
            return statusReads === 1 ? "" : " M runtime";
          }
          return `${SOURCE_COMMIT}\n`;
        },
        runtimeInputResolver: () => preparedRuntimeInput,
        repository: sourceRepository({ runtimeInput: preparedRuntimeInput }),
      }),
    /capture_manifest_worktree/,
  );

  assert.throws(
    () =>
      prepareCaptureSource("/repo", {
        git: (args) =>
          args[0] === "status" ? "" : `${SOURCE_COMMIT}\n`,
        runtimeInputResolver: () => preparedRuntimeInput,
        repository: sourceRepository({
          runtimeInput: {
            ...preparedRuntimeInput,
            digest: "b".repeat(64),
          },
        }),
      }),
    /capture_manifest_runtime/,
  );
});

test("requires source commit existence and ancestry", () => {
  withFixture(({ runtimeInput, verify }) => {
    assert.throws(
      () =>
        verify({
          repository: sourceRepository({ exists: false, runtimeInput }),
        }),
      /capture_manifest_source/,
    );
    assert.throws(
      () =>
        verify({
          repository: sourceRepository({ ancestor: false, runtimeInput }),
        }),
      /capture_manifest_source/,
    );
    assert.throws(
      () =>
        verify({
          repository: sourceRepository({
            runtimeInput: { ...runtimeInput, digest: "b".repeat(64) },
          }),
        }),
      /capture_manifest_runtime/,
    );
    assert.doesNotThrow(() =>
      verify({ repository: sourceRepository({ runtimeInput }) }),
    );
  });

  const root = mkdtempSync(join(tmpdir(), "capture-commit-tree-contract-"));
  try {
    runtimeFixture(root);
    const artifactDirectory = artifactFixture(root);
    git(root, ["init", "--quiet"]);
    const sourceCommit = commitFixture(root, "initial runtime");
    const runtimeInput = computeRuntimeInput(root);
    const manifest = buildCaptureManifest({
      artifactDirectory,
      browserVersion: BROWSER_VERSION,
      runtimeInput,
      sourceCommit,
    });
    writeFileSync(
      join(artifactDirectory, CAPTURE_MANIFEST_FILENAME),
      `${JSON.stringify(manifest, null, 2)}\n`,
    );
    assert.doesNotThrow(() =>
      verifyCaptureDirectory({ artifactDirectory, root }),
    );

    const checkOnlyPackage = structuredClone(ROOT_PACKAGE);
    checkOnlyPackage.scripts.check = "new evidence-only wiring";
    writeFileSync(
      join(root, "package.json"),
      JSON.stringify(checkOnlyPackage),
    );
    commitFixture(root, "check wiring only");
    assert.doesNotThrow(() =>
      verifyCaptureDirectory({ artifactDirectory, root }),
    );

    writeFileSync(join(root, "server", "main.py"), "runtime changed");
    commitFixture(root, "runtime drift");
    assert.throws(
      () => verifyCaptureDirectory({ artifactDirectory, root }),
      /capture_manifest_runtime/,
    );

    symlinkSync(
      join(root, "server", "main.py"),
      join(root, "server", "linked.py"),
    );
    const unsafeCommit = commitFixture(root, "unsafe runtime symlink");
    writeFileSync(
      join(artifactDirectory, CAPTURE_MANIFEST_FILENAME),
      JSON.stringify({ ...manifest, sourceCommit: unsafeCommit }),
    );
    assert.throws(
      () => verifyCaptureDirectory({ artifactDirectory, root }),
      /capture_manifest_runtime/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("installs a reverified sibling stage atomically and rolls back failures", () => {
  const success = validFixture();
  try {
    const finalDirectory = join(success.root, "docs", "assets", "final");
    mkdirSync(finalDirectory, { recursive: true });
    writeFileSync(join(finalDirectory, "old.txt"), "old evidence");
    installCaptureEvidence({
      captureDirectory: success.directory,
      finalDirectory,
      manifest: success.manifest,
      repository: sourceRepository({ runtimeInput: success.runtimeInput }),
      root: success.root,
      runtimeInputResolver: () => success.runtimeInput,
    });
    assert.equal(existsSync(join(finalDirectory, "old.txt")), false);
    assert.equal(
      existsSync(join(finalDirectory, CAPTURE_MANIFEST_FILENAME)),
      true,
    );
    assert.deepEqual(
      verifyCaptureDirectory({
        artifactDirectory: finalDirectory,
        repository: sourceRepository({ runtimeInput: success.runtimeInput }),
        root: success.root,
        runtimeInputResolver: () => success.runtimeInput,
      }),
      success.manifest,
    );
  } finally {
    rmSync(success.root, { recursive: true, force: true });
  }

  const rollback = validFixture();
  try {
    const finalDirectory = join(rollback.root, "docs", "assets", "final");
    mkdirSync(finalDirectory, { recursive: true });
    writeFileSync(join(finalDirectory, "old.txt"), "old evidence");
    let renameCalls = 0;
    assert.throws(
      () =>
        installCaptureEvidence({
          captureDirectory: rollback.directory,
          finalDirectory,
          manifest: rollback.manifest,
          repository: sourceRepository({ runtimeInput: rollback.runtimeInput }),
          root: rollback.root,
          runtimeInputResolver: () => rollback.runtimeInput,
          rename(source, destination) {
            renameCalls += 1;
            if (renameCalls === 2) {
              throw new Error("simulated install failure");
            }
            renameSync(source, destination);
          },
        }),
      /capture_manifest_install/,
    );
    assert.equal(
      readFileSync(join(finalDirectory, "old.txt"), "utf8"),
      "old evidence",
    );
  } finally {
    rmSync(rollback.root, { recursive: true, force: true });
  }

  const cleanupFailure = validFixture();
  try {
    const finalDirectory = join(
      cleanupFailure.root,
      "docs",
      "assets",
      "final",
    );
    const parentDirectory = dirname(finalDirectory);
    mkdirSync(finalDirectory, { recursive: true });
    writeFileSync(join(finalDirectory, "old.txt"), "old evidence");
    installCaptureEvidence({
      captureDirectory: cleanupFailure.directory,
      finalDirectory,
      manifest: cleanupFailure.manifest,
      repository: sourceRepository({
        runtimeInput: cleanupFailure.runtimeInput,
      }),
      root: cleanupFailure.root,
      runtimeInputResolver: () => cleanupFailure.runtimeInput,
      remove(path, options) {
        if (path.endsWith(".backup")) {
          throw new Error("simulated cleanup failure");
        }
        rmSync(path, options);
      },
    });
    assert.deepEqual(
      verifyCaptureDirectory({
        artifactDirectory: finalDirectory,
        repository: sourceRepository({
          runtimeInput: cleanupFailure.runtimeInput,
        }),
        root: cleanupFailure.root,
        runtimeInputResolver: () => cleanupFailure.runtimeInput,
      }),
      cleanupFailure.manifest,
    );
    assert.equal(
      readdirSync(parentDirectory).some(
        (name) => name.startsWith(".final.stage-") && name.endsWith(".backup"),
      ),
      true,
    );
  } finally {
    rmSync(cleanupFailure.root, { recursive: true, force: true });
  }
});

test("publishes only allowlisted static manifest failure codes", () => {
  assert.equal(
    captureManifestFailureCode(
      new CaptureManifestError("capture_manifest_artifact"),
    ),
    "capture_manifest_artifact",
  );
  assert.equal(
    captureManifestFailureCode(
      new CaptureManifestError(`capture_manifest_${"secret".repeat(10)}`),
    ),
    "capture_manifest_unexpected",
  );
  assert.equal(
    captureManifestFailureCode(new Error("sensitive arbitrary detail")),
    "capture_manifest_unexpected",
  );
});
