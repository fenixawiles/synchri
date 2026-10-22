"""Dedicated provider credentials, held only by the recording relay (ISO-06).

Credentials are read from **one explicitly named file**, never from the
environment, a provider CLI's configuration directory, a hosted session's
token files, or ``SYNCHRI_HOME``. The file must be a regular, owner-only file
belonging to the relay's user. Values are wrapped in :class:`Secret`, whose
``repr`` and ``str`` never reveal them; only :meth:`Secret.reveal` does, and
only the relay's upstream request builder calls it.

File format (JSON)::

    {"anthropic": "<dedicated research key>", "openai": "<dedicated research key>"}
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .canonical import sha256_hex
from .errors import CredentialError

PROVIDERS = ("anthropic", "openai")

#: Directories and files that belong to provider CLIs, hosted sessions, or
#: Synchri's production state. A research credential file inside any of them
#: is refused: it would mean reusing (or copying next to) a non-research
#: credential.
_FORBIDDEN_HOME_RELATIVE = (
    ".claude", ".claude.json", ".codex", ".config/claude", ".config/anthropic",
    ".config/openai", ".config/github-copilot", ".synchri", ".aidapter", ".ccr",
)
_FORBIDDEN_ENV_PATHS = (
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "SYNCHRI_HOME", "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
)


class Secret:
    """A credential value that does not print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def fingerprint(self) -> str:
        return sha256_hex(self._value.encode("utf-8"))

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):  # refuse pickling, which would serialize the value
        raise TypeError("Secret values cannot be serialized")


def forbidden_roots(environ=None) -> list[Path]:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    roots = [home / rel for rel in _FORBIDDEN_HOME_RELATIVE]
    for name in _FORBIDDEN_ENV_PATHS:
        value = env.get(name)
        if value:
            roots.append(Path(value))
    return roots


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class CredentialVault:
    """Provider credentials for one relay process."""

    def __init__(self, secrets: dict[str, Secret], source: str) -> None:
        self._secrets = secrets
        self.source = source

    def __repr__(self) -> str:
        return f"CredentialVault(providers={sorted(self._secrets)}, source={self.source!r})"

    @classmethod
    def load(cls, path, *, environ=None) -> "CredentialVault":
        if path is None:
            raise CredentialError("the relay needs an explicit --credentials file")
        given = Path(path)
        if not given.is_absolute():
            raise CredentialError("the credentials path must be absolute")
        try:
            info = os.lstat(given)
        except OSError as exc:
            raise CredentialError(f"cannot read the credentials file: {exc.strerror}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise CredentialError("the credentials file must not be a symbolic link")
        if not stat.S_ISREG(info.st_mode):
            raise CredentialError("the credentials path is not a regular file")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise CredentialError("the credentials file must belong to the relay's user")
        if info.st_mode & 0o077:
            raise CredentialError(
                "the credentials file is readable or writable by others; chmod 600 it"
            )
        resolved = given.resolve()
        for root in forbidden_roots(environ):
            for candidate in {root, root.resolve() if root.exists() else root}:
                if _within(resolved, candidate):
                    raise CredentialError(
                        "refusing a credentials file inside a provider CLI, hosted-session, "
                        f"or Synchri state location ({candidate}); research keys must be "
                        "dedicated and stored apart from them"
                    )
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialError(f"the credentials file is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not data:
            raise CredentialError("the credentials file must map provider names to keys")
        secrets: dict[str, Secret] = {}
        for provider, value in data.items():
            if provider not in PROVIDERS:
                raise CredentialError(f"unknown provider {provider!r} in the credentials file")
            if not isinstance(value, str) or len(value.strip()) < 8:
                raise CredentialError(f"the {provider} credential is empty or malformed")
            secrets[provider] = Secret(value.strip())
        return cls(secrets, str(resolved))

    @classmethod
    def for_tests(cls, secrets: dict[str, str]) -> "CredentialVault":
        """Dummy credentials for mock-provider tests (never a real key)."""
        return cls({k: Secret(v) for k, v in secrets.items()}, "test")

    def providers(self) -> list[str]:
        return sorted(self._secrets)

    def get(self, provider: str) -> Secret:
        secret = self._secrets.get(provider)
        if secret is None:
            raise CredentialError(f"no {provider} credential is loaded in the relay")
        return secret

    def fingerprints(self) -> dict[str, str]:
        return {name: secret.fingerprint() for name, secret in sorted(self._secrets.items())}

    def contains_value(self, text: str) -> bool:
        """True if any credential value occurs in ``text`` (leak checks)."""
        return any(secret.reveal() in text for secret in self._secrets.values())
