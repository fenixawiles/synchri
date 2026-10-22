"""The in-container launcher: the only Synchri code that runs inside a container.

It is written, with its launch plan, into a fresh per-invocation directory that
is mounted read-only at ``/opt/synchri``. Inside the container it:

1. bridges ``127.0.0.1:<port>`` to the mounted relay socket, so the CLI can use
   an ordinary ``http://`` base URL while the container has no network at all
   (``--network none``; spec ISO-05, D6);
2. creates the plan's directories and files in the fresh tmpfs home;
3. runs the CLI with the prompt on stdin, passing stdout and stderr through;
4. after the CLI exits, reports which files under the tmpfs home and working
   directory contain any of the plan's persistence probes (VT-31), as a single
   delimited line at the end of stderr.

The source below targets the container image's Python (3.11), stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path

from .canonical import sha256_hex

INVENTORY_MARKER = "@@SYNCHRI-RESEARCH-INVENTORY@@"
TOKEN_PLACEHOLDER = "@@SYNCHRI-RELAY-TOKEN@@"
TOKEN_SOURCE_ENV = "RESEARCH_GRANT_TOKEN"

LAUNCHER_SOURCE = r'''
import json, os, socket, subprocess, sys, threading

MARKER = "@@SYNCHRI-RESEARCH-INVENTORY@@"
with open("/opt/synchri/launch.json", encoding="utf-8") as fh:
    PLAN = json.load(fh)

def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        return
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            return

def serve(listener):
    while True:
        client, _ = listener.accept()
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(PLAN["relay_socket"])
        except OSError:
            client.close()
            continue
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", PLAN["bridge_port"]))
listener.listen(16)
threading.Thread(target=serve, args=(listener,), daemon=True).start()

for directory in PLAN["dirs"]:
    os.makedirs(directory, mode=0o700, exist_ok=True)
for path, text in PLAN["files"].items():
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)

env = {}
for name, value in PLAN["env"].items():
    # The relay token arrives in the container's own environment (docker run
    # -e NAME, value from the orchestrator's process), never on disk or argv.
    env[name] = os.environ[PLAN["token_source"]] if value == PLAN["token_placeholder"] else value
os.environ.pop(PLAN["token_source"], None)
proc = subprocess.run(PLAN["argv"], env=env, cwd=PLAN["cwd"])

inventory = {"files": [], "probe_hits": {}}
for root in PLAN["inventory_roots"]:
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            inventory["files"].append({"path": path, "size": len(data)})
            for probe in PLAN["persistence_probes"]:
                if probe.encode("utf-8") in data:
                    inventory["probe_hits"].setdefault(probe, []).append(path)
sys.stderr.flush()
sys.stderr.write("\n" + MARKER + " " + json.dumps(inventory) + "\n")
sys.stderr.flush()
sys.exit(proc.returncode)
'''


def write_launcher(directory: Path, plan: dict) -> dict[str, str]:
    """Write the launcher and its plan; return {filename: sha256} for the record."""
    directory.mkdir(mode=0o755, parents=True, exist_ok=False)
    directory.chmod(0o755)
    files = {
        "launcher.py": LAUNCHER_SOURCE.encode("utf-8"),
        "launch.json": json.dumps(plan, sort_keys=True).encode("utf-8"),
    }
    for name, data in files.items():
        path = directory / name
        path.write_bytes(data)
        path.chmod(0o644)  # the container's non-root UID must read it
    return {name: sha256_hex(data) for name, data in files.items()}


def split_inventory(stderr: bytes) -> tuple[bytes, dict | None]:
    """Separate the launcher's trailing inventory line from the CLI's stderr."""
    marker = ("\n" + INVENTORY_MARKER + " ").encode("utf-8")
    index = stderr.rfind(marker)
    if index < 0:
        return stderr, None
    line = stderr[index + len(marker):].strip()
    try:
        return stderr[:index], json.loads(line.decode("utf-8"))
    except ValueError:
        return stderr, None
