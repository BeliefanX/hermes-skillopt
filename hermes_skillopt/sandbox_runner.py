from __future__ import annotations

"""Tiny default command for HermesSkillOpt sandbox executor tests.

It reads the sandbox-staged SKILL.md path passed by HermesSandboxRunner and
prints deterministic markers.  It never touches the live profile; HOME and
HERMES_HOME are expected to be sandbox temp directories.
"""

import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("missing SKILL.md path")
        return 2
    path = Path(argv[0]).resolve()
    hermes_home = Path(os.environ.get("HERMES_HOME", "")).resolve()
    if not hermes_home or hermes_home not in path.parents:
        print("SANDBOX_ISOLATION_FAILED")
        return 3
    text = path.read_text(encoding="utf-8")
    print("SANDBOX_OK")
    print(f"SKILL_CHARS={len(text)}")
    for marker in ("verify", "rollback", "blocker", "tool"):
        if marker in text.lower():
            print(f"MARKER:{marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
