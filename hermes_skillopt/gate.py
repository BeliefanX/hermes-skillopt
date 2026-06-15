from __future__ import annotations

"""Validation gate for SkillOpt candidates."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateDecision:
    iteration: int
    current_score: float
    candidate_score: float
    accepted: bool
    rationale: str
    current_eval: dict[str, Any]
    candidate_eval: dict[str, Any]
    judge: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "current_score": self.current_score,
            "candidate_score": self.candidate_score,
            "accepted": self.accepted,
            "rationale": self.rationale,
            "current_eval": self.current_eval,
            "candidate_eval": self.candidate_eval,
            "judge": self.judge,
            "acceptance_rule": "candidate_score > current_score from frozen-target held-out validation",
        }


class ValidationGate:
    """The only inner acceptance gate: validation candidate_score > current_score."""

    def decide(
        self,
        iteration: int,
        current_eval: dict[str, Any],
        candidate_eval: dict[str, Any],
        current_text: str,
        candidate_text: str,
        judge: dict[str, Any] | None = None,
    ) -> GateDecision:
        current_score = float(current_eval.get("score", 0.0))
        candidate_score = float(candidate_eval.get("score", 0.0))
        accepted = bool(candidate_text != current_text and candidate_score > current_score)
        rationale = (
            "accepted: candidate validation score strictly improved"
            if accepted
            else "rejected: validation score did not strictly improve or edit was a no-op"
        )
        if judge:
            rationale += "; LLM judge recorded as auxiliary explanation only"
        return GateDecision(
            iteration=iteration,
            current_score=current_score,
            candidate_score=candidate_score,
            accepted=accepted,
            rationale=rationale,
            current_eval=current_eval,
            candidate_eval=candidate_eval,
            judge=judge,
        )
