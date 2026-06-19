from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hermes_skillopt import core


def stage_review_fixture(home: Path, skill: str = "demo", *, status: str = "staged_best", adoptable: bool = False) -> dict:
    """Create a minimal staged-run fixture for review/adoption tests.

    This helper is intentionally test-only. It writes enough current review/staging
    artifacts for WebUI/fleet/hygiene tests while keeping production adoption
    disabled unless the caller explicitly builds a full-run fixture elsewhere.
    """

    target = core.find_skill(home, skill)
    original = target.path.read_text(encoding="utf-8")
    proposed = original.rstrip() + "\n\n## SkillOpt Candidate Improvements (staged)\n\n- Fixture proposal for review UI and artifact hygiene tests.\n"
    rid = f"fixture-{uuid.uuid4().hex[:12]}-{target.name.replace('/', '-')}"
    run_dir = home / "skillopt" / "staging" / rid
    run_dir.mkdir(parents=True, exist_ok=False)

    diff = core.make_diff(original, proposed, target.relpath)
    files = {
        "original": "original_SKILL.md",
        "proposed": "proposed_SKILL.md",
        "diff": "diff.patch",
        "report": "report.md",
    }
    (run_dir / files["original"]).write_text(original, encoding="utf-8")
    (run_dir / files["proposed"]).write_text(proposed, encoding="utf-8")
    (run_dir / files["diff"]).write_text(diff, encoding="utf-8")
    (run_dir / files["report"]).write_text(
        f"# SkillOpt staged review fixture\n\n- run_id: {rid}\n- skill: {target.name}\n- adoptable: {str(adoptable).lower()}\n",
        encoding="utf-8",
    )
    manifest = {
        "run_id": rid,
        "status": status,
        "adoptable": bool(adoptable),
        "review_only": not adoptable,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_path": str(target.path),
        "skill_relpath": target.relpath,
        "original_sha256": core.sha256_text(original),
        "proposed_sha256": core.sha256_text(proposed),
        "production_gate_eligible": False,
        "test_gate_eligible": False,
        "production_eligibility_reasons": ["test fixture is review-only; no full-run runtime evidence"],
        "gate_policy": {"mode": "strict"},
        "files": files,
    }
    manifest["artifact_sha256"] = core.artifact_hashes(run_dir, files)
    core.save_manifest(run_dir, manifest)
    return {
        "success": True,
        "run_id": rid,
        "status": status,
        "adoptable": bool(adoptable),
        "run_dir": str(run_dir),
        "skill": target.name,
        "diff_path": str(run_dir / files["diff"]),
        "report_path": str(run_dir / files["report"]),
        "changed": bool(diff),
    }
