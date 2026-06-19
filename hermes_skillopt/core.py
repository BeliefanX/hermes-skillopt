from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_skillopt.bounded_edit import apply_bounded_edits, frontmatter_split

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_URL = "https://github.com/microsoft/SkillOpt.git"
ALGORITHM_VERSION = "hermes-native-skillopt-core-adapter-track-b-p2"
UPSTREAM_SEAM_MATRIX = [
    {"seam": "trainer_loop", "upstream_concept": "rollout/reflection/update/evaluate loop", "hermes_adapter": "SixStageSkillOptTrainer rollout→reflect→aggregate→select→update→evaluate", "status": "adapted", "checklist": ["six stage artifacts", "train/val/test split", "staged-only writes"]},
    {"seam": "reflection_prompts", "upstream_concept": "LLM reflection over rollout evidence", "hermes_adapter": "OptimizerBackend.reflect with deterministic labels and prompt hashes", "status": "adapted", "checklist": ["redacted prompts", "rejected history", "prompt_sha256 provenance"]},
    {"seam": "skill_aware_reflection", "upstream_concept": "skill-specific failure analysis", "hermes_adapter": "analyze_rollout_reflections labels skill_defect/execution_lapse from EvalTask evidence", "status": "adapted"},
    {"seam": "aggregate_clip", "upstream_concept": "merge/rank proposed edits", "hermes_adapter": "aggregate_edit_proposals dedupes, rejects previous failures, clips to edit_budget", "status": "adapted"},
    {"seam": "gate", "upstream_concept": "candidate validation gate", "hermes_adapter": "ValidationGate soft/hard/mixed/strict metric policy; LLM judge explanation-only", "status": "hardened"},
    {"seam": "artifact_resume", "upstream_concept": "checkpoint and artifacts", "hermes_adapter": "manifest/checkpoint/artifact hashes; completed-run resume only", "status": "hardened"},
    {"seam": "benchmarks_tests", "upstream_concept": "benchmark/evaluation packs", "hermes_adapter": "Hermes eval packs plus curated production validation/test policy", "status": "adapted"},
    {"seam": "benchmark_bridge", "upstream_concept": "external benchmark manifest ingestion", "hermes_adapter": "JSON-only importer converts upstream-style manifests to Hermes eval packs; rejects executable/remote fields", "status": "p3_local_adapter"},
    {"seam": "transfer_eval", "upstream_concept": "cross-target/generalization evaluation", "hermes_adapter": "read-only staged skill evaluation across deterministic target/profile configurations with fingerprints", "status": "p3_report_only"},
    {"seam": "conformance", "upstream_concept": "adapter regression/conformance checks", "hermes_adapter": "local compile/pytest conformance reports; no upstream code execution or external services required", "status": "p3_local_contract"},
]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:sk|ghp|gho|xox[baprs])-[-A-Za-z0-9_]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b"),
]

TOOL_SAFETY_METADATA: dict[str, dict[str, Any]] = {
    "hermes_skillopt_status": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_scout": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": True, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_doctor": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": True, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_eval_pack_inventory": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": True, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_eval_pack_doctor": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": True, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_eval_pack_workflow": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "manual_surface": True, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_skill_readiness_queue": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "manual_surface": True, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_review": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": "digest_only", "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_resume_inspect": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_fleet_report": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_fleet_resume_plan": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_fleet_rollback_plan": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_artifact_hygiene_report": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_upstream_status": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_compare_upstream_pin": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_benchmark_parity_status": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_batch_preflight": {"safety_group": "read_only", "risk_level": "low", "writes": False, "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_conformance": {"safety_group": "read_only_report", "risk_level": "low", "writes": "optional_report_only", "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_transfer_eval": {"safety_group": "read_only_report", "risk_level": "low", "writes": "optional_report_only", "cron_safe": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_dry_run": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "staging_only", "cron_safe": False, "auto_adopt": False, "curator_replacement": False},
    "hermes_skillopt_run": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "staging_only", "cron_safe": False, "auto_adopt": False, "curator_replacement": False},
    "hermes_skillopt_full_run": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "staging_only", "cron_safe": False, "auto_adopt": False, "curator_replacement": False},
    "hermes_skillopt_optimize": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "staging_only", "cron_safe": False, "auto_adopt": False, "curator_replacement": False},
    "hermes_skillopt_batch_run": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "staging_only", "cron_safe": False},
    "hermes_skillopt_eval_pack_autopilot": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_review_only_eval_pack_output_only_when_write_draft_true", "cron_safe": False, "manual_surface": True},
    "hermes_skillopt_eval_pack_scaffold": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_eval_pack_output", "cron_safe": False},
    "hermes_skillopt_eval_pack_curate": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_eval_pack_output", "cron_safe": False},
    "hermes_skillopt_eval_pack_mine_sessions": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_review_only_eval_pack_output", "cron_safe": False},
    "hermes_skillopt_eval_pack_ingest_correction": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_review_only_eval_pack_output", "cron_safe": False},
    "hermes_skillopt_eval_pack_ingest_context": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_review_only_eval_pack_output", "cron_safe": False},
    "hermes_skillopt_eval_pack_negative_boundary": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_review_only_eval_pack_output", "cron_safe": False},
    "hermes_skillopt_eval_pack_promote": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_eval_pack_output_no_auto_adopt", "cron_safe": False},
    "hermes_skillopt_skill_quality": {"safety_group": "read_only_report", "risk_level": "low", "writes": "optional_guarded_review_only_eval_skeleton", "cron_safe": False, "manual_surface": True, "auto_adopt": False, "live_skill_writes": False, "native_hermes_metadata": "read_only_advisory"},
    "hermes_skillopt_import_upstream_benchmark": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "guarded_eval_pack_output", "cron_safe": False},
    "hermes_skillopt_handoff_optimize": {"safety_group": "stage_artifacts", "risk_level": "medium", "writes": "handoff_artifact_only", "cron_safe": False},
    "hermes_skillopt_adopt": {"safety_group": "writeback", "risk_level": "high", "writes": "live_skill_with_backup", "cron_safe": False, "auto_adopt": False, "requires_typed_confirmation": True, "curator_replacement": False},
    "hermes_skillopt_rollback": {"safety_group": "writeback", "risk_level": "high", "writes": "live_skill_restore", "cron_safe": False, "auto_adopt": False, "requires_typed_confirmation": True, "curator_replacement": False},
    "hermes_skillopt_upstream_update": {"safety_group": "upstream", "risk_level": "high", "writes": "pinned_upstream_clone_and_lock", "cron_safe": False},
}
TOOL_SAFETY_GROUPS: dict[str, dict[str, Any]] = {
    "read_only": {"description": "Inspection/inventory/digest only; no live skill writes, no optimize/adopt/rollback/fetch. Only scout, doctor, eval-pack-inventory, eval-pack-doctor, and review --digest are cron-safe defaults; workflow/queue surfaces are manual, and autopilot/skill-quality are manual unless they add explicit digest-only modes.", "scheduled_default": "limited"},
    "read_only_report": {"description": "Read-only checks that may write an explicitly requested report artifact; not cron-safe by default.", "scheduled_default": False},
    "stage_artifacts": {"description": "Creates guarded staging/eval/handoff artifacts for human review; never auto-adopts.", "scheduled_default": False},
    "writeback": {"description": "Writes live SKILL.md after explicit typed confirmation and guard checks.", "scheduled_default": False},
    "upstream": {"description": "Touches the pinned upstream clone/lock only; not plugin code, but network/write side effects remain human-triggered.", "scheduled_default": False},
}


def tool_safety_catalog() -> dict[str, Any]:
    """Compatibility-preserving safety/risk metadata for all registered tools."""

    return {
        "schema_version": "hermes-skillopt-tool-safety-v1",
        "compatibility": "metadata_only_all_existing_tools_remain_available",
        "scheduled_default_guidance": "Schedule only scout, doctor, eval-pack-inventory, eval-pack-doctor, or review --digest surfaces; eval-pack-autopilot and skill-quality are manual unless explicit digest-only modes are added and cataloged; never cron optimize, full-run, adopt, rollback, writeback, upstream-update, or status.",
        "native_hermes_boundary": "SkillOpt reads native Hermes metadata as advisory guard input only. It does not replace the Hermes curator: curator owns lifecycle/archive/consolidation; SkillOpt owns staged eval evidence and adoption recommendations.",
        "groups": json.loads(json.dumps(TOOL_SAFETY_GROUPS, sort_keys=True)),
        "tools": json.loads(json.dumps(TOOL_SAFETY_METADATA, sort_keys=True)),
    }


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_secrets(text: str) -> str:
    out = text or ""
    out = SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=<REDACTED>", out)
    for pat in SECRET_PATTERNS[1:]:
        out = pat.sub("<REDACTED>", out)
    return out


def _official_hermes_home() -> str | os.PathLike[str] | None:
    """Best-effort bridge to Hermes' official runtime home helper.

    Hermes may expose ``get_hermes_home`` from different import paths across
    runtime versions.  SkillOpt must not require that package at import time, so
    this helper probes known locations and returns None when unavailable.
    """

    for module_name in ("hermes.config", "hermes.runtime", "hermes_agent.config", "hermes_agent.runtime"):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        helper = getattr(module, "get_hermes_home", None)
        if not callable(helper):
            continue
        try:
            value = helper()
        except Exception:
            continue
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value)
    return None


def hermes_home(explicit: str | None = None) -> Path:
    if explicit:
        raw = explicit
    else:
        raw = _official_hermes_home() or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(raw).expanduser().resolve()


def active_hermes_home() -> Path:
    """Return the active runtime Hermes profile home, ignoring per-call overrides."""
    return hermes_home(None)


def guard_writeback_home(home: Path, *, unsafe_cross_profile: bool = False) -> None:
    """Prevent adopt/rollback from writing a non-active profile by default."""
    active = active_hermes_home()
    if home == active:
        return
    if unsafe_cross_profile:
        return
    raise ValueError(
        "Refusing live skill writeback outside the active Hermes profile home; "
        f"requested {home}, active {active}. Set HERMES_HOME to the target profile "
        "or use the explicit unsafe cross-profile CLI confirmation for offline maintenance."
    )


def ensure_dirs(home: Path) -> dict[str, Path]:
    base = home / "skillopt"
    dirs = {"base": base, "staging": base / "staging", "backups": base / "backups", "upstream": base / "upstream"}
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def skillopt_paths(home: Path) -> dict[str, Path]:
    """Return canonical SkillOpt paths without creating directories."""

    base = home / "skillopt"
    return {"base": base, "staging": base / "staging", "backups": base / "backups", "upstream": base / "upstream"}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _post_write_readback_verification(target: Path, *, expected_sha256: str, expected_skill_name: str | None = None) -> dict[str, Any]:
    """Verify the runtime-visible SKILL.md bytes immediately after writeback."""

    evidence: dict[str, Any] = {
        "schema_version": "hermes-skillopt-post-write-readback-v1",
        "path": str(target),
        "expected_sha256": expected_sha256,
        "verified": False,
    }
    try:
        text = read_text(target)
    except Exception as exc:
        evidence.update({"error_type": type(exc).__name__, "error": redact_secrets(str(exc))})
        raise ValueError("Post-write readback failed; refusing writeback success") from exc
    actual_sha = sha256_text(text)
    actual_name = parse_frontmatter_name(text)
    evidence.update({
        "actual_sha256": actual_sha,
        "hash_match": actual_sha == expected_sha256,
        "frontmatter_skill_name": actual_name,
        "expected_skill_name": expected_skill_name,
        "skill_name_match": True if expected_skill_name is None or actual_name is None else actual_name == expected_skill_name,
    })
    if actual_sha != expected_sha256:
        raise ValueError("Post-write readback sha mismatch; refusing writeback success")
    if expected_skill_name is not None and actual_name is not None and actual_name != expected_skill_name:
        raise ValueError("Post-write readback skill name mismatch; refusing writeback success")
    evidence["verified"] = True
    return evidence


def parse_frontmatter_name(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    for line in text[4:end].splitlines():
        if line.strip().startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"\'') or None
    return None


@dataclass
class Skill:
    name: str
    path: Path
    relpath: str
    sha256: str


def discover_skills(home: Path) -> list[Skill]:
    home = home.resolve()
    skills_dir = (home / "skills").resolve()
    if not skills_dir.exists():
        return []
    found: list[Skill] = []
    for p in sorted(skills_dir.rglob("SKILL.md")):
        resolved = p.resolve()
        if p.is_symlink() or not _is_relative_to(resolved, skills_dir):
            raise ValueError(f"Skill path escapes HERMES_HOME/skills: {p}")
        try:
            text = read_text(resolved)
        except UnicodeDecodeError:
            continue
        name = parse_frontmatter_name(text) or p.parent.name
        found.append(Skill(name=name, path=resolved, relpath=str(resolved.relative_to(home)), sha256=sha256_text(text)))
    return found


def find_skill(home: Path, skill: str | None) -> Skill:
    skills = discover_skills(home)
    if not skills:
        raise ValueError(f"No skills found under {home / 'skills'}")
    if not skill:
        if len(skills) == 1:
            return skills[0]
        raise ValueError("skill is required when multiple skills exist")
    q = skill.strip().lower()
    matches = [s for s in skills if s.name.lower() == q or s.path.parent.name.lower() == q or s.relpath.lower() == q]
    if not matches:
        matches = [s for s in skills if q in s.name.lower() or q in s.relpath.lower()]
    if not matches:
        raise ValueError(f"Skill not found: {skill}")
    if len(matches) > 1:
        raise ValueError(f"Ambiguous skill {skill}: " + ", ".join(s.relpath for s in matches[:8]))
    return matches[0]


def _frontmatter_split(text: str) -> tuple[str, str]:
    return frontmatter_split(text)


def make_diff(original: str, proposed: str, relpath: str) -> str:
    return "".join(difflib.unified_diff(original.splitlines(True), proposed.splitlines(True), fromfile=f"a/{relpath}", tofile=f"b/{relpath}"))


def load_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads(read_text(run_dir / "manifest.json"))


def save_manifest(run_dir: Path, data: dict[str, Any]) -> None:
    write_text(run_dir / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_hashes(run_dir: Path, files: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, rel in files.items():
        if key == "manifest":
            continue
        p = (run_dir / rel).resolve()
        if not _is_relative_to(p, run_dir.resolve()) or not p.is_file() or p.is_symlink():
            continue
        out[key] = sha256_file(p)
    return out


def verify_artifact_hashes(run_dir: Path, manifest: dict[str, Any]) -> None:
    expected = manifest.get("artifact_sha256") or {}
    files = manifest.get("files") or {}
    if not expected:
        raise ValueError("Manifest missing artifact_sha256 integrity map")
    if not isinstance(expected, dict) or not isinstance(files, dict):
        raise ValueError("Manifest artifact integrity fields are invalid")
    for key, digest in expected.items():
        rel = files.get(key)
        if not isinstance(rel, str) or not isinstance(digest, str):
            raise ValueError("Manifest artifact integrity entry is invalid")
        p = (run_dir / rel).resolve()
        if not _is_relative_to(p, run_dir.resolve()) or p.is_symlink() or not p.is_file():
            raise ValueError(f"Artifact {key} is missing or unsafe")
        if sha256_file(p) != digest:
            if key == "proposed":
                raise ValueError("Staged proposed_SKILL.md sha does not match manifest artifact_sha256; refusing to adopt")
            raise ValueError(f"Artifact hash mismatch for {key}")


def _artifact_path(run_dir: Path, manifest: dict[str, Any], key: str) -> Path:
    files = manifest.get("files") or {}
    hashes = manifest.get("artifact_sha256") or {}
    rel = files.get(key)
    if not isinstance(rel, str) or key not in hashes:
        raise ValueError(f"Manifest missing required hashed artifact: {key}")
    p = (run_dir / rel).resolve()
    if not _is_relative_to(p, run_dir.resolve()) or p.is_symlink() or not p.is_file():
        raise ValueError(f"Artifact {key} is missing or unsafe")
    return p


def _read_hashed_json(run_dir: Path, manifest: dict[str, Any], key: str) -> Any:
    return json.loads(read_text(_artifact_path(run_dir, manifest, key)))


def _read_hashed_jsonl(run_dir: Path, manifest: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in read_text(_artifact_path(run_dir, manifest, key)).splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _manifest_equal(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        label = "production eligible" if field == "production_gate_eligible" else field
        raise ValueError(f"Manifest {label} does not match verified artifacts; refusing to adopt")


def _mock_provenance_reasons(manifest: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    backend = str(manifest.get("backend") or "").lower()
    optimizer_backend = str(manifest.get("optimizer_backend") or "").lower()
    cfg = manifest.get("optimizer_backend_config") if isinstance(manifest.get("optimizer_backend_config"), dict) else manifest.get("optimizer_config") if isinstance(manifest.get("optimizer_config"), dict) else {}
    cfg_backend = str(cfg.get("backend") or "").lower() if isinstance(cfg, dict) else ""
    requested = str(cfg.get("requested_backend") or "").lower() if isinstance(cfg, dict) else ""
    if backend == "mock":
        reasons.append("mock optimizer backend is review-only")
    if optimizer_backend == "mock" or cfg_backend == "mock" or requested == "mock":
        reasons.append("mock optimizer provenance is review-only")
    if isinstance(cfg, dict) and cfg.get("allow_mock") is True:
        reasons.append("allow_mock optimizer fallback is review-only")
    return reasons


def _target_binding_payload(home: Path, target: Skill, original: str, *, target_config: dict[str, Any] | None = None, target_execution_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = {
        "classification": "frozen_hermes_target_execution_v1",
        "required_fields": [
            "frozen_target_config_id",
            "frozen_target_fingerprint_sha256",
            "provider_fingerprint",
            "model_fingerprint",
            "tool_policy_fingerprint",
            "session_fingerprint",
            "runtime_fingerprint",
            "isolated_runtime_evidence",
            "permissions.task_commands_allowed=false",
            "permissions.profile_write_allowed=false",
            "trajectory_or_transcript_artifact_fingerprint",
            "execution_scoring_evidence",
        ],
        "missing_required_evidence_makes_production_eligible": False,
        "live_profile_writes_allowed": False,
        "task_commands_allowed": False,
    }
    payload = {
        "schema_version": "skillopt-target-binding-v1",
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_relpath": target.relpath,
        "skill_path": str(target.path),
        "original_sha256": sha256_text(original),
        "target_backend_config": target_config or {},
        "frozen_target_config_id": (target_config or {}).get("target_config_id"),
        "frozen_target_fingerprint_sha256": (target_config or {}).get("fingerprint_sha256"),
        "target_execution_evidence_contract": contract,
        "target_execution_evidence": target_execution_evidence or {"available": False, "review_only_reason": "no target execution evidence supplied to binding"},
        "permissions": {"task_commands_allowed": False, "profile_write_allowed": False, "live_profile_writes": False},
    }
    payload["fingerprint_sha256"] = _stable_json_sha(payload)
    return payload


def _provenance_binding_payload(*, backend: str, optimizer_config: dict[str, Any], target_backend: str, target_config: dict[str, Any], target_executor: str, target_config_id: str, gate_policy: dict[str, Any]) -> dict[str, Any]:
    """Immutable hashed binding for manifest-mutable provenance/adopt fields."""

    payload = {
        "schema_version": "skillopt-provenance-binding-v1",
        "backend": backend,
        "optimizer_backend": optimizer_config.get("backend"),
        "optimizer_backend_config": optimizer_config,
        "optimizer_config": optimizer_config,
        "target_backend": target_backend,
        "target_backend_config": target_config,
        "target_executor": target_executor,
        "target_config_id": target_config_id,
        "gate_policy": gate_policy,
        "allow_mock": bool(optimizer_config.get("allow_mock")),
        "mock_provenance_reasons": _mock_provenance_reasons({
            "backend": backend,
            "optimizer_backend": optimizer_config.get("backend"),
            "optimizer_backend_config": optimizer_config,
            "optimizer_config": optimizer_config,
        }),
    }
    payload["binding_sha256"] = _stable_json_sha(payload)
    return payload


def _adopt_time_artifact_crosscheck(run_dir: Path, manifest: dict[str, Any]) -> None:
    """Re-derive adopt eligibility from hashed artifacts, not mutable manifest fields."""

    required = ("gate_results", "test_results", "val", "test", "candidate_summary", "evidence", "proposed", "target_binding", "provenance_binding", "target_execution_evidence", "reviewer_gate")
    for key in required:
        _artifact_path(run_dir, manifest, key)

    gate_results = _read_hashed_json(run_dir, manifest, "gate_results")
    test_results = _read_hashed_json(run_dir, manifest, "test_results")
    val_items = _read_hashed_jsonl(run_dir, manifest, "val")
    test_items = _read_hashed_jsonl(run_dir, manifest, "test")
    candidate_summary_artifact = _read_hashed_json(run_dir, manifest, "candidate_summary")
    evidence = _read_hashed_json(run_dir, manifest, "evidence")
    target_binding = _read_hashed_json(run_dir, manifest, "target_binding")
    provenance_binding = _read_hashed_json(run_dir, manifest, "provenance_binding")
    target_execution_evidence = _read_hashed_json(run_dir, manifest, "target_execution_evidence")
    reviewer_gate = _read_hashed_json(run_dir, manifest, "reviewer_gate")

    if not isinstance(target_execution_evidence, dict) or target_execution_evidence.get("schema_version") != "skillopt-target-execution-evidence-v1":
        raise ValueError("Manifest target execution evidence artifact is invalid; refusing to adopt")
    reviewer_gate_for_ledger: dict[str, Any] = reviewer_gate if isinstance(reviewer_gate, dict) else {}
    evidence_ledger = _eval_evidence_ledger(target_execution_evidence=target_execution_evidence, reviewer_gate=reviewer_gate_for_ledger)
    if evidence_ledger.get("production_runtime_ready") is not True:
        raise ValueError("Frozen Hermes target execution evidence is not production-runtime complete; refusing to adopt: " + "; ".join(str(x) for x in (evidence_ledger.get("blockers") or [])))
    manifest_txe = manifest.get("target_execution_evidence") if isinstance(manifest.get("target_execution_evidence"), dict) else {}
    _manifest_equal(target_execution_evidence.get("fingerprint_sha256"), manifest_txe.get("fingerprint_sha256"), "target_execution_evidence")
    for field in ("complete", "classification", "real_hermes_runtime_evidence", "real_hermes_runtime_invocation", "task_commands_executed", "internal_review_only_runner"):
        _manifest_equal(target_execution_evidence.get(field), manifest_txe.get(field), f"target_execution_evidence.{field}")
    if not isinstance(reviewer_gate, dict) or reviewer_gate.get("schema_version") != "skillopt-reviewer-gate-v1":
        raise ValueError("Manifest reviewer gate artifact is invalid; refusing to adopt")
    if reviewer_gate.get("adoptable_after_reviewer_gate") is not True or reviewer_gate.get("passed") is not True:
        raise ValueError("Deterministic reviewer gate did not pass; refusing to adopt")
    manifest_rg = manifest.get("reviewer_gate") if isinstance(manifest.get("reviewer_gate"), dict) else {}
    _manifest_equal(reviewer_gate.get("fingerprint_sha256"), manifest_rg.get("fingerprint_sha256"), "reviewer_gate")

    if not isinstance(target_binding, dict) or target_binding.get("schema_version") != "skillopt-target-binding-v1":
        raise ValueError("Manifest target binding artifact is invalid; refusing to adopt")
    for field in ("hermes_home", "skill_name", "skill_relpath", "skill_path", "original_sha256"):
        _manifest_equal(target_binding.get(field), manifest.get(field), field)
    if not isinstance(provenance_binding, dict) or provenance_binding.get("schema_version") != "skillopt-provenance-binding-v1":
        raise ValueError("Manifest provenance binding artifact is invalid; refusing to adopt")
    binding_without_sha = {k: v for k, v in provenance_binding.items() if k != "binding_sha256"}
    _manifest_equal(_stable_json_sha(binding_without_sha), provenance_binding.get("binding_sha256"), "provenance_binding")
    bound_mock_reasons = provenance_binding.get("mock_provenance_reasons") if isinstance(provenance_binding.get("mock_provenance_reasons"), list) else []
    if bound_mock_reasons:
        raise ValueError("Mock/non-production optimizer provenance is review-only and cannot be adopted: " + "; ".join(str(r) for r in bound_mock_reasons))
    for field in ("backend", "optimizer_backend", "optimizer_backend_config", "optimizer_config", "target_backend", "target_backend_config", "target_executor", "target_config_id", "gate_policy"):
        _manifest_equal(provenance_binding.get(field), manifest.get(field), field)
    bound_gate_policy = provenance_binding.get("gate_policy")
    if not isinstance(bound_gate_policy, dict) or str(bound_gate_policy.get("mode") or "").lower() != "strict":
        raise ValueError("Production adoption requires strict gate mode; soft/mixed/hard gate manifests are review-only")

    best_gate = gate_results.get("best_gate") if isinstance(gate_results, dict) else None
    production_best_gate = gate_results.get("production_best_gate") if isinstance(gate_results, dict) else None
    _manifest_equal(best_gate, manifest.get("gate"), "gate")
    _manifest_equal(production_best_gate, manifest.get("production_gate"), "production_gate")
    _manifest_equal(test_results, manifest.get("test_results"), "test_results")
    _manifest_equal(candidate_summary_artifact.get("rounds") if isinstance(candidate_summary_artifact, dict) else None, manifest.get("candidate_summary"), "candidate_summary")

    from hermes_skillopt.env import EvalTask, is_production_gate_task

    def task_from_row(row: dict[str, Any]) -> EvalTask:
        return EvalTask(
            id=str(row.get("id", "")),
            prompt=str(row.get("prompt", "")),
            source=str(row.get("source", "")),
            expected_behavior=str(row.get("expected_behavior", "")),
            assertions=tuple(row.get("assertions") or ()),
            judge=str(row.get("judge", "keyword_scorecard")),
            allowed_tools=tuple(row.get("allowed_tools") or ()),
            timeout=float(row.get("timeout", 30.0)),
            fixtures=dict(row.get("fixtures") or {}),
            expected_terms=tuple(row.get("expected_terms") or ()),
            failure_terms=tuple(row.get("failure_terms") or ()),
            all_required_keywords=tuple(row.get("all_required_keywords") or ()),
            required_markers=tuple(row.get("required_markers") or ()),
            forbidden_markers=tuple(row.get("forbidden_markers") or ()),
            split=str(row.get("split", "validation")),
            weight=float(row.get("weight", 1.0)),
            success_criteria=tuple(row.get("success_criteria") or ()),
            metadata=dict(row.get("metadata") or {}),
        )

    task_rows = {
        "train": _read_hashed_jsonl(run_dir, manifest, "train") if "train" in (manifest.get("artifact_sha256") or {}) else [],
        "val": val_items,
        "test": test_items,
    }
    eval_tasks = {split: [task_from_row(r) for r in rows] for split, rows in task_rows.items()}
    production_val_tasks = [t for t in eval_tasks["val"] if is_production_gate_task(t)]
    production_gate_available = bool(production_val_tasks) and bool(evidence.get("production_gate_eligible"))
    recomputed_production_gate_eligible = production_gate_available and bool(production_best_gate and production_best_gate.get("accepted") is True)
    production_test_results = [r for r in (test_results.get("results") or []) if isinstance(r, dict) and isinstance(r.get("metadata"), dict) and r["metadata"].get("production_gate_eligible")]
    recomputed_test_gate_eligible = bool(production_test_results) and all(float(r.get("score", 0.0)) >= 0.55 and bool(r.get("passed")) for r in production_test_results)
    recomputed_policy = _production_eval_policy(evidence, production_gate_available, recomputed_test_gate_eligible)
    original_text = read_text(_artifact_path(run_dir, manifest, "original"))
    proposed_text = read_text(_artifact_path(run_dir, manifest, "proposed"))
    recomputed_optimizer_config = provenance_binding.get("optimizer_backend_config") if isinstance(provenance_binding.get("optimizer_backend_config"), dict) else provenance_binding.get("optimizer_config") if isinstance(provenance_binding.get("optimizer_config"), dict) else {}
    if not isinstance(recomputed_optimizer_config, dict):
        recomputed_optimizer_config = {}
    recomputed_provenance = _provenance_fingerprint(
        eval_file_used=evidence.get("eval_file"),
        tasks=eval_tasks,
        backend_mode=str(provenance_binding.get("backend")),
        target_executor_mode=str(provenance_binding.get("target_executor")),
        target_config_id=str(provenance_binding.get("target_config_id")),
        production_gate_available=production_gate_available,
        home=Path(str(manifest.get("hermes_home"))) if manifest.get("hermes_home") else None,
        skill_relpath=manifest.get("skill_relpath") if isinstance(manifest.get("skill_relpath"), str) else None,
        original_sha256=sha256_text(original_text),
        proposed_sha256=sha256_text(proposed_text),
        optimizer_config=recomputed_optimizer_config,
        target_config=provenance_binding.get("target_backend_config") if isinstance(provenance_binding.get("target_backend_config"), dict) else None,
        gate_policy=provenance_binding.get("gate_policy") if isinstance(provenance_binding.get("gate_policy"), dict) else None,
        production_eval_policy=recomputed_policy,
        eval_pack=evidence.get("eval_pack") if isinstance(evidence.get("eval_pack"), dict) else None,
        optimizer_prompt_fingerprints=recomputed_optimizer_config.get("prompt_fingerprints") if isinstance(recomputed_optimizer_config.get("prompt_fingerprints"), list) else [],
        algorithm_version=str(manifest.get("algorithm_version") or ALGORITHM_VERSION),
    )

    _manifest_equal(recomputed_production_gate_eligible, manifest.get("production_gate_eligible"), "production_gate_eligible")
    _manifest_equal(recomputed_test_gate_eligible, manifest.get("test_gate_eligible"), "test_gate_eligible")
    _manifest_equal(recomputed_policy, manifest.get("production_eval_policy"), "production_eval_policy")
    _manifest_equal(recomputed_provenance, manifest.get("provenance_fingerprint"), "provenance_fingerprint")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_run_id(run_id: str) -> None:
    if not run_id or not RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ValueError("Invalid run_id")


def resolve_run_dir(home: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    staging = ensure_dirs(home)["staging"].resolve()
    p = (staging / run_id).resolve()
    if not _is_relative_to(p, staging):
        raise ValueError("Invalid run_id path")
    if not p.exists():
        raise ValueError(f"run_id not found: {run_id}")
    return p


def manifest_skill_path(home: Path, manifest: dict[str, Any]) -> Path:
    manifest_home = manifest.get("hermes_home")
    if manifest_home is not None and manifest_home != str(home):
        raise ValueError("Manifest hermes_home does not match current home")
    relpath = manifest.get("skill_relpath")
    if not isinstance(relpath, str) or not relpath:
        raise ValueError("Manifest missing skill_relpath")
    target = (home / relpath).resolve()
    skills_dir = (home / "skills").resolve()
    if not _is_relative_to(target, skills_dir):
        raise ValueError("Manifest skill_relpath escapes skills directory")
    return target


def guard_manifest_skill_path(home: Path, manifest: dict[str, Any], target: Path) -> None:
    raw = manifest.get("skill_path")
    if raw is None:
        return
    try:
        recorded = Path(str(raw)).expanduser().resolve()
    except Exception as exc:
        raise ValueError("Invalid manifest skill_path") from exc
    if recorded != target:
        raise ValueError("Manifest skill_path does not match skill_relpath target")


def resolve_backup_skill_path(home: Path, manifest: dict[str, Any], run_id: str, target: Path) -> Path:
    if manifest.get("run_id") != run_id:
        raise ValueError("Staging manifest run_id does not match rollback run_id; refusing to rollback")
    raw = manifest.get("backup_dir")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Manifest missing backup_dir; refusing to rollback")
    backups_root = ensure_dirs(home)["backups"].resolve()
    try:
        backup_dir = Path(raw).expanduser().resolve()
    except Exception as exc:
        raise ValueError("Invalid backup_dir; refusing to rollback") from exc
    if not _is_relative_to(backup_dir, backups_root):
        raise ValueError("Manifest backup_dir escapes skillopt backups directory; refusing to rollback")
    if not backup_dir.is_dir():
        raise ValueError("Manifest backup_dir does not exist; refusing to rollback")

    backup_skill = (backup_dir / "SKILL.md").resolve()
    if not _is_relative_to(backup_skill, backup_dir):
        raise ValueError("Backup SKILL.md escapes backup_dir; refusing to rollback")
    if not backup_skill.is_file():
        raise ValueError("Backup SKILL.md missing; refusing to rollback")

    restored = read_text(backup_skill)
    restored_sha = sha256_text(restored)

    backup_manifest_path = (backup_dir / "manifest.json").resolve()
    if not backup_manifest_path.exists():
        raise ValueError("Backup manifest missing; refusing to rollback")
    if not _is_relative_to(backup_manifest_path, backup_dir) or not backup_manifest_path.is_file():
        raise ValueError("Backup manifest escapes backup_dir; refusing to rollback")
    backup_manifest = json.loads(read_text(backup_manifest_path))
    if backup_manifest.get("run_id") != run_id or backup_manifest.get("run_id") != manifest.get("run_id"):
        raise ValueError("Backup manifest run_id does not match staging manifest; refusing to rollback")
    backup_skill_path = backup_manifest.get("skill_path")
    if backup_skill_path is None or Path(str(backup_skill_path)).expanduser().resolve() != target:
        raise ValueError("Backup manifest skill_path does not match target; refusing to rollback")
    backup_relpath = backup_manifest.get("skill_relpath")
    if backup_relpath is not None and backup_relpath != manifest.get("skill_relpath"):
        raise ValueError("Backup manifest skill_relpath does not match staging manifest; refusing to rollback")
    backup_original_sha = backup_manifest.get("original_sha256")
    if not backup_original_sha:
        raise ValueError("Backup manifest missing original_sha256; refusing to rollback")
    if backup_original_sha != restored_sha or backup_manifest.get("sha256") != restored_sha:
        raise ValueError("Backup manifest sha does not match SKILL.md; refusing to rollback")
    if backup_original_sha != manifest.get("original_sha256"):
        raise ValueError("Backup manifest original_sha256 does not match staging manifest; refusing to rollback")
    expected_adopted_sha = manifest.get("adopted_sha256") or manifest.get("proposed_sha256")
    for sha_key in ("proposed_sha256", "adopted_sha256"):
        backup_sha = backup_manifest.get(sha_key)
        if backup_sha is None:
            continue
        if backup_sha != manifest.get(sha_key):
            raise ValueError("Backup manifest adopted/proposed sha does not match staging manifest; refusing to rollback")
        if expected_adopted_sha is not None and backup_sha != expected_adopted_sha:
            raise ValueError("Backup manifest adopted/proposed sha does not match current rollback guard; refusing to rollback")
    return backup_skill


# ---------------- Hermes-native full SkillOpt cycle ----------------

def _jsonl_write(path: Path, rows: list[dict[str, Any]]) -> None:
    write_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def _jsonl_read(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-max(0, int(limit)):]


def load_rejected_edit_history(home: Path, skill_name: str, limit: int = 20) -> list[dict[str, Any]]:
    staging = home / "skillopt" / "staging"
    if not staging.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(staging.glob("*/rejected_edits.jsonl"), key=lambda p: p.stat().st_mtime):
        try:
            manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("skill_name") != skill_name:
                continue
        except Exception:
            continue
        for row in _jsonl_read(path, limit=limit):
            rows.append({"run_id": path.parent.name, **row})
    return rows[-max(0, int(limit)):]


def _safe_sql_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("unsafe sqlite identifier")
    return '"' + name + '"'


def _row_timestamp(row: dict[str, Any]) -> float | None:
    for k, v in row.items():
        lk = k.lower()
        if any(x in lk for x in ("time", "created", "updated", "timestamp", "date")) and v is not None:
            try:
                f = float(v)
                if f > 1e12:
                    f /= 1000.0
                if f > 1e9:
                    return f
            except Exception:
                s = str(v).strip().replace("Z", "+00:00")
                try:
                    return datetime.fromisoformat(s).timestamp()
                except Exception:
                    pass
    return None


def harvest_sessions(home: Path, skill: Skill, query: str | None = None, lookback_days: int = 14, limit: int = 50) -> list[dict[str, Any]]:
    """Harvest recent redacted Hermes session/message fragments from state.db and session files."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(lookback_days)))).timestamp()
    terms = [t.lower() for t in [query, skill.name, skill.path.parent.name] if t]
    out: list[dict[str, Any]] = []

    def maybe_add(source: str, text: str, meta: dict[str, Any] | None = None) -> None:
        if len(out) >= limit or not text:
            return
        flat = redact_secrets(re.sub(r"\s+", " ", text))[:4000]
        hay = flat.lower()
        if terms and not any(t in hay for t in terms):
            return
        out.append({"id": f"h{len(out)+1}", "source": source, "text": flat, "review_only": True, "allow_production_adoption": False, "provenance": "direct-session-harvest", "warning": "best-effort direct state/log harvest; review-only and never production adoption evidence", "meta": meta or {}})

    db = home / "state.db"
    if db.exists():
        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                qtable = _safe_sql_ident(table)
                cols = [r[1] for r in cur.execute(f"PRAGMA table_info({qtable})").fetchall()]
                text_cols = [c for c in cols if any(k in c.lower() for k in ("content", "message", "text", "prompt", "response", "goal", "error", "tool"))]
                if not text_cols:
                    continue
                select_cols = list(dict.fromkeys(text_cols + [c for c in cols if any(k in c.lower() for k in ("time", "created", "updated", "role", "session"))]))
                rows = cur.execute(f"SELECT {', '.join(_safe_sql_ident(c) for c in select_cols)} FROM {qtable} ORDER BY rowid DESC LIMIT ?", (max(limit * 10, 100),)).fetchall()
                for r in rows:
                    d = dict(r)
                    ts = _row_timestamp(d)
                    if ts is not None and ts < cutoff:
                        continue
                    text = "\n".join(str(d[c]) for c in text_cols if d.get(c))
                    maybe_add(f"state.db:{table}", text, {k: redact_secrets(str(v))[:500] for k, v in d.items() if k not in text_cols and v is not None})
                    if len(out) >= limit:
                        return out
        except Exception as exc:
            out.append({"id": "harvest-warning", "source": "state.db", "text": f"harvest warning: {type(exc).__name__}: {redact_secrets(str(exc))}", "review_only": True, "allow_production_adoption": False, "provenance": "direct-session-harvest", "meta": {"warning": True}})
        finally:
            try:
                con.close()  # type: ignore[name-defined]
            except Exception:
                pass
    for root in [home / "sessions", home / "logs"]:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
            if len(out) >= limit:
                return out
            if not p.is_file() or p.suffix.lower() not in (".json", ".jsonl", ".txt", ".md", ".log"):
                continue
            if p.stat().st_mtime < cutoff:
                continue
            try:
                maybe_add(str(p.relative_to(home)), read_text(p)[:6000], {"mtime": p.stat().st_mtime})
            except Exception:
                pass
    return out


def evidence_from_state(home: Path, query: str | None = None, limit: int = 6) -> list[str]:
    dummy = Skill(name=query or "skill", path=home / "skills" / (query or "skill") / "SKILL.md", relpath="", sha256="")
    return [x["text"] for x in harvest_sessions(home, dummy, query=query, limit=limit)]


def mine_items(snippets: list[dict[str, Any]], skill: Skill, query: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for i, snip in enumerate(snippets, 1):
        text = snip["text"]
        lower = text.lower()
        tools = sorted(set(re.findall(r"\b(?:tool|function|terminal|pytest|error|traceback|exception|failed|success)\b", lower)))
        failure = any(w in lower for w in ("error", "traceback", "failed", "exception", "wrong", "bug", "timeout"))
        success = any(w in lower for w in ("success", "passed", "done", "fixed", "works", "verified")) and not failure
        items.append({
            "id": f"item-{i:04d}",
            "source_id": snip.get("id"),
            "user_goal": (query or skill.name),
            "assistant_outcome": "failure_or_gap" if failure else ("success" if success else "unknown"),
            "evidence": text[:2000],
            "tools_errors": tools,
            "skill_relevance": 1.0 if skill.name.lower() in lower or (query and query.lower() in lower) else 0.6,
            "success_hints": ["verified outcome"] if success else [],
            "failure_hints": ["tool/error/verification gap present"] if failure else [],
            "review_only": True,
            "allow_production_adoption": False,
            "evidence_provenance": snip.get("provenance") or "direct-session-harvest",
        })
    if not items:
        items.append({"id": "item-0001", "source_id": None, "user_goal": query or skill.name, "assistant_outcome": "unknown", "evidence": "No recent matching Hermes sessions were found; optimize from skill text only.", "tools_errors": [], "skill_relevance": 0.2, "success_hints": [], "failure_hints": ["missing harvested evidence"], "review_only": True, "allow_production_adoption": False, "evidence_provenance": "direct-session-harvest-empty"})
    return items


def split_items(items: list[dict[str, Any]], test: bool = True) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(items, key=lambda x: sha256_text(str(x.get("id", "")) + str(x.get("evidence", ""))))
    buckets = {"train": [], "val": [], "test": []}
    for idx, item in enumerate(ordered):
        mod = idx % (5 if test else 4)
        if mod in (0, 1, 2):
            buckets["train"].append(item)
        elif mod == 3:
            buckets["val"].append(item)
        else:
            buckets["test"].append(item)
    if not buckets["val"] and buckets["train"]:
        buckets["val"].append(buckets["train"].pop())
    return buckets


class LLMBackend:
    def __init__(self, backend: str = "auto", allow_mock: bool = False, ctx: Any = None):
        self.backend = backend or "auto"
        self.allow_mock = allow_mock
        self.ctx = ctx
        self.mode = "mock" if self.backend == "mock" else "hermes"
        if self.backend in ("auto", "hermes"):
            llm = self._resolve_llm(ctx)
            if llm is not None and (getattr(llm, "complete_structured", None) or getattr(llm, "complete", None)):
                self.llm = llm
                self.mode = "hermes"
                return
            if self.backend == "hermes" or not allow_mock:
                ctx_type = type(ctx).__name__ if ctx is not None else "None"
                available = sorted(name for name in ("llm", "complete_structured", "complete", "acomplete_structured", "acomplete") if ctx is not None and getattr(ctx, name, None))
                raise RuntimeError(
                    "Hermes LLM ctx unavailable. Expected plugin runtime ctx.llm with complete_structured/complete "
                    f"or a ctx exposing those methods directly; got ctx_type={ctx_type}, available_llm_attrs={available}. "
                    "Use backend='mock' explicitly or backend='auto' with allow_mock=true for tests/smoke."
                )
        self.llm = None
        self.mode = "mock"

    def _resolve_llm(self, ctx: Any) -> Any:
        if ctx is None:
            return None
        llm = getattr(ctx, "llm", None)
        if llm is not None:
            return llm
        if getattr(ctx, "complete_structured", None) or getattr(ctx, "complete", None):
            return ctx
        return None

    def _structured_result_to_dict(self, res: Any) -> dict[str, Any] | None:
        if isinstance(res, dict):
            return res
        parsed = getattr(res, "parsed", None)
        if isinstance(parsed, dict):
            return parsed
        if hasattr(res, "model_dump"):
            dumped = res.model_dump()
            if isinstance(dumped, dict):
                parsed = dumped.get("parsed")
                if isinstance(parsed, dict):
                    return parsed
                return dumped
        return None

    def _complete_structured_json(self, prompt: str, schema_hint: dict[str, Any]) -> dict[str, Any] | None:
        complete_structured = getattr(self.llm, "complete_structured", None)
        if not complete_structured:
            return None
        try:
            res = complete_structured(prompt=prompt, schema=schema_hint)
        except TypeError as old_signature_error:
            try:
                res = complete_structured(
                    instructions="Return a single strict JSON object for the requested SkillOpt optimizer step.",
                    input=[{"type": "text", "text": prompt}],
                    json_schema=schema_hint,
                    json_mode=True,
                    schema_name=f"skillopt_{schema_hint.get('kind') or 'optimizer'}",
                    purpose="hermes-skillopt.optimizer",
                )
            except TypeError:
                raise old_signature_error
        return self._structured_result_to_dict(res)

    def _complete_json(self, prompt: str) -> Any:
        complete = getattr(self.llm, "complete", None)
        if not complete:
            raise RuntimeError("Hermes LLM ctx has no complete or compatible complete_structured method")
        messages = [{"role": "user", "content": prompt + "\nReturn strict JSON only."}]
        try:
            return complete(messages, purpose="hermes-skillopt.optimizer")
        except TypeError:
            return complete(prompt + "\nReturn strict JSON only.")

    def json(self, prompt: str, schema_hint: dict[str, Any], repair_path: Path | None = None) -> dict[str, Any]:
        prompt = redact_secrets(prompt)
        if self.mode == "mock":
            return self._mock(prompt, schema_hint)
        data = self._complete_structured_json(prompt, schema_hint)
        if isinstance(data, dict):
            return data
        raw = self._complete_json(prompt)
        text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
        try:
            return json.loads(text)
        except Exception as first:
            m = re.search(r"\{.*\}", text, flags=re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
            if repair_path:
                write_text(repair_path, json.dumps({"error": str(first), "raw_redacted": redact_secrets(text)[:8000]}, ensure_ascii=False, indent=2) + "\n")
            raise ValueError("LLM JSON parse failed") from first

    def _mock(self, prompt: str, schema_hint: dict[str, Any]) -> dict[str, Any]:
        kind = schema_hint.get("kind")
        if kind == "reflect":
            return {"recurring_defects": ["insufficient verification after edits", "unclear handling of tool errors"], "missing_rules": ["state expected artifacts and run tests before final"], "over_broad_rules": [], "verification_gaps": ["no train/val/test gate mentioned"], "reasoning": "mock reflection over redacted harvested items"}
        if kind == "edit":
            return {"edits": [{"op": "append", "text": "\n\n## SkillOpt Learned Rules\n\n- Verify changes with the most relevant command or test before reporting completion.\n- Preserve safety/path guards, backup/rollback reversibility, and avoid mutating real Hermes profile state during smoke tests.\n- When tool errors occur, summarize the blocker and try a bounded alternate path.\n"}], "reasoning": "bounded append based on recurring verification and safety gaps"}
        if kind == "gate":
            return {"current_score": 0.45, "candidate_score": 0.82, "accepted": True, "rationale": "candidate adds concrete verification and safety rules"}
        return {}


def reflect_items(backend: LLMBackend, train: list[dict[str, Any]], current_skill: str, run_dir: Path, iteration: int) -> dict[str, Any]:
    prompt = "Reflect on Hermes skill performance. Redacted train items:\n" + json.dumps(train, ensure_ascii=False)[:12000] + "\nCurrent skill:\n" + current_skill[:8000]
    data = backend.json(prompt, {"kind": "reflect"}, run_dir / f"llm_reflect_repair_{iteration}.json")
    return data


def bounded_edits(backend: LLMBackend, reflections: dict[str, Any], current_skill: str, edit_budget: int, run_dir: Path, iteration: int) -> dict[str, Any]:
    prompt = "Generate bounded edits for SKILL.md. Allowed ops: append, replace, delete, insert_after. Do not edit YAML frontmatter. Max edits: %d. Reflections:\n%s\nSkill:\n%s" % (edit_budget, json.dumps(reflections, ensure_ascii=False), current_skill[:12000])
    data = backend.json(prompt, {"kind": "edit"}, run_dir / f"llm_edit_repair_{iteration}.json")
    edits = data.get("edits") if isinstance(data, dict) else []
    if not isinstance(edits, list):
        edits = []
    data["edits"] = edits[: max(0, int(edit_budget))]
    return data


def _stable_json_sha(data: Any) -> str:
    return sha256_text(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))


def _best_effort_repo_commit(path: Path) -> dict[str, Any]:
    """Return local git identity for provenance without requiring git/network.

    The SkillOpt adapter is intentionally standalone; this records the plugin
    repo revision that produced a staged run when available, but never fetches,
    merges, or treats upstream state as authority for adoptability.
    """

    try:
        code, head = _git(["rev-parse", "HEAD"], path, timeout=10)
        code_dirty, dirty = _git(["status", "--porcelain"], path, timeout=10)
        return {
            "repo_path": str(path.resolve()),
            "commit": head if code == 0 and head else None,
            "dirty": bool(dirty.strip()) if code_dirty == 0 else None,
            "available": code == 0 and bool(head),
        }
    except Exception as exc:  # pragma: no cover - defensive provenance only
        return {"repo_path": str(path.resolve()), "commit": None, "dirty": None, "available": False, "error": f"{type(exc).__name__}: {exc}"}


def _upstream_lock_provenance() -> dict[str, Any]:
    lock = PLUGIN_ROOT / "skillopt_upstream.lock"
    if not lock.is_file():
        return {"path": str(lock), "exists": False, "sha256": None, "pinned_commit": None}
    data: dict[str, Any] | None = None
    try:
        parsed = json.loads(read_text(lock))
        data = parsed if isinstance(parsed, dict) else None
    except Exception:
        data = None
    return {
        "path": str(lock),
        "exists": True,
        "sha256": sha256_file(lock),
        "pinned_commit": (data or {}).get("pinned_commit"),
        "upstream_url": (data or {}).get("upstream_url", UPSTREAM_URL),
    }


def _profile_fingerprint(home: Path) -> dict[str, Any]:
    payload = {"hermes_home": str(home.resolve()), "skills_dir": str((home / "skills").resolve())}
    return {**payload, "fingerprint_sha256": _stable_json_sha(payload)}


def _task_fingerprint(tasks: dict[str, list[Any]]) -> dict[str, Any]:
    rows = []
    for split, split_tasks in sorted(tasks.items()):
        for task in split_tasks:
            rows.append({
                "id": getattr(task, "id", None),
                "split": split,
                "source": getattr(task, "source", None),
                "expected_terms": list(getattr(task, "expected_terms", ())),
                "assertions": list(getattr(task, "assertions", ())),
                "required_markers": list(getattr(task, "required_markers", ())),
                "forbidden_markers": list(getattr(task, "forbidden_markers", ())),
                "weight": getattr(task, "weight", None),
                "production_gate_eligible": bool(getattr(task, "metadata", {}).get("production_gate_eligible")),
            })
    return {"sha256": _stable_json_sha(rows), "task_ids": [r["id"] for r in rows], "task_count": len(rows)}


def _provenance_fingerprint(*, eval_file_used: str | None, tasks: dict[str, list[Any]], backend_mode: str, target_executor_mode: str, target_config_id: str, production_gate_available: bool, home: Path | None = None, skill_relpath: str | None = None, original_sha256: str | None = None, proposed_sha256: str | None = None, optimizer_config: dict[str, Any] | None = None, target_config: dict[str, Any] | None = None, gate_policy: dict[str, Any] | None = None, production_eval_policy: dict[str, Any] | None = None, eval_pack: dict[str, Any] | None = None, optimizer_prompt_fingerprints: list[dict[str, Any]] | None = None, algorithm_version: str = ALGORITHM_VERSION) -> dict[str, Any]:
    eval_sha = sha256_file(Path(eval_file_used)) if eval_file_used and Path(eval_file_used).is_file() else None
    task_fp = _task_fingerprint(tasks)
    optimizer_payload = {"backend": backend_mode, **(optimizer_config or {})}
    target_payload = target_config or {"executor": target_executor_mode, "target_config_id": target_config_id}
    gate_payload = gate_policy or {"mode": "soft", "min_delta": 0.0, "llm_override_allowed": False}
    skill_payload = {"skill_relpath": skill_relpath, "original_sha256": original_sha256, "proposed_sha256": proposed_sha256}
    plugin_repo = _best_effort_repo_commit(PLUGIN_ROOT)
    upstream_lock = _upstream_lock_provenance()
    profile = _profile_fingerprint(home) if home is not None else None
    payload = {
        "schema_version": "skillopt-provenance-v2",
        "algorithm_version": algorithm_version,
        "plugin_repo": plugin_repo,
        "upstream_lock": upstream_lock,
        "eval_file": eval_file_used,
        "eval_file_sha256": eval_sha,
        "eval_pack": eval_pack or {},
        "eval_pack_id": (eval_pack or {}).get("pack_id"),
        "eval_pack_version": (eval_pack or {}).get("version"),
        "eval_pack_fingerprint_sha256": (eval_pack or {}).get("fingerprint_sha256"),
        "eval_fingerprint_sha256": _stable_json_sha({"eval_file": eval_file_used, "eval_file_sha256": eval_sha, "eval_pack": eval_pack or {}, "task_sha256": task_fp["sha256"]}),
        "task_sha256": task_fp["sha256"],
        "optimizer_backend": backend_mode,
        "optimizer_config": optimizer_config or {},
        "optimizer_prompt_fingerprints": optimizer_prompt_fingerprints or [],
        "optimizer_prompt_fingerprint_sha256": _stable_json_sha(optimizer_prompt_fingerprints or []),
        "optimizer_backend_config": optimizer_payload,
        "optimizer_fingerprint_sha256": _stable_json_sha(optimizer_payload),
        "target_executor": target_executor_mode,
        "target_config_id": target_config_id,
        "target_backend_config": target_payload,
        "target_fingerprint_sha256": target_payload.get("fingerprint_sha256") if isinstance(target_payload, dict) and target_payload.get("fingerprint_sha256") else _stable_json_sha(target_payload),
        "gate_policy": gate_payload,
        "gate_policy_fingerprint_sha256": _stable_json_sha(gate_payload),
        "profile": profile,
        "profile_fingerprint_sha256": profile.get("fingerprint_sha256") if isinstance(profile, dict) else None,
        "skill": skill_payload,
        "skill_fingerprint_sha256": _stable_json_sha(skill_payload),
        "production_eval_policy_fingerprint_sha256": (production_eval_policy or {}).get("policy_fingerprint_sha256"),
        "production_gate_available": production_gate_available,
    }
    # Back-compat aliases retained for older review/report consumers.
    payload["backend"] = backend_mode
    return {**payload, "fingerprint_sha256": _stable_json_sha(payload), "task_ids": task_fp["task_ids"], "task_count": task_fp["task_count"]}


def _per_task_delta(current_eval: dict[str, Any] | None, candidate_eval: dict[str, Any] | None) -> list[dict[str, Any]]:
    current_rows = {r.get("task_id"): r for r in ((current_eval or {}).get("results") or []) if isinstance(r, dict)}
    out = []
    for cand in ((candidate_eval or {}).get("results") or []):
        if not isinstance(cand, dict):
            continue
        raw_cur = current_rows.get(cand.get("task_id"))
        cur = raw_cur if isinstance(raw_cur, dict) else {}
        cur_meta = cur.get("metadata") if isinstance(cur.get("metadata"), dict) else {}
        cand_meta = cand.get("metadata") if isinstance(cand.get("metadata"), dict) else {}
        cur_traj = cur_meta.get("trajectory") if isinstance(cur_meta.get("trajectory"), dict) else {}
        cand_traj = cand_meta.get("trajectory") if isinstance(cand_meta.get("trajectory"), dict) else {}
        cur_scores = cur_traj.get("scores") if isinstance(cur_traj.get("scores"), dict) else {}
        cand_scores = cand_traj.get("scores") if isinstance(cand_traj.get("scores"), dict) else {}
        cur_failed = set(cur_meta.get("failed_checks") or cur_scores.get("failed_checks") or [])
        cand_failed = set(cand_meta.get("failed_checks") or cand_scores.get("failed_checks") or [])
        cur_passed = set(cur_meta.get("passed_checks") or cur_scores.get("passed_checks") or [])
        cand_passed = set(cand_meta.get("passed_checks") or cand_scores.get("passed_checks") or [])
        changed_failed = sorted(cur_failed ^ cand_failed)
        changed_passed = sorted(cur_passed ^ cand_passed)
        changed_checks = sorted(set(changed_failed + changed_passed))
        expected_term_changes = [c for c in changed_checks if c.startswith("expected_keyword:") or c.startswith("all_required_keyword:")]
        assertion_changes = [c for c in changed_checks if c.startswith("assertion:")]
        delta = round(float(cand.get("score", 0.0)) - float(cur.get("score", 0.0)), 6)
        pass_changed = cur.get("passed") != cand.get("passed")
        out.append({
            "task_id": cand.get("task_id"),
            "current_score": cur.get("score"),
            "candidate_score": cand.get("score"),
            "delta": delta,
            "current_passed": cur.get("passed"),
            "candidate_passed": cand.get("passed"),
            "regressed": delta < 0 or (cur.get("passed") is True and cand.get("passed") is False),
            "candidate_vs_current_sensitive": bool(delta or pass_changed or changed_checks),
            "sensitivity_warning": None if (delta or pass_changed or changed_checks) else "no score/pass/check delta; this task may not distinguish candidate from current",
            "changed_checks": changed_checks,
            "changed_expected_terms": expected_term_changes,
            "changed_assertions": assertion_changes,
            "production_gate_eligible": bool(cand_meta.get("production_gate_eligible")),
        })
    return out


def _heldout_test_sensitivity(current_test_eval: dict[str, Any] | None, candidate_test_eval: dict[str, Any] | None) -> dict[str, Any]:
    rows = _per_task_delta(current_test_eval, candidate_test_eval)
    insensitive = [row.get("task_id") for row in rows if row.get("candidate_vs_current_sensitive") is False]
    return {
        "schema_version": "skillopt-heldout-test-sensitivity-v1",
        "candidate_vs_current_sensitive": bool(rows) and not insensitive,
        "insensitive_task_ids": insensitive,
        "warnings": ["heldout test has no candidate-vs-current sensitivity for: " + ", ".join(str(x) for x in insensitive)] if insensitive else [],
        "per_task_delta": rows,
    }


def _production_eval_policy(evidence: dict[str, Any], production_gate_available: bool, test_gate_eligible: bool) -> dict[str, Any]:
    policy = {
        "policy_version": "production-eval-schema-v1",
        "adopt_requires": [
            "eval_file under active HERMES_HOME with hash/path guard",
            "explicit curated validation task with concrete scorecard/assertions",
            "strict candidate improvement on frozen target executor",
            "held-out production-eligible test split passes threshold",
            "fallback/session/synthetic tasks are review-only and cannot authorize adopt",
            "frozen_hermes_target_execution_v1 target_execution_evidence artifact is present and complete",
            "deterministic reviewer gate artifact passes; reviewer notes cannot override validation gate",
        ],
        "eval_file": evidence.get("eval_file"),
        "eval_pack": evidence.get("eval_pack") or {},
        "eval_pack_id": evidence.get("eval_pack_id"),
        "eval_pack_version": evidence.get("eval_pack_version"),
        "eval_pack_fingerprint_sha256": evidence.get("eval_pack_fingerprint_sha256"),
        "split_governance": evidence.get("split_governance") or {},
        "curated_task_count": evidence.get("curated_task_count", 0),
        "production_gate_available": production_gate_available,
        "test_gate_eligible": test_gate_eligible,
    }
    policy["policy_fingerprint_sha256"] = _stable_json_sha(policy)
    return policy


def _eval_evidence_ledger(payload: dict[str, Any] | None = None, *, target_execution_evidence: dict[str, Any] | None = None, reviewer_gate: dict[str, Any] | None = None) -> dict[str, Any]:
    """Explicit maturity ledger for all review/status/adopt surfaces."""

    p: dict[str, Any] = payload if isinstance(payload, dict) else {}
    txe: dict[str, Any] = target_execution_evidence if isinstance(target_execution_evidence, dict) else {}
    if not txe and isinstance(p.get("target_execution_evidence"), dict):
        txe = p["target_execution_evidence"]
    rg: dict[str, Any] = reviewer_gate if isinstance(reviewer_gate, dict) else {}
    if not rg and isinstance(p.get("reviewer_gate"), dict):
        rg = p["reviewer_gate"]
    complete_frozen = txe.get("classification") == "frozen_hermes_target_execution_v1" and txe.get("complete") is True
    real_runtime = txe.get("real_hermes_runtime_evidence") is True and txe.get("real_hermes_runtime_invocation") is True
    no_task_commands = txe.get("task_commands_executed") is False
    internal_review_only = txe.get("internal_review_only_runner") is True
    reviewer_adoptable = rg.get("passed") is True and rg.get("adoptable_after_reviewer_gate") is True
    production_ready = bool(complete_frozen and real_runtime and no_task_commands and not internal_review_only and reviewer_adoptable)
    level = "production" if production_ready else "review_only"
    blockers: list[str] = []
    if not complete_frozen:
        blockers.append("complete frozen target execution evidence is missing")
    if not real_runtime:
        blockers.append("real Hermes runtime invocation/evidence is missing")
    if not no_task_commands:
        blockers.append("task-provided command execution is absent/unknown or executed")
    if internal_review_only:
        blockers.append("internal deterministic/replay/sandbox/live-disabled runner is review-only")
    if not reviewer_adoptable:
        blockers.append("deterministic reviewer gate is missing or not adoptable")
    return {
        "schema_version": "hermes-skillopt-evidence-ledger-v1",
        "eval_level": level,
        "evidence_maturity": "production_runtime_complete" if production_ready else "review_only_static_replay_or_incomplete_runtime",
        "review_only_unless_complete_real_hermes_runtime": True,
        "production_runtime_ready": production_ready,
        "complete_frozen_evidence": complete_frozen,
        "real_hermes_runtime_evidence": txe.get("real_hermes_runtime_evidence") is True,
        "real_hermes_runtime_invocation": txe.get("real_hermes_runtime_invocation") is True,
        "task_commands_executed": txe.get("task_commands_executed"),
        "internal_review_only_runner": internal_review_only,
        "reviewer_gate_adoptable": reviewer_adoptable,
        "blockers": sorted(set(blockers)),
    }


def _flatten_eval_results(*evals: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in evals:
        if isinstance(ev, dict):
            rows.extend(r for r in (ev.get("results") or []) if isinstance(r, dict))
    return rows


def _target_execution_evidence_summary(*, target_config: dict[str, Any], validation_summary: dict[str, Any] | None, production_validation_summary: dict[str, Any] | None, test_results: dict[str, Any], target_backend: str) -> dict[str, Any]:
    """Uniform, hashed evidence summary for frozen-Hermes target runs."""

    evals: list[dict[str, Any]] = []
    if isinstance(validation_summary, dict) and isinstance(validation_summary.get("candidate_eval"), dict):
        evals.append(validation_summary["candidate_eval"])
    if isinstance(production_validation_summary, dict) and isinstance(production_validation_summary.get("candidate_eval"), dict):
        evals.append(production_validation_summary["candidate_eval"])
    if isinstance(test_results, dict):
        evals.append(test_results)
    results = _flatten_eval_results(*evals)
    metadata_rows: list[dict[str, Any]] = []
    for r in results:
        meta = r.get("metadata")
        if isinstance(meta, dict):
            metadata_rows.append(meta)
    contract_checks = [c for ev in evals for c in (ev.get("eval_execution_contract_checks") or []) if isinstance(c, dict)]
    params = target_config.get("parameters") if isinstance(target_config.get("parameters"), dict) else {}
    frozen_requested = target_backend in {"hermes_sandbox_executor_mvp", "frozen-hermes", "frozen_hermes_target_execution_v1", "sandbox"} or bool(params.get("frozen_hermes_contract"))
    missing = sorted({str(x) for c in contract_checks for x in (c.get("missing_runtime_evidence") or []) if x})
    exemplar = next((m for m in metadata_rows if m.get("frozen_hermes_contract") == "frozen_hermes_target_execution_v1"), metadata_rows[0] if metadata_rows else {})
    runtime_available = any(isinstance(m.get("runtime_fingerprint"), dict) and m["runtime_fingerprint"].get("available") is True for m in metadata_rows)
    sandbox_mvp = str(target_config.get("executor") or target_backend) == "hermes_sandbox_executor_mvp"
    real_hermes_runtime_evidence = any(m.get("real_hermes_runtime_evidence") is True for m in metadata_rows)
    real_hermes_runtime_invocation = any(isinstance(m.get("runtime_fingerprint"), dict) and m["runtime_fingerprint"].get("invokes_hermes_core_or_gateway") is True for m in metadata_rows)
    raw_params = target_config.get("parameters")
    params = raw_params if isinstance(raw_params, dict) else {}
    backend_kind = str(params.get("backend_kind") or "")
    internal_review_only_runner = sandbox_mvp or backend_kind in {"sandbox_fixed_internal_runner", "deterministic_trace_replay", "deterministic_fallback_scorecard"}
    permissions = exemplar.get("permissions") if isinstance(exemplar.get("permissions"), dict) else {}
    task_commands_executed = any(m.get("task_commands_executed") is True for m in metadata_rows)
    complete = bool(
        frozen_requested
        and contract_checks
        and not missing
        and permissions.get("task_commands_allowed") is False
        and permissions.get("profile_write_allowed") is False
        and runtime_available
        and real_hermes_runtime_evidence
        and real_hermes_runtime_invocation
        and not internal_review_only_runner
        and not task_commands_executed
    )
    payload = {
        "schema_version": "skillopt-target-execution-evidence-v1",
        "classification": "frozen_hermes_target_execution_v1" if frozen_requested else "non_frozen_or_scorecard_target",
        "implementation_label": "sandbox_mvp_fixed_runner_review_only" if sandbox_mvp else (backend_kind or "deterministic_or_readonly_target"),
        "eval_level": "production" if complete else "review_only",
        "evidence_maturity": "production_runtime_complete" if complete else "review_only_static_replay_or_incomplete_runtime",
        "production_gate_eligible": complete,
        "production_adoption_requires_complete_evidence": True,
        "complete": complete,
        "review_only_unless_complete": True,
        "explicit_real_runtime_required": True,
        "real_hermes_runtime_evidence": real_hermes_runtime_evidence,
        "real_hermes_runtime_invocation": real_hermes_runtime_invocation,
        "internal_review_only_runner": internal_review_only_runner,
        "frozen_target_config": target_config,
        "frozen_target_config_id": target_config.get("target_config_id"),
        "frozen_target_fingerprint_sha256": target_config.get("fingerprint_sha256"),
        "provider_fingerprint": exemplar.get("provider_fingerprint"),
        "model_fingerprint": exemplar.get("model_fingerprint"),
        "toolset_fingerprint": exemplar.get("toolset_fingerprint"),
        "tool_policy_fingerprint": exemplar.get("tool_policy_fingerprint"),
        "session_fingerprint": exemplar.get("session_fingerprint"),
        "runtime_fingerprint": exemplar.get("runtime_fingerprint"),
        "isolated_runtime_proof": exemplar.get("isolated_runtime_evidence"),
        "permissions": {"task_commands_allowed": permissions.get("task_commands_allowed"), "profile_write_allowed": permissions.get("profile_write_allowed"), "live_profile_writes": permissions.get("live_profile_writes")},
        "task_command_policy": "task-provided commands disabled/blocked by default; task-provided command execution makes evidence non-production",
        "task_commands_executed": task_commands_executed,
        "trajectory_or_transcript_artifact_fingerprint": {
            "validation": (validation_summary or {}).get("candidate_eval", {}).get("trajectory_fingerprint_sha256") if isinstance(validation_summary, dict) else None,
            "production_validation": (production_validation_summary or {}).get("candidate_eval", {}).get("trajectory_fingerprint_sha256") if isinstance(production_validation_summary, dict) else None,
            "test": test_results.get("trajectory_fingerprint_sha256") if isinstance(test_results, dict) else None,
            "per_result_trace_fingerprints": [m.get("trace_fingerprint_sha256") for m in metadata_rows if m.get("trace_fingerprint_sha256")],
        },
        "execution_scoring_evidence": [m.get("execution_scoring_evidence") for m in metadata_rows if isinstance(m.get("execution_scoring_evidence"), dict)][:20],
        "contract_checks": contract_checks,
        "missing_required_evidence": missing,
        "evidence_result_count": len(metadata_rows),
    }
    payload["fingerprint_sha256"] = _stable_json_sha(payload)
    return payload


def _reviewer_gate_artifact(*, candidate_summary: list[dict[str, Any]], validation_summary: dict[str, Any] | None, production_validation_summary: dict[str, Any] | None, test_results: dict[str, Any], regression_cases: list[Any], adoptability_reasons: list[str], final_status: str, deterministic_adoptable_before_review: bool) -> dict[str, Any]:
    """Deterministic reviewer checklist; optional LLM notes remain non-authoritative."""

    ranked = [c for round_row in candidate_summary for c in (round_row.get("ranked_candidates") or []) if isinstance(c, dict)]
    selected_ids = {str(c.get("candidate_id")) for c in ranked if c.get("selected")}
    non_selected = [c for c in ranked if str(c.get("candidate_id")) not in selected_ids]
    selected = [c for c in ranked if str(c.get("candidate_id")) in selected_ids]
    protected_reasons = {"protected_append", "protected_replace", "protected_insert", "protected_section", "protected_frontmatter", "boundary_marker_mutation", "allowed_region_marker_mutation", "outside_allowed_region"}
    rejected_reasons = [str(e.get("reason")) for c in ranked for e in (c.get("rejected_edits") or []) if isinstance(e, dict) and e.get("reason")]
    validation_failures = [c for c in selected if c.get("validation_ok") is False]
    hard_failures: list[Any] = []
    for gate in (validation_summary, production_validation_summary):
        if isinstance(gate, dict) and isinstance(gate.get("metric_summary"), dict):
            hard_failures.extend(gate["metric_summary"].get("candidate_hard_failures") or [])
            hard_failures.extend(gate["metric_summary"].get("production_candidate_hard_failures") or [])
    if isinstance(test_results, dict):
        hard_failures.extend(test_results.get("regression_cases") or [])
    checklist = {
        "bounded_edit_validation_passed": not validation_failures,
        "protected_marker_mutation_absent": not (set(rejected_reasons) & protected_reasons),
        "validation_delta_nonnegative": all(float(c.get("delta") or 0.0) >= 0.0 for c in selected),
        "production_delta_nonnegative_or_unavailable": all(c.get("production_delta") is None or float(c.get("production_delta") or 0.0) >= 0.0 for c in selected),
        "heldout_test_no_regressions": not regression_cases,
        "hard_failures_absent": not hard_failures,
        "deterministic_gate_adoptable_before_review": bool(deterministic_adoptable_before_review),
    }
    reviewer_passed = all(bool(v) for v in checklist.values())
    payload = {
        "schema_version": "skillopt-reviewer-gate-v1",
        "deterministic": True,
        "authority": "deterministic_validation_gate",
        "deterministic_validation_gate_authoritative": True,
        "reviewer_artifact_authoritative": False,
        "llm_notes_authoritative": False,
        "cannot_override_validation_gate": True,
        "final_status": final_status,
        "checklist": checklist,
        "passed": reviewer_passed,
        "adoptable_after_reviewer_gate": bool(deterministic_adoptable_before_review and reviewer_passed),
        "selected_candidate_ids": sorted(selected_ids),
        "selected_candidate_deltas": [{"candidate_id": c.get("candidate_id"), "validation_delta": c.get("delta"), "production_delta": c.get("production_delta"), "validation_ok": c.get("validation_ok"), "accepted": c.get("accepted"), "production_accepted": c.get("production_accepted")} for c in selected],
        "non_selected_candidate_reasons": [{"candidate_id": c.get("candidate_id"), "rank": c.get("rank"), "accepted": c.get("accepted"), "selected": False, "reasons": c.get("rejection_reasons") or (["lower deterministic rank than selected candidate"] if c.get("accepted") else ["not accepted by deterministic gate"])} for c in non_selected],
        "hard_failures": hard_failures,
        "regression_cases": regression_cases,
        "adoptability_reasons_before_reviewer": adoptability_reasons,
        "optional_llm_notes": {"present": False, "authoritative": False},
    }
    payload["fingerprint_sha256"] = _stable_json_sha(payload)
    return payload


def _resume_input_payload(*, home: Path, target: Skill, original: str, query: str | None, lookback_days: int, limit: int, iterations: int, edit_budget: int, candidate_count: int, optimizer_backend: str | None, target_backend: str | None, gate_mode: str, eval_file: str | None, allow_mock: bool) -> dict[str, Any]:
    from hermes_skillopt.env import load_eval_pack, resolve_eval_file
    from hermes_skillopt.state import SkillState
    from hermes_skillopt.target import TRACE_SCHEMA_VERSION, TargetExecutor

    state = SkillState(name=target.name, path=target.path, relpath=target.relpath, text=original, sha256=sha256_text(original), hermes_home=home)
    native_metadata = native_skill_metadata_snapshot(home, target)
    resolved_eval = resolve_eval_file(home, state, eval_file)
    eval_path = str(resolved_eval) if resolved_eval else None
    eval_pack_identity: dict[str, Any] | None = None
    if eval_path and Path(eval_path).is_file():
        try:
            _tasks, pack = load_eval_pack(Path(eval_path))
            eval_pack_identity = pack.as_dict()
        except Exception:
            eval_pack_identity = None
    return {
        "schema_version": "skillopt-checkpoint-v1",
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_relpath": target.relpath,
        "original_sha256": sha256_text(original),
        "native_hermes_metadata": native_metadata,
        "query": query,
        "lookback_days": int(lookback_days),
        "limit": int(limit),
        "iterations": max(1, int(iterations)),
        "edit_budget": int(edit_budget),
        "candidate_count": max(1, int(candidate_count)),
        "optimizer_backend": optimizer_backend,
        "target_backend": target_backend,
        "target_config_id": TargetExecutor().target_config_id,
        "target_trace_schema": TRACE_SCHEMA_VERSION,
        "gate_mode": gate_mode,
        "eval_file": eval_path,
        "eval_file_sha256": sha256_file(Path(eval_path)) if eval_path and Path(eval_path).is_file() else None,
        "eval_pack": eval_pack_identity or {},
        "eval_pack_id": (eval_pack_identity or {}).get("pack_id"),
        "eval_pack_version": (eval_pack_identity or {}).get("version"),
        "eval_pack_fingerprint_sha256": (eval_pack_identity or {}).get("fingerprint_sha256"),
        "allow_mock": bool(allow_mock),
    }


def _write_checkpoint(run_dir: Path, payload: dict[str, Any], *, status: str, completed_stages: list[str] | None = None) -> None:
    data = {"schema_version": "skillopt-checkpoint-v1", "status": status, "input": payload, "input_sha256": _stable_json_sha(payload), "completed_stages": completed_stages or [], "stage_resume_policy": "read_only_inspection_or_completed_run_reuse_only", "updated_at": datetime.now(timezone.utc).isoformat()}
    write_text(run_dir / "checkpoint.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _load_resume_checkpoint(run_dir: Path, expected_input: dict[str, Any]) -> dict[str, Any]:
    cp_path = run_dir / "checkpoint.json"
    if not cp_path.is_file():
        raise ValueError("Resume requested but checkpoint.json is missing")
    checkpoint = json.loads(read_text(cp_path))
    if checkpoint.get("input_sha256") != _stable_json_sha(expected_input) or checkpoint.get("input") != expected_input:
        raise ValueError("Resume checkpoint input/config/provenance fingerprint mismatch; refusing to reuse artifacts")
    return checkpoint


def _resume_completed_run(run_dir: Path, expected_input: dict[str, Any]) -> dict[str, Any] | None:
    _load_resume_checkpoint(run_dir, expected_input)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, manifest)
    reviewed = review(str(manifest.get("run_id")), hermes_home_path=str(expected_input["hermes_home"]))
    reviewed.update({"success": True, "resumed": True, "resume_reused": True, "checkpoint_status": "complete"})
    return reviewed


def _maybe_sha_file(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def _slim_artifact_state(run_dir: Path, files: dict[str, str] | None = None) -> dict[str, Any]:
    tracked = {
        "checkpoint": "checkpoint.json",
        "manifest": "manifest.json",
        "report": "report.md",
        "original": "original_SKILL.md",
        "current": "current_SKILL.md",
        "proposed": "proposed_SKILL.md",
        "best": "best_skill.md",
        "history": "history.json",
        "target_binding": "target_binding.json",
        "provenance_binding": "provenance_binding.json",
    }
    if files:
        tracked.update({k: v for k, v in files.items() if k in {"original", "current", "proposed", "best", "history", "target_binding", "provenance_binding", "report"}})
    out: dict[str, Any] = {}
    for name, rel in tracked.items():
        path = run_dir / rel
        out[name] = {"path": rel, "exists": path.is_file(), "sha256": _maybe_sha_file(path)}
    return out


def _score_provenance_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Concise score/provenance identity for slim status/review/fleet surfaces."""

    m = manifest or {}
    raw_provenance = m.get("provenance_fingerprint")
    raw_eval_pack = m.get("eval_pack")
    raw_policy = m.get("production_eval_policy")
    raw_gate_policy = m.get("gate_policy")
    raw_split_scores = m.get("split_scores")
    raw_task_counts = m.get("task_counts")
    provenance: dict[str, Any] = dict(raw_provenance) if isinstance(raw_provenance, dict) else {}
    eval_pack: dict[str, Any] = dict(raw_eval_pack) if isinstance(raw_eval_pack, dict) else {}
    policy: dict[str, Any] = dict(raw_policy) if isinstance(raw_policy, dict) else {}
    gate_policy: dict[str, Any] = dict(raw_gate_policy) if isinstance(raw_gate_policy, dict) else {}
    split_scores: dict[str, Any] = dict(raw_split_scores) if isinstance(raw_split_scores, dict) else {}
    task_counts: dict[str, Any] = dict(raw_task_counts) if isinstance(raw_task_counts, dict) else {}
    warnings: list[str] = []
    test_score_present = m.get("test_score") is not None or (isinstance(m.get("test_results"), dict) and m["test_results"].get("score") is not None)
    if test_score_present and not m.get("heldout_test_sensitivity"):
        warnings.append("heldout test score is present but no heldout_test_sensitivity artifact/summary is recorded")
    if not m.get("eval_file") and not m.get("eval_pack_id") and not eval_pack:
        warnings.append("score provenance has no explicit eval pack/file identity; treat as review-only")
    score_source = "production_curated_eval_pack" if m.get("production_gate_eligible") is True else "review_only_or_non_production_eval"
    if str(m.get("backend") or "").lower() == "mock" or str(m.get("optimizer_backend") or "").lower() == "mock":
        score_source = "mock_review_only"
    heldout_test = m.get("test_score")
    if heldout_test is None and isinstance(m.get("test_results"), dict):
        heldout_test = m["test_results"].get("score")
    return {
        "schema_version": "hermes-skillopt-score-provenance-v1",
        "target_executor": m.get("target_executor") or m.get("target_backend") or provenance.get("target_executor"),
        "target_backend": m.get("target_backend") or m.get("target_executor") or provenance.get("target_backend"),
        "optimizer_backend": m.get("optimizer_backend") or m.get("backend"),
        "eval_pack": {
            "id": m.get("eval_pack_id") or eval_pack.get("pack_id"),
            "version": m.get("eval_pack_version") or eval_pack.get("version"),
            "path": m.get("eval_file"),
            "fingerprint_sha256": m.get("eval_pack_fingerprint_sha256") or eval_pack.get("fingerprint_sha256") or provenance.get("eval_pack_fingerprint_sha256"),
            "eval_file_sha256": provenance.get("eval_file_sha256") or m.get("eval_file_sha256"),
        },
        "policy": {
            "gate_mode": gate_policy.get("mode") or m.get("gate_mode"),
            "production_eval_policy_version": policy.get("policy_version"),
            "production_eval_policy_fingerprint_sha256": policy.get("policy_fingerprint_sha256"),
        },
        "score_source": score_source,
        "split_labels": {
            "selection": "validation",
            "production_validation": "production_validation",
            "heldout_final_gate": "test",
            "available": sorted(str(k) for k in split_scores.keys()) if split_scores else sorted(str(k) for k in task_counts.keys()),
        },
        "scores": {
            "validation_current": m.get("validation_current_score"),
            "validation_candidate": m.get("validation_candidate_score"),
            "production_validation_current": m.get("production_validation_current_score"),
            "production_validation_candidate": m.get("production_validation_candidate_score"),
            "heldout_test": heldout_test,
        },
        "fingerprints": {
            "provenance": provenance.get("fingerprint_sha256"),
            "policy": policy.get("policy_fingerprint_sha256"),
            "target": provenance.get("target_fingerprint_sha256"),
            "task": provenance.get("task_sha256"),
        },
        "warnings": warnings,
    }


def _safe_skill_package_support(skill: Skill, home: Path) -> dict[str, Any]:
    """Summarize supporting package dirs without reading large content or escaping profile."""

    home_resolved = home.resolve()
    skill_dir = skill.path.parent.resolve()
    allowed_dirs = ("references", "templates", "scripts", "assets")
    warnings: list[str] = []
    dirs: dict[str, Any] = {}
    total_files = 0
    for dirname in allowed_dirs:
        root = skill_dir / dirname
        rel_root = f"{Path(skill.relpath).parent.as_posix()}/{dirname}"
        if not root.exists():
            dirs[dirname] = {"present": False, "file_count": 0, "files": []}
            continue
        root_resolved = root.resolve()
        if root.is_symlink() or not _is_relative_to(root_resolved, skill_dir) or not _is_relative_to(root_resolved, home_resolved):
            warnings.append(f"{rel_root} is symlinked or escapes the skill/profile boundary; skipped")
            dirs[dirname] = {"present": True, "unsafe": True, "file_count": 0, "files": []}
            continue
        files: list[dict[str, Any]] = []
        omitted = 0
        for p in sorted(root_resolved.rglob("*")):
            try:
                resolved = p.resolve()
            except Exception:
                omitted += 1
                continue
            if p.is_symlink() or not _is_relative_to(resolved, root_resolved) or not _is_relative_to(resolved, home_resolved):
                warnings.append(f"{p} is symlinked or escapes the skill/profile boundary; skipped")
                omitted += 1
                continue
            if not resolved.is_file():
                continue
            total_files += 1
            item = {"relpath": str(resolved.relative_to(home_resolved)), "bytes": resolved.stat().st_size, "sha256": sha256_file(resolved)}
            if len(files) < 20:
                files.append(item)
            else:
                omitted += 1
        dirs[dirname] = {"present": True, "file_count": len(files) + omitted, "files": files, "truncated": omitted > 0, "omitted_count": omitted}
    return {"schema_version": "hermes-skillopt-skill-package-support-v1", "advisory_only": True, "content_included": False, "skill_relpath": skill.relpath, "support_dirs": dirs, "total_file_count": total_files, "warnings": sorted(set(warnings))}


def _artifact_lineage_status(run_dir: Path, manifest: dict[str, Any] | None = None, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    m = manifest or {}
    cp_input = checkpoint.get("input", {}) if isinstance(checkpoint, dict) else {}
    history_path = run_dir / str((m.get("files") or {}).get("history") or "history.json")
    history: dict[str, Any] = {}
    if history_path.is_file():
        try:
            history = json.loads(read_text(history_path))
        except Exception:
            history = {}
    target_binding: dict[str, Any] = {}
    tb_path = run_dir / str((m.get("files") or {}).get("target_binding") or "target_binding.json")
    if tb_path.is_file():
        try:
            target_binding = json.loads(read_text(tb_path))
        except Exception:
            target_binding = {}
    provenance = m.get("provenance_fingerprint") if isinstance(m.get("provenance_fingerprint"), dict) else {}
    return {
        "schema_version": "skillopt-artifact-lineage-status-v1",
        "run_id": m.get("run_id") or run_dir.name,
        "parent_run_id": m.get("parent_run_id") or m.get("resume_run_id") or history.get("parent_run_id"),
        "resume_run_id": m.get("resume_run_id"),
        "skill_name": m.get("skill_name") or cp_input.get("skill_name"),
        "skill_relpath": m.get("skill_relpath") or cp_input.get("skill_relpath"),
        "skill_hashes": {
            "source_original_sha256": m.get("original_sha256") or cp_input.get("original_sha256") or history.get("parent_sha256"),
            "current_artifact_sha256": _maybe_sha_file(run_dir / "current_SKILL.md"),
            "proposed_sha256": m.get("proposed_sha256") or history.get("proposed_sha256") or _maybe_sha_file(run_dir / "proposed_SKILL.md"),
            "best_artifact_sha256": _maybe_sha_file(run_dir / "best_skill.md"),
        },
        "eval_pack": {
            "eval_file": m.get("eval_file") or cp_input.get("eval_file"),
            "eval_file_sha256": (provenance or {}).get("eval_file_sha256") or cp_input.get("eval_file_sha256"),
            "pack_id": m.get("eval_pack_id") or cp_input.get("eval_pack_id"),
            "version": m.get("eval_pack_version") or cp_input.get("eval_pack_version"),
            "fingerprint_sha256": m.get("eval_pack_fingerprint_sha256") or cp_input.get("eval_pack_fingerprint_sha256"),
        },
        "target_provenance": {
            "target_executor": m.get("target_executor") or (provenance or {}).get("target_executor") or cp_input.get("target_backend"),
            "target_config_id": m.get("target_config_id") or (provenance or {}).get("target_config_id") or cp_input.get("target_config_id"),
            "target_fingerprint_sha256": (provenance or {}).get("target_fingerprint_sha256"),
            "provenance_fingerprint_sha256": (provenance or {}).get("fingerprint_sha256"),
            "target_binding_sha256": _maybe_sha_file(tb_path),
            "target_binding_summary": {k: target_binding.get(k) for k in ("schema_version", "target_config_id", "skill_relpath", "skill_sha256") if k in target_binding},
        },
        "artifact_state": _slim_artifact_state(run_dir, m.get("files") if isinstance(m.get("files"), dict) else None),
        "history_path": history_path.name if history_path.is_file() else None,
        "history_sha256": _maybe_sha_file(history_path),
    }


def inspect_resume_run(run_id: str, *, hermes_home_path: str | None = None, expected_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only step-level resume inspection; never replays partial stages."""

    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    refusal_reasons: list[str] = []
    checkpoint: dict[str, Any] = {}
    try:
        checkpoint = json.loads(read_text(run_dir / "checkpoint.json"))
    except Exception as exc:
        refusal_reasons.append(f"checkpoint unreadable: {type(exc).__name__}: {exc}")
    expected_input_sha256 = _stable_json_sha(expected_input) if expected_input is not None else None
    if expected_input is not None and checkpoint:
        if checkpoint.get("input_sha256") != expected_input_sha256 or checkpoint.get("input") != expected_input:
            refusal_reasons.append("fingerprint mismatch: checkpoint input/config/provenance differs from requested resume")
    stages: list[dict[str, Any]] = []
    for path in sorted((run_dir / "stages").glob("*.json")):
        try:
            row = json.loads(read_text(path))
            has_fp = bool(row.get("input_sha256") and row.get("output_sha256"))
            stages.append({"file": path.name, "stage": row.get("stage"), "iteration": row.get("iteration"), "input_sha256": row.get("input_sha256"), "output_sha256": row.get("output_sha256"), "stage_file_sha256": sha256_file(path), "fingerprints_present": has_fp})
            if not has_fp:
                refusal_reasons.append(f"stage {path.name} missing input/output fingerprint")
        except Exception as exc:
            refusal_reasons.append(f"stage {path.name} unreadable: {type(exc).__name__}: {exc}")
    manifest: dict[str, Any] = {}
    manifest_hash_verified = False
    if (run_dir / "manifest.json").is_file():
        try:
            manifest = load_manifest(run_dir)
            verify_artifact_hashes(run_dir, manifest)
            manifest_hash_verified = True
        except Exception as exc:
            refusal_reasons.append(f"artifact hash verification failed: {type(exc).__name__}: {exc}")
    status = checkpoint.get("status")
    safe_reuse_completed = bool(status == "complete" and manifest_hash_verified and not refusal_reasons)
    if not safe_reuse_completed:
        if status != "complete":
            refusal_reasons.append("run is incomplete; partial-stage continuation is refused because replay could skip gates/adopt checks")
        if not manifest_hash_verified:
            refusal_reasons.append("completed-run reuse unavailable until manifest artifact hashes verify")
    cleanup_guidance = []
    if not safe_reuse_completed:
        cleanup_guidance.append("No automatic cleanup is performed. If this run is abandoned, manually inspect run_dir, ensure no optimizer process is still writing it, then remove the directory or keep it for audit.")
        if not (run_dir / "manifest.json").is_file():
            cleanup_guidance.append("No manifest/report is present; treat staged artifacts as incomplete and non-adoptable.")
    return {"success": True, "run_id": run_id, "run_dir": str(run_dir), "checkpoint_status": status, "checkpoint_input_sha256": checkpoint.get("input_sha256"), "expected_input_sha256": expected_input_sha256, "completed_stages": checkpoint.get("completed_stages", []), "stages": stages, "stage_count": len(stages), "manifest_present": (run_dir / "manifest.json").is_file(), "report_present": (run_dir / "report.md").is_file(), "manifest_hash_verified": manifest_hash_verified, "artifact_state": _slim_artifact_state(run_dir, manifest.get("files") if isinstance(manifest.get("files"), dict) else None), "artifact_lineage": _artifact_lineage_status(run_dir, manifest, checkpoint), "safe_reuse_completed": safe_reuse_completed, "partial_continuation_available": False, "refusal_reasons": sorted(set(refusal_reasons)), "cleanup_guidance": cleanup_guidance}


def _slow_meta_artifact(original: str, evidence: dict[str, Any], rejected: list[dict[str, Any]], candidate_summary: list[dict[str, Any]]) -> dict[str, Any]:
    """Evidence-only slow update foundation. Never writes live skill content."""

    fm, body = frontmatter_split(original)
    protected = {
        "frontmatter_sha256": sha256_text(fm),
        "protected_headings": ["system", "developer", "safety", "profile isolation"],
        "protected_markers": ["<!-- skillopt:protected:start -->", "<!-- skillopt:protected:end -->"],
        "allowed_markers": ["<!-- skillopt:allowed:start -->", "<!-- skillopt:allowed:end -->"],
        "policy": "slow/meta suggestions are evidence-only candidate context unless passed through bounded edits and normal validation/adopt gates",
    }
    suggestions = []
    for row in candidate_summary[-3:]:
        suggestions.append({"source": "candidate_summary", "iteration": row.get("iteration"), "selected_candidate_id": row.get("selected_candidate_id"), "rationale": row.get("selected_candidate_rationale")})
    stability_epochs = []
    accepted_total = 0
    for row in candidate_summary:
        ranked = row.get("ranked_candidates", []) if isinstance(row, dict) else []
        accepted = sum(1 for cand in ranked if isinstance(cand, dict) and cand.get("accepted"))
        rejected_in_epoch = sum(1 for cand in ranked if isinstance(cand, dict) and not cand.get("accepted"))
        accepted_total += accepted
        stability_epochs.append({
            "iteration": row.get("iteration") if isinstance(row, dict) else None,
            "selected_candidate_id": row.get("selected_candidate_id") if isinstance(row, dict) else None,
            "accepted_candidates": accepted,
            "rejected_candidates": rejected_in_epoch,
            "selected_rationale": row.get("selected_candidate_rationale") if isinstance(row, dict) else None,
            "stable_signal": "accepted_candidate_available" if accepted else "no_accepted_candidate_rejected_or_noop",
        })
    rejected_memory = []
    for row in rejected[-20:]:
        if not isinstance(row, dict):
            continue
        raw_gate = row.get("gate")
        gate: dict[str, Any] = raw_gate if isinstance(raw_gate, dict) else {}
        rejected_memory.append({
            "iteration": row.get("iteration"),
            "candidate_id": row.get("candidate_id"),
            "reasoning": str(row.get("reasoning") or "")[:500],
            "rejection_reasons": list(gate.get("rejection_reasons") or [])[:8],
            "validation_errors": list(row.get("validation_errors") or [])[:8],
            "rejected_edit_reasons": [str(e.get("reason")) for e in (row.get("rejected_edits") or []) if isinstance(e, dict) and e.get("reason")][:8],
        })
    return {
        "schema_version": "skillopt-slow-meta-v1",
        "mode": "evidence_only_no_live_write",
        "optimizer_memory_mode": "optimizer_only_evidence_no_live_write",
        "artifact_role": "optimizer_memory_only_not_deployable_skill",
        "write_policy": "MUST NOT be copied into live SKILL.md except as a bounded candidate that passes normal validation/adopt gates",
        "budget": {"max_rejected_memory_items": 20, "max_candidate_suggestions": 3, "max_reasoning_chars_per_item": 500},
        "protected_regions": protected,
        "skill_body_sha256": sha256_text(body),
        "evidence_summary": {"curated_task_count": evidence.get("curated_task_count", 0), "production_gate_eligible": evidence.get("production_gate_eligible"), "snippets": len(evidence.get("snippets", []))},
        "rejected_buffer_count": len(rejected),
        "optimizer_rejected_memory": rejected_memory,
        "candidate_context_suggestions": suggestions,
        "deployed_skill_boundary": {
            "live_skill_content_included": False,
            "source_skill_sha256": sha256_text(original),
            "optimizer_memory_must_not_be_deployed_as_skill": True,
            "deployed_skill_artifacts": ["proposed_SKILL.md", "best_skill.md when present"],
        },
        "epoch_stability_signals": {
            "iterations_observed": len(candidate_summary),
            "accepted_candidate_count": accepted_total,
            "rejected_candidate_count": len(rejected),
            "epochs": stability_epochs,
            "rejection_buffer_captured": bool(rejected_memory),
        },
        "normal_gate_required_for_any_write": True,
    }


def _history_artifact(*, run_id: str, original_sha256: str, proposed_sha256: str, candidate_summary: list[dict[str, Any]], gates: list[dict[str, Any]], production_gates: list[dict[str, Any]], rejected: list[dict[str, Any]], stage_records: list[Any], parent_run_id: str | None = None) -> dict[str, Any]:
    gate_by_candidate = {g.get("candidate_id"): g for g in gates if isinstance(g, dict) and g.get("candidate_id")}
    prod_by_candidate = {g.get("candidate_id"): g for g in production_gates if isinstance(g, dict) and g.get("candidate_id")}
    rejected_ids = {r.get("candidate_id") for r in rejected if isinstance(r, dict)}
    candidates: list[dict[str, Any]] = []
    for round_row in candidate_summary:
        ranked = round_row.get("ranked_candidates", []) if isinstance(round_row, dict) else []
        for cand in ranked:
            if not isinstance(cand, dict):
                continue
            cid = cand.get("candidate_id")
            gate = gate_by_candidate.get(cid, {})
            pgate = prod_by_candidate.get(cid) or cand.get("production_gate") or {}
            accepted = bool(cand.get("accepted"))
            candidates.append({
                "candidate_id": cid,
                "iteration": cand.get("iteration"),
                "rank": cand.get("rank"),
                "selected": bool(cand.get("selected")),
                "accepted": accepted,
                "lineage_status": "accepted" if accepted else "rejected",
                "parent_sha256": original_sha256,
                "selected_candidate_id": round_row.get("selected_candidate_id"),
                "accept_reject_reasons": cand.get("rejection_reasons") or gate.get("rejection_reasons") or ([gate.get("rationale")] if gate.get("rationale") else []),
                "gate": {"accepted": gate.get("accepted", cand.get("accepted")), "current_score": cand.get("current_score", gate.get("current_score")), "candidate_score": cand.get("candidate_score", gate.get("candidate_score")), "rationale": gate.get("rationale")},
                "production_gate": {"accepted": pgate.get("accepted"), "current_score": pgate.get("current_score"), "candidate_score": pgate.get("candidate_score"), "rationale": pgate.get("rationale")},
                "rejected_buffered": cid in rejected_ids,
            })
    timeline = [{"type": "stage", "iteration": getattr(r, "iteration", None), "stage": getattr(r, "stage", None), "evidence": getattr(r, "evidence", {})} for r in stage_records]
    timeline.extend({"type": "candidate", "iteration": c.get("iteration"), "candidate_id": c.get("candidate_id"), "status": c.get("lineage_status"), "rank": c.get("rank")} for c in candidates)
    return {"schema_version": "skillopt-history-v1", "run_id": run_id, "parent_run_id": parent_run_id, "parent_sha256": original_sha256, "proposed_sha256": proposed_sha256, "timeline": timeline, "candidates": candidates, "accepted_candidate_ids": [c["candidate_id"] for c in candidates if c.get("accepted")], "rejected_candidate_ids": [c["candidate_id"] for c in candidates if not c.get("accepted")], "explainability": "candidate rows preserve rank, selected flag, gates, production gates, rejection reasons, and parent hash"}


def eval_only(skill: str | None = None, *, skill_file: str | None = None, eval_file: str | None = None, hermes_home_path: str | None = None, target_executor: str = "auto", target_backend: str | None = None) -> dict[str, Any]:
    """Read-only evaluation of a fixed skill against an explicit eval pack."""

    if not eval_file:
        raise ValueError("eval_only requires an explicit --eval-file")
    if skill and skill_file:
        raise ValueError("pass either --skill or --skill-file, not both")
    from hermes_skillopt.env import JsonEvalPackBenchmarkAdapter, load_eval_pack, resolve_eval_file
    from hermes_skillopt.state import SkillState
    from hermes_skillopt.target import DeterministicKeywordScorecard, HermesRolloutRunner, HermesSandboxRunner, LiveHermesReadOnlyRunner, TargetExecutor

    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    if skill_file:
        raw_skill = Path(skill_file).expanduser()
        candidate_skill_path = raw_skill if raw_skill.is_absolute() else home / raw_skill
        resolved_skill_path = candidate_skill_path.resolve(strict=True)
        if candidate_skill_path.is_symlink() or not resolved_skill_path.is_file() or not _is_relative_to(resolved_skill_path, home.resolve()):
            raise ValueError("skill_file must resolve to a regular file under HERMES_HOME")
        skill_text = read_text(resolved_skill_path)
        skill_name = resolved_skill_path.parent.name
        skill_relpath = str(resolved_skill_path.relative_to(home.resolve()))
        skill_path = resolved_skill_path
    else:
        target = find_skill(home, skill)
        skill_text = read_text(target.path)
        skill_name = target.name
        skill_relpath = target.relpath
        skill_path = target.path

    state = SkillState(name=skill_name, path=skill_path, relpath=skill_relpath, text=skill_text, sha256=sha256_text(skill_text), hermes_home=home)
    eval_path = resolve_eval_file(home, state, eval_file)
    if eval_path is None:
        raise FileNotFoundError(f"eval_file not found: {eval_file}")
    tasks_all, eval_pack = load_eval_pack(eval_path)
    benchmark_adapter = JsonEvalPackBenchmarkAdapter(eval_path)
    _adapter_tasks, _adapter_meta, governance = benchmark_adapter.load()
    tasks: dict[str, list[Any]] = {"train": [], "val": [], "test": []}
    alias = {"validation": "val", "valid": "val", "dev": "val"}
    for task in tasks_all:
        split = alias.get(task.split, task.split)
        tasks.setdefault(split, []).append(task)

    requested_target = target_backend or target_executor
    if requested_target in {"sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1"}:
        runner = HermesSandboxRunner()
    elif requested_target == "scorecard":
        runner = DeterministicKeywordScorecard()
    elif requested_target == "live-readonly":
        runner = LiveHermesReadOnlyRunner(profile_home=str(home))
    else:
        runner = HermesRolloutRunner()
    executor = TargetExecutor(runner=runner, requested_executor=requested_target)
    split_results = {name: executor.evaluate(skill_text, split_tasks, label=f"eval_only_{name}") for name, split_tasks in tasks.items() if split_tasks}
    all_result = executor.evaluate(skill_text, tasks_all, label="eval_only_all")

    rid = now_id() + "-eval-only-" + skill_name.replace("/", "-")
    run_dir = dirs["staging"] / rid
    run_dir.mkdir(parents=True, exist_ok=False)
    report_payload = {
        "schema_version": "skillopt-eval-only-v1",
        "run_id": rid,
        "mode": "eval_only_no_training_no_adoption",
        "skill_name": skill_name,
        "skill_path": str(skill_path),
        "skill_relpath": skill_relpath,
        "skill_sha256": sha256_text(skill_text),
        "eval_file": str(eval_path),
        "eval_file_sha256": sha256_file(eval_path),
        "eval_pack": eval_pack.as_dict(),
        "eval_pack_governance": governance,
        "benchmark_adapter": {"loader": benchmark_adapter.rollout_metadata(), "scorer": benchmark_adapter.scorer_metadata()},
        "target_executor": executor.mode,
        "target_backend_config": executor.config.as_dict(),
        "task_counts": {name: len(split_tasks) for name, split_tasks in tasks.items()},
        "split_results": split_results,
        "all_results": all_result,
        "side_effect_policy": "read-only evaluation; no optimizer/training/adoption artifacts are produced",
    }
    benchmark_report = {
        "schema_version": "hermes-native-benchmark-report-v1",
        "run_id": rid,
        "mode": "read_only_benchmark",
        "reproducibility": {
            "skill_sha256": report_payload["skill_sha256"],
            "eval_file_sha256": report_payload["eval_file_sha256"],
            "eval_pack_fingerprint_sha256": eval_pack.fingerprint_sha256,
            "target_fingerprint_sha256": report_payload["target_backend_config"]["fingerprint_sha256"],
            "task_counts": report_payload["task_counts"],
        },
        "safety": {
            "read_only": True,
            "optimizer_training": False,
            "adoption_side_effects": False,
            "task_provided_commands_allowed": False,
            "target_executor": executor.mode,
        },
        "scorecard": {
            "overall_score": all_result.get("score"),
            "overall_hard_pass_rate": all_result.get("hard_pass_rate"),
            "split_scores": {name: {"score": result.get("score"), "hard_pass_rate": result.get("hard_pass_rate"), "production_gate_eligible": result.get("production_gate_eligible")} for name, result in split_results.items()},
            "regression_cases": all_result.get("regression_cases") or [],
        },
        "eval_pack": eval_pack.as_dict(),
        "eval_pack_governance": governance,
        "benchmark_adapter": {"loader": benchmark_adapter.rollout_metadata(), "scorer": benchmark_adapter.scorer_metadata()},
        "target_backend_config": report_payload["target_backend_config"],
    }
    write_text(run_dir / "evaluated_SKILL.md", skill_text)
    write_text(run_dir / "eval_report.json", json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_text(run_dir / "benchmark_report.json", json.dumps(benchmark_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_text(run_dir / "report.md", f"# Hermes SkillOpt eval-only\n\n- run_id: {rid}\n- mode: eval_only_no_training_no_adoption\n- skill: {skill_name}\n- eval_file: {eval_path}\n- eval_pack_id: {eval_pack.pack_id}\n- benchmark_report: benchmark_report.json\n- target_executor: {executor.mode}\n- score: {all_result.get('score')}\n- task_counts: {json.dumps(report_payload['task_counts'], ensure_ascii=False)}\n- no_training_or_adoption_side_effects: true\n")
    files = {"evaluated_skill": "evaluated_SKILL.md", "eval_report": "eval_report.json", "benchmark_report": "benchmark_report.json", "report": "report.md"}
    manifest = {
        "run_id": rid,
        "status": "eval_only_complete",
        "adoptable": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hermes_home": str(home),
        "skill_name": skill_name,
        "skill_path": str(skill_path),
        "skill_relpath": skill_relpath,
        "skill_sha256": sha256_text(skill_text),
        "eval_file": str(eval_path),
        "eval_file_sha256": sha256_file(eval_path),
        "eval_pack": eval_pack.as_dict(),
        "eval_pack_governance": governance,
        "benchmark_adapter": {"loader": benchmark_adapter.rollout_metadata(), "scorer": benchmark_adapter.scorer_metadata()},
        "target_executor": executor.mode,
        "target_backend_config": executor.config.as_dict(),
        "task_counts": report_payload["task_counts"],
        "score": all_result.get("score"),
        "mode": "eval_only_no_training_no_adoption",
        "files": files,
    }
    manifest["artifact_sha256"] = artifact_hashes(run_dir, files)
    save_manifest(run_dir, manifest)
    return {"success": True, "run_id": rid, "status": "eval_only_complete", "adoptable": False, "run_dir": str(run_dir), "report_path": str(run_dir / "report.md"), "eval_report_path": str(run_dir / "eval_report.json"), "benchmark_report_path": str(run_dir / "benchmark_report.json"), "score": all_result.get("score"), "task_counts": report_payload["task_counts"], "eval_file": str(eval_path), "target_executor": executor.mode}


def full_run(skill: str | None = None, query: str | None = None, lookback_days: int = 14, limit: int = 50, iterations: int = 1, edit_budget: int = 3, candidate_count: int = 1, backend: str = "auto", optimizer_backend: str | None = None, allow_mock: bool = False, auto_adopt: bool = False, force: bool = False, hermes_home_path: str | None = None, ctx: Any = None, dry_run: bool = False, eval_file: str | None = None, target_executor: str = "auto", target_backend: str | None = None, gate_mode: str = "strict", resume_run_id: str | None = None) -> dict[str, Any]:
    """Run the Hermes adapter of the SkillOpt core abstraction.

    Pipeline: load trainable SkillState -> build benchmark tasks -> evaluate
    current with a frozen TargetExecutor -> optimizer reflects/proposes bounded
    edits -> evaluate candidate on held-out validation -> ValidationGate accepts
    only strict score improvements -> stage artifacts for user review/adopt.
    """
    if force and auto_adopt:
        raise ValueError("auto_adopt cannot be combined with force")
    if dry_run:
        raise ValueError("full_run(dry_run=True) is no longer supported; use dry_run/legacy mode for review-only proposals")
    if auto_adopt:
        raise ValueError("auto_adopt is disabled in production; run full-run, review artifacts, then call adopt explicitly")
    from hermes_skillopt.env import HermesEnvAdapter, HermesSkillEnv, is_production_gate_task
    from hermes_skillopt.gate import GateMetricPolicy, ValidationGate
    from hermes_skillopt.optimizer import OptimizerBackend, OptimizerBackendConfig
    from hermes_skillopt.state import SkillOptArtifacts, SkillState
    from hermes_skillopt.target import DeterministicKeywordScorecard, HermesRolloutRunner, HermesSandboxRunner, LiveHermesReadOnlyRunner, TargetExecutor
    from hermes_skillopt.trainer import SixStageSkillOptTrainer

    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    target = find_skill(home, skill)
    original = read_text(target.path)
    optimizer_backend = optimizer_backend or backend
    target_backend = target_backend or target_executor
    resume_input = _resume_input_payload(home=home, target=target, original=original, query=query, lookback_days=lookback_days, limit=limit, iterations=iterations, edit_budget=edit_budget, candidate_count=candidate_count, optimizer_backend=optimizer_backend, target_backend=target_backend, gate_mode=gate_mode, eval_file=eval_file, allow_mock=allow_mock)
    native_metadata = resume_input.get("native_hermes_metadata") if isinstance(resume_input.get("native_hermes_metadata"), dict) else native_skill_metadata_snapshot(home, target)
    if resume_run_id:
        resume_dir = resolve_run_dir(home, resume_run_id)
        try:
            resumed = _resume_completed_run(resume_dir, resume_input)
        except ValueError:
            raise
        if resumed is not None:
            resumed["resume_inspection"] = inspect_resume_run(resume_run_id, hermes_home_path=str(home), expected_input=resume_input)
            return resumed
        inspection = inspect_resume_run(resume_run_id, hermes_home_path=str(home), expected_input=resume_input)
        raise ValueError("Resume checkpoint validated but run is incomplete; safe partial-stage replay is not available for this checkpoint; refusal_reasons=" + json.dumps(inspection.get("refusal_reasons", []), ensure_ascii=False))
    state = SkillState(name=target.name, path=target.path, relpath=target.relpath, text=original, sha256=sha256_text(original), hermes_home=home)
    rid = now_id() + "-" + target.name.replace("/", "-")
    run_dir = dirs["staging"] / rid
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_checkpoint(run_dir, resume_input, status="running", completed_stages=[])
    artifacts = SkillOptArtifacts.for_run(rid, run_dir)
    llm = LLMBackend(backend=optimizer_backend, allow_mock=allow_mock, ctx=ctx)
    env = HermesSkillEnv(state, query=query, lookback_days=lookback_days, limit=limit, eval_file=eval_file)
    env_adapter = HermesEnvAdapter(env)
    tasks, evidence = env_adapter.load_tasks()
    evidence["env_adapter_rollout_metadata"] = env_adapter.rollout_metadata()
    evidence["env_adapter_scorer_metadata"] = env_adapter.scorer_metadata()
    production_val_tasks = [t for t in tasks["val"] if is_production_gate_task(t)]
    production_gate_available = bool(production_val_tasks) and bool(evidence.get("production_gate_eligible"))
    evidence["production_gate_task_count"] = len(production_val_tasks)
    evidence["production_gate_eligible"] = production_gate_available
    if target_backend in {"sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1"}:
        runner = HermesSandboxRunner()
    elif target_backend == "scorecard":
        runner = DeterministicKeywordScorecard()
    elif target_backend == "live-readonly":
        runner = LiveHermesReadOnlyRunner(profile_home=str(home))
    else:
        runner = HermesSandboxRunner() if any((t.metadata.get("executor") == "sandbox" or t.judge == "hermes_sandbox") for split_tasks in tasks.values() for t in split_tasks) else HermesRolloutRunner()
    executor = TargetExecutor(runner=runner, requested_executor=target_backend)
    optimizer_config_obj = OptimizerBackendConfig(backend=llm.mode, requested_backend=optimizer_backend, allow_mock=bool(allow_mock), edit_budget=edit_budget, candidate_count=candidate_count, iterations=iterations)
    optimizer = OptimizerBackend(llm, edit_budget=edit_budget, config=optimizer_config_obj)
    gate_policy_obj = GateMetricPolicy(mode=gate_mode)
    gatekeeper = ValidationGate(gate_policy_obj)
    trainer = SixStageSkillOptTrainer(executor, optimizer, gatekeeper, llm, artifacts, run_dir)

    rejected_history = load_rejected_edit_history(home, target.name)
    current = original

    write_text(artifacts.original, original)
    write_text(artifacts.current, current)
    write_text(artifacts.evidence, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(artifacts.train, [t.__dict__ for t in tasks["train"]])
    _jsonl_write(artifacts.val, [t.__dict__ for t in tasks["val"]])
    _jsonl_write(artifacts.test, [t.__dict__ for t in tasks["test"]])

    trainer_result = trainer.run(original, tasks, iterations, production_val_tasks=production_val_tasks if production_gate_available else [], rejected_history=rejected_history, candidate_count=candidate_count)
    current = trainer_result.current
    best = trainer_result.best
    best_gate = trainer_result.best_gate
    all_reflections = trainer_result.all_reflections
    all_edits = trainer_result.all_edits
    all_gates = trainer_result.all_gates
    all_production_gates = trainer_result.all_production_gates
    candidate_summary = trainer_result.candidate_summary
    rejected = trainer_result.rejected

    final_status = "staged_best" if best != original else "rejected"
    eval_file_used = evidence.get("eval_file")
    validation_summary = best_gate or (all_gates[-1] if all_gates else None)
    production_validation_summary = trainer_result.production_best_gate
    test_results = trainer_result.test_results
    current_test_results = executor.evaluate(original, tasks["test"], label="original_test_sensitivity") if tasks["test"] else {"results": [], "score": 0.0}
    heldout_test_sensitivity = _heldout_test_sensitivity(current_test_results, test_results)
    production_test_results = [r for r in (test_results.get("results") or []) if isinstance(r, dict) and isinstance(r.get("metadata"), dict) and r["metadata"].get("production_gate_eligible")]
    test_gate_eligible = bool(production_test_results) and all(float(r.get("score", 0.0)) >= 0.55 and bool(r.get("passed")) for r in production_test_results)
    production_gate_eligible = production_gate_available and bool(
        production_validation_summary.get("accepted") if production_validation_summary else False
    )
    strict_gate_mode = gate_policy_obj.normalized_mode() == "strict"
    adoptable = final_status == "staged_best" and production_gate_eligible and test_gate_eligible and strict_gate_mode
    adoptability_reasons = []
    if final_status != "staged_best":
        adoptability_reasons.append("no staged_best candidate")
    if not production_gate_eligible:
        adoptability_reasons.append("missing accepted explicit curated production validation gate")
    production_hard_failures = []
    if isinstance(production_validation_summary, dict):
        metric_summary = production_validation_summary.get("metric_summary")
        if isinstance(metric_summary, dict):
            production_hard_failures = [f for f in (metric_summary.get("production_candidate_hard_failures") or []) if isinstance(f, dict)]
    for failure in production_hard_failures:
        task_id = failure.get("task_id") or "unknown"
        reason = f"production-eligible validation task hard-failed: {task_id}"
        if reason not in adoptability_reasons:
            adoptability_reasons.append(reason)
    if not test_gate_eligible:
        adoptability_reasons.append("held-out test split is missing, non-production, or below threshold")
    if not strict_gate_mode:
        adoptability_reasons.append("production adoption requires strict gate mode; requested gate mode is review-only")
    proposed = best if final_status == "staged_best" else original
    diff = make_diff(original, proposed, target.relpath)
    if final_status == "staged_best":
        write_text(artifacts.best, best)
    write_text(artifacts.proposed, proposed)
    write_text(artifacts.diff, diff)
    write_text(artifacts.reflections, json.dumps(all_reflections, ensure_ascii=False, indent=2) + "\n")
    write_text(artifacts.candidate_edits, json.dumps(all_edits, ensure_ascii=False, indent=2) + "\n")
    write_text(artifacts.gate_results, json.dumps({"gates": all_gates, "best_gate": best_gate, "production_gates": all_production_gates, "production_best_gate": production_validation_summary}, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(artifacts.rejected_edits, rejected)
    slow_meta = _slow_meta_artifact(original, evidence, rejected, candidate_summary)
    write_text(artifacts.slow_meta, json.dumps(slow_meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    target_config_for_binding = executor.config.as_dict()
    target_execution_evidence = _target_execution_evidence_summary(
        target_config=target_config_for_binding,
        validation_summary=validation_summary,
        production_validation_summary=production_validation_summary,
        test_results=test_results,
        target_backend=executor.mode,
    )
    write_text(artifacts.target_execution_evidence, json.dumps(target_execution_evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if target_execution_evidence.get("classification") == "frozen_hermes_target_execution_v1" and not target_execution_evidence.get("complete"):
        adoptable = False
        reason = "missing complete frozen target execution evidence artifact"
        if reason not in adoptability_reasons:
            adoptability_reasons.append(reason)
    target_binding = _target_binding_payload(home, target, original, target_config=target_config_for_binding, target_execution_evidence=target_execution_evidence)
    write_text(artifacts.target_binding, json.dumps(target_binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    current_score = validation_summary.get("current_score") if validation_summary else None
    candidate_score = validation_summary.get("candidate_score") if validation_summary else None
    production_current_score = production_validation_summary.get("current_score") if production_validation_summary else None
    production_candidate_score = production_validation_summary.get("candidate_score") if production_validation_summary else None
    gate_reason = validation_summary.get("rationale") if validation_summary else "none"
    per_task_delta = _per_task_delta((best_gate or {}).get("current_eval") if isinstance(best_gate, dict) else None, (best_gate or {}).get("candidate_eval") if isinstance(best_gate, dict) else None)
    split_scores = {
        "validation": {"current": current_score, "candidate": candidate_score},
        "production_validation": {"current": production_current_score, "candidate": production_candidate_score},
        "heldout_test": {"best": test_results.get("split_score", test_results.get("score")), "current": current_test_results.get("split_score", current_test_results.get("score")), "candidate_vs_current_sensitive": heldout_test_sensitivity.get("candidate_vs_current_sensitive")},
    }
    candidate_comparison = candidate_summary[-1]["ranked_candidates"] if candidate_summary else []
    regression_cases = sorted(set((test_results.get("regression_cases") or []) + [d.get("task_id") for d in per_task_delta if isinstance(d, dict) and d.get("regressed") and d.get("task_id")]))
    reviewer_gate = _reviewer_gate_artifact(
        candidate_summary=candidate_summary,
        validation_summary=validation_summary,
        production_validation_summary=production_validation_summary,
        test_results=test_results,
        regression_cases=regression_cases,
        adoptability_reasons=adoptability_reasons,
        final_status=final_status,
        deterministic_adoptable_before_review=adoptable,
    )
    if not reviewer_gate.get("adoptable_after_reviewer_gate"):
        adoptable = False
        reason = "deterministic reviewer gate did not pass"
        if reason not in adoptability_reasons:
            adoptability_reasons.append(reason)
    write_text(artifacts.reviewer_gate, json.dumps(reviewer_gate, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    evidence_ledger = _eval_evidence_ledger(target_execution_evidence=target_execution_evidence, reviewer_gate=reviewer_gate)
    if adoptable and evidence_ledger.get("production_runtime_ready") is not True:
        adoptable = False
        for reason in evidence_ledger.get("blockers") or ["evidence ledger is not production-runtime ready"]:
            if reason not in adoptability_reasons:
                adoptability_reasons.append(str(reason))
    production_eval_policy = _production_eval_policy(evidence, production_gate_available, test_gate_eligible)
    optimizer_config = optimizer.config.as_dict()
    optimizer_prompt_fingerprints = list(getattr(optimizer, "prompt_fingerprints", []) or [])
    optimizer_config["prompt_fingerprints"] = optimizer_prompt_fingerprints
    optimizer_config["prompt_fingerprint_sha256"] = _stable_json_sha(optimizer_prompt_fingerprints)
    optimizer_config["algorithm_version"] = ALGORITHM_VERSION
    optimizer_config.setdefault("sampling", {"random_seed": None, "temperature": None, "deterministic": llm.mode == "mock"})
    target_config = executor.config.as_dict()
    gate_policy = gate_policy_obj.as_dict()
    benchmark_parity = benchmark_parity_status(str(home))
    provenance_binding = _provenance_binding_payload(
        backend=llm.mode,
        optimizer_config=optimizer_config,
        target_backend=executor.mode,
        target_config=target_config,
        target_executor=executor.mode,
        target_config_id=executor.target_config_id,
        gate_policy=gate_policy,
    )
    write_text(artifacts.provenance_binding, json.dumps(provenance_binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    mock_reasons = _mock_provenance_reasons({"backend": llm.mode, "optimizer_backend": optimizer_config.get("backend"), "optimizer_backend_config": optimizer_config})
    if mock_reasons:
        adoptable = False
        for reason in mock_reasons:
            if reason not in adoptability_reasons:
                adoptability_reasons.append(reason)
    provenance = _provenance_fingerprint(
        eval_file_used=eval_file_used,
        tasks=tasks,
        backend_mode=llm.mode,
        target_executor_mode=executor.mode,
        target_config_id=executor.target_config_id,
        production_gate_available=production_gate_available,
        home=home,
        skill_relpath=target.relpath,
        original_sha256=sha256_text(original),
        proposed_sha256=sha256_text(proposed),
        optimizer_config=optimizer_config,
        target_config=target_config,
        gate_policy=gate_policy,
        production_eval_policy=production_eval_policy,
        eval_pack=evidence.get("eval_pack") if isinstance(evidence.get("eval_pack"), dict) else None,
        optimizer_prompt_fingerprints=optimizer_prompt_fingerprints,
        algorithm_version=ALGORITHM_VERSION,
    )
    write_text(artifacts.candidate_summary, json.dumps({"candidate_count": max(1, int(candidate_count)), "rounds": candidate_summary, "split_scores": split_scores, "per_task_delta": per_task_delta, "candidate_comparison": candidate_comparison, "regression_cases": regression_cases, "heldout_test_sensitivity": heldout_test_sensitivity, "production_eligibility_reasons": adoptability_reasons, "selection_policy": "rank candidates on same validation set; when production gates exist, prefer candidates with both generic and production strict improvement"}, ensure_ascii=False, indent=2) + "\n")
    history = _history_artifact(run_id=rid, original_sha256=sha256_text(original), proposed_sha256=sha256_text(proposed), candidate_summary=candidate_summary, gates=all_gates, production_gates=all_production_gates, rejected=rejected, stage_records=trainer_result.stage_records)
    write_text(artifacts.history, json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    report = f"# Hermes SkillOpt full run\n\n- abstraction: SkillOpt-inspired Hermes adapter (trainable skill state, frozen scorecard/replay target, optimizer bounded edit, benchmark env, validation gate)\n- run_id: {rid}\n- status: {final_status}\n- adoptable: {adoptable}\n- skill: {target.name}\n- backend: {llm.mode}\n- optimizer_backend: {optimizer_config['backend']}\n- optimizer_requested_backend: {optimizer_config['requested_backend']}\n- target_executor: {executor.mode}\n- target_backend_requested: {target_config['requested_executor']}\n- target_config_id: {executor.target_config_id}\n- gate_policy: {gate_policy['mode']}\n- eval_file: {eval_file_used or 'none'}\n- eval_pack_id: {evidence.get('eval_pack_id') or 'none'}\n- eval_pack_version: {evidence.get('eval_pack_version') or 'none'}\n- eval_pack_fingerprint: {evidence.get('eval_pack_fingerprint_sha256') or 'none'}\n- split_governance: validation_selects_candidates_test_is_heldout\n- benchmark_parity_label: {benchmark_parity['parity_label']}\n- benchmark_parity_mode: {benchmark_parity['mode']}\n- provenance_fingerprint: {provenance['fingerprint_sha256']}\n- eval_fingerprint: {provenance.get('eval_file_sha256') or 'none'}\n- task_fingerprint: {provenance['task_sha256']}\n- production_eval_policy: {production_eval_policy['policy_version']}\n- production_eval_policy_fingerprint: {production_eval_policy['policy_fingerprint_sha256']}\n- optimizer_fingerprint: {provenance['optimizer_fingerprint_sha256']}\n- optimizer_prompt_fingerprint: {provenance['optimizer_prompt_fingerprint_sha256']}\n- algorithm_version: {provenance['algorithm_version']}\n- target_fingerprint: {provenance['target_fingerprint_sha256']}\n- profile_fingerprint: {provenance['profile_fingerprint_sha256']}\n- skill_fingerprint: {provenance['skill_fingerprint_sha256']}\n- curated_task_count: {evidence.get('curated_task_count', 0)}\n- production_gate_eligible: {production_gate_eligible}\n- production_gate_task_count: {len(production_val_tasks)}\n- harvested_fragments: {len(evidence.get('snippets', []))}\n- train/val/test: {len(tasks['train'])}/{len(tasks['val'])}/{len(tasks['test'])}\n- baseline/current/candidate/best/test: original_sha={sha256_text(original)[:12]}, current_sha={sha256_text(current)[:12]}, candidate_sha={sha256_text(proposed)[:12]}, best_sha={sha256_text(best)[:12]}, test_score={test_results.get('score')}\n- iterations: {max(1, int(iterations))}\n- candidate_count_per_iteration: {max(1, int(candidate_count))}\n- six_stage_trainer_artifacts: stages/NNN_rollout|reflect|aggregate|select|update|evaluate.json\n- rejected_history_count: {len(rejected_history)}\n- validation_scores: current={current_score}, candidate={candidate_score}\n- production_validation_scores: current={production_current_score}, candidate={production_candidate_score}\n- heldout_test_score: {test_results.get('score')}\n- split_scores: {json.dumps(split_scores, ensure_ascii=False)}\n- regression_cases: {json.dumps(regression_cases, ensure_ascii=False)}\n- production_eligibility_reasons: {', '.join(adoptability_reasons) or 'eligible'}\n- test_gate_eligible: {test_gate_eligible}\n- not_adoptable_reasons: {', '.join(adoptability_reasons) or 'none'}\n- not_adoptable_checklist: production_validation={production_gate_eligible}; heldout_test={test_gate_eligible}; staged_best={final_status == 'staged_best'}\n- gate_reason: {gate_reason}\n- acceptance_gate: candidate production curated validation score must strictly improve for adoptable; session/fallback/synthetic validation is review-only evidence\n- best_gate: {json.dumps(best_gate, ensure_ascii=False) if best_gate else 'none'}\n- production_best_gate: {json.dumps(production_validation_summary, ensure_ascii=False) if production_validation_summary else 'none'}\n- score_ledger: production_curated={production_candidate_score}; review_only_validation={candidate_score}; heldout_test={test_results.get('score')}\n- heldout_test_sensitivity: {json.dumps(heldout_test_sensitivity, ensure_ascii=False)}\n- per_task_delta: {json.dumps(per_task_delta, ensure_ascii=False)}\n- changed: {bool(diff)}\n\n## Multi-candidate rank/select\n\n```json\n{json.dumps(candidate_summary, ensure_ascii=False, indent=2)[:4000]}\n```\n\n## Diff preview\n\n```diff\n{diff[:4000]}\n```\n"
    write_text(artifacts.report, report)
    manifest = {
        "run_id": rid,
        "status": final_status,
        "adoptable": adoptable,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_path": str(target.path),
        "skill_relpath": target.relpath,
        "original_sha256": sha256_text(original),
        "native_hermes_metadata": native_metadata,
        "native_hermes_adopt_guard_at_stage": native_adopt_guard(home, target, {"native_hermes_metadata": native_metadata}),
        "proposed_sha256": sha256_text(proposed),
        "engine": "hermes-native-skillopt-core-adapter",
        "algorithm_version": ALGORITHM_VERSION,
        "backend": llm.mode,
        "optimizer_backend": optimizer_config["backend"],
        "optimizer_backend_config": optimizer_config,
        "optimizer_config": optimizer_config,
        "target_backend": executor.mode,
        "target_backend_config": target_config,
        "target_executor": executor.mode,
        "target_config_id": executor.target_config_id,
        "gate_policy": gate_policy,
        "strict_gate_mode": strict_gate_mode,
        "candidate_count": max(1, int(candidate_count)),
        "rejected_history_count": len(rejected_history),
        "eval_file": eval_file_used,
        "eval_pack": evidence.get("eval_pack") or {},
        "eval_pack_governance": evidence.get("eval_pack_governance") or {},
        "eval_pack_id": evidence.get("eval_pack_id"),
        "eval_pack_version": evidence.get("eval_pack_version"),
        "eval_pack_fingerprint_sha256": evidence.get("eval_pack_fingerprint_sha256"),
        "split_governance": evidence.get("split_governance") or {},
        "benchmark_parity_status": benchmark_parity,
        "provenance_fingerprint": provenance,
        "production_eval_policy": production_eval_policy,
        "eval_level": evidence_ledger.get("eval_level"),
        "evidence_maturity": evidence_ledger.get("evidence_maturity"),
        "evidence_ledger": evidence_ledger,
        "target_execution_evidence": {"file": "target_execution_evidence.json", "complete": target_execution_evidence.get("complete"), "classification": target_execution_evidence.get("classification"), "eval_level": target_execution_evidence.get("eval_level"), "evidence_maturity": target_execution_evidence.get("evidence_maturity"), "real_hermes_runtime_evidence": target_execution_evidence.get("real_hermes_runtime_evidence"), "real_hermes_runtime_invocation": target_execution_evidence.get("real_hermes_runtime_invocation"), "task_commands_executed": target_execution_evidence.get("task_commands_executed"), "internal_review_only_runner": target_execution_evidence.get("internal_review_only_runner"), "fingerprint_sha256": target_execution_evidence.get("fingerprint_sha256")},
        "reviewer_gate": {"file": "reviewer_gate.json", "passed": reviewer_gate.get("passed"), "adoptable_after_reviewer_gate": reviewer_gate.get("adoptable_after_reviewer_gate"), "fingerprint_sha256": reviewer_gate.get("fingerprint_sha256")},
        "per_task_delta": per_task_delta,
        "heldout_test_sensitivity": heldout_test_sensitivity,
        "split_scores": split_scores,
        "candidate_comparison": candidate_comparison,
        "regression_cases": regression_cases,
        "candidate_summary": candidate_summary,
        "task_counts": evidence.get("task_counts", {k: len(v) for k, v in tasks.items()}),
        "curated_task_count": evidence.get("curated_task_count", 0),
        "validation_current_score": current_score,
        "validation_candidate_score": candidate_score,
        "production_validation_current_score": production_current_score,
        "production_validation_candidate_score": production_candidate_score,
        "production_gate_eligible": production_gate_eligible,
        "production_gate_task_count": len(production_val_tasks),
        "test_score": test_results.get("score"),
        "test_gate_eligible": test_gate_eligible,
        "production_eligibility_reasons": adoptability_reasons,
        "gate_reason": gate_reason,
        "core_abstraction": {
            "skill_document": "trainable_state",
            "target_agent_model": "frozen_scorecard_replay_executor",
            "optimizer_model": "reflection_plus_bounded_edit",
            "environment_benchmark": "hermes_curated_replay_session_synthetic_tasks",
            "validation_gate": "sole_acceptance_gate_candidate_score_gt_current_score_plus_curated_production_and_test_eligibility",
            "hermes_outer_shell": "staged_safety_adopt_rollback_profile_isolation",
        },
        "gate": best_gate,
        "production_gate": production_validation_summary,
        "test_results": test_results,
        "files": artifacts.manifest_files(include_best=final_status == "staged_best"),
    }
    _write_checkpoint(run_dir, resume_input, status="complete", completed_stages=["rollout", "reflect", "aggregate", "select", "update", "evaluate", "final_artifacts"])
    manifest["artifact_lineage"] = _artifact_lineage_status(run_dir, manifest)
    manifest["artifact_sha256"] = artifact_hashes(run_dir, manifest["files"])
    save_manifest(run_dir, manifest)
    result = {"success": True, "run_id": rid, "status": final_status, "adoptable": adoptable, "production_gate_eligible": production_gate_eligible, "test_gate_eligible": test_gate_eligible, "strict_gate_mode": strict_gate_mode, "eval_level": evidence_ledger.get("eval_level"), "evidence_maturity": evidence_ledger.get("evidence_maturity"), "evidence_ledger": evidence_ledger, "not_adoptable_reasons": adoptability_reasons, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(artifacts.diff), "report_path": str(artifacts.report), "gate": best_gate, "production_gate": production_validation_summary, "test_results": test_results, "heldout_test_sensitivity": heldout_test_sensitivity, "split_scores": split_scores, "per_task_delta": per_task_delta, "candidate_comparison": candidate_comparison, "regression_cases": regression_cases, "candidate_summary": candidate_summary, "optimizer_backend_config": optimizer_config, "target_backend_config": target_config, "gate_policy": gate_policy, "provenance_fingerprint": provenance, "artifact_lineage": manifest["artifact_lineage"], "benchmark_parity_status": benchmark_parity, "changed": bool(diff), "eval_file": eval_file_used, "task_counts": evidence.get("task_counts", {k: len(v) for k, v in tasks.items()}), "current_score": current_score, "candidate_score": candidate_score, "production_current_score": production_current_score, "production_candidate_score": production_candidate_score, "gate_reason": gate_reason, "checkpoint_path": str(run_dir / "checkpoint.json"), "slow_meta_path": str(artifacts.slow_meta)}
    return result


def propose_skill(original: str, evidence: list[str] | None = None, goal: str | None = None, ctx: Any = None, use_llm: bool = False) -> tuple[str, str]:
    fm, body = _frontmatter_split(original)
    bullets = []
    if goal:
        bullets.append(f"- Candidate rule: when this skill is invoked, explicitly check the user's goal: {goal.strip()[:240]}")
    if evidence:
        bullets.append("- Evidence-informed TODO: review recent redacted session snippets and convert recurring successful patterns into stable instructions.")
        for i, snip in enumerate(evidence[:3], 1):
            bullets.append(f"  - Redacted snippet {i}: {snip[:220]}")
    if not bullets:
        bullets = ["- Candidate rule: add a short self-check before final answers: confirm scope, required tools, and safety constraints."]
    appendix = "\n\n## SkillOpt Candidate Improvements (staged)\n\n" + "\n".join(bullets) + "\n"
    return fm + body.rstrip() + appendix, "deterministic-legacy"


def dry_run(skill: str | None = None, goal: str | None = None, session_search: str | None = None, hermes_home_path: str | None = None, ctx: Any = None, use_llm: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    target = find_skill(home, skill)
    original = read_text(target.path)
    evidence = evidence_from_state(home, session_search or goal)
    proposed, engine = propose_skill(original, evidence, goal, ctx, use_llm)
    rid = now_id() + "-" + target.name.replace("/", "-")
    run_dir = dirs["staging"] / rid
    run_dir.mkdir(parents=True, exist_ok=False)
    diff = make_diff(original, proposed, target.relpath)
    manifest = {"run_id": rid, "status": "staged", "adoptable": False, "review_only": True, "created_at": datetime.now(timezone.utc).isoformat(), "hermes_home": str(home), "skill_name": target.name, "skill_path": str(target.path), "skill_relpath": target.relpath, "original_sha256": sha256_text(original), "proposed_sha256": sha256_text(proposed), "engine": engine, "files": {"original": "original_SKILL.md", "proposed": "proposed_SKILL.md", "diff": "diff.patch", "report": "report.md", "evidence": "evidence.json"}}
    write_text(run_dir / "original_SKILL.md", original)
    write_text(run_dir / "proposed_SKILL.md", proposed)
    write_text(run_dir / "diff.patch", diff)
    write_text(run_dir / "evidence.json", json.dumps({"snippets": evidence, "session_harvest_provenance": {"schema_version": "hermes-skillopt-session-harvest-provenance-v1", "source": "direct-state-db-and-log-fallback", "review_only": True, "allow_production_adoption": False, "warning": "Dry-run direct/session-mined evidence is review-only and cannot satisfy production adoption."}}, ensure_ascii=False, indent=2) + "\n")
    write_text(run_dir / "report.md", f"# SkillOpt dry run\n\n- run_id: {rid}\n- skill: {target.name}\n- engine: {engine}\n- changed: {bool(diff)}\n\n```diff\n{diff[:4000]}\n```\n")
    manifest["artifact_sha256"] = artifact_hashes(run_dir, manifest["files"])
    save_manifest(run_dir, manifest)
    return {"success": True, "run_id": rid, "status": "staged", "adoptable": False, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "changed": bool(diff)}


def status(hermes_home_path: str | None = None) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = skillopt_paths(home)
    runs = []
    seen_dirs: set[Path] = set()
    manifest_keys = ("run_id", "status", "skill_name", "created_at", "engine", "backend", "adoptable", "production_gate_eligible", "test_gate_eligible", "eval_level", "evidence_maturity", "evidence_ledger", "target_executor", "target_execution_evidence", "reviewer_gate", "split_scores", "production_eligibility_reasons", "validation_current_score", "validation_candidate_score", "test_score", "native_hermes_metadata", "native_hermes_adopt_guard_at_stage")
    for m in sorted(dirs["staging"].glob("*/manifest.json"), reverse=True)[:20]:
        try:
            d = json.loads(read_text(m))
            run_dir = m.parent
            seen_dirs.add(run_dir)
            row = {k: d.get(k) for k in manifest_keys}
            row["readiness_adoptability"] = readiness_adoptability_schema(d)
            row["evidence_ledger"] = d.get("evidence_ledger") if isinstance(d.get("evidence_ledger"), dict) else _eval_evidence_ledger(d)
            row["eval_level"] = row["evidence_ledger"].get("eval_level")
            row["evidence_maturity"] = row["evidence_ledger"].get("evidence_maturity")
            row["evidence_class"] = "production_candidate" if row["readiness_adoptability"].get("adoptable") and row["readiness_adoptability"].get("production_gate_eligible") and row["readiness_adoptability"].get("test_gate_eligible") and row["evidence_ledger"].get("production_runtime_ready") else "review_only_or_not_ready"
            row["score_provenance"] = _score_provenance_summary(d)
            row["run_dir"] = str(run_dir)
            hygiene = _hygiene_row(run_dir, stale_after_hours=24.0)
            row["artifact_lineage"] = d.get("artifact_lineage") or _artifact_lineage_status(run_dir, d)
            row["artifact_classification"] = hygiene.get("classification") or "unverified"
            row["artifact_hygiene_reasons"] = hygiene.get("reasons") or []
            row["safe_reuse_completed"] = row["artifact_classification"] == "complete_verified"
            row["partial_continuation_available"] = False
            row["next_safe_action"] = hygiene.get("next_safe_action") or row["readiness_adoptability"].get("next_safe_action")
            runs.append(row)
        except Exception:
            pass
    checkpoint_only = []
    for cp in sorted(dirs["staging"].glob("*/checkpoint.json"), reverse=True)[:20]:
        run_dir = cp.parent
        if run_dir in seen_dirs:
            continue
        try:
            checkpoint = json.loads(read_text(cp))
        except Exception as exc:
            checkpoint = {"status": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
        lineage = _artifact_lineage_status(run_dir, {}, checkpoint)
        reasons = []
        if checkpoint.get("status") != "complete":
            reasons.append("run is incomplete; partial-stage continuation is refused because replay could skip gates/adopt checks")
        if not (run_dir / "manifest.json").is_file():
            reasons.append("completed-run reuse unavailable until manifest artifact hashes verify")
        age_hours = round(_dir_age_seconds(run_dir) / 3600.0, 3)
        artifact_classification = "checkpoint_only_recent" if age_hours < 24.0 else "stale_incomplete"
        if checkpoint.get("status") == "complete":
            artifact_classification = "checkpoint_only_recent" if age_hours < 24.0 else "stale_checkpoint_only"
        next_safe_action = "Inspect checkpoint/report only; retry as a new full run if abandoned. Do not resume partial stages or adopt."
        row = {
            "run_id": run_dir.name,
            "status": checkpoint.get("status") or "checkpoint_only",
            "checkpoint_status": checkpoint.get("status"),
            "skill_name": lineage.get("skill_name"),
            "adoptable": False,
            "manifest_present": False,
            "report_present": (run_dir / "report.md").is_file(),
            "partial_continuation_available": False,
            "safe_reuse_completed": False,
            "artifact_classification": artifact_classification,
            "age_hours": age_hours,
            "next_safe_action": next_safe_action,
            "refusal_reasons": sorted(set(reasons)),
            "cleanup_guidance": "No automatic cleanup; inspect run_dir and confirm no writer is active before manual removal.",
            "run_dir": str(run_dir),
            "artifact_state": _slim_artifact_state(run_dir),
            "artifact_lineage": lineage,
            "eval_level": "review_only",
            "evidence_maturity": "review_only_static_replay_or_incomplete_runtime",
            "evidence_ledger": _eval_evidence_ledger({}),
            "evidence_class": "review_only_or_not_ready",
        }
        checkpoint_only.append(row)
    runs.extend(checkpoint_only)
    runs = runs[:20]
    return {"success": True, "hermes_home": str(home), "skills_count": len(discover_skills(home)), "staging": str(dirs["staging"]), "backups": str(dirs["backups"]), "recent_runs": runs, "stale_or_incomplete_checkpoints": checkpoint_only[:20], "tool_safety": tool_safety_catalog()}


def latest_run_id(hermes_home_path: str | None = None) -> str:
    """Return the newest staged run id without creating or modifying files."""

    rows = status(hermes_home_path).get("recent_runs") or []
    if not rows:
        raise ValueError("no SkillOpt staged runs found")
    rid = str(rows[0].get("run_id") or "").strip()
    if not rid:
        raise ValueError("newest SkillOpt run has no run_id")
    return rid


def review_latest(hermes_home_path: str | None = None, include_diff_chars: int = 4000, slim: bool = False) -> dict[str, Any]:
    """Review the newest staged run; read-only convenience wrapper."""

    return review(latest_run_id(hermes_home_path), hermes_home_path=hermes_home_path, include_diff_chars=include_diff_chars, slim=slim)


def _gate_name(gate: Any, *, fallback: str) -> dict[str, Any]:
    if isinstance(gate, dict):
        return {
            "present": True,
            "accepted": gate.get("accepted") is True,
            "current_score": gate.get("current_score"),
            "candidate_score": gate.get("candidate_score"),
            "rationale": gate.get("rationale"),
            "metric_policy": gate.get("metric_policy"),
        }
    return {"present": False, "accepted": False, "reason": fallback}


def readiness_adoptability_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Canonical decision vocabulary shared by status/review/doctor surfaces.

    This is intentionally descriptive only.  It does not relax adoption gates;
    adoption still reads the manifest guard fields and requires explicit adopt.
    """

    blockers = [str(r) for r in (payload.get("production_eligibility_reasons") or payload.get("not_adoptable_reasons") or []) if r]
    warnings: list[str] = []
    adoptable = payload.get("adoptable") is True
    production_gate_eligible = payload.get("production_gate_eligible") is True
    test_gate_eligible = payload.get("test_gate_eligible") is True
    strict_gate_mode = payload.get("strict_gate_mode") is True or (isinstance(payload.get("gate_policy"), dict) and payload["gate_policy"].get("mode") == "strict")
    raw_ledger = payload.get("evidence_ledger")
    evidence_ledger: dict[str, Any] = raw_ledger if isinstance(raw_ledger, dict) else _eval_evidence_ledger(payload)
    if adoptable and evidence_ledger.get("production_runtime_ready") is not True:
        warnings.append("inconsistent manifest: adoptable true without production-runtime evidence ledger readiness")
    if adoptable and (not production_gate_eligible or not test_gate_eligible):
        warnings.append("inconsistent manifest: adoptable true without both production/test eligibility gates")
    review_only = not adoptable
    if not blockers and review_only:
        if not production_gate_eligible:
            blockers.append("missing accepted explicit curated production validation gate")
        if not test_gate_eligible:
            blockers.append("held-out test split is missing, non-production, or below threshold")
        if not strict_gate_mode:
            blockers.append("production adoption requires strict gate mode; requested gate mode is review-only")
    status_value = payload.get("status")
    validation_gate = _gate_name(payload.get("gate"), fallback="validation gate unavailable")
    production_best_gate = _gate_name(payload.get("production_gate"), fallback="production best gate unavailable")
    heldout_test_gate = {
        "present": isinstance(payload.get("test_results"), dict),
        "eligible": test_gate_eligible,
        "score": (payload.get("test_results") or {}).get("score") if isinstance(payload.get("test_results"), dict) else payload.get("test_score"),
    }
    if adoptable:
        next_safe_action = f"Review artifacts, then explicitly run adopt with typed confirmation for run {payload.get('run_id')}."
    elif status_value in {"staged_best", "accepted", "adopted"}:
        next_safe_action = "Inspect report/diff; rerun with production intent, strict gate, no mock, and explicit curated eval evidence before adopt."
    else:
        next_safe_action = "Inspect report/gate reasons; improve eval coverage or skill inputs and rerun staged optimize."
    return {
        "schema_version": "hermes-skillopt-readiness-adoptability-v1",
        "validation_gate": validation_gate,
        "production_best_gate": production_best_gate,
        "heldout_test_gate": heldout_test_gate,
        "adoptable": adoptable,
        "production_gate_eligible": production_gate_eligible,
        "test_gate_eligible": test_gate_eligible,
        "eval_level": evidence_ledger.get("eval_level"),
        "evidence_maturity": evidence_ledger.get("evidence_maturity"),
        "evidence_ledger": evidence_ledger,
        "review_only": review_only,
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
        "next_safe_action": next_safe_action,
    }


def review_decision_summary(run_id: str | None = None, hermes_home_path: str | None = None) -> dict[str, Any]:
    """Return a slim, decision-first review summary for CLI/plugin UX."""

    rid = (run_id or "").strip()
    reviewed = review_latest(hermes_home_path, slim=True) if not rid or rid == "latest" else review(rid, hermes_home_path=hermes_home_path, slim=True)
    reasons = reviewed.get("not_adoptable_reasons") or []
    if reviewed.get("adoptable") is True:
        decision = "ready_for_explicit_adopt"
        next_action = f"Review artifacts, then type ADOPT {reviewed['run_id']} with the adopt command if you intend production writeback."
    elif reviewed.get("accepted") is True:
        decision = "review_only_not_adoptable"
        next_action = "Inspect report/diff; rerun with production intent and strict real eval evidence before adopt."
    else:
        decision = "not_ready_rejected_or_incomplete"
        next_action = "Inspect report/gate reasons; improve eval coverage or skill inputs and rerun staged optimize."
    adoptable = reviewed.get("adoptable") is True
    production_gate_eligible = reviewed.get("production_gate_eligible") is True
    test_gate_eligible = reviewed.get("test_gate_eligible") is True
    evidence_class = "production_candidate" if adoptable and production_gate_eligible and test_gate_eligible and (reviewed.get("evidence_ledger") or {}).get("production_runtime_ready") else "review_only_or_not_ready"
    readiness = readiness_adoptability_schema(reviewed)
    return {
        "success": True,
        "run_id": reviewed.get("run_id"),
        "decision": decision,
        "status": reviewed.get("status"),
        "skill": reviewed.get("skill"),
        "adoptable": adoptable,
        "accepted": reviewed.get("accepted") is True,
        "production_gate_eligible": production_gate_eligible,
        "test_gate_eligible": test_gate_eligible,
        "evidence_class": evidence_class,
        "eval_level": reviewed.get("eval_level") or readiness.get("eval_level"),
        "evidence_maturity": reviewed.get("evidence_maturity") or readiness.get("evidence_maturity"),
        "evidence_ledger": reviewed.get("evidence_ledger") or readiness.get("evidence_ledger"),
        "native_hermes_metadata": reviewed.get("native_hermes_metadata"),
        "native_hermes_adopt_guard": reviewed.get("native_hermes_adopt_guard"),
        "validation_gate": readiness["validation_gate"],
        "production_best_gate": readiness["production_best_gate"],
        "heldout_test_gate": readiness["heldout_test_gate"],
        "review_only": readiness["review_only"],
        "blockers": readiness["blockers"],
        "warnings": readiness["warnings"],
        "not_adoptable_reasons": readiness["blockers"],
        "gate": reviewed.get("gate"),
        "score_provenance": reviewed.get("score_provenance"),
        "artifact_refs": reviewed.get("artifact_refs"),
        "next_action": next_action,
        "next_safe_action": next_action,
        "readiness_adoptability": readiness,
    }


def review_digest(run_id: str | None = None, hermes_home_path: str | None = None) -> dict[str, Any]:
    """Telegram-friendly slim review digest: decision fields plus artifact refs only."""

    summary = review_decision_summary(run_id or "latest", hermes_home_path=hermes_home_path)
    refs = summary.get("artifact_refs") or {}
    raw_score_prov = summary.get("score_provenance")
    score_prov: dict[str, Any] = dict(raw_score_prov) if isinstance(raw_score_prov, dict) else {}
    raw_eval_pack = score_prov.get("eval_pack")
    eval_pack: dict[str, Any] = dict(raw_eval_pack) if isinstance(raw_eval_pack, dict) else {}
    lines = [
        f"Hermes SkillOpt review digest: {summary.get('run_id')}",
        f"decision: {summary.get('decision')}",
        f"adoptable: {summary.get('adoptable')} | production_gate_eligible: {summary.get('production_gate_eligible')} | test_gate_eligible: {summary.get('test_gate_eligible')}",
        f"review_only: {summary.get('review_only')}",
        f"eval_level: {summary.get('eval_level')} | evidence_maturity: {summary.get('evidence_maturity')}",
        f"evidence_class: {summary.get('evidence_class')}",
        "boundary: SkillOpt is staged eval evidence/adoption recommendations only; Hermes curator owns lifecycle/archive/consolidation.",
        f"score_provenance: executor={score_prov.get('target_executor')} backend={score_prov.get('optimizer_backend')} source={score_prov.get('score_source')}",
        f"eval_pack: id={eval_pack.get('id')} version={eval_pack.get('version')} path={eval_pack.get('path')} fingerprint={eval_pack.get('fingerprint_sha256')}",
    ]
    blockers = summary.get("blockers") or []
    warnings = summary.get("warnings") or []
    if blockers:
        lines.append("blockers: " + "; ".join(str(b) for b in blockers[:6]))
    if warnings:
        lines.append("warnings: " + "; ".join(str(w) for w in warnings[:4]))
    lines.append("next_safe_action: " + str(summary.get("next_safe_action") or "inspect artifacts"))
    if isinstance(refs, dict):
        for key in ("report", "diff"):
            ref = refs.get(key) if isinstance(refs.get(key), dict) else None
            if ref:
                lines.append(f"{key}: {ref.get('path')} sha256={ref.get('sha256')}")
    return {"success": True, "schema_version": "hermes-skillopt-review-digest-v1", "run_id": summary.get("run_id"), "read_only": True, "auto_adopt": False, "digest": "\n".join(lines), "summary": summary, "artifact_refs": refs}


def notification_digest(surface: str, payload: dict[str, Any], *, limit: int = 6) -> dict[str, Any]:
    """Generic Telegram-friendly digest wrapper for read-only diagnostic surfaces."""

    surface_label = surface.replace("_", "-")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    matrix = payload.get("readiness_matrix") if isinstance(payload.get("readiness_matrix"), dict) else {}
    lines = [
        f"Hermes SkillOpt {surface_label} digest",
        f"success: {payload.get('success')} | mode: {payload.get('mode')}",
        "read_only: True | auto_adopt: False",
        "boundary: scheduled usage is diagnostic only and never auto-adopts; SkillOpt complements Hermes curator lifecycle ownership.",
    ]
    if summary:
        keys = ["skills_count", "inventory_skill_count", "production_eligible_eval_pack_count", "no_eval_pack_count", "invalid_eval_pack_count", "recent_staged_run_count", "latest_run_id"]
        parts = [f"{k}={summary.get(k)}" for k in keys if k in summary]
        if parts:
            lines.append("summary: " + " | ".join(parts))
    if matrix:
        keys = ["total_skills", "no_pack_count", "only_review_only_count", "production_eligible_count", "invalid_pack_count"]
        parts = [f"{k}={matrix.get(k)}" for k in keys if k in matrix]
        if parts:
            lines.append("eval_matrix: " + " | ".join(parts))
    latest = payload.get("latest_run") if isinstance(payload.get("latest_run"), dict) else None
    if latest:
        lines.append(f"latest_run: {latest.get('run_id')} status={latest.get('status')} adoptable={latest.get('adoptable')} eval_level={latest.get('eval_level')}")
    cap = max(1, min(int(limit or 6), 10))
    actions = payload.get("next_actions") if isinstance(payload.get("next_actions"), list) else []
    if actions:
        compact = []
        for row in actions[:cap]:
            if isinstance(row, dict):
                compact.append(f"{row.get('priority', 'info')}:{row.get('action')} — {row.get('reason')}")
        if compact:
            lines.append("next_actions: " + " ; ".join(compact))
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), list) else []
    if diagnostics:
        rows = []
        for row in diagnostics[:cap]:
            if isinstance(row, dict):
                rows.append(f"{row.get('skill')}: has_pack={row.get('has_eval_pack')} prod={row.get('production_eligible')} invalid={row.get('invalid_eval_pack_count')}")
        if rows:
            lines.append("diagnostics: " + " ; ".join(rows))
    skills = payload.get("skills") if isinstance(payload.get("skills"), list) else []
    if skills:
        rows = []
        for row in skills[:cap]:
            if isinstance(row, dict):
                rows.append(f"{row.get('skill')}: has_pack={row.get('has_eval_pack')} prod={row.get('production_eligible')} split={row.get('split_complete')}")
        if rows:
            lines.append("skills: " + " ; ".join(rows))
    rec = payload.get("recommended_next_action") or payload.get("next_safe_action")
    if rec:
        lines.append("recommended_next_action: " + str(rec))
    return {"success": True, "schema_version": "hermes-skillopt-notification-digest-v1", "surface": surface_label, "read_only": True, "auto_adopt": False, "digest": "\n".join(lines), "summary": payload}


def doctor(hermes_home_path: str | None = None, *, skill: str | None = None) -> dict[str, Any]:
    """Read-only readiness/guided UX summary. Does not run evals, adopt, rollback, or fetch network."""

    home = hermes_home(hermes_home_path)
    st = status(str(home))
    dirs = skillopt_paths(home)
    skills = discover_skills(home)
    selected_skill = None
    skill_ready = bool(skills)
    skill_reason = "skills discovered" if skills else "no skills discovered under HERMES_HOME/skills"
    if skill:
        try:
            found = find_skill(home, skill)
            selected_skill = {"name": found.name, "path": str(found.path), "sha256": found.sha256, "package_support": _safe_skill_package_support(found, home)}
            skill_ready = True
            skill_reason = "requested skill found"
        except Exception as exc:
            skill_ready = False
            skill_reason = f"requested skill unavailable: {type(exc).__name__}: {exc}"
    eval_inventory: dict[str, Any]
    try:
        from hermes_skillopt.eval_packs import eval_pack_inventory, eval_pack_workflow_summary

        inv = eval_pack_inventory(hermes_home_path=str(home), skill=skill)
        workflow = eval_pack_workflow_summary(hermes_home_path=str(home), skill=skill, limit=20)
        eval_inventory = {
            "available": True,
            "summary": {k: inv.get(k) for k in ("skills_count", "eval_packs_count", "production_ready_count", "review_only_count") if k in inv},
            "items": (inv.get("items") or inv.get("skills") or [])[:10],
            "readiness_matrix": inv.get("readiness_matrix"),
        }
    except Exception as exc:
        eval_inventory = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    upstream = benchmark_parity_status(str(home))
    recent = st.get("recent_runs") or []
    latest = recent[0] if recent else None
    inventory_items_raw = eval_inventory.get("items")
    inventory_items = inventory_items_raw if isinstance(inventory_items_raw, list) else []
    production_ready = bool(skill_ready and eval_inventory.get("available") and any(isinstance(item, dict) and item.get("production_eligible") for item in inventory_items))
    checklist = [
        {"item": "skill_discovered", "ready": skill_ready, "detail": skill_reason},
        {"item": "eval_inventory_readable", "ready": bool(eval_inventory.get("available")), "detail": eval_inventory.get("reason") or "inventory read"},
        {"item": "strict_real_optimizer_for_production", "ready": False, "detail": "verified at optimize/adopt time; production intent requires strict gate, no mock, and explicit eval_file"},
        {"item": "staged_only", "ready": True, "detail": "doctor/optimize never auto-adopt; adopt requires a separate typed confirmation"},
        {"item": "upstream_parity_claims", "ready": True, "detail": upstream.get("parity_label")},
    ]
    doctor_readiness = {"schema_version": "hermes-skillopt-readiness-adoptability-v1", "validation_gate": {"present": bool(eval_inventory.get("available")), "accepted": bool(eval_inventory.get("available")), "reason": eval_inventory.get("reason") or "inventory read"}, "production_best_gate": {"present": False, "accepted": False, "reason": "doctor is read-only and does not run production validation"}, "heldout_test_gate": {"present": False, "eligible": False, "reason": "doctor is read-only and does not run held-out tests"}, "adoptable": False, "production_gate_eligible": False, "test_gate_eligible": False, "review_only": True, "blockers": ["doctor is read-only; run strict production optimize with explicit curated eval before adopt"], "warnings": [], "next_safe_action": "Run hermes-skillopt eval-pack-inventory, then production optimize with strict/no-mock/explicit curated eval if inventory is production-eligible."}
    return {
        "success": True,
        "mode": "read_only_doctor_no_full_run_no_adopt_no_rollback_no_fetch",
        "read_only": True,
        "auto_adopt": False,
        "hermes_home": str(home),
        "active_hermes_home": str(active_hermes_home()),
        "paths": {"skills": str(home / "skills"), "staging": str(dirs["staging"]), "backups": str(dirs["backups"]), "upstream": str(dirs["upstream"])},
        "skill_readiness": {"ready": skill_ready, "reason": skill_reason, "skills_count": len(skills), "selected_skill": selected_skill},
        "eval_readiness": eval_inventory,
        "recent_runs": recent[:5],
        "latest_run": latest,
        "upstream_parity_posture": {"full_parity_claim": False, "parity_label": upstream.get("parity_label"), "supported_parity_levels": upstream.get("supported_parity_levels"), "unsupported_parity_levels": upstream.get("unsupported_parity_levels")},
        "production_readiness_checklist": checklist,
        "production_ready_hint": production_ready,
        "readiness_adoptability": doctor_readiness,
        "recommended_next_action": {
            "smoke": "hermes-skillopt optimize --intent smoke --skill <skill>",
            "review": "hermes-skillopt optimize --intent review --skill <skill> --eval-file <curated-or-review-pack>",
            "production": "hermes-skillopt optimize --intent production --skill <skill> --eval-file <curated-production-pack> --optimizer-backend hermes --gate-mode strict",
            "ci": "hermes-skillopt conformance --mode quick",
        },
    }


def _skill_frontmatter_map(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    data: dict[str, str] = {}
    for raw in text[4:end].splitlines():
        line = raw.strip()
        if not line or ":" not in line or line.startswith("#"):
            continue
        key, value = line.split(":", 1)
        data[key.strip().lower()] = value.strip().strip('"\'')
    return data


def _truthy_metadata_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "pinned", "archived"}


def _native_sidecar_json(path: Path) -> tuple[Any | None, dict[str, Any]]:
    """Best-effort read of Hermes-owned skill metadata sidecars; never writes."""

    meta = {"path": str(path), "present": path.is_file(), "readable": False, "sha256": None, "error": None}
    if not path.is_file() or path.is_symlink():
        if path.is_symlink():
            meta["error"] = "symlink sidecar skipped"
        return None, meta
    try:
        meta["sha256"] = sha256_file(path)
        data = json.loads(read_text(path))
        meta["readable"] = True
        return data, meta
    except Exception as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        return None, meta


def _native_record_for_skill(data: Any, skill: Skill) -> dict[str, Any]:
    if not isinstance(data, (dict, list)):
        return {}
    keys = {
        skill.name.lower(),
        skill.relpath.lower(),
        str(Path(skill.relpath).parent).lower(),
        skill.path.parent.name.lower(),
    }

    def score_record(key: str | None, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        candidates = {str(key or "").lower()}
        for field in ("name", "skill", "skill_name", "relpath", "path", "skill_relpath", "directory", "id"):
            if value.get(field) is not None:
                candidates.add(str(value.get(field)).lower())
        if candidates & keys:
            return dict(value)
        return {}

    if isinstance(data, dict):
        containers = [data]
        for field in ("skills", "skill_usage", "usage", "installed", "bundled", "manifests", "entries"):
            child = data.get(field)
            if isinstance(child, dict):
                containers.append(child)
            elif isinstance(child, list):
                for row in child:
                    hit = score_record(None, row)
                    if hit:
                        return hit
        for container in containers:
            for key, value in container.items():
                hit = score_record(str(key), value)
                if hit:
                    return hit
                if str(key).lower() in keys and not isinstance(value, dict):
                    return {"value": value}
    if isinstance(data, list):
        for row in data:
            hit = score_record(None, row)
            if hit:
                return hit
    return {}


def _native_bool(record: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if _truthy_metadata_value(str(value) if value is not None else None):
            return True
    return False


def native_skill_metadata_snapshot(home: Path, skill: Skill) -> dict[str, Any]:
    """Read-only snapshot of native Hermes skill metadata used for advisory UX and adopt guards."""

    sidecar_paths = {
        "usage": home / "skills" / ".usage.json",
        "curator_state": home / "skills" / ".curator_state",
        "hub_manifest": home / "skills" / ".hub_manifest.json",
        "bundled_manifest": home / "skills" / ".bundled_manifest.json",
        "manifest": home / "skills" / ".manifest.json",
        "provenance": home / "skills" / ".provenance.json",
        "skill_provenance": home / "skills" / ".skill_provenance.json",
        "per_skill_provenance": skill.path.parent / ".provenance.json",
        "per_skill_skill_provenance": skill.path.parent / ".skill_provenance.json",
    }
    sidecars: dict[str, Any] = {}
    records: dict[str, dict[str, Any]] = {}
    per_skill_sidecars = {"per_skill_provenance", "per_skill_skill_provenance"}
    for name, path in sidecar_paths.items():
        data, meta = _native_sidecar_json(path)
        sidecars[name] = meta
        record = _native_record_for_skill(data, skill) if data is not None else {}
        # Per-skill provenance sidecars are already scoped by their path, so a
        # bare record like {"created_by": "agent", "origin": "hub"} is valid.
        if not record and name in per_skill_sidecars and isinstance(data, dict):
            record = dict(data)
        records[name] = record
    usage = records.get("usage", {})
    curator = records.get("curator_state", {})
    hub = records.get("hub_manifest", {})
    bundled = records.get("bundled_manifest", {}) or records.get("manifest", {})
    merged_text = " ".join(json.dumps(r, sort_keys=True, ensure_ascii=False).lower() for r in records.values() if r)
    provenance_text = " ".join(str(r.get(field, "")).lower() for r in records.values() for field in ("created_by", "origin", "source", "createdBy", "created-by") if isinstance(r, dict))
    agent_tokens = ("agent", "agent_created", "agent-created", "background_review", "background-review")
    user_tokens = ("user", "manual", "human", "local")
    hub_installed = bool(hub) or _native_bool(usage, "hub_installed", "installed_from_hub") or "hub" in provenance_text or "hub" in str(usage.get("origin") or usage.get("source") or "").lower()
    bundled_signal = bool(bundled) or _native_bool(usage, "bundled", "is_bundled") or "bundled" in merged_text
    agent_created = _native_bool(curator, "agent_created", "background_review") or any(token in merged_text for token in agent_tokens) or any(token in provenance_text for token in agent_tokens)
    background_review = _native_bool(curator, "background_review", "reviewed_in_background") or "background_review" in merged_text or "background-review" in merged_text
    user_created = any(token in provenance_text for token in user_tokens) or any(f'"created_by": "{token}"' in merged_text for token in user_tokens)
    signals = {
        "hub_installed": hub_installed,
        "bundled": bundled_signal,
        "agent_created": agent_created,
        "user_created": user_created,
        "background_review": background_review,
        "local_manual_unknown": not bool(hub_installed or bundled_signal or agent_created or background_review or user_created) and not any(token in merged_text for token in ("hub", "bundled", "agent_created", "background_review")),
        "pinned": _native_bool(usage, "pinned", "pin") or _native_bool(curator, "pinned", "pin") or "pinned" in merged_text,
        "archived": _native_bool(usage, "archived") or _native_bool(curator, "archived") or "archived" in merged_text,
        "curator_managed": bool(curator) or _native_bool(curator, "managed", "curator_managed"),
        "stale": _native_bool(usage, "stale") or "stale" in str(usage.get("state") or usage.get("status") or "").lower(),
        "active": _native_bool(usage, "active") or str(usage.get("state") or usage.get("status") or "").lower() == "active",
    }
    labels = [k for k, v in signals.items() if v and k != "curator_managed"] or ["local_manual_unknown"]
    snapshot = {
        "schema_version": "hermes-native-skill-metadata-snapshot-v1",
        "read_only": True,
        "skill_name": skill.name,
        "skill_relpath": skill.relpath,
        "labels": labels,
        "signals": signals,
        "records": {k: v for k, v in records.items() if v},
        "sidecars": sidecars,
    }
    snapshot["fingerprint_sha256"] = _stable_json_sha({"signals": signals, "records": snapshot["records"], "sidecars": {k: {"present": v.get("present"), "readable": v.get("readable"), "sha256": v.get("sha256")} for k, v in sidecars.items()}})
    return snapshot


def native_adopt_guard(home: Path, skill: Skill, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    current = native_skill_metadata_snapshot(home, skill)
    signals = current.get("signals") if isinstance(current.get("signals"), dict) else {}
    blockers = []
    for key in ("hub_installed", "bundled", "pinned", "archived", "curator_managed"):
        if signals.get(key):
            blockers.append(f"native Hermes metadata marks skill as {key}; adopt is blocked by default and force does not override")
    base = (manifest or {}).get("native_hermes_metadata") if isinstance(manifest, dict) else None
    base_fingerprint = base.get("fingerprint_sha256") if isinstance(base, dict) else None
    if base_fingerprint and base_fingerprint != current.get("fingerprint_sha256"):
        blockers.append("native Hermes metadata changed since staging; rerun SkillOpt after reviewing native sidecars")
    return {"schema_version": "hermes-native-adopt-guard-v1", "allowed": not blockers, "blockers": blockers, "current": current, "base_fingerprint_sha256": base_fingerprint, "current_fingerprint_sha256": current.get("fingerprint_sha256"), "force_override_allowed": False}


def _skill_metadata_summary(skill: Skill, home: Path | None = None) -> dict[str, Any]:
    """Advisory curator/package metadata; never an adoption authority."""

    try:
        text = read_text(skill.path)
    except Exception:
        text = ""
    fm = _skill_frontmatter_map(text)
    rel_lower = skill.relpath.lower()
    values = " ".join(str(v).lower() for v in fm.values())
    origin = str(fm.get("origin") or fm.get("source") or fm.get("created_by") or "").lower()
    signals = {
        "pinned": _truthy_metadata_value(fm.get("pinned")) or "pinned" in values or "/pinned/" in f"/{rel_lower}",
        "archived": _truthy_metadata_value(fm.get("archived")) or "archived" in values or "/archive" in f"/{rel_lower}",
        "agent_created": any(token in origin or token in values for token in ("agent", "hermes", "assistant", "ai-generated", "ai generated")),
        "bundled": "bundled" in values or "/bundled/" in f"/{rel_lower}",
        "hub_installed": "hub" in origin or "hub" in values or "/hub/" in f"/{rel_lower}",
        "local": "local" in origin or "/local/" in f"/{rel_lower}",
        "custom": "custom" in origin or "custom" in values or "/custom/" in f"/{rel_lower}",
    }
    labels = [key for key, present in signals.items() if present]
    warnings = []
    if signals["pinned"]:
        warnings.append("pinned skill: scout treats this as advisory/skip-by-default; do not optimize without explicit owner review")
    if signals["archived"]:
        warnings.append("archived skill: scout treats this as advisory/skip-by-default; prefer no optimization")
    package_support = _safe_skill_package_support(skill, home) if home is not None else {"schema_version": "hermes-skillopt-skill-package-support-v1", "advisory_only": True, "content_included": False, "skill_relpath": skill.relpath, "support_dirs": {}, "total_file_count": 0, "warnings": ["home unavailable; package support not scanned"]}
    native = native_skill_metadata_snapshot(home, skill) if home is not None else {"schema_version": "hermes-native-skill-metadata-snapshot-v1", "read_only": True, "labels": ["unavailable"], "signals": {}, "sidecars": {}}
    native_signals = native.get("signals") if isinstance(native.get("signals"), dict) else {}
    combined_signals = {**signals, **{f"native_{k}": v for k, v in native_signals.items()}}
    return {
        "schema_version": "hermes-skillopt-skill-metadata-advisory-v1",
        "advisory_only": True,
        "hard_gate": False,
        "labels": sorted(set((labels or ["unlabeled"]) + [f"native:{x}" for x in (native.get("labels") or [])])),
        "signals": combined_signals,
        "native_hermes_metadata": native,
        "frontmatter_keys": sorted(fm.keys()),
        "package_summary": {
            "name": skill.name,
            "relpath": skill.relpath,
            "sha256": skill.sha256,
            "source": fm.get("source") or fm.get("origin"),
            "version": fm.get("version"),
            "description": fm.get("description"),
            "support_total_file_count": package_support.get("total_file_count"),
            "support_dirs_present": [name for name, data in (package_support.get("support_dirs") or {}).items() if isinstance(data, dict) and data.get("present")],
        },
        "package_support": package_support,
        "warnings": sorted(set(warnings + list(package_support.get("warnings") or []))),
    }



def _usage_score_from_native(native: dict[str, Any]) -> tuple[int, list[str]]:
    records = native.get("records") if isinstance(native.get("records"), dict) else {}
    usage = records.get("usage") if isinstance(records.get("usage"), dict) else {}
    reasons: list[str] = []
    score = 0
    for key in ("count", "usage_count", "invocation_count", "calls", "runs"):
        try:
            value = int(usage.get(key) or 0)
        except Exception:
            value = 0
        if value > 0:
            score += min(value, 20)
            reasons.append(f"usage.{key}={value}")
            break
    state = str(usage.get("state") or usage.get("status") or "").lower()
    if state == "active":
        score += 8
        reasons.append("native usage marks active")
    if usage.get("last_used") or usage.get("last_invoked_at"):
        score += 5
        reasons.append("native usage has recent-use metadata")
    return score, reasons


def skill_readiness_queue(hermes_home_path: str | None = None, *, skill: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Deterministic read-only queue of high-value SkillOpt candidates and readiness blockers."""

    home = hermes_home(hermes_home_path)
    try:
        from hermes_skillopt.eval_packs import eval_pack_inventory

        inv = eval_pack_inventory(hermes_home_path=str(home), skill=skill)
    except Exception as exc:
        inv = {"success": False, "error": f"{type(exc).__name__}: {redact_secrets(str(exc))}", "skills": []}
    rows_raw = inv.get("skills") if isinstance(inv, dict) else []
    inv_rows = [r for r in rows_raw if isinstance(r, dict)] if isinstance(rows_raw, list) else []
    cap = max(1, min(int(limit or 20), 200))
    command_home = shlex.quote(str(home))
    queue: list[dict[str, Any]] = []
    for row in inv_rows:
        skill_name = str(row.get("skill") or "")
        try:
            sk = find_skill(home, skill_name)
            metadata = _skill_metadata_summary(sk, home)
        except Exception:
            metadata = {"signals": {}, "warnings": ["skill metadata unavailable"], "package_summary": {}, "package_support": {}}
        signals = metadata.get("signals") if isinstance(metadata.get("signals"), dict) else {}
        native = row.get("native_hermes_metadata") if isinstance(row.get("native_hermes_metadata"), dict) else {}
        skill_type = row.get("skill_type") if isinstance(row.get("skill_type"), dict) else {}
        package_support = row.get("skill_package_support") if isinstance(row.get("skill_package_support"), dict) else {}
        reasons: list[str] = []
        blockers: list[str] = []
        warnings: list[str] = []
        score = 0
        usage_score, usage_reasons = _usage_score_from_native(native)
        score += usage_score
        reasons.extend(usage_reasons)
        type_name = str((skill_type or {}).get("type") or (skill_type or {}).get("kind") or "")
        if type_name:
            score += 5
            reasons.append(f"skill_type={type_name}")
        support_count = int((package_support or {}).get("total_file_count") or 0)
        if support_count:
            score += min(10, support_count)
            reasons.append(f"package_support_files={support_count}")
        if row.get("production_eligible"):
            score += 25
            reasons.append("production-eligible eval pack exists")
        elif row.get("split_complete"):
            score += 12
            reasons.append("review eval pack has complete train/val/test splits")
        elif row.get("has_eval_pack"):
            score += 5
            reasons.append("eval pack exists but is immature/review-only")
        else:
            blockers.append("no matching eval pack found")
        if row.get("invalid_eval_pack_count"):
            blockers.append("invalid eval pack must be fixed")
            score -= 20
        native_guard = bool(signals.get("pinned") or signals.get("archived") or signals.get("native_pinned") or signals.get("native_archived") or signals.get("native_hub_installed") or signals.get("native_bundled") or signals.get("native_curator_managed"))
        if native_guard:
            blockers.append("native/pinned/hub/bundled/archived/curator-managed guard requires owner review; optimize/adopt blocked by default")
            score -= 100
        if row.get("review_only"):
            warnings.append("eval evidence is review-only; production requires explicit curated policy/contract and strict run")
        warnings.extend(str(w) for w in (metadata.get("warnings") or []) if w)
        readiness = "blocked_native_guard" if native_guard else ("ready_for_strict_candidate_review" if row.get("production_eligible") else ("needs_eval_pack_curation" if row.get("has_eval_pack") else "needs_eval_pack_authoring"))
        band = "blocked" if native_guard or row.get("invalid_eval_pack_count") else ("high" if score >= 30 else "medium" if score >= 12 else "low")
        if not row.get("has_eval_pack"):
            safe_cmd = f"hermes-skillopt --home {command_home} eval-pack-autopilot --skill {shlex.quote(skill_name)}"
        else:
            safe_cmd = f"hermes-skillopt --home {command_home} eval-pack-doctor --skill {shlex.quote(skill_name)}"
        queue.append({
            "skill": skill_name,
            "priority_score": int(score),
            "priority_band": band,
            "readiness": readiness,
            "reasons": sorted(set(reasons)) or ["baseline discovered skill"],
            "blockers": sorted(set(str(b) for b in blockers if b)),
            "warnings": sorted(set(str(w) for w in warnings if w)),
            "safe_next_command": safe_cmd,
            "blocked_by_native_guard": native_guard,
            "production_gate_eligible": bool(row.get("production_eligible")),
            "review_only": bool(row.get("review_only")),
            "eval_pack_maturity": {"has_eval_pack": bool(row.get("has_eval_pack")), "split_complete": bool(row.get("split_complete")), "invalid_eval_pack_count": int(row.get("invalid_eval_pack_count") or 0), "recommended_next_action": row.get("recommended_next_action")},
            "native_hermes_metadata": native,
            "skill_type": skill_type,
        })
    queue.sort(key=lambda r: (r["blocked_by_native_guard"], -int(r["priority_score"]), str(r["skill"])))
    return {"success": True, "schema_version": "hermes-skillopt-skill-readiness-queue-v1", "mode": "skill_readiness_queue_read_only_no_full_run_no_optimize_no_adopt_no_rollback_no_fetch", "read_only": True, "auto_adopt": False, "cron_schedule": False, "hermes_home": str(home), "skill_count": len(queue), "queue": queue[:cap], "eval_pack_inventory_summary": {"schema_version": inv.get("schema_version") if isinstance(inv, dict) else None, "no_pack_count": inv.get("no_pack_count") if isinstance(inv, dict) else None, "production_eligible_count": inv.get("production_eligible_count") if isinstance(inv, dict) else None, "invalid_pack_count": inv.get("invalid_pack_count") if isinstance(inv, dict) else None}, "read_only_guards": ["does not call full_run", "does not call optimize/guided_optimize", "does not adopt", "does not rollback", "does not fetch/update upstream", "does not schedule cron"]}

def scout(hermes_home_path: str | None = None, *, skill: str | None = None, limit: int = 5, stale_after_hours: float = 24.0, output_path: str | None = None) -> dict[str, Any]:
    """Notification-ready read-only SkillOpt scout summary."""

    home = hermes_home(hermes_home_path)
    cap = max(1, min(int(limit or 5), 20))
    st = status(str(home))
    try:
        from hermes_skillopt.eval_packs import eval_pack_inventory, eval_pack_workflow_summary

        inv = eval_pack_inventory(hermes_home_path=str(home), skill=skill)
        workflow = eval_pack_workflow_summary(hermes_home_path=str(home), skill=skill, limit=limit)
    except Exception as exc:
        inv = {"success": False, "error": f"{type(exc).__name__}: {redact_secrets(str(exc))}", "skills": []}
        workflow = {"success": False, "error": inv.get("error"), "workflow": []}
    try:
        readiness_queue = skill_readiness_queue(str(home), skill=skill, limit=cap)
    except Exception as exc:
        readiness_queue = {"success": False, "error": f"{type(exc).__name__}: {redact_secrets(str(exc))}", "queue": []}
    hygiene = artifact_hygiene_report(str(home), limit=max(cap, 20), stale_after_hours=stale_after_hours)

    skills = discover_skills(home)
    if skill:
        try:
            skills = [find_skill(home, skill)]
        except Exception:
            skills = []
    metadata_by_name = {sk.name: _skill_metadata_summary(sk, home) for sk in skills}
    skipped_by_default = [name for name, meta in metadata_by_name.items() if meta["signals"].get("pinned") or meta["signals"].get("archived") or meta["signals"].get("native_pinned") or meta["signals"].get("native_archived") or meta["signals"].get("native_hub_installed") or meta["signals"].get("native_bundled")]
    inv_skills_raw = inv.get("skills") if isinstance(inv, dict) else []
    inv_skills = inv_skills_raw if isinstance(inv_skills_raw, list) else []
    production_ready = [row for row in inv_skills if isinstance(row, dict) and row.get("production_eligible")]
    no_pack = [row for row in inv_skills if isinstance(row, dict) and not row.get("has_eval_pack")]
    invalid_pack = [row for row in inv_skills if isinstance(row, dict) and int(row.get("invalid_eval_pack_count") or 0) > 0]
    recent_runs_raw = st.get("recent_runs") if isinstance(st, dict) else []
    recent_runs = recent_runs_raw if isinstance(recent_runs_raw, list) else []
    latest = recent_runs[0] if recent_runs else None
    latest_run_id = str((latest or {}).get("run_id") or "").strip()
    hygiene_counts = hygiene.get("classification_counts") if isinstance(hygiene, dict) else {}
    hygiene_attention = {k: v for k, v in (hygiene_counts or {}).items() if k not in {"complete_verified"} and v}

    command_home = shlex.quote(str(home))
    skill_suffix = f" --skill {shlex.quote(str(skill))}" if skill else ""
    safe_commands: dict[str, str] = {
        "scout": f"hermes-skillopt --home {command_home} scout{skill_suffix}",
        "eval_inventory": f"hermes-skillopt --home {command_home} eval-pack-inventory{skill_suffix}",
        "eval_workflow": f"hermes-skillopt --home {command_home} eval-pack-workflow{skill_suffix}",
        "skill_readiness_queue": f"hermes-skillopt --home {command_home} skill-readiness-queue{skill_suffix}",
        "artifact_hygiene": f"hermes-skillopt --home {command_home} artifact-hygiene-report --limit 200",
        "conformance_quick": "hermes-skillopt conformance --mode quick",
    }
    if latest_run_id:
        safe_commands["review_latest_staged_run"] = f"hermes-skillopt --home {command_home} review {shlex.quote(latest_run_id)} --digest"
    if production_ready:
        first = shlex.quote(str(production_ready[0].get("skill") or "<skill>"))
        safe_commands["production_candidate_when_eligible"] = f"hermes-skillopt --home {command_home} optimize --intent production --skill {first} --eval-file <curated-production-pack> --optimizer-backend hermes --gate-mode strict"
    coverage_target = no_pack[0] if no_pack else (invalid_pack[0] if invalid_pack else {})
    first_coverage_gap = shlex.quote(str(coverage_target.get("skill") or skill or "<skill>"))
    safe_commands["create_or_curate_eval_pack"] = f"hermes-skillopt --home {command_home} eval-pack-scaffold --skill {first_coverage_gap}"

    next_actions: list[dict[str, Any]] = []
    if latest_run_id:
        next_actions.append({"action": "review_staged_run", "priority": "high", "command": safe_commands["review_latest_staged_run"], "reason": "newest staged run is available for explicit review; no auto-adopt"})
    if no_pack or invalid_pack or not inv_skills:
        next_actions.append({"action": "create_or_curate_eval_pack", "priority": "high", "command": safe_commands["create_or_curate_eval_pack"], "reason": "missing/invalid eval-pack coverage blocks production-grade evidence"})
    if production_ready:
        next_actions.append({"action": "run_production_candidate_only_when_eligible", "priority": "medium", "command": safe_commands["production_candidate_when_eligible"], "reason": "inventory found production-eligible pack metadata; actual optimize/adopt gates remain authoritative"})
    if hygiene_attention:
        next_actions.append({"action": "inspect_artifact_hygiene", "priority": "medium", "command": safe_commands["artifact_hygiene"], "reason": "staging hygiene has non-verified/tamper/stale classifications"})
    if skipped_by_default:
        next_actions.append({"action": "owner_review_for_pinned_or_archived", "priority": "medium", "command": safe_commands["eval_inventory"], "reason": "pinned/archived advisory metadata present; scout skips/warns by default", "skills": skipped_by_default[:10]})
    if not next_actions:
        next_actions.append({"action": "keep_monitoring", "priority": "low", "command": safe_commands["scout"], "reason": "no immediate staged run, eval-pack, or artifact hygiene action detected"})

    out = {
        "success": True,
        "schema_version": "hermes-skillopt-scout-v1",
        "mode": "read_only_scout_no_full_run_no_optimize_no_adopt_no_rollback_no_fetch",
        "read_only": True,
        "auto_adopt": False,
        "hermes_home": str(home),
        "active_hermes_home": str(active_hermes_home()),
        "profile_isolation": {"requested_home": str(home), "active_home": str(active_hermes_home()), "writes_live_profile": False},
        "summary": {
            "skills_count": st.get("skills_count", len(skills)),
            "inventory_skill_count": inv.get("skill_count") or inv.get("total_skills") or len(inv_skills),
            "production_eligible_eval_pack_count": inv.get("production_eligible_count", len(production_ready)),
            "no_eval_pack_count": inv.get("no_pack_count", len(no_pack)),
            "invalid_eval_pack_count": inv.get("invalid_pack_count", len(invalid_pack)),
            "recent_staged_run_count": len(recent_runs),
            "latest_run_id": latest_run_id or None,
            "latest_eval_level": (latest or {}).get("eval_level"),
            "latest_evidence_maturity": (latest or {}).get("evidence_maturity"),
            "artifact_hygiene_attention": hygiene_attention,
            "pinned_or_archived_skipped_by_default": skipped_by_default[:20],
        },
        "skills_metadata": metadata_by_name,
        "eval_pack_inventory": {"schema_version": inv.get("schema_version"), "readiness_matrix": inv.get("readiness_matrix"), "skills": inv_skills[:cap], "error": inv.get("error")},
        "eval_pack_workflow": {"schema_version": workflow.get("schema_version"), "workflow": (workflow.get("workflow") or [])[:cap], "webui_production_one_click": workflow.get("webui_production_one_click"), "error": workflow.get("error")},
        "skill_readiness_queue": {"schema_version": readiness_queue.get("schema_version"), "queue": (readiness_queue.get("queue") or [])[:cap], "error": readiness_queue.get("error")},
        "recent_runs": recent_runs[:cap],
        "evidence_ledger": (latest or {}).get("evidence_ledger") if latest else _eval_evidence_ledger({}),
        "artifact_hygiene": {"schema_version": hygiene.get("schema_version"), "classification_counts": hygiene_counts, "runs": (hygiene.get("runs") or [])[:cap]},
        "next_actions": next_actions[:6],
        "safe_next_commands": safe_commands,
        "cron_recommendation": {"create_cron_job": False, "auto_adopt_from_cron": False, "suggested_read_only_command": f"hermes-skillopt --home {command_home} scout{skill_suffix}", "allowed_cron_surfaces": ["scout", "doctor", "eval-pack-inventory", "eval-pack-doctor", "review --digest"], "manual_read_only_surfaces": ["eval-pack-workflow", "skill-readiness-queue"], "note": "If scheduled externally, run only scout/doctor/eval-pack-inventory/eval-pack-doctor/review --digest and route JSON to notifications; keep workflow/queue surfaces manual unless they expose explicit digest-only modes; never schedule status/full-run/optimize/adopt/rollback/writeback without human review."},
        "native_hermes_boundary": "Native Hermes metadata is read-only advisory evidence. SkillOpt does not replace Hermes curator lifecycle/archive/consolidation; it produces staged eval evidence and adoption recommendations only.",
        "read_only_guards": ["does not call full_run", "does not call optimize/guided_optimize", "does not adopt", "does not rollback", "does not fetch/update upstream", "does not write live skills/config/cron/memories", "does not archive/consolidate/replace Hermes curator lifecycle ownership"],
        "report_path": None,
    }
    if output_path:
        from hermes_skillopt.safety import guard_safe_output_path

        report_path = guard_safe_output_path(output_path, kind="scout report", hermes_home=home, required_suffix=".json")
        out["report_path"] = str(report_path)
        write_text(report_path, json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return out


def guided_optimize(intent: str = "review", **kwargs: Any) -> dict[str, Any]:
    """Intent-presets wrapper around full_run; always staged-only and never auto-adopts."""

    intent = (intent or "review").strip().lower()
    if intent not in {"smoke", "review", "production"}:
        raise ValueError("intent must be one of: smoke, review, production")
    params = dict(kwargs)
    params["auto_adopt"] = False
    params["force"] = False
    if intent == "smoke":
        params["backend"] = params.get("backend") or "mock"
        params["optimizer_backend"] = params.get("optimizer_backend") or "mock"
        params["allow_mock"] = True
        params["gate_mode"] = params.get("gate_mode") or "soft"
        params["limit"] = int(params.get("limit") or 10)
        params["iterations"] = int(params.get("iterations") or 1)
        params["candidate_count"] = int(params.get("candidate_count") or 1)
    elif intent == "review":
        params["backend"] = params.get("backend") or "auto"
        params["optimizer_backend"] = params.get("optimizer_backend")
        params["allow_mock"] = bool(params.get("allow_mock", True))
        params["gate_mode"] = params.get("gate_mode") or "soft"
    else:
        eval_file = params.get("eval_file")
        if not eval_file:
            raise ValueError("production intent requires explicit --eval-file; use review/smoke for discovery")
        if params.get("allow_mock"):
            raise ValueError("production intent requires --allow-mock to be false")
        if (params.get("backend") or "auto") == "mock" or (params.get("optimizer_backend") or "") == "mock":
            raise ValueError("production intent requires a non-mock optimizer backend")
        if (params.get("gate_mode") or "strict") != "strict":
            raise ValueError("production intent requires --gate-mode strict")
        home = hermes_home(params.get("hermes_home_path"))
        candidate = Path(str(eval_file)).expanduser()
        if not candidate.is_absolute():
            candidate = home / candidate
        if not candidate.is_file():
            raise ValueError(f"production intent eval_file does not exist: {candidate}")
        params["backend"] = params.get("backend") or "auto"
        params["optimizer_backend"] = params.get("optimizer_backend")
        params["allow_mock"] = False
        params["gate_mode"] = "strict"
    out = full_run(**params)
    out["intent"] = intent
    out["auto_adopt"] = False
    out["guided_behavior"] = "staged_only_explicit_review_required" if intent != "production" else "staged_only_no_auto_adopt_production_gates_required"
    if intent in {"smoke", "review"}:
        out["review_only_label"] = "review-only intent; do not treat as production adoption evidence"
    return out


def _fleet_manifest_row(home: Path, run_dir: Path, manifest: dict[str, Any], *, warnings: list[str] | None = None, verified: bool = False) -> dict[str, Any]:
    raw_gate_policy = manifest.get("gate_policy")
    gate_policy: dict[str, Any] = raw_gate_policy if isinstance(raw_gate_policy, dict) else {}
    raw_provenance = manifest.get("provenance_fingerprint")
    provenance: dict[str, Any] = raw_provenance if isinstance(raw_provenance, dict) else {}
    raw_ledger = manifest.get("evidence_ledger")
    evidence_ledger: dict[str, Any] = raw_ledger if isinstance(raw_ledger, dict) else _eval_evidence_ledger(manifest)
    production_ready = manifest.get("adoptable") is True and manifest.get("production_gate_eligible") is True and manifest.get("test_gate_eligible") is True and str(gate_policy.get("mode") or "").lower() == "strict" and evidence_ledger.get("production_runtime_ready") is True
    backup_dir = manifest.get("backup_dir")
    rollback_available = False
    rollback_reason = "run is not adopted"
    backup_status: dict[str, Any] = {"present": False, "manifest_present": False, "skill_sha256": None, "manifest_sha256": None, "verified": False}
    current_sha_status: dict[str, Any] = {"checked": False, "skill_path": None, "current_sha256": None, "expected_adopted_sha256": manifest.get("adopted_sha256") or manifest.get("proposed_sha256"), "matches_adopted": None, "reason": "run is not adopted"}
    if manifest.get("status") == "adopted":
        backup_path = Path(str(backup_dir)) if backup_dir else None
        backup_skill = backup_path / "SKILL.md" if backup_path else None
        backup_manifest = backup_path / "manifest.json" if backup_path else None
        if backup_skill and backup_manifest:
            backup_status.update({"present": backup_skill.is_file(), "manifest_present": backup_manifest.is_file()})
            if backup_skill.is_file() and not backup_skill.is_symlink():
                backup_status["skill_sha256"] = sha256_file(backup_skill)
            if backup_manifest.is_file() and not backup_manifest.is_symlink():
                try:
                    backup_raw = json.loads(read_text(backup_manifest))
                    backup_status["manifest_sha256"] = backup_raw.get("sha256") or backup_raw.get("original_sha256")
                    backup_status["verified"] = bool(backup_status["skill_sha256"] and backup_status["manifest_sha256"] == backup_status["skill_sha256"])
                except Exception as exc:
                    warnings = [*(warnings or []), f"backup manifest unreadable: {type(exc).__name__}: {exc}"]
        try:
            target = manifest_skill_path(home, manifest)
            guard_manifest_skill_path(home, manifest, target)
            current_sha_status.update({"checked": True, "skill_path": str(target), "reason": None})
            if target.exists() and target.is_file() and not target.is_symlink():
                current_sha = sha256_file(target)
                current_sha_status["current_sha256"] = current_sha
                current_sha_status["matches_adopted"] = bool(current_sha_status["expected_adopted_sha256"] and current_sha == current_sha_status["expected_adopted_sha256"])
            else:
                current_sha_status["reason"] = "current skill missing or unsafe"
        except Exception as exc:
            current_sha_status["reason"] = f"current skill check unavailable: {type(exc).__name__}: {exc}"
        if backup_dir and backup_status["present"] and backup_status["manifest_present"]:
            rollback_available = True
            rollback_reason = "adopted run has backup SKILL.md and backup manifest; use per-run rollback command only"
        else:
            rollback_reason = "adopted run lacks safely inferable backup artifacts"
    skill_type: dict[str, Any] = {"category": "unknown", "confidence": "none", "advisory_only": True, "hard_gate": False, "reason": "live skill unavailable for advisory classification"}
    try:
        target_path = manifest_skill_path(home, manifest)
        if target_path.exists() and target_path.is_file() and not target_path.is_symlink():
            from hermes_skillopt.skill_types import classify_skill_type

            text = read_text(target_path)
            skill_type = classify_skill_type(Skill(name=str(manifest.get("skill_name") or target_path.parent.name), path=target_path, relpath=str(target_path.resolve().relative_to(home.resolve())), sha256=sha256_text(text)), text=text)
    except Exception as exc:
        skill_type["reason"] = f"classification unavailable: {type(exc).__name__}: {exc}"
    cp_path = run_dir / "checkpoint.json"
    checkpoint_status = None
    checkpoint_input_sha256 = None
    resume_safe = False
    if cp_path.is_file():
        try:
            cp = json.loads(read_text(cp_path))
            checkpoint_status = cp.get("status")
            checkpoint_input_sha256 = cp.get("input_sha256")
            resume_safe = checkpoint_status == "complete" and verified
        except Exception as exc:
            warnings = [*(warnings or []), f"checkpoint unreadable: {type(exc).__name__}: {exc}"]
    return {
        "run_id": manifest.get("run_id") or run_dir.name,
        "run_dir": str(run_dir),
        "kind": "batch_parent" if manifest.get("batch_id") else "single_run",
        "batch_id": manifest.get("batch_id"),
        "skill_name": manifest.get("skill_name"),
        "skill_relpath": manifest.get("skill_relpath"),
        "created_at": manifest.get("created_at") or manifest.get("started_at") or manifest.get("completed_at"),
        "status": manifest.get("status") or ("batch_complete" if manifest.get("batch_id") else None),
        "adoptable": manifest.get("adoptable") is True,
        "production_eligible": production_ready,
        "readiness": {"status": "production_candidate" if production_ready else "review_only_or_not_ready", "adoptable": manifest.get("adoptable") is True, "production_gate_eligible": manifest.get("production_gate_eligible") is True, "test_gate_eligible": manifest.get("test_gate_eligible") is True, "strict_gate_mode": str(gate_policy.get("mode") or "").lower() == "strict" or manifest.get("strict_gate_mode") is True, "eval_level": evidence_ledger.get("eval_level"), "evidence_maturity": evidence_ledger.get("evidence_maturity"), "reasons": manifest.get("production_eligibility_reasons") or []},
        "eval_level": evidence_ledger.get("eval_level"),
        "evidence_maturity": evidence_ledger.get("evidence_maturity"),
        "evidence_ledger": evidence_ledger,
        "skill_type": skill_type,
        "evidence_contract": {"target_execution": manifest.get("target_execution_evidence") if isinstance(manifest.get("target_execution_evidence"), dict) else {"complete": False, "classification": None}, "reviewer_gate": manifest.get("reviewer_gate") if isinstance(manifest.get("reviewer_gate"), dict) else {"passed": False}, "eval_pack_governance": manifest.get("eval_pack_governance") or {}, "production_eval_policy_version": (manifest.get("production_eval_policy") or {}).get("policy_version") if isinstance(manifest.get("production_eval_policy"), dict) else None, "manifest_artifacts_verified": verified},
        "test_eligible": manifest.get("test_gate_eligible") is True,
        "strict_gate_mode": str(gate_policy.get("mode") or "").lower() == "strict" or manifest.get("strict_gate_mode") is True,
        "gate_mode": gate_policy.get("mode"),
        "optimizer_backend": manifest.get("optimizer_backend") or manifest.get("backend"),
        "target_executor": manifest.get("target_executor") or manifest.get("target_backend"),
        "split_scores": manifest.get("split_scores") or {},
        "score_summary": {"validation_current": manifest.get("validation_current_score"), "validation_candidate": manifest.get("validation_candidate_score"), "production_validation_current": manifest.get("production_validation_current_score"), "production_validation_candidate": manifest.get("production_validation_candidate_score"), "test": manifest.get("test_score")},
        "score_provenance": _score_provenance_summary(manifest),
        "not_adoptable_reasons": manifest.get("production_eligibility_reasons") or [],
        "resume": {"checkpoint_present": cp_path.is_file(), "checkpoint_status": checkpoint_status, "checkpoint_input_sha256": checkpoint_input_sha256, "safe_completed_reuse": resume_safe, "partial_continuation_available": False},
        "rollback": {"available": rollback_available, "reason": rollback_reason, "backup_dir": backup_dir, "backup_status": backup_status, "current_sha_status": current_sha_status},
        "artifact_lineage": manifest.get("artifact_lineage") or _artifact_lineage_status(run_dir, manifest),
        "fingerprints": {"provenance": provenance.get("fingerprint_sha256"), "eval": provenance.get("eval_fingerprint_sha256") or provenance.get("eval_file_sha256"), "task": provenance.get("task_sha256"), "optimizer": provenance.get("optimizer_fingerprint_sha256"), "target": provenance.get("target_fingerprint_sha256"), "profile": provenance.get("profile_fingerprint_sha256"), "manifest_artifacts_verified": verified},
        "warnings": sorted(set(warnings or [])),
    }


def _fleet_checkpoint_row(run_dir: Path, checkpoint: dict[str, Any] | None, warnings: list[str]) -> dict[str, Any]:
    cp = checkpoint or {}
    lineage = _artifact_lineage_status(run_dir, {}, cp)
    reasons = []
    if cp.get("status") != "complete":
        reasons.append("run is incomplete; partial-stage continuation is refused because replay could skip gates/adopt checks")
    reasons.append("completed-run reuse unavailable until a manifest exists and artifact hashes verify")
    classification = "checkpoint_only_recent" if cp.get("status") == "complete" else "stale_or_incomplete_checkpoint_only"
    next_safe_action = "Inspect checkpoint artifacts only; retry as a new full run if abandoned. Do not resume partial stages, adopt, or delete automatically."
    return {"run_id": run_dir.name, "run_dir": str(run_dir), "kind": "checkpoint_only", "artifact_classification": classification, "skill_name": lineage.get("skill_name"), "skill_relpath": lineage.get("skill_relpath"), "status": cp.get("status") or "checkpoint_only", "adoptable": False, "production_eligible": False, "test_eligible": False, "eval_level": "review_only", "evidence_maturity": "review_only_static_replay_or_incomplete_runtime", "evidence_ledger": _eval_evidence_ledger({}), "split_scores": {}, "resume": {"checkpoint_present": True, "checkpoint_status": cp.get("status"), "checkpoint_input_sha256": cp.get("input_sha256"), "safe_completed_reuse": False, "partial_continuation_available": False}, "rollback": {"available": False, "reason": "checkpoint-only run has no adopted manifest", "backup_dir": None}, "artifact_lineage": lineage, "fingerprints": {"manifest_artifacts_verified": False}, "partial_continuation_available": False, "next_safe_action": next_safe_action, "refusal_reasons": sorted(set(reasons)), "cleanup_guidance": ["No automatic cleanup is performed. Inspect run_dir, ensure no process is writing it, then remove or retry with a new full-run if desired."], "warnings": sorted(set(warnings))}


def _scan_fleet_runs(home: Path, limit: int = 50) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    dirs = skillopt_paths(home)
    cap = max(1, min(int(limit or 50), 200))
    rows: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    warnings: list[str] = []
    run_dirs = sorted([p for p in dirs["staging"].iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True) if dirs["staging"].exists() else []
    for run_dir in run_dirs[:cap]:
        manifest_path = run_dir / "manifest.json"
        checkpoint_path = run_dir / "checkpoint.json"
        if manifest_path.is_file():
            try:
                manifest = load_manifest(run_dir)
                verify_artifact_hashes(run_dir, manifest)
                rows.append(_fleet_manifest_row(home, run_dir, manifest, verified=True))
            except Exception as exc:
                warning = f"manifest missing/tampered/unverified for {run_dir.name}: {type(exc).__name__}: {exc}"
                warnings.append(warning)
                try:
                    raw = json.loads(read_text(manifest_path))
                except Exception:
                    raw = {"run_id": run_dir.name, "status": "manifest_unreadable"}
                rows.append(_fleet_manifest_row(home, run_dir, raw, warnings=[warning], verified=False))
            continue
        if checkpoint_path.is_file():
            cp_warnings: list[str] = []
            try:
                cp = json.loads(read_text(checkpoint_path))
            except Exception as exc:
                cp = None
                cp_warnings.append(f"checkpoint unreadable: {type(exc).__name__}: {exc}")
            row = _fleet_checkpoint_row(run_dir, cp, cp_warnings)
            rows.append(row)
            incomplete.append(row)
    return rows, incomplete, warnings


def _dir_age_seconds(path: Path) -> float:
    try:
        return max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)
    except Exception:
        return 0.0


def _hygiene_row(run_dir: Path, *, stale_after_hours: float) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    checkpoint_path = run_dir / "checkpoint.json"
    age_seconds = _dir_age_seconds(run_dir)
    reasons: list[str] = []
    safe_guidance = [
        "Read-only report only: no files are deleted or modified.",
        "Before manual cleanup, inspect run_dir and confirm no SkillOpt process is writing it.",
        "Prefer retaining complete_verified/adopted artifacts until external retention policy says otherwise.",
    ]
    next_safe_action = "Inspect artifacts; no automatic cleanup or adoption action is taken."
    partial_continuation_available = False
    score_provenance: dict[str, Any] | None = None
    classification = "missing_manifest_or_checkpoint"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = load_manifest(run_dir)
            verify_artifact_hashes(run_dir, manifest)
            classification = "complete_verified"
            score_provenance = _score_provenance_summary(manifest)
            next_safe_action = "Keep for audit/review; if adoptable, use explicit review/adopt flow only. Do not delete automatically."
            if manifest.get("batch_id"):
                jobs_path = run_dir / "jobs.json"
                try:
                    jobs = json.loads(read_text(jobs_path)) if jobs_path.is_file() else []
                except Exception as exc:
                    jobs = []
                    reasons.append(f"batch jobs unreadable: {type(exc).__name__}: {exc}")
                child_missing = [str(j.get("run_id")) for j in jobs if isinstance(j, dict) and j.get("run_id") and not (run_dir.parent / str(j.get("run_id"))).is_dir()]
                if child_missing:
                    classification = "orphaned_batch_child"
                    reasons.append("batch parent references missing child run dirs: " + ", ".join(child_missing))
                    next_safe_action = "Inspect batch jobs.json and child run dirs; retry missing children as new runs if needed. Do not delete or adopt automatically."
        except Exception as exc:
            classification = "tampered_hash_mismatch"
            next_safe_action = "Do not adopt or reuse. Inspect manifest/artifact hashes; rerun optimize to produce a fresh staged run. Do not delete automatically."
            reasons.append(f"manifest missing/hash mismatch/unverified: {type(exc).__name__}: {redact_secrets(str(exc))}")
            try:
                manifest = json.loads(read_text(manifest_path))
            except Exception:
                manifest = {}
    elif checkpoint_path.is_file():
        try:
            cp = json.loads(read_text(checkpoint_path))
        except Exception as exc:
            cp = {}
            reasons.append(f"checkpoint unreadable: {type(exc).__name__}: {exc}")
        status = cp.get("status")
        stale = age_seconds >= stale_after_hours * 3600.0
        classification = "checkpoint_only_recent" if not stale else "stale_incomplete_checkpoint_only"
        if status == "complete":
            classification = "checkpoint_only_recent" if not stale else "stale_checkpoint_only"
            reasons.append("checkpoint status is complete but manifest is missing; completed-run reuse unavailable")
            next_safe_action = "Inspect checkpoint/report; rerun or recover manifest only from trusted backup. Do not resume partial stages or adopt."
        elif stale:
            reasons.append(f"checkpoint-only/incomplete directory is older than stale_after_hours={stale_after_hours:g}")
            next_safe_action = "Treat as abandoned unless a writer is active; retry as a new full run or manually remove only after inspection."
        else:
            reasons.append("recent checkpoint-only/incomplete directory; leave in place unless confirmed abandoned")
            next_safe_action = "Leave in place and check whether a SkillOpt process is still writing; do not resume partial stages or adopt."
        raw_input = cp.get("input")
        input_obj: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        manifest = {"status": status, "skill_name": input_obj.get("skill_name")}
    else:
        reasons.append("directory has neither manifest.json nor checkpoint.json")
        if age_seconds >= stale_after_hours * 3600.0:
            classification = "stale_incomplete_checkpoint_only"
            next_safe_action = "Treat as abandoned unless a writer is active; retry as a new full run or manually remove only after inspection."
    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "classification": classification,
        "age_seconds": round(age_seconds, 3),
        "age_hours": round(age_seconds / 3600.0, 3),
        "status": manifest.get("status"),
        "kind": "batch_parent" if manifest.get("batch_id") else "single_or_checkpoint",
        "skill_name": manifest.get("skill_name"),
        "manifest_present": manifest_path.is_file(),
        "checkpoint_present": checkpoint_path.is_file(),
        "artifact_state": _slim_artifact_state(run_dir),
        "score_provenance": score_provenance,
        "partial_continuation_available": partial_continuation_available,
        "next_safe_action": next_safe_action,
        "reasons": sorted(set(reasons)),
        "safe_manual_cleanup_guidance": safe_guidance,
    }


def artifact_hygiene_report(hermes_home_path: str | None = None, *, limit: int = 200, stale_after_hours: float = 24.0) -> dict[str, Any]:
    """Read-only artifact hygiene planner for staging run dirs; never deletes."""
    home = hermes_home(hermes_home_path)
    staging = skillopt_paths(home)["staging"]
    cap = max(1, min(int(limit or 200), 500))
    run_dirs = sorted([p for p in staging.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True) if staging.exists() else []
    rows = [_hygiene_row(p, stale_after_hours=stale_after_hours) for p in run_dirs[:cap]]
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["classification"])] = counts.get(str(row["classification"]), 0) + 1
    return {
        "success": True,
        "schema_version": "hermes-skillopt-artifact-hygiene-v1",
        "mode": "read_only_artifact_hygiene_report_no_delete_no_writeback",
        "hermes_home": str(home),
        "staging": str(staging),
        "limit": cap,
        "stale_after_hours": stale_after_hours,
        "run_count": len(rows),
        "classification_counts": counts,
        "runs": rows,
        "safe_manual_cleanup_guidance": ["This report is a planner only and performs no deletion.", "Remove only stale_incomplete/checkpoint-only/orphaned artifacts after manual inspection and after confirming no active writer.", "Do not auto-adopt or infer production readiness from hygiene status."],
        "read_only_guards": ["does not delete", "does not call full_run", "does not adopt", "does not rollback", "does not write skills or staging artifacts"],
    }


def fleet_report(hermes_home_path: str | None = None, *, limit: int = 50, skill: str | None = None) -> dict[str, Any]:
    """Read-only fleet report over recent single and batch SkillOpt runs."""
    home = hermes_home(hermes_home_path)
    rows, incomplete, warnings = _scan_fleet_runs(home, limit)
    if skill:
        rows = [r for r in rows if r.get("skill_name") == skill]
        incomplete = [r for r in incomplete if r.get("skill_name") == skill]
    by_skill: dict[str, dict[str, Any]] = {}
    grouped: dict[str, dict[str, Any]] = {"by_skill": by_skill, "by_skill_type": {}, "by_readiness": {}, "by_adoptability": {}, "by_rollbackability": {}}
    batch_children: dict[str, list[str]] = {}

    def add_group(group_name: str, key: str, row: dict[str, Any]) -> None:
        group = grouped[group_name].setdefault(key, {"key": key, "run_count": 0, "run_ids": []})
        group["run_count"] += 1
        group["run_ids"].append(row.get("run_id"))

    for row in rows:
        if row.get("kind") == "batch_parent":
            try:
                jobs = json.loads(read_text(Path(row["run_dir"]) / "jobs.json")) if (Path(row["run_dir"]) / "jobs.json").is_file() else []
                batch_children[str(row.get("batch_id") or row.get("run_id"))] = [str(j.get("run_id")) for j in jobs if isinstance(j, dict) and j.get("run_id")]
            except Exception as exc:
                row.setdefault("warnings", []).append(f"batch jobs unreadable: {type(exc).__name__}: {exc}")
        sk = row.get("skill_name") or "<unknown>"
        bucket = by_skill.setdefault(sk, {"skill_name": sk, "runs": [], "latest_run": None, "adoptable_count": 0, "rollbackable_count": 0, "incomplete_count": 0, "readiness_counts": {}, "skill_type_counts": {}})
        bucket["runs"].append(row)
        if bucket["latest_run"] is None:
            bucket["latest_run"] = row
        if row.get("adoptable"):
            bucket["adoptable_count"] += 1
        if (row.get("rollback") or {}).get("available"):
            bucket["rollbackable_count"] += 1
        if row.get("kind") == "checkpoint_only" or (row.get("resume") or {}).get("checkpoint_status") not in (None, "complete"):
            bucket["incomplete_count"] += 1
        readiness_key = str((row.get("readiness") or {}).get("status") or ("production_candidate" if row.get("production_eligible") else "review_only_or_not_ready"))
        type_key = str((row.get("skill_type") or {}).get("category") or "unknown")
        adopt_key = "adoptable" if row.get("adoptable") else "not_adoptable"
        rollback_key = "rollbackable" if (row.get("rollback") or {}).get("available") else "not_rollbackable"
        bucket["readiness_counts"][readiness_key] = bucket["readiness_counts"].get(readiness_key, 0) + 1
        bucket["skill_type_counts"][type_key] = bucket["skill_type_counts"].get(type_key, 0) + 1
        add_group("by_skill_type", type_key, row)
        add_group("by_readiness", readiness_key, row)
        add_group("by_adoptability", adopt_key, row)
        add_group("by_rollbackability", rollback_key, row)
    return {"success": True, "schema_version": "hermes-skillopt-fleet-report-v2", "mode": "read_only_report_no_resume_no_rollback_no_writeback", "hermes_home": str(home), "limit": max(1, min(int(limit or 50), 200)), "run_count": len(rows), "latest_runs": rows, "skills": list(by_skill.values()), "groups": {k: (list(v.values()) if k != "by_skill" else list(by_skill.values())) for k, v in grouped.items()}, "batch_children": batch_children, "incomplete_or_checkpoint_only": incomplete, "warnings": sorted(set(warnings)), "read_only_guards": ["does not call full_run", "does not adopt", "does not rollback", "does not write skills or staging artifacts"]}


def fleet_resume_plan(hermes_home_path: str | None = None, *, limit: int = 50, skill: str | None = None) -> dict[str, Any]:
    """Read-only resume plan. Completed exact-fingerprint runs may be reused; partial continuation is refused."""
    report = fleet_report(hermes_home_path, limit=limit, skill=skill)
    reusable = []
    refused = []
    for row in report["latest_runs"]:
        resume = row.get("resume") or {}
        if resume.get("safe_completed_reuse"):
            reusable.append({"run_id": row.get("run_id"), "skill_name": row.get("skill_name"), "checkpoint_input_sha256": resume.get("checkpoint_input_sha256"), "guidance": "Pass this run_id to --resume-run-id only with identical input/config/provenance; core will re-verify exact fingerprint before reuse."})
        elif resume.get("checkpoint_present"):
            refused.append({"run_id": row.get("run_id"), "skill_name": row.get("skill_name"), "checkpoint_status": resume.get("checkpoint_status"), "partial_continuation_available": False, "refusal_reasons": row.get("refusal_reasons") or ["completed exact-fingerprint reuse is unavailable; partial continuation is refused"], "cleanup_guidance": row.get("cleanup_guidance") or ["Retry with a new full-run. Manually clean abandoned run dirs only after confirming no writer is active."]})
    return {"success": True, "schema_version": "hermes-skillopt-fleet-resume-plan-v1", "mode": "read_only_plan_no_resume_execution", "hermes_home": report["hermes_home"], "completed_exact_fingerprint_reusable": reusable, "refused_incomplete_or_partial": refused, "partial_continuation_available": False, "cleanup_retry_guidance": ["No automatic cleanup or resume is performed.", "Completed-run reuse still requires exact input/config/provenance match enforced by full_run.", "Incomplete partial-stage continuation is refused; retry as a new run or manually remove abandoned artifacts after inspection."], "read_only_guards": report["read_only_guards"]}


def fleet_rollback_plan(hermes_home_path: str | None = None, *, limit: int = 50, skill: str | None = None) -> dict[str, Any]:
    """Read-only rollback plan; actual rollback remains the per-run rollback command."""
    report = fleet_report(hermes_home_path, limit=limit, skill=skill)
    rollbackable = []
    not_rollbackable = []
    for row in report["latest_runs"]:
        rollback = row.get("rollback") or {}
        item = {
            "run_id": row.get("run_id"),
            "skill_name": row.get("skill_name"),
            "skill_type": row.get("skill_type"),
            "readiness": row.get("readiness"),
            "status": row.get("status"),
            "backup_dir": rollback.get("backup_dir"),
            "backup_status": rollback.get("backup_status"),
            "current_sha_status": rollback.get("current_sha_status"),
            "reason": rollback.get("reason"),
        }
        if rollback.get("available"):
            item["one_run_command"] = f"hermes-skillopt rollback {row.get('run_id')}"
            item["command"] = item["one_run_command"]
            item["bulk_safe"] = False
            rollbackable.append(item)
        else:
            not_rollbackable.append(item)
    return {"success": True, "schema_version": "hermes-skillopt-fleet-rollback-plan-v2", "mode": "read_only_plan_no_bulk_rollback_no_writeback", "hermes_home": report["hermes_home"], "rollbackable_adopted_runs": rollbackable, "not_rollbackable_recent_runs": not_rollbackable, "bulk_rollback_available": False, "exact_one_run_command_template": "hermes-skillopt rollback <run_id>", "guidance": "Run the existing per-run rollback command for exactly one reviewed run_id; this plan verifies backup/current-sha status when safely readable and never writes.", "read_only_guards": report["read_only_guards"]}


def review(run_id: str, hermes_home_path: str | None = None, include_diff_chars: int = 4000, slim: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, m)
    diff = read_text(run_dir / "diff.patch") if (run_dir / "diff.patch").exists() else ""
    report = read_text(run_dir / "report.md") if (run_dir / "report.md").exists() else ""
    gate = m.get("gate") or (json.loads(read_text(run_dir / "gate_results.json")).get("best_gate") if (run_dir / "gate_results.json").exists() else None)
    artifact_lineage = m.get("artifact_lineage") or _artifact_lineage_status(run_dir, m)
    native_guard = None
    try:
        target_path = manifest_skill_path(home, m)
        guard_manifest_skill_path(home, m, target_path)
        native_guard = native_adopt_guard(home, Skill(name=str(m.get("skill_name") or target_path.parent.name), path=target_path, relpath=str(m.get("skill_relpath") or target_path.resolve().relative_to(home.resolve())), sha256=""), m)
    except Exception as exc:
        native_guard = {"schema_version": "hermes-native-adopt-guard-v1", "allowed": False, "blockers": [f"native metadata guard unavailable: {type(exc).__name__}: {exc}"], "force_override_allowed": False}
    score_provenance = _score_provenance_summary(m)
    raw_evidence_ledger = m.get("evidence_ledger")
    evidence_ledger: dict[str, Any] = raw_evidence_ledger if isinstance(raw_evidence_ledger, dict) else _eval_evidence_ledger(m)
    report_summary = {"timeline": {"status": m.get("status"), "created_at": m.get("created_at")}, "eligibility": {"adoptable": m.get("adoptable") is True, "reasons": m.get("production_eligibility_reasons") or [], "production_gate_eligible": m.get("production_gate_eligible") is True, "test_gate_eligible": m.get("test_gate_eligible") is True}, "score_provenance": score_provenance, "split_scores": m.get("split_scores") or {}, "candidate_comparison": m.get("candidate_comparison") or [], "regression_cases": m.get("regression_cases") or [], "artifact_lineage": artifact_lineage, "provenance_security": {"artifact_integrity": "verified", "provenance_fingerprint": (m.get("provenance_fingerprint") or {}).get("fingerprint_sha256") if isinstance(m.get("provenance_fingerprint"), dict) else None, "production_eval_policy": (m.get("production_eval_policy") or {}).get("policy_version") if isinstance(m.get("production_eval_policy"), dict) else None, "target_executor": m.get("target_executor"), "gate_policy": m.get("gate_policy")}}
    diff_path = run_dir / "diff.patch"
    report_path = run_dir / "report.md"
    artifact_refs = {
        "diff": {"path": str(diff_path), "sha256": sha256_text(diff) if diff else None, "bytes": len(diff.encode("utf-8")), "preview_chars": 0 if slim else min(len(diff), include_diff_chars)},
        "report": {"path": str(report_path), "sha256": sha256_text(report) if report else None, "bytes": len(report.encode("utf-8")), "preview_chars": 0 if slim else min(len(report), 1200)},
    }
    readiness = readiness_adoptability_schema({**m, "gate": gate})
    return {"success": True, "run_id": run_id, "status": m.get("status"), "adoptable": m.get("adoptable") is True, "production_gate_eligible": m.get("production_gate_eligible") is True, "test_gate_eligible": m.get("test_gate_eligible") is True, "eval_level": evidence_ledger.get("eval_level"), "evidence_maturity": evidence_ledger.get("evidence_maturity"), "evidence_ledger": evidence_ledger, "target_execution_evidence": m.get("target_execution_evidence"), "reviewer_gate": m.get("reviewer_gate"), "validation_gate": readiness["validation_gate"], "production_best_gate": readiness["production_best_gate"], "heldout_test_gate": readiness["heldout_test_gate"], "review_only": readiness["review_only"], "blockers": readiness["blockers"], "warnings": readiness["warnings"], "next_safe_action": readiness["next_safe_action"], "readiness_adoptability": readiness, "native_hermes_metadata": m.get("native_hermes_metadata"), "native_hermes_adopt_guard": native_guard, "not_adoptable_reasons": m.get("production_eligibility_reasons") or [], "skill": m.get("skill_name"), "gate": gate, "production_gate": m.get("production_gate"), "test_results": m.get("test_results"), "split_scores": m.get("split_scores") or {}, "per_task_delta": m.get("per_task_delta") or [], "heldout_test_sensitivity": m.get("heldout_test_sensitivity") or {}, "candidate_comparison": m.get("candidate_comparison") or [], "regression_cases": m.get("regression_cases") or [], "candidate_summary": m.get("candidate_summary") or [], "report_fields": report_summary, "optimizer_backend_config": m.get("optimizer_backend_config") or m.get("optimizer_config"), "target_backend_config": m.get("target_backend_config"), "gate_policy": m.get("gate_policy"), "provenance_fingerprint": m.get("provenance_fingerprint"), "production_eval_policy": m.get("production_eval_policy"), "score_provenance": score_provenance, "artifact_lineage": artifact_lineage, "accepted": m.get("status") in ("staged_best", "accepted", "adopted"), "artifact_integrity": "verified", "run_dir": str(run_dir), "diff_path": str(diff_path), "report_path": str(report_path), "artifact_refs": artifact_refs, "slim": bool(slim), "diff_preview": "" if slim else diff[:include_diff_chars], "report_summary": "" if slim else report[:1200]}


def _adopt_unlocked(run_id: str, hermes_home_path: str | None = None, force: bool = False, unsafe_cross_profile: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    guard_writeback_home(home, unsafe_cross_profile=unsafe_cross_profile)
    dirs = ensure_dirs(home)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, m)
    mock_reasons = _mock_provenance_reasons(m)
    if mock_reasons:
        raise ValueError("Mock/non-production optimizer provenance is review-only and cannot be adopted: " + "; ".join(mock_reasons))
    if m.get("status") != "staged_best" or m.get("adoptable") is not True:
        raise ValueError("Only adoptable full-run staged_best manifests may be adopted; legacy/fallback/dry-run proposals are review-only")
    gate_policy = m.get("gate_policy")
    if not isinstance(gate_policy, dict) or str(gate_policy.get("mode") or "").lower() != "strict":
        raise ValueError("Production adoption requires strict gate mode; soft/mixed/hard gate manifests are review-only")
    _adopt_time_artifact_crosscheck(run_dir, m)
    gate = m.get("gate")
    if not isinstance(gate, dict) or gate.get("accepted") is not True:
        raise ValueError("Manifest missing accepted validation gate; refusing to adopt")
    if m.get("production_gate_eligible") is not True:
        raise ValueError("Manifest validation gate is not production eligible; refusing to adopt")
    if m.get("test_gate_eligible") is not True:
        raise ValueError("Manifest held-out test gate is not production eligible; refusing to adopt")
    policy = m.get("production_eval_policy")
    provenance = m.get("provenance_fingerprint")
    if not isinstance(policy, dict) or policy.get("policy_version") != "production-eval-schema-v1":
        raise ValueError("Manifest missing production eval schema policy; refusing to adopt")
    if not isinstance(provenance, dict) or not provenance.get("fingerprint_sha256"):
        raise ValueError("Manifest missing provenance fingerprint; refusing to adopt")
    target = manifest_skill_path(home, m)
    guard_manifest_skill_path(home, m, target)
    target_skill = Skill(name=str(m.get("skill_name") or target.parent.name), path=target, relpath=str(m.get("skill_relpath") or target.resolve().relative_to(home.resolve())), sha256="")
    native_guard = native_adopt_guard(home, target_skill, m)
    if not native_guard.get("allowed"):
        raise ValueError("Native Hermes metadata conflict guard blocked adopt; force=true does not override: " + "; ".join(str(x) for x in (native_guard.get("blockers") or [])))
    m["native_hermes_adopt_guard_at_adopt"] = native_guard
    if not target.exists():
        raise ValueError(f"target skill missing: {target}")
    current = read_text(target)
    current_sha = sha256_text(current)
    if current_sha != m.get("original_sha256") and not force:
        raise ValueError("Current skill sha does not match staged original; pass force=true to override")
    proposed = read_text(run_dir / "proposed_SKILL.md")
    expected_proposed_sha = m.get("proposed_sha256")
    if not expected_proposed_sha:
        raise ValueError("Manifest missing proposed_sha256; refusing to adopt")
    actual_proposed_sha = sha256_text(proposed)
    if actual_proposed_sha != expected_proposed_sha:
        raise ValueError("Staged proposed_SKILL.md sha does not match manifest proposed_sha256; refusing to adopt")
    backup_dir = dirs["backups"] / f"{now_id()}-{run_id}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    proposed_sha = sha256_text(proposed)
    write_text(backup_dir / "SKILL.md", current)
    write_text(backup_dir / "manifest.json", json.dumps({"run_id": run_id, "skill_path": str(target), "skill_relpath": m.get("skill_relpath"), "sha256": current_sha, "original_sha256": current_sha, "proposed_sha256": proposed_sha, "adopted_sha256": proposed_sha}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_text(target, proposed)
    readback = _post_write_readback_verification(target, expected_sha256=proposed_sha, expected_skill_name=m.get("skill_name"))
    m.update({"status": "adopted", "adopted_at": datetime.now(timezone.utc).isoformat(), "backup_dir": str(backup_dir), "original_sha256": current_sha, "adopted_sha256": proposed_sha, "post_write_readback": readback})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "adopted", "skill_path": str(target), "backup_dir": str(backup_dir), "post_write_readback": readback}


def _rollback_unlocked(run_id: str, hermes_home_path: str | None = None, force: bool = False, unsafe_cross_profile: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    guard_writeback_home(home, unsafe_cross_profile=unsafe_cross_profile)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, m)
    target = manifest_skill_path(home, m)
    guard_manifest_skill_path(home, m, target)
    expected_sha = m.get("adopted_sha256") or m.get("proposed_sha256")
    if not force:
        if not expected_sha:
            raise ValueError("No adopted sha available for rollback guard; pass force=true to override")
        if not target.exists():
            raise ValueError("Current skill missing; pass force=true to rollback")
        current_sha = sha256_text(read_text(target))
        if current_sha != expected_sha:
            raise ValueError("Current skill sha does not match adopted state; pass force=true to rollback")
    backup_skill = resolve_backup_skill_path(home, m, run_id, target)
    restored = read_text(backup_skill)
    restored_sha = sha256_text(restored)
    write_text(target, restored)
    readback = _post_write_readback_verification(target, expected_sha256=restored_sha, expected_skill_name=m.get("skill_name"))
    m.update({"status": "rolled_back", "rolled_back_at": datetime.now(timezone.utc).isoformat(), "rolled_back_sha256": restored_sha, "post_write_readback": readback})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "rolled_back", "skill_path": str(target), "post_write_readback": readback}



def _writeback_lock_file(home: Path) -> Path:
    return ensure_dirs(home)["base"] / "writeback.lock"


def _writeback_audit_file(home: Path) -> Path:
    return ensure_dirs(home)["base"] / "writeback_audit.jsonl"


def _audit_writeback_event(home: Path, action: str, run_id: str, outcome: str, details: dict[str, Any] | None = None) -> None:
    """Append a best-effort adopt/rollback audit event under the active profile."""

    row = {
        "schema_version": "hermes-skillopt-writeback-audit-v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "run_id": run_id,
        "outcome": outcome,
        "hermes_home": str(home),
        "details": details or {},
    }
    path = _writeback_audit_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _with_writeback_lock(home: Path, action: str, run_id: str, fn):
    """Serialize adopt/rollback writeback and audit attempts/results."""

    lock = _writeback_lock_file(home)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        _audit_writeback_event(home, action, run_id, "blocked_lock_busy", {"lock_path": str(lock)})
        raise RuntimeError(f"Another adopt/rollback writeback is in progress: {lock}") from exc
    try:
        os.write(fd, json.dumps({"action": action, "run_id": run_id, "pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}, sort_keys=True).encode("utf-8"))
        os.close(fd)
        _audit_writeback_event(home, action, run_id, "attempt", {"lock_path": str(lock)})
        try:
            result = fn()
        except Exception as exc:
            _audit_writeback_event(home, action, run_id, "error", {"error_type": type(exc).__name__, "error": redact_secrets(str(exc))})
            raise
        _audit_writeback_event(home, action, run_id, "success", {k: v for k, v in result.items() if k in {"status", "skill_path", "backup_dir", "post_write_readback"}} if isinstance(result, dict) else {})
        return result
    finally:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


def adopt(run_id: str, hermes_home_path: str | None = None, force: bool = False, unsafe_cross_profile: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    guard_writeback_home(home, unsafe_cross_profile=unsafe_cross_profile)
    return _with_writeback_lock(home, "adopt", run_id, lambda: _adopt_unlocked(run_id, hermes_home_path, force, unsafe_cross_profile))


def rollback(run_id: str, hermes_home_path: str | None = None, force: bool = False, unsafe_cross_profile: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    guard_writeback_home(home, unsafe_cross_profile=unsafe_cross_profile)
    return _with_writeback_lock(home, "rollback", run_id, lambda: _rollback_unlocked(run_id, hermes_home_path, force, unsafe_cross_profile))


def compare_upstream_pin(hermes_home_path: str | None = None) -> dict[str, Any]:
    """Read-only pinned-upstream comparison; never fetches, merges, or writes."""

    status = upstream_status(hermes_home_path)
    diff = status.get("upstream_diff") or {}
    lock = status.get("lock") or {}
    pinned = lock.get("pinned_commit")
    reviewed = lock.get("last_reviewed_upstream_commit") or pinned
    current = status.get("current_commit")
    return {
        "success": True,
        "mode": "read_only_report_only_no_fetch_no_merge",
        "parity_label": "Upstream pin status only; no Microsoft SkillOpt upstream benchmark parity/result is claimed",
        "true_upstream_execution_supported": False,
        "upstream_url": status.get("upstream_url"),
        "clone_path": status.get("clone_path"),
        "clone_exists": status.get("clone_exists"),
        "pinned_commit": pinned,
        "last_reviewed_upstream_commit": reviewed,
        "current_commit": current,
        "pin_matches_clone": bool(pinned and current and pinned == current),
        "semantic_status": diff.get("semantic_status"),
        "ahead": diff.get("ahead"),
        "behind": diff.get("behind"),
        "dirty": diff.get("dirty"),
        "feature_matrix": status.get("feature_matrix") or [],
        "safety_invariants": [
            "standalone Hermes adapter; upstream code is not vendored or merged automatically",
            "status uses locally fetched origin/main only and performs no network access",
            "benchmark/parity modes are report-only unless explicitly staged as artifacts",
            "no upstream benchmark code is executed by compare/status reporting",
        ],
    }


def benchmark_parity_status(hermes_home_path: str | None = None) -> dict[str, Any]:
    """Report Hermes benchmark coverage versus upstream parity without executing benchmarks."""

    from hermes_skillopt.env import built_in_benchmarks, EVAL_PACK_SCHEMA_VERSION, EXPLICIT_CURATED_EVAL_PACK_CONTRACT, EVAL_CONTRACT_CLASSIFICATIONS, REAL_TARGET_REQUIRED_EVIDENCE

    upstream = compare_upstream_pin(hermes_home_path)
    catalog = built_in_benchmarks()
    parity_manifest_schema = {
        "schema_version": "hermes-upstream-parity-pack-manifest-v1",
        "safe_inputs": ["local JSON manifest", "canonical pinned upstream JSON manifest", "Hermes eval pack JSON"],
        "forbidden": ["import upstream Python", "execute benchmark commands", "network fetch", "write live skills", "remote URLs", "path references outside canonical clone/guarded eval output"],
        "adapter_levels": {
            "none": {"supported": True, "full_parity_claim": False, "description": "no adapter evidence"},
            "json_import_only": {"supported": True, "full_parity_claim": False, "description": "JSON-only manifest conversion to Hermes eval pack"},
            "pinned_manifest_replay": {"supported": True, "full_parity_claim": False, "description": "data-only manifest under canonical pinned clone converted to Hermes eval pack with provenance"},
            "pinned_upstream_execution": {"supported": False, "full_parity_claim": False, "description": "unsupported: upstream execution is intentionally not run by this adapter"},
            "parity_evidence_complete": {"supported": False, "full_parity_claim": False, "description": "unsupported until equivalent pinned upstream benchmark execution evidence exists"},
        },
    }
    builtin = {
        bid: {
            "name": b.name,
            "origin": b.origin,
            "production_eligible": b.production_eligible,
            "split_counts": {split: sum(1 for t in b.tasks if t.split == split) for split in ("train", "val", "test")},
        }
        for bid, b in catalog.items()
    }
    return {
        "success": True,
        "schema_version": "hermes-skillopt-benchmark-parity-status-v1",
        "mode": "read_only_report_only_no_rollout_no_adopt",
        "parity_label": "Hermes-native benchmark mode; not an upstream SkillOpt benchmark result",
        "full_parity_claim": False,
        "adapter_levels": parity_manifest_schema["adapter_levels"],
        "evidence_files": {
            "json_import_only": {"generated_by": "import-upstream-benchmark", "evidence_type": "Hermes eval pack JSON with upstream_bridge provenance"},
            "pinned_manifest_replay": {"generated_by": "import-upstream-benchmark --from-pinned-manifest", "evidence_type": "Hermes eval pack JSON with pinned commit, manifest sha, conversion sha, unsupported fields"},
            "pinned_upstream_execution": None,
            "parity_evidence_complete": None,
        },
        "reporting_boundary": "no Microsoft SkillOpt upstream benchmark parity is claimed; status/import are conservative Hermes-native report-only capabilities",
        "upstream_parity_claim": "no full upstream parity claimed; compare-upstream-pin reports pinned source status only",
        "supported_parity_levels": {
            "pinned_upstream_status": "supported_read_only_no_fetch",
            "json_import_only": "supported_read_only_conversion_no_code_execution",
            "json_import_only_bridge": "supported_read_only_conversion_no_code_execution",
            "pinned_manifest_replay": "supported_data_only_canonical_clone_manifest_conversion_no_code_execution",
            "hermes_eval_pack_replay": "supported_native_not_upstream_parity",
        },
        "unsupported_parity_levels": {
            "pinned_upstream_execution": "unsupported_no_arbitrary_upstream_code_execution",
            "true_upstream_benchmark_execution": "unsupported_no_adapter_no_arbitrary_code_execution",
            "parity_evidence_complete": "unsupported_no_equivalent_pinned_upstream_execution_evidence",
            "networked_upstream_fetch_during_status": "unsupported_status_is_offline_only",
            "upstream_result_equivalence_claim": "unsupported_no_claim_until adapters compare against pinned upstream outputs",
        },
        "upstream_benchmark_parity": {
            "upstream_pin": {k: upstream.get(k) for k in ("clone_exists", "pinned_commit", "current_commit", "semantic_status", "ahead", "behind", "dirty")},
            "local_clone_status": "available" if upstream.get("clone_exists") else "not_cloned",
            "import_only_bridge": {"supported": True, "adapter_level": "json_import_only", "command": "import-upstream-benchmark", "schema_version": "hermes-upstream-benchmark-bridge-v1", "safe_read_only": True, "full_parity_claim": False, "parity_label": "JSON/data-only import to Hermes eval pack; not upstream execution parity"},
            "pinned_manifest_replay": {"supported": True, "adapter_level": "pinned_manifest_replay", "command": "import-upstream-benchmark --from-pinned-manifest", "requires": "manifest JSON under canonical pinned clone only", "safe_read_only": True, "full_parity_claim": False, "parity_label": "Data-only pinned manifest conversion; adapter-level evidence, not upstream execution parity"},
            "true_benchmark_execution": {"supported": False, "adapter_level": "pinned_upstream_execution", "full_parity_claim": False, "reason": "no upstream execution adapter; arbitrary code/network execution remains disabled"},
            "required_next_adapter_steps": [
                "define a read-only upstream benchmark manifest/adapter schema with pinned expected outputs",
                "bind adapter to pinned upstream commit and local clone fingerprint",
                "map outputs into eval_execution_contract=frozen_hermes_target_execution_v1 with required execution evidence",
                "add conformance tests proving no code/network/task-command execution during status/import",
            ],
            "parity_pack_manifest_schema": parity_manifest_schema,
        },
        "hermes_benchmark_mode": {
            "default": "deterministic Hermes eval packs / replay / scorecard",
            "production_gate": "only explicit curated val/test eval packs can gate adoption",
            "schema_version": EVAL_PACK_SCHEMA_VERSION,
            "contract": EXPLICIT_CURATED_EVAL_PACK_CONTRACT,
            "eval_contract_classifications": EVAL_CONTRACT_CLASSIFICATIONS,
            "real_target_required_evidence": REAL_TARGET_REQUIRED_EVIDENCE,
        },
        "builtin_benchmarks": builtin,
        "upstream_pin": {k: upstream.get(k) for k in ("clone_exists", "pinned_commit", "current_commit", "semantic_status", "ahead", "behind", "dirty")},
        "read_only_guards": ["does not call full_run", "does not adopt", "does not write skills", "does not fetch network", "does not execute upstream benchmark code"],
    }

def _git(args: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.strip()


def upstream_status(
    hermes_home_path: str | None = None,
    repo_path: str | None = None,
    *,
    allow_repo_path: bool = False,
) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    canonical = (skillopt_paths(home)["upstream"] / "SkillOpt").resolve()
    if repo_path is not None:
        requested = Path(repo_path).expanduser().resolve()
        if requested != canonical and not allow_repo_path:
            raise ValueError(f"upstream_status repo_path is restricted to canonical clone: {canonical}")
        clone = requested
    else:
        clone = canonical
    lock = PLUGIN_ROOT / "skillopt_upstream.lock"
    lock_data = {}
    if lock.exists():
        try:
            lock_data = json.loads(read_text(lock))
        except Exception:
            lock_data = {"raw": read_text(lock)}
    exists = (clone / ".git").exists()
    commit = None
    upstream_diff = {"semantic_status": "not_cloned", "ahead": None, "behind": None, "dirty": None, "note": "No network is used by status; run upstream-update/fetch-only to refresh the local clone."}
    if exists:
        code, out = _git(["rev-parse", "HEAD"], clone)
        commit = out if code == 0 else None
        code, dirty = _git(["status", "--porcelain"], clone)
        code_ab, ab = _git(["rev-list", "--left-right", "--count", "HEAD...origin/main"], clone)
        ahead = behind = None
        semantic = "local_unknown"
        if code_ab == 0 and ab:
            parts = ab.split()
            if len(parts) >= 2:
                ahead, behind = int(parts[0]), int(parts[1])
                semantic = "up_to_date" if ahead == 0 and behind == 0 else ("behind_origin" if ahead == 0 else "ahead_of_origin" if behind == 0 else "diverged")
        upstream_diff = {"semantic_status": semantic, "ahead": ahead, "behind": behind, "dirty": bool(dirty.strip()) if code == 0 else None, "note": "Compared against locally fetched origin/main only; status does not fetch or require network."}
    return {"success": True, "mode": "offline_status_only_no_fetch_no_benchmark_execution", "parity_label": "Pinned upstream source status only; no Microsoft SkillOpt upstream benchmark parity/result is claimed", "supported_parity_level": "pinned_upstream_status_only", "unsupported_true_upstream_execution": {"supported": False, "reason": "status is offline/report-only and never imports or runs upstream benchmark code"}, "upstream_url": UPSTREAM_URL, "clone_path": str(clone), "clone_exists": exists, "current_commit": commit, "lock": lock_data, "current_lock_pin": lock_data.get("pinned_commit"), "last_reviewed_upstream_commit": lock_data.get("last_reviewed_upstream_commit") or lock_data.get("pinned_commit"), "feature_matrix": UPSTREAM_SEAM_MATRIX, "delta_checklist": UPSTREAM_SEAM_MATRIX, "upstream_diff": upstream_diff}


def upstream_update(hermes_home_path: str | None = None, repo_path: str | None = None, fetch_only: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    canonical = (ensure_dirs(home)["upstream"] / "SkillOpt").resolve()
    if repo_path is not None:
        requested = Path(repo_path).expanduser().resolve()
        if requested != canonical:
            raise ValueError(f"upstream_update repo_path is restricted to canonical clone: {canonical}")
    clone = canonical
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        code, out = _git(["clone", "--depth", "1", UPSTREAM_URL, str(clone)], timeout=300)
        if code != 0:
            raise RuntimeError(out)
    else:
        code, out = _git(["fetch", "origin", "main", "--tags"], clone, timeout=300)
        if code != 0:
            raise RuntimeError(out)
        if not fetch_only:
            _git(["checkout", "main"], clone)
            code, out = _git(["pull", "--ff-only", "origin", "main"], clone, timeout=300)
            if code != 0:
                raise RuntimeError(out)
    code, commit = _git(["rev-parse", "HEAD"], clone)
    if code != 0:
        raise RuntimeError(commit)
    lock_data = {"upstream_url": UPSTREAM_URL, "clone_path": str(clone), "pinned_commit": commit, "last_reviewed_upstream_commit": commit, "feature_matrix_version": "upstream-seam-matrix-v1", "updated_at": datetime.now(timezone.utc).isoformat(), "note": "Pinned external upstream only; no plugin code merged automatically."}
    write_text(PLUGIN_ROOT / "skillopt_upstream.lock", json.dumps(lock_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"success": True, **lock_data}
