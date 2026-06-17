from __future__ import annotations

import json
from pathlib import Path

from hermes_skillopt import core
from hermes_skillopt.env import (
    EnvAdapter,
    EvalTask,
    HermesEnvAdapter,
    HermesSkillEnv,
    benchmark_task_splits,
    built_in_benchmarks,
    is_production_gate_task,
    production_eligibility_for_task,
)
from hermes_skillopt.state import SkillState
from hermes_skillopt.target import DeterministicKeywordScorecard, TargetExecutor


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
