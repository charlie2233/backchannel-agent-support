import type { Plugin } from "vite";

export const STATIC_ARTIFACT_MANIFEST_NAME: string;
export const STATIC_ARTIFACT_MANIFEST_MAX_BYTES: number;
export const STATIC_ARTIFACT_MAX_FILES: number;
export const STATIC_ARTIFACT_MAX_DIRECTORIES: number;
export const STATIC_ARTIFACT_MAX_TREE_ENTRIES: number;
export const STATIC_ARTIFACT_MAX_PATH_BYTES: number;
export const STATIC_ARTIFACT_MAX_FILE_BYTES: number;
export const STATIC_ARTIFACT_MAX_TOTAL_BYTES: number;

export type StaticArtifactFile = {
  path: string;
  size: number;
  sha256: string;
};

export type StaticArtifactManifest = {
  version: 1;
  files: StaticArtifactFile[];
};

export class StaticArtifactManifestError extends Error {}

export function writeStaticArtifactManifest(
  staticDirectory: string,
): Promise<StaticArtifactManifest>;

export function staticArtifactManifestPlugin(): Plugin;
