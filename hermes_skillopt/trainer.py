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
    candidate_summary: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    stage_records: list[StageRecord]


class StageRecorder:
    """Small artifact writer for explicit six stage records."""

    def __init__(self, run_dir: Path):
        self.stage_dir = run_dir / "stages"
        self.records: list[dict[str, Any]] = []

    def _fingerprint(self, payload: Any) -> str:
        from hermes_skillopt.core import _stable_json_sha  # lazy import avoids module cycle

        return _stable_json_sha(payload)

    def _record(self, iteration: int, stage: str, evidence: dict[str, Any], *, input_payload: Any | None = None, output_payload: Any | None = None) -> None:
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        input_payload = evidence if input_payload is None else input_payload
        output_payload = evidence if output_payload is None else output_payload
        row = {"schema_version": "skillopt-stage-v1", "iteration": iteration, "stage": stage, "input_sha256": self._fingerprint(input_payload), "output_sha256": self._fingerprint(output_payload), "evidence": evidence}
        self.records.append(row)
        (self.stage_dir / f"{iteration:03d}_{stage}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def rollout(self, iteration: int, train_eval: dict[str, Any]) -> None:
        evidence = {"score": train_eval.get("score"), "num_tasks": train_eval.get("num_tasks"), "executor": train_eval.get("executor")}
        self._record(iteration, "rollout", evidence, output_payload=train_eval)

    def reflect(self, iteration: int, reflection: dict[str, Any], rejected_context_count: int) -> None:
        evidence = {"keys": sorted(reflection.keys()), "rejected_context_count": rejected_context_count}
        self._record(iteration, "reflect", evidence, input_payload={"rejected_context_count": rejected_context_count}, output_payload=reflection)

    def aggregate(self, iteration: int, reflection: dict[str, Any], edit_budget: int) -> None:
        evidence = {"strategy": "multi_candidate_conservative_rank_select", "reflection_keys": sorted(reflection.keys()), "edit_budget": edit_budget}
        self._record(iteration, "aggregate", evidence, input_payload=reflection, output_payload=evidence)

    def select(self, iteration: int, edit_plan: dict[str, Any]) -> None:
        evidence = {"selected_candidate_id": edit_plan.get("candidate_id"), "selected_edits": len(edit_plan.get("edits") or []), "bounded": bool(edit_plan.get("bounded")), "validation": edit_plan.get("validation"), "ranked_candidates": edit_plan.get("ranked_candidates", [])}
        self._record(iteration, "select", evidence, input_payload=edit_plan.get("ranked_candidates", []), output_payload=edit_plan)

    def update(self, iteration: int, candidate_sha256: str, changed: bool) -> None:
        evidence = {"candidate_sha256": candidate_sha256, "candidate_changed": changed}
        self._record(iteration, "update", evidence, output_payload=evidence)

    def evaluate(self, iteration: int, current_val: dict[str, Any], candidate_val: dict[str, Any], gate: dict[str, Any]) -> None:
        evidence = {"current_score": current_val.get("score"), "candidate_score": candidate_val.get("score"), "accepted": gate.get("accepted"), "candidate_id": gate.get("candidate_id")}
        self._record(iteration, "evaluate", evidence, input_payload={"current_val": current_val, "candidate_val": candidate_val}, output_payload=gate)


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
        candidate_count: int = 1,
    ) -> TrainerResult:
        from hermes_skillopt.core import write_text, _jsonl_write, sha256_text  # lazy import avoids module cycle

        total_iterations = max(1, int(iterations))
        per_round_candidates = max(1, int(candidate_count))
        production_val_tasks = production_val_tasks or []
        current = original
        best = original
        best_gate: dict[str, Any] | None = None
        all_reflections: list[dict[str, Any]] = []
        all_edits: list[dict[str, Any]] = []
        all_gates: list[dict[str, Any]] = []
        all_production_gates: list[dict[str, Any]] = []
        candidate_summary: list[dict[str, Any]] = []
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
            self.stage_records.append(StageRecord("aggregate", it, {"edit_budget": self.optimizer.edit_budget, "candidate_count": per_round_candidates}))

            current_val = self.executor.evaluate(current, tasks["val"], label=f"current_val_{it}")
            production_current_val = self.executor.evaluate(current, production_val_tasks, label=f"production_current_val_{it}") if production_val_tasks else None
            ranked: list[dict[str, Any]] = []
            candidate_evals: list[tuple[CandidateSkill, dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]] = []
            round_rejected_buffer: list[dict[str, Any]] = []
            for ci in range(1, per_round_candidates + 1):
                proposal_context = summarize_rejected_edits(rejected_history + rejected + round_rejected_buffer)
                cand = self.optimizer.propose_candidate(reflection, current, self.run_dir, it, ci, rejected_context=proposal_context)
                candidate_val = self.executor.evaluate(cand.text, tasks["val"], label=f"candidate_val_{it}_{ci}")
                judge = self._judge(current_val, candidate_val, it, ci)
                gate = self.gatekeeper.decide(it, current_val, candidate_val, current, cand.text, judge=judge).as_dict()
                gate["candidate_id"] = cand.candidate_id
                invalid_edit = not bool(cand.validation.get("ok", True))
                production_candidate_val = None
                production_gate: dict[str, Any] | None = None
                if production_val_tasks and production_current_val is not None:
                    production_candidate_val = self.executor.evaluate(cand.text, production_val_tasks, label=f"production_candidate_val_{it}_{ci}")
                    production_gate = self.gatekeeper.decide(it, production_current_val, production_candidate_val, current, cand.text, judge={"note": "production/adopt gate uses only explicit curated validation tasks"}).as_dict()
                    assert production_gate is not None
                    production_gate["gate_scope"] = "production_curated_validation_only"
                    production_gate["task_ids"] = [t.id for t in production_val_tasks]
                    production_gate["candidate_id"] = cand.candidate_id
                production_delta = None
                if production_gate is not None:
                    production_delta = round(float(production_gate.get("candidate_score", 0.0)) - float(production_gate.get("current_score", 0.0)), 6)
                row = {
                    "candidate_id": cand.candidate_id,
                    "iteration": it,
                    "candidate_index": ci,
                    "accepted": bool(gate.get("accepted")) and not invalid_edit,
                    "current_score": gate.get("current_score"),
                    "candidate_score": gate.get("candidate_score"),
                    "delta": round(float(gate.get("candidate_score", 0.0)) - float(gate.get("current_score", 0.0)), 6),
                    "metric_policy": gate.get("metric_policy"),
                    "metric_summary": gate.get("metric_summary"),
                    "rejection_reasons": list(gate.get("rejection_reasons") or []) + (["bounded edit validation failed"] if invalid_edit else []),
                    "production_gate": production_gate,
                    "production_delta": production_delta,
                    "production_accepted": bool(production_gate and production_gate.get("accepted")) and not invalid_edit,
                    "validation_ok": not invalid_edit,
                    "edit_count": len(cand.edits),
                }
                ranked.append(row)
                candidate_evals.append((cand, candidate_val, gate, production_candidate_val, production_gate))
                if invalid_edit or not bool(gate.get("accepted")) or (production_gate is not None and not bool(production_gate.get("accepted"))):
                    round_rejected_buffer.append({"iteration": it, "candidate_id": cand.candidate_id, "gate": gate, "production_gate": production_gate, "edits": cand.edits, "reasoning": cand.reasoning, "validation_errors": cand.validation.get("errors", []), "rejected_edits": cand.validation.get("rejected_edits", []), "buffer_scope": "same_run_step_reuse"})
                write_text(self.run_dir / f"candidate_{it}_{ci}_SKILL.md", cand.text)
                write_text(self.run_dir / f"candidate_{it}_{ci}_edits.json", json.dumps({"iteration": it, "candidate_id": cand.candidate_id, "edits": cand.edits, "reasoning": cand.reasoning, "bounded": True, "validation": cand.validation, "gate": gate, "production_gate": production_gate}, ensure_ascii=False, indent=2) + "\n")

            accepted_rows = [r for r in ranked if r["accepted"]]
            production_accepted_rows = [r for r in accepted_rows if r.get("production_accepted")]
            select_pool = production_accepted_rows or accepted_rows or ranked
            selected_row = max(select_pool, key=lambda r: (float(r.get("production_delta") or 0.0), float(r["candidate_score"] or 0.0), float(r["delta"] or 0.0), -int(r["candidate_index"])))
            for rank, row in enumerate(sorted(ranked, key=lambda r: (float(r.get("production_delta") or 0.0), float(r.get("candidate_score") or 0.0), float(r.get("delta") or 0.0), -int(r.get("candidate_index") or 0)), reverse=True), 1):
                row["rank"] = rank
                row["selected"] = row["candidate_id"] == selected_row["candidate_id"]
            selected_idx = int(selected_row["candidate_index"]) - 1
            candidate, candidate_val, gate, production_candidate_val, production_gate = candidate_evals[selected_idx]

            selection_rationale = (
                "selected production-accepted candidate with strongest production/generic deterministic deltas"
                if production_accepted_rows and selected_row.get("production_accepted")
                else "selected generic validation-accepted candidate with strongest deterministic weighted score delta"
                if accepted_rows and selected_row.get("accepted")
                else "no candidate accepted; selected highest-ranked rejected candidate only for artifact comparison"
            )
            edit_plan = {"iteration": it, "candidate_id": candidate.candidate_id, "edits": candidate.edits, "reasoning": candidate.reasoning, "bounded": True, "validation": candidate.validation, "ranked_candidates": ranked, "selected_candidate_rationale": selection_rationale, "selection_rule": "prefer candidates with both generic validation strict improvement and production validation strict improvement when production gates exist; non-selected candidates are rejected/buffered"}
            self.stages.select(it, edit_plan)
            self.stage_records.append(StageRecord("select", it, {"selected_candidate_id": candidate.candidate_id, "selected_edits": len(candidate.edits), "validation": candidate.validation, "ranked_candidates": ranked}))
            write_text(self.run_dir / f"candidate_{it}_SKILL.md", candidate.text)
            write_text(self.run_dir / f"candidate_{it}_edits.json", json.dumps(edit_plan, ensure_ascii=False, indent=2) + "\n")
            self.stages.update(it, sha256_text(candidate.text), candidate.text != current)
            self.stage_records.append(StageRecord("update", it, {"candidate_changed": candidate.text != current}))

            write_text(self.artifacts.current_validation_results if it == total_iterations else self.run_dir / f"current_validation_results_{it}.json", json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
            write_text(self.artifacts.candidate_validation_results if it == total_iterations else self.run_dir / f"candidate_validation_results_{it}.json", json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")
            if it != total_iterations:
                write_text(self.artifacts.current_validation_results, json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
                write_text(self.artifacts.candidate_validation_results, json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")
            if production_val_tasks and production_current_val is not None and production_candidate_val is not None:
                write_text(self.run_dir / f"production_current_validation_results_{it}.json", json.dumps(production_current_val, ensure_ascii=False, indent=2) + "\n")
                write_text(self.run_dir / f"production_candidate_validation_results_{it}.json", json.dumps(production_candidate_val, ensure_ascii=False, indent=2) + "\n")

            self.stages.evaluate(it, current_val, candidate_val, gate)
            self.stage_records.append(StageRecord("evaluate", it, {"current_score": current_val.get("score"), "candidate_score": candidate_val.get("score"), "accepted": gate.get("accepted"), "candidate_id": candidate.candidate_id}))

            invalid_edit = not bool(candidate.validation.get("ok", True))
            if gate["accepted"] and not invalid_edit:
                current = candidate.text
                best = candidate.text
                best_gate = gate
                status_value = "accepted"
            else:
                status_value = "rejected"
                rejected.append({"iteration": it, "candidate_id": candidate.candidate_id, "gate": gate, "edits": candidate.edits, "reasoning": candidate.reasoning, "validation_errors": candidate.validation.get("errors", []), "rejected_edits": candidate.validation.get("rejected_edits", [])})
            for cand, _cval, cgate, _pcval, pgate in candidate_evals:
                if cand.candidate_id != candidate.candidate_id:
                    rejected.append({"iteration": it, "candidate_id": cand.candidate_id, "gate": cgate, "production_gate": pgate, "edits": cand.edits, "reasoning": cand.reasoning, "validation_errors": cand.validation.get("errors", []), "rejected_edits": cand.validation.get("rejected_edits", []), "selection_rejection": True})
            all_reflections.append(reflection)
            all_edits.append(edit_plan)
            all_gates.append(gate | {"status": status_value})
            if production_gate is not None:
                all_production_gates.append(production_gate | {"status": status_value})
            candidate_summary.append({"iteration": it, "selected_candidate_id": candidate.candidate_id, "selected_candidate_rationale": selection_rationale, "ranked_candidates": ranked})
            write_text(self.artifacts.current, current)
            _jsonl_write(self.run_dir / "stage_records.jsonl", [r.__dict__ for r in self.stage_records])

        test_subject = best if best != original else original
        test_results = self.executor.evaluate(test_subject, tasks["test"], label="final_best_test")
        write_text(self.run_dir / "test_results.json", json.dumps(test_results, ensure_ascii=False, indent=2) + "\n")
        production_best_gate = next((g for g in reversed(all_production_gates) if g.get("status") == "accepted"), None)
        if production_best_gate is None and all_production_gates:
            production_best_gate = all_production_gates[-1]
        return TrainerResult(current, best, best_gate, production_best_gate, test_results, all_reflections, all_edits, all_gates, all_production_gates, candidate_summary, rejected, self.stage_records)

    def _judge(self, current_val: dict[str, Any], candidate_val: dict[str, Any], iteration: int, candidate_index: int = 1) -> dict[str, Any]:
        try:
            return self.llm.json(
                "Explain current vs candidate on validation. Explanation only; cannot accept.\n"
                + json.dumps({"current_eval": current_val, "candidate_eval": candidate_val}, ensure_ascii=False)[:10000],
                {"kind": "gate"},
                self.run_dir / f"llm_gate_repair_{iteration}_{candidate_index}.json",
            )
        except Exception as exc:  # pragma: no cover - defensive repair path
            return {"judge_error": str(exc)}
