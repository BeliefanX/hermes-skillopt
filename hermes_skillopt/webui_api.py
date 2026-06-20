from __future__ import annotations

"""Server-side WebUI API contracts for Hermes SkillOpt.

All safety-sensitive decisions stay here/Python-side. The React frontend is only a
client for these functions and never bypasses staged-only runs or typed writeback
confirmations.
"""

import json
import re
from pathlib import Path
from typing import Any

from hermes_skillopt import core
from hermes_skillopt import eval_packs


MAX_TEXT_CHARS = 20_000
ALLOWED_ARTIFACTS = {
    "manifest.json",
    "checkpoint.json",
    "report.md",
    "diff.patch",
    "gate_results.json",
    "candidate_summary.json",
    "rejected_edits.jsonl",
    "proposed_SKILL.md",
    "best_skill.md",
}


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


def _compact_native_metadata(data: Any) -> dict[str, Any] | None:
    """Return concise native-Hermes metadata for WebUI display, without large records."""

    if not isinstance(data, dict):
        return None
    sidecars_obj = data.get("sidecars")
    sidecars: dict[str, Any] = sidecars_obj if isinstance(sidecars_obj, dict) else {}
    compact_sidecars = {
        str(name): {
            key: meta.get(key)
            for key in ("present", "readable", "sha256")
            if isinstance(meta, dict) and key in meta
        }
        for name, meta in sidecars.items()
    }
    return _safe_json(
        {
            "schema_version": data.get("schema_version"),
            "read_only": data.get("read_only"),
            "skill_name": data.get("skill_name"),
            "skill_relpath": data.get("skill_relpath"),
            "labels": data.get("labels") or [],
            "signals": data.get("signals") or {},
            "fingerprint_sha256": data.get("fingerprint_sha256"),
            "sidecars": compact_sidecars,
            "records_omitted": True,
        }
    )


def _compact_native_guard(data: Any) -> dict[str, Any] | None:
    """Return concise native-Hermes adopt guard state for WebUI display."""

    if not isinstance(data, dict):
        return None
    current = data.get("current") if isinstance(data.get("current"), dict) else None
    return _safe_json(
        {
            "schema_version": data.get("schema_version"),
            "allowed": data.get("allowed"),
            "blockers": data.get("blockers") or [],
            "force_override_allowed": data.get("force_override_allowed"),
            "base_fingerprint_sha256": data.get("base_fingerprint_sha256"),
            "current_fingerprint_sha256": data.get("current_fingerprint_sha256"),
            "current": _compact_native_metadata(current) if current else None,
        }
    )


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


def eval_pack_doctor(home: str | None = None, skill: str | None = None) -> dict[str, Any]:
    """Read-only eval-pack coverage diagnostics for the WebUI."""

    return eval_packs.eval_pack_doctor(hermes_home_path=home or None, skill=skill or None)


def eval_pack_workflow(home: str | None = None, skill: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Read-only eval-pack authoring workflow summary; no production one-click."""

    return eval_packs.eval_pack_workflow_summary(hermes_home_path=home or None, skill=skill or None, limit=limit)


def skill_readiness_queue(home: str | None = None, skill: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Read-only high-value skill readiness queue for the WebUI."""

    return core.skill_readiness_queue(home or None, skill=skill or None, limit=limit)


def eval_pack_autopilot(payload: dict[str, Any]) -> dict[str, Any]:
    """Plan or write a review-only eval-pack draft; never adopts or edits live skills."""

    skill = str(payload.get("skill") or "").strip()
    if not skill:
        raise ValueError("skill is required")
    return eval_packs.eval_pack_autopilot(
        skill=skill,
        output=payload.get("output") or None,
        hermes_home_path=payload.get("home") or None,
        write_draft=bool(payload.get("write_draft")),
        overwrite=bool(payload.get("overwrite")),
    )


def eval_pack_promote(payload: dict[str, Any]) -> dict[str, Any]:
    """Promote a draft to a curated review pack only; production promotion is not exposed."""

    skill = str(payload.get("skill") or "").strip()
    input_path = str(payload.get("input_path") or "").strip()
    if not skill:
        raise ValueError("skill is required")
    if not input_path:
        raise ValueError("input_path is required")
    if bool(payload.get("production")):
        raise ValueError("WebUI promotes review packs only; production promotion requires an explicit policy/contract outside this one-click UX")
    return eval_packs.promote_eval_pack(
        skill=skill,
        input_path=input_path,
        output=payload.get("output") or None,
        hermes_home_path=payload.get("home") or None,
        production=False,
        overwrite=bool(payload.get("overwrite")),
    )


def skill_quality(payload: dict[str, Any]) -> dict[str, Any]:
    """Read-only skill quality/lint report; optional eval skeleton is review-only."""

    from hermes_skillopt.skill_quality import skill_quality_digest, skill_quality_report

    report = skill_quality_report(
        hermes_home_path=payload.get("home") or None,
        skill=payload.get("skill") or None,
        skill_path=payload.get("skill_path") or None,
        create_eval_skeleton=bool(payload.get("create_eval_skeleton")),
        output=payload.get("output") or None,
        overwrite=bool(payload.get("overwrite")),
    )
    return skill_quality_digest(report) if bool(payload.get("digest")) else report


def _redacted_json(data: Any) -> str:
    return core.redact_secrets(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _safe_artifact_path(run_dir: Path, filename: str) -> Path | None:
    """Return a safe fixed artifact path under run_dir, rejecting symlink escapes."""
    if filename not in ALLOWED_ARTIFACTS:
        return None
    if Path(filename).name != filename:
        return None
    base = run_dir.resolve()
    path = base / filename
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    if path.is_symlink() or not core._is_relative_to(resolved, base) or not resolved.is_file():
        return None
    return resolved


def _read_artifact_limited(run_dir: Path, filename: str, limit: int = MAX_TEXT_CHARS) -> str:
    path = _safe_artifact_path(run_dir, filename)
    if path is None:
        return ""
    return core.redact_secrets(path.read_text(encoding="utf-8", errors="replace")[:limit])


def _latest_run_id(home: str | None = None) -> str:
    rows = core.status(home or None).get("recent_runs") or []
    if not rows:
        return ""
    return str(rows[0].get("run_id") or "")


def _review_payload(run_id: str | None = None, home: str | None = None) -> tuple[str, str, str, str, str, str]:
    rid = (run_id or "").strip() or _latest_run_id(home)
    if not rid:
        return "No staged runs found.", "", "", "", "", ""
    try:
        run_dir = core.resolve_run_dir(core.hermes_home(home or None), rid)
        manifest_text = _read_artifact_limited(run_dir, "manifest.json")
        if not manifest_text:
            raise ValueError("manifest.json missing or unsafe")
        manifest = json.loads(manifest_text)
        report = _read_artifact_limited(run_dir, "report.md")
        diff = _read_artifact_limited(run_dir, "diff.patch")
        gate_text = _read_artifact_limited(run_dir, "gate_results.json")
        gate_data = manifest.get("gate")
        if gate_text:
            try:
                gate_data = json.loads(gate_text).get("best_gate")
            except Exception:
                gate_data = gate_text
        gate = gate_text or _redacted_json(gate_data)
        candidate_summary = _read_artifact_limited(run_dir, "candidate_summary.json")
        if candidate_summary:
            gate = (gate + "\n\n## Candidate summary\n" + candidate_summary) if gate else candidate_summary
        candidate = _read_artifact_limited(run_dir, "proposed_SKILL.md") or _read_artifact_limited(run_dir, "best_skill.md")
        rejected = _read_artifact_limited(run_dir, "rejected_edits.jsonl")
        readiness = core.readiness_adoptability_schema(core._runtime_evidence_payload(run_dir, manifest))
        summary = [
            f"## Review `{rid}`",
            f"- status: {manifest.get('status')}",
            f"- skill: {manifest.get('skill_name')}",
            f"- manifest_adoptable: {manifest.get('adoptable')}",
            f"- production_adoptable: {readiness.get('production_adoptable')}",
            f"- review_only: {readiness.get('review_only')}",
            f"- production_gate_eligible: {manifest.get('production_gate_eligible')}",
            f"- test_gate_eligible: {manifest.get('test_gate_eligible')}",
            f"- run_dir: `{run_dir}`",
            f"- diff_path: `{run_dir / 'diff.patch'}`",
            f"- report_path: `{run_dir / 'report.md'}`",
        ]
        return "\n".join(summary), report, diff, gate, candidate, rejected
    except Exception as exc:
        return f"Review failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}", "", "", "", "", ""


def review(run_id: str | None = None, home: str | None = None) -> dict[str, Any]:
    rid = (run_id or "").strip() or _latest_run_id(home or None)
    summary, report, diff, gate, candidate, rejected = _review_payload(rid, home or None)
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
        "production_adoptable": decision.get("production_adoptable", decision.get("adoptable")),
        "manifest_adoptable": decision.get("manifest_adoptable"),
        "review_only": decision.get("review_only"),
        "blockers": decision.get("not_adoptable_reasons") or [],
        "production_gate": decision.get("production_gate_eligible"),
        "test_gate": decision.get("test_gate_eligible"),
        "evidence_class": "production_candidate" if decision.get("adoptable") and (decision.get("evidence_ledger") or {}).get("production_runtime_ready") else "review_only_or_not_ready",
        "eval_level": decision.get("eval_level"),
        "evidence_maturity": decision.get("evidence_maturity"),
        "evidence_ledger": decision.get("evidence_ledger"),
        "native_hermes_metadata": _compact_native_metadata(decision.get("native_hermes_metadata")),
        "native_hermes_adopt_guard": _compact_native_guard(decision.get("native_hermes_adopt_guard")),
        "native_hermes_boundary": "SkillOpt reads native Hermes metadata as advisory guard input only. It does not replace the Hermes curator: curator owns lifecycle/archive/consolidation; SkillOpt owns staged eval evidence and adoption recommendations.",
        "artifacts": decision.get("artifact_refs") or {},
        "artifact_refs": decision.get("artifact_refs") or {},
        "score_provenance": decision.get("score_provenance") or review_json.get("score_provenance"),
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
