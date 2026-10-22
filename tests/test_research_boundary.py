"""Research Mode stays out of production code paths and state (PRO-01, D5, VT-01).

``synchri/__init__.py`` imports the broker for the public API, so importing any
``synchri.*`` module loads that module's code. The boundary is therefore
asserted three ways: (1) no research module imports a production module;
(2) importing the research package loads nothing beyond what ``import synchri``
already loads; (3) research commands never construct a Broker and never create
``SYNCHRI_HOME``.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from synchri.research.streams import parse_claude, parse_codex

RESEARCH = Path(__file__).resolve().parents[1] / "synchri" / "research"
FORBIDDEN = ("broker", "session", "memory", "queue", "ui", "runner", "briefing", "repo")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module", sorted(p.name for p in RESEARCH.glob("*.py")))
def test_no_research_module_imports_production_code(module):
    for name in _imported_modules(RESEARCH / module):
        bare = name.lstrip(".")
        parts = bare.split(".")
        relative_parent = name.startswith("..")
        if relative_parent or bare.startswith("synchri."):
            top = parts[0] if relative_parent else parts[1]
            assert top not in FORBIDDEN, f"{module} imports {name}"


def test_importing_research_loads_nothing_beyond_the_package_root():
    code = (
        "import sys, json\n"
        "import synchri\n"
        "base = set(sys.modules)\n"
        "import synchri.research.cli, synchri.research.conformance, synchri.research.invocation\n"
        "added = sorted(m for m in set(sys.modules) - base\n"
        "               if m.startswith('synchri') and not m.startswith('synchri.research'))\n"
        "print(json.dumps(added))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == []


def test_research_commands_never_open_production_state(tmp_path, monkeypatch):
    import synchri.broker.broker as broker_module
    from synchri.cli.main import main
    from synchri.research.store import RunStore

    home = tmp_path / "synchri-home"
    monkeypatch.setenv("SYNCHRI_HOME", str(home))

    def refuse(*_args, **_kwargs):
        raise AssertionError("a research command constructed a Broker")

    monkeypatch.setattr(broker_module.Broker, "__init__", refuse)
    store = RunStore.create(tmp_path / "run")
    store.append("relay.halted", {"reason": "t", "detail": ""}, actor="relay")
    store.close()
    assert main(["research", "verify", str(tmp_path / "run")]) == 0
    assert main(["research", "verify", str(tmp_path / "absent")]) == 12
    assert not home.exists(), "SYNCHRI_HOME must not be created by research commands"


def test_claude_stream_facts():
    stream = b"\n".join(json.dumps(e).encode() for e in [
        {"type": "system", "subtype": "init", "model": "claude-requested", "session_id": "s1"},
        {"type": "assistant", "message": {"model": "served", "content": [
            {"type": "text", "text": "4"}, {"type": "tool_use", "name": "Bash"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "4",
         "usage": {"input_tokens": 3}},
    ])
    facts = parse_claude(stream)
    assert facts.final_text == "4" and facts.model_reported == "claude-requested"
    assert facts.is_error is True, "is_error is read, not subtype (PC-12)"
    assert facts.tool_events == 1 and facts.provider_session_id == "s1"


def test_codex_stream_facts():
    stream = b"\n".join(json.dumps(e).encode() for e in [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "4"}},
        {"type": "turn.completed", "usage": {"input_tokens": 3}},
    ])
    facts = parse_codex(stream)
    assert facts.final_text == "4" and facts.model_reported is None
    assert facts.usage == {"input_tokens": 3} and facts.provider_session_id == "t1"
    assert parse_codex(b'{"type":"turn.failed"}').is_error is True
