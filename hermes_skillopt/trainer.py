from __future__ import annotations

"""Six-stage SkillOpt trainer skeleton for Hermes.

The MVP keeps the existing deterministic behavior but makes the upstream-shaped
stages explicit: rollout, reflect, aggregate, select, update, evaluate.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_skillopt.env import EvalTask
from hermes_skillopt.optimizer import OptimizerBackend, summarize_rejected_edits
from hermes_skillopt.state import CandidateSkill, SkillOptArtifacts
from hermes_skillopt.target import TargetExecutor


@dataclass
class StageRecord:
    stage: str
    iteration: int
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrainerResult:
    current: str
    best: str
    best_gate: dict[str, Any] | None
    all_reflections: list[dict[str, Any]]
    all_edits: list[dict[str, Any]]
    all_gates: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    stage_records: list[StageRecord]


class StageRecorder:
    """Small artifact writer for the explicit six stage full_run skeleton."""

    def __init__(self, run_dir: Path):
        self.stage_dir = run_dir / "stages"
        self.records: list[dict[str, Any]] = []

    def _record(self, iteration: int, stage: str, evidence: dict[str, Any]) -> None:
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        row = {"iteration": iteration, "stage": stage, "evidence": evidence}
        self.records.append(row)
        (self.stage_dir / f"{iteration:03d}_{stage}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def rollout(self, iteration: int, train_eval: dict[str, Any]) -> None:
        self._record(iteration, "rollout", {"score": train_eval.get("score"), "num_tasks": train_eval.get("num_tasks"), "executor": train_eval.get("executor")})

    def reflect(self, iteration: int, reflection: dict[str, Any], rejected_context_count: int) -> None:
        self._record(iteration, "reflect", {"keys": sorted(reflection.keys()), "rejected_context_count": rejected_context_count})

    def aggregate(self, iteration: int, reflection: dict[str, Any], edit_budget: int) -> None:
        self._record(iteration, "aggregate", {"strategy": "single_reflection_minimal", "reflection_keys": sorted(reflection.keys()), "edit_budget": edit_budget})

    def select(self, iteration: int, edit_plan: dict[str, Any]) -> None:
        self._record(iteration, "select", {"selected_edits": len(edit_plan.get("edits") or []), "bounded": bool(edit_plan.get("bounded"))})

    def update(self, iteration: int, candidate_sha256: str, changed: bool) -> None:
        self._record(iteration, "update", {"candidate_sha256": candidate_sha256, "candidate_changed": changed})

    def evaluate(self, iteration: int, current_val: dict[str, Any], candidate_val: dict[str, Any], gate: dict[str, Any]) -> None:
        self._record(iteration, "evaluate", {"current_score": current_val.get("score"), "candidate_score": candidate_val.get("score"), "accepted": gate.get("accepted")})


class SixStageSkillOptTrainer:
    """Minimal six-stage trainer orchestration for one staged full_run."""

    def __init__(self, executor: TargetExecutor, optimizer: OptimizerBackend, gatekeeper: Any, llm: Any, artifacts: SkillOptArtifacts, run_dir: Path):
        self.executor = executor
        self.optimizer = optimizer
        self.gatekeeper = gatekeeper
        self.llm = llm
        self.artifacts = artifacts
        self.run_dir = run_dir
        self.stage_records: list[StageRecord] = []

    def run(self, original: str, tasks: dict[str, list[EvalTask]], iterations: int) -> TrainerResult:
        from hermes_skillopt.core import write_text, _jsonl_write  # lazy import avoids module cycle

        total_iterations = max(1, int(iterations))
        current = original
        best = original
        best_gate: dict[str, Any] | None = None
        all_reflections: list[dict[str, Any]] = []
        all_edits: list[dict[str, Any]] = []
        all_gates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for it in range(1, total_iterations + 1):
            train_eval = self.rollout(current, tasks["train"], it)
            rejected_context = summarize_rejected_edits(rejected)
            reflection = self.reflect(tasks["train"], current, train_eval, rejected_context, it)
            candidate = self.aggregate_select_update(reflection, current, rejected_context, it)
            edit_plan = {"iteration": it, "edits": candidate.edits, "reasoning": candidate.reasoning, "bounded": True}
            write_text(self.run_dir / f"candidate_{it}_SKILL.md", candidate.text)
            write_text(self.run_dir / f"candidate_{it}_edits.json", json.dumps(edit_plan, ensure_ascii=False, indent=2) + "\n")

            current_val, candidate_val, gate = self.evaluate(current, candidate, tasks["val"], it)
            write_text(self.artifacts.current_validation_results if it == total_iterations else self.run_dir / f"current_validation_results_{it}.json", json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
            write_text(self.artifacts.candidate_validation_results if it == total_iterations else self.run_dir / f"candidate_validation_results_{it}.json", json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")
            if it != total_iterations:
                write_text(self.artifacts.current_validation_results, json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
                write_text(self.artifacts.candidate_validation_results, json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")

            if gate["accepted"]:
                current = candidate.text
                best = candidate.text
                best_gate = gate
                status_value = "accepted"
            else:
                status_value = "rejected"
                rejected.append({"iteration": it, "gate": gate, "edits": candidate.edits, "reasoning": candidate.reasoning})
            all_reflections.append(reflection)
            all_edits.append(edit_plan)
            all_gates.append(gate | {"status": status_value})
            write_text(self.artifacts.current, current)
            _jsonl_write(self.run_dir / "stage_records.jsonl", [r.__dict__ for r in self.stage_records])

        return TrainerResult(current, best, best_gate, all_reflections, all_edits, all_gates, rejected, self.stage_records)

    def rollout(self, skill_text: str, train_tasks: list[EvalTask], iteration: int) -> dict[str, Any]:
        result = self.executor.evaluate(skill_text, train_tasks, label=f"current_train_{iteration}")
        self.stage_records.append(StageRecord("rollout", iteration, {"score": result.get("score"), "num_tasks": result.get("num_tasks"), "executor": result.get("executor")}))
        return result

    def reflect(self, train_tasks: list[EvalTask], current: str, train_eval: dict[str, Any], rejected_context: list[dict[str, Any]], iteration: int) -> dict[str, Any]:
        reflection = self.optimizer.reflect(train_tasks, current, train_eval, self.run_dir, iteration, rejected_context=rejected_context)
        self.stage_records.append(StageRecord("reflect", iteration, {"rejected_context_count": len(rejected_context)}))
        return reflection

    def aggregate_select_update(self, reflection: dict[str, Any], current: str, rejected_context: list[dict[str, Any]], iteration: int) -> CandidateSkill:
        # MVP aggregate/select are deterministic: keep backend-proposed bounded edits,
        # respect edit_budget in OptimizerBackend, and persist evidence for future ports.
        self.stage_records.append(StageRecord("aggregate", iteration, {"strategy": "single_reflection_dedupe_future"}))
        candidate = self.optimizer.propose(reflection, current, self.run_dir, iteration, rejected_context=rejected_context)
        self.stage_records.append(StageRecord("select", iteration, {"selected_edits": len(candidate.edits), "edit_budget": self.optimizer.edit_budget}))
        self.stage_records.append(StageRecord("update", iteration, {"candidate_changed": candidate.text != current}))
        return candidate

    def evaluate(self, current: str, candidate: CandidateSkill, val_tasks: list[EvalTask], iteration: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        current_val = self.executor.evaluate(current, val_tasks, label=f"current_val_{iteration}")
        candidate_val = self.executor.evaluate(candidate.text, val_tasks, label=f"candidate_val_{iteration}")
        judge: dict[str, Any] | None = None
        try:
            judge = self.llm.json(
                "Explain current vs candidate on validation. Explanation only; cannot accept.\n"
                + json.dumps({"current_eval": current_val, "candidate_eval": candidate_val}, ensure_ascii=False)[:10000],
                {"kind": "gate"},
                self.run_dir / f"llm_gate_repair_{iteration}.json",
            )
        except Exception as exc:  # pragma: no cover - defensive repair path
            judge = {"judge_error": str(exc)}
        gate = self.gatekeeper.decide(iteration, current_val, candidate_val, current, candidate.text, judge=judge).as_dict()
        self.stage_records.append(StageRecord("evaluate", iteration, {"current_score": current_val.get("score"), "candidate_score": candidate_val.get("score"), "accepted": gate.get("accepted")}))
        return current_val, candidate_val, gate
