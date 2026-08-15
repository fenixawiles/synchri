"""GitHub OAuth identity for Synchri's local desktop app.

The desktop client authenticates *as the person using Synchri*, not as a
shared service.  GitHub owns identity and repository authorization; Synchri
keeps only a renewable user token and the public profile that goes with it on
that person's machine.  On macOS that
means Keychain.  The small 0600 file fallback keeps the Python package usable
on platforms that do not provide a native credential store.

No GitHub App private key or client secret belongs in a distributed desktop
application.  Device flow is intentionally designed for this case: it uses
the public client ID, while GitHub shows the person the authorization screen.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import platform
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi

from ..config import Workspace, write_private
from ..errors import StateError

# This is a public identifier for the Synchri GitHub App, not a secret.  It is
# intentionally compiled into the client so every downloaded DMG can start the
# device flow without a user supplying credentials or a Synchri web service.
GITHUB_APP_CLIENT_ID = "Iv23liG9lp0zV1zzOPcn"
# Repository access is a second, deliberately explicit decision. The GitHub
# App install page is where a person chooses the account or organization and
# the exact repositories Synchri may open. It does not grant an account login.
# The public page must be enabled in the App registration before this link is
# usable. Once it is, this canonical URL lets every desktop build send people
# straight to the selected-repository screen without a separate web service.
GITHUB_APP_INSTALL_URL = "https://github.com/apps/synchri/installations/new"
# The GA calendar version GitHub documents for the REST API. Requesting an
# unsupported version makes api.github.com reject every call with HTTP 400,
# which this client surfaces as an empty repository list — so this constant
# must only ever hold a version GitHub actually publishes.
GITHUB_API_VERSION = "2022-11-28"
GITHUB_WEB_URL = "https://github.com"
GITHUB_API_URL = "https://api.github.com"
DEVICE_CODE_URL = f"{GITHUB_WEB_URL}/login/device/code"
TOKEN_URL = f"{GITHUB_WEB_URL}/login/oauth/access_token"
KEYCHAIN_SERVICE = "com.synchri.github"
KEYCHAIN_ACCOUNT_PREFIX = "github-app-user-token"
REQUEST_TIMEOUT_SECONDS = 15.0
_KEYCHAIN_ITEM_NOT_FOUND = -25300


def trusted_ssl_context() -> ssl.SSLContext:
    """Return Synchri's verified public-root store for GitHub HTTPS requests.

    The standalone macOS engine does not inherit a separately installed Python
    certificate bundle.  Bundling ``certifi`` keeps the desktop app portable
    without ever relaxing certificate or hostname verification.
    """
    return ssl.create_default_context(cafile=certifi.where())


@dataclass(frozen=True)
class DeviceAuthorization:
    """A short-lived GitHub verification code shown inside the app."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "expires_at": self.expires_at,
            "interval": self.interval,
        }


def start_device_authorization() -> DeviceAuthorization:
    """Ask GitHub for a one-time device code without opening a terminal."""
    payload = _post_form(DEVICE_CODE_URL, {"client_id": GITHUB_APP_CLIENT_ID})
    error = str(payload.get("error") or "")
    if error == "device_flow_disabled":
        _device_error(error, payload)
    if error:
        raise StateError(
            "GitHub could not start sign-in. Check the Synchri GitHub App settings, then try again.",
            code="github_login_failed",
            resolution={"kind": "github_app_settings"},
        )
    try:
        return DeviceAuthorization(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=str(payload.get("verification_uri") or f"{GITHUB_WEB_URL}/login/device"),
            expires_at=time.time() + int(payload["expires_in"]),
            interval=max(1, int(payload.get("interval", 5))),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError(
            "GitHub returned an incomplete sign-in code. Please try again.",
            code="github_login_failed",
            resolution={"kind": "retry_github_login"},
        ) from exc


def complete_device_authorization(workspace: Workspace, device_code: str) -> dict[str, Any]:
    """Exchange an approved device code and create the local GitHub profile."""
    if not (device_code or "").strip():
        raise StateError(
            "That GitHub sign-in request is no longer available. Connect GitHub again.",
            code="github_login_expired",
            resolution={"kind": "connect_github"},
        )
    payload = _post_form(
        TOKEN_URL,
        {
            "client_id": GITHUB_APP_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    error = payload.get("error")
    if error:
        return _device_error(str(error), payload)
    if not payload.get("access_token"):
        raise StateError(
            "GitHub did not finish sign-in. Connect GitHub and try again.",
            code="github_login_failed",
            resolution={"kind": "retry_github_login"},
        )
    # Signing in creates the local Synchri profile. Repository authorization is
    # intentionally separate: a user selects repositories when installing the
    # GitHub App, after their identity is established.
    try:
        profile = _account_for_token(str(payload["access_token"]))
    except StateError:
        # A successful OAuth exchange is still a valid sign-in during a brief
        # GitHub API outage. The profile is filled on the next status check.
        profile = None
    save_credentials(workspace, payload, account=profile)
    result: dict[str, Any] = {"state": "connected"}
    if profile:
        result["account"] = profile
    return result


def credentials(workspace: Workspace, *, refresh: bool = True) -> dict[str, Any] | None:
    """Load a valid GitHub App user token, refreshing it when GitHub permits."""
    stored = _load_credentials(workspace)
    if not stored:
        return None
    access_token = str(stored.get("access_token") or "")
    if not access_token:
        delete_credentials(workspace)
        return None
    # Refresh a little early so a repository chooser cannot start with a token
    # that will fail mid-request.  Device-flow-generated tokens can be
    # refreshed with the public client ID alone.
    expires_at = _number(stored.get("expires_at"))
    if refresh and expires_at and expires_at <= time.time() + 120:
        refresh_token = str(stored.get("refresh_token") or "")
        if not refresh_token:
            delete_credentials(workspace)
            return None
        try:
            payload = _post_form(
                TOKEN_URL,
                {
                    "client_id": GITHUB_APP_CLIENT_ID,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except StateError:
            # A transient network failure must not make a still-valid local
            # sign-in vanish.  The next repository request will surface its
            # own actionable network error instead.
            if expires_at > time.time():
                return stored
            raise
        if payload.get("error") or not payload.get("access_token"):
            delete_credentials(workspace)
            return None
        save_credentials(workspace, payload, existing=stored)
        stored = _load_credentials(workspace) or {}
    return stored or None


def account(workspace: Workspace) -> dict[str, Any] | None:
    """Return the non-secret GitHub identity for this Mac's Synchri profile."""
    stored = credentials(workspace)
    if not stored:
        return None
    # Startup must stay instant and offline-capable. New grants save this
    # profile during OAuth; an older grant simply shows “GitHub connected”
    # until the person reconnects rather than making a network call here.
    return _safe_account(stored.get("account"))


def save_credentials(
    workspace: Workspace,
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    account: dict[str, Any] | None = None,
) -> None:
    """Persist a token bundle without ever exposing its values through API JSON."""
    now = time.time()
    prior = existing or {}
    stored = {
        "access_token": str(payload["access_token"]),
        "refresh_token": str(payload.get("refresh_token") or prior.get("refresh_token") or ""),
        "expires_at": now + int(payload.get("expires_in") or 0) if payload.get("expires_in") else None,
        "refresh_expires_at": now + int(payload.get("refresh_token_expires_in") or 0)
        if payload.get("refresh_token_expires_in")
        else prior.get("refresh_expires_at"),
        # This is intentionally only display-safe public identity data. No
        # credential ever crosses into the local UI API.
        "account": _safe_account(account) or _safe_account(prior.get("account")),
    }
    _save_credentials(workspace, stored)


def delete_credentials(workspace: Workspace) -> None:
    """Forget Synchri's local GitHub grant; the GitHub App grant remains user-controlled."""
    if _use_macos_keychain():
        subprocess.run(
            [
                "/usr/bin/security", "delete-generic-password", "-a", _keychain_account(workspace),
                "-s", KEYCHAIN_SERVICE,
            ],
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    try:
        workspace.github_credentials_path.unlink(missing_ok=True)
    except OSError:
        pass


def _load_credentials(workspace: Workspace) -> dict[str, Any] | None:
    if _use_macos_keychain():
        try:
            serialized = _load_macos_keychain_secret(workspace)
        except OSError:
            serialized = None
        if serialized:
            try:
                value = json.loads(serialized)
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None
    if _use_macos_keychain():
        return None
    try:
        value = json.loads(workspace.github_credentials_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _save_credentials(workspace: Workspace, stored: dict[str, Any]) -> None:
    serialized = json.dumps(stored, separators=(",", ":"), sort_keys=True)
    if _use_macos_keychain():
        try:
            _save_macos_keychain_secret(workspace, serialized)
            return
        except OSError as exc:
            raise StateError(
                "Synchri could not save your GitHub sign-in in Keychain. Try connecting again.",
                code="github_credential_store_failed",
                resolution={"kind": "retry_github_login"},
            ) from exc
    write_private(workspace.github_credentials_path, serialized + "\n")


def _use_macos_keychain() -> bool:
    return platform.system() == "Darwin" and Path("/usr/bin/security").is_file()


def _keychain_account(workspace: Workspace) -> str:
    """Keep custom/test workspaces isolated while retaining one local login."""
    fingerprint = hashlib.sha256(str(workspace.home).encode("utf-8")).hexdigest()[:20]
    return f"{KEYCHAIN_ACCOUNT_PREFIX}:{fingerprint}"


def _load_macos_keychain_secret(workspace: Workspace) -> str | None:
    """Read one secret directly from Keychain without a token-bearing subprocess."""
    security, core_foundation = _macos_keychain_frameworks()
    service, account_name = _macos_keychain_identifiers(workspace)
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account_name),
        account_name,
        ctypes.byref(password_length),
        ctypes.byref(password_data),
        ctypes.byref(item),
    )
    if status == _KEYCHAIN_ITEM_NOT_FOUND:
        return None
    if status != 0:
        raise OSError(f"Keychain could not read the Synchri GitHub sign-in (status {status}).")
    try:
        raw = ctypes.string_at(password_data, password_length.value)
        return raw.decode("utf-8")
    finally:
        if password_data.value:
            security.SecKeychainItemFreeContent(None, password_data)
        if item.value:
            core_foundation.CFRelease(item)


def _save_macos_keychain_secret(workspace: Workspace, serialized: str) -> None:
    """Upsert one Keychain item without putting OAuth credentials in argv.

    Keychain Services is used instead of the ``security`` command because that
    command only accepts the secret as a command-line argument when running
    non-interactively.  This keeps both access and refresh tokens out of the
    process list while retaining a dependency-free macOS implementation.
    """
    security, core_foundation = _macos_keychain_frameworks()
    service, account_name = _macos_keychain_identifiers(workspace)
    payload = serialized.encode("utf-8")
    payload_buffer = ctypes.create_string_buffer(payload)
    payload_pointer = ctypes.cast(payload_buffer, ctypes.c_void_p)
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account_name),
        account_name,
        None,
        None,
        ctypes.byref(item),
    )
    if status == 0:
        try:
            status = security.SecKeychainItemModifyAttributesAndData(
                item, None, len(payload), payload_pointer
            )
        finally:
            if item.value:
                core_foundation.CFRelease(item)
    elif status == _KEYCHAIN_ITEM_NOT_FOUND:
        created_item = ctypes.c_void_p()
        status = security.SecKeychainAddGenericPassword(
            None,
            len(service),
            service,
            len(account_name),
            account_name,
            len(payload),
            payload_pointer,
            ctypes.byref(created_item),
        )
        if created_item.value:
            core_foundation.CFRelease(created_item)
    if status != 0:
        raise OSError(f"Keychain could not save the Synchri GitHub sign-in (status {status}).")


def _macos_keychain_identifiers(workspace: Workspace) -> tuple[bytes, bytes]:
    return KEYCHAIN_SERVICE.encode("utf-8"), _keychain_account(workspace).encode("utf-8")


def _macos_keychain_frameworks() -> tuple[Any, Any]:
    """Load the two system frameworks needed by the Keychain C API."""
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None
    return security, core_foundation


def _account_for_token(access_token: str) -> dict[str, Any] | None:
    """Resolve the GitHub user without ever returning the token to the UI."""
    request = Request(
        f"{GITHUB_API_URL}/user",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    try:
        with urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS, context=trusted_ssl_context()
        ) as response:  # noqa: S310 - fixed GitHub API host
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise StateError(
                "Your GitHub sign-in expired. Connect GitHub again to continue.",
                code="github_connection_expired",
                resolution={"kind": "connect_github"},
            ) from exc
        raise StateError(
            "GitHub could not finish setting up your profile. It will retry automatically.",
            code="github_unavailable",
            resolution={"kind": "retry_github_login"},
        ) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise StateError(
            "GitHub could not finish setting up your profile. It will retry automatically.",
            code="github_unavailable",
            resolution={"kind": "retry_github_login"},
        ) from exc
    return _safe_account(value)


def _safe_account(value: object) -> dict[str, Any] | None:
    """Whitelist public identity fields before they enter local persistence."""
    if not isinstance(value, dict):
        return None
    login = str(value.get("login") or "").strip()
    if not login:
        return None
    account: dict[str, Any] = {"login": login}
    try:
        account["id"] = int(value["id"])
    except (KeyError, TypeError, ValueError):
        pass
    for key in ("name", "avatar_url"):
        if value.get(key):
            account[key] = str(value[key])
    return account


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    body = urlencode(fields).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS, context=trusted_ssl_context()
        ) as response:  # noqa: S310 - fixed GitHub URLs
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise StateError(
            "GitHub could not be reached. Check your connection, then try again.",
            code="github_unavailable",
            resolution={"kind": "retry_github_login"},
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise StateError(
            "GitHub could not be reached. Check your connection, then try again.",
            code="github_unavailable",
            resolution={"kind": "retry_github_login"},
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(
            "GitHub returned an unexpected sign-in response. Please try again.",
            code="github_login_failed",
            resolution={"kind": "retry_github_login"},
        ) from exc
    return value if isinstance(value, dict) else {}


def _device_error(error: str, payload: dict[str, Any]) -> dict[str, Any]:
    if error in {"authorization_pending", "slow_down"}:
        return {"state": "pending", "interval": int(payload.get("interval") or 5)}
    if error in {"expired_token", "token_expired", "access_denied"}:
        return {"state": "expired"}
    if error == "device_flow_disabled":
        raise StateError(
            "GitHub sign-in needs Device Flow enabled for the Synchri GitHub App. Try again after it is enabled.",
            code="github_device_flow_disabled",
            resolution={"kind": "github_app_settings"},
        )
    raise StateError(
        "GitHub could not complete sign-in. Please try again.",
        code="github_login_failed",
        resolution={"kind": "retry_github_login"},
    )


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
