"""Failure discrimination and per-participant recovery plumbing.

Five distinct terminal readings of one invocation, because they need five
distinct responses:

* ``auth_failure`` — the provider CLI is signed out or its auth expired.
  Recovery genuinely requires the human; escalate, never retry into it.
* ``provider_refusal`` — the provider refused or rate-limited the request.
  Also a human decision, not a retry loop.
* ``crash`` — the process died on its own. The ladder applies: resume where
  supported, then a fresh relaunch, then one replacement incarnation.
* ``unresponsive`` — no reply within the timeout; the process group was
  ended. Same ladder as a crash.
* ``normal_stop`` — a clean exit or a deliberate cancellation. Not a failure.

Classification reads only what the invocation itself produced. It never
grants anything and it never guesses "flaky": an unknown non-zero exit is a
crash, honestly.
"""

from __future__ import annotations

AUTH_FAILURE = "auth_failure"
PROVIDER_REFUSAL = "provider_refusal"
CRASH = "crash"
UNRESPONSIVE = "unresponsive"
NORMAL_STOP = "normal_stop"

#: Failure kinds the resume→relaunch→replace ladder applies to. The other two
#: escalate to the human immediately — retrying an expired sign-in or a
#: provider refusal only burns invocations.
LADDER_KINDS = frozenset({CRASH, UNRESPONSIVE})

FAILURE_DETAILS = {
    AUTH_FAILURE: "the provider CLI is signed out or its auth expired",
    PROVIDER_REFUSAL: "the provider refused the request",
    UNRESPONSIVE: "the last turn timed out and its processes were ended",
}

_AUTH_MARKERS = (
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
    "token expired",
)

_REFUSAL_MARKERS = (
    "rate limit",
    "rate-limit",
    "usage limit",
    "quota",
    "overloaded",
    "capacity",
    "too many requests",
    "content policy",
    "cannot assist",
)


def classify_failure(result) -> str:
    """Read one :class:`AgentResult` as a failure kind."""
    if result.cancelled:
        return NORMAL_STOP
    if result.timed_out:
        return UNRESPONSIVE
    if result.returncode == 0:
        return NORMAL_STOP
    stderr = (result.stderr or "").lower()
    if any(marker in stderr for marker in _AUTH_MARKERS):
        return AUTH_FAILURE
    if any(marker in stderr for marker in _REFUSAL_MARKERS):
        return PROVIDER_REFUSAL
    return CRASH


def failure_detail(kind: str, result) -> str:
    if kind == CRASH:
        return f"the last turn exited with code {result.returncode}"
    return FAILURE_DETAILS.get(kind, "the last turn failed")


class CompositeCancel:
    """Read-only view over several cancel events.

    An invocation is cancelled when the session says stop *or* when this one
    participant is being restarted — without either signal reaching the other
    participants' processes.
    """

    def __init__(self, *events) -> None:
        self._events = [event for event in events if event is not None]

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)
