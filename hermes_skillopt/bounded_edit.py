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


def _protected_section_span(body: str, anchor: str) -> tuple[int, int] | None:
    if not anchor:
        return None
    pos = body.find(anchor)
    if pos < 0:
        return None
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


def _edit_payload_chars(edit: dict[str, Any]) -> int:
    return sum(len(str(edit.get(k, ""))) for k in ("text", "old", "new", "anchor"))


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
            if not str(edit.get("text") or "").strip():
                errors.append(f"edit {idx} append text is empty")
                rejected.append({"index": idx, "reason": "empty_text", "edit": edit})
                continue
        elif op == "replace":
            old = str(edit.get("old") or "")
            if not old or "\n---" in old or old.startswith("---"):
                errors.append(f"edit {idx} invalid/protected replace anchor")
                rejected.append({"index": idx, "reason": "protected_frontmatter", "edit": edit})
                continue
            if body.count(old) != 1:
                errors.append(f"edit {idx} replace anchor must be unique")
                rejected.append({"index": idx, "reason": "non_unique_anchor", "edit": {"op": op, "old": old[:200]}})
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
            if body.count(old) != 1:
                errors.append(f"edit {idx} delete anchor must be unique")
                rejected.append({"index": idx, "reason": "non_unique_anchor", "edit": {"op": op, "old": old[:200]}})
                continue
            if _protected_section_span(body, old):
                errors.append(f"edit {idx} targets protected section")
                rejected.append({"index": idx, "reason": "protected_section", "edit": edit})
                continue
        elif op == "insert_after":
            anchor = str(edit.get("anchor") or "")
            if not anchor or "\n---" in anchor or anchor.startswith("---"):
                errors.append(f"edit {idx} invalid/protected insert anchor")
                rejected.append({"index": idx, "reason": "protected_frontmatter", "edit": edit})
                continue
            if body.count(anchor) != 1:
                errors.append(f"edit {idx} insert_after anchor must be unique")
                rejected.append({"index": idx, "reason": "non_unique_anchor", "edit": {"op": op, "anchor": anchor[:200]}})
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
