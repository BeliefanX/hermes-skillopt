from __future__ import annotations

import json
from pathlib import Path

from hermes_skillopt import core
from hermes_skillopt.env import EvalTask
from hermes_skillopt.target import HermesSandboxRunner, TargetExecutor


def make_skill(home: Path, name: str = "demo") -> None:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\nUse tools safely.\n", encoding="utf-8")


def write_frozen_eval(home: Path, name: str = "demo", *, with_command: bool = False) -> Path:
    p = home / "skillopt" / "evals" / f"{name}-frozen.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    contract = {
        "classification": "frozen_hermes_target_execution_v1",
        "adoption_eligible": True,
        "required_evidence": {"provider_model_tool_session_runtime_transcript_scoring": True},
    }
    tasks = []
    for split in ("train", "validation", "test"):
        rec = {
            "id": f"{split}-1",
            "split": split,
            "prompt": f"{split} frozen target evidence case",
            "expected_terms": ["verify"],
            "assertions": [{"type": "contains", "value": "verify"}],
            "production_gate_eligible": split != "train",
            "task_origin": "curated",
            "timeout": 10,
        }
        if with_command:
            rec["fixtures"] = {"command": "touch /tmp/hermes-skillopt-should-not-run"}
        tasks.append(rec)
    payload = {
        "schema_version": "hermes-curated-eval-pack-v1",
        "pack_id": f"{name}-frozen",
        "version": "test-v1",
        "sample_pack": False,
        "task_origin": "curated",
        "require_complete_splits": True,
        "production_policy": {"allow_production_adoption": True},
        "eval_execution_contract": contract,
        "tasks": tasks,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_frozen_mvp_full_run_emits_target_execution_evidence_and_reviewer_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_skill(tmp_path)
    eval_path = write_frozen_eval(tmp_path)

    out = core.full_run(
        skill="demo",
        eval_file=str(eval_path),
        backend="mock",
        optimizer_backend="mock",
        allow_mock=True,
        target_executor="frozen_hermes_target_execution_v1",
        gate_mode="strict",
        candidate_count=2,
        hermes_home_path=str(tmp_path),
    )
    run_dir = Path(out["run_dir"])
    evidence = json.loads((run_dir / "target_execution_evidence.json").read_text(encoding="utf-8"))
    reviewer = json.loads((run_dir / "reviewer_gate.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert evidence["schema_version"] == "skillopt-target-execution-evidence-v1"
    assert evidence["classification"] == "frozen_hermes_target_execution_v1"
    assert evidence["implementation_label"] == "sandbox_mvp_fixed_runner_review_only"
    assert evidence["complete"] is False
    assert evidence["explicit_real_runtime_required"] is True
    assert evidence["real_hermes_runtime_evidence"] is False
    assert evidence["real_hermes_runtime_invocation"] is False
    assert evidence["internal_review_only_runner"] is True
    assert evidence["permissions"]["task_commands_allowed"] is False
    assert evidence["permissions"]["profile_write_allowed"] is False
    assert evidence["provider_fingerprint"]["fingerprint_sha256"]
    assert evidence["model_fingerprint"]["fingerprint_sha256"]
    assert evidence["tool_policy_fingerprint"]["fingerprint_sha256"]
    assert evidence["session_fingerprint"]["fingerprint_sha256"]
    assert evidence["runtime_fingerprint"]["fingerprint_sha256"]
    assert evidence["isolated_runtime_proof"]["fingerprint_sha256"]
    assert evidence["trajectory_or_transcript_artifact_fingerprint"]["test"]
    assert evidence["execution_scoring_evidence"]
    assert reviewer["schema_version"] == "skillopt-reviewer-gate-v1"
    assert "authoritative" not in reviewer
    assert reviewer["authority"] == "deterministic_validation_gate"
    assert reviewer["deterministic_validation_gate_authoritative"] is True
    assert reviewer["reviewer_artifact_authoritative"] is False
    assert reviewer["llm_notes_authoritative"] is False
    assert reviewer["cannot_override_validation_gate"] is True
    assert len(reviewer["non_selected_candidate_reasons"]) >= 1
    assert manifest["target_execution_evidence"]["fingerprint_sha256"] == evidence["fingerprint_sha256"]
    assert manifest["reviewer_gate"]["fingerprint_sha256"] == reviewer["fingerprint_sha256"]
    assert manifest["files"]["target_execution_evidence"] == "target_execution_evidence.json"
    assert manifest["files"]["reviewer_gate"] == "reviewer_gate.json"


def test_missing_frozen_target_evidence_blocks_production_summary():
    summary = core._target_execution_evidence_summary(
        target_config={"executor": "hermes_sandbox_executor_mvp", "target_config_id": "frozen-hermes-trace-replay-v2", "fingerprint_sha256": "abc", "parameters": {"frozen_hermes_contract": "frozen_hermes_target_execution_v1"}},
        validation_summary={"candidate_eval": {"results": [], "eval_execution_contract_checks": [{"missing_runtime_evidence": ["runtime_fingerprint"]}]}},
        production_validation_summary=None,
        test_results={"results": []},
        target_backend="hermes_sandbox_executor_mvp",
    )
    assert summary["classification"] == "frozen_hermes_target_execution_v1"
    assert summary["complete"] is False
    assert "runtime_fingerprint" in summary["missing_required_evidence"]
    assert summary["production_adoption_requires_complete_evidence"] is True


def test_target_execution_evidence_is_incomplete_if_task_commands_were_executed():
    target_config = {"executor": "hermes_sandbox_executor_mvp", "target_config_id": "frozen-hermes-trace-replay-v2", "fingerprint_sha256": "abc", "parameters": {"frozen_hermes_contract": "frozen_hermes_target_execution_v1"}}
    metadata = {
        "frozen_hermes_contract": "frozen_hermes_target_execution_v1",
        "permissions": {"task_commands_allowed": False, "profile_write_allowed": False, "live_profile_writes": False, "fingerprint_sha256": "perm"},
        "provider_fingerprint": {"fingerprint_sha256": "provider"},
        "model_fingerprint": {"fingerprint_sha256": "model"},
        "toolset_fingerprint": {"fingerprint_sha256": "toolset"},
        "tool_policy_fingerprint": {"fingerprint_sha256": "tool-policy"},
        "session_fingerprint": {"fingerprint_sha256": "session"},
        "runtime_fingerprint": {"available": True, "fingerprint_sha256": "runtime"},
        "isolated_runtime_evidence": {"fingerprint_sha256": "isolated"},
        "execution_scoring_evidence": {"fingerprint_sha256": "scoring"},
        "task_commands_executed": True,
    }
    summary = core._target_execution_evidence_summary(
        target_config=target_config,
        validation_summary={"candidate_eval": {"results": [{"metadata": metadata}], "eval_execution_contract_checks": [{"runtime_evidence_complete": True, "missing_runtime_evidence": []}]}},
        production_validation_summary=None,
        test_results={"results": []},
        target_backend="hermes_sandbox_executor_mvp",
    )
    assert summary["classification"] == "frozen_hermes_target_execution_v1"
    assert summary["task_commands_executed"] is True
    assert summary["complete"] is False


def test_task_command_injection_is_blocked_by_frozen_sandbox_runner():
    task = EvalTask(id="cmd", prompt="try command", fixtures={"command": "touch /tmp/should-not-run"}, metadata={"eval_execution_contract": {"classification": "frozen_hermes_target_execution_v1"}}, timeout=5)
    result = TargetExecutor(runner=HermesSandboxRunner(), requested_executor="frozen_hermes_target_execution_v1").evaluate("# demo\nverify", [task], label="cmd")
    row = result["results"][0]
    assert row["passed"] is False
    assert row["metadata"]["task_commands_executed"] is False
    assert row["metadata"]["sandbox_command_blocked"] is True
    assert result["eval_execution_contract_checks"][0]["runtime_evidence_complete"] is False


def test_reviewer_gate_cannot_override_regression_or_hard_fail():
    reviewer = core._reviewer_gate_artifact(
        candidate_summary=[{"iteration": 1, "ranked_candidates": [{"candidate_id": "candidate-1-1", "selected": True, "accepted": True, "delta": 0.4, "validation_ok": True}]}],
        validation_summary={"metric_summary": {"candidate_hard_failures": [{"task_id": "val-hard"}]}},
        production_validation_summary=None,
        test_results={"regression_cases": ["test-regression"]},
        regression_cases=["test-regression"],
        adoptability_reasons=[],
        final_status="staged_best",
        deterministic_adoptable_before_review=True,
    )
    assert reviewer["passed"] is False
    assert reviewer["adoptable_after_reviewer_gate"] is False
    assert reviewer["checklist"]["hard_failures_absent"] is False
    assert reviewer["checklist"]["heldout_test_no_regressions"] is False
    assert "authoritative" not in reviewer
    assert reviewer["authority"] == "deterministic_validation_gate"
    assert reviewer["deterministic_validation_gate_authoritative"] is True
    assert reviewer["reviewer_artifact_authoritative"] is False
    assert reviewer["llm_notes_authoritative"] is False
