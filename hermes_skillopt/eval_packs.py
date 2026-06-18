from __future__ import annotations

"""Eval-pack inventory and review-only scaffold helpers."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from hermes_skillopt import core
from hermes_skillopt.env import EVAL_PACK_SCHEMA_VERSION, load_eval_pack, production_eligibility_for_task
from hermes_skillopt.safety import guard_safe_output_path


def _candidate_eval_paths(home: Path, skill: core.Skill) -> list[Path]:
    names = {skill.name, skill.path.parent.name, Path(skill.relpath).parent.name}
    out: list[Path] = []
    for name in names:
        for suffix in (".json", ".jsonl"):
            out.append(home / "skillopt" / "evals" / f"{name}{suffix}")
    local_eval_dir = skill.path.parent / "evals"
    if local_eval_dir.is_dir():
        out.extend(sorted(local_eval_dir.glob("*.json")))
        out.extend(sorted(local_eval_dir.glob("*.jsonl")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in out:
        try:
            r = p.expanduser().resolve(strict=False)
        except Exception:
            r = p
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def _pack_report(path: Path) -> dict[str, Any]:
    try:
        tasks, meta = load_eval_pack(path)
    except Exception as exc:
        return {"path": str(path), "exists": path.exists(), "valid": False, "error_type": type(exc).__name__, "error": core.redact_secrets(str(exc)), "production_eligible": False, "review_only": True, "missing_reasons": ["eval pack failed validation"]}
    split_counts = dict(meta.split_counts)
    missing = [s for s in ("train", "val", "test") if int(split_counts.get(s) or 0) <= 0]
    prod_tasks = [t for t in tasks if production_eligibility_for_task(t).eligible]
    reasons = list((meta.production_policy or {}).get("refusal_reasons") or [])
    if missing:
        reasons.append("missing complete splits: " + ", ".join(missing))
    if not prod_tasks:
        reasons.append("no production-eligible val/test tasks")
    production_eligible = not missing and bool(prod_tasks) and bool((meta.production_policy or {}).get("allow_production_adoption"))
    return {
        "path": str(path),
        "exists": True,
        "valid": True,
        "pack_id": meta.pack_id,
        "version": meta.version,
        "schema_version": meta.schema_version,
        "fingerprint_sha256": meta.fingerprint_sha256,
        "task_count": meta.task_count,
        "split_counts": split_counts,
        "production_eligible_task_count": len(prod_tasks),
        "production_eligible": production_eligible,
        "review_only": not production_eligible,
        "missing_reasons": sorted(set(str(r) for r in reasons if r)),
        "sample_pack": bool((meta.production_policy or {}).get("sample_pack")),
        "eval_execution_contract": meta.eval_execution_contract,
    }


def eval_pack_inventory(*, hermes_home_path: str | None = None, skill: str | None = None) -> dict[str, Any]:
    """Read-only report of discovered skills and matching eval packs."""

    home = core.hermes_home(hermes_home_path)
    skills = core.discover_skills(home)
    if skill:
        selected = [core.find_skill(home, skill)]
    else:
        selected = skills
    entries: list[dict[str, Any]] = []
    for sk in selected:
        candidates = _candidate_eval_paths(home, sk)
        existing = [p for p in candidates if p.is_file()]
        packs = [_pack_report(p) for p in existing]
        entries.append({
            "skill": sk.name,
            "skill_relpath": sk.relpath,
            "skill_sha256": sk.sha256,
            "candidate_eval_paths": [str(p) for p in candidates],
            "eval_packs": packs,
            "has_eval_pack": bool(packs),
            "production_eligible": any(p.get("production_eligible") for p in packs),
            "review_only": bool(packs) and not any(p.get("production_eligible") for p in packs),
            "missing_reasons": [] if packs else ["no matching eval pack found"],
        })
    return {"success": True, "mode": "eval_pack_inventory_read_only", "hermes_home": str(home), "skill_count": len(entries), "skills": entries}


def scaffold_eval_pack(*, skill: str, output: str | Path | None = None, hermes_home_path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Write a review-only eval-pack scaffold with complete train/val/test samples."""

    home = core.hermes_home(hermes_home_path)
    sk = core.find_skill(home, skill)
    out_raw = output or (home / "skillopt" / "evals" / f"{sk.path.parent.name}-scaffold.json")
    # Resolve before the shared guard so /var vs /private/var aliases on macOS do
    # not fail the guard's lexical+resolved HERMES_HOME checks.
    out = guard_safe_output_path(Path(out_raw).expanduser().resolve(strict=False), kind="eval pack scaffold", hermes_home=home, required_suffix=".json")
    if out.exists() and not overwrite:
        raise ValueError(f"eval pack scaffold output already exists: {out}")
    payload = {
        "schema_version": EVAL_PACK_SCHEMA_VERSION,
        "pack_id": f"{sk.path.parent.name}-review-scaffold",
        "version": "scaffold-review-only-v1",
        "sample_pack": True,
        "task_origin": "sample-eval-pack",
        "require_complete_splits": True,
        "production_policy": {
            "allow_production_adoption": False,
            "review_only": True,
            "refusal_reasons": ["scaffold is sample/review-only and contains no production evidence"],
        },
        "eval_execution_contract": {
            "classification": "static_review_only",
            "evidence": {},
        },
        "scaffold_notice": "Review-only starter pack. Replace sample prompts/expected terms with curated evidence before considering production gates.",
        "tasks": [
            {"id": "train-sample-1", "split": "train", "prompt": f"Sample training case for {sk.name}: describe expected safe behavior.", "expected_terms": ["sample"], "task_origin": "sample-eval-pack", "production_gate_eligible": False},
            {"id": "val-sample-1", "split": "validation", "prompt": f"Sample validation case for {sk.name}: verify behavior without live writes.", "expected_terms": ["sample"], "task_origin": "sample-eval-pack", "production_gate_eligible": False},
            {"id": "test-sample-1", "split": "test", "prompt": f"Sample held-out test case for {sk.name}: ensure rollback/blocker handling is stated.", "expected_terms": ["sample"], "task_origin": "sample-eval-pack", "production_gate_eligible": False},
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.scaffold.", suffix=".tmp.json", dir=out.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _, meta = load_eval_pack(tmp)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    report = _pack_report(out)
    return {"success": True, "mode": "eval_pack_scaffold_review_only", "output_path": str(out), "skill": sk.name, "review_only": True, "production_eligible": False, "eval_pack": payload, "report": report}
