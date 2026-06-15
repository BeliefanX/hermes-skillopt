from __future__ import annotations

"""Frozen target evaluator abstractions for current/candidate comparisons.

Default tests and smoke runs use a deterministic scorecard/replay runner. It is
frozen (same config + same tasks for current and candidate), fast, and safe, but
it is explicitly reported as ``deterministic_mock_scorecard`` rather than a live
Hermes rollout. Production adoption is gated separately by manifest fields that
require explicit curated scorecards.
"""

from dataclasses import dataclass
from typing import Protocol

from hermes_skillopt.env import EvalResult, EvalTask


class ScorecardRunner(Protocol):
    mode: str

    def score(self, skill_text: str, task: EvalTask) -> EvalResult: ...


@dataclass(frozen=True)
class DeterministicKeywordScorecard:
    """Deterministic/mock fallback scorecard for replay tasks.

    This is intentionally not presented as a real Hermes executor. It scores a
    frozen skill document against explicit task keywords/forbidden terms and is
    suitable for tests, smoke runs, and review-only fallback runs.
    """

    mode: str = "deterministic_mock_scorecard"

    def score(self, skill_text: str, task: EvalTask) -> EvalResult:
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
            evidence=f"runner={self.mode}; matched={matched}; penalties={penalties}",
            metadata={
                "source": task.source,
                "prompt": task.prompt,
                "split": task.split,
                "weight": task.weight,
                "success_criteria": task.success_criteria,
                "scorecard_explicit": bool(task.metadata.get("scorecard_explicit")),
                "production_gate_eligible": bool(task.metadata.get("production_gate_eligible")),
            },
        )


@dataclass(frozen=True)
class TargetExecutor:
    """Frozen evaluator used for both current and candidate skill.

    The executor never trains or edits. It applies the same frozen runner/config
    to the same task set for both labels. A future live Hermes rollout runner can
    implement ScorecardRunner and be injected without changing ValidationGate.
    """

    runner: ScorecardRunner | None = None
    target_config_id: str = "frozen-local-scorecard-v1"

    def __post_init__(self) -> None:
        if self.runner is None:
            object.__setattr__(self, "runner", DeterministicKeywordScorecard())

    @property
    def mode(self) -> str:
        assert self.runner is not None
        return self.runner.mode

    def evaluate(self, skill_text: str, tasks: list[EvalTask], label: str = "skill") -> dict[str, object]:
        assert self.runner is not None
        results = [self.runner.score(skill_text, task) for task in tasks]
        total_weight = sum(max(0.0, float(task.weight)) for task in tasks)
        weighted = sum(r.score * max(0.0, float(task.weight)) for r, task in zip(results, tasks))
        mean = round(weighted / total_weight, 3) if results and total_weight > 0 else 0.0
        production_gate_eligible = bool(results) and all(bool(r.metadata.get("production_gate_eligible")) for r in results)
        return {
            "label": label,
            "executor": self.mode,
            "target_config_id": self.target_config_id,
            "score": mean,
            "num_tasks": len(results),
            "total_weight": round(total_weight, 3),
            "production_gate_eligible": production_gate_eligible,
            "results": [r.__dict__ for r in results],
        }
