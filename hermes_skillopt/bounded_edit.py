from __future__ import annotations

"""Single implementation of bounded SKILL.md edits.

Bounded edits intentionally operate only on the markdown body. YAML
frontmatter is preserved byte-for-byte so skill identity/metadata cannot be
mutated by optimizer proposals.
"""

import re
from typing import Any


def frontmatter_split(text: str) -> tuple[str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            after = end + len("\n---")
            if after < len(text) and text[after] == "\n":
                after += 1
            return text[:after], text[after:]
    return "", text


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


def apply_bounded_edits(current: str, edits: list[dict[str, Any]]) -> str:
    fm, body = frontmatter_split(current)
    new_body = body
    for edit in edits:
        new_body = _apply_one(new_body, edit)
    return fm + new_body
