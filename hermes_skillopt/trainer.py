from __future__ import annotations

"""Six-stage SkillOpt trainer for Hermes.

The full-run shell in core owns staging/adoptability/artifact integrity.  This
trainer owns the upstream-shaped optimization evidence: rollout, reflect,
aggregate, select, update, evaluate/gate, plus final held-out test evaluation.
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
    production_best_gate: dict[str, Any] | None
    test_results: dict[str, Any]
    all_reflections: list[dict[str, Any]]
    all_edits: list[dict[str, Any]]
    all_gates: list[dict[str, Any]]
    all_production_gates: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    stage_records: list[StageRecord]


class StageRecorder:
    """Small artifact writer for explicit six stage records."""

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
        self._record(iteration, "select", {"selected_edits": len(edit_plan.get("edits") or []), "bounded": bool(edit_plan.get("bounded")), "validation": edit_plan.get("validation")})

    def update(self, iteration: int, candidate_sha256: str, changed: bool) -> None:
        self._record(iteration, "update", {"candidate_sha256": candidate_sha256, "candidate_changed": changed})

    def evaluate(self, iteration: int, current_val: dict[str, Any], candidate_val: dict[str, Any], gate: dict[str, Any]) -> None:
        self._record(iteration, "evaluate", {"current_score": current_val.get("score"), "candidate_score": candidate_val.get("score"), "accepted": gate.get("accepted")})


class SixStageSkillOptTrainer:
    """Six-stage trainer orchestration for one staged full_run."""

    def __init__(self, executor: TargetExecutor, optimizer: OptimizerBackend, gatekeeper: Any, llm: Any, artifacts: SkillOptArtifacts, run_dir: Path):
        self.executor = executor
        self.optimizer = optimizer
        self.gatekeeper = gatekeeper
        self.llm = llm
        self.artifacts = artifacts
        self.run_dir = run_dir
        self.stages = StageRecorder(run_dir)
        self.stage_records: list[StageRecord] = []

    def run(
        self,
        original: str,
        tasks: dict[str, list[EvalTask]],
        iterations: int,
        *,
        production_val_tasks: list[EvalTask] | None = None,
        rejected_history: list[dict[str, Any]] | None = None,
    ) -> TrainerResult:
        from hermes_skillopt.core import write_text, _jsonl_write, sha256_text  # lazy import avoids module cycle

        total_iterations = max(1, int(iterations))
        production_val_tasks = production_val_tasks or []
        current = original
        best = original
        best_gate: dict[str, Any] | None = None
        all_reflections: list[dict[str, Any]] = []
        all_edits: list[dict[str, Any]] = []
        all_gates: list[dict[str, Any]] = []
        all_production_gates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        rejected_history = rejected_history or []

        for it in range(1, total_iterations + 1):
            train_eval = self.executor.evaluate(current, tasks["train"], label=f"current_train_{it}")
            self.stages.rollout(it, train_eval)
            self.stage_records.append(StageRecord("rollout", it, {"score": train_eval.get("score")}))

            rejected_context = summarize_rejected_edits(rejected_history + rejected)
            reflection = self.optimizer.reflect(tasks["train"], current, train_eval, self.run_dir, it, rejected_context=rejected_context)
            self.stages.reflect(it, reflection, len(rejected_context))
            self.stage_records.append(StageRecord("reflect", it, {"rejected_context_count": len(rejected_context)}))

            self.stages.aggregate(it, reflection, self.optimizer.edit_budget)
            self.stage_records.append(StageRecord("aggregate", it, {"edit_budget": self.optimizer.edit_budget}))
            candidate = self.optimizer.propose(reflection, current, self.run_dir, it, rejected_context=rejected_context)
            edit_plan = {"iteration": it, "edits": candidate.edits, "reasoning": candidate.reasoning, "bounded": True, "validation": candidate.validation}
            self.stages.select(it, edit_plan)
            self.stage_records.append(StageRecord("select", it, {"selected_edits": len(candidate.edits), "validation": candidate.validation}))
            write_text(self.run_dir / f"candidate_{it}_SKILL.md", candidate.text)
            write_text(self.run_dir / f"candidate_{it}_edits.json", json.dumps(edit_plan, ensure_ascii=False, indent=2) + "\n")
            self.stages.update(it, sha256_text(candidate.text), candidate.text != current)
            self.stage_records.append(StageRecord("update", it, {"candidate_changed": candidate.text != current}))

            current_val = self.executor.evaluate(current, tasks["val"], label=f"current_val_{it}")
            candidate_val = self.executor.evaluate(candidate.text, tasks["val"], label=f"candidate_val_{it}")
            write_text(self.artifacts.current_validation_results if it == total_iterations else self.run_dir / f"current_validation_results_{it}.json", json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
            write_text(self.artifacts.candidate_validation_results if it == total_iterations else self.run_dir / f"candidate_validation_results_{it}.json", json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")
            if it != total_iterations:
                write_text(self.artifacts.current_validation_results, json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
                write_text(self.artifacts.candidate_validation_results, json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")
            judge = self._judge(current_val, candidate_val, it)
            gate = self.gatekeeper.decide(it, current_val, candidate_val, current, candidate.text, judge=judge).as_dict()

            production_gate: dict[str, Any] | None = None
            if production_val_tasks:
                production_current_val = self.executor.evaluate(current, production_val_tasks, label=f"production_current_val_{it}")
                production_candidate_val = self.executor.evaluate(candidate.text, production_val_tasks, label=f"production_candidate_val_{it}")
                production_gate = self.gatekeeper.decide(it, production_current_val, production_candidate_val, current, candidate.text, judge={"note": "production/adopt gate uses only explicit curated validation tasks"}).as_dict()
                production_gate["gate_scope"] = "production_curated_validation_only"
                production_gate["task_ids"] = [t.id for t in production_val_tasks]
                write_text(self.run_dir / f"production_current_validation_results_{it}.json", json.dumps(production_current_val, ensure_ascii=False, indent=2) + "\n")
                write_text(self.run_dir / f"production_candidate_validation_results_{it}.json", json.dumps(production_candidate_val, ensure_ascii=False, indent=2) + "\n")

            self.stages.evaluate(it, current_val, candidate_val, gate)
            self.stage_records.append(StageRecord("evaluate", it, {"current_score": current_val.get("score"), "candidate_score": candidate_val.get("score"), "accepted": gate.get("accepted")}))

            invalid_edit = not bool(candidate.validation.get("ok", True))
            if gate["accepted"] and not invalid_edit:
                current = candidate.text
                best = candidate.text
                best_gate = gate
                status_value = "accepted"
            else:
                status_value = "rejected"
                rejected.append({"iteration": it, "gate": gate, "edits": candidate.edits, "reasoning": candidate.reasoning, "validation_errors": candidate.validation.get("errors", []), "rejected_edits": candidate.validation.get("rejected_edits", [])})
            all_reflections.append(reflection)
            all_edits.append(edit_plan)
            all_gates.append(gate | {"status": status_value})
            if production_gate is not None:
                all_production_gates.append(production_gate | {"status": status_value})
            write_text(self.artifacts.current, current)
            _jsonl_write(self.run_dir / "stage_records.jsonl", [r.__dict__ for r in self.stage_records])

        test_subject = best if best != original else original
        test_results = self.executor.evaluate(test_subject, tasks["test"], label="final_best_test")
        write_text(self.run_dir / "test_results.json", json.dumps(test_results, ensure_ascii=False, indent=2) + "\n")
        production_best_gate = next((g for g in reversed(all_production_gates) if g.get("status") == "accepted"), None)
        if production_best_gate is None and all_production_gates:
            production_best_gate = all_production_gates[-1]
        return TrainerResult(current, best, best_gate, production_best_gate, test_results, all_reflections, all_edits, all_gates, all_production_gates, rejected, self.stage_records)

    def _judge(self, current_val: dict[str, Any], candidate_val: dict[str, Any], iteration: int) -> dict[str, Any]:
        try:
            return self.llm.json(
                "Explain current vs candidate on validation. Explanation only; cannot accept.\n"
                + json.dumps({"current_eval": current_val, "candidate_eval": candidate_val}, ensure_ascii=False)[:10000],
                {"kind": "gate"},
                self.run_dir / f"llm_gate_repair_{iteration}.json",
            )
        except Exception as exc:  # pragma: no cover - defensive repair path
            return {"judge_error": str(exc)}
