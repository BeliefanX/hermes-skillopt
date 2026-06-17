from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_skillopt import core
from hermes_skillopt.env import (
    EnvAdapter,
    EvalTask,
    HermesEnvAdapter,
    HermesSkillEnv,
    benchmark_task_splits,
    built_in_benchmarks,
    is_production_gate_task,
    load_eval_pack,
    production_eligibility_for_task,
)
from hermes_skillopt.state import SkillState
from hermes_skillopt.target import DeterministicKeywordScorecard, HermesRolloutRunner, HermesSandboxRunner, TargetExecutor


def make_skill(home: Path, name: str = "demo", body: str = "Use tools safely.") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return p


def write_eval_file(home: Path, rows: list[dict]) -> Path:
    p = home / "skillopt" / "evals" / "demo.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_env_adapter_contract_and_metadata(tmp_path):
    skill = make_skill(tmp_path)
    state = SkillState("demo", skill, "skills/demo/SKILL.md", skill.read_text(encoding="utf-8"), core.sha256_text(skill.read_text(encoding="utf-8")), tmp_path)
    adapter: EnvAdapter = HermesEnvAdapter(HermesSkillEnv(state, limit=1))

    tasks, evidence = adapter.load_tasks()

    assert set(tasks) == {"train", "val", "test"}
    assert evidence["env_adapter_contract"] == "HermesEnvAdapter-v1"
    assert evidence["split_policy"]["name"] == "hermes-skillopt-train-val-test-v1"
    assert adapter.rollout_metadata()["adapter"] == "HermesEnvAdapter"
    assert adapter.scorer_metadata()["llm_judge_can_accept"] is False


def test_three_builtin_benchmarks_have_train_val_test_and_are_non_production():
    benchmarks = built_in_benchmarks()
    assert {"delegation-handoff", "tool-use-replay", "skill-authoring-review"} <= set(benchmarks)
    splits = benchmark_task_splits(benchmarks.values())
    assert all(splits[name] for name in ("train", "val", "test"))
    for benchmark in benchmarks.values():
        assert benchmark.production_eligible is False
        assert {task.split for task in benchmark.tasks} == {"train", "val", "test"}
        assert all(not is_production_gate_task(task) for task in benchmark.tasks)


def test_dream_synthetic_and_session_mined_tasks_are_production_isolated():
    tasks = [
        EvalTask("dream-val", "dream", source="dream", split="val", expected_terms=("verify",), metadata={"task_origin": "dream", "scorecard_explicit": True, "production_gate_eligible": True}),
        EvalTask("synthetic-val", "synthetic", source="synthetic", split="val", expected_terms=("verify",), metadata={"task_origin": "synthetic", "scorecard_explicit": True, "production_gate_eligible": True}),
        EvalTask("session-val", "session", source="session-mined", split="val", expected_terms=("verify",), metadata={"task_origin": "session-mined", "scorecard_explicit": True, "production_gate_eligible": True}),
    ]
    for task in tasks:
        decision = production_eligibility_for_task(task)
        assert decision.eligible is False
        assert not is_production_gate_task(task)
        assert any("review-only" in reason or "non-production" in reason for reason in decision.reasons)


def test_report_fields_and_target_split_output(tmp_path):
    make_skill(tmp_path, body="Use tools safely. verify tool rollback blocker prodgold")
    eval_path = write_eval_file(tmp_path, [
        {"id": "prod-val", "prompt": "production validation", "expected_keywords": ["prodgold"], "split": "validation", "production_gate_eligible": True},
        {"id": "prod-test", "prompt": "production test", "expected_keywords": ["rollback"], "split": "test", "production_gate_eligible": True},
        {"id": "train", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
    ])

    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "candidate_summary.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")

    for key in ("split_scores", "per_task_delta", "candidate_comparison", "regression_cases", "production_eligibility_reasons"):
        assert key in manifest or key == "production_eligibility_reasons"
        assert key in summary or key == "production_eligibility_reasons"
    assert "split_scores" in report
    assert "regression_cases" in report

    executor = TargetExecutor(runner=DeterministicKeywordScorecard())
    eval_out = executor.evaluate("verify tool", [EvalTask("v", "p", split="val", expected_terms=("verify",), metadata={"task_origin": "synthetic", "scorecard_explicit": True})])
    assert eval_out["split_score"] == eval_out["score"]
    assert eval_out["split"] == "val"
    assert "regression_cases" in eval_out


def test_trace_replay_returns_structured_trajectory_and_blocks_commands():
    task = EvalTask(
        "trace-1",
        "Use tool evidence without executing fixture commands.",
        split="val",
        expected_terms=("verify", "tool"),
        allowed_tools=("read_file",),
        fixtures={
            "tool_calls": [{"name": "read_file", "args": {"path": "SKILL.md"}}, {"name": "terminal", "args": {"command": "rm -rf /"}}],
            "observations": ["fixture observation only"],
            "command": "touch /tmp/should-not-run",
        },
        metadata={"task_origin": "curated", "scorecard_explicit": True},
    )
    out = TargetExecutor(runner=HermesRolloutRunner()).evaluate("Verify with tool evidence.", [task], label="current_val")
    result = out["results"][0]
    trace = result["metadata"]["trajectory"]

    assert out["target_backend_config"] == TargetExecutor(runner=HermesRolloutRunner()).config.as_dict()
    assert out["target_fingerprint_sha256"] == out["target_backend_config"]["fingerprint_sha256"]
    assert trace["schema_version"] == "hermes-target-trace-v1"
    assert trace["messages"] and trace["tool_calls"] and trace["observations"] and trace["scores"]
    assert result["metadata"]["task_commands_executed"] is False
    assert result["metadata"]["sandbox_command_blocked"] is True
    assert "task_provided_command_blocked" in result["metadata"]["failure_tags"]
    assert any(call["name"] == "terminal" and call["blocked_reason"] == "tool_not_in_allowed_tools" for call in trace["tool_calls"])


def test_sandbox_rejects_task_command_before_command_runner_is_called():
    called = False

    def runner(argv, cwd, env, timeout):
        nonlocal called
        called = True
        return 0, "should not run"

    task = EvalTask("sandbox-block", "blocked", fixtures={"command": "touch /tmp/nope"})
    out = TargetExecutor(runner=HermesSandboxRunner(command_runner=runner), requested_executor="sandbox").evaluate("verify", [task])
    result = out["results"][0]

    assert called is False
    assert result["score"] == 0.0
    assert result["metadata"]["sandbox_command_blocked"] is True
    assert result["metadata"]["task_commands_executed"] is False
    assert result["metadata"]["trajectory"]["tool_calls"][0]["blocked_reason"] == "arbitrary_command_execution_disabled"


def test_sandbox_executor_runs_fixed_internal_runner_without_task_shell(tmp_path):
    marker = tmp_path / "should_not_exist"
    task = EvalTask(
        "sandbox-real",
        "Run fixed sandbox runner only.",
        split="test",
        expected_terms=("verify", "tool"),
        required_markers=("SANDBOX_OK", "MARKER:verify"),
        metadata={"task_origin": "synthetic", "scorecard_explicit": True, "command": None},
    )

    out = TargetExecutor(runner=HermesSandboxRunner(), requested_executor="sandbox").evaluate(f"verify tool; never touch {marker}", [task])
    result = out["results"][0]

    assert marker.exists() is False
    assert result["metadata"]["exit_code"] == 0
    assert result["metadata"]["sandbox_isolated"] is True
    assert result["metadata"]["task_commands_executed"] is False
    tool_calls = result["metadata"]["trajectory"]["tool_calls"]
    assert any(isinstance(call, dict) and call["name"] == "hermes_skillopt.sandbox_runner" and call["executed"] is True for call in tool_calls)
    assert "SANDBOX_OK" in result["metadata"]["transcript_preview"]


def test_frozen_target_config_provenance_freezes_model_profile_and_tool_policy():
    task = EvalTask("freeze", "verify", split="val", expected_terms=("verify",), metadata={"task_origin": "synthetic", "scorecard_explicit": True})

    out = TargetExecutor(runner=HermesRolloutRunner(), requested_executor="replay").evaluate("verify", [task])
    config = out["target_backend_config"]

    assert config["schema_version"] == "skillopt-target-backend-config-v2"
    assert config["executor"] == "hermes_trace_replay_runner_v1"
    assert config["requested_executor"] == "replay"
    assert config["model_id"] == "no-live-model-deterministic-target-v1"
    assert config["profile_policy"] == "isolated-or-readonly-profile-no-live-profile-writes"
    assert config["tool_policy"].startswith("task-commands-never-executed")
    assert config["live_tool_execution_allowed"] is False
    assert config["task_provided_commands_allowed"] is False
    assert config["parameters"]["backend_kind"] == "deterministic_trace_replay"
    assert config["parameters"]["review_only_unless_curated_pack"] is True
    assert out["target_fingerprint_sha256"] == config["fingerprint_sha256"]
    assert len(out["target_fingerprint_sha256"]) == 64


def test_rollout_output_preserves_trajectory_and_hard_soft_scores_for_gate_reflection():
    tasks = [
        EvalTask("pass", "prompt", split="train", expected_terms=("verify",), weight=2.0, fixtures={"observations": ["obs"]}, metadata={"task_origin": "synthetic", "scorecard_explicit": True}),
        EvalTask("fail", "prompt", split="train", expected_terms=("missing",), weight=1.0, metadata={"task_origin": "synthetic", "scorecard_explicit": True}),
    ]

    out = TargetExecutor(runner=HermesRolloutRunner()).evaluate("verify", tasks, label="current_train")

    assert out["soft_score"] == out["score"]
    assert out["hard_score"] == out["hard_pass_rate"] == 0.666667
    assert out["evaluation_scope"] == "review_only_deterministic_fallback"
    assert out["production_gate_eligible"] is False
    assert out["trajectory_index"]["schema_version"] == "hermes-target-trace-v1"
    assert len(out["trajectory_index"]["items"]) == 2
    assert all(item["has_trajectory"] for item in out["trajectory_index"]["items"])
    assert out["result_summary"][0]["soft_score"] == out["results"][0]["score"]
    assert out["results"][0]["metadata"]["trajectory"]["observations"]
    assert len(out["trajectory_fingerprint_sha256"]) == 64


def test_task_command_injection_is_refused_and_never_executed_by_replay(tmp_path):
    marker = tmp_path / "should_not_exist"
    task = EvalTask(
        "inject",
        "do not execute command",
        expected_terms=("verify",),
        fixtures={"command": f"touch {marker}"},
        metadata={"command": f"touch {marker}", "task_origin": "synthetic", "scorecard_explicit": True},
    )

    out = TargetExecutor(runner=HermesRolloutRunner()).evaluate("verify", [task])
    result = out["results"][0]
    calls = result["metadata"]["trajectory"]["tool_calls"]

    assert marker.exists() is False
    assert result["passed"] is False
    assert result["metadata"]["task_commands_executed"] is False
    assert result["metadata"]["sandbox_command_blocked"] is True
    assert "task_provided_command_blocked" in result["metadata"]["failure_tags"]
    assert any(call["name"] == "task_provided_command" and call["executed"] is False and call["blocked_reason"] == "arbitrary_command_execution_disabled" for call in calls)


def _write_pack(path: Path, *, sample: bool = False, duplicate_test_id: bool = False) -> Path:
    payload = {
        "schema_version": "hermes-curated-eval-pack-v1",
        "pack_id": "demo-hermes-pack",
        "version": "2026.06",
        "sample_pack": sample,
        "require_complete_splits": True,
        "production_policy": {"allow_production_adoption": not sample, "reviewed_by": "unit-test"},
        "tasks": [
            {"id": "train-tool", "split": "train", "prompt": "Train on tool use verification.", "expected_keywords": ["tool", "verify"], "production_gate_eligible": False},
            {"id": "val-edit", "split": "validation", "prompt": "Validate bounded file editing safety.", "expected_keywords": ["bounded", "guard"], "production_gate_eligible": True},
            {"id": "test-profile" if not duplicate_test_id else "val-edit", "split": "test", "prompt": "Held-out test for profile isolation and rollback.", "expected_keywords": ["profile", "rollback"], "production_gate_eligible": True},
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_eval_pack_loading_validation_fingerprint_and_split_isolation(tmp_path):
    pack_path = _write_pack(tmp_path / "skillopt" / "evals" / "demo.json")
    tasks, meta = load_eval_pack(pack_path)

    assert meta.pack_id == "demo-hermes-pack"
    assert meta.version == "2026.06"
    assert len(meta.fingerprint_sha256) == 64
    assert meta.split_counts == {"train": 1, "val": 1, "test": 1}
    assert {t.metadata["eval_pack_id"] for t in tasks} == {"demo-hermes-pack"}
    assert [t.id for t in tasks if t.split == "test"] == ["test-profile"]
    assert is_production_gate_task(next(t for t in tasks if t.id == "val-edit"))

    bad_path = _write_pack(tmp_path / "skillopt" / "evals" / "bad.json", duplicate_test_id=True)
    with pytest.raises(ValueError, match="leaks task id"):
        load_eval_pack(bad_path)


def test_sample_eval_pack_is_review_only_even_with_production_flags(tmp_path):
    pack_path = _write_pack(tmp_path / "skillopt" / "evals" / "sample.json", sample=True)
    tasks, meta = load_eval_pack(pack_path)

    assert meta.production_eligible_task_count == 0
    for task in tasks:
        assert task.metadata["task_origin"] == "sample-eval-pack"
        assert not is_production_gate_task(task)
        assert production_eligibility_for_task(task).eligible is False


def test_eval_pack_metadata_flows_to_evidence_report_manifest_and_checkpoint(tmp_path):
    make_skill(tmp_path, body="Use tools safely. verify tool bounded guard profile rollback blocker")
    pack_path = _write_pack(tmp_path / "skillopt" / "evals" / "demo.json")

    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(pack_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")

    assert manifest["eval_pack_id"] == "demo-hermes-pack"
    assert manifest["eval_pack_fingerprint_sha256"] == evidence["eval_pack_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["eval_pack_id"] == "demo-hermes-pack"
    assert manifest["production_eval_policy"]["eval_pack_id"] == "demo-hermes-pack"
    assert checkpoint["input"]["eval_pack_id"] == "demo-hermes-pack"
    assert "eval_pack_id: demo-hermes-pack" in report
    assert "validation_selects_candidates_test_is_heldout" in report
    assert evidence["split_governance"]["test"].startswith("held-out final gate")

    stage_record = json.loads((run_dir / "stages" / "001_rollout.json").read_text(encoding="utf-8"))
    assert stage_record["deterministic_batch"]["seed"] == 0
    assert stage_record["deterministic_batch"]["batch_schema"] == "skillopt-deterministic-batch-v1"


def test_eval_only_benchmark_report_is_reproducible_and_read_only(tmp_path):
    make_skill(tmp_path, body="Use tools safely. verify tool bounded guard profile rollback blocker")
    pack_path = _write_pack(tmp_path / "skillopt" / "evals" / "demo.json")

    out = core.eval_only(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(pack_path), target_executor="replay")
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "benchmark_report.json").read_text(encoding="utf-8"))

    assert out["adoptable"] is False
    assert out["benchmark_report_path"].endswith("benchmark_report.json")
    assert manifest["files"]["benchmark_report"] == "benchmark_report.json"
    assert report["schema_version"] == "hermes-native-benchmark-report-v1"
    assert report["safety"]["read_only"] is True
    assert report["safety"]["optimizer_training"] is False
    assert report["safety"]["task_provided_commands_allowed"] is False
    assert report["reproducibility"]["eval_pack_fingerprint_sha256"] == manifest["eval_pack"]["fingerprint_sha256"]
    assert report["reproducibility"]["target_fingerprint_sha256"] == report["target_backend_config"]["fingerprint_sha256"]
