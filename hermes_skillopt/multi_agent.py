from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


REQUIRED_PACKAGE_FIELDS = ("goal", "scope", "acceptance", "verification", "slim_output")
VERBOSE_LOG_MARKERS = ("full log", "raw log", "transcript", "entire output", "complete stdout", "complete stderr")
ACCEPTANCE_MARKERS = ("acceptance", "done when", "success criteria", "must include", "验收", "完成标准")


@dataclass
class DispatcherPolicy:
    """Policy for dispatcher -> worker handoffs, not global prompt rewrites."""

    mode: str = "dispatcher_worker_handoff"
    default_worker: str = "focused implementation worker"
    split_policy: str = "delegate bounded, testable work packages with explicit acceptance and verification"
    context_budget_chars: int = 6000
    no_global_auto_adopt: bool = True


@dataclass
class SlimOutputContract:
    """Worker response contract optimized to reduce context bloat."""

    required_fields: list[str] = field(default_factory=lambda: [
        "status",
        "conclusion",
        "changed_files",
        "key_evidence",
        "risks_blockers",
        "recommended_next_step",
    ])
    forbidden: list[str] = field(default_factory=lambda: [
        "raw full logs unless explicitly requested",
        "large diffs or pasted file contents",
        "claims not backed by command/test evidence",
    ])
    max_words: int = 250


@dataclass
class ReviewerRubric:
    required_checks: list[str] = field(default_factory=lambda: [
        "all acceptance criteria addressed",
        "focused tests or meaningful verification ran",
        "no unrelated files or user dirty work overwritten",
        "output follows the slim contract",
    ])
    acceptance_omission_penalty: float = 0.35
    verbose_log_penalty: float = 0.20


@dataclass
class EscalationRules:
    retry_when: list[str] = field(default_factory=lambda: [
        "missing acceptance evidence",
        "tests were not run and no blocker was reported",
        "worker changed files outside delegated scope",
    ])
    escalate_when: list[str] = field(default_factory=lambda: [
        "requirements are contradictory or need user approval",
        "repository/worktree isolation cannot be established",
        "safe verification is blocked by unavailable credentials or services",
    ])
    max_retries: int = 1


@dataclass
class HandoffMetrics:
    context_size_chars: int
    context_size_score: float
    rework_risk: float
    acceptance_omissions: int
    verbose_log_risk: float
    total_score: float


@dataclass
class DelegateHandoffTemplate:
    kind: str
    dispatcher_policy: DispatcherPolicy
    context_package: dict[str, Any]
    worker_output_contract: SlimOutputContract
    reviewer_rubric: ReviewerRubric
    escalation_rules: EscalationRules
    metrics: HandoffMetrics
    recommendations: list[str]
    staged_only: bool = True
    no_global_auto_adopt: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_text(value: str | dict[str, Any]) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _extract_list(requirements: str, heading: str) -> list[str]:
    pattern = re.compile(rf"(?:^|\n)\s*(?:#+\s*)?{re.escape(heading)}\s*(?::|\n|\Z)\s*(.*?)(?=\n\s*(?:#+\s*)?[A-Z][A-Za-z _/-]+\s*:|\Z)", re.S | re.I)
    match = pattern.search(requirements)
    if not match:
        return []
    block = match.group(1).strip()
    items = []
    for line in block.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if cleaned:
            items.append(cleaned)
    return items or ([block] if block else [])


def _has_any(text: str, markers: tuple[str, ...] | list[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, round(value, 3)))


def build_delegate_handoff(requirements: str | dict[str, Any], *, worker: str | None = None, context_budget_chars: int = 6000) -> DelegateHandoffTemplate:
    """Build a deterministic delegate_task handoff template for dispatcher + worker workflows.

    This first-version optimizer is intentionally scoped to multi-agent handoff packaging:
    it does not rewrite global skills, does not call an LLM, and never adopts changes.
    """

    text = _as_text(requirements)
    title = text.splitlines()[0].strip("# :-") if text.splitlines() else "Delegated task"
    acceptance = _extract_list(text, "acceptance criteria") or _extract_list(text, "acceptance")
    verification = _extract_list(text, "verification") or _extract_list(text, "tests")
    scope = _extract_list(text, "scope") or _extract_list(text, "task scope")

    if not acceptance:
        acceptance = [
            "worker states concrete completion status",
            "worker provides command/test evidence or an explicit blocker",
            "worker reports changed files and risks",
        ]
    if not verification:
        verification = ["run focused tests or a meaningful no-network deterministic check"]
    if not scope:
        scope = ["bounded dispatcher + worker handoff optimization only; no global prompt auto-adoption"]

    policy = DispatcherPolicy(default_worker=worker or "focused implementation worker", context_budget_chars=context_budget_chars)
    contract = SlimOutputContract()
    rubric = ReviewerRubric()
    escalation = EscalationRules()
    package = {
        "goal": title or "Delegated task",
        "scope": scope,
        "requirements": text,
        "acceptance": acceptance,
        "verification": verification,
        "context_budget_chars": context_budget_chars,
        "handoff_instructions": [
            "preserve user dirty work and isolate risky edits",
            "act only inside the delegated scope",
            "return concise evidence, not raw logs",
        ],
        "slim_output": asdict(contract),
    }
    metrics = score_handoff(package)
    recommendations = recommend_handoff(metrics, package)
    return DelegateHandoffTemplate(
        kind="multi_agent_delegate_task_handoff",
        dispatcher_policy=policy,
        context_package=package,
        worker_output_contract=contract,
        reviewer_rubric=rubric,
        escalation_rules=escalation,
        metrics=metrics,
        recommendations=recommendations,
    )


def score_handoff(handoff: str | dict[str, Any]) -> HandoffMetrics:
    """Score a handoff package or worker result without network/LLM calls."""

    text = _as_text(handoff)
    data = handoff if isinstance(handoff, dict) else {}
    missing = 0
    for field_name in REQUIRED_PACKAGE_FIELDS:
        if isinstance(data, dict):
            value = data.get(field_name)
            missing += 0 if value else 1
        elif field_name.replace("_", " ") not in text.lower() and field_name not in text.lower():
            missing += 1

    if not _has_any(text, ACCEPTANCE_MARKERS) and not (isinstance(data, dict) and data.get("acceptance")):
        missing += 1

    verbose_hits = sum(1 for marker in VERBOSE_LOG_MARKERS if marker in text.lower())
    context_size = len(text)
    context_budget = 6000
    if isinstance(data, dict):
        try:
            context_budget = max(1, int(data.get("context_budget_chars") or context_budget))
        except (TypeError, ValueError):
            context_budget = 6000
    context_score = _clamp(1.0 - max(0, context_size - context_budget) / (context_budget * 2))
    omission_risk = min(1.0, missing / (len(REQUIRED_PACKAGE_FIELDS) + 1))
    verbose_risk = _clamp(min(1.0, verbose_hits * 0.35 + (0.25 if context_size > 10000 else 0)))
    rework = _clamp(0.15 + omission_risk * 0.55 + verbose_risk * 0.25)
    total = _clamp(context_score * 0.35 + (1 - rework) * 0.40 + (1 - omission_risk) * 0.25)
    return HandoffMetrics(
        context_size_chars=context_size,
        context_size_score=context_score,
        rework_risk=rework,
        acceptance_omissions=missing,
        verbose_log_risk=verbose_risk,
        total_score=total,
    )


def recommend_handoff(metrics: HandoffMetrics, handoff: str | dict[str, Any] | None = None) -> list[str]:
    recs = [
        "Use a reviewer gate against acceptance criteria before accepting worker output.",
        "Keep dispatcher policy scoped to delegate_task handoff packages; do not auto-adopt global prompt changes.",
        "Retry once when worker evidence is missing; escalate to the user/control plane when worktree isolation, credentials, or contradictory requirements block safe completion.",
    ]
    if metrics.acceptance_omissions:
        recs.append("Add explicit acceptance and verification bullets before dispatch, then retry once if evidence is missing.")
    if metrics.verbose_log_risk > 0:
        recs.append("Replace raw logs with concise command results, changed files, risks, and reviewer-ready evidence.")
    if metrics.context_size_score < 0.8:
        recs.append("Trim background context and pass only task-relevant files, constraints, and acceptance checks.")
    return recs


def optimize_delegate_handoff(requirements: str | dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Public API returning a JSON-serializable optimized handoff package."""

    return build_delegate_handoff(requirements, **kwargs).to_dict()
