import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";

const environment = { ...process.env };
delete environment.OPENAI_API_KEY;
environment.UV_CACHE_DIR ??= join(tmpdir(), "backchannel-smoke-uv-cache");
environment.UV_NO_SYNC ??= "1";
environment.PYTHONWARNINGS = [
  environment.PYTHONWARNINGS,
  "ignore:Using `httpx` with `starlette.testclient` is deprecated",
].filter(Boolean).join(",");

const result = spawnSync("uv", ["run", "python", "-m", "server.smoke_stub"], {
  cwd: process.cwd(),
  encoding: "utf8",
  env: environment,
  timeout: 60_000,
});

if (result.stdout) {
  process.stdout.write(result.stdout);
}
if (result.stderr) {
  process.stderr.write(result.stderr);
}
if (result.error) {
  const message = result.error.code === "ETIMEDOUT"
    ? "Stub smoke exceeded its 60 second timeout"
    : `Stub smoke could not start: ${result.error.message}`;
  process.stderr.write(`${message}\n`);
  process.exit(1);
}
process.exit(result.status ?? 1);
