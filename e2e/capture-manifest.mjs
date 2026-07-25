import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  copyFileSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const MODULE_PATH = fileURLToPath(import.meta.url);
const ROOT = resolve(dirname(MODULE_PATH), "..");
const FINAL_ASSET_DIRECTORY = join(ROOT, "docs", "assets", "final");
const SOURCE_COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CHROME_VERSION_PATTERN = /^\d+\.\d+\.\d+\.\d+$/;
const RUNTIME_HASH_DOMAIN = Buffer.from(
  "backchannel.capture-runtime-input.v1\0",
  "utf8",
);

const REQUIRED_RUNTIME_FILES = Object.freeze([
  ".node-version",
  "e2e/capture-manifest.mjs",
  "e2e/judge-flow.mjs",
  "package.json",
  "package-lock.json",
  "pyproject.toml",
  "scripts/start.py",
  "uv.lock",
  "web/index.html",
  "web/vite.config.ts",
]);

const MANIFEST_KEYS = Object.freeze([
  "schemaVersion",
  "sourceCommit",
  "runtimeInput",
  "captureProfile",
  "browser",
  "environment",
  "artifacts",
]);
const RUNTIME_INPUT_KEYS = Object.freeze(["algorithm", "digest", "paths"]);
const BROWSER_KEYS = Object.freeze([
  "name",
  "version",
  "channel",
  "headless",
]);
const ENVIRONMENT_KEYS = Object.freeze([
  "locale",
  "timezone",
  "reducedMotion",
  "colorScheme",
]);
const ARTIFACT_KEYS = Object.freeze([
  "filename",
  "sha256",
  "width",
  "height",
  "scenarioId",
  "executionMode",
  "state",
]);

const PUBLIC_FAILURE_CODES = new Set([
  "capture_manifest_artifact",
  "capture_manifest_files",
  "capture_manifest_install",
  "capture_manifest_parse",
  "capture_manifest_runtime",
  "capture_manifest_schema",
  "capture_manifest_source",
  "capture_manifest_worktree",
]);

export class CaptureManifestError extends Error {
  constructor(code) {
    super(code);
    this.name = "CaptureManifestError";
  }
}

export const CAPTURE_MANIFEST_FILENAME = "manifest.json";

export const CAPTURE_ARTIFACT_SPECS = Object.freeze([
  Object.freeze({
    filename: "desktop-consent.png",
    width: 1440,
    height: 1024,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    state: "consent",
  }),
  Object.freeze({
    filename: "desktop-completed.png",
    width: 1440,
    height: 1024,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    state: "completed",
  }),
  Object.freeze({
    filename: "desktop-declined.png",
    width: 1440,
    height: 1024,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    state: "declined",
  }),
  Object.freeze({
    filename: "mobile-consent.png",
    width: 390,
    height: 844,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    state: "consent",
  }),
  Object.freeze({
    filename: "mobile-completed.png",
    width: 390,
    height: 844,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    state: "completed",
  }),
  Object.freeze({
    filename: "mobile-declined.png",
    width: 390,
    height: 844,
    scenarioId: "hotel",
    executionMode: "sdk_stub",
    state: "declined",
  }),
]);

function fail(code) {
  throw new CaptureManifestError(code);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasExactKeys(value, keys) {
  return (
    isPlainObject(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function compareRuntimePaths(left, right) {
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

function uint64(value) {
  const frame = Buffer.alloc(8);
  frame.writeBigUInt64BE(BigInt(value));
  return frame;
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return value.map(canonicalJson);
  }
  if (isPlainObject(value)) {
    const canonical = Object.create(null);
    for (const key of Object.keys(value).sort()) {
      canonical[key] = canonicalJson(value[key]);
    }
    return canonical;
  }
  return value;
}

function normalizedRuntimeBytes(path, bytes) {
  if (path !== "package.json") {
    return bytes;
  }
  try {
    const source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    const packageValue = JSON.parse(source);
    if (!isPlainObject(packageValue)) {
      fail("capture_manifest_runtime");
    }
    const projected = structuredClone(packageValue);
    if (Object.hasOwn(projected, "scripts")) {
      if (!isPlainObject(projected.scripts)) {
        fail("capture_manifest_runtime");
      }
      delete projected.scripts.check;
    }
    return Buffer.from(JSON.stringify(canonicalJson(projected)), "utf8");
  } catch (error) {
    if (error instanceof CaptureManifestError) {
      throw error;
    }
    fail("capture_manifest_runtime");
  }
}

function pngDimensions(bytes) {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (
    !Buffer.isBuffer(bytes) ||
    bytes.length < 24 ||
    !bytes.subarray(0, 8).equals(signature) ||
    bytes.subarray(12, 16).toString("ascii") !== "IHDR"
  ) {
    fail("capture_manifest_artifact");
  }
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  if (width === 0 || height === 0) {
    fail("capture_manifest_artifact");
  }
  return { width, height };
}

function defaultGit(root, args, options = {}) {
  return execFileSync("git", ["-C", root, ...args], {
    encoding: options.encoding ?? "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
}

function defaultTrackedPaths(root) {
  let output;
  try {
    output = defaultGit(root, ["ls-files", "-z"], { encoding: "buffer" });
  } catch {
    fail("capture_manifest_runtime");
  }
  return output
    .toString("utf8")
    .split("\0")
    .filter((path) => path.length > 0);
}

function isRuntimePath(path) {
  return (
    typeof path === "string" &&
    (path.startsWith("server/") ||
      path.startsWith("web/src/") ||
      REQUIRED_RUNTIME_FILES.includes(path) ||
      /^web\/package(?:-[^/]+)?\.json$/.test(path) ||
      /^web\/tsconfig(?:\.[^/]+)?\.json$/.test(path))
  );
}

function validateRuntimePath(path) {
  return (
    typeof path === "string" &&
    path.length > 0 &&
    !isAbsolute(path) &&
    !path.includes("\\") &&
    path.split("/").every((part) => part !== "" && part !== "." && part !== "..")
  );
}

function assertRuntimeCoverage(paths) {
  if (
    !paths.some((path) => path.startsWith("server/")) ||
    !paths.some((path) => path.startsWith("web/src/")) ||
    !paths.includes("web/package.json") ||
    !paths.some((path) => /^web\/tsconfig(?:\.[^/]+)?\.json$/.test(path)) ||
    REQUIRED_RUNTIME_FILES.some((path) => !paths.includes(path))
  ) {
    fail("capture_manifest_runtime");
  }
}

function computeRuntimeInputFromEntries(entries) {
  if (!Array.isArray(entries)) {
    fail("capture_manifest_runtime");
  }
  const selectedEntries = entries
    .filter((entry) => isPlainObject(entry) && isRuntimePath(entry.path))
    .sort((left, right) => compareRuntimePaths(left.path, right.path));
  const paths = selectedEntries.map((entry) => entry.path);
  if (
    paths.length === 0 ||
    new Set(paths).size !== paths.length ||
    paths.some((path) => !validateRuntimePath(path))
  ) {
    fail("capture_manifest_runtime");
  }
  assertRuntimeCoverage(paths);

  const digest = createHash("sha256");
  digest.update(RUNTIME_HASH_DOMAIN);
  digest.update(uint64(paths.length));
  for (const entry of selectedEntries) {
    if (
      entry.type !== "blob" ||
      (entry.mode !== "100644" && entry.mode !== "100755") ||
      !Buffer.isBuffer(entry.bytes)
    ) {
      fail("capture_manifest_runtime");
    }
    const path = entry.path;
    const bytes = normalizedRuntimeBytes(path, entry.bytes);
    const pathBytes = Buffer.from(path, "utf8");
    digest.update(uint64(pathBytes.length));
    digest.update(pathBytes);
    digest.update(uint64(bytes.length));
    digest.update(bytes);
  }
  return {
    algorithm: "sha256",
    digest: digest.digest("hex"),
    paths,
  };
}

export function computeRuntimeInput(
  root,
  { trackedPaths = defaultTrackedPaths(root) } = {},
) {
  if (!Array.isArray(trackedPaths)) {
    fail("capture_manifest_runtime");
  }
  const runtimePaths = trackedPaths.filter(isRuntimePath);
  const entries = runtimePaths.map((path) => {
    const absolutePath = join(root, path);
    try {
      const metadata = lstatSync(absolutePath);
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        fail("capture_manifest_runtime");
      }
      return {
        path,
        mode: metadata.mode & 0o111 ? "100755" : "100644",
        type: "blob",
        bytes: readFileSync(absolutePath),
      };
    } catch (error) {
      if (error instanceof CaptureManifestError) {
        throw error;
      }
      fail("capture_manifest_runtime");
    }
  });
  return computeRuntimeInputFromEntries(entries);
}

function validateRuntimeInput(runtimeInput) {
  return (
    hasExactKeys(runtimeInput, RUNTIME_INPUT_KEYS) &&
    runtimeInput.algorithm === "sha256" &&
    typeof runtimeInput.digest === "string" &&
    SHA256_PATTERN.test(runtimeInput.digest) &&
    Array.isArray(runtimeInput.paths) &&
    runtimeInput.paths.length > 0 &&
    runtimeInput.paths.every(validateRuntimePath) &&
    sameJson(
      runtimeInput.paths,
      [...runtimeInput.paths].sort(compareRuntimePaths),
    ) &&
    new Set(runtimeInput.paths).size === runtimeInput.paths.length
  );
}

function artifactFromFile(artifactDirectory, spec) {
  const artifactPath = join(artifactDirectory, spec.filename);
  let metadata;
  let bytes;
  try {
    metadata = lstatSync(artifactPath);
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      fail("capture_manifest_artifact");
    }
    bytes = readFileSync(artifactPath);
  } catch (error) {
    if (error instanceof CaptureManifestError) {
      throw error;
    }
    fail("capture_manifest_artifact");
  }
  const dimensions = pngDimensions(bytes);
  if (dimensions.width !== spec.width || dimensions.height !== spec.height) {
    fail("capture_manifest_artifact");
  }
  return {
    filename: spec.filename,
    sha256: sha256(bytes),
    width: spec.width,
    height: spec.height,
    scenarioId: spec.scenarioId,
    executionMode: spec.executionMode,
    state: spec.state,
  };
}

export function buildCaptureManifest({
  artifactDirectory,
  browserVersion,
  runtimeInput,
  sourceCommit,
}) {
  if (
    typeof artifactDirectory !== "string" ||
    !SOURCE_COMMIT_PATTERN.test(sourceCommit) ||
    !validateRuntimeInput(runtimeInput) ||
    typeof browserVersion !== "string" ||
    !CHROME_VERSION_PATTERN.test(browserVersion)
  ) {
    fail("capture_manifest_schema");
  }
  return {
    schemaVersion: 1,
    sourceCommit,
    runtimeInput,
    captureProfile: "keyless_sdk_stub",
    browser: {
      name: "Google Chrome",
      version: browserVersion,
      channel: "chrome",
      headless: true,
    },
    environment: {
      locale: "en-US",
      timezone: "UTC",
      reducedMotion: "reduce",
      colorScheme: "light",
    },
    artifacts: CAPTURE_ARTIFACT_SPECS.map((spec) =>
      artifactFromFile(artifactDirectory, spec),
    ),
  };
}

function validateManifestSchema(manifest) {
  if (
    !hasExactKeys(manifest, MANIFEST_KEYS) ||
    manifest.schemaVersion !== 1 ||
    typeof manifest.sourceCommit !== "string" ||
    !SOURCE_COMMIT_PATTERN.test(manifest.sourceCommit) ||
    !validateRuntimeInput(manifest.runtimeInput) ||
    manifest.captureProfile !== "keyless_sdk_stub" ||
    !hasExactKeys(manifest.browser, BROWSER_KEYS) ||
    manifest.browser.name !== "Google Chrome" ||
    typeof manifest.browser.version !== "string" ||
    !CHROME_VERSION_PATTERN.test(manifest.browser.version) ||
    manifest.browser.channel !== "chrome" ||
    manifest.browser.headless !== true ||
    !hasExactKeys(manifest.environment, ENVIRONMENT_KEYS) ||
    manifest.environment.locale !== "en-US" ||
    manifest.environment.timezone !== "UTC" ||
    manifest.environment.reducedMotion !== "reduce" ||
    manifest.environment.colorScheme !== "light" ||
    !Array.isArray(manifest.artifacts) ||
    manifest.artifacts.length !== CAPTURE_ARTIFACT_SPECS.length
  ) {
    fail("capture_manifest_schema");
  }

  for (let index = 0; index < CAPTURE_ARTIFACT_SPECS.length; index += 1) {
    const artifact = manifest.artifacts[index];
    const spec = CAPTURE_ARTIFACT_SPECS[index];
    if (
      !hasExactKeys(artifact, ARTIFACT_KEYS) ||
      artifact.filename !== spec.filename ||
      typeof artifact.sha256 !== "string" ||
      !SHA256_PATTERN.test(artifact.sha256) ||
      artifact.width !== spec.width ||
      artifact.height !== spec.height ||
      artifact.scenarioId !== spec.scenarioId ||
      artifact.executionMode !== spec.executionMode ||
      artifact.state !== spec.state
    ) {
      fail("capture_manifest_schema");
    }
  }
}

function parseCommitTree(output) {
  if (!Buffer.isBuffer(output)) {
    fail("capture_manifest_runtime");
  }
  const entries = [];
  for (const record of output.subarray(0, -1).toString("binary").split("\0")) {
    const recordBytes = Buffer.from(record, "binary");
    const tabIndex = recordBytes.indexOf(9);
    if (tabIndex <= 0) {
      fail("capture_manifest_runtime");
    }
    const header = recordBytes.subarray(0, tabIndex).toString("ascii");
    const [mode, type, objectId, ...extra] = header.split(" ");
    if (
      extra.length > 0 ||
      !/^[0-9]{6}$/.test(mode) ||
      (type !== "blob" && type !== "commit") ||
      !/^[0-9a-f]{40,64}$/.test(objectId)
    ) {
      fail("capture_manifest_runtime");
    }
    let path;
    try {
      path = new TextDecoder("utf-8", { fatal: true }).decode(
        recordBytes.subarray(tabIndex + 1),
      );
    } catch {
      fail("capture_manifest_runtime");
    }
    entries.push({ mode, type, objectId, path });
  }
  return entries;
}

function runtimeInputAtGitCommit(root, commit, git = defaultGit) {
  let treeEntries;
  try {
    const output = git(root, ["ls-tree", "-r", "-z", "--full-tree", commit], {
      encoding: "buffer",
    });
    if (output.length === 0 || output.at(-1) !== 0) {
      fail("capture_manifest_runtime");
    }
    treeEntries = parseCommitTree(output).filter((entry) =>
      isRuntimePath(entry.path),
    );
    return computeRuntimeInputFromEntries(
      treeEntries.map((entry) => {
        if (
          !validateRuntimePath(entry.path) ||
          entry.type !== "blob" ||
          (entry.mode !== "100644" && entry.mode !== "100755")
        ) {
          fail("capture_manifest_runtime");
        }
        return {
          ...entry,
          bytes: git(root, ["cat-file", "blob", entry.objectId], {
            encoding: "buffer",
          }),
        };
      }),
    );
  } catch (error) {
    if (error instanceof CaptureManifestError) {
      throw error;
    }
    fail("capture_manifest_runtime");
  }
}

function defaultRepository(root) {
  return {
    assertCommitExists(commit) {
      try {
        defaultGit(root, ["cat-file", "-e", `${commit}^{commit}`]);
      } catch {
        fail("capture_manifest_source");
      }
    },
    assertAncestor(commit) {
      try {
        execFileSync("git", [
          "-C",
          root,
          "merge-base",
          "--is-ancestor",
          commit,
          "HEAD",
        ], { stdio: "ignore" });
      } catch {
        fail("capture_manifest_source");
      }
    },
    runtimeInputAtCommit(commit) {
      return runtimeInputAtGitCommit(root, commit);
    },
  };
}

function exactDirectoryEntries(artifactDirectory) {
  try {
    const metadata = lstatSync(artifactDirectory);
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      fail("capture_manifest_files");
    }
    return readdirSync(artifactDirectory).sort();
  } catch (error) {
    if (error instanceof CaptureManifestError) {
      throw error;
    }
    fail("capture_manifest_files");
  }
}

export function verifyCaptureDirectory({
  artifactDirectory,
  root = ROOT,
  repository = defaultRepository(root),
  runtimeInputResolver = computeRuntimeInput,
}) {
  const expectedFiles = [
    ...CAPTURE_ARTIFACT_SPECS.map((spec) => spec.filename),
    CAPTURE_MANIFEST_FILENAME,
  ].sort();
  if (!sameJson(exactDirectoryEntries(artifactDirectory), expectedFiles)) {
    fail("capture_manifest_files");
  }

  let manifest;
  try {
    manifest = JSON.parse(
      readFileSync(join(artifactDirectory, CAPTURE_MANIFEST_FILENAME), "utf8"),
    );
  } catch {
    fail("capture_manifest_parse");
  }
  validateManifestSchema(manifest);

  try {
    repository.assertCommitExists(manifest.sourceCommit);
    repository.assertAncestor(manifest.sourceCommit);
  } catch (error) {
    if (
      error instanceof CaptureManifestError &&
      error.message === "capture_manifest_source"
    ) {
      throw error;
    }
    fail("capture_manifest_source");
  }

  let commitRuntimeInput;
  let currentRuntimeInput;
  try {
    commitRuntimeInput = repository.runtimeInputAtCommit(
      manifest.sourceCommit,
    );
    currentRuntimeInput = runtimeInputResolver(root);
  } catch (error) {
    if (error instanceof CaptureManifestError) {
      throw error;
    }
    fail("capture_manifest_runtime");
  }
  if (
    !validateRuntimeInput(commitRuntimeInput) ||
    !validateRuntimeInput(currentRuntimeInput) ||
    !sameJson(manifest.runtimeInput, commitRuntimeInput) ||
    !sameJson(manifest.runtimeInput, currentRuntimeInput)
  ) {
    fail("capture_manifest_runtime");
  }

  for (let index = 0; index < CAPTURE_ARTIFACT_SPECS.length; index += 1) {
    const actual = artifactFromFile(
      artifactDirectory,
      CAPTURE_ARTIFACT_SPECS[index],
    );
    if (!sameJson(actual, manifest.artifacts[index])) {
      fail("capture_manifest_artifact");
    }
  }
  return manifest;
}

export function prepareCaptureSource(
  root,
  options = {},
) {
  const git =
    options.git ?? ((args, gitOptions) => defaultGit(root, args, gitOptions));
  const runtimeInputResolver =
    options.runtimeInputResolver ?? computeRuntimeInput;
  const repository = options.repository ?? defaultRepository(root);
  const readStatus = () => {
    try {
      return git([
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "-z",
      ]);
    } catch {
      fail("capture_manifest_worktree");
    }
  };
  const readHead = () => {
    try {
      const commit = String(
        git(["rev-parse", "--verify", "HEAD^{commit}"]),
      ).trim();
      if (!SOURCE_COMMIT_PATTERN.test(commit)) {
        fail("capture_manifest_source");
      }
      return commit;
    } catch (error) {
      if (error instanceof CaptureManifestError) {
        throw error;
      }
      fail("capture_manifest_source");
    }
  };

  const initialStatus = readStatus();
  if (initialStatus !== "") {
    fail("capture_manifest_worktree");
  }
  const sourceCommit = readHead();
  try {
    repository.assertCommitExists(sourceCommit);
  } catch (error) {
    if (error instanceof CaptureManifestError) {
      throw error;
    }
    fail("capture_manifest_source");
  }
  let runtimeInput;
  let commitRuntimeInput;
  try {
    runtimeInput = runtimeInputResolver(root);
    commitRuntimeInput = repository.runtimeInputAtCommit(sourceCommit);
  } catch (error) {
    if (error instanceof CaptureManifestError) {
      throw error;
    }
    fail("capture_manifest_runtime");
  }
  if (!validateRuntimeInput(runtimeInput)) {
    fail("capture_manifest_runtime");
  }
  if (
    !validateRuntimeInput(commitRuntimeInput) ||
    !sameJson(runtimeInput, commitRuntimeInput)
  ) {
    fail("capture_manifest_runtime");
  }
  const finalCommit = readHead();
  if (finalCommit !== sourceCommit) {
    fail("capture_manifest_source");
  }
  const finalStatus = readStatus();
  if (finalStatus !== "") {
    fail("capture_manifest_worktree");
  }
  return { sourceCommit, runtimeInput };
}

export function installCaptureEvidence({
  captureDirectory,
  finalDirectory,
  manifest,
  root = ROOT,
  repository = defaultRepository(root),
  runtimeInputResolver = computeRuntimeInput,
  rename = renameSync,
  remove = rmSync,
}) {
  validateManifestSchema(manifest);
  const parentDirectory = dirname(finalDirectory);
  const finalName = basename(finalDirectory);
  let stageDirectory;
  try {
    mkdirSync(parentDirectory, { recursive: true });
    stageDirectory = mkdtempSync(join(parentDirectory, `.${finalName}.stage-`));
  } catch {
    fail("capture_manifest_install");
  }
  const backupDirectory = `${stageDirectory}.backup`;
  let backupCreated = false;
  let installed = false;
  let committed = false;
  try {
    const captureFiles = exactDirectoryEntries(captureDirectory);
    const expectedCaptures = CAPTURE_ARTIFACT_SPECS.map(
      (spec) => spec.filename,
    ).sort();
    const expectedCapturesWithManifest = [
      ...expectedCaptures,
      CAPTURE_MANIFEST_FILENAME,
    ].sort();
    if (
      !sameJson(captureFiles, expectedCaptures) &&
      !sameJson(captureFiles, expectedCapturesWithManifest)
    ) {
      fail("capture_manifest_files");
    }
    for (const spec of CAPTURE_ARTIFACT_SPECS) {
      copyFileSync(
        join(captureDirectory, spec.filename),
        join(stageDirectory, spec.filename),
      );
    }
    writeFileSync(
      join(stageDirectory, CAPTURE_MANIFEST_FILENAME),
      `${JSON.stringify(manifest, null, 2)}\n`,
      { encoding: "utf8", flag: "wx" },
    );
    verifyCaptureDirectory({
      artifactDirectory: stageDirectory,
      repository,
      root,
      runtimeInputResolver,
    });

    if (existsSync(finalDirectory)) {
      rename(finalDirectory, backupDirectory);
      backupCreated = true;
    }
    rename(stageDirectory, finalDirectory);
    installed = true;
    verifyCaptureDirectory({
      artifactDirectory: finalDirectory,
      repository,
      root,
      runtimeInputResolver,
    });
    committed = true;
    if (backupCreated) {
      try {
        remove(backupDirectory, { recursive: true, force: true });
        backupCreated = false;
      } catch {
        // Installation is committed; a residual backup is safe to ignore.
      }
    }
  } catch {
    if (!committed) {
      try {
        if (installed && existsSync(finalDirectory)) {
          remove(finalDirectory, { recursive: true, force: true });
        }
        if (backupCreated && existsSync(backupDirectory)) {
          renameSync(backupDirectory, finalDirectory);
          backupCreated = false;
        }
      } catch {
        // Keep the public result static even if rollback itself cannot complete.
      }
    }
    fail("capture_manifest_install");
  } finally {
    if (existsSync(stageDirectory)) {
      try {
        remove(stageDirectory, { recursive: true, force: true });
      } catch {
        // Temporary staging cleanup is best-effort.
      }
    }
  }
}

export function captureManifestFailureCode(error) {
  if (
    error instanceof CaptureManifestError &&
    PUBLIC_FAILURE_CODES.has(error.message)
  ) {
    return error.message;
  }
  return "capture_manifest_unexpected";
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === MODULE_PATH) {
  try {
    verifyCaptureDirectory({ artifactDirectory: FINAL_ASSET_DIRECTORY });
    process.stdout.write("capture_manifest_valid\n");
  } catch (error) {
    process.stderr.write(`${captureManifestFailureCode(error)}\n`);
    process.exitCode = 1;
  }
}
