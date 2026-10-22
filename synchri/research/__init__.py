"""Research Mode: isolated infrastructure for controlled multi-model studies.

This package is deliberately separate from the rest of Synchri. It never
touches ``SYNCHRI_HOME``, rooms, the broker, the turn queue, contracts, the
memory ledger, worktrees, Planning Mode, gates, or the dropbox (spec D1,
PRO-01). Its foundation is:

* :mod:`.store` — the append-only, hash-chained event log and blob store
  (spec §6, LOG-01, LOG-03);
* :mod:`.relay` — the trusted recording relay, the participant's only network
  channel and the only holder of provider credentials (D6, LOG-11, ISO-06);
* :mod:`.container` — per-invocation container execution (D4, ISO-05);
* :mod:`.conformance` — provider conformance checks (VT-28 to VT-33).

Experimental rounds, outcome scoring, and data collection are not part of
this phase.
"""

RESEARCH_SCHEMA_VERSION = 1
