import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  mkdtemp,
  mkdir,
  readFile,
  rm,
  symlink,
  truncate,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  STATIC_ARTIFACT_MANIFEST_NAME,
  STATIC_ARTIFACT_MAX_DIRECTORIES,
  STATIC_ARTIFACT_MAX_FILE_BYTES,
  STATIC_ARTIFACT_MAX_FILES,
  STATIC_ARTIFACT_MAX_PATH_BYTES,
  StaticArtifactManifestError,
  writeStaticArtifactManifest,
} from "./staticArtifactManifest.js";

const temporaryDirectories: string[] = [];

async function makeValidOutput(): Promise<string> {
  const outputDirectory = await mkdtemp(
    path.join(tmpdir(), "backchannel-static-manifest-"),
  );
  temporaryDirectories.push(outputDirectory);
  await mkdir(path.join(outputDirectory, "assets"));
  await writeFile(
    path.join(outputDirectory, "index.html"),
    [
      "<!doctype html>",
      '<script type="module" src="/assets/index-a1.js"></script>',
      '<link rel="stylesheet" href="/assets/index-b2.css">',
    ].join("\n"),
  );
  await writeFile(
    path.join(outputDirectory, "assets", "index-a1.js"),
    "console.log('ready');\n",
  );
  await writeFile(
    path.join(outputDirectory, "assets", "index-b2.css"),
    "body { color: #123; }\n",
  );
  return outputDirectory;
}

function sha256(contents: string): string {
  return createHash("sha256").update(contents).digest("hex");
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true }),
    ),
  );
});

describe("static artifact manifest generation", () => {
  it("writes the same canonical manifest for the same complete build", async () => {
    const outputDirectory = await makeValidOutput();
    const indexContents = await readFile(
      path.join(outputDirectory, "index.html"),
      "utf8",
    );
    const javascriptContents = await readFile(
      path.join(outputDirectory, "assets", "index-a1.js"),
      "utf8",
    );
    const stylesheetContents = await readFile(
      path.join(outputDirectory, "assets", "index-b2.css"),
      "utf8",
    );

    await writeStaticArtifactManifest(outputDirectory);
    const first = await readFile(
      path.join(outputDirectory, STATIC_ARTIFACT_MANIFEST_NAME),
      "utf8",
    );
    await writeStaticArtifactManifest(outputDirectory);
    const second = await readFile(
      path.join(outputDirectory, STATIC_ARTIFACT_MANIFEST_NAME),
      "utf8",
    );

    expect(second).toBe(first);
    expect(JSON.parse(first)).toEqual({
      version: 1,
      files: [
        {
          path: "assets/index-a1.js",
          size: Buffer.byteLength(javascriptContents),
          sha256: sha256(javascriptContents),
        },
        {
          path: "assets/index-b2.css",
          size: Buffer.byteLength(stylesheetContents),
          sha256: sha256(stylesheetContents),
        },
        {
          path: "index.html",
          size: Buffer.byteLength(indexContents),
          sha256: sha256(indexContents),
        },
      ],
    });
    expect(first.endsWith("\n")).toBe(true);
  });

  it.each([
    {
      name: "a root extra",
      mutate: async (outputDirectory: string) => {
        await writeFile(path.join(outputDirectory, "robots.txt"), "extra\n");
      },
    },
    {
      name: "an empty artifact",
      mutate: async (outputDirectory: string) => {
        await truncate(
          path.join(outputDirectory, "assets", "index-a1.js"),
          0,
        );
      },
    },
    {
      name: "a non-ASCII artifact path",
      mutate: async (outputDirectory: string) => {
        await writeFile(
          path.join(outputDirectory, "assets", "café.js"),
          "export {};\n",
        );
      },
    },
    {
      name: "an unsafe artifact path",
      mutate: async (outputDirectory: string) => {
        await writeFile(
          path.join(outputDirectory, "assets", "bad name.js"),
          "export {};\n",
        );
      },
    },
    {
      name: "an artifact symlink",
      mutate: async (outputDirectory: string) => {
        await rm(path.join(outputDirectory, "assets", "index-a1.js"));
        await symlink(
          path.join(outputDirectory, "index.html"),
          path.join(outputDirectory, "assets", "index-a1.js"),
        );
      },
    },
    {
      name: "a special artifact file",
      mutate: async (outputDirectory: string) => {
        execFileSync("mkfifo", [
          path.join(outputDirectory, "assets", "special.js"),
        ]);
      },
    },
    {
      name: "an oversized artifact file",
      mutate: async (outputDirectory: string) => {
        await truncate(
          path.join(outputDirectory, "assets", "index-a1.js"),
          STATIC_ARTIFACT_MAX_FILE_BYTES + 1,
        );
      },
    },
  ])("rejects $name", async ({ mutate }) => {
    const outputDirectory = await makeValidOutput();
    await mutate(outputDirectory);

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toBeInstanceOf(StaticArtifactManifestError);
  });

  it("rejects a symbolic-link output root", async () => {
    const outputDirectory = await makeValidOutput();
    const linkParent = await mkdtemp(
      path.join(tmpdir(), "backchannel-static-link-"),
    );
    temporaryDirectories.push(linkParent);
    const linkedOutput = path.join(linkParent, "dist");
    await symlink(outputDirectory, linkedOutput, "dir");

    await expect(
      writeStaticArtifactManifest(linkedOutput),
    ).rejects.toThrow("output root must be a regular directory");
  });

  it("rejects an artifact count above the bounded manifest budget", async () => {
    const outputDirectory = await makeValidOutput();
    const extraCount = STATIC_ARTIFACT_MAX_FILES;
    await Promise.all(
      Array.from({ length: extraCount }, (_, index) =>
        writeFile(
          path.join(
            outputDirectory,
            "assets",
            `lazy-${index.toString().padStart(3, "0")}.js`,
          ),
          "x",
        ),
      ),
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("too many files");
  });

  it("rejects a directory tree above the shared runtime budget", async () => {
    const outputDirectory = await makeValidOutput();
    const branches = ["a", "b", "c"];
    const depth = Math.ceil(STATIC_ARTIFACT_MAX_DIRECTORIES / branches.length);
    for (const branch of branches) {
      let branchPath = path.join(outputDirectory, "assets", branch);
      for (let index = 0; index < depth; index += 1) {
        branchPath = path.join(branchPath, "d");
      }
      await mkdir(branchPath, { recursive: true });
      await writeFile(path.join(branchPath, "leaf.js"), "x");
    }

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("too many directories");
  });

  it("rejects an artifact path above the bounded path budget", async () => {
    const outputDirectory = await makeValidOutput();
    const suffix = ".js";
    const fileName = `${"a".repeat(
      STATIC_ARTIFACT_MAX_PATH_BYTES - "assets/".length - suffix.length + 1,
    )}${suffix}`;
    await writeFile(
      path.join(outputDirectory, "assets", fileName),
      "export {};\n",
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("unsafe static artifact path");
  });

  it("rejects a total artifact size above the bounded bundle budget", async () => {
    const outputDirectory = await makeValidOutput();
    await truncate(
      path.join(outputDirectory, "assets", "index-a1.js"),
      STATIC_ARTIFACT_MAX_FILE_BYTES,
    );
    await truncate(
      path.join(outputDirectory, "assets", "index-b2.css"),
      STATIC_ARTIFACT_MAX_FILE_BYTES,
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("total size budget");
  });

  it("rejects a canonical manifest above the bounded manifest budget", async () => {
    const outputDirectory = await makeValidOutput();
    const extraCount = STATIC_ARTIFACT_MAX_FILES - 3;
    await Promise.all(
      Array.from({ length: extraCount }, (_, index) => {
        const suffix = `-${index.toString().padStart(3, "0")}.js`;
        const fileName = `${"m".repeat(230 - suffix.length)}${suffix}`;
        return writeFile(
          path.join(outputDirectory, "assets", fileName),
          "x",
        );
      }),
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("manifest exceeds its size budget");
  });

  it("rejects missing or relative JavaScript references in the index", async () => {
    const outputDirectory = await makeValidOutput();
    await writeFile(
      path.join(outputDirectory, "index.html"),
      '<script type="module" src="assets/index-a1.js"></script>',
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("must be absolute");
  });

  it("rejects index references that are not present in the manifest", async () => {
    const outputDirectory = await makeValidOutput();
    await writeFile(
      path.join(outputDirectory, "index.html"),
      '<script type="module" src="/assets/missing.js"></script>',
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("unlisted artifact");
  });

  it("rejects an index without a JavaScript artifact reference", async () => {
    const outputDirectory = await makeValidOutput();
    await writeFile(
      path.join(outputDirectory, "index.html"),
      '<link rel="stylesheet" href="/assets/index-b2.css">',
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("at least one JavaScript");
  });

  it("does not treat commented markup as a live JavaScript reference", async () => {
    const outputDirectory = await makeValidOutput();
    await writeFile(
      path.join(outputDirectory, "index.html"),
      '<!-- <script type="module" src="/assets/index-a1.js"></script> -->',
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).rejects.toThrow("at least one JavaScript");
  });

  it("ignores commented missing assets when a live entry is valid", async () => {
    const outputDirectory = await makeValidOutput();
    await writeFile(
      path.join(outputDirectory, "index.html"),
      [
        '<!-- <script src="/assets/missing.js"></script> -->',
        '<script type="module" src="/assets/index-a1.js"></script>',
      ].join("\n"),
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).resolves.toMatchObject({ version: 1 });
  });

  it("does not treat inline script text as a nested asset tag", async () => {
    const outputDirectory = await makeValidOutput();
    await writeFile(
      path.join(outputDirectory, "index.html"),
      [
        "<script>",
        '  const example = \'<script src="/assets/missing.js"></script>\';',
        "</script>",
        '<script type="module" src="/assets/index-a1.js"></script>',
      ].join("\n"),
    );

    await expect(
      writeStaticArtifactManifest(outputDirectory),
    ).resolves.toMatchObject({ version: 1 });
  });
});
