from __future__ import annotations

import json
import sqlite3
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


def make_state_db(home: Path, secret: str = "api_key=sk-abcdefghijklmnopqrstuvwxyz") -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE messages (created_at TEXT, role TEXT, content TEXT)")
    con.execute("INSERT INTO messages VALUES (?, ?, ?)", ("2099-01-01T00:00:00+00:00", "user", f"demo skill failed with pytest error; {secret}"))
    con.execute("INSERT INTO messages VALUES (?, ?, ?)", ("2099-01-01T00:00:01+00:00", "assistant", "demo skill fixed, tests passed and verified"))
    con.commit(); con.close()


def test_harvest_from_temp_state_db_and_redaction(tmp_path):
    skill_path = make_skill(tmp_path, "demo")
    make_state_db(tmp_path)
    skill = core.find_skill(tmp_path, "demo")
    snippets = core.harvest_sessions(tmp_path, skill, query="demo", limit=5)
    assert snippets
    joined = json.dumps(snippets)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in joined
    assert "<REDACTED>" in joined


def test_split_deterministic_train_val_test(tmp_path):
    items = [{"id": f"i{i}", "evidence": str(i)} for i in range(8)]
    a = core.split_items(items)
    b = core.split_items(list(reversed(items)))
    assert a == b
    assert a["train"] and a["val"] and a["test"]


def test_full_run_mock_accepted_artifacts_and_review(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    make_state_db(tmp_path)
    out = core.full_run(skill="demo", query="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, iterations=2)
    assert out["status"] == "staged_best"
    run_dir = Path(out["run_dir"])
    for fn in ["manifest.json", "original_SKILL.md", "current_SKILL.md", "best_skill.md", "proposed_SKILL.md", "diff.patch", "report.md", "evidence.json", "train_items.jsonl", "val_items.jsonl", "test_items.jsonl", "reflections.json", "candidate_edits.json", "gate_results.json", "rejected_edits.jsonl"]:
        assert (run_dir / fn).exists(), fn
    assert "SkillOpt Learned Rules" in (run_dir / "best_skill.md").read_text(encoding="utf-8")
    review = core.review(out["run_id"], hermes_home_path=str(tmp_path))
    assert review["gate"]["candidate_score"] > review["gate"]["current_score"]
    assert review["accepted"] is True


def test_rejected_gate_writes_rejected_buffer(tmp_path):
    make_skill(tmp_path, "demo", body="Verify tests, guard paths, redact secrets, handle error tool failures, rollback safely, artifact report.")
    # no-op append makes candidate equal current after strip? Use direct gate/apply path via monkey patch.
    class BadBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no useful edits"}
            return {"current_score": .9, "candidate_score": .1, "rationale": "bad"}
    old = core.LLMBackend
    core.LLMBackend = lambda *a, **k: BadBackend()  # type: ignore[assignment]
    try:
        out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    finally:
        core.LLMBackend = old  # type: ignore[assignment]
    assert out["status"] == "rejected"
    rej = Path(out["run_dir"]) / "rejected_edits.jsonl"
    assert rej.exists() and rej.read_text(encoding="utf-8").strip()


def test_auto_backend_requires_mock_permission_without_ctx(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(RuntimeError, match="allow_mock"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="auto", allow_mock=False)


def test_plugin_registration_includes_full_tool_schema():
    import importlib.util
    spec = importlib.util.spec_from_file_location("skillopt_plugin", Path(__file__).resolve().parents[1] / "__init__.py")
    plugin = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(plugin)
    names = []
    class Ctx:
        def register_tool(self, **kw): names.append((kw["name"], kw["schema"]))
    plugin.register(Ctx())
    assert "hermes_skillopt_full_run" in [n for n, _ in names]
    run_schema = dict(names)["hermes_skillopt_run"]
    assert "iterations" in run_schema["properties"]
    assert "backend" in run_schema["properties"]


def test_full_run_auto_adopt_and_rollback_temp_home_only(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, auto_adopt=True)
    assert out["adopt"]["status"] == "adopted"
    assert "SkillOpt Learned Rules" in skill.read_text(encoding="utf-8")
    rb = core.rollback(out["run_id"], hermes_home_path=str(tmp_path))
    assert rb["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8") == original
