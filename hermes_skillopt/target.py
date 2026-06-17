from __future__ import annotations

"""Frozen target evaluator abstractions for current/candidate comparisons.

Default tests and smoke runs use deterministic local scorecard/replay runners.
They are frozen (same config + same tasks for current and candidate), fast, and
safe, but are explicitly reported as local replay/scorecard rather than a live
Hermes rollout. Production adoption is gated separately by manifest fields that
require explicit curated scorecards.
"""

from dataclasses import dataclass
from typing import Protocol

from hermes_skillopt.env import EvalResult, EvalTask


class ScorecardRunner(Protocol):
    @property
    def mode(self) -> str: ...

    def score(self, skill_text: str, task: EvalTask) -> EvalResult: ...


def _variants(term: str) -> set[str]:
    term_l = term.lower()
    variants = {term_l}
    if term_l == "verify":
        variants |= {"verification", "verified"}
    if term_l == "test":
        variants |= {"pytest", "tests"}
    if term_l == "guard":
        variants |= {"path guard", "sha", "safety"}
    if term_l == "staged":
        variants |= {"stage", "staging", "staged-only"}
    return variants


@dataclass(frozen=True)
class DeterministicKeywordScorecard:
    """Deterministic/mock fallback scorecard for smoke tasks.

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
            if any(v in low for v in _variants(term)):
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
            metadata=_result_metadata(task),
        )


@dataclass(frozen=True)
class HermesRolloutRunner:
    """Safe offline/local replay runner for curated Hermes eval tasks.

    MVP semantics: no live profile writes, no tool execution, no LLM judge. It
    evaluates skill text against frozen task assertions, expected behavior,
    expected/forbidden keywords, and success criteria. The same runner instance
    and same task set are used for current and candidate by TargetExecutor.
    """

    mode: str = "hermes_replay_runner_mvp"

    def score(self, skill_text: str, task: EvalTask) -> EvalResult:
        low = skill_text.lower()
        checks: list[tuple[str, bool, float]] = []
        for term in task.expected_terms:
            checks.append((f"expected_keyword:{term}", any(v in low for v in _variants(term)), 1.0))
        for assertion in task.assertions:
            name, passed = self._assertion_passed(low, assertion)
            checks.append((name, passed, float(assertion.get("weight", 1.0) or 1.0)))
        for criterion in task.success_criteria:
            # Only short criteria become local replay checks; long criteria are evidence.
            words = criterion.lower().split()
            if 0 < len(words) <= 3:
                checks.append((f"success_criteria:{criterion}", criterion.lower() in low, 0.75))
        if not checks and task.expected_behavior:
            for word in task.expected_behavior.lower().split():
                if len(word) >= 4:
                    checks.append((f"expected_behavior:{word}", word in low, 0.25))
        penalties = [term for term in task.failure_terms if term.lower() in low]
        total = sum(max(0.0, weight) for _, _, weight in checks)
        passed_weight = sum(max(0.0, weight) for _, ok, weight in checks if ok)
        base = passed_weight / total if total > 0 else DeterministicKeywordScorecard().score(skill_text, task).score
        score = round(max(0.0, min(1.0, base - 0.20 * len(penalties))), 3)
        passed_names = [name for name, ok, _ in checks if ok]
        failed_names = [name for name, ok, _ in checks if not ok]
        return EvalResult(
            task_id=task.id,
            score=score,
            passed=score >= 0.55 and not penalties,
            evidence=f"runner={self.mode}; passed={passed_names}; failed={failed_names}; penalties={penalties}; judge=not_used_for_acceptance",
            metadata={**_result_metadata(task), "assertion_count": len(task.assertions), "assertion_results": {name: ok for name, ok, _ in checks if name.startswith("assertion:")}, "judge_present": bool(task.judge)},
        )

    def _assertion_passed(self, low_skill: str, assertion: dict[str, object]) -> tuple[str, bool]:
        typ = str(assertion.get("type") or assertion.get("op") or "contains").lower()
        value = str(assertion.get("value") or assertion.get("text") or assertion.get("keyword") or "").lower()
        if typ in {"contains", "keyword", "must_contain"}:
            return f"assertion:{typ}:{value}", bool(value and value in low_skill)
        if typ in {"not_contains", "forbidden", "must_not_contain"}:
            return f"assertion:{typ}:{value}", bool(value and value not in low_skill)
        if typ == "all_keywords":
            raw_values = assertion.get("values") or assertion.get("keywords") or []
            if isinstance(raw_values, str):
                values = [raw_values]
            elif isinstance(raw_values, (list, tuple, set)):
                values = list(raw_values)
            else:
                values = []
            vals = [str(v).lower() for v in values]
            return f"assertion:all_keywords:{','.join(vals)}", bool(vals and all(v in low_skill for v in vals))
        return f"assertion:unsupported:{typ}", False


@dataclass(frozen=True)
class TargetExecutor:
    """Frozen evaluator used for both current and candidate skill.

    The executor never trains or edits. It applies the same frozen runner/config
    to the same task set for both labels. A future live Hermes rollout runner can
    implement ScorecardRunner and be injected without changing ValidationGate.
    """

    runner: ScorecardRunner | None = None
    target_config_id: str = "frozen-hermes-replay-mvp-v1"

    def __post_init__(self) -> None:
        if self.runner is None:
            object.__setattr__(self, "runner", HermesRolloutRunner())

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


def _result_metadata(task: EvalTask) -> dict[str, object]:
    return {
        "source": task.source,
        "prompt": task.prompt,
        "split": task.split,
        "weight": task.weight,
        "success_criteria": task.success_criteria,
        "expected_behavior": task.expected_behavior,
        "allowed_tools": task.allowed_tools,
        "timeout": task.timeout,
        "fixtures": task.fixtures,
        "scorecard_explicit": bool(task.metadata.get("scorecard_explicit")),
        "production_gate_eligible": bool(task.metadata.get("production_gate_eligible")),
    }
