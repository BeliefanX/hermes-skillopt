from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import core
from hermes_skillopt.batch import batch_preflight, run_batch
from hermes_skillopt.eval_packs import eval_pack_inventory, scaffold_eval_pack


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


def test_batch_preflight_rejects_writeback_and_budget_and_prod_missing_eval(tmp_path):
    plan = {
        "schema_version": "hermes-skillopt-batch-plan-v1",
        "budget": {"max_jobs": 1, "max_total_iterations": 1, "max_total_candidates": 1},
        "jobs": [
            {"skill": "demo", "iterations": 2, "candidate_count": 2, "force": True, "backend": "hermes", "gate_mode": "strict"},
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

    out_path = tmp_path / "skillopt" / "evals" / "demo-scaffold.json"
    out = scaffold_eval_pack(skill="demo", output=out_path, hermes_home_path=str(tmp_path))
    assert out["success"] is True
    assert out["review_only"] is True
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["production_policy"]["allow_production_adoption"] is False
    assert {t["split"] for t in payload["tasks"]} == {"train", "validation", "test"}
    assert all(t["production_gate_eligible"] is False for t in payload["tasks"])


def test_cli_help_and_plugin_metadata_include_p1_p2_tools():
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--help"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    assert proc.returncode == 0, proc.stdout
    assert "batch-preflight" in proc.stdout
    assert "eval-pack-scaffold" in proc.stdout
    assert "fleet-report" in proc.stdout
    assert "fleet-resume-plan" in proc.stdout
    assert "fleet-rollback-plan" in proc.stdout
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
    assert {"hermes_skillopt_batch_preflight", "hermes_skillopt_batch_run", "hermes_skillopt_eval_pack_inventory", "hermes_skillopt_eval_pack_scaffold"}.issubset(registered)
    assert {"hermes_skillopt_fleet_report", "hermes_skillopt_fleet_resume_plan", "hermes_skillopt_fleet_rollback_plan"}.issubset(registered)


def test_fleet_report_resume_and_rollback_plans_are_read_only(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
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
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    run_dir = tmp_path / "skillopt" / "staging" / run["run_id"]
    (run_dir / "report.md").write_text("tampered", encoding="utf-8")
    report = core.fleet_report(str(tmp_path))
    row = next(r for r in report["latest_runs"] if r["run_id"] == run["run_id"])
    assert row["fingerprints"]["manifest_artifacts_verified"] is False
    assert "tampered" in " ".join(report["warnings"] + row["warnings"])
