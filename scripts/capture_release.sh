#!/usr/bin/env bash
set -euo pipefail

release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release_output="$release_root/output/playwright/release"
release_captures="$release_root/docs/assets/final"
release_config="$release_root/e2e/playwright-cli.config.json"
release_tmp_parent="${TMPDIR:-/tmp}"
release_tmp="$(mktemp -d "${release_tmp_parent%/}/backchannel-release.XXXXXX")"
release_uv_cache="${UV_CACHE_DIR:-${release_tmp_parent%/}/backchannel-uv-cache}"
release_no_global_config="$release_tmp/global-config-disabled"
release_path="${PATH:?PATH is required}"
release_home="${HOME:?HOME is required}"
release_port=""
release_pid=""
release_session="backchannel-release-$$"
release_browser_opened="false"

run_release_cli() {
  env -i \
    PATH="$release_path" \
    HOME="$release_home" \
    TMPDIR="$release_tmp_parent" \
    CI=1 \
    NO_UPDATE_NOTIFIER=1 \
    PWTEST_CLI_GLOBAL_CONFIG="$release_no_global_config" \
    npx --no-install --prefix "$release_root" playwright-cli "$@"
}

cleanup_release_capture() {
  if [[ "$release_browser_opened" == "true" ]]; then
    (
      cd "$release_output"
      run_release_cli --session "$release_session" close >/dev/null 2>&1 || true
    )
  fi
  if [[ -n "$release_pid" ]] && kill -0 "$release_pid" >/dev/null 2>&1; then
    kill "$release_pid" >/dev/null 2>&1 || true
    wait "$release_pid" >/dev/null 2>&1 || true
  fi
  case "$release_tmp" in
    */backchannel-release.*) rm -rf -- "$release_tmp" ;;
  esac
}
trap cleanup_release_capture EXIT INT TERM

if ! command -v npx >/dev/null 2>&1; then
  echo "npx is required for Playwright CLI capture." >&2
  exit 1
fi
if [[ ! -x "$release_root/node_modules/.bin/playwright-cli" ]]; then
  echo "Run npm ci to install the repository-locked Playwright CLI." >&2
  exit 1
fi
if [[ ! -f "$release_config" ]]; then
  echo "Repository Playwright CLI configuration is unavailable." >&2
  exit 1
fi

case "$(uname -s)" in
  Darwin)
    if [[ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
      echo "Google Chrome is required at the standard macOS application path." >&2
      exit 1
    fi
    ;;
  Linux)
    if ! command -v google-chrome >/dev/null 2>&1 \
      && ! command -v google-chrome-stable >/dev/null 2>&1; then
      echo "Google Chrome is required on PATH for Linux capture." >&2
      exit 1
    fi
    ;;
  *)
    echo "Release capture supports macOS and Linux with Google Chrome." >&2
    exit 1
    ;;
esac

cd "$release_root"
npm run build
mkdir -p "$release_output" "$release_captures"
release_port="$(env -i \
  PATH="$release_path" \
  HOME="$release_home" \
  TMPDIR="$release_tmp_parent" \
  UV_CACHE_DIR="$release_uv_cache" \
  uv run python -c 'import socket; listener=socket.socket(); listener.bind(("127.0.0.1", 0)); print(listener.getsockname()[1]); listener.close()')"

# sanitized-server-env:start
env -i \
  PATH="$release_path" \
  HOME="$release_home" \
  TMPDIR="$release_tmp_parent" \
  UV_CACHE_DIR="$release_uv_cache" \
  BACKCHANNEL_DB_PATH="$release_tmp/release.sqlite3" \
  BACKCHANNEL_DEMO_RESET_ENABLED=true \
  BACKCHANNEL_DEPLOYED_MODE=false \
  PORT="$release_port" \
  uv run python scripts/start.py \
  >"$release_output/server.stdout.log" \
  2>"$release_output/server.stderr.log" &
# sanitized-server-env:end
release_pid="$!"

BACKCHANNEL_E2E_URL="http://127.0.0.1:$release_port" \
  UV_CACHE_DIR="$release_uv_cache" uv run python - <<'PY'
import os
import time
import urllib.error
import urllib.request

url = os.environ["BACKCHANNEL_E2E_URL"] + "/readyz"
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError):
        time.sleep(0.1)
else:
    raise SystemExit("Production capture server did not become ready.")
PY

cd "$release_output"
run_release_cli --session "$release_session" open \
  "http://127.0.0.1:$release_port" \
  --browser chrome \
  --config "$release_config"
release_browser_opened="true"
run_release_cli --session "$release_session" run-code --filename \
  "$release_root/e2e/capture-release.mjs"

cd "$release_root"
UV_CACHE_DIR="$release_uv_cache" \
  uv run pytest -q \
    tests/release/test_release_evidence.py::test_final_build_captures_exist_at_exact_viewports
echo "Release capture passed: six final-build screenshots have exact dimensions."
