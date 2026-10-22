"""The trusted recording relay (spec D6, LOG-11, ISO-06, BUD-05).

The relay is the participant's only network channel. For every request it:

1. identifies the invocation by the per-invocation token the CLI presents in
   place of a provider credential;
2. records the exact request bytes (``relay.request_received``) **before**
   deciding anything — a request that cannot be recorded is not forwarded and
   the relay halts (LOG-03);
3. enforces the grant (model, tool definitions, injected-context markers,
   route, request count, expiry) and refuses violations
   (``relay.request_rejected``), never forwarding them;
4. forwards to the provider with the client's authorization stripped and the
   dedicated credential injected — the credential never leaves this process;
5. streams the response back while capturing it, then records the exact
   response bytes, status, timing, served model, and usage
   (``relay.upstream_response``).

The relay listens on a Unix socket (the channel a container receives) or, for
tests and diagnostics, on loopback TCP. Upstreams must be HTTPS; plain HTTP is
accepted only when the relay is constructed for mock-provider tests.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from ..ids import new_secret
from .canonical import sha256_hex
from .credentials import CredentialVault
from .errors import RelayError, StoreWriteError
from .policy import (
    DEFAULT_FORBIDDEN_MARKERS,
    PROFILES,
    Grant,
    inspect_request,
    new_token,
    token_matches,
)
from .store import RunStore

RELAY_VERSION = "1"
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
UPSTREAM_TIMEOUT_S = 900.0
MAX_SOCKET_PATH = 100  # AF_UNIX sun_path is 108 bytes on Linux

#: Never recorded verbatim, never forwarded from the client.
_SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization"}
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding",
    "upgrade", "host", "content-length", "accept-encoding",
}


def redact_headers(items) -> dict[str, str]:
    return {
        name: ("<redacted>" if name.lower() in _SECRET_HEADERS else value)
        for name, value in items
    }


@dataclass(frozen=True)
class Upstream:
    base_url: str
    ca_bundle: str | None = None
    #: An HTTP CONNECT proxy ("host:port") the relay itself must use, if any.
    proxy: str | None = None

    def split(self):
        parts = urlsplit(self.base_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.scheme, parts.hostname, port, parts.path.rstrip("/")


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class RecordingRelay:
    def __init__(
        self,
        store: RunStore,
        vault: CredentialVault,
        upstreams: dict[str, Upstream],
        *,
        socket_path: str | os.PathLike | None = None,
        tcp_port: int | None = None,
        control_socket_path: str | os.PathLike | None = None,
        allow_insecure_upstream: bool = False,
        upstream_timeout_s: float = UPSTREAM_TIMEOUT_S,
    ) -> None:
        if (socket_path is None) == (tcp_port is None):
            raise RelayError("listen on exactly one of a Unix socket or a loopback TCP port")
        for provider, upstream in upstreams.items():
            if provider not in PROFILES:
                raise RelayError(f"unknown provider {provider!r}")
            scheme = urlsplit(upstream.base_url).scheme
            if scheme != "https" and not (allow_insecure_upstream and scheme == "http"):
                raise RelayError(
                    f"the {provider} upstream must be https (got {upstream.base_url!r})"
                )
            vault.get(provider)  # fail now, not on the first request
        self.store = store
        self.vault = vault
        self.upstreams = dict(upstreams)
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.control_socket_path = Path(control_socket_path) if control_socket_path else None
        self._control = None
        self.tcp_port = tcp_port
        self.upstream_timeout_s = upstream_timeout_s
        self.halted: str | None = None
        self._grants: dict[str, Grant] = {}
        self._grants_lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> "RecordingRelay":
        handler = _make_handler(self)
        if self.socket_path is not None:
            if len(os.fsencode(self.socket_path)) > MAX_SOCKET_PATH:
                raise RelayError(
                    f"the relay socket path is too long for AF_UNIX ({self.socket_path}); "
                    "use a short directory such as /tmp/srr-<id>/"
                )
            self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.socket_path.exists():
                raise RelayError(f"refusing to replace an existing socket at {self.socket_path}")
            self._server = _UnixServer(str(self.socket_path), handler)
            os.chmod(self.socket_path, 0o666)  # the container's non-root UID must connect
            listen = {"kind": "unix", "path_name": self.socket_path.name}
        else:
            self._server = _TCPServer(("127.0.0.1", self.tcp_port), handler)
            self.tcp_port = self._server.server_address[1]
            listen = {"kind": "tcp", "host": "127.0.0.1", "port": self.tcp_port}
        self.store.append(
            "relay.started",
            {
                "relay_version": RELAY_VERSION,
                "listen": listen,
                "providers": {name: up.base_url for name, up in sorted(self.upstreams.items())},
                "credential_fingerprints": {
                    name: fp for name, fp in self.vault.fingerprints().items()
                    if name in self.upstreams
                },
            },
            actor="relay",
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if self.control_socket_path is not None:
            if len(os.fsencode(self.control_socket_path)) > MAX_SOCKET_PATH:
                raise RelayError(f"the control socket path is too long: {self.control_socket_path}")
            if self.control_socket_path.exists():
                raise RelayError(f"refusing to replace an existing socket at {self.control_socket_path}")
            self.control_socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            old_umask = os.umask(0o177)
            try:
                self._control = _UnixServer(str(self.control_socket_path), _make_control_handler(self))
            finally:
                os.umask(old_umask)
            os.chmod(self.control_socket_path, 0o600)  # the orchestrator's user only
            threading.Thread(target=self._control.serve_forever, daemon=True).start()
        return self

    def stop(self, reason: str = "stopped") -> None:
        if self._control is not None:
            self._control.shutdown()
            self._control.server_close()
            self._control = None
            if self.control_socket_path.exists():
                self.control_socket_path.unlink()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self.socket_path is not None and self.socket_path.exists():
            self.socket_path.unlink()
        if self.halted is None and self.store.failed is None:
            self.store.append("relay.halted", {"reason": reason, "detail": ""}, actor="relay")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def base_url(self, provider: str) -> str:
        """The URL a CLI on the relay's own host would use (tests, diagnostics)."""
        if self.tcp_port is None:
            raise RelayError("this relay listens on a Unix socket")
        return f"http://127.0.0.1:{self.tcp_port}/{provider}"

    def halt(self, reason: str, detail: str) -> None:
        """Stop forwarding. Recorded if the store still accepts writes."""
        if self.halted is not None:
            return
        self.halted = f"{reason}: {detail}"
        if self.store.failed is None:
            try:
                self.store.append("relay.halted", {"reason": reason, "detail": detail}, actor="relay")
            except StoreWriteError as exc:
                # The store failed too. Forwarding is already blocked by
                # ``self.halted``; keep the reason the halt went unrecorded.
                self.halted += f" (halt not recorded: {exc.message})"

    # -- grants ---------------------------------------------------------

    def issue_grant(
        self,
        *,
        invocation_id: str,
        provider: str,
        model: str,
        allowed_tools=(),
        max_requests: int = 1,
        forbidden_markers=None,
        max_tokens: int | None = None,
        ttl_s: float = 1800.0,
    ) -> tuple[Grant, str]:
        """Authorize one invocation. Returns the grant and its token (shown once)."""
        if provider not in self.upstreams:
            raise RelayError(f"this relay has no {provider} upstream")
        if self.halted is not None:
            raise RelayError(f"the relay is halted ({self.halted})")
        if max_requests < 1:
            raise RelayError("a grant must allow at least one request")
        token = new_token()
        grant = Grant(
            grant_id=f"grant_{new_secret(9)}",
            invocation_id=invocation_id,
            provider=provider,
            model=model,
            allowed_tools=frozenset(allowed_tools),
            max_requests=max_requests,
            forbidden_markers=tuple(
                DEFAULT_FORBIDDEN_MARKERS[provider] if forbidden_markers is None
                else forbidden_markers
            ),
            expires_monotonic=time.monotonic() + ttl_s,
            expires_at=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl_s)
            ),
            token_sha256=sha256_hex(token.encode("utf-8")),
            max_tokens=max_tokens,
        )
        self.store.append(
            "relay.grant_issued",
            {
                "grant_id": grant.grant_id,
                "invocation_id": invocation_id,
                "provider": provider,
                "model": model,
                "allowed_tools": sorted(grant.allowed_tools),
                "max_requests": max_requests,
                "policy_sha256": grant.policy_sha256(),
                "token_sha256": grant.token_sha256,
                "expires_at": grant.expires_at,
            },
            actor="relay",
            invocation_id=invocation_id,
        )
        with self._grants_lock:
            self._grants[grant.token_sha256] = grant
        return grant, token

    def grant_for(self, token: str | None) -> Grant | None:
        if not token:
            return None
        with self._grants_lock:
            for digest, grant in self._grants.items():
                if token_matches(token, digest):
                    return grant
        return None

    def requests_recorded(self, grant: Grant) -> int:
        return grant.requests_seen

    def credential_fingerprints(self) -> dict[str, str]:
        return {p: fp for p, fp in self.vault.fingerprints().items() if p in self.upstreams}


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        chunks, total = [], 0
        while True:
            size_line = handler.rfile.readline(1024)
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline(1024)
                break
            total += size
            if total > MAX_REQUEST_BYTES:
                raise ValueError("request body exceeds the relay limit")
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline(1024)
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_REQUEST_BYTES:
        raise ValueError("request body exceeds the relay limit")
    return handler.rfile.read(length) if length else b""


def _make_handler(relay: RecordingRelay):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "SynchriResearchRelay/" + RELAY_VERSION

        def log_message(self, *args):  # the run store is the log
            pass

        def address_string(self):
            return "relay-client"

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def do_PUT(self):
            self._handle()

        def do_DELETE(self):
            self._handle()

        # ------------------------------------------------------------

        def _reply(self, status: int, body: bytes, content_type="application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _handle(self):
            if relay.halted is not None:
                self._reply(503, b'{"error":{"message":"research relay halted"}}')
                return
            provider, _, rest = self.path.lstrip("/").partition("/")
            profile = PROFILES.get(provider)
            path = "/" + rest
            try:
                raw = _read_body(self)
            except ValueError as exc:
                self._reply(413, (b'{"error":{"message":"%s"}}' % str(exc).encode()))
                return
            token = profile.client_token(self.headers) if profile else None
            grant = relay.grant_for(token)
            headers = redact_headers(self.headers.items())
            content_encoding = self.headers.get("Content-Encoding") or "identity"

            if profile is None or grant is None:
                index = 0
                reasons = ["unknown_provider"] if profile is None else ["unknown_or_missing_token"]
                inspection = None
            else:
                index = grant.next_index()
                inspection = inspect_request(
                    grant, profile, self.command, path, raw, content_encoding, index
                )
                reasons = inspection.violations
            decision = "reject" if reasons else "forward"
            grant_id = grant.grant_id if grant else None
            invocation_id = grant.invocation_id if grant else None

            # 1. record the exact request before anything else (LOG-11, LOG-03)
            try:
                digest = relay.store.put_blob(raw, "application/json")
                relay.store.append(
                    "relay.request_received",
                    {
                        "grant_id": grant_id,
                        "request_index": index,
                        "method": self.command,
                        "path": self.path,
                        "headers": headers,
                        "request": digest,
                        "request_sha256": digest,
                        "content_encoding": content_encoding,
                        "model": inspection.model if inspection else None,
                        "tool_names": inspection.tool_names if inspection else [],
                        "decision": decision,
                    },
                    actor="relay",
                    invocation_id=invocation_id,
                )
                if reasons:
                    status = 403
                    relay.store.append(
                        "relay.request_rejected",
                        {"grant_id": grant_id, "request_index": index,
                         "reasons": reasons, "status_returned": status},
                        actor="relay",
                        invocation_id=invocation_id,
                    )
            except StoreWriteError as exc:
                relay.halt("store_write_failed", exc.message)
                self._reply(503, b'{"error":{"message":"research relay could not record"}}')
                return
            if reasons:
                message = "research relay refused this request: " + ", ".join(reasons)
                self._reply(403, (profile or PROFILES["openai"]).error_body(message))
                return

            # 2. forward with the dedicated credential
            self._forward(profile, grant, index, path, raw)

        def _forward(self, profile, grant, index, path, raw):
            upstream = relay.upstreams[profile.name]
            scheme, host, port, base_path = upstream.split()
            query = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
            target = f"{base_path}{path.split('?', 1)[0]}{query}"
            out_headers = {
                name: value for name, value in self.headers.items()
                if name.lower() not in _HOP_HEADERS and name.lower() not in _SECRET_HEADERS
            }
            out_headers.update(profile.auth_headers(relay.vault.get(profile.name).reveal()))
            out_headers["Accept-Encoding"] = "identity"
            out_headers["Content-Length"] = str(len(raw))
            started = time.monotonic()
            try:
                if scheme == "https":
                    context = ssl.create_default_context(cafile=upstream.ca_bundle or _certifi())
                    if upstream.proxy:
                        proxy_host, _, proxy_port = upstream.proxy.rpartition(":")
                        conn = http.client.HTTPSConnection(
                            proxy_host, int(proxy_port), context=context,
                            timeout=relay.upstream_timeout_s,
                        )
                        conn.set_tunnel(host, port)
                    else:
                        conn = http.client.HTTPSConnection(
                            host, port, context=context, timeout=relay.upstream_timeout_s
                        )
                else:
                    conn = http.client.HTTPConnection(host, port, timeout=relay.upstream_timeout_s)
                conn.request(self.command, target, body=raw, headers=out_headers)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                self._record_upstream_error(grant, index, type(exc).__name__, str(exc))
                self._reply(502, profile.error_body("research relay could not reach the provider"))
                return

            captured = bytearray()
            truncated = False
            try:
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in _HOP_HEADERS:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    if len(captured) + len(chunk) > MAX_RESPONSE_BYTES:
                        truncated = True
                        break
                    captured.extend(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError:
                # The client went away mid-stream: record what arrived, marked
                # incomplete, so the invocation is not mistaken for a clean one.
                truncated = True
            finally:
                conn.close()
            duration_ms = int((time.monotonic() - started) * 1000)
            served, usage = profile.response_facts(bytes(captured))
            try:
                digest = relay.store.put_blob(bytes(captured), response.getheader(
                    "Content-Type", "application/octet-stream"))
                relay.store.append(
                    "relay.upstream_response",
                    {
                        "grant_id": grant.grant_id,
                        "request_index": index,
                        "status": response.status,
                        "headers": redact_headers(response.getheaders()),
                        "response": digest,
                        "response_sha256": digest,
                        "served_model": served,
                        "usage": usage,
                        "duration_ms": duration_ms,
                        "truncated": truncated,
                    },
                    actor="relay",
                    invocation_id=grant.invocation_id,
                )
            except StoreWriteError as exc:
                relay.halt("store_write_failed", exc.message)

        def _record_upstream_error(self, grant, index, kind, detail):
            try:
                relay.store.append(
                    "relay.upstream_error",
                    {"grant_id": grant.grant_id, "request_index": index,
                     "error_kind": kind, "detail": detail[:2000]},
                    actor="relay",
                    invocation_id=grant.invocation_id,
                )
            except StoreWriteError as exc:
                relay.halt("store_write_failed", exc.message)

    return Handler


def _peer_uid(sock) -> int | None:
    """The connecting process's UID (Linux SO_PEERCRED), or None if unavailable."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    import struct

    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def _make_control_handler(relay: RecordingRelay):
    """Grant issuance for an orchestrator in another process (owner-only socket)."""

    class ControlHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def address_string(self):
            return "relay-control"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            uid = _peer_uid(self.connection)
            if uid is not None and hasattr(os, "getuid") and uid != os.getuid():
                self._send(403, {"error": "the control socket serves the relay's own user only"})
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            if self.path != "/status":
                self._send(404, {"error": "unknown control path"})
                return
            self._send(200, {"halted": relay.halted, "run_id": relay.store.run_id,
                             "credential_fingerprints": relay.credential_fingerprints(),
                             "socket_name": relay.socket_path.name if relay.socket_path else None})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path != "/grants":
                self._send(404, {"error": "unknown control path"})
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                grant, token = relay.issue_grant(
                    invocation_id=str(request["invocation_id"]),
                    provider=str(request["provider"]),
                    model=str(request["model"]),
                    allowed_tools=tuple(request.get("allowed_tools") or ()),
                    max_requests=int(request.get("max_requests", 1)),
                    max_tokens=request.get("max_tokens"),
                    ttl_s=float(request.get("ttl_s", 1800.0)),
                )
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": f"malformed grant request: {exc}"})
                return
            except (RelayError, StoreWriteError) as exc:
                self._send(409, {"error": exc.message, "code": exc.code})
                return
            self._send(200, {"grant_id": grant.grant_id, "invocation_id": grant.invocation_id,
                             "token": token})

    return ControlHandler


@dataclass(frozen=True)
class GrantRef:
    grant_id: str
    invocation_id: str


class RelayClient:
    """An orchestrator's handle on a relay running in another process.

    It holds no credential: grants come over the owner-only control socket and
    leak checks use the relay's credential fingerprints.
    """

    def __init__(self, control_socket: str | os.PathLike, data_socket: str | os.PathLike) -> None:
        self.control_socket = str(control_socket)
        self.socket_path = Path(data_socket)

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        conn = UnixHTTPConnection(self.control_socket)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/json"} if body else {})
            response = conn.getresponse()
            data = json.loads(response.read() or b"{}")
        except (OSError, ValueError) as exc:
            raise RelayError(f"the relay control socket is unreachable: {exc}") from exc
        finally:
            conn.close()
        if response.status != 200:
            raise RelayError(f"the relay refused {path}: {data.get('error')}")
        return data

    @property
    def halted(self) -> str | None:
        return self._call("GET", "/status")["halted"]

    def credential_fingerprints(self) -> dict[str, str]:
        return self._call("GET", "/status")["credential_fingerprints"]

    def issue_grant(self, *, invocation_id: str, provider: str, model: str, allowed_tools=(),
                    max_requests: int = 1, max_tokens: int | None = None, ttl_s: float = 1800.0,
                    forbidden_markers=None):
        if forbidden_markers is not None:
            raise RelayError("a remote relay applies its own default forbidden markers")
        data = self._call("POST", "/grants", {
            "invocation_id": invocation_id, "provider": provider, "model": model,
            "allowed_tools": list(allowed_tools), "max_requests": max_requests,
            "max_tokens": max_tokens, "ttl_s": ttl_s,
        })
        return GrantRef(data["grant_id"], data["invocation_id"]), data["token"]


def _certifi() -> str | None:
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is a declared dependency
        return None
    return certifi.where()


class UnixHTTPConnection(http.client.HTTPConnection):
    """An ``http.client`` connection over a Unix socket (tests, the bridge)."""

    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock
