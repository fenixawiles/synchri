"""Opt-in provider conformance suite: real pinned CLIs, real containers, mock upstream.

Spec VT-28 to VT-31 and VT-33 (PC-03; evidence class H), plus negative
controls showing the checks fail when a flag set is weakened, and a container
probe showing the isolation boundary holds even for a program that actively
tries to escape it.

Skipped unless ``SYNCHRI_RESEARCH_CONFORMANCE=1``, a Docker daemon is
reachable, and the pins are given:

    SYNCHRI_RESEARCH_IMAGE=<image@sha256:… or sha256:…>
    SYNCHRI_RESEARCH_CLAUDE_ROOT / _CLAUDE_RELPATH / _CLAUDE_SHA256 / _CLAUDE_VERSION
    SYNCHRI_RESEARCH_CODEX_ROOT  / _CODEX_RELPATH  / _CODEX_SHA256  / _CODEX_VERSION

No real provider is contacted and no real credential is used.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from synchri.research.conformance import run_conformance
from synchri.research.container import docker_available
from synchri.research.credentials import CredentialVault
from synchri.research.invocation import CliConfig, run_invocation
from synchri.research.mock_provider import MockProvider
from synchri.research.providers import ClaudeAdapter, CodexAdapter, LaunchPlan, ProviderAdapter
from synchri.research.relay import RecordingRelay, RelayClient, Upstream
from synchri.research.store import RunStore

ENABLED = os.environ.get("SYNCHRI_RESEARCH_CONFORMANCE") == "1"
pytestmark = pytest.mark.skipif(
    not ENABLED or not docker_available() or not os.environ.get("SYNCHRI_RESEARCH_IMAGE"),
    reason="opt-in: set SYNCHRI_RESEARCH_CONFORMANCE=1, SYNCHRI_RESEARCH_IMAGE, and the CLI pins",
)
IMAGE = os.environ.get("SYNCHRI_RESEARCH_IMAGE", "")


def _cli(runtime: str, prefix: str) -> CliConfig:
    values = [os.environ.get(f"SYNCHRI_RESEARCH_{prefix}_{k}") for k in ("ROOT", "RELPATH", "SHA256", "VERSION")]
    if not all(values):
        pytest.skip(f"{runtime} pins not configured")
    return CliConfig(runtime, Path(values[0]), values[1], values[2], values[3])


@pytest.fixture
def run_dir():
    # Outside pytest's tmp tree on purpose: it is mounted from, so it must not
    # sit under the invoking user's home (the container module refuses that).
    path = Path(tempfile.mkdtemp(prefix="synchri-conformance-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _assert_all_pass(report):
    failed = {c.vt: c.details for c in report.checks if not c.passed}
    assert not failed, json.dumps(failed, indent=1)[:4000]
    assert report.store_verified


def test_claude_conformance(run_dir):
    report = run_conformance(runtime="claude_code", cli=_cli("claude_code", "CLAUDE"), image=IMAGE,
                             model="claude-sonnet-5-5", run_dir=run_dir / "claude")
    _assert_all_pass(report)
    constants = report.checks[0].details["cli_added_constants"]
    assert constants["tool_definitions"] == []
    assert any("Claude Agent SDK" in b for b in constants["system_blocks_other_than_ours"]), (
        "the fixed identity line is recorded as a treatment constant (PC-08)"
    )


def test_codex_conformance(run_dir):
    report = run_conformance(runtime="codex", cli=_cli("codex", "CODEX"), image=IMAGE,
                             model="gpt-5-codex-research-test", run_dir=run_dir / "codex")
    _assert_all_pass(report)
    assert report.checks[0].details["tool_names"] == ["request_user_input"]
    assert report.checks[1].details["refusal_evidence"] == ["unsupported call: exec_command"]


class _ClaudeWithoutBare(ClaudeAdapter):
    def plan(self, **kwargs):
        plan = super().plan(**kwargs)
        return replace(plan, argv=[a for a in plan.argv if a != "--bare"])


class _CodexWithShell(CodexAdapter):
    def plan(self, **kwargs):
        plan = super().plan(**kwargs)
        argv, skip = [], False
        for i, arg in enumerate(plan.argv):
            if skip:
                skip = False
                continue
            if arg == "--disable" and plan.argv[i + 1] in {"shell_tool", "unified_exec"}:
                skip = True
                continue
            argv.append(arg)
        return replace(plan, argv=argv)


def test_negative_control_claude_without_bare_is_caught(run_dir):
    report = run_conformance(runtime="claude_code", cli=_cli("claude_code", "CLAUDE"), image=IMAGE,
                             model="claude-sonnet-5-5", run_dir=run_dir / "neg-claude",
                             adapter=_ClaudeWithoutBare())
    shape = report.checks[0]
    assert not shape.passed, "injected environment context must fail VT-28"
    assert "relay_refused_request" in shape.details["failure_reasons"]


def test_negative_control_codex_with_shell_is_caught(run_dir):
    report = run_conformance(runtime="codex", cli=_cli("codex", "CODEX"), image=IMAGE,
                             model="gpt-5-codex-research-test", run_dir=run_dir / "neg-codex",
                             adapter=_CodexWithShell())
    shape = report.checks[0]
    assert not shape.passed and "exec_command" in shape.details["tool_names"]


PROBE = r'''#!/usr/bin/python3 -I
import json, os, socket, sys
# /proc/self/environ is the environment given at execve, before Python's own
# locale coercion (PEP 538) adds LC_CTYPE to os.environ.
with open("/proc/self/environ", "rb") as fh:
    given = sorted(e.split(b"=", 1)[0].decode() for e in fh.read().split(b"\0") if e)
report = {"uid": os.getuid(), "env": given, "reads": {}, "writes": {}, "net": None}
for path in sys.argv[1:]:
    try:
        with open(path, "rb") as fh:
            report["reads"][path] = "READ:" + fh.read(64).decode("utf-8", "replace")
    except OSError as exc:
        report["reads"][path] = type(exc).__name__
for path in ("/probe-write", "/opt/cli/probe-write", "/opt/synchri/probe-write", "/work/ok", "/home/slot/ok"):
    try:
        open(path, "w").write("x")
        report["writes"][path] = "WROTE"
    except OSError as exc:
        report["writes"][path] = type(exc).__name__
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    report["net"] = "CONNECTED"
except OSError as exc:
    report["net"] = type(exc).__name__
print(json.dumps(report))
'''


class _ProbeAdapter(ProviderAdapter):
    runtime = "probe"
    provider = "anthropic"
    executable = "probe"
    verified_versions = ("probe",)
    allowed_tools = ()
    token_env = "ANTHROPIC_API_KEY"
    allowed_env = frozenset({"PATH", "HOME", "TMPDIR", "ANTHROPIC_API_KEY"})

    def __init__(self, targets):
        self.targets = targets

    def plan(self, *, model, token, system_prompt, effort=None, max_output_tokens=None):
        return LaunchPlan(argv=["/opt/cli/probe", *self.targets],
                          env={"PATH": "/usr/bin:/bin", "HOME": "/home/slot", "TMPDIR": "/work",
                               "ANTHROPIC_API_KEY": token},
                          files={}, dirs=())


def test_the_container_boundary_holds_against_an_active_probe(run_dir):
    """Not a CLI: a program that tries to read host state, write, and reach the network.

    Driven through an out-of-process-style relay client, so the invocation path
    runs without any credential on the orchestrator side.
    """
    cli_root = run_dir / "probe-cli"
    cli_root.mkdir()
    (cli_root / "probe").write_text(PROBE)
    (cli_root / "probe").chmod(0o755)
    secret_file = run_dir / "store" / "host-secret.txt"
    secret_file.parent.mkdir()
    secret_file.write_text("HOST-SECRET-" + secrets.token_hex(8))
    targets = [str(secret_file), "/etc/shadow", "/root/.bashrc", str(Path.home() / ".claude.json"),
               "/run/synchri/relay.sock"]
    store = RunStore.create(run_dir / "run")
    vault = CredentialVault.for_tests({"anthropic": "dummy-anthropic-" + secrets.token_hex(8)})
    sock_dir = Path(tempfile.mkdtemp(prefix="srr-", dir="/tmp"))
    with MockProvider() as mock:
        relay = RecordingRelay(store, vault, {"anthropic": Upstream(mock.base_url)},
                               socket_path=sock_dir / "relay.sock",
                               control_socket_path=sock_dir / "control.sock",
                               allow_insecure_upstream=True).start()
        # The orchestrator side holds no vault: grants and leak checks come
        # from the relay over its control socket (ISO-06).
        client = RelayClient(sock_dir / "control.sock", sock_dir / "relay.sock")
        try:
            import hashlib
            cli = CliConfig("probe", cli_root, "probe",
                            hashlib.sha256((cli_root / "probe").read_bytes()).hexdigest(), "probe")
            outcome = run_invocation(store=store, relay=client,
                                     adapter=_ProbeAdapter(targets), cli=cli, image=IMAGE,
                                     work_root=run_dir / "inv", model="m", prompt="",
                                     system_prompt="", timeout_s=120)
        finally:
            relay.stop()
    shutil.rmtree(sock_dir, ignore_errors=True)
    report = json.loads(outcome.stdout.decode().strip().splitlines()[-1])
    assert report["uid"] == 10001
    for path in targets[:-1]:
        assert not report["reads"][path].startswith("READ:"), f"{path} readable inside the container"
    assert report["writes"]["/probe-write"] != "WROTE", "root filesystem must be read-only"
    assert report["writes"]["/opt/cli/probe-write"] != "WROTE" and report["writes"]["/opt/synchri/probe-write"] != "WROTE"
    assert report["writes"]["/work/ok"] == "WROTE" and report["writes"]["/home/slot/ok"] == "WROTE"
    assert report["net"] != "CONNECTED", "no network beyond the relay channel"
    assert set(report["env"]) <= {"PATH", "HOME", "TMPDIR", "ANTHROPIC_API_KEY"}
    assert vault.get("anthropic").reveal().encode() not in outcome.stdout
