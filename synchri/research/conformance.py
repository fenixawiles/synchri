"""Provider conformance tests VT-28 to VT-31 and VT-33 (spec PC-03; evidence H).

Each check runs the **real, pinned CLI binary inside the participant container,
through the recording relay**, against a mock upstream provider with dummy
credentials. Nothing reaches a real provider and no real credential is used.
The checks establish client-enforced properties only; model and server
behavior need live tests (spec §9.3).

* VT-28 request shape — the request declares exactly the allowed tool
  definitions, names the requested model, carries no injected-context marker
  and no host path; the CLI-added constants are recorded (PC-08).
* VT-29 forged tool call — the mock answers with a call to a disabled tool
  whose *execution* would produce a canary that its *text* does not contain;
  the canary must appear in no later request.
* VT-30 hidden retries — the mock fails the first request; the relay must see
  exactly one request, and the invocation must be classified failed.
* VT-31 persistence — no file in the fresh home or working directory contains
  the prompt's canary after the invocation.
* VT-33 credential isolation — the upstream received the relay's credential on
  every request and never the token; no credential or token value appears in
  the container command line, the launcher files, or the run store.
"""

from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .canonical import sha256_hex
from .credentials import CredentialVault
from .invocation import CliConfig, run_invocation
from .mock_provider import SERVED_MODEL, MockProvider
from .policy import DEFAULT_FORBIDDEN_MARKERS
from .providers import adapter_for
from .relay import RecordingRelay, Upstream
from .store import RunStore

SYSTEM_PROMPT = "Answer the question."
FORGED_TOOLS = {"claude_code": ("Bash", "command"), "codex": ("exec_command", "cmd")}


@dataclass
class CheckResult:
    vt: str
    name: str
    passed: bool
    details: dict = field(default_factory=dict)


@dataclass
class ConformanceReport:
    runtime: str
    cli_version: str
    cli_sha256: str
    image: str
    run_dir: str
    checks: list[CheckResult]
    store_verified: bool

    @property
    def passed(self) -> bool:
        return self.store_verified and all(c.passed for c in self.checks)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["passed"] = self.passed
        return data


def _canary(label: str) -> str:
    return f"CANARY-{label}-{secrets.token_hex(8)}"


def _request_text(store: RunStore, requests: list[dict]) -> list[str]:
    return [store.blob(r["payload"]["request"]).decode("utf-8", "replace") for r in requests]


def _cli_constants(provider: str, body: dict) -> dict:
    """The CLI-added, non-removable content of a request (PC-08)."""
    if provider == "anthropic":
        system = body.get("system") or []
        blocks = [b.get("text", "") for b in system if isinstance(b, dict)]
        return {"system_blocks_other_than_ours": [b for b in blocks if b != SYSTEM_PROMPT],
                "tool_definitions": [t.get("name") for t in body.get("tools") or []]}
    return {"tool_definitions": [t.get("name") or t.get("type") for t in body.get("tools") or []],
            "developer_inputs": [
                c.get("text", "") for item in body.get("input") or []
                if item.get("role") == "developer" for c in item.get("content") or []
                if isinstance(c, dict) and c.get("text") != SYSTEM_PROMPT]}


def _tool_refusals(provider: str, follow_up: str) -> list[str]:
    """The CLI's own refusal of the forged call, as sent back to the provider."""
    try:
        body = json.loads(follow_up)
    except ValueError:
        return []
    found = []
    if provider == "anthropic":
        for message in body.get("messages") or []:
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    found.append(str(block.get("content"))[:300])
    else:
        for item in body.get("input") or []:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                found.append(str(item.get("output"))[:300])
    return found


def run_conformance(*, runtime: str, cli: CliConfig, image: str, model: str,
                    run_dir: Path, timeout_s: float = 300.0, adapter=None) -> ConformanceReport:
    """Run the checks. ``adapter`` overrides the pinned one (negative controls)."""
    adapter = adapter or adapter_for(runtime)
    run_dir = Path(run_dir)
    store = RunStore.create(run_dir)
    dummy = {adapter.provider: f"dummy-{adapter.provider}-{secrets.token_hex(16)}"}
    vault = CredentialVault.for_tests(dummy)
    socket_dir = Path(tempfile.mkdtemp(prefix="srr-", dir="/tmp"))
    mock = MockProvider().start()
    relay = RecordingRelay(
        store, vault, {adapter.provider: Upstream(mock.base_url)},
        socket_path=socket_dir / "relay.sock", allow_insecure_upstream=True,
    ).start()
    checks: list[CheckResult] = []
    common = dict(store=store, relay=relay, vault=vault, adapter=adapter, cli=cli, image=image,
                  work_root=run_dir / "invocations", model=model, system_prompt=SYSTEM_PROMPT,
                  timeout_s=timeout_s, allow_unverified_version=True)
    host_paths = [str(run_dir.resolve()), str(Path(cli.cli_root).resolve())]
    try:
        # VT-28 -------------------------------------------------------
        mock.script = []
        outcome = run_invocation(prompt="What is 2+2?", **common)
        texts = _request_text(store, outcome.relay_requests)
        first = outcome.relay_requests[0]["payload"] if outcome.relay_requests else {}
        body = json.loads(texts[0]) if texts else {}
        markers = [m for m in DEFAULT_FORBIDDEN_MARKERS[adapter.provider] if texts and m in texts[0]]
        leaked_paths = [p for p in host_paths if texts and p in texts[0]]
        served = [r["payload"]["served_model"] for r in outcome.relay_responses]
        checks.append(CheckResult("VT-28", "request shape", bool(
            outcome.ok and len(texts) == 1
            and sorted(first.get("tool_names", [])) == sorted(adapter.allowed_tools)
            and first.get("model") == model and not markers and not leaked_paths
            and served == [SERVED_MODEL]
        ), {
            "outcome_ok": outcome.ok, "failure_reasons": outcome.failure_reasons,
            "requests": len(texts), "tool_names": first.get("tool_names"),
            "requested_model": first.get("model"), "served_model": served,
            "injected_markers": markers, "host_paths_in_request": leaked_paths,
            "cli_added_constants": _cli_constants(adapter.provider, body) if body else None,
            "temperature_sent": "temperature" in body, "top_p_sent": "top_p" in body,
            "final_text": outcome.final_text,
        }))

        # VT-29 -------------------------------------------------------
        left, right = _canary("EXEC")[:20], secrets.token_hex(6)
        tool, field_name = FORGED_TOOLS[runtime]
        mock.script = [("toolcall", tool, {field_name: f'echo {left}""{right}'})]
        outcome = run_invocation(prompt="What is 3+3?", max_requests=2, **common)
        texts = _request_text(store, outcome.relay_requests)
        executed = any(left + right in t for t in texts) or (left + right).encode() in outcome.stdout
        refusals = _tool_refusals(adapter.provider, texts[1]) if len(texts) > 1 else []
        checks.append(CheckResult("VT-29", "forged tool call refused", bool(
            not executed and (len(texts) == 1 or refusals)
        ), {
            "forged_tool": tool, "requests": len(texts), "execution_evidence_found": executed,
            "refusal_evidence": refusals,
        }))

        # VT-30 -------------------------------------------------------
        mock.script = [("status", 500)]
        outcome = run_invocation(prompt="What is 4+4?", max_requests=3, **common)
        checks.append(CheckResult("VT-30", "no hidden retries", bool(
            len(outcome.relay_requests) == 1 and not outcome.ok
        ), {
            "requests_seen_by_relay": len(outcome.relay_requests),
            "classified_failed": not outcome.ok, "failure_reasons": outcome.failure_reasons,
            "returncode": outcome.returncode,
        }))

        # VT-31 -------------------------------------------------------
        mock.script = []
        persist = _canary("PERSIST")
        outcome = run_invocation(prompt=f"Repeat nothing. Token: {persist}",
                                 persistence_probes=(persist,), **common)
        inventory = outcome.inventory
        checks.append(CheckResult("VT-31", "no persistence of the prompt", bool(
            inventory is not None and not inventory.get("probe_hits") and outcome.ok
        ), {
            "inventory_reported": inventory is not None,
            "files_after_run": [f["path"] for f in (inventory or {}).get("files", [])],
            "probe_hits": (inventory or {}).get("probe_hits"),
            "outcome_ok": outcome.ok, "failure_reasons": outcome.failure_reasons,
        }))

        # VT-33 -------------------------------------------------------
        credential = dummy[adapter.provider]
        expected = sha256_hex(credential.encode())
        model_requests = [r for r in mock.requests if r["method"] == "POST"]
        upstream_ok = bool(model_requests) and all(r["auth_sha256"] == expected for r in model_requests)
        launcher_text = "".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in (run_dir / "invocations").glob("*/launcher/*")
        )
        argv_text = " ".join(outcome.container_argv)
        raw = sqlite3.connect(store.path)
        stored = b"".join(
            (v.encode() if isinstance(v, str) else bytes(v))
            for table in ("research_events", "research_blobs")
            for row in raw.execute(f"SELECT * FROM {table}") for v in row
            if isinstance(v, (str, bytes))
        )
        raw.close()
        token_like = [e["payload"]["token_sha256"] for e in store.events("relay.grant_issued")]
        checks.append(CheckResult("VT-33", "credential isolation", bool(
            upstream_ok and credential not in launcher_text and credential not in argv_text
            and credential.encode() not in stored and "srt_" not in launcher_text
            and "srt_" not in argv_text
        ), {
            "upstream_received_relay_credential_on_every_request": upstream_ok,
            "credential_in_launcher_files": credential in launcher_text,
            "credential_in_container_argv": credential in argv_text,
            "credential_in_run_store": credential.encode() in stored,
            "token_in_launcher_or_argv": "srt_" in launcher_text or "srt_" in argv_text,
            "grants_recorded_by_hash_only": len(token_like),
        }))
    finally:
        relay.stop()
        mock.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)
    verified = store.verify()
    report = ConformanceReport(
        runtime=runtime, cli_version=cli.cli_version, cli_sha256=cli.cli_sha256, image=image,
        run_dir=str(run_dir), checks=checks, store_verified=verified.ok,
    )
    (run_dir / "conformance-report.json").write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return report
