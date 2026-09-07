#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HINDSIGHT_START_ALL_SOURCE_ONLY=true
source "$SCRIPT_DIR/start-all.sh"
unset HINDSIGHT_START_ALL_SOURCE_ONLY

TMP_DIR="$(mktemp -d)"
HTTP_SERVER_PID=""

cleanup() {
    if [ -n "$HTTP_SERVER_PID" ]; then
        kill "$HTTP_SERVER_PID" 2>/dev/null || true
        wait "$HTTP_SERVER_PID" 2>/dev/null || true
    fi
    chmod -R u+rwx "$TMP_DIR" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

assert_contains() {
    local output="$1"
    local expected="$2"

    if [[ "$output" != *"$expected"* ]]; then
        echo "Expected output to contain: $expected"
        echo "Actual output:"
        echo "$output"
        exit 1
    fi
}

assert_not_contains() {
    local output="$1"
    local unexpected="$2"

    if [[ "$output" == *"$unexpected"* ]]; then
        echo "Expected output not to contain: $unexpected"
        echo "Actual output:"
        echo "$output"
        exit 1
    fi
}

assert_empty() {
    local output="$1"

    if [ -n "$output" ]; then
        echo "Expected no output, got:"
        echo "$output"
        exit 1
    fi
}

# =============================================================================
# http_probe
#
# The contract is `curl -sf` without -L, which this replaced. The redirect
# cases are the ones that matter: curl does not follow redirects unless asked,
# so a 302 is a completed transfer and succeeds no matter what it points at.
# A probe that followed them would report a healthy service as "not ready".
#
# The server runs until the trap kills it - it is deliberately not a
# "handle N requests" loop, because then adding a test case here would make the
# script block forever on the request the server had stopped waiting for.
# =============================================================================
HTTP_PORT_FILE="$TMP_DIR/http-port"

python3 - "$HTTP_PORT_FILE" <<'PY' &
import http.server
import pathlib
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self.send_response(204)
        elif self.path == "/redirect-to-ok":
            self.send_response(302)
            self.send_header("Location", "/ok")
        elif self.path == "/redirect-to-missing":
            self.send_response(302)
            self.send_header("Location", "/missing")
        elif self.path == "/500":
            self.send_response(500)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
pathlib.Path(sys.argv[1]).write_text(str(server.server_port), encoding="ascii")
server.serve_forever()
PY
HTTP_SERVER_PID=$!

for _ in $(seq 1 50); do
    [ -s "$HTTP_PORT_FILE" ] && break
    sleep 0.1
done
if [ ! -s "$HTTP_PORT_FILE" ]; then
    echo "HTTP probe test server did not start"
    exit 1
fi
HTTP_TEST_URL="http://127.0.0.1:$(cat "$HTTP_PORT_FILE")"

assert_probe_succeeds() {
    if ! http_probe "$HTTP_TEST_URL$1" 5 >/dev/null 2>&1; then
        echo "http_probe should succeed for $1 (curl -sf does)"
        exit 1
    fi
}

assert_probe_fails() {
    if http_probe "$HTTP_TEST_URL$1" 5 >/dev/null 2>&1; then
        echo "http_probe should fail for $1 (curl -sf does)"
        exit 1
    fi
}

assert_probe_succeeds "/ok"
assert_probe_succeeds "/redirect-to-ok"
# The regression this guards: urllib.request.urlopen follows the redirect and
# raises on the 404 behind it, where curl -sf reports success.
assert_probe_succeeds "/redirect-to-missing"
assert_probe_fails "/missing"
assert_probe_fails "/500"

# Nothing listening: a connection error fails like curl's exit 7.
CLOSED_PORT_URL="http://127.0.0.1:$(python3 -c '
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
print(port)
')/ok"
if http_probe "$CLOSED_PORT_URL" 2 >/dev/null 2>&1; then
    echo "http_probe should fail when nothing is listening"
    exit 1
fi

echo "start-all HTTP probe checks passed"

mkdir -p "$TMP_DIR/empty"
assert_empty "$(check_pg0_data_integrity "$TMP_DIR/empty")"

mkdir -p "$TMP_DIR/direct"
touch "$TMP_DIR/direct/PG_VERSION"
direct_output="$(check_pg0_data_integrity "$TMP_DIR/direct")"
assert_contains "$direct_output" "Existing pg0 data directory detected"
assert_not_contains "$direct_output" "WARNING"

mkdir -p "$TMP_DIR/legacy/instance"
touch "$TMP_DIR/legacy/instance/PG_VERSION"
legacy_output="$(check_pg0_data_integrity "$TMP_DIR/legacy")"
assert_contains "$legacy_output" "Existing pg0 data directory detected"
assert_not_contains "$legacy_output" "WARNING"

mkdir -p "$TMP_DIR/nested/instances/hindsight/data"
touch "$TMP_DIR/nested/instances/hindsight/data/PG_VERSION"
nested_output="$(check_pg0_data_integrity "$TMP_DIR/nested")"
assert_contains "$nested_output" "Existing pg0 data directory detected"
assert_not_contains "$nested_output" "WARNING"

mkdir -p "$TMP_DIR/nonempty/instances/hindsight"
touch "$TMP_DIR/nonempty/instances/hindsight/instance.json"
nonempty_output="$(check_pg0_data_integrity "$TMP_DIR/nonempty")"
assert_contains "$nonempty_output" "WARNING: pg0 data directory exists"

echo "start-all pg0 integrity checks passed"

# =============================================================================
# resolve_api_startup_wait_seconds (#3733)
# =============================================================================
assert_equals() {
    local actual="$1"
    local expected="$2"

    if [ "$actual" != "$expected" ]; then
        echo "Expected: $expected"
        echo "Actual:   $actual"
        exit 1
    fi
}

# Neither knob set: the wrapper default.
assert_equals "$(HINDSIGHT_API_STARTUP_WAIT_SECONDS= HINDSIGHT_API_MODEL_INIT_TIMEOUT= resolve_api_startup_wait_seconds)" "300"

# The documented knob raised: the wrapper waits at least that long, so raising
# it actually takes effect instead of being cut short at the default.
assert_equals "$(HINDSIGHT_API_MODEL_INIT_TIMEOUT=7200 resolve_api_startup_wait_seconds)" "7230"

# Floats are accepted — the API parses its cap as one.
assert_equals "$(HINDSIGHT_API_MODEL_INIT_TIMEOUT=7200.0 resolve_api_startup_wait_seconds)" "7230"

# A shorter cap never shortens the wrapper wait.
assert_equals "$(HINDSIGHT_API_MODEL_INIT_TIMEOUT=60 resolve_api_startup_wait_seconds)" "300"

# Garbage falls back to the default rather than breaking startup.
assert_equals "$(HINDSIGHT_API_MODEL_INIT_TIMEOUT=abc resolve_api_startup_wait_seconds)" "300"

# An explicit wrapper setting always wins.
assert_equals "$(HINDSIGHT_API_STARTUP_WAIT_SECONDS=45 HINDSIGHT_API_MODEL_INIT_TIMEOUT=7200 resolve_api_startup_wait_seconds)" "45"

echo "start-all API startup wait checks passed"

# =============================================================================
# check_pg0_writable (#1483)
# These rely on filesystem permissions, which root bypasses; skip under root.
# =============================================================================
if [ "$(id -u)" != "0" ]; then
    # Writable directory: returns 0, prints nothing, leaves no artifact behind.
    mkdir -p "$TMP_DIR/writable"
    writable_output="$(check_pg0_writable "$TMP_DIR/writable")"
    assert_empty "$writable_output"
    if [ -e "$TMP_DIR/writable/.hindsight-write-test" ]; then
        echo "check_pg0_writable left its write-test file behind"
        exit 1
    fi

    # Non-writable directory: returns 1 with actionable guidance.
    mkdir -p "$TMP_DIR/readonly"
    chmod 000 "$TMP_DIR/readonly"
    set +e
    readonly_output="$(check_pg0_writable "$TMP_DIR/readonly" 2>&1)"
    readonly_rc=$?
    set -e
    chmod 755 "$TMP_DIR/readonly"
    if [ "$readonly_rc" -eq 0 ]; then
        echo "check_pg0_writable should fail on a non-writable directory"
        exit 1
    fi
    assert_contains "$readonly_output" "not writable"
    assert_contains "$readonly_output" "hindsight-data:/home/hindsight/.pg0"
    assert_contains "$readonly_output" "--user"

    # External database configured: skip the check regardless of dir perms.
    mkdir -p "$TMP_DIR/extdb"
    chmod 000 "$TMP_DIR/extdb"
    set +e
    HINDSIGHT_API_DATABASE_URL="postgres://x" check_pg0_writable "$TMP_DIR/extdb" >/dev/null 2>&1
    extdb_rc=$?
    set -e
    chmod 755 "$TMP_DIR/extdb"
    if [ "$extdb_rc" -ne 0 ]; then
        echo "check_pg0_writable should skip when an external database is configured"
        exit 1
    fi

    echo "start-all pg0 writability checks passed"
else
    echo "⚠️  Running as root; skipping pg0 writability checks (permissions are bypassed)."
fi
