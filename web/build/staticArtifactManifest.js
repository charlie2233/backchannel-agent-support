import { createHash } from "node:crypto";
import { constants } from "node:fs";
import {
  lstat,
  open,
  opendir,
  rename,
  unlink,
} from "node:fs/promises";
import path from "node:path";
import { TextDecoder } from "node:util";

export const STATIC_ARTIFACT_MANIFEST_NAME =
  ".backchannel-static-manifest.json";
export const STATIC_ARTIFACT_MANIFEST_MAX_BYTES = 64 * 1024;
export const STATIC_ARTIFACT_MAX_FILES = 256;
export const STATIC_ARTIFACT_MAX_DIRECTORIES = 256;
export const STATIC_ARTIFACT_MAX_TREE_ENTRIES = 512;
export const STATIC_ARTIFACT_MAX_PATH_BYTES = 255;
export const STATIC_ARTIFACT_MAX_FILE_BYTES = 16 * 1024 * 1024;
export const STATIC_ARTIFACT_MAX_TOTAL_BYTES = 32 * 1024 * 1024;

const HASH_CHUNK_BYTES = 64 * 1024;
const MANIFEST_TEMP_NAME = `${STATIC_ARTIFACT_MANIFEST_NAME}.tmp`;
const SAFE_PATH_COMPONENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export class StaticArtifactManifestError extends Error {
  constructor(message) {
    super(message);
    this.name = "StaticArtifactManifestError";
  }
}

function artifactError(message) {
  throw new StaticArtifactManifestError(message);
}

function isMissing(error) {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    error.code === "ENOENT"
  );
}

function validateRelativeArtifactPath(relativePath) {
  if (
    relativePath !== "index.html" &&
    !relativePath.startsWith("assets/")
  ) {
    artifactError(`unexpected static artifact path: ${relativePath}`);
  }
  if (
    relativePath.length === 0 ||
    Buffer.byteLength(relativePath, "ascii") !== relativePath.length ||
    relativePath.length > STATIC_ARTIFACT_MAX_PATH_BYTES ||
    relativePath.includes("\\")
  ) {
    artifactError(`unsafe static artifact path: ${relativePath}`);
  }

  const components = relativePath.split("/");
  if (
    components.some(
      (component) =>
        component.length === 0 ||
        component === "." ||
        component === ".." ||
        !SAFE_PATH_COMPONENT.test(component),
    )
  ) {
    artifactError(`unsafe static artifact path: ${relativePath}`);
  }
}

async function lstatRequired(entryPath) {
  try {
    return await lstat(entryPath);
  } catch (error) {
    if (isMissing(error)) {
      artifactError(`missing static artifact path: ${entryPath}`);
    }
    throw error;
  }
}

async function hashRegularFile(
  absolutePath,
  relativePath,
  captureContents,
) {
  validateRelativeArtifactPath(relativePath);

  const pathStats = await lstatRequired(absolutePath);
  if (pathStats.isSymbolicLink() || !pathStats.isFile()) {
    artifactError(`static artifact is not a regular file: ${relativePath}`);
  }
  if (
    pathStats.size <= 0 ||
    pathStats.size > STATIC_ARTIFACT_MAX_FILE_BYTES
  ) {
    artifactError(`static artifact has an invalid size: ${relativePath}`);
  }

  let fileHandle;
  try {
    fileHandle = await open(
      absolutePath,
      constants.O_RDONLY | constants.O_NOFOLLOW,
    );
  } catch (error) {
    artifactError(
      `static artifact could not be opened safely: ${relativePath} (${String(error)})`,
    );
  }

  try {
    const before = await fileHandle.stat();
    if (
      !before.isFile() ||
      before.size !== pathStats.size ||
      before.dev !== pathStats.dev ||
      before.ino !== pathStats.ino
    ) {
      artifactError(`static artifact changed before hashing: ${relativePath}`);
    }

    const digest = createHash("sha256");
    const chunks = [];
    const buffer = Buffer.allocUnsafe(
      Math.min(HASH_CHUNK_BYTES, before.size),
    );
    let bytesReadTotal = 0;

    while (bytesReadTotal < before.size) {
      const bytesToRead = Math.min(
        buffer.length,
        before.size - bytesReadTotal,
      );
      const { bytesRead } = await fileHandle.read(
        buffer,
        0,
        bytesToRead,
        bytesReadTotal,
      );
      if (bytesRead === 0) {
        break;
      }
      const chunk = buffer.subarray(0, bytesRead);
      digest.update(chunk);
      if (captureContents) {
        chunks.push(Buffer.from(chunk));
      }
      bytesReadTotal += bytesRead;
    }

    const after = await fileHandle.stat();
    if (
      bytesReadTotal !== before.size ||
      after.size !== before.size ||
      after.dev !== before.dev ||
      after.ino !== before.ino ||
      after.mtimeMs !== before.mtimeMs ||
      after.ctimeMs !== before.ctimeMs
    ) {
      artifactError(`static artifact changed while hashing: ${relativePath}`);
    }

    return {
      path: relativePath,
      size: before.size,
      sha256: digest.digest("hex"),
      ...(captureContents ? { contents: Buffer.concat(chunks) } : {}),
    };
  } finally {
    await fileHandle.close();
  }
}

async function collectAssetFiles(
  staticDirectory,
  directoryPath,
  relativeDirectory,
  budget,
) {
  const directoryStats = await lstatRequired(directoryPath);
  if (directoryStats.isSymbolicLink() || !directoryStats.isDirectory()) {
    artifactError(
      `static asset directory is not a regular directory: ${relativeDirectory}`,
    );
  }

  budget.directories += 1;
  if (budget.directories > STATIC_ARTIFACT_MAX_DIRECTORIES) {
    artifactError("static artifact contains too many directories");
  }

  const entries = [];
  const directory = await opendir(directoryPath);
  for await (const entry of directory) {
    budget.treeEntries += 1;
    if (budget.treeEntries > STATIC_ARTIFACT_MAX_TREE_ENTRIES) {
      artifactError("static artifact tree exceeds its entry budget");
    }
    entries.push(entry);
  }
  if (entries.length === 0) {
    artifactError(`static asset directory is empty: ${relativeDirectory}`);
  }

  const files = [];
  for (const entry of entries) {
    const relativePath = path.posix.join(relativeDirectory, entry.name);
    validateRelativeArtifactPath(relativePath);

    const absolutePath = path.join(staticDirectory, ...relativePath.split("/"));
    const entryStats = await lstatRequired(absolutePath);
    if (entryStats.isSymbolicLink()) {
      artifactError(`static artifact may not be a symlink: ${relativePath}`);
    }
    if (entryStats.isDirectory()) {
      files.push(
        ...(await collectAssetFiles(
          staticDirectory,
          absolutePath,
          relativePath,
          budget,
        )),
      );
      continue;
    }
    if (!entryStats.isFile()) {
      artifactError(`static artifact is not a regular file: ${relativePath}`);
    }
    budget.files += 1;
    if (budget.files > STATIC_ARTIFACT_MAX_FILES) {
      artifactError("static artifact contains too many files");
    }
    files.push(relativePath);
  }
  return files;
}

async function validateRootEntries(staticDirectory) {
  const rootStats = await lstatRequired(staticDirectory);
  if (rootStats.isSymbolicLink() || !rootStats.isDirectory()) {
    artifactError("static output root must be a regular directory");
  }

  const entries = [];
  const directory = await opendir(staticDirectory);
  for await (const entry of directory) {
    if (entries.length >= 3) {
      artifactError("static output root contains too many entries");
    }
    entries.push(entry);
  }
  const allowed = new Set([
    "assets",
    "index.html",
    STATIC_ARTIFACT_MANIFEST_NAME,
  ]);
  for (const entry of entries) {
    if (!allowed.has(entry.name)) {
      artifactError(`unexpected static output root entry: ${entry.name}`);
    }

    const entryPath = path.join(staticDirectory, entry.name);
    const entryStats = await lstatRequired(entryPath);
    if (entryStats.isSymbolicLink()) {
      artifactError(`static output root entry may not be a symlink: ${entry.name}`);
    }
    if (entry.name === "assets" && !entryStats.isDirectory()) {
      artifactError("static assets entry must be a directory");
    }
    if (entry.name !== "assets" && !entryStats.isFile()) {
      artifactError(`static output root entry must be a file: ${entry.name}`);
    }
  }

  for (const required of ["assets", "index.html"]) {
    if (!entries.some((entry) => entry.name === required)) {
      artifactError(`missing static output root entry: ${required}`);
    }
  }
}

function validateIndexReferences(
  indexContents,
  artifactPaths,
) {
  let indexHtml;
  try {
    indexHtml = new TextDecoder("utf-8", { fatal: true }).decode(indexContents);
  } catch {
    artifactError("static index is not valid UTF-8");
  }

  const assetReferences = new Set();
  const lowerIndexHtml = indexHtml.toLowerCase();
  let cursor = 0;
  while (cursor < indexHtml.length) {
    const tagStart = indexHtml.indexOf("<", cursor);
    if (tagStart === -1) {
      break;
    }
    if (indexHtml.startsWith("<!--", tagStart)) {
      const commentEnd = indexHtml.indexOf("-->", tagStart + 4);
      cursor = commentEnd === -1 ? indexHtml.length : commentEnd + 3;
      continue;
    }
    const firstTagCharacter = indexHtml[tagStart + 1];
    if (firstTagCharacter === undefined || !/[A-Za-z]/u.test(firstTagCharacter)) {
      cursor = tagStart + 1;
      continue;
    }

    let quote = null;
    let tagEnd = -1;
    for (let index = tagStart + 1; index < indexHtml.length; index += 1) {
      const character = indexHtml[index];
      if (quote !== null) {
        if (character === quote) {
          quote = null;
        }
      } else if (character === '"' || character === "'") {
        quote = character;
      } else if (character === ">") {
        tagEnd = index;
        break;
      } else if (character === "<") {
        break;
      }
    }
    if (tagEnd === -1) {
      cursor = tagStart + 1;
      continue;
    }

    const startTag = indexHtml.slice(tagStart, tagEnd + 1);
    const tagNameMatch = /^<([A-Za-z][A-Za-z0-9:-]*)/u.exec(startTag);
    if (tagNameMatch === null) {
      cursor = tagEnd + 1;
      continue;
    }
    const attributePattern =
      /\b(?:src|href)\s*=\s*(["'])([^"'<>]*)\1/giu;
    let match;
    while ((match = attributePattern.exec(startTag)) !== null) {
      const reference = match[2];
      if (
        reference.startsWith("assets/") ||
        reference.startsWith("./assets/")
      ) {
        artifactError(
          `static index asset reference must be absolute: ${reference}`,
        );
      }
      if (!reference.startsWith("/assets/")) {
        continue;
      }

      const relativePath = reference.slice(1);
      validateRelativeArtifactPath(relativePath);
      if (!artifactPaths.has(relativePath)) {
        artifactError(
          `static index references an unlisted artifact: ${relativePath}`,
        );
      }
      assetReferences.add(relativePath);
    }

    const tagName = tagNameMatch[1].toLowerCase();
    const isRawTextTag = tagName === "script" || tagName === "style";
    const isSelfClosing = /\/\s*>$/u.test(startTag);
    if (isRawTextTag && !isSelfClosing) {
      const closingTagStart = lowerIndexHtml.indexOf(
        `</${tagName}`,
        tagEnd + 1,
      );
      if (closingTagStart === -1) {
        cursor = indexHtml.length;
        continue;
      }
      const closingTagEnd = indexHtml.indexOf(">", closingTagStart + 2);
      cursor = closingTagEnd === -1 ? indexHtml.length : closingTagEnd + 1;
      continue;
    }
    cursor = tagEnd + 1;
  }

  if (
    ![...assetReferences].some((reference) =>
      reference.endsWith(".js"),
    )
  ) {
    artifactError("static index must reference at least one JavaScript asset");
  }
}

async function removeExistingManifest(staticDirectory) {
  const manifestPath = path.join(
    staticDirectory,
    STATIC_ARTIFACT_MANIFEST_NAME,
  );
  try {
    const manifestStats = await lstat(manifestPath);
    if (manifestStats.isSymbolicLink() || !manifestStats.isFile()) {
      artifactError("existing static artifact manifest is not a regular file");
    }
    await unlink(manifestPath);
  } catch (error) {
    if (!isMissing(error)) {
      throw error;
    }
  }
}

async function writeManifestAtomically(
  staticDirectory,
  manifestBytes,
) {
  const manifestPath = path.join(
    staticDirectory,
    STATIC_ARTIFACT_MANIFEST_NAME,
  );
  const temporaryPath = path.join(staticDirectory, MANIFEST_TEMP_NAME);

  let temporaryHandle;
  try {
    temporaryHandle = await open(
      temporaryPath,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_NOFOLLOW,
      0o644,
    );
    await temporaryHandle.writeFile(manifestBytes);
    await temporaryHandle.sync();
  } catch (error) {
    await temporaryHandle?.close();
    try {
      await unlink(temporaryPath);
    } catch (cleanupError) {
      if (!isMissing(cleanupError)) {
        throw cleanupError;
      }
    }
    throw error;
  }
  await temporaryHandle.close();

  try {
    await rename(temporaryPath, manifestPath);
  } catch (error) {
    try {
      await unlink(temporaryPath);
    } catch (cleanupError) {
      if (!isMissing(cleanupError)) {
        throw cleanupError;
      }
    }
    throw error;
  }
}

export async function writeStaticArtifactManifest(
  staticDirectory,
) {
  const outputDirectory = path.resolve(staticDirectory);
  await validateRootEntries(outputDirectory);

  const assetPaths = await collectAssetFiles(
    outputDirectory,
    path.join(outputDirectory, "assets"),
    "assets",
    {
      directories: 0,
      files: 1,
      treeEntries: 0,
    },
  );
  const artifactPaths = ["index.html", ...assetPaths].sort();
  if (artifactPaths.length > STATIC_ARTIFACT_MAX_FILES) {
    artifactError("static artifact contains too many files");
  }

  const hashedFiles = [];
  let totalBytes = 0;
  for (const relativePath of artifactPaths) {
    const hashedFile = await hashRegularFile(
      path.join(outputDirectory, ...relativePath.split("/")),
      relativePath,
      relativePath === "index.html",
    );
    totalBytes += hashedFile.size;
    if (totalBytes > STATIC_ARTIFACT_MAX_TOTAL_BYTES) {
      artifactError("static artifact exceeds the total size budget");
    }
    hashedFiles.push(hashedFile);
  }

  const indexFile = hashedFiles.find((file) => file.path === "index.html");
  if (indexFile?.contents === undefined) {
    artifactError("static index contents were not captured");
  }
  validateIndexReferences(indexFile.contents, new Set(artifactPaths));

  const manifest = {
    version: 1,
    files: hashedFiles.map(({ contents: _contents, ...file }) => file),
  };
  const manifestBytes = Buffer.from(
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  if (manifestBytes.length > STATIC_ARTIFACT_MANIFEST_MAX_BYTES) {
    artifactError("static artifact manifest exceeds its size budget");
  }

  await removeExistingManifest(outputDirectory);
  await writeManifestAtomically(outputDirectory, manifestBytes);
  return manifest;
}

export function staticArtifactManifestPlugin() {
  let resolvedConfig;

  return {
    name: "backchannel-static-artifact-manifest",
    apply: "build",
    enforce: "post",
    configResolved(config) {
      resolvedConfig = config;
    },
    async writeBundle(outputOptions) {
      if (resolvedConfig === undefined) {
        artifactError("Vite config was not resolved before writing the manifest");
      }
      const configuredOutput = outputOptions.dir ?? resolvedConfig.build.outDir;
      const outputDirectory = path.isAbsolute(configuredOutput)
        ? configuredOutput
        : path.resolve(resolvedConfig.root, configuredOutput);
      await writeStaticArtifactManifest(outputDirectory);
    },
  };
}
