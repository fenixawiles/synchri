"""The event catalog (spec §6.2): every event type and its required payload.

A write whose type is not listed here, or whose payload omits a required
field, is refused (VT-09). A required field may hold ``null`` where the spec
allows it; it may not be absent. Fields listed under ``blobs`` hold a SHA-256
reference into ``research_blobs`` (or ``null``), and the store checks that the
referenced blob exists before appending.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventSpec:
    required: tuple[str, ...]
    blobs: tuple[str, ...] = ()


def _spec(required: str, blobs: str = "") -> EventSpec:
    return EventSpec(tuple(required.split()), tuple(blobs.split()))


_RUN_TRANSITION = _spec("reason trials_terminal trials_remaining budget_snapshot")
_INVOCATION_END = _spec(
    "returncode timed_out cancelled wall_s monotonic_elapsed_ns failure_kind"
)

CATALOG: dict[str, EventSpec] = {
    # -- run ------------------------------------------------------------
    "run.manifest_frozen": _spec(
        "manifest manifest_sha256 synchri_version git_commit git_dirty git_diff "
        "python_version sqlite_version platform item_file_sha256 item_count "
        "reference_file_sha256 prompt_templates preregistration_ref isolation_tier",
        "manifest git_diff",
    ),
    "run.preflight_passed": _spec("slots failures"),
    "run.preflight_failed": _spec("slots failures"),
    "run.schedule_materialized": _spec("seed schedule schedule_sha256", "schedule"),
    "run.started": _RUN_TRANSITION,
    "run.generation_complete": _RUN_TRANSITION,
    "run.halted": _RUN_TRANSITION,
    "run.archived": _spec("export_path export_sha256 chain_head"),
    # -- trial ----------------------------------------------------------
    "trial.started": _spec("item_id condition_id replicate block slots letters"),
    "round.sealed": _spec("round responses seal_sha256"),
    "trial.final_sealed": _spec("final_responses rounds_completed"),
    "trial.aborted": _spec("reason round_reached detail"),
    # -- invocation -----------------------------------------------------
    "invocation.prepared": _spec(
        "template_id template_version template_sha256 prompt prompt_sha256 prompt_chars "
        "inputs argv cwd env_names env_value_sha256 timeout_s budget_before",
        "prompt",
    ),
    "invocation.isolation": _spec(
        "runtime_kind image_ref container_argv mounts network limits "
        "cli_binary_sha256 cli_version"
    ),
    "invocation.launched": _spec("pid started_monotonic_ns"),
    "invocation.exited": _INVOCATION_END,
    "invocation.failed": _INVOCATION_END,
    "invocation.captured": _spec(
        "stdout stderr stream stdout_truncated final_text final_text_source "
        "model_reported provider_session_id usage canary_scan tool_events_seen",
        "stdout stderr stream final_text",
    ),
    "response.recorded": _spec(
        "response_record response_sha256 format_valid parse_error answer abstain "
        "confidence rationale source_invocation_id",
        "response_record rationale",
    ),
    # -- budget, isolation, derived, operator ---------------------------
    "budget.charged": _spec(
        "invocation_id tokens_in tokens_out cached_in cost_usd wall_s prompt_chars "
        "output_chars totals_trial totals_condition totals_run"
    ),
    "budget.exhausted": _spec("scope limit_name limit value"),
    "isolation.violation_detected": _spec("invocation_id kind locations evidence", "evidence"),
    "derived.disagreement": _spec("round metric_version pairwise any_disagreement"),
    "operator.action": _spec("operator action reason targets"),
    # -- evaluation -----------------------------------------------------
    "evaluation.started": _spec(
        "evaluator_version scorer_id scorer_version scorer_sha256 reference_file_sha256"
    ),
    "evaluation.scored": _spec(
        "trial_id slot round response_sha256 outcome match_detail scorer_version"
    ),
    "evaluation.completed": _spec("scored_count excluded chain_head"),
    # -- recording relay (PC-01) ------------------------------------------
    "relay.started": _spec("relay_version listen providers credential_fingerprints"),
    "relay.grant_issued": _spec(
        "grant_id invocation_id provider model allowed_tools max_requests "
        "policy_sha256 token_sha256 expires_at"
    ),
    "relay.request_received": _spec(
        "grant_id request_index method path headers request request_sha256 "
        "content_encoding model tool_names decision",
        "request",
    ),
    "relay.request_rejected": _spec("grant_id request_index reasons status_returned"),
    "relay.upstream_response": _spec(
        "grant_id request_index status headers response response_sha256 served_model "
        "usage duration_ms truncated",
        "response",
    ),
    "relay.upstream_error": _spec("grant_id request_index error_kind detail"),
    "relay.halted": _spec("reason detail"),
}


def spec_for(event_type: str) -> EventSpec | None:
    return CATALOG.get(event_type)
