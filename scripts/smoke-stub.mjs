import { spawnSync } from "node:child_process";

const environment = { ...process.env };
delete environment.OPENAI_API_KEY;

const result = spawnSync("uv", ["run", "python", "-m", "server.smoke_stub"], {
  cwd: process.cwd(),
  encoding: "utf8",
  env: environment,
  timeout: 20_000,
});

if (result.stdout) {
  process.stdout.write(result.stdout);
}
if (result.stderr) {
  process.stderr.write(result.stderr);
}
if (result.error) {
  const message = result.error.code === "ETIMEDOUT"
    ? "Stub smoke exceeded its 20 second timeout"
    : `Stub smoke could not start: ${result.error.message}`;
  process.stderr.write(`${message}\n`);
  process.exit(1);
}
process.exit(result.status ?? 1);
