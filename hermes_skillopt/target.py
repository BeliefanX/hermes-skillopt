from __future__ import annotations

"""Frozen target executor for candidate/current skill comparisons."""

from dataclasses import dataclass

from hermes_skillopt.env import EvalResult, EvalTask


@dataclass(frozen=True)
class TargetExecutor:
    """Deterministic frozen executor used for both current and candidate skill.

    The executor never trains or edits.  It scores how well a skill document
    would guide the same frozen Hermes target behavior on the same tasks.
    """

    mode: str = "deterministic"

    def evaluate(self, skill_text: str, tasks: list[EvalTask], label: str = "skill") -> dict[str, object]:
        results = [self._score_task(skill_text, task) for task in tasks]
        total_weight = sum(max(0.0, float(task.weight)) for task in tasks)
        weighted = sum(r.score * max(0.0, float(task.weight)) for r, task in zip(results, tasks))
        mean = round(weighted / total_weight, 3) if results and total_weight > 0 else 0.0
        return {
            "label": label,
            "executor": self.mode,
            "score": mean,
            "num_tasks": len(results),
            "total_weight": round(total_weight, 3),
            "results": [r.__dict__ for r in results],
        }

    def _score_task(self, skill_text: str, task: EvalTask) -> EvalResult:
        low = skill_text.lower()
        score = 0.20
        matched: list[str] = []
        expected = task.expected_terms or ("verify", "tool")
        for term in expected:
            variants = {term.lower()}
            if term == "verify":
                variants |= {"verification", "verified"}
            if term == "test":
                variants |= {"pytest", "tests"}
            if term == "guard":
                variants |= {"path guard", "sha", "safety"}
            if term == "staged":
                variants |= {"stage", "staging", "staged-only"}
            if any(v in low for v in variants):
                score += 0.12
                matched.append(term)
        if "skillopt learned rules" in low or "skillopt candidate improvements" in low:
            score += 0.12
            matched.append("learned_rules")
        if "validation" in low and "gate" in low:
            score += 0.10
            matched.append("validation_gate")
        if "bounded" in low and "edit" in low:
            score += 0.08
            matched.append("bounded_edit")
        penalties = [term for term in task.failure_terms if term.lower() in low]
        score -= 0.15 * len(penalties)
        score = round(max(0.0, min(1.0, score)), 3)
        return EvalResult(
            task_id=task.id,
            score=score,
            passed=score >= 0.55,
            evidence=f"matched={matched}; penalties={penalties}",
            metadata={"source": task.source, "prompt": task.prompt, "split": task.split, "weight": task.weight, "success_criteria": task.success_criteria},
        )
