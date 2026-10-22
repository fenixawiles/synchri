"""``synchri research`` — the Research Mode command-line entry point.

Research commands never construct a Broker or touch ``SYNCHRI_HOME`` (spec D5,
PRO-01); :func:`synchri.cli.main.main` dispatches them before any production
state is opened.

    synchri research verify <run-dir>
    synchri research relay --run-dir D --credentials FILE --socket PATH \\
        [--control-socket PATH] [--anthropic-upstream URL] [--openai-upstream URL] \\
        [--ca-bundle F] [--proxy H:P]
    synchri research conformance --runtime claude_code|codex --cli-root DIR \\
        --cli-relpath NAME --cli-sha256 HEX --cli-version V --image REF@sha256:… \\
        --model ID --run-dir D
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from .errors import ResearchError

DEFAULT_UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}


def add_parser(command) -> None:
    research = command("research", "Research Mode infrastructure (isolated from all sessions).")
    sub = research.add_subparsers(dest="research_command", metavar="<research-command>")

    verify = sub.add_parser("verify", help="verify the hash chain and blobs, and rebuild every invocation")
    verify.add_argument("run_dir")

    relay = sub.add_parser("relay", help="serve the recording relay for one run")
    relay.add_argument("--run-dir", required=True, help="new run directory for the event store")
    relay.add_argument("--credentials", required=True,
                       help="absolute path to the dedicated, owner-only credentials file")
    relay.add_argument("--socket", required=True, help="Unix socket path (short, e.g. /tmp/srr-x/relay.sock)")
    relay.add_argument("--provider", action="append", choices=sorted(DEFAULT_UPSTREAMS),
                       help="provider to serve (repeatable; default: every provider in the file)")
    relay.add_argument("--anthropic-upstream", default=DEFAULT_UPSTREAMS["anthropic"])
    relay.add_argument("--openai-upstream", default=DEFAULT_UPSTREAMS["openai"])
    relay.add_argument("--ca-bundle", help="CA bundle for upstream TLS (default: certifi)")
    relay.add_argument("--proxy", help="HTTP CONNECT proxy host:port the relay must use")
    relay.add_argument("--control-socket",
                       help="owner-only (0600) socket where an orchestrator in another process "
                            "obtains per-invocation grants; it never sees a credential")
    relay.add_argument("--allow-loopback-http-upstream", action="store_true",
                       help="testing only: permit plain-HTTP upstreams on 127.0.0.1 (mock providers)")

    conf = sub.add_parser("conformance",
                          help="run provider conformance checks VT-28..VT-31, VT-33 (mock upstream)")
    conf.add_argument("--runtime", required=True, choices=["claude_code", "codex"])
    conf.add_argument("--cli-root", required=True, help="host directory mounted read-only as /opt/cli")
    conf.add_argument("--cli-relpath", required=True, help="binary path inside --cli-root")
    conf.add_argument("--cli-sha256", required=True, help="pinned SHA-256 of the binary")
    conf.add_argument("--cli-version", required=True, help="pinned CLI version string")
    conf.add_argument("--image", required=True, help="digest-pinned container image")
    conf.add_argument("--model", required=True, help="full model ID to request")
    conf.add_argument("--run-dir", required=True, help="new run directory for the evidence")
    conf.add_argument("--timeout", type=float, default=300.0)


def run(args: argparse.Namespace) -> int:
    try:
        handler = {
            "verify": _verify, "relay": _relay, "conformance": _conformance,
        }.get(args.research_command)
        if handler is None:
            print("usage: synchri research {verify,relay,conformance} ...", file=sys.stderr)
            return 2
        return handler(args)
    except ResearchError as exc:
        payload = exc.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(payload), file=sys.stderr)
        else:
            print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return exc.exit_code


def _emit(args, payload: dict, text: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else text)


def _verify(args) -> int:
    from .store import RunStore

    from .reconstruct import reconstruct

    store = RunStore.open(Path(args.run_dir))
    chain = store.verify()
    rebuilt = reconstruct(store)
    lines = [f"events: {chain.events}  blobs checked: {chain.blobs_checked}",
             f"chain head: {chain.chain_head}",
             f"invocations rebuilt: {len(rebuilt.invocations)}",
             "OK: chain, triggers, required fields, blobs, and every invocation verify"
             if rebuilt.ok else "FAILED:\n  " + "\n  ".join(rebuilt.problems),
             "not yet applicable in this phase: " + "; ".join(rebuilt.not_yet_applicable)]
    _emit(args, {"chain": chain.to_dict(), "reconstruction": rebuilt.to_dict()}, "\n".join(lines))
    return 0 if rebuilt.ok else 12


def _relay(args) -> int:
    from .credentials import CredentialVault
    from urllib.parse import urlsplit

    from .errors import RelayError
    from .relay import RecordingRelay, Upstream
    from .store import RunStore

    vault = CredentialVault.load(Path(args.credentials))
    providers = args.provider or vault.providers()
    urls = {"anthropic": args.anthropic_upstream, "openai": args.openai_upstream}
    upstreams = {p: Upstream(urls[p], ca_bundle=args.ca_bundle, proxy=args.proxy) for p in providers}
    if args.allow_loopback_http_upstream:
        for p, upstream in upstreams.items():
            parsed = urlsplit(upstream.base_url)
            if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
                raise RelayError(f"--allow-loopback-http-upstream covers loopback only, not {p}: "
                                 f"{upstream.base_url}")
    store = RunStore.create(Path(args.run_dir))
    relay = RecordingRelay(store, vault, upstreams, socket_path=Path(args.socket),
                           control_socket_path=args.control_socket,
                           allow_insecure_upstream=args.allow_loopback_http_upstream).start()
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    _emit(args, {"run_id": store.run_id, "socket": str(args.socket), "providers": providers,
                 "control_socket": args.control_socket},
          f"relay serving run {store.run_id} on {args.socket} for {', '.join(providers)}; Ctrl-C stops")
    done.wait()
    relay.stop("operator stop")
    return 0


def _conformance(args) -> int:
    from .conformance import run_conformance
    from .invocation import CliConfig

    cli = CliConfig(runtime=args.runtime, cli_root=Path(args.cli_root),
                    cli_relpath=args.cli_relpath, cli_sha256=args.cli_sha256,
                    cli_version=args.cli_version)
    report = run_conformance(runtime=args.runtime, cli=cli, image=args.image, model=args.model,
                             run_dir=Path(args.run_dir), timeout_s=args.timeout)
    lines = [f"{c.vt}  {'PASS' if c.passed else 'FAIL'}  {c.name}" for c in report.checks]
    lines.append(f"store verified: {report.store_verified}")
    lines.append(f"report: {Path(args.run_dir) / 'conformance-report.json'}")
    _emit(args, report.to_dict(), "\n".join(lines))
    return 0 if report.passed else 1
