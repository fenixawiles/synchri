# Shipping Synchri

Synchri is distributed as a **local agent room**, not as a Python project a
user is expected to understand. The product promise is intentionally narrow:

> Download Synchri once. Choose a repository and the coding tools already on
> your Mac. Synchri creates an isolated workspace and starts the collaboration.

Synchri never asks for model API keys, hosts a relay, or charges for model use.
The user keeps using the Codex, Claude Code, Copilot, or other account they
already have.

## The supported first-run path

1. Download the signed `Synchri.app` that matches the Mac's processor from the
   GitHub release linked at `synchri.com/download`.
2. Move it to Applications and open it. No Python, package manager, shell PATH,
   account, or repository clone is required.
3. Synchri checks Git and the local coding tools it can find. It calls a tool
   *ready* only when it can launch it; provider authentication and contract
   agreement are verified when the session starts.
4. Choose a local repository (or a GitHub repository to clone), two agents,
   and the goal. Click **Start agents**.
5. Synchri creates the worktree and room, gives each managed agent its local
   identity, asks each to accept the exact contract, and only then activates
   the builder. The human sees the chat, live work trail, and any genuine
   decision request.

The browser interface is still loopback-only. The application is local; no
project data, transcript, room token, or agent output is sent to Synchri.

## Managed and external agents

**Managed** is the default whenever Synchri recognizes an installed tool and a
maintained unattended command. Synchri can then prove the whole sequence:
executable found → agent attached → contract accepted → opening turn started.
The maintained defaults use Codex `exec`, Claude Code print mode, and Copilot
CLI prompt mode with its quiet-response flag. Synchri never adds a provider's
dangerous “allow everything” option; provider sign-in and approval controls
remain a separate ceiling.

**External** is an honest fallback for a tool Synchri cannot launch yet. The UI
shows one copyable prompt for that agent and does not claim it is connected
until it actually joins and acknowledges. This keeps new providers useful
without lowering the reliability bar for the default path.

An external agent is never silently promoted to managed. Adding a provider
adapter means adding a maintained command, a readiness check, and an end-to-end
test before it appears behind **Start agents**.

## Release channels

| Audience | Channel | Promise |
|---|---|---|
| Most Mac users | Signed, notarized `Synchri.app` | Download, open, start a room |
| Developers and automation | PyPI wheel | `pipx install synchri` or the provided installer |
| Source users | GitHub source archive | Reproducible source and tests |

The source installer is intentionally a secondary option. It is useful on
Linux, CI, and for contributors; it is not the onboarding story on the site.

## Release checklist

- Tag a tested version as `vX.Y.Z`.
- The release workflow builds the Apple Silicon application and a wheel, plus
  SHA-256 checksums. Intel builds are intentionally released independently so
  limited legacy runner capacity cannot delay a signed public download.
- Configure these repository secrets before calling the Mac download supported:
  `MACOS_CERTIFICATE_P12_BASE64`, `MACOS_CERTIFICATE_PASSWORD`,
  `MACOS_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, and
  `APPLE_TEAM_ID`. The workflow signs, notarizes, staples, then re-archives the
  application. An unsigned build is for local development only.
- Configure PyPI trusted publishing, then use the workflow's explicit
  **publish_pypi** option. A GitHub release does not silently publish to PyPI.
- Point `synchri.com/download` at the current GitHub release; managed TLS from
  the hosting provider is sufficient—there is no separate certificate purchase
  for the download page.

## The recovery rule

There is one recovery command: `synchri doctor`. It reports the workspace,
Git, and each supported local agent as **ready**, **external**, or **not found**
without starting a room or changing the machine. The UI presents the same
information before a session starts.

If a managed run ends because the UI closes, the room, contract, worktree,
messages, and agent identities are already durable. Reopening Synchri shows
**Continue agents** for that same session; no setup is recreated or lost, and
the user consciously resumes model work rather than the app spending usage on
open.
