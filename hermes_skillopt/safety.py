from __future__ import annotations

"""Shared filesystem guards for report/eval output artifacts."""

import os
from pathlib import Path


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_home(hermes_home: str | Path | None) -> Path | None:
    home_raw = hermes_home or os.environ.get("HERMES_HOME")
    if not home_raw:
        return None
    return Path(home_raw).expanduser().resolve()


def guard_safe_output_path(
    output_path: str | Path,
    *,
    kind: str,
    hermes_home: str | Path | None = None,
    repo_root: str | Path | None = None,
    required_suffix: str | None = None,
) -> Path:
    """Resolve an output path while blocking live/runtime-sensitive targets.

    This guard is intentionally shared by transfer/import/conformance report-style
    writers.  Under HERMES_HOME, outputs must live below explicit skillopt
    eval/staging/report directories.  Outside HERMES_HOME, temporary or review
    report locations are allowed, but obvious live/runtime/plugin-source targets
    and symlink escapes are rejected.
    """

    raw = Path(output_path).expanduser()
    if raw.exists() and (raw.is_symlink() or not raw.is_file()):
        raise ValueError(f"{kind} output_path must be a regular report/eval file")
    if required_suffix and raw.suffix.lower() != required_suffix.lower():
        raise ValueError(f"{kind} output_path must be a {required_suffix} report/eval file")

    out = raw.resolve()
    blocked_names = {"SKILL.md", "plugin.yaml", "config.json", "memory.json", "memories.json", "cron.json"}
    blocked_parts = {"skills", "plugins", "plugin", "cron", "memory", "memories", "config", "runtime", "runs"}
    if out.name in blocked_names:
        raise ValueError(f"{kind} output_path is report-only and may not target live skills/plugins/config/memories/cron")

    home = _resolve_home(hermes_home)
    if home is not None:
        raw_abs = raw if raw.is_absolute() else Path.cwd() / raw
        raw_lexical_home = is_relative_to(raw_abs, home)
        resolved_home = is_relative_to(out, home)
        allowed_roots = [home / "skillopt" / "evals", home / "skillopt" / "staging", home / "skillopt" / "reports"]
        resolved_allowed = any(is_relative_to(out, root.resolve()) for root in allowed_roots)
        lexical_allowed = any(is_relative_to(raw_abs, root.resolve(strict=False)) for root in allowed_roots)
        if raw_lexical_home or resolved_home:
            if not (resolved_allowed and lexical_allowed):
                raise ValueError(f"{kind} output_path under HERMES_HOME must be an explicit skillopt eval/staging/report output path")
            return out

    root = Path(repo_root).expanduser().resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
    repo_blocked_roots = [root / "hermes_skillopt", root / "skills", root / "plugins", root / "cron", root / "memories", root / "config"]
    if any(is_relative_to(out, blocked.resolve()) for blocked in repo_blocked_roots) or out in {root / "__init__.py", root / "plugin.yaml"}:
        raise ValueError(f"{kind} output_path may not target plugin/repo source or runtime-sensitive paths")

    if any(part in blocked_parts for part in out.parts):
        raise ValueError(f"{kind} output_path may not target live skills, plugins, config, memory, cron, or runtime-sensitive paths")
    return out
