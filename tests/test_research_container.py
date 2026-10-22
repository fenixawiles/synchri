"""Per-invocation container specifications refuse anything that weakens isolation.

Spec ISO-01, ISO-02, ISO-05 (PC-04, PC-06, PC-10, PC-13) at the specification
level: no Docker daemon is needed. Container behavior itself is exercised by
the opt-in conformance suite (tests/test_research_conformance.py).
"""

from __future__ import annotations

import hashlib
import socket
import tempfile
from pathlib import Path

import pytest

from synchri.research.container import (
    ContainerSpec,
    build_argv,
    check_cli_environment,
    describe,
    validate,
)
from synchri.research.errors import IsolationError
from synchri.research.launcher import TOKEN_SOURCE_ENV, split_inventory, write_launcher
from synchri.research.providers import CODEX_DISABLED_FEATURES, ClaudeAdapter, CodexAdapter

IMAGE = "example/runner@sha256:" + "a" * 64
TOKEN = "srt_" + "t" * 43


@pytest.fixture
def parts(tmp_path):
    cli_root = tmp_path / "opt-cli"
    cli_root.mkdir()
    binary = cli_root / "claude"
    binary.write_bytes(b"#!fake binary\n")
    launcher = tmp_path / "run" / "inv" / "launcher"
    write_launcher(launcher, {"argv": []})
    sock_dir = Path(tempfile.mkdtemp(prefix="srr-", dir="/tmp"))
    sock_path = sock_dir / "relay.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    yield {
        "cli_root": cli_root, "sha": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "launcher": launcher, "socket": sock_path,
        "env": {"HOME": str(tmp_path / "home")},
    }
    server.close()
    sock_path.unlink()
    sock_dir.rmdir()


def _spec(parts, **overrides):
    values = dict(image=IMAGE, cli_root=parts["cli_root"], cli_relpath="claude",
                  cli_sha256=parts["sha"], launcher_dir=parts["launcher"],
                  relay_socket=parts["socket"], token=TOKEN)
    values.update(overrides)
    return ContainerSpec(**values)


def test_a_well_formed_spec_validates(parts):
    validate(_spec(parts), environ=parts["env"])


@pytest.mark.parametrize("image", ["debian:bookworm-slim", "runner:latest", "runner@sha256:abc", ""])
def test_images_must_be_digest_pinned(parts, image):
    with pytest.raises(IsolationError, match="pinned by digest"):
        validate(_spec(parts, image=image), environ=parts["env"])


def test_containers_never_run_as_root(parts):
    with pytest.raises(IsolationError, match="root"):
        validate(_spec(parts, uid=0), environ=parts["env"])


def test_the_binary_hash_must_match_the_pin(parts):
    with pytest.raises(IsolationError, match="hash does not match"):
        validate(_spec(parts, cli_sha256="0" * 64), environ=parts["env"])
    with pytest.raises(IsolationError, match="inside the CLI root"):
        validate(_spec(parts, cli_relpath="../escape"), environ=parts["env"])


@pytest.mark.parametrize("target", ["home", "home/.synchri", "home/.claude", "run-store-parent", "/"])
def test_production_and_credential_locations_are_never_mounted(parts, tmp_path, target):
    home = Path(parts["env"]["HOME"])
    (home / ".synchri").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    paths = {
        "home": home, "home/.synchri": home / ".synchri", "home/.claude": home / ".claude",
        "run-store-parent": tmp_path / "run", "/": Path("/"),
    }
    source = paths[target]
    (source / "claude").write_bytes(b"#!fake binary\n") if source != Path("/") else None
    spec = _spec(parts, cli_root=source, extra_forbidden=(tmp_path / "run",))
    with pytest.raises(IsolationError, match="refusing to mount|hash"):
        validate(spec, environ=parts["env"])


def test_the_launcher_directory_holds_exactly_the_launcher(parts):
    (parts["launcher"] / "extra.txt").write_text("smuggled")
    with pytest.raises(IsolationError, match="exactly"):
        validate(_spec(parts), environ=parts["env"])


def test_the_relay_channel_must_be_a_socket(parts, tmp_path):
    fake = tmp_path / "not-a-socket"
    fake.write_text("")
    with pytest.raises(IsolationError, match="not a socket"):
        validate(_spec(parts, relay_socket=fake), environ=parts["env"])


def test_only_a_relay_token_and_never_a_credential_reaches_the_container(parts):
    with pytest.raises(IsolationError, match="relay token"):
        validate(_spec(parts, token="sk-ant-real-looking"), environ=parts["env"])
    with pytest.raises(IsolationError, match="credential"):
        validate(_spec(parts, token=TOKEN), environ=parts["env"], forbidden_values=[TOKEN[4:20]])


def test_the_command_line_isolates_and_carries_no_secret(parts):
    spec = _spec(parts)
    argv = build_argv(spec)
    joined = " ".join(argv)
    for flag in ("--rm", "--read-only", "--cap-drop ALL", "no-new-privileges",
                 "--network none", "--user 10001:10001", "--pids-limit 256", "--memory 2g"):
        assert flag in joined
    assert argv[argv.index("-e") + 1] == TOKEN_SOURCE_ENV, "token passed by name only"
    assert TOKEN not in joined
    volumes = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert volumes == [
        f"{parts['cli_root']}:/opt/cli:ro",
        f"{parts['launcher']}:/opt/synchri:ro",
        f"{parts['socket']}:/run/synchri/relay.sock",
    ], "exactly three mounts, two read-only"
    for target in ("/work", "/home/slot", "/tmp"):
        assert any(a.startswith(target + ":") for a in argv), f"fresh tmpfs {target}"
    record = describe(spec)
    assert record["network"]["mode"] == "none" and TOKEN not in str(record)


def test_cli_environment_is_an_exact_allowlist():
    adapter = ClaudeAdapter()
    plan = adapter.plan(model="claude-x", token=TOKEN, system_prompt="Answer.")
    check_cli_environment(plan.env, adapter.allowed_env, adapter.token_env)
    with pytest.raises(IsolationError, match="outside the adapter allowlist"):
        check_cli_environment({**plan.env, "GH_TOKEN": "x"}, adapter.allowed_env, adapter.token_env)
    with pytest.raises(IsolationError, match="credential"):
        check_cli_environment(plan.env, adapter.allowed_env, adapter.token_env,
                              forbidden_values=[TOKEN])
    codex = CodexAdapter()
    with pytest.raises(IsolationError, match="outside"):
        check_cli_environment({**codex.plan(model="m", token=TOKEN, system_prompt="s").env,
                               "SYNCHRI_HOME": "/x"}, codex.allowed_env, codex.token_env)


def test_no_synchri_variable_reaches_a_participant_not_even_the_token():
    """ISO-01: the relay token's variable name carries no SYNCHRI_ prefix either."""
    for adapter in (ClaudeAdapter(), CodexAdapter()):
        env = adapter.plan(model="m", token=TOKEN, system_prompt="s").env
        assert not [k for k in env if k.startswith("SYNCHRI_")]
        assert not adapter.token_env.startswith("SYNCHRI_")
    assert not TOKEN_SOURCE_ENV.startswith("SYNCHRI_")
    with pytest.raises(IsolationError, match="Synchri state"):
        check_cli_environment({"SYNCHRI_RELAY_TOKEN": TOKEN}, frozenset({"SYNCHRI_RELAY_TOKEN"}),
                              "SYNCHRI_RELAY_TOKEN")


def test_claude_plan_is_the_minimal_context_flag_set():
    plan = ClaudeAdapter().plan(model="claude-full-id", token=TOKEN, system_prompt="Answer.",
                                effort="low", max_output_tokens=512)
    argv = plan.argv
    for flag in ("-p", "--bare", "--no-session-persistence", "--strict-mcp-config"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "claude-full-id"
    tools = argv.index("--tools")
    assert argv[tools + 1] == "" and argv[tools + 2] == "--disallowedTools", (
        "--tools is variadic: it must be followed by another option, never the prompt"
    )
    assert plan.env["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert plan.env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert plan.files == {"/home/slot/system-prompt.txt": "Answer."}


def test_codex_plan_disables_every_executable_tool_and_injected_context():
    plan = CodexAdapter().plan(model="gpt-full-id", token=TOKEN, system_prompt="Answer.")
    argv = plan.argv
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert disabled == set(CODEX_DISABLED_FEATURES)
    configs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    for entry in ("include_environment_context=false", "include_permissions_instructions=false",
                  'web_search="disabled"', "model_providers.research.request_max_retries=0",
                  "model_providers.research.stream_max_retries=0", "project_doc_max_bytes=0"):
        assert entry in configs
    assert any(c.startswith("skills.config=") and c.count("enabled=false") == 5 for c in configs)
    assert "--ephemeral" in argv and "--ignore-user-config" in argv and "--strict-config" in argv
    assert plan.env["CODEX_HOME"] == "/home/slot/codexhome" and plan.dirs == ("/home/slot/codexhome",)
    with pytest.raises(ValueError, match="output-token cap"):
        CodexAdapter().plan(model="m", token=TOKEN, system_prompt="s", max_output_tokens=10)


def test_the_inventory_line_is_split_from_cli_stderr():
    stderr = b"cli says hi\n\n@@SYNCHRI-RESEARCH-INVENTORY@@ {\"files\": [], \"probe_hits\": {}}\n"
    cli, inventory = split_inventory(stderr)
    assert cli == b"cli says hi\n" and inventory == {"files": [], "probe_hits": {}}
    assert split_inventory(b"no marker") == (b"no marker", None)
