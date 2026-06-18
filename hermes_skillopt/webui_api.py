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


def run_full(payload: dict[str, Any]) -> dict[str, Any]:
    """Run full cycle from WebUI, always staged-only and never force-adopt."""
    out = core.full_run(
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
        target_backend=payload.get("target_backend") or None,
        gate_mode=payload.get("gate_mode") or "soft",
        resume_run_id=payload.get("resume_run_id") or None,
        allow_mock=bool(payload.get("allow_mock")),
        auto_adopt=False,
        force=False,
        hermes_home_path=payload.get("home") or None,
    )
    return out


def review(run_id: str | None = None, home: str | None = None) -> dict[str, Any]:
    rid = (run_id or "").strip() or legacy.latest_run_id(home or None)
    summary, report, diff, gate, candidate, rejected = legacy.review_payload(rid, home or None)
    return {
        "run_id": rid,
        "summary": summary,
        "report": report,
        "diff": diff,
        "gate": gate,
        "candidate": candidate,
        "rejected": rejected,
        "success": not summary.startswith("Review failed:"),
    }


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
