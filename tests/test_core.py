from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_skillopt import core


def make_skill(home: Path, name="demo", body="Use tools safely.") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return p


def test_skill_discovery_frontmatter(tmp_path):
    p = make_skill(tmp_path, "demo")
    skills = core.discover_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "demo"
    assert skills[0].path == p


def test_dry_run_stages_files_and_diff(tmp_path):
    make_skill(tmp_path, "demo")
    out = core.dry_run(skill="demo", goal="be safer", hermes_home_path=str(tmp_path))
    assert out["success"] is True
    run_dir = Path(out["run_dir"])
    for fn in ["manifest.json", "original_SKILL.md", "proposed_SKILL.md", "diff.patch", "report.md", "evidence.json"]:
        assert (run_dir / fn).exists()
    diff = (run_dir / "diff.patch").read_text(encoding="utf-8")
    assert "SkillOpt Candidate Improvements" in diff
    original = (run_dir / "original_SKILL.md").read_text(encoding="utf-8")
    proposed = (run_dir / "proposed_SKILL.md").read_text(encoding="utf-8")
    assert proposed.startswith("---\nname: demo")
    assert original != proposed


def test_adopt_sha_guard_and_rollback(tmp_path):
    skill = make_skill(tmp_path, "demo")
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    run_id = out["run_id"]
    skill.write_text(skill.read_text(encoding="utf-8") + "\nexternal edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha"):
        core.adopt(run_id, hermes_home_path=str(tmp_path))
    adopt = core.adopt(run_id, hermes_home_path=str(tmp_path), force=True)
    assert Path(adopt["backup_dir"]).exists()
    assert "SkillOpt Candidate Improvements" in skill.read_text(encoding="utf-8")
    rb = core.rollback(run_id, hermes_home_path=str(tmp_path))
    assert rb["status"] == "rolled_back"
    assert "external edit" in skill.read_text(encoding="utf-8")


def test_absolute_run_id_rejected(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(ValueError, match="Invalid run_id"):
        core.resolve_run_dir(tmp_path, "/tmp/evil")


def test_parent_traversal_run_id_rejected(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(ValueError, match="Invalid run_id"):
        core.resolve_run_dir(tmp_path, "../evil")


def test_tampered_manifest_skill_path_outside_home_rejected(tmp_path):
    make_skill(tmp_path, "demo")
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    run_dir = Path(out["run_dir"])
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outside = tmp_path.parent / "outside_SKILL.md"
    outside.write_text("outside unchanged", encoding="utf-8")
    manifest["skill_path"] = str(outside)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="skill_path"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path))
    assert outside.read_text(encoding="utf-8") == "outside unchanged"


def test_rollback_refuses_current_skill_changed_after_adopt_unless_force(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    run_id = out["run_id"]
    core.adopt(run_id, hermes_home_path=str(tmp_path))
    skill.write_text(skill.read_text(encoding="utf-8") + "\nuser edit after adopt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="adopted state"):
        core.rollback(run_id, hermes_home_path=str(tmp_path))
    rb = core.rollback(run_id, hermes_home_path=str(tmp_path), force=True)
    assert rb["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8") == original


def test_secret_redaction():
    text = "api_key=sk-abcdefghijklmnopqrstuvwxyz token: gho_abcdefghijklmnopqrstuvwxyz password=hunter2"
    red = core.redact_secrets(text)
    assert "sk-" not in red
    assert "gho_" not in red
    assert "hunter2" not in red
    assert "<REDACTED>" in red


def test_upstream_status_no_network(tmp_path):
    out = core.upstream_status(hermes_home_path=str(tmp_path))
    assert out["success"] is True
    assert out["clone_exists"] is False
    assert out["upstream_url"].endswith("microsoft/SkillOpt.git")
