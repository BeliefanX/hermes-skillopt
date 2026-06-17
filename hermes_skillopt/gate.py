from __future__ import annotations

"""Validation gate for SkillOpt candidates."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateMetricPolicy:
    """Deterministic hard/soft/mixed/strict metric gate policy.

    default ``soft`` preserves Phase0 behavior: candidate weighted validation
    score must strictly improve and the edit must not be a no-op. ``strict`` is
    a stronger policy: it also requires hard weighted pass-rate non-regression
    and no previously-passing task failures unless explicitly allowed.
    """

    mode: str = "soft"  # soft|hard|mixed|strict
    min_delta: float = 0.0
    hard_regression_allowed: bool = False

    def normalized_mode(self) -> str:
        return (self.mode or "soft").lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.normalized_mode(),
            "requested_mode": self.mode,
            "min_delta": float(self.min_delta),
            "hard_regression_allowed": bool(self.hard_regression_allowed),
            "deterministic": True,
            "llm_override_allowed": False,
            "policy_semantics": self._semantics(),
        }

    def _semantics(self) -> str:
        mode = self.normalized_mode()
        if mode == "soft":
            return "non-no-op candidate and soft weighted score strictly improves by min_delta"
        if mode == "hard":
            return "non-no-op candidate and hard weighted pass-rate strictly improves; per-task pass regressions fail unless hard_regression_allowed"
        if mode == "mixed":
            return "non-no-op candidate, soft weighted score strictly improves by min_delta, and hard weighted pass-rate does not regress; per-task pass regressions fail unless hard_regression_allowed"
        if mode == "strict":
            return "non-no-op candidate, soft weighted score strictly improves by min_delta, hard weighted pass-rate does not regress, and previously passing tasks remain passing unless hard_regression_allowed is explicit"
        return "unsupported gate mode"


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
    policy: GateMetricPolicy = GateMetricPolicy()
    metric_summary: dict[str, Any] | None = None
    rejection_reasons: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        policy = self.policy.as_dict()
        mode = policy["mode"]
        return {
            "iteration": self.iteration,
            "current_score": self.current_score,
            "candidate_score": self.candidate_score,
            "accepted": self.accepted,
            "rationale": self.rationale,
            "current_eval": self.current_eval,
            "candidate_eval": self.candidate_eval,
            "judge": self.judge,
            "metric_policy": policy,
            "metric_summary": self.metric_summary or {},
            "rejection_reasons": self.rejection_reasons or [],
            "acceptance_rule": f"deterministic {mode} metric gate from frozen-target held-out validation; LLM judge is explanation-only",
        }


class ValidationGate:
    """The only inner acceptance gate.

    Supports soft, hard, mixed, and strict metric policies. LLM judge output is
    recorded as auxiliary evidence and cannot override deterministic metric
    decisions.
    """

    def __init__(self, policy: GateMetricPolicy | None = None, *, gate_mode: str | None = None, min_delta: float = 0.0, hard_regression_allowed: bool = False):
        if policy is None:
            policy = GateMetricPolicy(mode=gate_mode or "soft", min_delta=min_delta, hard_regression_allowed=hard_regression_allowed)
        self.policy = policy

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
        metrics = self._metric_summary(current_eval, candidate_eval)
        mode = self.policy.normalized_mode()
        min_delta = float(self.policy.min_delta)
        no_op = candidate_text == current_text
        rejection_reasons: list[str] = []
        if no_op:
            rejection_reasons.append("candidate edit was a no-op")

        soft_ok = metrics["soft_delta"] > min_delta
        task_regressions = metrics.get("per_task_regressions") or []
        per_task_nonregress = not task_regressions or bool(self.policy.hard_regression_allowed)
        hard_improved = metrics["hard_delta"] > 0.0 and per_task_nonregress
        hard_nonregress = (metrics["hard_delta"] >= 0.0 and per_task_nonregress) or bool(self.policy.hard_regression_allowed)

        if mode == "soft":
            metric_ok = soft_ok
            if not soft_ok:
                rejection_reasons.append("soft weighted score did not strictly improve")
        elif mode == "hard":
            metric_ok = hard_improved
            if not hard_improved:
                rejection_reasons.append("hard pass-rate metric did not strictly improve or a previously passing task regressed")
        elif mode == "mixed":
            metric_ok = soft_ok and hard_nonregress
            if not soft_ok:
                rejection_reasons.append("mixed gate soft weighted score did not strictly improve")
            if not hard_nonregress:
                rejection_reasons.append("mixed gate hard pass-rate regressed")
        elif mode == "strict":
            strict_hard_nonregress = metrics["hard_delta"] >= 0.0
            strict_task_nonregress = not task_regressions or bool(self.policy.hard_regression_allowed)
            metric_ok = soft_ok and strict_hard_nonregress and strict_task_nonregress
            if not soft_ok:
                rejection_reasons.append("strict gate soft weighted score did not strictly improve")
            if not strict_hard_nonregress:
                rejection_reasons.append("strict gate hard weighted pass-rate regressed")
            if task_regressions and not bool(self.policy.hard_regression_allowed):
                rejection_reasons.append("strict gate previously passing task regressed")
        else:
            raise ValueError(f"unsupported gate mode: {self.policy.mode}")

        accepted = bool(not no_op and metric_ok)
        rationale = (
            f"accepted: candidate satisfied deterministic {mode} validation gate"
            if accepted
            else "rejected: " + "; ".join(rejection_reasons or ["validation metrics did not improve"])
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
            policy=self.policy,
            metric_summary=metrics,
            rejection_reasons=rejection_reasons,
        )

    def _metric_summary(self, current_eval: dict[str, Any], candidate_eval: dict[str, Any]) -> dict[str, Any]:
        current_soft = float(current_eval.get("score", 0.0))
        candidate_soft = float(candidate_eval.get("score", 0.0))
        current_hard = self._weighted_pass_rate(current_eval)
        candidate_hard = self._weighted_pass_rate(candidate_eval)
        return {
            "current": {"soft_score": current_soft, "hard_pass_rate": current_hard},
            "candidate": {"soft_score": candidate_soft, "hard_pass_rate": candidate_hard},
            "soft_delta": round(candidate_soft - current_soft, 6),
            "hard_delta": round(candidate_hard - current_hard, 6),
            "per_task_regressions": self._per_task_regressions(current_eval, candidate_eval),
            "mixed": {
                "soft_improved": candidate_soft > current_soft + float(self.policy.min_delta),
                "hard_nonregression": candidate_hard >= current_hard and not self._per_task_regressions(current_eval, candidate_eval) or bool(self.policy.hard_regression_allowed),
            },
            "strict": {
                "soft_improved": candidate_soft > current_soft + float(self.policy.min_delta),
                "hard_nonregression": candidate_hard >= current_hard,
                "per_task_nonregression": not self._per_task_regressions(current_eval, candidate_eval) or bool(self.policy.hard_regression_allowed),
            },
        }

    def _per_task_regressions(self, current_eval: dict[str, Any], candidate_eval: dict[str, Any]) -> list[dict[str, Any]]:
        current_rows = [r for r in (current_eval.get("results") or []) if isinstance(r, dict)]
        candidate_rows = [r for r in (candidate_eval.get("results") or []) if isinstance(r, dict)]
        by_id = {str(r.get("task_id") or r.get("id") or i): r for i, r in enumerate(candidate_rows)}
        regressions: list[dict[str, Any]] = []
        for i, row in enumerate(current_rows):
            task_id = str(row.get("task_id") or row.get("id") or i)
            cand = by_id.get(task_id)
            if cand is None:
                continue
            current_passed = bool(row.get("passed"))
            candidate_passed = bool(cand.get("passed"))
            if current_passed and not candidate_passed:
                regressions.append({
                    "task_id": task_id,
                    "current_passed": current_passed,
                    "candidate_passed": candidate_passed,
                    "current_score": row.get("score"),
                    "candidate_score": cand.get("score"),
                })
        return regressions

    def _weighted_pass_rate(self, eval_result: dict[str, Any]) -> float:
        rows = [r for r in (eval_result.get("results") or []) if isinstance(r, dict)]
        if not rows:
            return 0.0
        weights = []
        for row in rows:
            raw_meta = row.get("metadata")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            weights.append(max(0.0, float(meta.get("weight", 1.0) or 1.0)))
        total = sum(weights)
        if total <= 0:
            return 0.0
        passed = sum(w for w, row in zip(weights, rows) if bool(row.get("passed")))
        return round(passed / total, 6)
