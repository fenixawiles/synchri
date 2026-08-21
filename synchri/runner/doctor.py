"""Verify a runtime CLI's managed-launch path — passively, then for real.

Two tiers, deliberately separated so probes can never surprise the user:

*Passive doctor* — free to run at boot or while the chooser renders: is the
executable present, does ``--version`` parse inside the supported range, what
do the maintained adapter's capability flags say, and does a cached sign-in
look present.  It never invokes the provider and reports an honest
pass / fail / unknown per check — an absent credential file is *unknown*,
never a red X, because several CLIs keep auth in a keychain.

*Connection test* — user-initiated only: a real, bounded, cancellable CLI
invocation in a fresh temporary directory outside every repository, with a
fixed sentinel-only prompt and no repository mutation authority.  Where the
adapter defines resume, a second invocation resumes the first and proves it —
one invocation cannot prove resume.  The outcome is stored durably per runtime
(``runtime_connections``) and is invalidated whenever the executable path,
version, adapter revision, or cached-auth indication changes.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ..errors import ValidationError
from ..ids import utc_now
from ..session.modes import KNOWN_RUNTIMES
from ..storage import db
from .agent_command import AgentCommand
from .stream_events import parser_for

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

#: Resume outcomes stored on the durable connection record.
RESUME_VERIFIED = "verified"
RESUME_UNVERIFIED = "supported_unverified"
RESUME_UNSUPPORTED = "unsupported"

#: The passive tier never waits long: a CLI that cannot even print its version
#: quickly gets an honest "unknown", not a hung chooser.
PASSIVE_PROBE_TIMEOUT = 3.0
#: Per-invocation ceiling for the consented connection test.
CONNECTION_TEST_TIMEOUT = 180.0

_VERSION_PATTERN = re.compile(r"(\d+(?:\.\d+)+)")

#: Stderr fragments that make a failed invocation look like a sign-in problem
#: rather than a launch problem.  Classification only — never a grant.
_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please log in",
    "login required",
    "logged out",
    "please run /login",
    "sign in",
    "unauthorized",
    "authentication",
    "invalid api key",
    "credential",
)


def _check(key: str, state: str, detail: str = "") -> dict:
    return {"key": key, "state": state, "detail": detail}


def _definition(runtime: str, override: dict | None = None) -> dict:
    if override is not None:
        return override
    return KNOWN_RUNTIMES.get(runtime, KNOWN_RUNTIMES["generic"])


def adapter_revision(definition: dict) -> str:
    """A digest of the launch-relevant adapter fields.

    When a Synchri update changes how a runtime is launched, streamed, or
    resumed, every stored connection proof made under the old adapter must
    stop counting as proof.
    """
    relevant = {
        key: definition.get(key)
        for key in (
            "managed_command",
            "plain_managed_command",
            "stream_format",
            "resume_command",
            "min_version",
        )
    }
    encoded = json.dumps(relevant, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


# ----------------------------------------------------------------------
# passive tier
# ----------------------------------------------------------------------


def _probe_version(command: str, timeout: float) -> tuple[str, str | None, str]:
    """Run the CLI's local version command. Local invocation, no provider."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return UNKNOWN, None, f"unusable version command: {exc}"
    if not argv:
        return UNKNOWN, None, "no version command"
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return FAIL, None, "the executable disappeared while probing its version"
    except subprocess.TimeoutExpired:
        return UNKNOWN, None, f"did not answer --version within {timeout:g}s"
    except OSError as exc:
        return UNKNOWN, None, f"could not run the version probe: {exc}"
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    match = _VERSION_PATTERN.search(output)
    if result.returncode != 0 and not match:
        tail = (result.stderr or result.stdout or "").strip()
        return FAIL, None, f"the version probe exited {result.returncode}: {tail[:200]}"
    if not match:
        return UNKNOWN, None, "could not parse a version number from the CLI's output"
    return PASS, match.group(1), ""


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def passive_report(
    runtime: str, *, definition: dict | None = None, timeout: float = PASSIVE_PROBE_TIMEOUT
) -> dict:
    """The side-effect-free tier: executable, version, capability flags,
    cached-auth indication. Every check is pass, fail, or an honest unknown."""
    spec = _definition(runtime, definition)
    checks: list[dict] = []

    executable = spec.get("executable")
    path = shutil.which(executable) if executable else None
    if path:
        checks.append(_check("installed", PASS, path))
    elif executable:
        checks.append(_check("installed", FAIL, f"{executable} was not found on PATH"))
    else:
        checks.append(_check("installed", UNKNOWN, "this runtime has no known executable"))

    version: str | None = None
    if path:
        version_command = spec.get("version_command") or f"{shlex.quote(path)} --version"
        state, version, detail = _probe_version(version_command, timeout)
        floor = spec.get("min_version")
        if state == PASS and floor:
            if _version_tuple(version) < tuple(floor):
                floor_text = ".".join(str(part) for part in floor)
                state, detail = FAIL, (
                    f"version {version} is below the supported floor {floor_text}"
                )
            else:
                detail = version
        elif state == PASS:
            detail = version
        checks.append(_check("version", state, detail))
    else:
        checks.append(_check("version", UNKNOWN, "not installed"))

    managed = bool(spec.get("managed_command"))
    resume_defined = bool(spec.get("resume_command"))
    if managed:
        stream = spec.get("stream_format")
        parts = [
            "managed launch is maintained",
            f"streams as {stream}" if stream else "plain output",
            "resume defined" if resume_defined else "resume not defined",
        ]
        checks.append(_check("capabilities", PASS, "; ".join(parts)))
    else:
        checks.append(
            _check(
                "capabilities",
                FAIL,
                "no maintained managed command — connecting this runtime is unavailable",
            )
        )

    indicators = spec.get("auth_indicators") or []
    auth_indication: bool | None = None
    found = next(
        (
            candidate
            for candidate in indicators
            if Path(candidate).expanduser().exists()
        ),
        None,
    )
    if found:
        auth_indication = True
        checks.append(_check("auth", PASS, f"a cached sign-in is present ({found})"))
    elif indicators:
        auth_indication = False
        checks.append(
            _check(
                "auth",
                UNKNOWN,
                "no cached sign-in file found — the CLI may keep auth in a keychain",
            )
        )
    else:
        checks.append(_check("auth", UNKNOWN, "this adapter has no cached sign-in indicator"))

    return {
        "runtime": runtime,
        "checks": checks,
        "facts": {
            "executable_path": path,
            "version": version,
            "adapter_revision": adapter_revision(spec),
            "auth_indication": auth_indication,
            "managed": managed,
            "stream_format": spec.get("stream_format"),
            "resume_defined": resume_defined,
        },
    }


# ----------------------------------------------------------------------
# durable connection records
# ----------------------------------------------------------------------


def load_connection(conn, runtime: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runtime_connections WHERE runtime = ?", (runtime,)
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["checks"] = json.loads(record.get("checks") or "[]")
    record["auth_indication"] = (
        None if record["auth_indication"] is None else bool(record["auth_indication"])
    )
    return record


def _save_connection(conn, record: dict) -> None:
    now = utc_now()
    existing = conn.execute(
        "SELECT created_at FROM runtime_connections WHERE runtime = ?",
        (record["runtime"],),
    ).fetchone()
    created_at = existing["created_at"] if existing else now
    auth = record.get("auth_indication")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO runtime_connections (runtime, state, executable_path, version, "
            "adapter_revision, auth_indication, resume, checks, detail, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(runtime) DO UPDATE SET state=excluded.state, "
            "executable_path=excluded.executable_path, version=excluded.version, "
            "adapter_revision=excluded.adapter_revision, auth_indication=excluded.auth_indication, "
            "resume=excluded.resume, checks=excluded.checks, detail=excluded.detail, "
            "updated_at=excluded.updated_at",
            (
                record["runtime"],
                record["state"],
                record.get("executable_path"),
                record.get("version"),
                record.get("adapter_revision") or "",
                None if auth is None else int(bool(auth)),
                record.get("resume") or RESUME_UNSUPPORTED,
                json.dumps(record.get("checks") or []),
                record.get("detail") or "",
                created_at,
                now,
            ),
        )


def forget_connection(conn, runtime: str) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM runtime_connections WHERE runtime = ?", (runtime,))


def stored_connections(conn) -> dict[str, dict]:
    """Stored state only — no probes. Cheap enough for first paint."""
    rows = conn.execute("SELECT * FROM runtime_connections").fetchall()
    result = {}
    for row in rows:
        result[row["runtime"]] = {
            "state": row["state"],
            "resume": row["resume"],
            "version": row["version"],
            "tested_at": row["updated_at"],
            "detail": row["detail"],
        }
    return result


def connection_state(
    conn,
    runtime: str,
    *,
    definition: dict | None = None,
    timeout: float = PASSIVE_PROBE_TIMEOUT,
) -> dict:
    """The live answer to "is this runtime still connected?".

    A stored proof is only as good as the world it was made in: when the
    executable path, version, adapter revision, or cached-auth indication has
    changed since the test, the state downgrades to ``reconnect_needed`` with
    the reasons — never a silent stale green.
    """
    stored = load_connection(conn, runtime)
    if stored is None:
        return {
            "runtime": runtime,
            "state": "not_connected",
            "detail": "Run the connection test so Synchri can launch this agent itself.",
        }
    report = passive_report(runtime, definition=definition, timeout=timeout)
    facts = report["facts"]
    if stored["state"] != "connected":
        return {
            "runtime": runtime,
            "state": "failed",
            "detail": stored["detail"] or "the last connection test failed",
            "tested_at": stored["updated_at"],
        }
    reasons: list[str] = []
    if not facts["executable_path"]:
        reasons.append("the executable is no longer on PATH")
    elif stored["executable_path"] and facts["executable_path"] != stored["executable_path"]:
        reasons.append(
            f"the executable moved ({stored['executable_path']} → {facts['executable_path']})"
        )
    if stored["version"] and facts["version"] and facts["version"] != stored["version"]:
        reasons.append(f"the CLI version changed ({stored['version']} → {facts['version']})")
    if stored["adapter_revision"] and stored["adapter_revision"] != facts["adapter_revision"]:
        reasons.append("Synchri's maintained adapter for this runtime changed")
    if stored["auth_indication"] is True and facts["auth_indication"] is False:
        reasons.append("the cached sign-in is no longer present")
    if reasons:
        return {
            "runtime": runtime,
            "state": "reconnect_needed",
            "reasons": reasons,
            "detail": "Reconnect to re-verify: " + "; ".join(reasons) + ".",
            "tested_at": stored["updated_at"],
        }
    return {
        "runtime": runtime,
        "state": "connected",
        "resume": stored["resume"],
        "version": stored["version"],
        "tested_at": stored["updated_at"],
        "detail": "Connected. Synchri launches this agent itself — no paste step.",
    }


# ----------------------------------------------------------------------
# the user-initiated connection test
# ----------------------------------------------------------------------


def _sandbox_dir() -> str:
    """A fresh temporary directory outside every repository.

    The canary runs here with a sentinel-only prompt; it has nothing of the
    user's to read and no repository to mutate. Fail closed if the temp area
    is somehow inside a working tree.
    """
    path = tempfile.mkdtemp(prefix="synchri-connect-")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "true":
            shutil.rmtree(path, ignore_errors=True)
            raise ValidationError(
                "the temporary directory is inside a git repository; refusing to run the "
                "connection test there"
            )
    except (OSError, subprocess.TimeoutExpired):
        pass  # no git, or git is wedged — the mkdtemp location is still not a repo
    return path


def _sentinel_prompt(sentinel: str) -> str:
    return "\n".join(
        [
            "This is a Synchri connection test.",
            "Do not run any tools or commands. Do not read or write any files.",
            "Reply with exactly this single line and nothing else:",
            sentinel,
        ]
    )


def _looks_like_auth_failure(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


def _invoke(command: str, prompt: str, *, cwd: str, timeout: float, stream_format, cancel_event):
    """One bounded canary invocation, through the same machinery managed
    sessions use — process-group termination and stream ingestion included."""
    agent = AgentCommand.parse(f"connect={command}", cwd=cwd, timeout=timeout)
    holder: dict = {}
    if stream_format:
        def factory(fmt=stream_format):
            parser = parser_for(fmt)
            holder["parser"] = parser
            return parser

        agent.parser_factory = factory
    result = agent.invoke(prompt, cancel_event=cancel_event)
    return result, holder.get("parser")


def run_connection_test(
    conn,
    runtime: str,
    *,
    definition: dict | None = None,
    cancel_event=None,
    invoke_timeout: float = CONNECTION_TEST_TIMEOUT,
    progress=None,
) -> dict:
    """Prove the whole managed path with explicit user consent.

    Two bounded invocations where the adapter defines resume (one cannot prove
    it); a cancelled test stores nothing; a completed one stores the durable
    ``runtime_connections`` record, replacing any earlier proof.
    """
    spec = _definition(runtime, definition)
    command = spec.get("managed_command")
    if not command:
        raise ValidationError(f"no maintained managed command for runtime {runtime!r}")
    report = passive_report(runtime, definition=definition)
    facts = report["facts"]
    if not facts["executable_path"]:
        return {
            "runtime": runtime,
            "state": "failed",
            "stored": False,
            "checks": [_check("installed", FAIL, "the executable was not found on PATH")],
            "detail": "install the CLI first",
        }

    notify = progress or (lambda phase, detail="": None)
    stream_format = spec.get("stream_format")
    sandbox = _sandbox_dir()
    checks: list[dict] = []
    resume_state = RESUME_UNSUPPORTED
    resume_detail = ""
    try:
        nonce = secrets.token_hex(6)
        sentinel = f"SYNCHRI-CONNECT-OK-{nonce}"
        notify("invoking", "Launching the CLI with a sentinel-only prompt.")
        result, parser = _invoke(
            command,
            _sentinel_prompt(sentinel),
            cwd=sandbox,
            timeout=invoke_timeout,
            stream_format=stream_format,
            cancel_event=cancel_event,
        )
        used_plain = False
        if not result.ok and not result.cancelled and stream_format:
            from .managed import _looks_like_flag_rejection

            if _looks_like_flag_rejection(result):
                plain = spec.get("plain_managed_command") or command
                notify("invoking", "The CLI rejected its streaming flags; retrying plain.")
                result, parser = _invoke(
                    plain,
                    _sentinel_prompt(sentinel),
                    cwd=sandbox,
                    timeout=invoke_timeout,
                    stream_format=None,
                    cancel_event=cancel_event,
                )
                used_plain = True
        if result.cancelled:
            return {"runtime": runtime, "state": "cancelled", "stored": False}

        replied = sentinel in (result.stdout or "")
        if result.returncode is None:
            checks.append(_check("launch", FAIL, (result.stderr or "could not start").strip()[:300]))
        elif result.timed_out:
            checks.append(
                _check("launch", FAIL, f"unresponsive — no reply within {invoke_timeout:g}s")
            )
        elif result.returncode != 0:
            stderr_tail = (result.stderr or "").strip()[-300:]
            if _looks_like_auth_failure(result.stderr or ""):
                checks.append(_check("launch", PASS, "the CLI started"))
                checks.append(_check("auth", FAIL, f"sign-in problem: {stderr_tail}"))
            else:
                checks.append(_check("launch", FAIL, f"exit {result.returncode}: {stderr_tail}"))
        else:
            checks.append(_check("launch", PASS, "unattended launch works"))
            if replied:
                checks.append(
                    _check("bootstrap", PASS, "the injected prompt round-tripped verbatim")
                )
                checks.append(_check("auth", PASS, "the CLI answered — sign-in is working"))
            else:
                checks.append(
                    _check(
                        "bootstrap",
                        FAIL,
                        "the CLI exited cleanly but never echoed the sentinel",
                    )
                )

        if not stream_format:
            checks.append(_check("structured_output", UNKNOWN, "this runtime does not stream"))
        elif used_plain:
            checks.append(
                _check(
                    "structured_output",
                    FAIL,
                    "the installed CLI predates its streaming flags; plain output will be used",
                )
            )
        elif parser is not None and parser.saw_stream:
            checks.append(_check("structured_output", PASS, "the documented stream parsed"))
        else:
            checks.append(
                _check("structured_output", FAIL, "the CLI did not emit its documented stream")
            )

        # "Connected" means every proof held — launch, bootstrap, auth, and
        # (where the adapter streams) the structured-output parse. A reply
        # whose documented stream never parsed is a degraded pipeline, and
        # recording it green would be exactly the false green the passive
        # doctor refuses to fake.
        connected = replied and result.ok and not any(c["state"] == FAIL for c in checks)

        resume_command = spec.get("resume_command")
        if not resume_command:
            resume_detail = "the maintained adapter does not define resume for this runtime"
        elif not connected:
            resume_state = RESUME_UNVERIFIED
            resume_detail = "not attempted — the first invocation failed"
        else:
            resume_id = getattr(parser, "provider_session_id", None) if parser else None
            if not resume_id:
                resume_state = RESUME_UNVERIFIED
                resume_detail = (
                    "the stream did not expose a session id — resume will be verified at "
                    "first real recovery"
                )
            else:
                notify("proving_resume", "Resuming the canary session to prove resume.")
                nonce2 = secrets.token_hex(6)
                sentinel2 = f"SYNCHRI-RESUME-OK-{nonce2}"
                resume_invocation = resume_command.replace(
                    "{resume_id}", shlex.quote(resume_id)
                )
                second, second_parser = _invoke(
                    resume_invocation,
                    _sentinel_prompt(sentinel2),
                    cwd=sandbox,
                    timeout=invoke_timeout,
                    stream_format=stream_format,
                    cancel_event=cancel_event,
                )
                if second.cancelled:
                    return {"runtime": runtime, "state": "cancelled", "stored": False}
                if second.ok and sentinel2 in (second.stdout or ""):
                    resume_state = RESUME_VERIFIED
                    resume_detail = "a second invocation resumed the first and replied"
                else:
                    from .managed import _looks_like_flag_rejection

                    if _looks_like_flag_rejection(second):
                        resume_state = RESUME_UNSUPPORTED
                        resume_detail = "the installed CLI rejected the resume invocation"
                    else:
                        resume_state = RESUME_UNVERIFIED
                        resume_detail = (
                            "the resume invocation did not verify: "
                            + (
                                f"timed out after {invoke_timeout:g}s"
                                if second.timed_out
                                else (second.stderr or "no sentinel reply").strip()[-200:]
                            )
                        )
        checks.append(
            _check(
                "resume",
                PASS if resume_state == RESUME_VERIFIED else UNKNOWN,
                resume_detail,
            )
        )

        failed = [c for c in checks if c["state"] == FAIL]
        detail = (
            "connected"
            if connected
            else ("; ".join(f"{c['key']}: {c['detail']}" for c in failed) or "the test failed")
        )
        record = {
            "runtime": runtime,
            "state": "connected" if connected else "failed",
            "executable_path": facts["executable_path"],
            "version": facts["version"],
            "adapter_revision": facts["adapter_revision"],
            "auth_indication": facts["auth_indication"],
            "resume": resume_state,
            "checks": checks,
            "detail": detail,
        }
        _save_connection(conn, record)
        return {
            "runtime": runtime,
            "state": record["state"],
            "stored": True,
            "checks": checks,
            "resume": resume_state,
            "detail": detail,
        }
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# ----------------------------------------------------------------------
# background supervision for the UI
# ----------------------------------------------------------------------


class ConnectionTester:
    """Run connection tests off the request thread, one per runtime.

    Every worker opens its own database connection (SQLite connections are
    thread-affine), mirroring the managed-run registry.
    """

    def __init__(self, workspace) -> None:
        self.workspace = workspace
        self._runs: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, runtime: str, *, definition: dict | None = None) -> dict:
        with self._lock:
            thread = self._threads.get(runtime)
            if thread is not None and thread.is_alive():
                return dict(self._runs[runtime])
            run = {
                "runtime": runtime,
                "phase": "starting",
                "detail": "",
                "result": None,
                "started_at": utc_now(),
            }
            self._runs[runtime] = run
            cancel = threading.Event()
            self._cancels[runtime] = cancel
            worker = threading.Thread(
                target=self._work,
                args=(runtime, definition, cancel),
                name=f"synchri-connect-{runtime}",
                daemon=True,
            )
            self._threads[runtime] = worker
            worker.start()
            return dict(run)

    def _work(self, runtime: str, definition: dict | None, cancel: threading.Event) -> None:
        conn = db.connect(self.workspace.db_path)
        try:
            db.initialize(conn)

            def progress(phase: str, detail: str = "") -> None:
                with self._lock:
                    run = self._runs.get(runtime)
                    if run is not None:
                        run["phase"] = phase
                        run["detail"] = detail

            result = run_connection_test(
                conn,
                runtime,
                definition=definition,
                cancel_event=cancel,
                progress=progress,
            )
            with self._lock:
                run = self._runs.get(runtime) or {}
                run["phase"] = {
                    "connected": "done",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }.get(result.get("state"), "failed")
                run["detail"] = result.get("detail", "")
                run["result"] = result
        except ValidationError as exc:
            with self._lock:
                run = self._runs.get(runtime) or {}
                run["phase"] = "failed"
                run["detail"] = exc.message
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            with self._lock:
                run = self._runs.get(runtime) or {}
                run["phase"] = "failed"
                run["detail"] = f"the connection test could not run: {exc}"
        finally:
            with self._lock:
                self._cancels.pop(runtime, None)
            conn.close()

    def status(self, runtime: str) -> dict:
        with self._lock:
            run = self._runs.get(runtime)
            if run is None:
                return {"runtime": runtime, "phase": "idle", "detail": "", "result": None}
            payload = dict(run)
        thread = self._threads.get(runtime)
        payload["alive"] = bool(thread and thread.is_alive())
        return payload

    def cancel(self, runtime: str) -> dict:
        with self._lock:
            event = self._cancels.get(runtime)
            if event is not None:
                event.set()
            run = self._runs.get(runtime)
            if run is not None:
                run["phase"] = "cancelling"
        return self.status(runtime)

    def wait(self, runtime: str, timeout: float | None = None) -> dict:
        """Join the worker — used by tests and synchronous callers."""
        thread = self._threads.get(runtime)
        if thread is not None:
            thread.join(timeout=timeout)
        return self.status(runtime)
