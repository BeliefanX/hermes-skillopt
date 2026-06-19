from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import core


def make_skill(home: Path, name: str = "demo") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\nUse tools safely.\n", encoding="utf-8")
    return p


def load_plugin_module():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_skillopt_plugin_guided_ux", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def active_tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_doctor_is_read_only_for_fresh_home(tmp_path):
    out = core.doctor(hermes_home_path=str(tmp_path))

    assert out["success"] is True
    assert out["mode"] == "read_only_doctor_no_full_run_no_adopt_no_rollback_no_fetch"
    assert out["upstream_parity_posture"]["full_parity_claim"] is False
    assert "production" in out["recommended_next_action"]
    assert not (tmp_path / "skillopt").exists()


def test_scout_is_read_only_notification_summary_with_metadata(tmp_path):
    p = make_skill(tmp_path, "demo")
    p.write_text("---\nname: demo\ndescription: test\npinned: true\nsource: hermes hub custom\n---\n# demo\n\nUse tools safely.\n", encoding="utf-8")
    refs = p.parent / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("short reference\n", encoding="utf-8")
    (p.parent / "assets").symlink_to(tmp_path)

    out = core.scout(hermes_home_path=str(tmp_path), skill="demo")

    assert out["success"] is True
    assert out["schema_version"] == "hermes-skillopt-scout-v1"
    assert out["mode"] == "read_only_scout_no_full_run_no_optimize_no_adopt_no_rollback_no_fetch"
    assert out["summary"]["skills_count"] == 1
    assert out["summary"]["pinned_or_archived_skipped_by_default"] == ["demo"]
    assert out["skills_metadata"]["demo"]["advisory_only"] is True
    assert out["skills_metadata"]["demo"]["signals"]["pinned"] is True
    package = out["skills_metadata"]["demo"]["package_support"]
    assert package["content_included"] is False
    assert package["support_dirs"]["references"]["file_count"] == 1
    assert package["support_dirs"]["references"]["files"][0]["relpath"].endswith("references/guide.md")
    assert package["support_dirs"]["assets"]["unsafe"] is True
    assert any("escapes" in w or "symlinked" in w for w in package["warnings"])
    assert out["cron_recommendation"]["create_cron_job"] is False
    assert out["cron_recommendation"]["auto_adopt_from_cron"] is False
    assert any(a["action"] == "create_or_curate_eval_pack" for a in out["next_actions"])
    assert "eval-pack-scaffold --skill demo" in out["safe_next_commands"]["create_or_curate_eval_pack"]
    assert out["report_path"] is None
    assert not (tmp_path / "skillopt").exists()


def test_scout_optional_report_path_is_guarded(tmp_path):
    make_skill(tmp_path, "demo")
    report = tmp_path / "skillopt" / "reports" / "scout.json"

    out = core.scout(hermes_home_path=str(tmp_path), output_path=str(report))

    assert out["report_path"] == str(report.resolve())
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["schema_version"] == "hermes-skillopt-scout-v1"

    with pytest.raises(ValueError, match="output_path"):
        core.scout(hermes_home_path=str(tmp_path), output_path=str(tmp_path / "skills" / "demo" / "live.json"))


def test_scout_does_not_call_mutating_entrypoints(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo")

    def forbidden(*args, **kwargs):
        raise AssertionError("mutating entrypoint must not be called by scout")

    for name in ("full_run", "guided_optimize", "adopt", "rollback", "upstream_update"):
        monkeypatch.setattr(core, name, forbidden)

    out = core.scout(hermes_home_path=str(tmp_path), skill="demo")
    assert out["success"] is True
    assert "does not call full_run" in out["read_only_guards"]


def test_guided_optimize_production_fails_fast_without_explicit_eval(tmp_path):
    make_skill(tmp_path)

    with pytest.raises(ValueError, match="production intent requires explicit --eval-file"):
        core.guided_optimize(intent="production", skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)

    assert not (tmp_path / "skillopt").exists()


def test_guided_optimize_smoke_is_review_only_and_staged(tmp_path):
    make_skill(tmp_path)

    out = core.guided_optimize(intent="smoke", skill="demo", hermes_home_path=str(tmp_path))

    assert out["success"] is True
    assert out["intent"] == "smoke"
    assert out["auto_adopt"] is False
    assert out["adoptable"] is False
    assert "review-only" in out["review_only_label"]
    assert (Path(out["run_dir"]) / "manifest.json").exists()


def test_review_latest_and_summary(tmp_path):
    make_skill(tmp_path)
    out = core.guided_optimize(intent="smoke", skill="demo", hermes_home_path=str(tmp_path))

    latest = core.review_latest(hermes_home_path=str(tmp_path), slim=True)
    summary = core.review_decision_summary("latest", hermes_home_path=str(tmp_path))

    assert latest["run_id"] == out["run_id"]
    assert latest["slim"] is True
    assert summary["run_id"] == out["run_id"]
    assert summary["decision"] in {"review_only_not_adoptable", "not_ready_rejected_or_incomplete", "ready_for_explicit_adopt"}
    assert summary["production_gate_eligible"] is False
    assert summary["test_gate_eligible"] is False
    assert summary["evidence_class"] == "review_only_or_not_ready"
    assert summary["score_provenance"]["schema_version"] == "hermes-skillopt-score-provenance-v1"
    assert summary["score_provenance"]["score_source"] == "mock_review_only"
    assert summary["score_provenance"]["split_labels"]["heldout_final_gate"] == "test"
    assert summary["blockers"] == summary["not_adoptable_reasons"]
    assert summary["next_action"]
    assert summary["next_safe_action"] == summary["next_action"]
    for key in ("validation_gate", "production_best_gate", "heldout_test_gate", "review_only", "warnings", "readiness_adoptability"):
        assert key in latest
        assert key in summary
    assert summary["readiness_adoptability"]["adoptable"] is False
    assert "missing accepted explicit curated production validation gate" in summary["blockers"]

    status = core.status(str(tmp_path))
    assert status["recent_runs"][0]["readiness_adoptability"]["schema_version"] == "hermes-skillopt-readiness-adoptability-v1"
    assert status["recent_runs"][0]["evidence_class"] == "review_only_or_not_ready"
    assert status["tool_safety"]["schema_version"] == "hermes-skillopt-tool-safety-v1"

    digest = core.review_digest("latest", hermes_home_path=str(tmp_path))
    assert digest["schema_version"] == "hermes-skillopt-review-digest-v1"
    assert "report_summary" not in digest
    assert "diff_preview" not in digest
    assert "score_provenance:" in digest["digest"]
    assert "evidence_class: review_only_or_not_ready" in digest["digest"]
    assert "eval_pack:" in digest["digest"]
    assert "next_safe_action:" in digest["digest"]

    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "review", "--digest"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    cli_digest = json.loads(proc.stdout)
    assert cli_digest["schema_version"] == "hermes-skillopt-review-digest-v1"
    assert "report_summary" not in cli_digest
    assert "diff_preview" not in cli_digest


def test_artifact_hygiene_classifies_verified_tampered_and_checkpoint_with_safe_actions(tmp_path):
    make_skill(tmp_path)
    complete = core.guided_optimize(intent="smoke", skill="demo", hermes_home_path=str(tmp_path))
    checkpoint_dir = tmp_path / "skillopt" / "staging" / "checkpoint-only"
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "checkpoint.json").write_text(json.dumps({"status": "reflect", "input": {"skill_name": "demo"}, "input_sha256": "abc"}), encoding="utf-8")
    old = 946684800
    os.utime(checkpoint_dir, (old, old))

    tampered_dir = tmp_path / "skillopt" / "staging" / "tampered"
    tampered_dir.mkdir(parents=True)
    for name, text in {"report.md": "report\n", "diff.patch": "diff\n", "evidence.json": "{}\n", "original_SKILL.md": "a\n", "proposed_SKILL.md": "b\n"}.items():
        (tampered_dir / name).write_text(text, encoding="utf-8")
    manifest = {"run_id": "tampered", "status": "staged", "adoptable": False, "skill_name": "demo", "backend": "mock", "optimizer_backend": "mock", "files": {"report": "report.md", "diff": "diff.patch", "evidence": "evidence.json", "original": "original_SKILL.md", "proposed": "proposed_SKILL.md"}}
    manifest["artifact_sha256"] = core.artifact_hashes(tampered_dir, manifest["files"])
    core.save_manifest(tampered_dir, manifest)
    (tampered_dir / "report.md").write_text("tampered\n", encoding="utf-8")

    hygiene = core.artifact_hygiene_report(str(tmp_path), stale_after_hours=0.001)
    by_id = {row["run_id"]: row for row in hygiene["runs"]}

    assert by_id[complete["run_id"]]["classification"] == "complete_verified"
    assert by_id[complete["run_id"]]["partial_continuation_available"] is False
    assert by_id[complete["run_id"]]["score_provenance"]["schema_version"] == "hermes-skillopt-score-provenance-v1"
    assert by_id["checkpoint-only"]["classification"] == "stale_incomplete_checkpoint_only"
    assert by_id["checkpoint-only"]["partial_continuation_available"] is False
    assert "new full run" in by_id["checkpoint-only"]["next_safe_action"]
    assert by_id["tampered"]["classification"] == "tampered_hash_mismatch"
    assert "Do not adopt or reuse" in by_id["tampered"]["next_safe_action"]
    assert "tampered_hash_mismatch" in hygiene["classification_counts"]

    status = core.status(str(tmp_path))
    status_by_id = {row["run_id"]: row for row in status["recent_runs"]}
    assert status_by_id["tampered"]["artifact_classification"] == "tampered_hash_mismatch"
    assert status_by_id["tampered"]["safe_reuse_completed"] is False
    assert "Do not adopt or reuse" in status_by_id["tampered"]["next_safe_action"]

    scout = core.scout(hermes_home_path=str(tmp_path), skill="demo")
    scout_by_id = {row["run_id"]: row for row in scout["recent_runs"]}
    assert scout_by_id["tampered"]["artifact_classification"] == "tampered_hash_mismatch"
    assert scout_by_id["tampered"]["artifact_classification"] != "complete_verified"


def test_scout_safe_next_commands_quote_dynamic_home_and_skill(tmp_path):
    home = tmp_path / "home with spaces;touch pwned"
    make_skill(home, "demo odd;$(touch pwned)")

    out = core.scout(hermes_home_path=str(home), skill="demo odd;$(touch pwned)")

    scout_cmd = out["safe_next_commands"]["scout"]
    scaffold_cmd = out["safe_next_commands"]["create_or_curate_eval_pack"]
    assert str(home) in shlex.split(scout_cmd)
    assert "demo odd;$(touch pwned)" in shlex.split(scout_cmd)
    assert str(home) in shlex.split(scaffold_cmd)
    assert "demo odd;$(touch pwned)" in shlex.split(scaffold_cmd)
    assert f"--home {str(home)}" not in scout_cmd
    assert "--skill demo odd;$(touch pwned)" not in scout_cmd


def test_scout_mixed_production_ready_and_missing_eval_pack_does_not_crash(tmp_path):
    make_skill(tmp_path, "prod")
    make_skill(tmp_path, "missing odd;$(touch pwned)")
    from hermes_skillopt.eval_packs import create_curated_eval_pack

    create_curated_eval_pack(
        hermes_home_path=str(tmp_path),
        skill="prod",
        production_policy={"allow_production_adoption": True, "reviewed_by": "unit-test"},
        tasks=[
            {"id": "train-1", "split": "train", "prompt": "Train tool safety.", "expected_keywords": ["tool"], "production_gate_eligible": False},
            {"id": "val-1", "split": "validation", "prompt": "Validate grounded tool safety.", "expected_keywords": ["grounded", "tool"], "production_gate_eligible": True},
            {"id": "test-1", "split": "test", "prompt": "Held-out rollback guard test.", "expected_keywords": ["rollback", "guard"], "production_gate_eligible": True},
        ],
    )

    out = core.scout(hermes_home_path=str(tmp_path), limit=2)

    assert out["success"] is True
    assert out["summary"]["production_eligible_eval_pack_count"] == 1
    assert out["summary"]["no_eval_pack_count"] == 1
    assert any(a["action"] == "create_or_curate_eval_pack" for a in out["next_actions"])
    assert any(a["action"] == "run_production_candidate_only_when_eligible" for a in out["next_actions"])
    scaffold_cmd = out["safe_next_commands"]["create_or_curate_eval_pack"]
    production_cmd = out["safe_next_commands"]["production_candidate_when_eligible"]
    assert "missing odd;$(touch pwned)" in shlex.split(scaffold_cmd)
    assert "prod" in shlex.split(production_cmd)
    assert "--skill missing odd;$(touch pwned)" not in scaffold_cmd
    assert out["report_path"] is None


def test_scout_safe_next_commands_quote_latest_run_id(tmp_path):
    make_skill(tmp_path, "demo")
    malicious_run_id = "zz;touch pwned"
    run_dir = tmp_path / "skillopt" / "staging" / malicious_run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": malicious_run_id,
                "status": "staged",
                "skill_name": "demo",
                "created_at": "2026-01-01T00:00:00+00:00",
                "adoptable": False,
                "production_gate_eligible": False,
                "test_gate_eligible": False,
            }
        ),
        encoding="utf-8",
    )

    out = core.scout(hermes_home_path=str(tmp_path), skill="demo")

    cmd = out["safe_next_commands"]["review_latest_staged_run"]
    parts = shlex.split(cmd)
    assert out["summary"]["latest_run_id"] == malicious_run_id
    assert parts[parts.index("review") + 1] == malicious_run_id
    assert parts.count(malicious_run_id) == 1
    assert "review zz;touch pwned" not in cmd


def test_cli_adopt_requires_typed_confirmation_before_core_gate(tmp_path):
    cmd = [sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "adopt", "missing-run", "--yes-i-understand-skillopt-adopt"]
    bypass = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert bypass.returncode != 2

    no_confirm = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "adopt", "missing-run"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert no_confirm.returncode == 2
    assert "ADOPT missing-run" in no_confirm.stderr


def test_plugin_registers_doctor_optimize_and_enforces_adopt_confirmation(monkeypatch):
    plugin = load_plugin_module()

    assert "hermes_skillopt_doctor" in plugin.SCHEMAS
    assert "hermes_skillopt_scout" in plugin.SCHEMAS
    assert plugin.SCHEMAS["hermes_skillopt_scout"]["parameters"]["properties"]["output"]["type"] == "string"
    assert "hermes_skillopt_optimize" in plugin.SCHEMAS
    assert plugin.SCHEMAS["hermes_skillopt_optimize"]["parameters"]["properties"]["intent"]["enum"] == ["smoke", "review", "production"]
    assert "confirmation" in plugin.SCHEMAS["hermes_skillopt_adopt"]["parameters"]["properties"]
    assert plugin.SCHEMAS["hermes_skillopt_scout"]["x-hermes-skillopt-safety"]["safety_group"] == "read_only"
    assert plugin.SCHEMAS["hermes_skillopt_scout"]["x-hermes-skillopt-safety"]["cron_safe"] is True
    assert plugin.SCHEMAS["hermes_skillopt_optimize"]["x-hermes-skillopt-safety"]["safety_group"] == "stage_artifacts"
    assert plugin.SCHEMAS["hermes_skillopt_optimize"]["x-hermes-skillopt-safety"]["cron_safe"] is False
    assert plugin.SCHEMAS["hermes_skillopt_adopt"]["risk_level"] == "high"
    assert plugin.TOOL_SAFETY_CATALOG["groups"]["writeback"]["scheduled_default"] is False
    assert "never cron optimize" in plugin.TOOL_SAFETY_CATALOG["scheduled_default_guidance"]
    registered = {name for name, *_ in plugin._TOOLS}
    assert set(plugin.core.TOOL_SAFETY_METADATA) == registered
    assert "digest" in plugin.SCHEMAS["hermes_skillopt_review"]["parameters"]["properties"]
    rollback_confirm = plugin.SCHEMAS["hermes_skillopt_rollback"]["parameters"]["properties"]["confirmation"]
    assert "ROLLBACK <run_id>" in rollback_confirm["description"]

    raw = plugin._handle_adopt({"run_id": "abc"})
    denied = json.loads(raw)
    assert denied.get("success") is False or "error" in denied
    assert "ADOPT abc" in denied.get("error", json.dumps(denied))

    seen = {}

    def fake_adopt(**kw):
        seen["kw"] = kw
        return {"success": True}

    monkeypatch.setattr(plugin.core, "adopt", fake_adopt)
    raw = plugin._handle_adopt({"run_id": "abc", "confirmation": "ADOPT abc"})
    assert json.loads(raw)["success"] is True
    assert seen["kw"]["run_id"] == "abc"

    raw = plugin._handle_rollback({"run_id": "abc"})
    denied_rollback = json.loads(raw)
    assert denied_rollback.get("success") is False or "error" in denied_rollback
    assert "ROLLBACK abc" in denied_rollback.get("error", json.dumps(denied_rollback))


def test_scheduled_default_safety_metadata_is_conservative(tmp_path):
    plugin = load_plugin_module()

    scheduled_defaults = {
        "hermes_skillopt_scout",
        "hermes_skillopt_doctor",
        "hermes_skillopt_eval_pack_inventory",
        "hermes_skillopt_eval_pack_doctor",
    }
    digest_only_defaults = {"hermes_skillopt_review"}
    manual_read_only_surfaces = {"hermes_skillopt_eval_pack_workflow", "hermes_skillopt_skill_readiness_queue"}
    manual_non_cron_surfaces = {
        "hermes_skillopt_eval_pack_workflow",
        "hermes_skillopt_skill_readiness_queue",
        "hermes_skillopt_eval_pack_autopilot",
        "hermes_skillopt_skill_quality",
    }

    cron_safe_true = {name for name, meta in plugin.core.TOOL_SAFETY_METADATA.items() if meta.get("cron_safe") is True}
    assert cron_safe_true == scheduled_defaults
    for name in digest_only_defaults:
        assert plugin.core.TOOL_SAFETY_METADATA[name]["cron_safe"] == "digest_only"
    for name in manual_read_only_surfaces:
        meta = plugin.SCHEMAS[name]["x-hermes-skillopt-safety"]
        assert meta["safety_group"] == "read_only"
        assert meta["cron_safe"] is False
        assert meta["manual_surface"] is True
    for name in manual_non_cron_surfaces:
        meta = plugin.SCHEMAS[name]["x-hermes-skillopt-safety"]
        assert meta["cron_safe"] is False
        assert meta["manual_surface"] is True

    guidance = plugin.TOOL_SAFETY_CATALOG["scheduled_default_guidance"]
    assert "eval-pack-doctor" in guidance
    assert "eval-pack-autopilot" in guidance
    assert "skill-quality" in guidance
    default_clause = guidance.split("surfaces", 1)[0]
    assert "eval-pack-workflow" not in default_clause
    assert "skill-readiness-queue" not in default_clause
    read_only_description = plugin.TOOL_SAFETY_CATALOG["groups"]["read_only"]["description"]
    assert "eval-pack-doctor" in read_only_description
    assert "workflow/queue surfaces are manual" in read_only_description

    plugin_yaml = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
    assert "Schedule only scout, doctor, eval-pack-inventory, eval-pack-doctor, or review --digest" in plugin_yaml
    assert "workflow/queue surfaces are manual" in plugin_yaml

    make_skill(tmp_path, "demo")
    scout = plugin.core.scout(hermes_home_path=str(tmp_path), skill="demo")
    cron = scout["cron_recommendation"]
    assert cron["allowed_cron_surfaces"] == ["scout", "doctor", "eval-pack-inventory", "eval-pack-doctor", "review --digest"]
    assert cron["manual_read_only_surfaces"] == ["eval-pack-workflow", "skill-readiness-queue"]
    assert "eval-pack-workflow" not in cron["allowed_cron_surfaces"]
    assert "skill-readiness-queue" not in cron["allowed_cron_surfaces"]


def test_cli_optimize_production_refusal_is_clear(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "optimize", "--intent", "production", "--skill", "demo"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 2
    assert "optimize refused: production intent requires explicit --eval-file" in proc.stderr


def test_cli_scout_returns_slim_json_without_writing_default(tmp_path):
    make_skill(tmp_path)
    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "scout", "--limit", "2"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "hermes-skillopt-scout-v1"
    assert payload["report_path"] is None
    assert payload["safe_next_commands"]["scout"].endswith("scout")
    assert not (tmp_path / "skillopt").exists()


def test_cli_optimize_review_defaults_to_review_only_mock_allowed(tmp_path):
    make_skill(tmp_path)

    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "optimize", "--intent", "review", "--skill", "demo"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["success"] is True
    assert payload["intent"] == "review"
    assert payload["auto_adopt"] is False
    assert payload["adoptable"] is False
    assert "review-only" in payload["review_only_label"]


def test_cli_help_smoke_lists_guided_commands(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--help"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 0
    assert "doctor" in proc.stdout
    assert "scout" in proc.stdout
    assert "optimize" in proc.stdout
