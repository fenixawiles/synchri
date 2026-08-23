# Synchri — working notes for agents

Synchri orchestrates multiple coding agents (Claude Code, Codex, Copilot, …)
on one task in one room, each in an isolated git worktree.

## Architecture

- Python ≥ 3.10, **stdlib-only** backend (plus `certifi`). SQLite at
  `~/.synchri/synchri.db`; schema in `synchri/storage/schema.sql`, applied
  idempotently (plus additive `ALTER`s in `db.py`) on every boot.
- The **entire frontend is one file**: `synchri/ui/static/app.html` — inline
  CSS design tokens + vanilla JS. No framework, no build step, no npm
  frontend deps. Served by `synchri/ui/server.py`; JSON API in
  `synchri/ui/api.py`.
- `desktop/` is a thin Tauri v2 macOS shell (spawns the bundled Python
  engine, hosts the webview, handles auto-update). Business logic never
  goes in Rust.
- The old wizard flow inside `app.html` is retired but intentionally kept —
  do not modify or remove it.

## Conventions

- Tests: `pip install -e ".[dev]"` then `pytest -q`. Keep the suite green at
  every commit; new behavior needs tests.
- The version lives in **seven places** that must match: `pyproject.toml`,
  `synchri/__init__.py`, `desktop/package.json`, `desktop/package-lock.json`
  (two entries), `desktop/src-tauri/tauri.conf.json`,
  `desktop/src-tauri/Cargo.toml`, and the `synchri-desktop` entry in
  `desktop/src-tauri/Cargo.lock` — `tests/test_desktop_release.py` enforces
  the lockstep.
- Releases: pushing a `v*` tag runs `.github/workflows/release.yml`
  (build, sign, notarize, publish updater feed).
- All user-visible strings are escaped via `esc()`/`rich()` in `app.html`;
  new UI uses the existing CSS custom-property tokens and helpers
  (`$()`, `guard()`, `api()`, `flash()`).

## Current in-flight update

See `docs/updates/` for the active update's baseline, scope checklist, and
decisions — currently `docs/updates/v0.4.5.md`.
