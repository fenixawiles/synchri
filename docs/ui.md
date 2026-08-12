# The Synchri app

`synchri ui` starts a local server, opens your browser, and gives you the wizard
and a live dashboard. It is a desktop app in the way that matters — you click
through it, it is fast, it is yours — without shipping a browser engine.

```bash
cd ~/your-project
synchri ui
```

```
Synchri is running at

    http://127.0.0.1:8765/?token=…

Press Ctrl+C to stop.
```

## Why a local server and not Electron

Electron or Tauri would mean a 100&nbsp;MB download, a build toolchain, a signing
certificate per platform, and a second language in the repo. Serving a page to
the browser you already have costs a 33&nbsp;KB HTML file and keeps the whole
project a zero-dependency Python wheel. §22's local-first rule stays intact:
nothing is hosted, nothing phones home, the page loads no external resource.

The trade is real and worth stating: it is a browser tab, not a dock icon. If
that matters later, the same server can be wrapped in Tauri without changing a
line of the API.

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

**The wizard** — mode, repository, worktree, agents, permissions, spec, deadline,
review. Progressive disclosure: each step shows only its own decisions, you can
jump back to any earlier step, and "Start session" stays disabled with the
reasons listed until everything it needs is there.

Repositories are discovered, not typed: local git checkouts under the usual code
directories, plus your GitHub repositories if `gh` is signed in. GitHub entries
that are not cloned yet are shown but not selectable, because Synchri works on a
local worktree.

Permissions are three-state toggles — **Yes / Ask / No** — with risk labels on
anything high or destructive, and the description of what each one actually
permits next to it.

**The dashboard** — tiles across the top (status, time remaining, current actor,
gates passed, tests, commits, blockers, whether you are needed), then tabs:

| Tab | Shows |
|---|---|
| Conversation | Attributed messages, and a box to cut in — you always outrank the queue |
| Gates | Every acceptance gate, its status, and the evidence behind it |
| Tests | Run the project's own suite in the worktree; real counts, real output |
| Changes | The actual diff against the base branch |
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

- **Polling, not push.** The dashboard refreshes every 4 seconds. Fine locally;
  a websocket would be better and is a small change.
- **One browser at a time is assumed.** Two tabs work, but they do not
  coordinate their wizard drafts.
- **Wizard drafts live in memory.** Restarting `synchri ui` loses an unfinished
  wizard. Started sessions are fully durable — that is the part that matters.
- **No dark/light toggle.** It follows your OS.
