# The Synchri app

Opening the signed `Synchri.app` starts a native macOS window and a live
dashboard. `synchri ui` remains available for developers and automation: it
starts that same loopback interface in the browser.

```bash
cd ~/your-project
synchri ui
```

```
Synchri is running at

    http://127.0.0.1:8765/?token=…

Press Ctrl+C to stop.
```

## Why the native app still uses a local server

The signed macOS app uses a small Tauri shell to provide the dock icon, native
window, notarized DMG, and verified updates users expect. Its bundled Synchri
engine still serves the exact same loopback-only, token-gated page. The Python
package stays a zero-dependency CLI for terminals and automation. §22's
local-first rule stays intact: nothing is hosted, nothing phones home, and the
page loads no external resource.

The native wrapper owns no room state and opens no listening socket. It merely
starts the bundled engine, reads its private launch URL, and presents that page
inside the application window.

## This is the one socket Synchri opens

The broker still binds nothing — a test asserts it never calls `socket.bind`.
The UI is opt-in and separate, and it is locked down:

| Control | Behaviour |
|---|---|
| Bind address | `127.0.0.1` only. `--host` requires `--allow-remote`, which refuses otherwise and warns loudly when used. |
| Token | 256 random bits per launch, in the URL Synchri prints, compared with `hmac.compare_digest`. |
| Token storage | Moved into a `SameSite=Strict; HttpOnly` cookie on first load, so it leaves the address bar. |
| Origin | `Origin` must match `Host`; a page on another site cannot drive the API. |
| Content policy | `default-src 'none'`, `connect-src 'self'`, `frame-ancestors 'none'`. Everything is inlined; the page cannot reach the network. |
| Without the token | You get a short page explaining how to launch it — never the app. |

Stopping the server ends nothing. Sessions are on disk; `synchri ui` again picks
up exactly where you were.

## What you see

The interface is mono-forward — an instrument panel, not a marketing page.
Data (paths, branches, statuses, countdowns, logs) renders in monospace; prose
stays prose. By default it follows the OS light/dark preference; the header's
**theme picker** offers explicit palettes — Terminal (dark graphite, emerald),
Midnight (dark slate, blue), Daylight (light instrument), and Sage (warm light,
the original Synchri green) — stored in the browser, never on a server.

**Home** — sessions first: a dense list of every collaboration in this
workspace with a live status LED, repository and branch, and the first line of
its brief. Beside it, a compact **workflows** panel: your saved defaults (agent
team, permission ceiling, pacing), each with a one-click **Run**. Configuration
is something you edit when you choose to, not a gauntlet you repeat per
session.

**New session** — a numbered form (workflow, repository, brief, pacing) beside
a sticky **launch plan** that summarizes what will run and carries the page's
single call to action. Repositories are discovered, not typed: existing git
checkouts under the usual code directories, plus your GitHub repositories once
access is granted. Gate IDs in the brief (like `AUTH-01`) become individually
tracked acceptance gates.

Permissions are three-state toggles — **Allow / Ask / Deny** — with risk labels
on anything high or destructive, and the description of what each one actually
permits next to it.

**Preflight** — one checklist: each agent's connection state (not connected →
reading agreement → ready) with its setup prompt a disclosure away, and an
action bar whose single primary is **Start my agents** for tools Synchri can
launch itself, or **Begin collaboration** once every agent has acknowledged the
contract.

**The session** — a fixed rail (status, timebox countdown, gate progress, team
presence, controls) beside the conversation. Detail tabs open as a full working
surface over the chat:

| Tab | Shows |
|---|---|
| Conversation | Attributed, timestamped messages and a compose line — you always outrank the queue |
| Gates | Every acceptance gate, its status, and the evidence behind it |
| Tests | Run the project's own suite in the worktree; real counts, real output |
| Changes | The actual diff against the base branch, with added/removed line coloring |
| Commits | What landed on the session branch |
| Worktree | Where agents are working, and confirmation it is not your primary tree |
| Memory | The shared ledger, verbatim |
| Raw | The event log: every state transition with actor and timestamp |
| Contract | The exact text, each agent's acknowledgment, and the Activate button |

The principle from §18 holds: normal view is what is happening, the tabs are why,
and Raw is exactly what happened.

## Controls

Pause, resume, and stop are always available. Changing permissions, the spec, or
the deadline from the UI issues a **new contract revision** and drops the session
back to awaiting acknowledgment — the dashboard shows who has agreed and who has
not. Nothing changes silently.

## Limitations

- **The stream is per-tab.** Each open tab holds a server-sent-events
  connection and a SQLite reader — fine locally, not a fan-out design.
- **Drafts are durable, coordination is best-effort.** Unfinished setup drafts
  persist across restarts and sync across tabs via the stream, but two tabs
  editing the same draft simultaneously still race on last-write-wins.
- **Theme choice is per browser profile.** It lives in `localStorage`, so the
  native app window and a separate browser tab each remember their own pick;
  with no pick, both follow the OS.
