from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
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


def test_adopt_rejects_tampered_proposed_artifact_without_writing_target(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    run_dir = Path(out["run_dir"])
    (run_dir / "proposed_SKILL.md").write_text(original + "\ntampered staged artifact\n", encoding="utf-8")

    with pytest.raises(ValueError, match="proposed_SKILL.md sha"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)

    assert skill.read_text(encoding="utf-8") == original
    assert not any((tmp_path / "skillopt" / "backups").iterdir())


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


def test_rollback_refuses_tampered_backup_dir_outside_profile_without_writing_target(tmp_path):
    skill = make_skill(tmp_path, "demo")
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
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
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
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


def test_full_run_auto_adopt_and_rollback_temp_home_only(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, auto_adopt=True)
    assert out["adopt"]["status"] == "adopted"
    assert "SkillOpt Learned Rules" in skill.read_text(encoding="utf-8")
    rb = core.rollback(out["run_id"], hermes_home_path=str(tmp_path))
    assert rb["status"] == "rolled_back"
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
    assert manifest["core_abstraction"]["validation_gate"].startswith("sole_acceptance_gate")


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
