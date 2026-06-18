from __future__ import annotations

"""Deterministic advisory skill type classification.

The classifier is intentionally lightweight and explainable. Its output is
metadata for inventory/recommendation UX only; it is not an adoption gate and
must not be used to relax eval-pack or production safety policy.
"""

import re
from pathlib import Path
from typing import Any

from hermes_skillopt import core

SKILL_TYPE_POLICY_VERSION = "hermes-skill-type-advisory-v1"

CATEGORY_TEMPLATES: dict[str, list[str]] = {
    "safety_governance": ["policy/safety conformance", "risk review", "approval/rollback checklist"],
    "tool_runbook": ["tool preflight", "step-by-step runbook", "verification and rollback"],
    "software_development_reviewer": ["code change plan", "tests/lint/build verification", "review checklist"],
    "research_writer_creative": ["research brief", "outline/draft", "source/evidence notes"],
    "domain_specialist": ["domain checklist", "structured analysis", "expert caveats"],
    "general": ["task clarification", "concise answer", "verification notes"],
}

_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "safety_governance",
        ("safety", "governance", "policy", "compliance", "guardrail", "risk", "permission", "approval", "rollback", "refuse", "redact", "secret", "pii", "security"),
        ("security-reviewer", "governance", "compliance", "safety", "policy"),
    ),
    (
        "tool_runbook",
        ("runbook", "tool", "terminal", "command", "procedure", "workflow", "checklist", "incident", "operations", "deploy", "monitor", "troubleshoot", "playbook"),
        ("runbook", "ops", "sre", "tool", "workflow"),
    ),
    (
        "software_development_reviewer",
        ("code", "software", "developer", "review", "repository", "git", "diff", "patch", "test", "pytest", "lint", "build", "bug", "refactor", "api", "python", "typescript", "javascript"),
        ("coder", "reviewer", "developer", "software", "code-review"),
    ),
    (
        "research_writer_creative",
        ("research", "write", "writer", "draft", "creative", "story", "essay", "summarize", "literature", "outline", "narrative", "brainstorm", "copy", "article"),
        ("research", "writer", "creative", "content"),
    ),
    (
        "domain_specialist",
        ("medical", "legal", "finance", "accounting", "math", "science", "biology", "chemistry", "physics", "education", "marketing", "sales", "hr", "real estate", "domain", "expert"),
        ("legal", "finance", "medical", "domain", "expert"),
    ),
)


def _frontmatter_map(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    data: dict[str, str] = {}
    for raw in text[4:end].splitlines():
        line = raw.strip()
        if not line or ":" not in line or line.startswith("#"):
            continue
        key, value = line.split(":", 1)
        data[key.strip().lower()] = value.strip().strip('"\'')
    return data


def classify_skill_type(skill: core.Skill, *, text: str | None = None) -> dict[str, Any]:
    """Return advisory skill-type metadata for a Hermes SKILL.md.

    Categories are deterministic and based only on path/frontmatter/body marker
    matches. Confidence is heuristic, and the result is explicitly advisory.
    """

    body = text if text is not None else skill.path.read_text(encoding="utf-8")
    fm = _frontmatter_map(body)
    path_markers = [part.lower() for part in Path(skill.relpath).parts]
    frontmatter_text = " ".join(str(v).lower() for v in fm.values())
    body_lower = body.lower()
    haystacks = {
        "path": " ".join(path_markers),
        "frontmatter": frontmatter_text,
        "body": body_lower,
    }
    scored: list[tuple[str, float, list[str]]] = []
    for category, terms, path_terms in _RULES:
        score = 0.0
        reasons: list[str] = []
        for marker in path_terms:
            if marker in haystacks["path"]:
                score += 3.0
                reasons.append(f"path marker: {marker}")
            if marker in haystacks["frontmatter"]:
                score += 2.0
                reasons.append(f"frontmatter marker: {marker}")
        for term in terms:
            pattern = r"(?<![a-z0-9_-])" + re.escape(term) + r"(?![a-z0-9_-])"
            if re.search(pattern, haystacks["frontmatter"]):
                score += 1.5
                reasons.append(f"frontmatter term: {term}")
            elif re.search(pattern, haystacks["body"]):
                score += 1.0
                reasons.append(f"body term: {term}")
        if score:
            scored.append((category, score, reasons))
    scored.sort(key=lambda item: (-item[1], item[0]))
    if scored:
        category, score, reasons = scored[0]
        confidence = "high" if score >= 6 else "medium" if score >= 3 else "low"
        alternatives = [{"category": c, "score": s, "reasons": rs[:5]} for c, s, rs in scored[1:4]]
    else:
        category, score, reasons, confidence, alternatives = "general", 0.0, ["no specific path/frontmatter/body markers matched"], "low", []
    return {
        "policy_version": SKILL_TYPE_POLICY_VERSION,
        "category": category,
        "confidence": confidence,
        "score": round(score, 3),
        "reasons": reasons[:10],
        "templates": CATEGORY_TEMPLATES[category],
        "alternatives": alternatives,
        "advisory_only": True,
        "hard_gate": False,
    }
