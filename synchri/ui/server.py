"""The local Synchri app.

A single-page UI served from ``127.0.0.1`` by the stdlib HTTP server. The CLI
opens it in the user's browser; the signed macOS app loads the same capability
URL in its native Tauri window. The core stays dependency-free either way.

**This is the one place Synchri opens a socket**, and it is opt-in (`synchri ui`),
loopback-only, and token-gated. The broker itself still binds nothing — see
``docs/security.md``.

Security posture for a local UI, all enforced below:

* binds ``127.0.0.1`` only; ``--host`` exists but warns and requires ``--allow-remote``
* a per-launch 256-bit token is required on every request, compared in constant time
* the token travels in a header or a same-origin cookie set from the launch URL
* ``Origin``/``Host`` are checked, so a page on another site cannot drive the API
* no external requests are possible from the page: everything is inlined
"""

from __future__ import annotations

import errno
import hmac
import json
import secrets
import threading
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..broker import Broker
from ..errors import SynchriError
from ..session.manager import SessionManager
from .api import Api
from .stream import events as stream_events

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_ROOT = Path(__file__).parent / "static"

#: Requests without a valid token get this and nothing else.
_UNAUTHORIZED = {"error": {"code": "unauthorized", "message": "invalid or missing session token"}}


class SynchriUIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, api: Api, token: str, allow_remote: bool) -> None:
        super().__init__(address, handler)
        self.api = api
        self.token = token
        self.allow_remote = allow_remote


class Handler(BaseHTTPRequestHandler):
    server_version = "Synchri"
    sys_version = ""

    # -- plumbing ------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - silence default stderr spam
        pass

    def _json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        # Everything is inlined, so the page needs no external origin at all.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    # -- auth ----------------------------------------------------------

    def _authorized(self, query: dict) -> bool:
        supplied = (
            self.headers.get("X-Synchri-Token")
            or (query.get("token") or [None])[0]
            or self._cookie_token()
        )
        if not supplied:
            return False
        return hmac.compare_digest(str(supplied), self.server.token)

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "synchri_token":
                return value
        return None

    def _origin_ok(self) -> bool:
        """Reject cross-site requests: a page elsewhere must not drive this API."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # same-origin fetches and direct navigation send none
        host = self.headers.get("Host") or ""
        return urlparse(origin).netloc == host

    # -- routing -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            if not self._authorized(query):
                self._html(_LOCKED_PAGE)
                return
            # Move the token out of the URL bar into a same-site cookie so it
            # does not linger in history or get pasted around.
            body = (
                _read_static("app.html")
                .replace("__SYNCHRI_TOKEN__", self.server.token)
                .replace("__SYNCHRI_THEME__", self.server.api.appearance_theme())
            )
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header(
                "Set-Cookie",
                f"synchri_token={self.server.token}; Path=/; SameSite=Strict; HttpOnly",
            )
            self._security_headers()
            self.end_headers()
            self.wfile.write(encoded)
            return

        if not self._authorized(query) or not self._origin_ok():
            self._json(_UNAUTHORIZED, HTTPStatus.UNAUTHORIZED)
            return

        if parsed.path == "/api/stream":
            self._stream(query.get("session", [None])[0])
            return
        if parsed.path == "/api/package":
            # The one non-JSON API response: a zip download. _dispatch cannot
            # serve it, and the same-site cookie set at page load authorizes
            # plain anchor navigation here.
            self._package(query)
            return
        if parsed.path.startswith("/api/"):
            self._dispatch("GET", parsed.path[5:], query, {})
            return
        self._json({"error": {"code": "not_found", "message": parsed.path}}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query) or not self._origin_ok():
            self._json(_UNAUTHORIZED, HTTPStatus.UNAUTHORIZED)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            self._json(
                {"error": {"code": "bad_request", "message": "body must be JSON"}},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if not parsed.path.startswith("/api/"):
            self._json({"error": {"code": "not_found", "message": parsed.path}}, HTTPStatus.NOT_FOUND)
            return
        self._dispatch("POST", parsed.path[5:], query, body)

    def _package(self, query: dict) -> None:
        from ..session import package as package_module

        session_id = (query.get("session") or [None])[0] or ""
        api = self.server.api
        try:
            filename, data = package_module.build(api.broker, api.manager, session_id)
        except SynchriError as exc:
            self._json(exc.to_dict(), HTTPStatus.BAD_REQUEST)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, session_id: str | None) -> None:
        """Hold the connection open and push changes as they happen."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        # Proxies that buffer would defeat the point of streaming.
        self.send_header("X-Accel-Buffering", "no")
        self._security_headers()
        self.end_headers()
        try:
            for chunk in stream_events(self.server.api.broker.workspace, session_id):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab closed; the generator's finally still runs

    def _dispatch(self, method: str, route: str, query: dict, body: dict) -> None:
        handler = self.server.api.route(method, route)
        if handler is None:
            self._json(
                {"error": {"code": "not_found", "message": f"{method} /api/{route}"}},
                HTTPStatus.NOT_FOUND,
            )
            return
        try:
            payload = handler({k: v[0] for k, v in query.items()}, body)
        except SynchriError as exc:
            self._json(exc.to_dict(), HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:  # pragma: no cover - unexpected, still must not 500 silently
            self._json(
                {"error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._json(payload)


def _read_static(name: str) -> str:
    return (STATIC_ROOT / name).read_text(encoding="utf-8")


_LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>Synchri</title>
<style>body{font:16px/1.6 system-ui,sans-serif;max-width:34rem;margin:15vh auto;padding:0 1.5rem;
color:#1a1a1a}code{background:#f1f1f1;padding:.15rem .4rem;border-radius:4px}</style>
<h1>Synchri</h1>
<p>This page needs the launch link. Synchri prints it when you run:</p>
<p><code>synchri ui</code></p>
<p>The link carries a one-time token for this launch, so nothing else on your
machine can reach the session.</p>
"""


def create_server(
    broker: Broker,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    allow_remote: bool = False,
    default_repo: str | None = None,
) -> tuple[SynchriUIServer, str]:
    """Build the server. Returns it plus the launch URL, but does not serve."""
    if host != DEFAULT_HOST and not allow_remote:
        raise SynchriError(
            f"refusing to bind {host}: Synchri's UI is loopback-only unless you pass "
            "--allow-remote, which exposes your repositories and session control to "
            "anything that can reach this machine",
            code="refused_bind",
        )
    resolved_token = token or secrets.token_urlsafe(32)
    api = Api(broker, SessionManager(broker), default_repo=default_repo)
    try:
        server = SynchriUIServer(
            (host, port), Handler, api=api, token=resolved_token, allow_remote=allow_remote
        )
    except OSError as exc:
        # A desktop launch must not vanish just because a previous Synchri
        # window—or another local program—already owns the friendly default.
        # Fall back to an OS-selected loopback port; explicit non-default ports
        # remain deliberate and report their conflict to the caller.
        if host != DEFAULT_HOST or port != DEFAULT_PORT or exc.errno != errno.EADDRINUSE:
            raise
        server = SynchriUIServer(
            (host, 0), Handler, api=api, token=resolved_token, allow_remote=allow_remote
        )
    bound_port = server.server_address[1]
    return server, f"http://{host}:{bound_port}/?token={resolved_token}"


def serve(
    broker: Broker,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    allow_remote: bool = False,
    default_repo: str | None = None,
) -> None:
    """Run the UI until interrupted."""
    server, url = create_server(
        broker, host=host, port=port, allow_remote=allow_remote, default_repo=default_repo
    )
    # The native desktop shell consumes this capability URL from the bundled
    # engine's stdout. Flush it explicitly: a frozen sidecar writes to a pipe,
    # where Python would otherwise wait to fill its output buffer before the
    # window could open.
    print(f"Synchri is running at\n\n    {url}\n", flush=True)
    if host != DEFAULT_HOST:
        print("!! This is NOT loopback-only. Anything that can reach this host can drive it.\n")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Sessions keep their state; nothing was ended.")
    finally:
        server.shutdown()
        server.server_close()
