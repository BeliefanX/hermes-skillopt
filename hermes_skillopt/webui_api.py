from __future__ import annotations

"""Server-side WebUI API contracts for Hermes SkillOpt.

All safety-sensitive decisions stay here/Python-side. The React frontend is only a
client for these functions and never bypasses staged-only runs or typed writeback
confirmations.
"""

import re
from typing import Any

from hermes_skillopt import core
from hermes_skillopt import webui as legacy


def _safe_json(data: Any) -> Any:
    """Return JSON-shaped data with secret-bearing fields redacted.

    This preserves useful status paths such as HERMES_HOME while removing
    token/password/authorization-shaped fields and common credential values.
    """
    sensitive_key = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)")
    token_value = re.compile(r"(?i)(bearer\s+[-A-Za-z0-9._+/=]{8,}|\b(?:sk|ghp|gho|xox[baprs])-[-A-Za-z0-9_]{12,}\b|\b(api[_-]?key|token|secret|password|passwd|authorization)\s*[:=]\s*[^\s,;]+)")

    def scrub(value: Any, key: str = "") -> Any:
        if sensitive_key.search(key):
            return "<REDACTED>"
        if isinstance(value, dict):
            return {str(k): scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, key) for v in value]
        if isinstance(value, tuple):
            return [scrub(v, key) for v in value]
        if isinstance(value, str):
            return token_value.sub("<REDACTED>", value)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return token_value.sub("<REDACTED>", str(value))

    return scrub(data)


def status(home: str | None = None) -> dict[str, Any]:
    return core.status(home or None)


def scout(home: str | None = None, skill: str | None = None, limit: int = 5) -> dict[str, Any]:
    return core.scout(home or None, skill=skill or None, limit=limit)


def run_full(payload: dict[str, Any]) -> dict[str, Any]:
    """Run full cycle from WebUI, always staged-only and never force-adopt."""
    intent = str(payload.get("intent") or "review").strip().lower()
    common = dict(
        skill=payload.get("skill") or None,
        query=payload.get("query") or None,
        eval_file=payload.get("eval_file") or None,
        lookback_days=int(payload.get("lookback_days") or 14),
        limit=int(payload.get("limit") or 50),
        iterations=int(payload.get("iterations") or 1),
        edit_budget=int(payload.get("edit_budget") or 3),
        candidate_count=int(payload.get("candidate_count") or 1),
        backend=payload.get("backend") or "auto",
        optimizer_backend=payload.get("optimizer_backend") or None,
        target_executor=payload.get("target_executor") or "auto",
        target_backend=payload.get("target_backend") or None,
        gate_mode=payload.get("gate_mode") or "soft",
        resume_run_id=payload.get("resume_run_id") or None,
        hermes_home_path=payload.get("home") or None,
    )
    if "allow_mock" in payload:
        common["allow_mock"] = bool(payload.get("allow_mock"))
    if intent in {"smoke", "review", "production"}:
        return core.guided_optimize(intent=intent, **common)
    if intent:
        raise ValueError("intent must be one of: smoke, review, production")
    out = core.full_run(
        **common,
        auto_adopt=False,
        force=False,
    )
    return out


def doctor(home: str | None = None, skill: str | None = None) -> dict[str, Any]:
    return core.doctor(home or None, skill=skill or None)


def review(run_id: str | None = None, home: str | None = None) -> dict[str, Any]:
    try:
        rid = (run_id or "").strip() or legacy.latest_run_id(home or None)
    except Exception as exc:
        return {"run_id": "", "summary": f"Review failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}", "report": "", "diff": "", "gate": "", "candidate": "", "rejected": "", "success": False}
    summary, report, diff, gate, candidate, rejected = legacy.review_payload(rid, home or None)
    decision: dict[str, Any] = {}
    review_json: dict[str, Any] = {}
    if not summary.startswith("Review failed:"):
        try:
            decision = core.review_decision_summary(rid or "latest", hermes_home_path=home or None) if rid else {}
            review_json = core.review(rid, hermes_home_path=home or None, slim=True) if rid else {}
        except Exception as exc:
            decision = {"success": False, "not_adoptable_reasons": [f"decision summary unavailable: {type(exc).__name__}: {core.redact_secrets(str(exc))}"], "adoptable": False, "production_gate_eligible": False, "test_gate_eligible": False, "next_action": "Inspect raw staged artifacts; integrity verification blocked the decision summary."}
            review_json = {}
    return {
        "run_id": rid,
        "summary": summary,
        "decision": decision,
        "review": review_json,
        "adoptable": decision.get("adoptable"),
        "blockers": decision.get("not_adoptable_reasons") or [],
        "production_gate": decision.get("production_gate_eligible"),
        "test_gate": decision.get("test_gate_eligible"),
        "evidence_class": "production_candidate" if decision.get("adoptable") else "review_only_or_not_ready",
        "artifacts": decision.get("artifact_refs") or {},
        "next_safe_action": decision.get("next_action"),
        "report": report,
        "diff": diff,
        "gate": gate,
        "candidate": candidate,
        "rejected": rejected,
        "success": not summary.startswith("Review failed:"),
    }


def review_latest(home: str | None = None) -> dict[str, Any]:
    return review("", home)


def fleet_report(home: str | None = None, limit: int = 50, skill: str | None = None) -> dict[str, Any]:
    return core.fleet_report(home or None, limit=limit, skill=skill or None)


def fleet_resume_plan(home: str | None = None, limit: int = 50, skill: str | None = None) -> dict[str, Any]:
    return core.fleet_resume_plan(home or None, limit=limit, skill=skill or None)


def fleet_rollback_plan(home: str | None = None, limit: int = 50, skill: str | None = None) -> dict[str, Any]:
    return core.fleet_rollback_plan(home or None, limit=limit, skill=skill or None)


def adopt(run_id: str, confirmation: str, force: bool = False) -> dict[str, Any]:
    rid = (run_id or "").strip()
    expected = f"ADOPT {rid}"
    if not rid:
        raise ValueError("run_id is required")
    if (confirmation or "").strip() != expected:
        raise PermissionError(f"type {expected!r} exactly to confirm")
    return core.adopt(rid, hermes_home_path=None, force=bool(force))


def rollback(run_id: str, confirmation: str, force: bool = False) -> dict[str, Any]:
    rid = (run_id or "").strip()
    expected = f"ROLLBACK {rid}"
    if not rid:
        raise ValueError("run_id is required")
    if (confirmation or "").strip() != expected:
        raise PermissionError(f"type {expected!r} exactly to confirm")
    return core.rollback(rid, hermes_home_path=None, force=bool(force))


def upstream_status(home: str | None = None) -> dict[str, Any]:
    return core.upstream_status(hermes_home_path=home or None)


def upstream_parity(home: str | None = None) -> dict[str, Any]:
    return core.benchmark_parity_status(hermes_home_path=home or None)


def upstream_update(fetch_only: bool = False) -> dict[str, Any]:
    # Canonical active profile only. WebUI HERMES_HOME override is intentionally ignored.
    return core.upstream_update(hermes_home_path=None, repo_path=None, fetch_only=bool(fetch_only))
