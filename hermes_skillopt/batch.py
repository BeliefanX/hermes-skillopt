from __future__ import annotations

"""Safe staged batch runner for Hermes SkillOpt.

Batch plans are data-only JSON/dicts.  They never adopt, never honor writeback
fields, and always preflight before running child full_run calls.
"""

import json
from pathlib import Path
from typing import Any

from hermes_skillopt import core

BATCH_SCHEMA_VERSION = "hermes-skillopt-batch-plan-v1"
BATCH_RUN_SCHEMA_VERSION = "hermes-skillopt-batch-run-v1"
VALID_BACKENDS = {"auto", "hermes", "mock"}
VALID_TARGETS = {"auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"}
VALID_GATES = {"soft", "hard", "mixed", "strict"}
FORBIDDEN_JOB_FIELDS = {"auto_adopt", "force", "writeback", "adopt", "rollback", "unsafe_cross_profile_writeback"}
DEFAULT_BUDGET = {"max_jobs": 10, "max_total_iterations": 20, "max_total_candidates": 40}
FULL_RUN_FIELDS = {
    "skill", "query", "lookback_days", "limit", "iterations", "edit_budget", "candidate_count",
    "backend", "optimizer_backend", "allow_mock", "eval_file", "target_executor", "target_backend",
    "gate_mode", "resume_run_id",
}


def _load_plan(plan: str | Path | dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if isinstance(plan, dict):
        return dict(plan), None
    path = Path(plan).expanduser().resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("batch plan must be a JSON object")
    return data, str(path)


def _as_int(value: Any, default: int, field: str) -> int:
    try:
        out = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if out < 1:
        raise ValueError(f"{field} must be >= 1")
    return out


def _normalise_plan(plan: dict[str, Any]) -> dict[str, Any]:
    defaults = dict(plan.get("defaults") or {})
    jobs_raw = plan.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ValueError("batch plan requires non-empty jobs list")
    jobs: list[dict[str, Any]] = []
    for i, raw in enumerate(jobs_raw, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"job #{i} must be an object")
        merged = {**defaults, **raw}
        merged["iterations"] = _as_int(merged.get("iterations"), 1, f"job #{i}.iterations")
        merged["candidate_count"] = _as_int(merged.get("candidate_count"), 1, f"job #{i}.candidate_count")
        merged["edit_budget"] = _as_int(merged.get("edit_budget"), 3, f"job #{i}.edit_budget")
        merged["lookback_days"] = _as_int(merged.get("lookback_days"), 14, f"job #{i}.lookback_days")
        merged["limit"] = _as_int(merged.get("limit"), 50, f"job #{i}.limit")
        merged["backend"] = merged.get("backend") or "auto"
        merged["target_executor"] = merged.get("target_executor") or "auto"
        merged["gate_mode"] = merged.get("gate_mode") or "strict"
        jobs.append(merged)
    budget = {**DEFAULT_BUDGET, **dict(plan.get("budget") or {})}
    return {
        "schema_version": plan.get("schema_version") or BATCH_SCHEMA_VERSION,
        "defaults": defaults,
        "budget": budget,
        "jobs": jobs,
        "stop_on_first_failure": bool(plan.get("stop_on_first_failure", False)),
        "plan_id": plan.get("plan_id"),
    }


def batch_preflight(plan: str | Path | dict[str, Any], *, hermes_home_path: str | None = None) -> dict[str, Any]:
    """Deterministically validate a batch plan without writing or running jobs."""

    raw, source_path = _load_plan(plan)
    normalised = _normalise_plan(raw)
    errors: list[str] = []
    warnings: list[str] = []
    if normalised["schema_version"] != BATCH_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {normalised['schema_version']}")

    jobs = normalised["jobs"]
    budget = normalised["budget"]
    max_jobs = _as_int(budget.get("max_jobs"), DEFAULT_BUDGET["max_jobs"], "budget.max_jobs")
    max_total_iterations = _as_int(budget.get("max_total_iterations"), DEFAULT_BUDGET["max_total_iterations"], "budget.max_total_iterations")
    max_total_candidates = _as_int(budget.get("max_total_candidates"), DEFAULT_BUDGET["max_total_candidates"], "budget.max_total_candidates")
    budget = {"max_jobs": max_jobs, "max_total_iterations": max_total_iterations, "max_total_candidates": max_total_candidates}

    if len(jobs) > max_jobs:
        errors.append(f"job count {len(jobs)} exceeds budget.max_jobs {max_jobs}")
    total_iterations = sum(int(j["iterations"]) for j in jobs)
    total_candidates = sum(int(j["iterations"]) * int(j["candidate_count"]) for j in jobs)
    if total_iterations > max_total_iterations:
        errors.append(f"total iterations {total_iterations} exceeds budget.max_total_iterations {max_total_iterations}")
    if total_candidates > max_total_candidates:
        errors.append(f"total iteration*candidate count {total_candidates} exceeds budget.max_total_candidates {max_total_candidates}")

    for i, job in enumerate(jobs, 1):
        present_forbidden = sorted(k for k in FORBIDDEN_JOB_FIELDS if k in job and job.get(k) not in (None, False, ""))
        if present_forbidden:
            errors.append(f"job #{i} contains forbidden staged-only/writeback fields: {', '.join(present_forbidden)}")
        backend = str(job.get("backend") or "auto")
        opt_backend = str(job.get("optimizer_backend") or backend)
        target_backend = str(job.get("target_backend") or job.get("target_executor") or "auto")
        gate = str(job.get("gate_mode") or "strict")
        if backend not in VALID_BACKENDS:
            errors.append(f"job #{i} invalid backend: {backend}")
        if opt_backend not in VALID_BACKENDS:
            errors.append(f"job #{i} invalid optimizer_backend: {opt_backend}")
        if target_backend not in VALID_TARGETS:
            errors.append(f"job #{i} invalid target_backend/target_executor: {target_backend}")
        if gate not in VALID_GATES:
            errors.append(f"job #{i} invalid gate_mode: {gate}")
        production_capable_intent = bool(job.get("production_intent", gate == "strict" and opt_backend != "mock" and not bool(job.get("allow_mock"))))
        if production_capable_intent:
            if not job.get("skill"):
                errors.append(f"job #{i} missing skill for production-capable intent")
            if not job.get("eval_file"):
                errors.append(f"job #{i} missing eval_file for production-capable intent")
        if opt_backend == "mock" or bool(job.get("allow_mock")) or gate in {"soft", "mixed"}:
            warnings.append(f"job #{i} is review-only/non-production by backend/gate policy")

    report = {
        "success": not errors,
        "schema_version": BATCH_RUN_SCHEMA_VERSION,
        "mode": "batch_preflight_read_only",
        "plan_source": source_path,
        "hermes_home": str(core.hermes_home(hermes_home_path)),
        "job_count": len(jobs),
        "budget": budget,
        "budget_usage": {"total_iterations": total_iterations, "total_candidates": total_candidates},
        "errors": errors,
        "warnings": warnings,
        "jobs": [{k: v for k, v in j.items() if k in FULL_RUN_FIELDS or k in {"production_intent"}} for j in jobs],
    }
    if errors:
        raise ValueError("batch preflight failed: " + "; ".join(errors))
    return report


def run_batch(plan: str | Path | dict[str, Any], *, hermes_home_path: str | None = None, ctx: Any = None) -> dict[str, Any]:
    """Run a preflighted batch in staging only; never adopts or writebacks."""

    raw, source_path = _load_plan(plan)
    preflight = batch_preflight(raw, hermes_home_path=hermes_home_path)
    normalised = _normalise_plan(raw)
    home = core.hermes_home(hermes_home_path)
    dirs = core.ensure_dirs(home)
    batch_id = core.now_id() + "-batch"
    batch_dir = dirs["staging"] / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    jobs_report: list[dict[str, Any]] = []
    write = lambda name, payload: core.write_text(batch_dir / name, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write("preflight.json", preflight)
    manifest = {
        "schema_version": BATCH_RUN_SCHEMA_VERSION,
        "batch_id": batch_id,
        "plan_source": source_path,
        "staged_only": True,
        "auto_adopt": False,
        "force": False,
        "started_at": core.datetime.now(core.timezone.utc).isoformat(),
        "files": {"preflight": "preflight.json", "jobs": "jobs.json", "summary": "summary.json", "report": "report.md"},
    }
    write("manifest.json", manifest)

    for i, job in enumerate(normalised["jobs"], 1):
        safe_args = {k: job.get(k) for k in FULL_RUN_FIELDS if k in job}
        safe_args.update({"auto_adopt": False, "force": False, "hermes_home_path": str(home), "ctx": ctx})
        row: dict[str, Any] = {"index": i, "skill": safe_args.get("skill"), "status": "started"}
        try:
            result = core.full_run(**safe_args)
            row.update({"status": "success" if result.get("success", True) else "failed", "run_id": result.get("run_id"), "run_dir": result.get("run_dir"), "child_status": result.get("status"), "adoptable": result.get("adoptable", False)})
        except Exception as exc:  # keep batch artifact useful and optionally stop
            row.update({"status": "error", "error_type": type(exc).__name__, "error": core.redact_secrets(str(exc))})
            jobs_report.append(row)
            if normalised["stop_on_first_failure"]:
                break
        else:
            jobs_report.append(row)
            if row["status"] != "success" and normalised["stop_on_first_failure"]:
                break
        write("jobs.json", jobs_report)

    summary = {
        "success": all(j.get("status") == "success" for j in jobs_report) and len(jobs_report) == len(normalised["jobs"]),
        "batch_id": batch_id,
        "run_dir": str(batch_dir),
        "staged_only": True,
        "job_count": len(normalised["jobs"]),
        "completed_jobs": len(jobs_report),
        "failed_jobs": sum(1 for j in jobs_report if j.get("status") != "success"),
        "child_run_ids": [j.get("run_id") for j in jobs_report if j.get("run_id")],
    }
    write("summary.json", summary)
    lines = [f"# Hermes SkillOpt Batch {batch_id}", "", "Staged-only batch run. No adopt/writeback was attempted.", "", f"Jobs: {summary['completed_jobs']}/{summary['job_count']}", f"Failures: {summary['failed_jobs']}", ""]
    for job in jobs_report:
        lines.append(f"- job {job.get('index')}: {job.get('status')} run_id={job.get('run_id') or '-'} error={job.get('error') or '-'}")
    core.write_text(batch_dir / "report.md", "\n".join(lines) + "\n")
    manifest["completed_at"] = core.datetime.now(core.timezone.utc).isoformat()
    manifest["summary"] = summary
    write("manifest.json", manifest)
    return {**summary, "manifest": str(batch_dir / "manifest.json"), "preflight": preflight, "jobs": jobs_report}
