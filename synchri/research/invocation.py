"""One isolated participant invocation: grant -> container -> records.

This is the foundation-phase primitive only. Experimental rounds, prompt
templates, scheduling, and scoring are later phases and are not here.

Sequence (spec §4.3, with D4 and D6):

1. the relay issues a grant and a one-time token for this invocation;
2. the adapter's launch plan is checked against its environment allowlist and
   the container specification is validated (mounts, image pin, binary hash);
3. ``invocation.prepared`` and ``invocation.isolation`` are recorded;
4. the container runs with the prompt on stdin;
5. ``invocation.exited`` / ``invocation.failed`` and ``invocation.captured``
   are recorded, with the outcome cross-checked against the relay's record.

Any store failure raises and halts (LOG-03). Failure classification uses the
exit code, the CLI's ``is_error`` where present, and the relay's recorded
statuses and refusals — never Claude's ``result.subtype`` alone (PC-12).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .canonical import sha256_hex
from .container import ContainerSpec, build_argv, check_cli_environment, describe, run, validate
from .credentials import CredentialVault
from .errors import IsolationError, RelayError
from .launcher import TOKEN_PLACEHOLDER, TOKEN_SOURCE_ENV, split_inventory, write_launcher
from .providers import BRIDGE_PORT, HOME_DIR, RELAY_SOCKET, WORK_DIR, ProviderAdapter
from .relay import RecordingRelay, RelayClient
from .store import RunStore
from .streams import PARSERS, StreamFacts


@dataclass
class CliConfig:
    """A pinned CLI installation on the host."""

    runtime: str
    cli_root: Path
    cli_relpath: str
    cli_sha256: str
    cli_version: str


@dataclass
class InvocationOutcome:
    invocation_id: str
    ok: bool
    failure_reasons: list[str]
    returncode: int | None
    timed_out: bool
    stdout: bytes
    stderr: bytes
    final_text: str | None
    inventory: dict | None
    relay_requests: list[dict] = field(default_factory=list)
    relay_rejections: list[dict] = field(default_factory=list)
    relay_responses: list[dict] = field(default_factory=list)
    container_argv: list[str] = field(default_factory=list)


def run_invocation(
    *,
    store: RunStore,
    relay: RecordingRelay | RelayClient,
    adapter: ProviderAdapter,
    cli: CliConfig,
    image: str,
    work_root: Path,
    model: str,
    prompt: str,
    system_prompt: str,
    effort: str | None = None,
    max_output_tokens: int | None = None,
    max_requests: int = 1,
    timeout_s: float = 900.0,
    template_id: str = "foundation-probe",
    template_version: str = "1",
    persistence_probes: tuple[str, ...] = (),
    canaries: tuple[str, ...] = (),
    allow_unverified_version: bool = False,
    vault: CredentialVault | None = None,
) -> InvocationOutcome:
    if cli.runtime != adapter.runtime:
        raise IsolationError(f"CLI {cli.runtime} does not match adapter {adapter.runtime}")
    if cli.cli_version not in adapter.verified_versions and not allow_unverified_version:
        raise IsolationError(
            f"{adapter.runtime} {cli.cli_version} has no verified flag set "
            f"(verified: {', '.join(adapter.verified_versions)}); run the conformance suite first"
        )
    if relay.socket_path is None:
        raise RelayError("participant containers reach the relay only through its Unix socket")

    invocation_id = f"inv_{secrets.token_urlsafe(12)}"
    grant, token = relay.issue_grant(
        invocation_id=invocation_id, provider=adapter.provider, model=model,
        allowed_tools=adapter.allowed_tools, max_requests=max_requests,
        max_tokens=max_output_tokens if adapter.provider == "anthropic" else None,
    )
    plan = adapter.plan(model=model, token=token, system_prompt=system_prompt,
                        effort=effort, max_output_tokens=max_output_tokens)
    # With an out-of-process relay the orchestrator holds no credential (ISO-06):
    # leak checks then use the relay's fingerprints; a co-resident vault adds
    # substring checks.
    secret_values = [vault.get(p).reveal() for p in vault.providers()] if vault is not None else []
    fingerprints = set(relay.credential_fingerprints().values())
    check_cli_environment(plan.env, adapter.allowed_env, adapter.token_env, secret_values,
                          forbidden_fingerprints=fingerprints)

    invocation_dir = Path(work_root) / invocation_id
    launch = {
        "argv": plan.argv,
        "env": {k: (TOKEN_PLACEHOLDER if k == adapter.token_env else v) for k, v in plan.env.items()},
        "token_placeholder": TOKEN_PLACEHOLDER,
        "token_source": TOKEN_SOURCE_ENV,
        "files": plan.files,
        "dirs": list(plan.dirs),
        "cwd": WORK_DIR,
        "bridge_port": BRIDGE_PORT,
        "relay_socket": RELAY_SOCKET,
        "inventory_roots": [HOME_DIR, WORK_DIR],
        "persistence_probes": list(persistence_probes),
    }
    launcher_hashes = write_launcher(invocation_dir / "launcher", launch)
    spec = ContainerSpec(
        image=image, cli_root=Path(cli.cli_root), cli_relpath=cli.cli_relpath,
        cli_sha256=cli.cli_sha256, launcher_dir=invocation_dir / "launcher",
        relay_socket=relay.socket_path, token=token, timeout_s=timeout_s,
        extra_forbidden=(store.path.parent,),
    )
    validate(spec, forbidden_values=secret_values)

    prompt_bytes = prompt.encode("utf-8")
    prompt_blob = store.put_blob(prompt_bytes, "text/plain; charset=utf-8")
    env_hashes = {k: sha256_hex(v.encode()) for k, v in sorted(plan.env.items())}
    store.append(
        "invocation.prepared",
        {
            "template_id": template_id,
            "template_version": template_version,
            "template_sha256": sha256_hex(system_prompt.encode("utf-8")),
            "prompt": prompt_blob,
            "prompt_sha256": prompt_blob,
            "prompt_chars": len(prompt),
            "inputs": [],
            "argv": plan.argv + ["<stdin>"],
            "cwd": WORK_DIR,
            "env_names": sorted(plan.env),
            "env_value_sha256": env_hashes,
            "timeout_s": timeout_s,
            "budget_before": None,
        },
        invocation_id=invocation_id,
    )
    isolation = describe(spec)
    store.append(
        "invocation.isolation",
        {
            "runtime_kind": "container",
            "image_ref": isolation["image_ref"],
            "container_argv": isolation["container_argv"],
            "mounts": isolation["mounts"],
            "launcher_files": launcher_hashes,
            "network": isolation["network"],
            "limits": isolation["limits"],
            "cli_binary_sha256": cli.cli_sha256,
            "cli_version": cli.cli_version,
        },
        invocation_id=invocation_id,
    )

    def launched(pid: int, started_ns: int) -> None:
        store.append("invocation.launched", {"pid": pid, "started_monotonic_ns": started_ns},
                     invocation_id=invocation_id)

    result = run(spec, prompt_bytes, on_spawn=launched)

    cli_stderr, inventory = split_inventory(result.stderr)
    parse = PARSERS.get(adapter.runtime)
    facts = parse(result.stdout) if parse else StreamFacts()
    final = facts.final_text
    is_error = facts.is_error

    requests = [e for e in store.events("relay.request_received") if e["invocation_id"] == invocation_id]
    rejections = [e for e in store.events("relay.request_rejected") if e["invocation_id"] == invocation_id]
    responses = [e for e in store.events("relay.upstream_response") if e["invocation_id"] == invocation_id]
    errors = [e for e in store.events("relay.upstream_error") if e["invocation_id"] == invocation_id]

    reasons: list[str] = []
    if result.timed_out:
        reasons.append("timed_out")
    if result.returncode != 0:
        reasons.append(f"exit_code:{result.returncode}")
    if is_error:
        reasons.append("cli_is_error")
    if rejections:
        reasons.append("relay_refused_request")
    if errors:
        reasons.append("relay_upstream_error")
    if any(r["payload"]["status"] >= 400 for r in responses):
        reasons.append("upstream_error_status")
    if any(r["payload"]["truncated"] for r in responses):
        reasons.append("response_truncated")
    if not responses:
        reasons.append("no_upstream_response")
    if relay.halted:
        reasons.append("relay_halted")

    blobs = {
        "stdout": store.put_blob(result.stdout, "application/x-ndjson"),
        "stderr": store.put_blob(cli_stderr, "text/plain"),
        "stream": None,
        "final_text": store.put_blob(final.encode("utf-8"), "text/plain") if final else None,
    }
    blobs["stream"] = blobs["stdout"]
    scan_space = result.stdout + cli_stderr + b"".join(
        store.blob(r["payload"]["request"]) for r in requests
    )
    hits = [c for c in canaries if c.encode("utf-8") in scan_space]
    store.append(
        "invocation.failed" if reasons else "invocation.exited",
        {
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "cancelled": False,
            "wall_s": result.wall_s,
            "monotonic_elapsed_ns": result.monotonic_elapsed_ns,
            "failure_kind": ",".join(reasons) or None,
        },
        invocation_id=invocation_id,
    )
    store.append(
        "invocation.captured",
        {
            **blobs,
            "stdout_truncated": False,
            "final_text_source": "stream_result" if final else "raw_stdout",
            "model_reported": facts.model_reported,
            "provider_session_id": facts.provider_session_id,
            "usage": facts.usage,
            "canary_scan": "clean" if not hits else hits,
            "tool_events_seen": facts.tool_events,
        },
        invocation_id=invocation_id,
    )
    if hits:
        evidence = store.put_blob(json.dumps({"canaries": hits}).encode(), "application/json")
        store.append(
            "isolation.violation_detected",
            {"invocation_id": invocation_id, "kind": "canary", "locations": hits, "evidence": evidence},
            invocation_id=invocation_id,
        )
    return InvocationOutcome(
        invocation_id=invocation_id, ok=not reasons and not hits,
        failure_reasons=reasons + [f"canary:{c}" for c in hits],
        returncode=result.returncode, timed_out=result.timed_out,
        stdout=result.stdout, stderr=cli_stderr, final_text=final, inventory=inventory,
        relay_requests=requests, relay_rejections=rejections, relay_responses=responses,
        container_argv=build_argv(spec),
    )

