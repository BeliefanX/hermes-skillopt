from __future__ import annotations

"""Optimizer backends: reflection plus bounded skill edits."""

import json
from pathlib import Path
from typing import Any, Protocol

from hermes_skillopt.bounded_edit import apply_bounded_edits
from hermes_skillopt.env import EvalTask
from hermes_skillopt.state import CandidateSkill


class JsonBackend(Protocol):
    mode: str

    def json(self, prompt: str, schema_hint: dict[str, Any], repair_path: Path | None = None) -> dict[str, Any]: ...


def summarize_rejected_edits(rejected: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Concise optimizer memory from prior rejected candidate edits."""

    out: list[dict[str, Any]] = []
    for row in rejected[-max(0, int(limit)):]:
        gate = row.get("gate") if isinstance(row, dict) else {}
        edits = row.get("edits", []) if isinstance(row, dict) else []
        out.append({
            "iteration": row.get("iteration") if isinstance(row, dict) else None,
            "rationale": gate.get("rationale") if isinstance(gate, dict) else None,
            "current_score": gate.get("current_score") if isinstance(gate, dict) else None,
            "candidate_score": gate.get("candidate_score") if isinstance(gate, dict) else None,
            "edit_ops": [e.get("op") for e in edits if isinstance(e, dict)],
            "reasoning": str(row.get("reasoning") or "")[:500] if isinstance(row, dict) else "",
        })
    return out


class OptimizerBackend:
    """Reflection + bounded edit generator.

    The optimizer can inspect rollout/evaluation evidence and propose edits, but
    it never decides acceptance. ValidationGate is the only accept/reject gate.
    """

    def __init__(self, backend: JsonBackend, edit_budget: int = 3):
        self.backend = backend
        self.edit_budget = max(0, int(edit_budget))

    def reflect(self, train_tasks: list[EvalTask], current_skill: str, current_eval: dict[str, Any], run_dir: Path, iteration: int, rejected_context: list[dict[str, Any]] | None = None, rejected_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if rejected_context is None and rejected_history is not None:
            rejected_context = summarize_rejected_edits(rejected_history)
        prompt = (
            "Reflect on Hermes SkillOpt train rollouts. "
            "Skill document is trainable state; target executor is frozen. Avoid repeating previously rejected edits.\n"
            "TRAIN_TASKS=" + json.dumps([t.__dict__ for t in train_tasks], ensure_ascii=False)[:10000] + "\n"
            "CURRENT_EVAL=" + json.dumps(current_eval, ensure_ascii=False)[:10000] + "\n"
            "REJECTED_EDIT_HISTORY=" + json.dumps(rejected_context or [], ensure_ascii=False)[:6000] + "\n"
            "CURRENT_SKILL=" + current_skill[:8000]
        )
        data = self.backend.json(prompt, {"kind": "reflect"}, run_dir / f"llm_reflect_repair_{iteration}.json")
        data["iteration"] = iteration
        data["optimizer_role"] = "reflection_only_no_acceptance"
        data["rejected_context_count"] = len(rejected_context or [])
        return data

    def propose(self, reflection: dict[str, Any], current_skill: str, run_dir: Path, iteration: int, rejected_context: list[dict[str, Any]] | None = None) -> CandidateSkill:
        prompt = (
            "Generate bounded edits for Hermes SKILL.md trainable state. "
            f"Allowed ops: append, replace, delete, insert_after. Max edits: {self.edit_budget}. "
            "Do not edit YAML frontmatter, do not repeat rejected edits, and do not write files directly.\n"
            "REFLECTION=" + json.dumps(reflection, ensure_ascii=False)[:10000] + "\n"
            "REJECTED_EDIT_HISTORY=" + json.dumps(rejected_context or [], ensure_ascii=False)[:6000] + "\n"
            "SKILL=" + current_skill[:12000]
        )
        data = self.backend.json(prompt, {"kind": "edit"}, run_dir / f"llm_edit_repair_{iteration}.json")
        edits = data.get("edits") if isinstance(data, dict) else []
        if not isinstance(edits, list):
            edits = []
        edits = edits[: self.edit_budget]
        candidate_text = apply_bounded_edits(current_skill, edits)
        return CandidateSkill(
            iteration=iteration,
            text=candidate_text,
            edits=edits,
            reflection=reflection,
            reasoning=str(data.get("reasoning", "")) if isinstance(data, dict) else None,
        )
