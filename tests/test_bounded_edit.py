from __future__ import annotations

from hermes_skillopt.bounded_edit import apply_bounded_edits


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
