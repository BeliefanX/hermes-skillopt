from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import core
from skillopt_test_fixtures import stage_review_fixture
from hermes_skillopt.env import load_eval_pack
from hermes_skillopt.batch import batch_preflight, run_batch
from hermes_skillopt.eval_packs import create_curated_eval_pack, eval_pack_autopilot, eval_pack_doctor, eval_pack_inventory, generate_negative_boundary_eval_pack, ingest_skill_context_eval_seed, ingest_user_correction_eval_seed, mine_session_eval_pack, promote_eval_pack, scaffold_eval_pack
from hermes_skillopt.skill_types import classify_skill_type


@pytest.fixture(autouse=True)
def active_tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def make_skill(home: Path, name: str = "demo") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\nUse tools safely.\n", encoding="utf-8")
    return p


def write_review_eval(home: Path, name: str = "demo") -> Path:
    p = home / "skillopt" / "evals" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "hermes-curated-eval-pack-v1",
        "pack_id": f"{name}-review",
        "version": "test",
        "sample_pack": True,
        "task_origin": "sample-eval-pack",
        "require_complete_splits": True,
        "production_policy": {"allow_production_adoption": False},
        "eval_execution_contract": {"classification": "static_review_only"},
        "tasks": [
            {"id": "tr", "split": "train", "prompt": "train", "expected_terms": ["x"], "production_gate_eligible": False},
            {"id": "va", "split": "validation", "prompt": "val", "expected_terms": ["x"], "production_gate_eligible": False},
            {"id": "te", "split": "test", "prompt": "test", "expected_terms": ["x"], "production_gate_eligible": False},
        ],
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def write_production_eval(home: Path, name: str = "prod") -> Path:
    p = home / "skillopt" / "evals" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "hermes-curated-eval-pack-v1",
        "pack_id": f"{name}-prod",
        "version": "test",
        "sample_pack": False,
        "task_origin": "curated",
        "require_complete_splits": True,
        "production_policy": {"allow_production_adoption": True},
        "eval_execution_contract": {"classification": "deterministic_replay_contract_compliant"},
        "tasks": [
            {"id": "tr", "split": "train", "prompt": "train prod", "expected_terms": ["verify"], "production_gate_eligible": False},
            {"id": "va", "split": "validation", "prompt": "val prod", "expected_terms": ["verify"], "production_gate_eligible": True},
            {"id": "te", "split": "test", "prompt": "test prod", "expected_terms": ["verify"], "production_gate_eligible": True},
        ],
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def write_invalid_eval(home: Path, name: str = "bad") -> Path:
    p = home / "skillopt" / "evals" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"schema_version":"hermes-curated-eval-pack-v1","pack_id":"bad"', encoding="utf-8")
    return p


def curated_tasks() -> list[dict[str, object]]:
    return [
        {"id": "tr", "split": "train", "prompt": "Train safe tool use", "expected_terms": ["verify"], "production_gate_eligible": False},
        {"id": "va", "split": "validation", "prompt": "Validate safe tool use", "expected_terms": ["verify"], "production_gate_eligible": True},
        {"id": "te", "split": "test", "prompt": "Test safe tool use", "expected_terms": ["verify"], "production_gate_eligible": True},
    ]


def test_batch_preflight_rejects_writeback_and_budget_and_prod_missing_eval(tmp_path):
    plan = {
        "schema_version": "hermes-skillopt-batch-plan-v1",
        "budget": {"max_jobs": 1, "max_total_iterations": 1, "max_total_candidates": 1},
        "jobs": [
            {"skill": "demo", "iterations": 2, "candidate_count": 2, "force": True, "backend": "hermes", "gate_mode": "strict", "production_intent": True},
            {"skill": "demo", "eval_file": "x.json", "backend": "bogus"},
        ],
    }
    with pytest.raises(ValueError) as exc:
        batch_preflight(plan, hermes_home_path=str(tmp_path))
    msg = str(exc.value)
    assert "exceeds budget.max_jobs" in msg
    assert "force" in msg
    assert "missing eval_file" in msg
    assert "invalid backend" in msg


def test_batch_run_stages_parent_artifacts_and_forces_no_adopt(monkeypatch, tmp_path):
    calls = []

    def fake_full_run(**kwargs):
        calls.append(kwargs)
        return {"success": True, "run_id": "child-1", "run_dir": str(tmp_path / "skillopt" / "staging" / "child-1"), "status": "staged_best", "adoptable": False}

    monkeypatch.setattr("hermes_skillopt.batch.core.full_run", fake_full_run)
    plan = {
        "schema_version": "hermes-skillopt-batch-plan-v1",
        "jobs": [{"skill": "demo", "eval_file": "eval.json", "backend": "mock", "allow_mock": True, "gate_mode": "soft", "iterations": 1, "candidate_count": 1}],
    }
    out = run_batch(plan, hermes_home_path=str(tmp_path))
    batch_dir = Path(out["run_dir"])
    assert out["success"] is True
    assert (batch_dir / "manifest.json").exists()
    assert (batch_dir / "preflight.json").exists()
    assert (batch_dir / "jobs.json").exists()
    assert (batch_dir / "summary.json").exists()
    assert (batch_dir / "report.md").exists()
    assert calls and calls[0]["auto_adopt"] is False and calls[0]["force"] is False
    assert calls[0]["allow_mock"] is True


def test_eval_pack_inventory_and_scaffold_are_review_only(tmp_path):
    make_skill(tmp_path, "demo")
    eval_path = write_review_eval(tmp_path, "demo")

    inv = eval_pack_inventory(hermes_home_path=str(tmp_path))
    entry = inv["skills"][0]
    assert entry["has_eval_pack"] is True
    assert entry["production_eligible"] is False
    assert entry["eval_packs"][0]["path"] == str(eval_path.resolve())
    assert entry["eval_packs"][0]["review_only"] is True
    assert entry["skill_type"]["advisory_only"] is True
    assert entry["recommended_next_action"]
    assert inv["readiness_matrix"]["only_review_only_count"] == 1

    out_path = tmp_path / "skillopt" / "evals" / "demo-scaffold.json"
    out = scaffold_eval_pack(skill="demo", output=out_path, hermes_home_path=str(tmp_path))
    assert out["success"] is True
    assert out["review_only"] is True
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["production_policy"]["allow_production_adoption"] is False
    assert {t["split"] for t in payload["tasks"]} == {"train", "validation", "test"}
    assert all(t["production_gate_eligible"] is False for t in payload["tasks"])


def test_eval_pack_inventory_discovers_versioned_name_derived_packs_without_broad_false_matches(tmp_path):
    make_skill(tmp_path, "demo")
    versioned = write_production_eval(tmp_path, "demo-thermal-v3")
    unrelated = write_review_eval(tmp_path, "demographic")

    inv = eval_pack_inventory(hermes_home_path=str(tmp_path), skill="demo")
    entry = inv["skills"][0]
    paths = {p["path"] for p in entry["eval_packs"]}

    assert str(versioned.resolve()) in paths
    assert str(unrelated.resolve()) not in paths
    assert entry["production_eligible"] is True
    readiness = entry["readiness_adoptability"]
    assert readiness["schema_version"] == "hermes-skillopt-readiness-adoptability-v1"
    assert readiness["production_gate_eligible"] is True
    assert readiness["test_gate_eligible"] is True
    assert readiness["adoptable"] is False
    assert "inventory is discovery-only" in readiness["warnings"][0]


def test_curated_eval_pack_factory_validates_and_can_be_production_eligible(tmp_path):
    make_skill(tmp_path, "demo")
    out_path = tmp_path / "skillopt" / "evals" / "demo-curated.json"
    out = create_curated_eval_pack(
        skill="demo",
        tasks=curated_tasks(),
        output=out_path,
        hermes_home_path=str(tmp_path),
        production_policy={"allow_production_adoption": True},
        eval_execution_contract={"classification": "deterministic_replay_contract_compliant"},
    )
    assert out["success"] is True
    assert out["production_eligible"] is True
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["fingerprint_sha256"] == out["metadata"]["fingerprint_sha256"]
    assert payload["canonical_json"] is True
    tasks, meta = load_eval_pack(out_path)
    assert meta.split_counts == {"train": 1, "val": 1, "test": 1}
    assert meta.production_eligible_task_count == 2
    assert sum(1 for task in tasks if task.metadata["production_gate_eligible"]) == 2


def test_curated_eval_pack_factory_rejects_missing_splits_leakage_origins_and_unsafe_outputs(tmp_path):
    make_skill(tmp_path, "demo")
    safe_out = tmp_path / "skillopt" / "evals" / "bad.json"
    with pytest.raises(ValueError, match="missing: test"):
        create_curated_eval_pack(skill="demo", tasks=curated_tasks()[:2], output=safe_out, hermes_home_path=str(tmp_path))
    assert not safe_out.exists()

    leaked = curated_tasks()
    leaked[2] = {**leaked[2], "prompt": leaked[1]["prompt"]}
    with pytest.raises(ValueError, match="identical prompt"):
        create_curated_eval_pack(skill="demo", tasks=leaked, output=safe_out, hermes_home_path=str(tmp_path))

    session_origin = curated_tasks()
    session_origin[1] = {**session_origin[1], "task_origin": "session-mined"}
    with pytest.raises(ValueError, match="non-production origins"):
        create_curated_eval_pack(skill="demo", tasks=session_origin, output=safe_out, hermes_home_path=str(tmp_path), production_policy={"allow_production_adoption": True}, eval_execution_contract={"classification": "deterministic_replay_contract_compliant"})

    with pytest.raises(ValueError, match="under HERMES_HOME"):
        create_curated_eval_pack(skill="demo", tasks=curated_tasks(), output=tmp_path / "skills" / "demo" / "bad.json", hermes_home_path=str(tmp_path))


def test_curated_eval_pack_factory_invalid_production_request_does_not_create_final_output(tmp_path):
    make_skill(tmp_path, "demo")
    out_path = tmp_path / "skillopt" / "evals" / "invalid-prod.json"
    no_gate_tasks = [{**task, "production_gate_eligible": False} for task in curated_tasks()]

    with pytest.raises(ValueError, match="not production eligible"):
        create_curated_eval_pack(
            skill="demo",
            tasks=no_gate_tasks,
            output=out_path,
            hermes_home_path=str(tmp_path),
            production_policy={"allow_production_adoption": True},
            eval_execution_contract={"classification": "deterministic_replay_contract_compliant"},
        )

    assert not out_path.exists()
    assert not list(out_path.parent.glob(f".{out_path.name}.curated.*.tmp.json"))


def test_curated_eval_pack_factory_invalid_production_request_preserves_existing_file(tmp_path):
    make_skill(tmp_path, "demo")
    out_path = tmp_path / "skillopt" / "evals" / "invalid-prod.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "sentinel existing eval pack\n"
    out_path.write_text(sentinel, encoding="utf-8")
    no_gate_tasks = [{**task, "production_gate_eligible": False} for task in curated_tasks()]

    with pytest.raises(ValueError, match="not production eligible"):
        create_curated_eval_pack(
            skill="demo",
            tasks=no_gate_tasks,
            output=out_path,
            hermes_home_path=str(tmp_path),
            production_policy={"allow_production_adoption": True},
            eval_execution_contract={"classification": "deterministic_replay_contract_compliant"},
            overwrite=True,
        )

    assert out_path.read_text(encoding="utf-8") == sentinel
    assert not list(out_path.parent.glob(f".{out_path.name}.curated.*.tmp.json"))


def test_review_only_factories_cannot_become_production_eligible_automatically(tmp_path):
    make_skill(tmp_path, "demo")

    scaffold_path = tmp_path / "skillopt" / "evals" / "demo-scaffold.json"
    scaffold = scaffold_eval_pack(skill="demo", output=scaffold_path, hermes_home_path=str(tmp_path))
    scaffold_payload = json.loads(scaffold_path.read_text(encoding="utf-8"))
    assert scaffold["production_eligible"] is False
    assert scaffold["report"]["production_eligible"] is False
    assert scaffold_payload["production_policy"]["allow_production_adoption"] is False
    assert all(t["production_gate_eligible"] is False for t in scaffold_payload["tasks"])

    fixture = tmp_path / "sessions_fixture.json"
    fixture.write_text(json.dumps({"sessions": [{"id": "s1", "text": "verified demo tool behavior"}]}), encoding="utf-8")
    mined_path = tmp_path / "skillopt" / "evals" / "demo-session-mined.json"
    mined = mine_session_eval_pack(skill="demo", output=mined_path, hermes_home_path=str(tmp_path), session_fixture=fixture)
    mined_payload = json.loads(mined_path.read_text(encoding="utf-8"))
    assert mined["production_eligible"] is False
    assert mined["report"]["production_eligible"] is False
    assert mined_payload["task_origin"] == "session-mined"
    assert mined_payload["production_policy"]["allow_production_adoption"] is False
    assert all(t["production_gate_eligible"] is False for t in mined_payload["tasks"])
    assert all(t["task_origin"] == "session-mined" for t in mined_payload["tasks"])


def test_session_mined_eval_pack_is_review_only_redacted_and_loadable(tmp_path):
    make_skill(tmp_path, "demo")
    fixture = tmp_path / "sessions_fixture.json"
    fixture.write_text(json.dumps({"sessions": [{"id": "s1", "text": "demo tool failed with api_key=SECRET123 then verified fix passed"}]}), encoding="utf-8")
    out_path = tmp_path / "skillopt" / "evals" / "demo-session-mined.json"

    out = mine_session_eval_pack(skill="demo", output=out_path, hermes_home_path=str(tmp_path), session_fixture=fixture)
    assert out["success"] is True
    assert out["review_only"] is True
    assert out["production_eligible"] is False
    text = out_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["task_origin"] == "session-mined"
    assert payload["production_policy"]["allow_production_adoption"] is False
    assert "SECRET123" not in text
    assert "<REDACTED>" in text
    assert all(t["production_gate_eligible"] is False for t in payload["tasks"])
    assert all(t["task_origin"] == "session-mined" for t in payload["tasks"])
    tasks, meta = load_eval_pack(out_path)
    assert meta.split_counts == {"train": 1, "val": 1, "test": 1}
    assert meta.production_eligible_task_count == 0
    assert all(task.metadata["task_origin"] == "session-mined" for task in tasks)


def test_eval_pack_autopilot_seeds_negative_boundary_and_promotion_are_safe(tmp_path):
    make_skill(tmp_path, "demo")

    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    plan = eval_pack_autopilot(skill="demo", hermes_home_path=str(tmp_path))
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert plan["mode"] == "eval_pack_autopilot_plan_read_only"
    assert plan["read_only"] is True
    assert before == after

    doctor = eval_pack_doctor(hermes_home_path=str(tmp_path), skill="demo")
    assert doctor["mode"] == "eval_pack_doctor_read_only"
    assert doctor["live_skill_writes"] is False

    correction_path = tmp_path / "skillopt" / "evals" / "demo-correction.json"
    correction = ingest_user_correction_eval_seed(skill="demo", correction="When user corrects output, preserve api_key=SECRET123 redacted and verify regression.", output=correction_path, hermes_home_path=str(tmp_path))
    assert correction["production_eligible"] is False
    correction_text = correction_path.read_text(encoding="utf-8")
    assert "SECRET123" not in correction_text and "<REDACTED>" in correction_text
    correction_payload = json.loads(correction_text)
    assert {t["task_origin"] for t in correction_payload["tasks"]} == {"user-correction"}
    assert all(t["production_gate_eligible"] is False for t in correction_payload["tasks"])

    context = ingest_skill_context_eval_seed(skill="demo", context="Created to verify safe tool use boundaries", output=tmp_path / "skillopt" / "evals" / "demo-context.json", hermes_home_path=str(tmp_path))
    assert context["review_only"] is True
    negative = generate_negative_boundary_eval_pack(skill="demo", output=tmp_path / "skillopt" / "evals" / "demo-negative.json", hermes_home_path=str(tmp_path))
    assert negative["mode"] == "negative_boundary_eval_pack_review_only"
    assert negative["report"]["production_eligible"] is False

    draft = eval_pack_autopilot(skill="demo", output=tmp_path / "skillopt" / "evals" / "demo-autopilot.json", hermes_home_path=str(tmp_path), write_draft=True)
    assert draft["review_only"] is True and draft["auto_adopt"] is False
    promoted_path = tmp_path / "skillopt" / "evals" / "demo-promoted-review.json"
    promoted = promote_eval_pack(skill="demo", input_path=draft["draft"]["output_path"], output=promoted_path, hermes_home_path=str(tmp_path))
    assert promoted["mode"] == "eval_pack_promote_curated_review_default"
    assert promoted["review_only"] is True
    promoted_payload = json.loads(promoted_path.read_text(encoding="utf-8"))
    assert promoted_payload["production_policy"]["allow_production_adoption"] is False
    assert all(t["task_origin"] == "curated-review-promotion" for t in promoted_payload["tasks"])
    with pytest.raises(ValueError, match="production promotion requires explicit"):
        promote_eval_pack(skill="demo", input_path=draft["draft"]["output_path"], output=tmp_path / "skillopt" / "evals" / "bad-prod.json", hermes_home_path=str(tmp_path), production=True)


def test_eval_pack_inventory_readiness_matrix_covers_pack_states(tmp_path):
    for name, body in {
        "none": "General helper skill.",
        "bad": "Use policy and safety guardrails.",
        "review": "Runbook: use terminal commands and verify outputs.",
        "prod": "Review code diffs, run pytest, and verify builds.",
    }.items():
        make_skill(tmp_path, name).write_text(f"---\nname: {name}\ndescription: {body}\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    write_invalid_eval(tmp_path, "bad")
    write_review_eval(tmp_path, "review")
    write_production_eval(tmp_path, "prod")

    inv = eval_pack_inventory(hermes_home_path=str(tmp_path))
    matrix = inv["readiness_matrix"]
    by_skill = {row["skill"]: row for row in inv["skills"]}

    assert inv["total_skills"] == 4
    assert matrix["total_skills"] == 4
    assert matrix["no_pack_count"] == 1
    assert matrix["only_review_only_count"] == 1
    assert matrix["production_eligible_count"] == 1
    assert matrix["invalid_pack_count"] == 1
    assert matrix["split_completeness"] == {"complete": 2, "incomplete": 0, "invalid": 1, "no_pack": 1}
    assert matrix["execution_contract_buckets"]["invalid_pack"] == 1
    assert matrix["execution_contract_buckets"]["static_review_only"] == 1
    assert matrix["execution_contract_buckets"]["deterministic_replay_contract_compliant"] == 1
    assert by_skill["none"]["recommended_next_action"] == "scaffold_review_eval_pack"
    assert by_skill["bad"]["recommended_next_action"] == "fix_invalid_eval_pack"
    assert by_skill["review"]["recommended_next_action"] == "replace_static_review_pack_with_curated_replay_or_frozen_target_contract"
    assert by_skill["prod"]["recommended_next_action"] == "ready_for_strict_eval_run"
    assert by_skill["prod"]["production_eligible"] is True
    assert by_skill["bad"]["invalid_eval_pack_count"] == 1
    assert by_skill["prod"]["skill_type"]["category"] == "software_development_reviewer"
    assert matrix["safety_invariants"]["sample_static_session_mined_data_can_gate_production"] is False


def test_skill_type_classifier_examples(tmp_path):
    examples = {
        "safety": ("security-reviewer", "Safety policy governance risk approval rollback.", "safety_governance"),
        "runbook": ("ops-runbook", "Use terminal command workflow checklist for troubleshooting.", "tool_runbook"),
        "coder": ("python-coder", "Review repository diffs, patch code, run pytest and build.", "software_development_reviewer"),
        "writer": ("creative-writer", "Research an outline and draft a story narrative.", "research_writer_creative"),
        "domain": ("legal-domain", "Legal finance domain expert analysis with caveats.", "domain_specialist"),
        "plain": ("plain", "Answer clearly and helpfully.", "general"),
    }
    for name, (dirname, body, expected) in examples.items():
        path = tmp_path / "skills" / dirname / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {body}\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    skills = {s.name: s for s in core.discover_skills(tmp_path)}
    for name, (_dirname, _body, expected) in examples.items():
        result = classify_skill_type(skills[name])
        assert result["category"] == expected
        assert result["advisory_only"] is True
        assert result["hard_gate"] is False
        assert result["templates"]
        assert result["reasons"]


def test_cli_help_and_plugin_metadata_include_p1_p2_tools():
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--help"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    assert proc.returncode == 0, proc.stdout
    assert "batch-preflight" in proc.stdout
    assert "eval-pack-scaffold" in proc.stdout
    assert "eval-pack-autopilot" in proc.stdout
    assert "eval-pack-doctor" in proc.stdout
    assert "eval-pack-curate" in proc.stdout
    assert "eval-pack-mine-sessions" in proc.stdout
    assert "eval-pack-ingest-correction" in proc.stdout
    assert "eval-pack-ingest-context" in proc.stdout
    assert "eval-pack-negative-boundary" in proc.stdout
    assert "eval-pack-promote" in proc.stdout
    assert "fleet-report" in proc.stdout
    assert "fleet-resume-plan" in proc.stdout
    assert "fleet-rollback-plan" in proc.stdout
    assert "artifact-hygiene-report" in proc.stdout
    full_help = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "full-run", "--help"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    assert full_help.returncode == 0, full_help.stdout
    for text in ("--optimizer-backend", "--target-backend", "--gate-mode", "--allow-mock", "--candidate-count", "--edit-budget", "capable default", "judge explanation"):
        assert text in full_help.stdout

    spec = importlib.util.spec_from_file_location("skillopt_plugin_root_p1p2", repo / "__init__.py")
    assert spec and spec.loader
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    provided = [line.strip()[2:] for line in (repo / "plugin.yaml").read_text(encoding="utf-8").splitlines() if line.strip().startswith("- ")]
    registered = [name for name, _schema, _handler, _emoji in plugin._TOOLS]
    assert provided == registered
    assert {"hermes_skillopt_batch_preflight", "hermes_skillopt_batch_run", "hermes_skillopt_eval_pack_inventory", "hermes_skillopt_eval_pack_doctor", "hermes_skillopt_eval_pack_autopilot", "hermes_skillopt_eval_pack_scaffold", "hermes_skillopt_eval_pack_curate", "hermes_skillopt_eval_pack_mine_sessions", "hermes_skillopt_eval_pack_ingest_correction", "hermes_skillopt_eval_pack_ingest_context", "hermes_skillopt_eval_pack_negative_boundary", "hermes_skillopt_eval_pack_promote"}.issubset(registered)
    assert {"hermes_skillopt_fleet_report", "hermes_skillopt_fleet_resume_plan", "hermes_skillopt_fleet_rollback_plan", "hermes_skillopt_artifact_hygiene_report"}.issubset(registered)


def test_fleet_report_resume_and_rollback_plans_are_read_only(tmp_path):
    make_skill(tmp_path, "demo")
    run = stage_review_fixture(tmp_path, "demo")
    incomplete_dir = tmp_path / "skillopt" / "staging" / "incomplete-demo"
    incomplete_dir.mkdir(parents=True)
    cp = {"status": "running", "input": {"skill_name": "demo", "skill_relpath": "skills/demo/SKILL.md"}, "input_sha256": "abc", "completed_stages": ["rollout"]}
    (incomplete_dir / "checkpoint.json").write_text(json.dumps(cp), encoding="utf-8")

    adopted_dir = tmp_path / "skillopt" / "staging" / "adopted-demo"
    adopted_dir.mkdir(parents=True)
    (adopted_dir / "proposed_SKILL.md").write_text("proposed", encoding="utf-8")
    (adopted_dir / "report.md").write_text("report", encoding="utf-8")
    backup_dir = tmp_path / "skillopt" / "backups" / "backup-adopted-demo"
    backup_dir.mkdir(parents=True)
    (backup_dir / "SKILL.md").write_text("original", encoding="utf-8")
    (backup_dir / "manifest.json").write_text(json.dumps({"run_id": "adopted-demo"}), encoding="utf-8")
    manifest = {"run_id": "adopted-demo", "status": "adopted", "adoptable": True, "skill_name": "demo", "skill_relpath": "skills/demo/SKILL.md", "backup_dir": str(backup_dir), "gate_policy": {"mode": "strict"}, "production_gate_eligible": True, "test_gate_eligible": True, "split_scores": {"validation": {"current": 0.4, "candidate": 0.8}}, "files": {"proposed": "proposed_SKILL.md", "report": "report.md"}}
    manifest["artifact_sha256"] = core.artifact_hashes(adopted_dir, manifest["files"])
    core.save_manifest(adopted_dir, manifest)

    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    report = core.fleet_report(str(tmp_path), limit=20)
    resume = core.fleet_resume_plan(str(tmp_path), limit=20)
    rollback = core.fleet_rollback_plan(str(tmp_path), limit=20)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    assert before == after
    assert report["mode"].startswith("read_only_report")
    assert any(r["run_id"] == run["run_id"] for r in report["latest_runs"])
    assert any(r["run_id"] == "incomplete-demo" for r in report["incomplete_or_checkpoint_only"])
    refused = {r["run_id"]: r for r in resume["refused_incomplete_or_partial"]}
    assert refused["incomplete-demo"]["partial_continuation_available"] is False
    assert "partial-stage continuation is refused" in " ".join(refused["incomplete-demo"]["refusal_reasons"])
    assert rollback["bulk_rollback_available"] is False
    assert any(r["run_id"] == "adopted-demo" for r in rollback["rollbackable_adopted_runs"])


def test_fleet_report_warns_on_tampered_manifest(tmp_path):
    make_skill(tmp_path, "demo")
    run = stage_review_fixture(tmp_path, "demo")
    run_dir = tmp_path / "skillopt" / "staging" / run["run_id"]
    (run_dir / "report.md").write_text("tampered", encoding="utf-8")
    report = core.fleet_report(str(tmp_path))
    row = next(r for r in report["latest_runs"] if r["run_id"] == run["run_id"])
    assert row["fingerprints"]["manifest_artifacts_verified"] is False
    assert "tampered" in " ".join(report["warnings"] + row["warnings"])


def test_artifact_hygiene_report_classifies_verified_tampered_checkpoint_stale_and_orphaned(tmp_path):
    make_skill(tmp_path, "demo")
    verified = stage_review_fixture(tmp_path, "demo")
    verified_dir = tmp_path / "skillopt" / "staging" / verified["run_id"]
    assert verified_dir.is_dir()

    tampered = stage_review_fixture(tmp_path, "demo")
    tampered_dir = tmp_path / "skillopt" / "staging" / tampered["run_id"]
    (tampered_dir / "report.md").write_text("tampered", encoding="utf-8")

    recent_dir = tmp_path / "skillopt" / "staging" / "recent-cp"
    recent_dir.mkdir(parents=True)
    (recent_dir / "checkpoint.json").write_text(json.dumps({"status": "running", "input": {"skill_name": "demo"}}), encoding="utf-8")

    stale_dir = tmp_path / "skillopt" / "staging" / "stale-cp"
    stale_dir.mkdir(parents=True)
    (stale_dir / "checkpoint.json").write_text(json.dumps({"status": "running", "input": {"skill_name": "demo"}}), encoding="utf-8")
    old = 1_600_000_000
    os.utime(stale_dir / "checkpoint.json", (old, old))
    os.utime(stale_dir, (old, old))

    batch_dir = tmp_path / "skillopt" / "staging" / "batch-orphan"
    batch_dir.mkdir(parents=True)
    (batch_dir / "jobs.json").write_text(json.dumps([{"run_id": "missing-child"}]), encoding="utf-8")
    batch_manifest = {"schema_version": "hermes-skillopt-batch-run-v1", "batch_id": "batch-orphan", "files": {"jobs": "jobs.json"}}
    batch_manifest["artifact_sha256"] = core.artifact_hashes(batch_dir, batch_manifest["files"])
    core.save_manifest(batch_dir, batch_manifest)

    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    report = core.artifact_hygiene_report(str(tmp_path), stale_after_hours=1, limit=20)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after
    by_id = {row["run_id"]: row for row in report["runs"]}
    assert by_id[verified["run_id"]]["classification"] == "complete_verified"
    assert by_id[tampered["run_id"]]["classification"] == "tampered_hash_mismatch"
    assert by_id[tampered["run_id"]]["partial_continuation_available"] is False
    assert "Do not adopt or reuse" in by_id[tampered["run_id"]]["next_safe_action"]
    assert by_id["recent-cp"]["classification"] == "checkpoint_only_recent"
    assert by_id["recent-cp"]["partial_continuation_available"] is False
    assert by_id["stale-cp"]["classification"] == "stale_incomplete_checkpoint_only"
    assert "new full run" in by_id["stale-cp"]["next_safe_action"]
    assert by_id["batch-orphan"]["classification"] == "orphaned_batch_child"
    assert "missing children" in by_id["batch-orphan"]["next_safe_action"]
    assert report["mode"].startswith("read_only_artifact_hygiene_report")
    assert "does not delete" in report["read_only_guards"]


def test_batch_policy_profiles_reject_thresholds_and_production_unsafe(tmp_path):
    make_skill(tmp_path, "demo")
    review_eval = write_review_eval(tmp_path, "demo")
    plan = {
        "schema_version": "hermes-skillopt-batch-plan-v1",
        "policy_profile": "production_strict",
        "jobs": [{"skill": "demo", "eval_file": str(review_eval), "iterations": 4, "candidate_count": 1, "edit_budget": 1, "limit": 10, "optimizer_backend": "mock", "allow_mock": True, "target_executor": "scorecard", "gate_mode": "soft"}],
    }
    with pytest.raises(ValueError) as exc:
        batch_preflight(plan, hermes_home_path=str(tmp_path))
    msg = str(exc.value)
    assert "iterations 4 exceeds policy production_strict cap 3" in msg
    assert "production intent rejects allow_mock" in msg
    assert "production intent rejects mock optimizer_backend" in msg
    assert "requires strict gate_mode" in msg
    assert "requires enabled live-readonly" in msg
    assert "production-ready curated eval pack" in msg


def test_batch_policy_profiles_accept_review_and_production_ready(tmp_path):
    make_skill(tmp_path, "demo")
    review_eval = write_review_eval(tmp_path, "demo")
    review_report = batch_preflight({"schema_version": "hermes-skillopt-batch-plan-v1", "policy_profile": "review_small", "jobs": [{"skill": "demo", "eval_file": str(review_eval), "iterations": 2, "candidate_count": 2, "edit_budget": 4, "limit": 50, "optimizer_backend": "mock", "allow_mock": True, "target_executor": "scorecard", "gate_mode": "soft"}]}, hermes_home_path=str(tmp_path))
    assert review_report["policy_profile"] == "review_small"
    assert review_report["threshold_decisions"][0]["accepted"] is True
    assert review_report["threshold_decisions"][0]["production_intent"] is False

    prod_eval = write_production_eval(tmp_path, "demo")
    prod_report = batch_preflight({"schema_version": "hermes-skillopt-batch-plan-v1", "policy_profile": "production_strict", "jobs": [{"skill": "demo", "eval_file": str(prod_eval), "iterations": 1, "candidate_count": 1, "edit_budget": 1, "limit": 10, "optimizer_backend": "hermes", "allow_mock": False, "target_executor": "live-readonly", "gate_mode": "strict"}]}, hermes_home_path=str(tmp_path))
    assert prod_report["success"] is True
    assert prod_report["threshold_decisions"][0]["production_intent"] is True
    assert prod_report["threshold_decisions"][0]["accepted"] is True
