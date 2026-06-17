from __future__ import annotations

"""Optimizer backends: reflection plus bounded skill edits."""

import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hermes_skillopt.bounded_edit import apply_bounded_edits, validate_bounded_edits
from hermes_skillopt.env import EvalTask
from hermes_skillopt.state import CandidateSkill


class JsonBackend(Protocol):
    mode: str

    def json(self, prompt: str, schema_hint: dict[str, Any], repair_path: Path | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OptimizerBackendConfig:
    """Explicit optimizer backend identity recorded in run artifacts.

    This separates the optimizer (LLM/mock reflection + bounded-edit proposer)
    from the target backend (frozen evaluator). The optimizer may explain or
    propose candidates, but it is never part of candidate acceptance.
    """

    backend: str
    requested_backend: str = "auto"
    allow_mock: bool = False
    edit_budget: int = 3
    candidate_count: int = 1
    iterations: int = 1
    role: str = "reflection_plus_bounded_edit_no_acceptance"
    parameters: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "requested_backend": self.requested_backend,
            "allow_mock": self.allow_mock,
            "edit_budget": max(0, int(self.edit_budget)),
            "candidate_count": max(1, int(self.candidate_count)),
            "iterations": max(1, int(self.iterations)),
            "role": self.role,
            "parameters": self.parameters,
        }


def _edit_signature(edit: dict[str, Any]) -> str:
    """Stable small signature used for rejected-edit filtering/memory."""

    keys = ("op", "anchor", "old", "new", "text")
    payload = {k: str(edit.get(k, ""))[:500] for k in keys if k in edit}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def summarize_rejected_edits(rejected: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Concise optimizer memory from prior rejected/non-selected candidates."""

    out: list[dict[str, Any]] = []
    for row in rejected[-max(0, int(limit)):]:
        gate = row.get("gate") if isinstance(row, dict) else {}
        edits = row.get("edits", []) if isinstance(row, dict) else []
        reasons = list(gate.get("rejection_reasons") or []) if isinstance(gate, dict) else []
        if isinstance(row, dict):
            reasons.extend(str(e.get("reason")) for e in row.get("rejected_edits", []) if isinstance(e, dict) and e.get("reason"))
        out.append({
            "iteration": row.get("iteration") if isinstance(row, dict) else None,
            "candidate_id": row.get("candidate_id") if isinstance(row, dict) else None,
            "rationale": gate.get("rationale") if isinstance(gate, dict) else row.get("reason") if isinstance(row, dict) else None,
            "current_score": gate.get("current_score") if isinstance(gate, dict) else None,
            "candidate_score": gate.get("candidate_score") if isinstance(gate, dict) else None,
            "edit_ops": [e.get("op") for e in edits if isinstance(e, dict)],
            "edit_signatures": [_edit_signature(e) for e in edits if isinstance(e, dict)],
            "rejection_reasons": sorted({r for r in reasons if r}),
            "validation_errors": row.get("validation_errors", []) if isinstance(row, dict) else [],
            "selection_rejection": bool(row.get("selection_rejection")) if isinstance(row, dict) else False,
            "reasoning": str(row.get("reasoning") or "")[:500] if isinstance(row, dict) else "",
        })
    return out


def analyze_rollout_reflections(train_tasks: list[EvalTask], current_eval: dict[str, Any]) -> dict[str, Any]:
    """Deterministically split train evidence into success/failure reflections.

    Labels are evidence-only optimizer context. Acceptance still belongs only to
    the frozen target validation gate.
    """

    tasks_by_id = {str(t.id): t for t in train_tasks}
    failures: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    counts = {"skill_defect": 0, "execution_lapse": 0, "success": 0}
    for row in current_eval.get("results") or []:
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("task_id") or row.get("id") or "")
        task = tasks_by_id.get(task_id)
        feedback = str(row.get("feedback") or row.get("reason") or "")
        raw_metadata = row.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        passed = bool(row.get("passed"))
        expected = list(getattr(task, "expected_terms", ()) or []) if task else []
        label = "success" if passed else _failure_label(feedback, metadata, expected)
        item = {
            "task_id": task_id,
            "passed": passed,
            "score": row.get("score"),
            "label": label,
            "expected_terms": expected[:8],
            "feedback_preview": feedback[:500],
        }
        if passed:
            counts["success"] += 1
            successes.append(item)
        else:
            counts[label] = counts.get(label, 0) + 1
            failures.append(item)
    return {
        "failure_reflections": failures,
        "success_reflections": successes,
        "reflection_counts": counts,
        "labeling_policy": "deterministic heuristic: missing expected skill keywords/assertions => skill_defect; blocked/timeouts/exceptions => execution_lapse",
    }


def _failure_label(feedback: str, metadata: dict[str, Any], expected_terms: list[str]) -> str:
    low = feedback.lower()
    if metadata.get("sandbox_command_blocked") or any(token in low for token in ("timeout", "exception", "exit=", "blocked", "tool error")):
        return "execution_lapse"
    if expected_terms or "failed=" in low or "expected_keyword" in low or "assertion:" in low:
        return "skill_defect"
    return "skill_defect"


def aggregate_edit_proposals(raw_edits: Any, reflection: dict[str, Any], edit_budget: int, rejected_context: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge backend/reflection proposals, dedupe, filter rejected, and clip."""

    budget = max(0, int(edit_budget))
    sources: list[tuple[str, Any]] = [("backend", raw_edits)]
    for key in ("edit_proposals", "proposed_edits", "edits"):
        if isinstance(reflection, dict) and key in reflection:
            sources.append((f"reflection.{key}", reflection.get(key)))
    rejected_sigs = {sig for row in (rejected_context or []) if isinstance(row, dict) for sig in row.get("edit_signatures", []) if isinstance(sig, str)}
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    raw_count = 0
    for source, edits in sources:
        if not isinstance(edits, list):
            continue
        for edit in edits:
            raw_count += 1
            if not isinstance(edit, dict):
                rejected.append({"source": source, "reason": "schema", "edit": repr(edit)[:300]})
                continue
            sig = _edit_signature(edit)
            if sig in seen:
                rejected.append({"source": source, "reason": "duplicate_in_aggregate", "signature": sig})
                continue
            seen.add(sig)
            if sig in rejected_sigs:
                rejected.append({"source": source, "reason": "previously_rejected", "signature": sig, "edit": {"op": edit.get("op")}})
                continue
            if len(merged) >= budget:
                rejected.append({"source": source, "reason": "edit_budget_clip", "signature": sig, "edit": {"op": edit.get("op")}})
                continue
            merged.append(edit)
    return merged, {
        "raw_edit_count": raw_count,
        "selected_edit_count": len(merged),
        "edit_budget": budget,
        "clipped_count": sum(1 for r in rejected if r.get("reason") == "edit_budget_clip"),
        "filtered_rejected_count": sum(1 for r in rejected if r.get("reason") == "previously_rejected"),
        "rejected": rejected,
        "strategy": "hierarchical_merge_backend_first_dedupe_rejected_filter_budget_clip",
    }


class OptimizerBackend:
    """Reflection + bounded edit generator.

    The optimizer can inspect rollout/evaluation evidence and propose edits, but
    it never decides acceptance. ValidationGate is the only accept/reject gate.
    """

    def __init__(self, backend: JsonBackend, edit_budget: int = 3, config: OptimizerBackendConfig | None = None):
        self.backend = backend
        self.edit_budget = max(0, int(edit_budget))
        mode = str(getattr(backend, "mode", "unknown"))
        self.config = config or OptimizerBackendConfig(backend=mode, requested_backend=mode, edit_budget=self.edit_budget)
        self.prompt_fingerprints: list[dict[str, Any]] = []

    def _record_prompt(self, *, kind: str, prompt: str, iteration: int, candidate_index: int | None = None) -> None:
        self.prompt_fingerprints.append({
            "kind": kind,
            "iteration": int(iteration),
            "candidate_index": candidate_index,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
        })

    def reflect(self, train_tasks: list[EvalTask], current_skill: str, current_eval: dict[str, Any], run_dir: Path, iteration: int, rejected_context: list[dict[str, Any]] | None = None, rejected_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if rejected_context is None and rejected_history is not None:
            rejected_context = summarize_rejected_edits(rejected_history)
        deterministic_reflection = analyze_rollout_reflections(train_tasks, current_eval)
        prompt = (
            "Reflect on Hermes SkillOpt train rollouts. "
            "Skill document is trainable state; target executor is frozen. Avoid repeating previously rejected or invalid edits.\n"
            "Separate failures from successes; label likely skill_defect vs execution_lapse where evidence supports it.\n"
            "TRAIN_TASKS=" + json.dumps([t.__dict__ for t in train_tasks], ensure_ascii=False)[:10000] + "\n"
            "CURRENT_EVAL=" + json.dumps(current_eval, ensure_ascii=False)[:10000] + "\n"
            "DETERMINISTIC_REFLECTION=" + json.dumps(deterministic_reflection, ensure_ascii=False)[:8000] + "\n"
            "REJECTED_EDIT_HISTORY=" + json.dumps(rejected_context or [], ensure_ascii=False)[:6000] + "\n"
            "CURRENT_SKILL=" + current_skill[:8000]
        )
        self._record_prompt(kind="reflect", prompt=prompt, iteration=iteration)
        data = self.backend.json(prompt, {"kind": "reflect"}, run_dir / f"llm_reflect_repair_{iteration}.json")
        if not isinstance(data, dict):
            data = {}
        data.update({k: v for k, v in deterministic_reflection.items() if k not in data})
        data["iteration"] = iteration
        data["optimizer_role"] = "reflection_only_no_acceptance"
        data["rejected_context_count"] = len(rejected_context or [])
        data["rejected_context"] = rejected_context or []
        return data

    def propose(self, reflection: dict[str, Any], current_skill: str, run_dir: Path, iteration: int, rejected_context: list[dict[str, Any]] | None = None) -> CandidateSkill:
        return self.propose_candidate(reflection, current_skill, run_dir, iteration, 1, rejected_context=rejected_context)

    def propose_candidate(self, reflection: dict[str, Any], current_skill: str, run_dir: Path, iteration: int, candidate_index: int, rejected_context: list[dict[str, Any]] | None = None) -> CandidateSkill:
        prompt = (
            "Generate bounded edits for Hermes SKILL.md trainable state. "
            f"Allowed ops: append, replace, delete, insert_after. Max edits: {self.edit_budget}. "
            "Do not edit YAML frontmatter, use unique anchors, do not repeat rejected edits, and do not write files directly.\n"
            f"CANDIDATE_INDEX={candidate_index}; generate a conservative distinct candidate for this index.\n"
            "REFLECTION=" + json.dumps(reflection, ensure_ascii=False)[:10000] + "\n"
            "REJECTED_EDIT_HISTORY=" + json.dumps(rejected_context or [], ensure_ascii=False)[:6000] + "\n"
            "SKILL=" + current_skill[:12000]
        )
        self._record_prompt(kind="edit", prompt=prompt, iteration=iteration, candidate_index=candidate_index)
        suffix = f"{iteration}_{candidate_index}"
        data = self.backend.json(prompt, {"kind": "edit"}, run_dir / f"llm_edit_repair_{suffix}.json")
        edits = data.get("edits") if isinstance(data, dict) else []
        edits, aggregate = aggregate_edit_proposals(edits, reflection, self.edit_budget, rejected_context)
        validation = validate_bounded_edits(current_skill, edits)
        validation_rejected = list(validation.rejected_edits)
        validation_rejected.extend(aggregate["rejected"])
        if not validation.ok:
            payload = {"iteration": iteration, "candidate_index": candidate_index, "errors": validation.errors, "rejected_edits": validation_rejected, "edits": edits, "aggregate": aggregate}
            reject_path = run_dir / f"candidate_{suffix}_edit_validation_rejected.json"
            reject_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if candidate_index == 1:
                (run_dir / f"candidate_{iteration}_edit_validation_rejected.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            candidate_text = current_skill
        else:
            candidate_text = apply_bounded_edits(current_skill, edits, strict=True)
        return CandidateSkill(
            iteration=iteration,
            text=candidate_text,
            edits=edits,
            reflection=reflection,
            reasoning=str(data.get("reasoning", "")) if isinstance(data, dict) else None,
            validation={"ok": validation.ok, "errors": validation.errors, "rejected_edits": validation_rejected, "diff_chars": validation.diff_chars, "aggregate": aggregate},
            candidate_id=f"candidate-{iteration}-{candidate_index}",
        )
