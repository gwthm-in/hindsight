"""Parity tests for the container readiness probe.

Each case is pinned to what `curl -sf` (without -L) does for the same response,
because that is what `hindsight_probe` replaced in
`docker/standalone/start-all.sh`. The redirect cases are the point: the obvious
`urllib.request.urlopen` implementation follows redirects and fails on a 404
behind one, where curl reports success - so a healthy service that redirects
would be reported as "not ready".
"""

from __future__ import annotations

import base64
import http.server
import json
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest

from hindsight_probe import main, probe

_EXPECTED_AUTH = "Basic " + base64.b64encode(b"user:pa ss").decode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path == "/ok":
            self.send_response(204)
        elif self.path == "/redirect-to-ok":
            self.send_response(302)
            self.send_header("Location", "/ok")
        elif self.path == "/redirect-to-missing":
            self.send_response(302)
            self.send_header("Location", "/missing")
        elif self.path == "/server-error":
            self.send_response(500)
        elif self.path == "/query":
            self.send_response(204 if "expected=1" in self.path else 400)
        elif self.path.startswith("/query?"):
            self.send_response(204 if "expected=1" in self.path else 400)
        elif self.path == "/auth":
            got = self.headers.get("Authorization")
            self.send_response(204 if got == _EXPECTED_AUTH else 401)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@pytest.mark.parametrize(
    ("path", "expected", "why"),
    [
        ("/ok", True, "2xx succeeds"),
        ("/redirect-to-ok", True, "curl does not follow redirects; a 302 is a completed transfer"),
        (
            "/redirect-to-missing",
            True,
            "still a 302 to curl, which never sees the 404 behind it - urlopen would fail here",
        ),
        ("/missing", False, "curl -f fails on 4xx"),
        ("/server-error", False, "curl -f fails on 5xx"),
        ("/query?expected=1", True, "query string is preserved"),
        ("/query?expected=0", False, "query string is preserved"),
    ],
)
def test_matches_curl_sf(base_url: str, path: str, expected: bool, why: str) -> None:
    assert probe(f"{base_url}{path}", 5) is expected, why


def test_sends_url_credentials_as_basic_auth(base_url: str) -> None:
    host = base_url.removeprefix("http://")
    assert probe(f"http://user:pa%20ss@{host}/auth", 5) is True


def test_connection_refused_fails(closed_port: int) -> None:
    assert probe(f"http://127.0.0.1:{closed_port}/ok", 2) is False


def test_unsupported_scheme_fails() -> None:
    assert probe("ftp://127.0.0.1/ok", 2) is False


def test_main_exit_codes(base_url: str, closed_port: int) -> None:
    assert main([f"{base_url}/ok"]) == 0
    assert main([f"{base_url}/missing"]) == 1
    assert main([f"http://127.0.0.1:{closed_port}/ok", "2"]) == 1
    assert main([]) == 2
    assert main([f"{base_url}/ok", "not-a-number"]) == 2


# The probe must never reach into the API. Both packages install into the same
# virtualenv, so nothing at the packaging layer stops `import hindsight_api`
# here from resolving - only this test does. See hindsight_probe's docstring for
# why it matters: the loop runs once a second, and importing the API to ask
# whether the API is up risks starting the machinery it is checking for.
_IMPORT_AUDIT = """
import json, sys

before = set(sys.modules)
import hindsight_probe  # noqa: F401
new_top_level = {name.split(".")[0] for name in set(sys.modules) - before}
# _sysconfigdata_* is a platform-specific stdlib internal whose name embeds the
# build triple, so it is absent from stdlib_module_names on every platform.
print(json.dumps(sorted(
    name for name in new_top_level
    if name not in sys.stdlib_module_names
    and name != "hindsight_probe"
    and not name.startswith("_sysconfigdata")
)))
"""


def test_imports_nothing_but_the_standard_library() -> None:
    """Importing the probe must pull in no third-party module, and no API module."""
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_AUDIT],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(result.stdout) == [], (
        f"hindsight_probe must import only the standard library; it pulled in {result.stdout.strip()}"
    )


def test_runnable_as_a_module() -> None:
    """`python -m hindsight_probe` is how start-all.sh invokes it."""
    result = subprocess.run(
        [sys.executable, "-m", "hindsight_probe"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "usage: python -m hindsight_probe" in result.stderr
