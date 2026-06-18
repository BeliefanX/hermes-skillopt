from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
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


def hermes_home(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
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


def _target_binding_payload(home: Path, target: Skill, original: str) -> dict[str, Any]:
    return {
        "schema_version": "skillopt-target-binding-v1",
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_relpath": target.relpath,
        "skill_path": str(target.path),
        "original_sha256": sha256_text(original),
    }


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

    required = ("gate_results", "test_results", "val", "test", "candidate_summary", "evidence", "proposed", "target_binding", "provenance_binding")
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
        out.append({"id": f"h{len(out)+1}", "source": source, "text": flat, "meta": meta or {}})

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
            out.append({"id": "harvest-warning", "source": "state.db", "text": f"harvest warning: {type(exc).__name__}: {redact_secrets(str(exc))}", "meta": {"warning": True}})
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
        })
    if not items:
        items.append({"id": "item-0001", "source_id": None, "user_goal": query or skill.name, "assistant_outcome": "unknown", "evidence": "No recent matching Hermes sessions were found; optimize from skill text only.", "tools_errors": [], "skill_relevance": 0.2, "success_hints": [], "failure_hints": ["missing harvested evidence"]})
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


def _resume_input_payload(*, home: Path, target: Skill, original: str, query: str | None, lookback_days: int, limit: int, iterations: int, edit_budget: int, candidate_count: int, optimizer_backend: str | None, target_backend: str | None, gate_mode: str, eval_file: str | None, allow_mock: bool) -> dict[str, Any]:
    from hermes_skillopt.env import load_eval_pack, resolve_eval_file
    from hermes_skillopt.state import SkillState
    from hermes_skillopt.target import TRACE_SCHEMA_VERSION, TargetExecutor

    state = SkillState(name=target.name, path=target.path, relpath=target.relpath, text=original, sha256=sha256_text(original), hermes_home=home)
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
    target_binding = _target_binding_payload(home, target, original)
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
    result = {"success": True, "run_id": rid, "status": final_status, "adoptable": adoptable, "production_gate_eligible": production_gate_eligible, "test_gate_eligible": test_gate_eligible, "strict_gate_mode": strict_gate_mode, "not_adoptable_reasons": adoptability_reasons, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(artifacts.diff), "report_path": str(artifacts.report), "gate": best_gate, "production_gate": production_validation_summary, "test_results": test_results, "heldout_test_sensitivity": heldout_test_sensitivity, "split_scores": split_scores, "per_task_delta": per_task_delta, "candidate_comparison": candidate_comparison, "regression_cases": regression_cases, "candidate_summary": candidate_summary, "optimizer_backend_config": optimizer_config, "target_backend_config": target_config, "gate_policy": gate_policy, "provenance_fingerprint": provenance, "artifact_lineage": manifest["artifact_lineage"], "benchmark_parity_status": benchmark_parity, "changed": bool(diff), "eval_file": eval_file_used, "task_counts": evidence.get("task_counts", {k: len(v) for k, v in tasks.items()}), "current_score": current_score, "candidate_score": candidate_score, "production_current_score": production_current_score, "production_candidate_score": production_candidate_score, "gate_reason": gate_reason, "checkpoint_path": str(run_dir / "checkpoint.json"), "slow_meta_path": str(artifacts.slow_meta)}
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
    write_text(run_dir / "evidence.json", json.dumps({"snippets": evidence}, ensure_ascii=False, indent=2) + "\n")
    write_text(run_dir / "report.md", f"# SkillOpt dry run\n\n- run_id: {rid}\n- skill: {target.name}\n- engine: {engine}\n- changed: {bool(diff)}\n\n```diff\n{diff[:4000]}\n```\n")
    manifest["artifact_sha256"] = artifact_hashes(run_dir, manifest["files"])
    save_manifest(run_dir, manifest)
    return {"success": True, "run_id": rid, "status": "staged", "adoptable": False, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "changed": bool(diff)}


def status(hermes_home_path: str | None = None) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = skillopt_paths(home)
    runs = []
    seen_dirs: set[Path] = set()
    manifest_keys = ("run_id", "status", "skill_name", "created_at", "engine", "backend", "adoptable", "production_gate_eligible", "test_gate_eligible", "target_executor", "split_scores", "production_eligibility_reasons", "validation_current_score", "validation_candidate_score", "test_score")
    for m in sorted(dirs["staging"].glob("*/manifest.json"), reverse=True)[:20]:
        try:
            d = json.loads(read_text(m))
            run_dir = m.parent
            seen_dirs.add(run_dir)
            row = {k: d.get(k) for k in manifest_keys}
            row["run_dir"] = str(run_dir)
            row["artifact_lineage"] = d.get("artifact_lineage") or _artifact_lineage_status(run_dir, d)
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
            "refusal_reasons": sorted(set(reasons)),
            "cleanup_guidance": "No automatic cleanup; inspect run_dir and confirm no writer is active before manual removal.",
            "run_dir": str(run_dir),
            "artifact_state": _slim_artifact_state(run_dir),
            "artifact_lineage": lineage,
        }
        checkpoint_only.append(row)
    runs.extend(checkpoint_only)
    runs = runs[:20]
    return {"success": True, "hermes_home": str(home), "skills_count": len(discover_skills(home)), "staging": str(dirs["staging"]), "backups": str(dirs["backups"]), "recent_runs": runs, "stale_or_incomplete_checkpoints": checkpoint_only[:20]}


def review(run_id: str, hermes_home_path: str | None = None, include_diff_chars: int = 4000, slim: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, m)
    diff = read_text(run_dir / "diff.patch") if (run_dir / "diff.patch").exists() else ""
    report = read_text(run_dir / "report.md") if (run_dir / "report.md").exists() else ""
    gate = m.get("gate") or (json.loads(read_text(run_dir / "gate_results.json")).get("best_gate") if (run_dir / "gate_results.json").exists() else None)
    artifact_lineage = m.get("artifact_lineage") or _artifact_lineage_status(run_dir, m)
    report_summary = {"timeline": {"status": m.get("status"), "created_at": m.get("created_at")}, "eligibility": {"adoptable": m.get("adoptable") is True, "reasons": m.get("production_eligibility_reasons") or [], "production_gate_eligible": m.get("production_gate_eligible") is True, "test_gate_eligible": m.get("test_gate_eligible") is True}, "split_scores": m.get("split_scores") or {}, "candidate_comparison": m.get("candidate_comparison") or [], "regression_cases": m.get("regression_cases") or [], "artifact_lineage": artifact_lineage, "provenance_security": {"artifact_integrity": "verified", "provenance_fingerprint": (m.get("provenance_fingerprint") or {}).get("fingerprint_sha256") if isinstance(m.get("provenance_fingerprint"), dict) else None, "production_eval_policy": (m.get("production_eval_policy") or {}).get("policy_version") if isinstance(m.get("production_eval_policy"), dict) else None, "target_executor": m.get("target_executor"), "gate_policy": m.get("gate_policy")}}
    diff_path = run_dir / "diff.patch"
    report_path = run_dir / "report.md"
    artifact_refs = {
        "diff": {"path": str(diff_path), "sha256": sha256_text(diff) if diff else None, "bytes": len(diff.encode("utf-8")), "preview_chars": 0 if slim else min(len(diff), include_diff_chars)},
        "report": {"path": str(report_path), "sha256": sha256_text(report) if report else None, "bytes": len(report.encode("utf-8")), "preview_chars": 0 if slim else min(len(report), 1200)},
    }
    return {"success": True, "run_id": run_id, "status": m.get("status"), "adoptable": m.get("adoptable") is True, "production_gate_eligible": m.get("production_gate_eligible") is True, "test_gate_eligible": m.get("test_gate_eligible") is True, "not_adoptable_reasons": m.get("production_eligibility_reasons") or [], "skill": m.get("skill_name"), "gate": gate, "production_gate": m.get("production_gate"), "test_results": m.get("test_results"), "split_scores": m.get("split_scores") or {}, "per_task_delta": m.get("per_task_delta") or [], "heldout_test_sensitivity": m.get("heldout_test_sensitivity") or {}, "candidate_comparison": m.get("candidate_comparison") or [], "regression_cases": m.get("regression_cases") or [], "candidate_summary": m.get("candidate_summary") or [], "report_fields": report_summary, "optimizer_backend_config": m.get("optimizer_backend_config") or m.get("optimizer_config"), "target_backend_config": m.get("target_backend_config"), "gate_policy": m.get("gate_policy"), "provenance_fingerprint": m.get("provenance_fingerprint"), "production_eval_policy": m.get("production_eval_policy"), "artifact_lineage": artifact_lineage, "accepted": m.get("status") in ("staged_best", "accepted", "adopted"), "artifact_integrity": "verified", "run_dir": str(run_dir), "diff_path": str(diff_path), "report_path": str(report_path), "artifact_refs": artifact_refs, "slim": bool(slim), "diff_preview": "" if slim else diff[:include_diff_chars], "report_summary": "" if slim else report[:1200]}


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
    m.update({"status": "adopted", "adopted_at": datetime.now(timezone.utc).isoformat(), "backup_dir": str(backup_dir), "original_sha256": current_sha, "adopted_sha256": proposed_sha})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "adopted", "skill_path": str(target), "backup_dir": str(backup_dir)}


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
    write_text(target, restored)
    m.update({"status": "rolled_back", "rolled_back_at": datetime.now(timezone.utc).isoformat(), "rolled_back_sha256": sha256_text(restored)})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "rolled_back", "skill_path": str(target)}



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
        _audit_writeback_event(home, action, run_id, "success", {k: v for k, v in result.items() if k in {"status", "skill_path", "backup_dir"}} if isinstance(result, dict) else {})
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
        "safe_inputs": ["local JSON manifest", "Hermes eval pack JSON"],
        "forbidden": ["import upstream Python", "execute benchmark commands", "network fetch", "write live skills"],
        "adapter_levels": {
            "import_only_bridge": "supported: JSON-only manifest conversion to Hermes eval pack",
            "true_upstream_execution": "unsupported: no upstream runner parity claim yet",
            "frozen_hermes_target_execution": "future: requires eval_execution_contract=frozen_hermes_target_execution_v1 evidence",
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
        "reporting_boundary": "no Microsoft SkillOpt upstream benchmark parity is claimed; status/import are conservative Hermes-native report-only capabilities",
        "upstream_parity_claim": "no full upstream parity claimed; compare-upstream-pin reports pinned source status only",
        "supported_parity_levels": {
            "pinned_upstream_status": "supported_read_only_no_fetch",
            "json_import_only_bridge": "supported_read_only_conversion_no_code_execution",
            "hermes_eval_pack_replay": "supported_native_not_upstream_parity",
        },
        "unsupported_parity_levels": {
            "true_upstream_benchmark_execution": "unsupported_no_adapter_no_arbitrary_code_execution",
            "networked_upstream_fetch_during_status": "unsupported_status_is_offline_only",
            "upstream_result_equivalence_claim": "unsupported_no_claim_until adapters compare against pinned upstream outputs",
        },
        "upstream_benchmark_parity": {
            "upstream_pin": {k: upstream.get(k) for k in ("clone_exists", "pinned_commit", "current_commit", "semantic_status", "ahead", "behind", "dirty")},
            "local_clone_status": "available" if upstream.get("clone_exists") else "not_cloned",
            "import_only_bridge": {"supported": True, "command": "import-upstream-benchmark", "schema_version": "hermes-upstream-benchmark-bridge-v1", "safe_read_only": True, "parity_label": "JSON/data-only import to Hermes eval pack; not upstream execution parity"},
            "true_benchmark_execution": {"supported": False, "reason": "no upstream execution adapter; arbitrary code/network execution remains disabled"},
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
