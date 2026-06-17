from __future__ import annotations

import pytest

from hermes_skillopt.bounded_edit import apply_bounded_edits, validate_bounded_edits


SKILL = "---\nname: demo\ndescription: keep me\n---\n# Demo\n\nUse tools safely.\n"


def test_bounded_edits_preserve_frontmatter_for_replace_delete_insert_after():
    out = apply_bounded_edits(
        SKILL,
        [
            {"op": "replace", "old": "Use tools safely.", "new": "Use tools safely and verify."},
            {"op": "insert_after", "anchor": "# Demo\n", "text": "\nAlways preserve profile isolation.\n"},
            {"op": "delete", "text": "and verify"},
        ],
    )

    assert out.startswith("---\nname: demo\ndescription: keep me\n---\n")
    assert "Always preserve profile isolation." in out
    assert "Use tools safely ." in out
    assert "description: keep me" in out


def test_bounded_insert_after_fallback_stays_in_body_not_frontmatter():
    out = apply_bounded_edits(SKILL, [{"op": "insert_after", "anchor": "missing anchor", "text": "\n## Added\n\nVerify first."}])

    assert out.startswith("---\nname: demo\ndescription: keep me\n---\n# Demo")
    assert out.rstrip().endswith("## Added\n\nVerify first.")


def test_strict_bounded_validation_rejects_unknown_repeated_noop_and_frontmatter():
    edits = [
        {"op": "wat", "text": "x"},
        {"op": "replace", "old": "missing", "new": "x"},
        {"op": "replace", "old": "---\nname: demo", "new": "---\nname: pwn"},
        {"op": "append", "text": "\n## Added\n\nVerify first."},
        {"op": "append", "text": "\n## Added\n\nVerify first."},
    ]
    result = validate_bounded_edits(SKILL, edits)
    assert not result.ok
    reasons = {r["reason"] for r in result.rejected_edits}
    assert {"unknown_op", "non_unique_anchor", "protected_frontmatter", "repeated_edit"} <= reasons
    with pytest.raises(ValueError, match="bounded edit validation failed"):
        apply_bounded_edits(SKILL, edits, strict=True)


def test_strict_bounded_validation_rejects_protected_section_edit():
    skill = SKILL + "\n## Safety\n\nDo not weaken this guard.\n"
    result = validate_bounded_edits(skill, [{"op": "delete", "text": "Do not weaken this guard."}])
    assert not result.ok
    assert any(r["reason"] == "protected_section" for r in result.rejected_edits)
