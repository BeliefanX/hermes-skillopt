from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import core


@pytest.fixture(autouse=True)
def active_tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


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
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["adoptable"] is False


def test_adopt_sha_guard_and_rollback(tmp_path):
    skill = make_skill(tmp_path, "demo")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_id = out["run_id"]
    skill.write_text(skill.read_text(encoding="utf-8") + "\nexternal edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha"):
        core.adopt(run_id, hermes_home_path=str(tmp_path))
    adopt = core.adopt(run_id, hermes_home_path=str(tmp_path), force=True)
    assert Path(adopt["backup_dir"]).exists()
    assert "SkillOpt Learned Rules" in skill.read_text(encoding="utf-8")
    rb = core.rollback(run_id, hermes_home_path=str(tmp_path))
    assert rb["status"] == "rolled_back"
    assert "external edit" in skill.read_text(encoding="utf-8")


def test_adopt_rejects_tampered_proposed_artifact_without_writing_target(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    (run_dir / "proposed_SKILL.md").write_text(original + "\ntampered staged artifact\n", encoding="utf-8")

    with pytest.raises(ValueError, match="proposed_SKILL.md sha"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)

    assert skill.read_text(encoding="utf-8") == original
    assert not any((tmp_path / "skillopt" / "backups").iterdir())


def test_adopt_rejects_cross_profile_home_without_unsafe_confirmation(tmp_path):
    other_home = tmp_path.parent / f"other-profile-{tmp_path.name}"
    skill = make_skill(other_home, "demo")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(other_home)
    out = core.full_run(skill="demo", hermes_home_path=str(other_home), eval_file=str(eval_path), backend="mock", allow_mock=True)

    with pytest.raises(ValueError, match="outside the active Hermes profile home"):
        core.adopt(out["run_id"], hermes_home_path=str(other_home))

    assert skill.read_text(encoding="utf-8") == original


def test_cross_profile_writeback_requires_explicit_unsafe_confirmation(tmp_path):
    other_home = tmp_path.parent / f"offline-profile-{tmp_path.name}"
    skill = make_skill(other_home, "demo")
    eval_path = write_eval_file(other_home)
    out = core.full_run(skill="demo", hermes_home_path=str(other_home), eval_file=str(eval_path), backend="mock", allow_mock=True)

    adopt = core.adopt(out["run_id"], hermes_home_path=str(other_home), unsafe_cross_profile=True)
    assert adopt["status"] == "adopted"
    rollback = core.rollback(out["run_id"], hermes_home_path=str(other_home), unsafe_cross_profile=True)
    assert rollback["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8").startswith("---\nname: demo")


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
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
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
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_id = out["run_id"]
    core.adopt(run_id, hermes_home_path=str(tmp_path))
    skill.write_text(skill.read_text(encoding="utf-8") + "\nuser edit after adopt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="adopted state"):
        core.rollback(run_id, hermes_home_path=str(tmp_path))
    rb = core.rollback(run_id, hermes_home_path=str(tmp_path), force=True)
    assert rb["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8") == original


def test_rollback_refuses_tampered_backup_dir_outside_profile_without_writing_target(tmp_path):
    skill = make_skill(tmp_path, "demo")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_id = out["run_id"]
    core.adopt(run_id, hermes_home_path=str(tmp_path))
    adopted_text = skill.read_text(encoding="utf-8")

    outside_backup = tmp_path.parent / f"outside-backup-{tmp_path.name}"
    outside_backup.mkdir()
    (outside_backup / "SKILL.md").write_text("malicious external rollback content", encoding="utf-8")

    manifest_path = Path(out["run_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backup_dir"] = str(outside_backup)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="backup_dir"):
        core.rollback(run_id, hermes_home_path=str(tmp_path))

    assert skill.read_text(encoding="utf-8") == adopted_text


def test_rollback_refuses_backup_without_manifest_even_if_staging_sha_matches(tmp_path):
    skill = make_skill(tmp_path, "demo")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_id = out["run_id"]
    core.adopt(run_id, hermes_home_path=str(tmp_path))
    adopted_text = skill.read_text(encoding="utf-8")

    malicious_backup = tmp_path / "skillopt" / "backups" / "malicious-no-manifest"
    malicious_backup.mkdir(parents=True)
    malicious_text = "malicious rollback content"
    (malicious_backup / "SKILL.md").write_text(malicious_text, encoding="utf-8")

    manifest_path = Path(out["run_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backup_dir"] = str(malicious_backup)
    manifest["original_sha256"] = core.sha256_text(malicious_text)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Backup manifest missing"):
        core.rollback(run_id, hermes_home_path=str(tmp_path))

    assert skill.read_text(encoding="utf-8") == adopted_text


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
    assert out["clone_path"] == str((tmp_path / "skillopt" / "upstream" / "SkillOpt").resolve())


def test_upstream_status_rejects_noncanonical_repo_path_by_default(tmp_path):
    with pytest.raises(ValueError, match="canonical clone"):
        core.upstream_status(hermes_home_path=str(tmp_path), repo_path=str(tmp_path / "other"))


def test_upstream_status_internal_allow_repo_path_escape_hatch(tmp_path):
    other = tmp_path / "other"
    out = core.upstream_status(hermes_home_path=str(tmp_path), repo_path=str(other), allow_repo_path=True)
    assert out["success"] is True
    assert out["clone_path"] == str(other.resolve())
    assert out["clone_exists"] is False


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
    assert not (Path(out["run_dir"]) / "best_skill.md").exists()
    assert (Path(out["run_dir"]) / "diff.patch").read_text(encoding="utf-8") == ""


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
    assert "eval_file" in run_schema["properties"]
    assert "auto_adopt" not in run_schema["properties"]
    update_schema = dict(names)["hermes_skillopt_upstream_update"]
    assert "repo_path" not in update_schema["properties"]
    status_schema = dict(names)["hermes_skillopt_upstream_status"]
    assert "repo_path" not in status_schema["properties"]
    adopt_schema = dict(names)["hermes_skillopt_adopt"]
    rollback_schema = dict(names)["hermes_skillopt_rollback"]
    assert "hermes_home" not in adopt_schema["properties"]
    assert "hermes_home" not in rollback_schema["properties"]


def test_plugin_writeback_handlers_ignore_home_override(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("skillopt_plugin_writeback", Path(__file__).resolve().parents[1] / "__init__.py")
    assert spec and spec.loader
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    calls = []
    monkeypatch.setattr(plugin.core, "adopt", lambda **kwargs: calls.append(("adopt", kwargs)) or {"success": True})
    monkeypatch.setattr(plugin.core, "rollback", lambda **kwargs: calls.append(("rollback", kwargs)) or {"success": True})

    plugin._handle_adopt({"run_id": "rid", "hermes_home": "/tmp/other", "force": True})
    plugin._handle_rollback({"run_id": "rid", "hermes_home": "/tmp/other", "force": True})
    assert calls == [
        ("adopt", {"run_id": "rid", "hermes_home_path": None, "force": True}),
        ("rollback", {"run_id": "rid", "hermes_home_path": None, "force": True}),
    ]


def test_plugin_yaml_provides_tools_matches_registered_tools():
    import importlib.util

    repo = Path(__file__).resolve().parents[1]
    plugin_yaml = repo / "plugin.yaml"
    provided = []
    in_tools = False
    for raw in plugin_yaml.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line == "provides_tools:":
            in_tools = True
            continue
        if in_tools and line.startswith("- "):
            provided.append(line[2:].strip())
        elif in_tools and line:
            break

    spec = importlib.util.spec_from_file_location("skillopt_plugin_yaml_check", repo / "__init__.py")
    plugin = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(plugin)
    registered = []
    class Ctx:
        def register_tool(self, **kw): registered.append(kw["name"])
    plugin.register(Ctx())

    assert "hermes_skillopt_handoff_optimize" in provided
    assert "hermes_skillopt_handoff_optimize" in registered
    assert provided == registered


def write_eval_file(home: Path, name: str = "demo", rows: list[dict] | None = None) -> Path:
    p = home / "skillopt" / "evals" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = rows or [
        {"id": "v1", "prompt": "held-out validation replay", "expected_keywords": ["verify", "blocker"], "forbidden_keywords": ["fabricate"], "split": "validation", "weight": 2},
        {"id": "tr1", "prompt": "train replay", "expected_keywords": ["tool"], "split": "train"},
        {"id": "te1", "prompt": "test replay", "success_criteria": ["rollback"], "expected_keywords": ["rollback"], "split": "test"},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_curated_eval_file_loaded_and_candidate_improvement_stages_best(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    current_eval = json.loads((run_dir / "current_validation_results.json").read_text(encoding="utf-8"))
    candidate_eval = json.loads((run_dir / "candidate_validation_results.json").read_text(encoding="utf-8"))
    assert out["status"] == "staged_best"
    assert manifest["eval_file"] == str(eval_path)
    assert manifest["curated_task_count"] == 3
    assert manifest["task_counts"]["val"] >= 1
    assert current_eval["results"][0]["task_id"] == "v1"
    assert candidate_eval["score"] > current_eval["score"]
    assert (run_dir / "best_skill.md").exists()


def test_curated_eval_json_object_file_supported(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = tmp_path / "skillopt" / "evals" / "demo.json"
    eval_path.parent.mkdir(parents=True, exist_ok=True)
    eval_path.write_text(json.dumps({"tasks": [{"id": "vjson", "prompt": "json validation", "expected_keywords": ["verify"], "split": "validation"}]}, indent=2), encoding="utf-8")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    current_eval = json.loads((run_dir / "current_validation_results.json").read_text(encoding="utf-8"))
    assert current_eval["results"][0]["task_id"] == "vjson"


def test_long_success_criteria_is_metadata_not_full_semantic_judge(tmp_path):
    from hermes_skillopt.env import load_eval_tasks

    eval_path = write_eval_file(tmp_path, rows=[{
        "id": "long-criteria",
        "prompt": "long natural-language criterion",
        "success_criteria": "The answer should carefully analyze tradeoffs, mention rollback only when relevant, and provide a nuanced operational plan.",
        "split": "validation",
    }])

    task = load_eval_tasks(eval_path)[0]
    assert task.success_criteria
    assert task.expected_terms == ()


def test_curated_eval_file_no_candidate_improvement_does_not_stage_best(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    eval_path = write_eval_file(tmp_path, rows=[{"id": "v1", "prompt": "validation", "expected_keywords": ["uniquegold"], "split": "validation"}])

    class NoEditBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no-op"}
            return {"accepted": True, "rationale": "aux only"}

    old = core.LLMBackend
    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: NoEditBackend())
    try:
        out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    finally:
        core.LLMBackend = old  # type: ignore[assignment]
    run_dir = Path(out["run_dir"])
    assert out["status"] == "rejected"
    assert not (run_dir / "best_skill.md").exists()
    gate_data = json.loads((run_dir / "gate_results.json").read_text(encoding="utf-8"))
    assert gate_data["gates"][0]["accepted"] is False
    assert gate_data["gates"][0]["current_eval"]["results"][0]["task_id"] == "v1"


def test_eval_file_path_guard_and_missing_errors(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(FileNotFoundError, match="eval_file not found"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file="missing.jsonl", backend="mock", allow_mock=True)
    outside = tmp_path.parent / f"outside-eval-{tmp_path.name}.jsonl"
    outside.write_text('{"id":"v","prompt":"x","expected_keywords":["x"],"split":"validation"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="eval_file"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(outside), backend="mock", allow_mock=True)


def test_full_run_auto_adopt_disabled_even_for_temp_home(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="auto_adopt is disabled"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, auto_adopt=True)
    with pytest.raises(ValueError, match="auto_adopt cannot be combined with force"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, auto_adopt=True, force=True)
    assert skill.read_text(encoding="utf-8") == original


def test_validation_gate_ignores_judge_acceptance_when_scores_regress():
    from hermes_skillopt.gate import ValidationGate

    decision = ValidationGate().decide(
        1,
        {"score": 0.9, "results": []},
        {"score": 0.1, "results": []},
        "current",
        "candidate",
        judge={"accepted": True, "candidate_score": 1.0, "rationale": "LLM likes it"},
    )
    assert decision.accepted is False
    assert decision.current_score == 0.9
    assert decision.candidate_score == 0.1


def test_full_run_staged_best_manifest_records_core_abstractions(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert out["status"] == "staged_best"
    assert (run_dir / "best_skill.md").exists()
    assert manifest["core_abstraction"]["skill_document"] == "trainable_state"
    assert manifest["core_abstraction"]["target_agent_model"] == "frozen_scorecard_replay_executor"
    assert manifest["core_abstraction"]["validation_gate"].startswith("sole_acceptance_gate")
    assert manifest["adoptable"] is False
    assert manifest["production_gate_eligible"] is False
    stage_dir = run_dir / "stages"
    assert {p.name.split("_", 1)[1] for p in stage_dir.glob("001_*.json")} == {
        "rollout.json", "reflect.json", "aggregate.json", "select.json", "update.json", "evaluate.json"
    }


def test_extended_eval_schema_and_hermes_replay_runner(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path, rows=[{
        "id": "v-replay",
        "prompt": "replay validation",
        "expected_behavior": "candidate mentions verification and blockers",
        "assertions": [{"type": "contains", "value": "verify"}, {"type": "contains", "value": "blocker"}],
        "judge": "hermes_replay_assertions",
        "allowed_tools": ["terminal"],
        "timeout": 10,
        "fixtures": {"error.txt": "tool failed"},
        "success_criteria": ["verify", "blocker"],
        "expected_keywords": ["verify", "blocker"],
        "split": "validation",
    }, {"id": "tr", "prompt": "train", "split": "train", "expected_keywords": ["tool"]}])
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    candidate_eval = json.loads((run_dir / "candidate_validation_results.json").read_text(encoding="utf-8"))
    assert manifest["target_executor"] == "hermes_replay_runner_mvp"
    assert manifest["target_config_id"] == "frozen-hermes-replay-mvp-v1"
    assert candidate_eval["results"][0]["metadata"].get("assertion_results") or candidate_eval["results"][0]["metadata"].get("assertion_count") == 2


def test_rejected_edit_history_enters_reflection_prompt(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    old_run = tmp_path / "skillopt" / "staging" / "old-demo"
    old_run.mkdir(parents=True)
    (old_run / "manifest.json").write_text(json.dumps({"skill_name": "demo"}), encoding="utf-8")
    (old_run / "rejected_edits.jsonl").write_text(json.dumps({"iteration": 1, "reasoning": "do not repeat me"}) + "\n", encoding="utf-8")
    prompts = []

    class CaptureBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            prompts.append(prompt)
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no-op"}
            return {"accepted": False, "rationale": "aux only"}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: CaptureBackend())
    core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    assert any("REJECTED_EDIT_HISTORY" in p and "do not repeat me" in p for p in prompts)


def test_rejected_edit_history_skips_missing_malformed_and_mismatched_manifests(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    staging = tmp_path / "skillopt" / "staging"
    cases = {
        "missing-manifest": (None, "missing manifest contam"),
        "malformed-manifest": ("{not json", "malformed manifest contam"),
        "list-manifest": (json.dumps([{"skill_name": "demo"}]), "list manifest contam"),
        "wrong-skill": (json.dumps({"skill_name": "other"}), "wrong skill contam"),
        "valid-demo": (json.dumps({"skill_name": "demo"}), "valid history"),
    }
    for dirname, (manifest_text, rejected_reason) in cases.items():
        run = staging / dirname
        run.mkdir(parents=True)
        if manifest_text is not None:
            (run / "manifest.json").write_text(manifest_text, encoding="utf-8")
        (run / "rejected_edits.jsonl").write_text(json.dumps({"iteration": 1, "reasoning": rejected_reason}) + "\n", encoding="utf-8")

    prompts = []

    class CaptureBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            prompts.append(prompt)
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no-op"}
            return {"accepted": False, "rationale": "aux only"}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: CaptureBackend())
    core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    joined = "\n".join(prompts)
    assert "valid history" in joined
    assert "missing manifest contam" not in joined
    assert "malformed manifest contam" not in joined
    assert "list manifest contam" not in joined
    assert "wrong skill contam" not in joined


def test_legacy_dry_run_manifest_refuses_adopt_even_with_force(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    with pytest.raises(ValueError, match="review-only"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)
    assert skill.read_text(encoding="utf-8") == original


def test_full_run_with_explicit_curated_scorecard_is_adoptable(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    original = skill.read_text(encoding="utf-8")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    assert out["adoptable"] is True
    adopt = core.adopt(out["run_id"], hermes_home_path=str(tmp_path))
    assert adopt["status"] == "adopted"
    assert "SkillOpt Learned Rules" in skill.read_text(encoding="utf-8")
    rb = core.rollback(out["run_id"], hermes_home_path=str(tmp_path))
    assert rb["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8") == original


def test_mixed_validation_nonproduction_improvement_cannot_make_adoptable(monkeypatch, tmp_path):
    from hermes_skillopt.env import EvalTask, HermesSkillEnv

    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    eval_source = str(tmp_path / "skillopt" / "evals" / "demo.jsonl")

    def mixed_tasks(self):
        curated = EvalTask(
            id="curated-stable",
            prompt="curated production validation",
            source=eval_source,
            expected_terms=("uniquegold",),
            split="val",
            metadata={"scorecard_explicit": True, "production_gate_eligible": True},
        )
        mined = EvalTask(
            id="session-lift",
            prompt="session mined review-only validation",
            source="session-mined",
            expected_terms=("verify",),
            split="val",
            metadata={"production_gate_eligible": False},
        )
        train = EvalTask(id="train", prompt="train", source="curated-fallback", expected_terms=("tool",), split="train")
        test = EvalTask(id="test", prompt="test", source="synthetic", expected_terms=("rollback",), split="test")
        return {"train": [train], "val": [curated, mined], "test": [test]}, {
            "snippets": [],
            "items": [],
            "abstraction": "environment/benchmark",
            "eval_file": eval_source,
            "curated_task_count": 1,
            "task_counts": {"train": 1, "val": 2, "test": 1},
            "production_gate_task_count": 1,
            "production_gate_eligible": True,
        }

    monkeypatch.setattr(HermesSkillEnv, "build_tasks", mixed_tasks)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    gate_data = json.loads((run_dir / "gate_results.json").read_text(encoding="utf-8"))
    assert out["status"] == "staged_best"
    assert out["adoptable"] is False
    assert manifest["adoptable"] is False
    assert manifest["production_gate_eligible"] is False
    assert gate_data["best_gate"]["accepted"] is True
    assert gate_data["production_best_gate"]["accepted"] is False
    assert gate_data["production_best_gate"]["current_score"] == gate_data["production_best_gate"]["candidate_score"]
    with pytest.raises(ValueError, match="Only adoptable"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path))


def test_stage_recorder_writes_all_six_stage_artifacts(tmp_path):
    from hermes_skillopt.trainer import StageRecorder

    stages = StageRecorder(tmp_path)
    stages.rollout(1, {"score": 0.1, "num_tasks": 1, "executor": "mock"})
    stages.reflect(1, {"recurring_defects": []}, 0)
    stages.aggregate(1, {"recurring_defects": []}, 3)
    stages.select(1, {"edits": [{"op": "append"}], "bounded": True})
    stages.update(1, "abc123", True)
    stages.evaluate(1, {"score": 0.2}, {"score": 0.3}, {"accepted": True})
    assert {p.name for p in (tmp_path / "stages").glob("001_*.json")} == {
        "001_rollout.json", "001_reflect.json", "001_aggregate.json", "001_select.json", "001_update.json", "001_evaluate.json"
    }


def test_discover_skills_rejects_symlink_escape_before_read(tmp_path):
    outside = tmp_path.parent / f"outside-skill-{tmp_path.name}.md"
    outside.write_text("---\nname: escaped\n---\nsecret outside profile\n", encoding="utf-8")
    link_dir = tmp_path / "skills" / "escaped"
    link_dir.mkdir(parents=True)
    (link_dir / "SKILL.md").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        core.discover_skills(tmp_path)


def test_upstream_update_rejects_noncanonical_repo_path(tmp_path):
    with pytest.raises(ValueError, match="canonical clone"):
        core.upstream_update(hermes_home_path=str(tmp_path), repo_path=str(tmp_path / "other"), fetch_only=True)


def test_cli_help_commands_smoke():
    repo = Path(__file__).resolve().parents[1]
    for cmd in (
        [sys.executable, "-m", "hermes_skillopt.cli", "--help"],
        [sys.executable, "-m", "hermes_skillopt.cli", "full-run", "--help"],
        [sys.executable, "-m", "hermes_skillopt.cli", "webui", "--help"],
        [sys.executable, "-m", "hermes_skillopt.cli", "handoff-optimize", "--help"],
    ):
        proc = subprocess.run(cmd, cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        assert proc.returncode == 0
        assert "usage:" in proc.stdout.lower()
        if "full-run" in cmd:
            assert "--eval-file" in proc.stdout
            assert "--dry-run" not in proc.stdout
            assert "--target-executor" in proc.stdout


def test_heldout_test_results_and_artifact_hashes_are_recorded_and_verified(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert (run_dir / "test_results.json").exists()
    assert manifest["test_gate_eligible"] is True
    assert manifest["artifact_sha256"]["test_results"] == core.sha256_file(run_dir / "test_results.json")
    assert core.review(out["run_id"], hermes_home_path=str(tmp_path))["artifact_integrity"] == "verified"
    (run_dir / "test_results.json").write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Artifact hash mismatch for test_results"):
        core.review(out["run_id"], hermes_home_path=str(tmp_path))


def test_sandbox_target_executor_is_isolated_and_records_transcript(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "train", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "val", "prompt": "validation", "expected_keywords": ["verify", "blocker"], "split": "validation", "executor": "sandbox", "required_markers": ["SANDBOX_OK"]},
        {"id": "test", "prompt": "test", "expected_keywords": ["rollback"], "split": "test", "executor": "sandbox", "required_markers": ["SANDBOX_OK"]},
    ])
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True, target_executor="sandbox")
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    test_results = json.loads((run_dir / "test_results.json").read_text(encoding="utf-8"))
    assert manifest["target_executor"] == "hermes_sandbox_executor_mvp"
    meta = test_results["results"][0]["metadata"]
    assert meta["sandbox_isolated"] is True
    assert meta["live_profile_writes"] is False
    assert meta["sandbox_command_blocked"] is False
    assert "SANDBOX_OK" in meta["transcript_preview"]


def test_sandbox_executor_blocks_task_command_live_profile_write(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    live_write = tmp_path / "skills" / "demo" / "PWNED_BY_SANDBOX_COMMAND.txt"
    malicious = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(live_write)!r}).write_text('pwned', encoding='utf-8'); print('SANDBOX_OK')",
    ]
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "train", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "malicious-val", "prompt": "validation", "expected_keywords": ["verify"], "split": "validation", "executor": "sandbox", "fixtures": {"command": malicious}, "required_markers": ["SANDBOX_OK"]},
        {"id": "test", "prompt": "test", "expected_keywords": ["rollback"], "split": "test", "executor": "sandbox", "required_markers": ["SANDBOX_OK"]},
    ])

    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True, target_executor="sandbox")
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    candidate_eval = json.loads((run_dir / "candidate_validation_results.json").read_text(encoding="utf-8"))
    meta = candidate_eval["results"][0]["metadata"]

    assert not live_write.exists()
    assert manifest["adoptable"] is False
    assert manifest["production_gate_eligible"] is False
    assert candidate_eval["production_gate_eligible"] is False
    assert candidate_eval["results"][0]["passed"] is False
    assert meta["exit_code"] == 126
    assert meta["sandbox_command_blocked"] is True
    assert meta["live_profile_writes"] is False
    assert meta["production_gate_eligible"] is False
    assert "SANDBOX_COMMAND_BLOCKED" in meta["transcript_preview"]


def test_invalid_optimizer_edit_is_rejected_and_review_only(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")

    class InvalidBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [{"op": "replace", "old": "---\nname: demo", "new": "---\nname: pwn"}], "reasoning": "bad"}
            return {"accepted": True}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: InvalidBackend())
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    assert out["status"] == "rejected"
    rejected = (run_dir / "rejected_edits.jsonl").read_text(encoding="utf-8")
    assert "protected_frontmatter" in rejected
    assert (run_dir / "candidate_1_edit_validation_rejected.json").exists()
