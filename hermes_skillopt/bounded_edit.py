from __future__ import annotations

"""Single implementation of bounded SKILL.md edits.

Bounded edits intentionally operate only on the markdown body. YAML
frontmatter is preserved byte-for-byte so skill identity/metadata cannot be
mutated by optimizer proposals.  Optimizer-facing callers use strict validation
so malformed, repeated, no-op, over-budget, or frontmatter/section-protected
edits become reviewable rejection evidence instead of silent mutations.
"""

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

MAX_EDIT_CHARS = 12_000
MAX_DIFF_CHARS = 24_000
PROTECTED_HEADINGS = {"system", "developer", "safety", "profile isolation"}
PROTECTED_REGION_START = "<!-- skillopt:protected:start -->"
PROTECTED_REGION_END = "<!-- skillopt:protected:end -->"
ALLOWED_REGION_START = "<!-- skillopt:allowed:start -->"
ALLOWED_REGION_END = "<!-- skillopt:allowed:end -->"
BOUNDARY_MARKERS = (
    PROTECTED_REGION_START,
    PROTECTED_REGION_END,
    ALLOWED_REGION_START,
    ALLOWED_REGION_END,
)
ALLOWED_OPS = {"append", "replace", "delete", "insert_after"}


@dataclass(frozen=True)
class EditValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    rejected_edits: list[dict[str, Any]] = field(default_factory=list)
    diff_chars: int = 0


def frontmatter_split(text: str) -> tuple[str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            after = end + len("\n---")
            if after < len(text) and text[after] == "\n":
                after += 1
            return text[:after], text[after:]
    return "", text


def _marker_spans(body: str, start_marker: str, end_marker: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        s = body.find(start_marker, start)
        if s < 0:
            return spans
        e = body.find(end_marker, s + len(start_marker))
        if e < 0:
            spans.append((s, len(body)))
            return spans
        spans.append((s, e + len(end_marker)))
        start = e + len(end_marker)


def _overlaps(span: tuple[int, int], other: tuple[int, int]) -> bool:
    return max(span[0], other[0]) < min(span[1], other[1])


def _target_span(body: str, anchor: str) -> tuple[int, int] | None:
    if not anchor:
        return None
    pos = body.find(anchor)
    if pos < 0:
        return None
    return pos, pos + len(anchor)


def _protected_section_span(body: str, anchor: str) -> tuple[int, int] | None:
    target = _target_span(body, anchor)
    if target is None:
        return None
    for span in _marker_spans(body, PROTECTED_REGION_START, PROTECTED_REGION_END):
        if _overlaps(target, span):
            return span
    pos = target[0]
    heading_start = body.rfind("\n## ", 0, pos)
    if heading_start < 0:
        heading_start = 0 if body.startswith("## ") else -1
    if heading_start < 0:
        return None
    line_end = body.find("\n", heading_start + 1)
    heading = body[heading_start: line_end if line_end >= 0 else len(body)].strip("# \n\t").lower()
    if heading not in PROTECTED_HEADINGS:
        return None
    next_heading = body.find("\n## ", line_end if line_end >= 0 else heading_start)
    return heading_start, len(body) if next_heading < 0 else next_heading


def _allowed_region_violation(body: str, anchor: str) -> bool:
    """When explicit allowed-region markers exist, bounded edits must target one."""

    spans = _marker_spans(body, ALLOWED_REGION_START, ALLOWED_REGION_END)
    if not spans:
        return False
    target = _target_span(body, anchor)
    if target is None:
        return True
    return not any(_overlaps(target, span) for span in spans)


def _edit_payload_chars(edit: dict[str, Any]) -> int:
    return sum(len(str(edit.get(k, ""))) for k in ("text", "old", "new", "anchor"))


def _protected_heading_in_text(text: str) -> str | None:
    for match in re.finditer(r"^##\s+(.+?)\s*$", text, flags=re.M):
        heading = match.group(1).strip().lower()
        if heading in PROTECTED_HEADINGS:
            return heading
    return None


def _unsafe_new_text_reason(text: str) -> tuple[str, str | None] | None:
    protected_heading = _protected_heading_in_text(text)
    if protected_heading is not None or PROTECTED_REGION_START in text or PROTECTED_REGION_END in text:
        return "protected_heading_or_marker", protected_heading
    if ALLOWED_REGION_START in text or ALLOWED_REGION_END in text:
        return "allowed_region_marker_mutation", None
    return None


def _contains_boundary_marker(text: str) -> bool:
    return any(marker in text for marker in BOUNDARY_MARKERS)


def _apply_one(body: str, edit: dict[str, Any]) -> str:
    op = edit.get("op")
    if op == "append":
        text = str(edit.get("text", ""))
        heading = None
        m = re.search(r"^##\s+.+$", text.strip(), flags=re.M)
        if m:
            heading = re.escape(m.group(0).strip())
        if heading and re.search(rf"^({heading})$", body, flags=re.M):
            return re.sub(rf"\n*{heading}\n.*?(?=\n##\s|\Z)", "\n\n" + text.strip() + "\n", body, count=1, flags=re.S | re.M)
        return body.rstrip() + text + "\n"
    if op == "replace":
        old = str(edit.get("old", ""))
        new = str(edit.get("new", ""))
        return body.replace(old, new, 1) if old and old in body else body
    if op == "delete":
        old = str(edit.get("text") or edit.get("old") or "")
        return body.replace(old, "", 1) if old and old in body else body
    if op == "insert_after":
        anchor = str(edit.get("anchor", ""))
        text = str(edit.get("text", ""))
        return body.replace(anchor, anchor + text, 1) if anchor and anchor in body else body.rstrip() + text + "\n"
    return body


def validate_bounded_edits(
    current: str,
    edits: list[dict[str, Any]],
    *,
    max_edit_chars: int = MAX_EDIT_CHARS,
    max_diff_chars: int = MAX_DIFF_CHARS,
) -> EditValidationResult:
    errors: list[str] = []
    rejected: list[dict[str, Any]] = []
    if not isinstance(edits, list):
        return EditValidationResult(False, ["edits must be a list"], [{"reason": "schema", "edit": repr(edits)[:500]}])
    fm, body = frontmatter_split(current)
    seen: set[str] = set()
    trial = body
    for idx, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            errors.append(f"edit {idx} must be an object")
            rejected.append({"index": idx, "reason": "schema", "edit": repr(edit)[:500]})
            continue
        op = edit.get("op")
        sig = repr(sorted(edit.items()))
        if sig in seen:
            errors.append(f"edit {idx} repeats a prior edit")
            rejected.append({"index": idx, "reason": "repeated_edit", "edit": edit})
            continue
        seen.add(sig)
        if op not in ALLOWED_OPS:
            errors.append(f"edit {idx} has unsupported op {op!r}")
            rejected.append({"index": idx, "reason": "unknown_op", "edit": edit})
            continue
        if _edit_payload_chars(edit) > max_edit_chars:
            errors.append(f"edit {idx} exceeds max character budget")
            rejected.append({"index": idx, "reason": "max_chars", "edit": {"op": op}})
            continue
        if op == "append":
            text = str(edit.get("text") or "")
            if not text.strip():
                errors.append(f"edit {idx} append text is empty")
                rejected.append({"index": idx, "reason": "empty_text", "edit": edit})
                continue
            unsafe = _unsafe_new_text_reason(text)
            if unsafe and unsafe[0] == "protected_heading_or_marker":
                errors.append(f"edit {idx} append text targets protected heading/region")
                rejected.append({"index": idx, "reason": "protected_append", "protected_heading": unsafe[1], "edit": edit})
                continue
            if unsafe and unsafe[0] == "allowed_region_marker_mutation":
                errors.append(f"edit {idx} append text attempts to create/move allowed-region markers")
                rejected.append({"index": idx, "reason": "allowed_region_marker_mutation", "edit": edit})
                continue
            if _allowed_region_violation(body, ""):
                errors.append(f"edit {idx} targets outside allowed region")
                rejected.append({"index": idx, "reason": "outside_allowed_region", "edit": edit})
                continue
        elif op == "replace":
            old = str(edit.get("old") or "")
            new = str(edit.get("new") or "")
            if not old or "\n---" in old or old.startswith("---"):
                errors.append(f"edit {idx} invalid/protected replace anchor")
                rejected.append({"index": idx, "reason": "protected_frontmatter", "edit": edit})
                continue
            if _contains_boundary_marker(old):
                errors.append(f"edit {idx} replace anchor attempts to mutate boundary markers")
                rejected.append({"index": idx, "reason": "boundary_marker_mutation", "edit": edit})
                continue
            unsafe = _unsafe_new_text_reason(new)
            if unsafe and unsafe[0] == "protected_heading_or_marker":
                errors.append(f"edit {idx} replace text targets protected heading/region")
                rejected.append({"index": idx, "reason": "protected_replace", "protected_heading": unsafe[1], "edit": edit})
                continue
            if unsafe and unsafe[0] == "allowed_region_marker_mutation":
                errors.append(f"edit {idx} replace text attempts to create/move allowed-region markers")
                rejected.append({"index": idx, "reason": "allowed_region_marker_mutation", "edit": edit})
                continue
            if body.count(old) != 1:
                errors.append(f"edit {idx} replace anchor must be unique")
                rejected.append({"index": idx, "reason": "non_unique_anchor", "edit": {"op": op, "old": old[:200]}})
                continue
            if _allowed_region_violation(body, old):
                errors.append(f"edit {idx} targets outside allowed region")
                rejected.append({"index": idx, "reason": "outside_allowed_region", "edit": edit})
                continue
            if _protected_section_span(body, old):
                errors.append(f"edit {idx} targets protected section")
                rejected.append({"index": idx, "reason": "protected_section", "edit": edit})
                continue
        elif op == "delete":
            old = str(edit.get("text") or edit.get("old") or "")
            if not old or "\n---" in old or old.startswith("---"):
                errors.append(f"edit {idx} invalid/protected delete anchor")
                rejected.append({"index": idx, "reason": "protected_frontmatter", "edit": edit})
                continue
            if _contains_boundary_marker(old):
                errors.append(f"edit {idx} delete anchor attempts to mutate boundary markers")
                rejected.append({"index": idx, "reason": "boundary_marker_mutation", "edit": edit})
                continue
            if body.count(old) != 1:
                errors.append(f"edit {idx} delete anchor must be unique")
                rejected.append({"index": idx, "reason": "non_unique_anchor", "edit": {"op": op, "old": old[:200]}})
                continue
            if _allowed_region_violation(body, old):
                errors.append(f"edit {idx} targets outside allowed region")
                rejected.append({"index": idx, "reason": "outside_allowed_region", "edit": edit})
                continue
            if _protected_section_span(body, old):
                errors.append(f"edit {idx} targets protected section")
                rejected.append({"index": idx, "reason": "protected_section", "edit": edit})
                continue
        elif op == "insert_after":
            anchor = str(edit.get("anchor") or "")
            text = str(edit.get("text") or "")
            if not anchor or "\n---" in anchor or anchor.startswith("---"):
                errors.append(f"edit {idx} invalid/protected insert anchor")
                rejected.append({"index": idx, "reason": "protected_frontmatter", "edit": edit})
                continue
            if _contains_boundary_marker(anchor):
                errors.append(f"edit {idx} insert_after anchor attempts to mutate boundary markers")
                rejected.append({"index": idx, "reason": "boundary_marker_mutation", "edit": edit})
                continue
            unsafe = _unsafe_new_text_reason(text)
            if unsafe and unsafe[0] == "protected_heading_or_marker":
                errors.append(f"edit {idx} insert_after text targets protected heading/region")
                rejected.append({"index": idx, "reason": "protected_insert", "protected_heading": unsafe[1], "edit": edit})
                continue
            if unsafe and unsafe[0] == "allowed_region_marker_mutation":
                errors.append(f"edit {idx} insert_after text attempts to create/move allowed-region markers")
                rejected.append({"index": idx, "reason": "allowed_region_marker_mutation", "edit": edit})
                continue
            if body.count(anchor) != 1:
                errors.append(f"edit {idx} insert_after anchor must be unique")
                rejected.append({"index": idx, "reason": "non_unique_anchor", "edit": {"op": op, "anchor": anchor[:200]}})
                continue
            if _allowed_region_violation(body, anchor):
                errors.append(f"edit {idx} targets outside allowed region")
                rejected.append({"index": idx, "reason": "outside_allowed_region", "edit": edit})
                continue
            if _protected_section_span(body, anchor):
                errors.append(f"edit {idx} targets protected section")
                rejected.append({"index": idx, "reason": "protected_section", "edit": edit})
                continue
        before = trial
        trial = _apply_one(trial, edit)
        if trial == before:
            errors.append(f"edit {idx} is a no-op")
            rejected.append({"index": idx, "reason": "no_op", "edit": edit})
    candidate = fm + trial
    if frontmatter_split(candidate)[0] != fm:
        errors.append("candidate frontmatter changed")
        rejected.append({"reason": "frontmatter_changed"})
    diff = "".join(difflib.unified_diff(current.splitlines(True), candidate.splitlines(True)))
    if len(diff) > max_diff_chars:
        errors.append("candidate diff exceeds budget")
        rejected.append({"reason": "diff_budget", "diff_chars": len(diff)})
    if not errors and candidate == current:
        errors.append("edit plan is a no-op")
        rejected.append({"reason": "no_op_plan"})
    return EditValidationResult(ok=not errors, errors=errors, rejected_edits=rejected, diff_chars=len(diff))


def apply_bounded_edits(current: str, edits: list[dict[str, Any]], *, strict: bool = False) -> str:
    if strict:
        result = validate_bounded_edits(current, edits)
        if not result.ok:
            raise ValueError("bounded edit validation failed: " + "; ".join(result.errors))
    fm, body = frontmatter_split(current)
    new_body = body
    for edit in edits:
        new_body = _apply_one(new_body, edit)
    return fm + new_body
