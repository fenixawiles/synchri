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

The interface is editorial — Palatino prose on layered surfaces, with
monospace reserved for data (paths, branches, statuses, countdowns, logs).
The page is built on a small elevation ladder: the environment carries a
single collapsible sidebar; the working area rides on it as one raised
sheet; inspectable surfaces (the setup sheet, the session inspector) slide
over that; menus, dialogs, and toasts float above everything. By default the
palette follows the OS light/dark preference; the sidebar's **Theme** picker
offers ten explicit palettes spanning the spectrum — dark: Graphite
(charcoal, emerald), Midnight (slate, blue), Ember (warm, coral red),
Copper (umber, burnt orange), Orchid (violet); light: Daylight (instrument
green), Sage (warm paper, the original Synchri green), Solar (parchment,
gold), Harbor (grey-blue), Iris (lavender, indigo) — kept by Synchri
itself, so every launch and every window agrees.

**The workspace** — where you land. If a session is active, Synchri opens
straight into its conversation; otherwise a calm blank workspace offers a
brief composer ("What should the team build?"), one-click quick-starts for
saved workflows, and a quiet **Now** strip for anything that needs you.
The sidebar carries workflows (run, edit, rename, delete, and **+ New**)
and recent sessions — status LED, capped at six with a show-all toggle,
each row's **⋯** menu able to rename or (once finished) delete. **Agent
connections** live in a dialog off the sidebar foot.

**New session** — typing a brief on the workspace (or pressing New
session) raises a **setup sheet** that asks one question at a time:
repository → workspace → brief (or "Help me make a plan") → team → Ready.
Answered steps collapse to one-line summaries with an Edit affordance.
Repositories are discovered, not typed: existing git checkouts under the
usual code directories, plus your GitHub repositories once access is
granted. The workspace step is mandatory and explicit: a new isolated
worktree and every existing worktree are offered with nothing preselected,
and the session cannot be created until one is deliberately chosen.

Permissions are three-state toggles — **Allow / Ask / Deny** — with risk labels
on anything high or destructive, and the description of what each one actually
permits next to it. Profile cards highlight the one currently in effect;
fine-tuning any capability clears the highlight, because the result is no
longer any named profile.

**Preflight** — the session assembling inside its own shell: the session's
name in the header, and one checklist where the conversation will be — each
agent's connection state (not connected → reading agreement → ready) with
its setup prompt a disclosure away, and an action bar whose single primary
is **Start my agents** for tools Synchri can launch itself, or **Begin
collaboration** once every agent has acknowledged the contract. Activation
fills the same shell with the live room.

**The session** — the sidebar becomes the session rail (status, timebox
countdown, gate progress, tools grouped under Work · Context · Record, team
presence with each agent's live runtime state, controls) beside the
conversation. Messages are written in a wrapping composer — Enter sends,
Shift+Enter starts a new line — and while Claude Code or Codex works, a live
feed shows its actual stream: status lines, reasoning snippets, commands with
exit codes, a notice when it compacts its context, and one collapsible diff
card per file it touches, updating as the edit happens. Runtimes without a
machine-readable stream keep the cooperative activity note. An agent that
crashes, times out, or stops responding is flagged with a banner and a
one-click **Restart**; two consecutive hard failures drop it and raise an
escalation instead of burning further turns. Detail tabs slide over the
conversation as an inspector panel — the chat stays visible and live
beneath it, and the scrim, Close, or Escape return to it:

| Tab | Shows |
|---|---|
| Conversation | Attributed, timestamped messages, the live work feed, and the composer — you always outrank the queue |
| Gates | Every acceptance gate, its status, the evidence behind it, and an add-gate control |
| Tests | Run the project's own suite in the worktree; real counts, real output |
| Changes | One collapsible card per changed file — committed, uncommitted, and untracked — with added/removed coloring |
| Commits | What landed on the session branch, dated, with ids linking to the commit on GitHub |
| Worktree | Where agents are working, and confirmation it is not your primary tree |
| Memory | The shared ledger, verbatim |
| Raw | The event log: every state transition with actor and timestamp |
| Contract | The exact text, each agent's acknowledgment, and the Activate button |

The principle from §18 holds: normal view is what is happening, the tabs are why,
and Raw is exactly what happened.

## Controls

Pause, resume, and stop are always available; stopping terminates the agents'
whole process trees, including the tool processes their CLIs spawned.
**Complete session** verifies the gates: when some are unmet it lists them and
offers an explicit "complete anyway", which records a waiver on each remaining
gate and in the final changelog rather than pretending they passed. Changing
permissions, the spec, or the deadline from the UI issues a **new contract
revision** and drops the session back to awaiting acknowledgment — the
dashboard shows who has agreed and who has not. Nothing changes silently.

Every finished session offers a **session package**: one zip with the rendered
transcript (plus raw JSONL), the final changelog, the gates record, a usage
summary (per-agent tokens, cache, cost, and time where the runtime reported
them), the commit list, and the full diff. Deleting a session removes it from
Synchri but never loses git history — the worktree is removed only when it is
clean and fully pushed, and remote branches are never touched.

## Limitations

- **The stream is per-tab.** Each open tab holds a server-sent-events
  connection and a SQLite reader — fine locally, not a fan-out design.
- **Drafts are durable, coordination is best-effort.** Unfinished setup drafts
  persist across restarts and sync across tabs via the stream, but two tabs
  editing the same draft simultaneously still race on last-write-wins.
- **Theme choice is per workspace, not per browser.** It is stored by Synchri
  (the app's loopback port — and so its browser origin — changes every
  launch, which rules out browser storage), so every window shows the same
  pick; with no pick, all follow the OS.
