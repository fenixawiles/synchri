"""Provider profiles, invocation grants, and request inspection for the relay.

A *grant* authorizes exactly one participant invocation: one provider, one
model (full ID), a fixed tool-definition allowlist, a request limit, and a
list of injected-context markers that must never reach the model. The CLI
holds the grant's token in place of a provider credential (ISO-06); the relay
recognizes it, inspects every request against the grant, and refuses anything
outside it before forwarding (LOG-11, ISO-03, CFG-02, CFG-06, BUD-05).
"""

from __future__ import annotations

import gzip
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from .canonical import canonical_json, sha256_hex


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    #: (method, path regex) pairs a participant may call; everything else is refused.
    allowed_routes: tuple[tuple[str, str], ...]

    def route_allowed(self, method: str, path: str) -> bool:
        bare = path.split("?", 1)[0]
        return any(method == m and re.fullmatch(p, bare) for m, p in self.allowed_routes)

    # -- client side ----------------------------------------------------

    def client_token(self, headers) -> str | None:
        if self.name == "anthropic":
            value = headers.get("x-api-key")
            if value:
                return value.strip()
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    # -- upstream side --------------------------------------------------

    def auth_headers(self, credential: str) -> dict[str, str]:
        if self.name == "anthropic":
            return {"x-api-key": credential}
        return {"Authorization": f"Bearer {credential}"}

    # -- request content ------------------------------------------------

    @staticmethod
    def model(body: dict) -> str | None:
        value = body.get("model")
        return value if isinstance(value, str) else None

    @staticmethod
    def tool_names(body: dict) -> list[str]:
        names = []
        for tool in body.get("tools") or []:
            if isinstance(tool, dict):
                names.append(str(tool.get("name") or tool.get("type") or "<unnamed>"))
            else:
                names.append("<malformed>")
        return names

    # -- response content -----------------------------------------------

    def response_facts(self, data: bytes) -> tuple[str | None, object]:
        """(served model, provider usage block) from a JSON or SSE response."""
        text = data.decode("utf-8", "replace")
        objects = []
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                objects.append(json.loads(stripped))
            except ValueError:
                objects = []  # not JSON: no facts to extract; the raw bytes are recorded
        else:
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        objects.append(json.loads(line[5:].strip()))
                    except ValueError:
                        continue
        served, usage = None, None
        if self.name == "anthropic":
            usage_parts = {}
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "message_start":
                    message = obj.get("message") or {}
                    served = served or message.get("model")
                    if message.get("usage") is not None:
                        usage_parts["message_start"] = message.get("usage")
                elif obj.get("type") == "message_delta" and obj.get("usage") is not None:
                    usage_parts["message_delta"] = obj.get("usage")
                elif obj.get("type") == "message":
                    served = served or obj.get("model")
                    if obj.get("usage") is not None:
                        usage_parts["message"] = obj.get("usage")
            usage = usage_parts or None
        else:
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                response = obj.get("response") if isinstance(obj.get("response"), dict) else obj
                if isinstance(response, dict):
                    served = response.get("model") or served
                    if response.get("usage") is not None:
                        usage = response.get("usage")
        return served, usage

    def error_body(self, message: str) -> bytes:
        if self.name == "anthropic":
            body = {"type": "error", "error": {"type": "permission_error", "message": message}}
        else:
            body = {"error": {"message": message, "type": "invalid_request_error",
                              "code": "research_relay_policy"}}
        return json.dumps(body).encode("utf-8")


PROFILES = {
    "anthropic": ProviderProfile("anthropic", (("POST", r"/v1/messages"),)),
    "openai": ProviderProfile("openai", (("POST", r"/v1/responses"),)),
}

#: Model-visible context the pinned CLIs inject when misconfigured
#: (Feasibility §4.4). A request containing any of them is refused.
DEFAULT_FORBIDDEN_MARKERS = {
    "anthropic": (
        "Primary working directory",
        "Is a git repository",
        "Today's date is",
        "auto memory",
        "MEMORY.md",
        "\nCWD: ",
        "gitStatus",
    ),
    "openai": (
        "<environment_context>",
        "<skills_instructions>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<multi_agent",
        "AGENTS.md",
    ),
}


@dataclass
class Grant:
    grant_id: str
    invocation_id: str
    provider: str
    model: str
    allowed_tools: frozenset[str]
    max_requests: int
    forbidden_markers: tuple[str, ...]
    expires_monotonic: float
    expires_at: str
    token_sha256: str
    max_tokens: int | None = None
    requests_seen: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def policy(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "allowed_tools": sorted(self.allowed_tools),
            "max_requests": self.max_requests,
            "forbidden_markers": list(self.forbidden_markers),
            "max_tokens": self.max_tokens,
        }

    def policy_sha256(self) -> str:
        return sha256_hex(canonical_json(self.policy()))

    def next_index(self) -> int:
        with self._lock:
            self.requests_seen += 1
            return self.requests_seen


def new_token() -> str:
    return "srt_" + secrets.token_urlsafe(32)


def token_matches(token: str, digest: str) -> bool:
    return hmac.compare_digest(sha256_hex(token.encode("utf-8")), digest)


@dataclass
class Inspection:
    body: dict | None
    model: str | None
    tool_names: list[str]
    content_encoding: str
    violations: list[str]


class UnsupportedEncoding(ValueError):
    pass


def decode_body(raw: bytes, content_encoding: str) -> bytes:
    encoding = (content_encoding or "identity").strip().lower()
    if encoding in ("", "identity"):
        return raw
    if encoding == "gzip":
        return gzip.decompress(raw)
    raise UnsupportedEncoding(f"unsupported content encoding {encoding!r}")


def inspect_request(
    grant: Grant, profile: ProviderProfile, method: str, path: str, raw: bytes,
    content_encoding: str, request_index: int,
) -> Inspection:
    """Every rule the relay enforces before forwarding, as violation codes."""
    violations: list[str] = []
    if grant.provider != profile.name:
        violations.append("provider_mismatch")
    if time.monotonic() > grant.expires_monotonic:
        violations.append("grant_expired")
    if request_index > grant.max_requests:
        violations.append("request_limit_exceeded")
    if not profile.route_allowed(method, path):
        violations.append("route_not_allowed")
    body: dict | None = None
    try:
        decoded = decode_body(raw, content_encoding)
        parsed = json.loads(decoded.decode("utf-8")) if decoded else None
        if isinstance(parsed, dict):
            body = parsed
        else:
            violations.append("body_not_json_object")
    except UnsupportedEncoding:
        violations.append("encoding_unsupported")
        decoded = b""
    except (ValueError, OSError, EOFError):
        violations.append("body_unreadable")
        decoded = b""
    model = profile.model(body) if body else None
    tools = profile.tool_names(body) if body else []
    if body is not None:
        if model != grant.model:
            violations.append("model_mismatch")
        for name in tools:
            if name not in grant.allowed_tools:
                violations.append(f"tool_not_allowed:{name}")
        if grant.max_tokens is not None and body.get("max_tokens") != grant.max_tokens:
            violations.append("max_tokens_mismatch")
        text = decoded.decode("utf-8", "replace")
        for marker in grant.forbidden_markers:
            if marker in text:
                violations.append(f"injected_marker:{marker.strip()}")
    return Inspection(body, model, tools, (content_encoding or "identity").lower(), violations)
