"""Per-invocation container execution (spec D4, ISO-01, ISO-02, ISO-05, PC-04).

Every participant invocation runs in a fresh container that:

* uses a digest-pinned image;
* has a read-only root filesystem, a non-root UID, no capabilities,
  ``no-new-privileges``, and process and memory limits;
* gets fresh tmpfs ``/work``, ``/home/slot``, and ``/tmp`` (destroyed with the
  container, ``--rm``);
* has **no network** (``--network none``); its only channel out is the relay's
  Unix socket, bind-mounted and bridged to loopback by the launcher;
* receives exactly three mounts, all read-only except the socket: the pinned
  CLI directory, the per-invocation launcher directory, and the relay socket.
  Any other mount, and any mount source that is or contains production state,
  is refused (:func:`validate`).

The environment is a replace-not-merge allowlist: the docker client is started
with a minimal environment of its own, the container receives only the relay
token (by name, value from that minimal environment), and the launcher hands
the CLI exactly the adapter's allowlisted variables.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..ids import new_secret
from .canonical import sha256_hex
from .credentials import forbidden_roots
from .errors import IsolationError
from .launcher import TOKEN_SOURCE_ENV
from .providers import CLI_MOUNT, HOME_DIR, LAUNCHER_MOUNT, RELAY_SOCKET, WORK_DIR

_PINNED_IMAGE = re.compile(r"^(?:[^\s@]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$")
_CREDENTIALISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE)", re.IGNORECASE)
_LAUNCHER_FILES = {"launcher.py", "launch.json"}
DEFAULT_UID = 10001


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def production_roots(environ=None) -> list[Path]:
    """Paths whose contents must never be mounted into a participant container."""
    env = os.environ if environ is None else environ
    roots = list(forbidden_roots(env))
    home = Path(env.get("HOME") or Path.home())
    roots += [home, Path("/root"), Path("/home"), Path("/etc"), Path("/var/run"),
              Path("/run"), Path("/proc"), Path("/sys"), Path("/dev")]
    return roots


def _overlaps(source: Path, root: Path) -> bool:
    """True if ``source`` is ``root``, inside it, or an ancestor of it."""
    try:
        source_r, root_r = source.resolve(), root.resolve()
    except OSError:
        source_r, root_r = source, root
    if source_r == Path("/"):
        return True
    for a, b in ((source_r, root_r), (root_r, source_r)):
        try:
            a.relative_to(b)
            return True
        except ValueError:
            continue
    return False


@dataclass
class ContainerSpec:
    image: str
    cli_root: Path
    cli_relpath: str
    cli_sha256: str
    launcher_dir: Path
    relay_socket: Path
    token: str
    #: Production locations to refuse in addition to :func:`production_roots`.
    extra_forbidden: tuple[Path, ...] = ()
    uid: int = DEFAULT_UID
    memory: str = "2g"
    pids: int = 256
    tmpfs_size: str = "64m"
    timeout_s: float = 900.0
    docker: str = "docker"
    name: str = field(default_factory=lambda: "synchri-research-" + new_secret(8).lower().replace("_", "x").replace("-", "y"))

    def mounts(self) -> list[tuple[Path, str, bool]]:
        """(host source, container target, read-only)."""
        return [
            (self.cli_root, CLI_MOUNT, True),
            (self.launcher_dir, LAUNCHER_MOUNT, True),
            (self.relay_socket, RELAY_SOCKET, False),
        ]


def validate(spec: ContainerSpec, *, environ=None, forbidden_values=()) -> None:
    """Refuse any specification that would weaken isolation. Raises IsolationError."""
    if not _PINNED_IMAGE.match(spec.image or ""):
        raise IsolationError(
            f"the container image must be pinned by digest (name@sha256:… or sha256:…), got {spec.image!r}"
        )
    if spec.uid == 0:
        raise IsolationError("participant containers never run as root")

    roots = production_roots(environ) + [Path(p) for p in spec.extra_forbidden]
    cli_root = Path(spec.cli_root)
    if not cli_root.is_absolute() or not cli_root.is_dir():
        raise IsolationError(f"the CLI root must be an existing absolute directory: {cli_root}")
    for root in roots:
        if _overlaps(cli_root, root):
            raise IsolationError(
                f"refusing to mount {cli_root}: it is, contains, or lies inside a production "
                f"or credential location ({root})"
            )
    binary = cli_root / spec.cli_relpath
    try:
        binary.resolve().relative_to(cli_root.resolve())
    except ValueError:
        raise IsolationError("the CLI binary must live inside the CLI root") from None
    if not binary.is_file():
        raise IsolationError(f"the pinned CLI binary is missing: {binary}")
    actual = file_sha256(binary)
    if actual != spec.cli_sha256:
        raise IsolationError(
            f"the CLI binary hash does not match the pin ({actual[:16]}… != {spec.cli_sha256[:16]}…)"
        )

    # The launcher directory is a leaf the orchestrator just wrote: exactly the
    # launcher and its plan, as regular files. Mounting it can expose nothing
    # else, wherever the run directory lives.
    launcher = Path(spec.launcher_dir)
    if launcher.is_symlink() or not launcher.is_dir():
        raise IsolationError(f"the launcher directory is not a plain directory: {launcher}")
    entries = list(launcher.iterdir())
    if {p.name for p in entries} != _LAUNCHER_FILES or not all(
        p.is_file() and not p.is_symlink() for p in entries
    ):
        raise IsolationError(
            f"the launcher directory must contain exactly {sorted(_LAUNCHER_FILES)} as regular files"
        )

    try:
        mode = os.stat(spec.relay_socket).st_mode
    except OSError as exc:
        raise IsolationError(f"the relay socket is missing: {spec.relay_socket}") from exc
    if not stat.S_ISSOCK(mode):
        raise IsolationError(f"the relay channel is not a socket: {spec.relay_socket}")

    if not spec.token.startswith("srt_"):
        raise IsolationError("the container may receive only a relay token, nothing credential-shaped")
    for value in forbidden_values:
        if value and (value in spec.token or value in spec.image or value in str(spec.cli_root)):
            raise IsolationError("a provider credential would reach the container")


def check_cli_environment(env: dict[str, str], allowed: frozenset[str], token_env: str,
                          forbidden_values=(), forbidden_fingerprints=()) -> None:
    """The CLI's environment: allowlisted names only, one token, no credential values."""
    extra = sorted(set(env) - set(allowed))
    if extra:
        raise IsolationError(f"environment variables outside the adapter allowlist: {extra}")
    for name, value in env.items():
        if name.startswith("SYNCHRI_"):  # ISO-01: none at all, the token included
            raise IsolationError(f"Synchri state variables never reach a participant: {name}")
        if _CREDENTIALISH.search(name) and name != token_env and not name.endswith("_TOKENS"):
            raise IsolationError(f"credential-shaped variable {name} is not the relay token")
        for secret in forbidden_values:
            if secret and secret in value:
                raise IsolationError(f"a provider credential would reach the container via {name}")
        # A remote relay shares only fingerprints: catch a credential passed whole.
        if forbidden_fingerprints and sha256_hex(value.encode("utf-8")) in forbidden_fingerprints:
            raise IsolationError(f"a provider credential would reach the container via {name}")


def build_argv(spec: ContainerSpec) -> list[str]:
    argv = [
        spec.docker, "run", "--rm", "-i", "--name", spec.name,
        "--network", "none",
        "--read-only",
        "--user", f"{spec.uid}:{spec.uid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(spec.pids),
        "--memory", spec.memory,
        "--tmpfs", f"{WORK_DIR}:rw,size={spec.tmpfs_size},uid={spec.uid},gid={spec.uid},mode=0700",
        "--tmpfs", f"{HOME_DIR}:rw,size={spec.tmpfs_size},uid={spec.uid},gid={spec.uid},mode=0700",
        "--tmpfs", f"/tmp:rw,size={spec.tmpfs_size},mode=1777",
        "--workdir", WORK_DIR,
        "-e", TOKEN_SOURCE_ENV,  # value from the docker client's environment, never argv
    ]
    for source, target, read_only in spec.mounts():
        argv += ["-v", f"{source}:{target}{':ro' if read_only else ''}"]
    argv += [spec.image, "python3", "-I", f"{LAUNCHER_MOUNT}/launcher.py"]
    return argv


def describe(spec: ContainerSpec) -> dict:
    """The invocation.isolation record: everything except secret values."""
    return {
        "image_ref": spec.image,
        "container_argv": build_argv(spec),
        "mounts": [
            {"target": target, "source_name": Path(source).name, "read_only": ro,
             "source_sha256": sha256_hex(str(Path(source).resolve()).encode())}
            for source, target, ro in spec.mounts()
        ],
        "network": {"mode": "none", "relay_channel": RELAY_SOCKET},
        "limits": {"uid": spec.uid, "memory": spec.memory, "pids": spec.pids,
                   "timeout_s": spec.timeout_s, "tmpfs_size": spec.tmpfs_size},
    }


@dataclass
class ContainerResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    wall_s: float
    monotonic_elapsed_ns: int
    pid: int | None


def run(spec: ContainerSpec, stdin: bytes, on_spawn=None) -> ContainerResult:
    """Run the container to completion. Kills it on timeout."""
    docker = shutil.which(spec.docker) or spec.docker
    argv = build_argv(spec)
    argv[0] = docker
    client_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        TOKEN_SOURCE_ENV: spec.token,
    }
    if os.environ.get("DOCKER_HOST"):
        client_env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    started_wall, started = time.time(), time.monotonic_ns()
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=client_env)
    if on_spawn is not None:
        try:
            on_spawn(proc.pid, started)
        except BaseException:
            # Recording the launch failed (LOG-03): never let the container run
            # unrecorded.
            subprocess.run([docker, "kill", spec.name], env=client_env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            proc.kill()
            proc.communicate()
            raise
    timed_out = False
    try:
        stdout, stderr = proc.communicate(stdin, timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run([docker, "kill", spec.name], env=client_env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        stdout, stderr = proc.communicate()
    return ContainerResult(
        returncode=proc.returncode, stdout=stdout, stderr=stderr, timed_out=timed_out,
        wall_s=round(time.time() - started_wall, 3),
        monotonic_elapsed_ns=time.monotonic_ns() - started, pid=proc.pid,
    )


def docker_available(docker: str = "docker") -> bool:
    path = shutil.which(docker)
    if not path:
        return False
    try:
        probe = subprocess.run([path, "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0
