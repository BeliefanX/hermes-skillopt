from __future__ import annotations

"""Local deterministic conformance/regression runner."""

import json
import os
import subprocess
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CONFORMANCE_SCHEMA_VERSION = "hermes-skillopt-conformance-v1"
CONFORMANCE_MODES = ("quick", "full")
QUICK_PYTEST_ARGS = ["tests/test_phase2_env_adapter.py", "tests/test_p3.py"]


def _run(cmd: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    tail = proc.stdout[-8000:]
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "output_tail": tail,
        "output_tail_chars": len(tail),
        "output_total_chars": len(proc.stdout),
        "output_truncated": len(proc.stdout) > len(tail),
        "output_tail_sha256": hashlib.sha256(tail.encode("utf-8")).hexdigest(),
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_conformance_report_path(output_path: str | Path, *, repo_root: Path) -> Path:
    """Resolve a conformance report path without allowing live/runtime writeback targets."""

    out = Path(output_path).expanduser().resolve()
    if out.exists() and (out.is_symlink() or not out.is_file()):
        raise ValueError("conformance output_path must be a regular report file")
    if out.name == "SKILL.md":
        raise ValueError("conformance output_path is report-only and may not target live skills")
    blocked_roots = [repo_root / "skills", repo_root / "plugins", repo_root / "cron", repo_root / "memories", repo_root / "config"]
    home_raw = os.environ.get("HERMES_HOME")
    if home_raw:
        home = Path(home_raw).expanduser().resolve()
        blocked_roots.extend([home / "skills", home / "plugins", home / "cron", home / "memories", home / "config"])
    if any(_is_relative_to(out, root.resolve()) for root in blocked_roots):
        raise ValueError("conformance output_path is report-only and may not target live skills/plugins/config/memories/cron")
    return out


def run_conformance(*, repo_root: str | Path | None = None, output_path: str | Path | None = None, pytest_args: Iterable[str] | None = None, timeout: int = 180, mode: str = "quick") -> dict[str, Any]:
    """Run local conformance and emit a JSON report.

    ``mode='quick'`` is the default deterministic smoke/regression suite and is
    intentionally not a full repository health check. Use ``mode='full'`` to run
    all pytest tests after compileall.
    """

    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
    if mode not in CONFORMANCE_MODES:
        raise ValueError(f"unsupported conformance mode {mode!r}; expected quick|full")
    if pytest_args is not None:
        args = list(pytest_args)
        suite = "custom-pytest-args"
        scope_note = "Custom pytest args supplied by caller; this is not necessarily a full repository health check."
    elif mode == "full":
        args = ["tests"]
        suite = "full-local-pytest"
        scope_note = "Full local repository pytest suite plus compileall; still no external services/upstream parity certification."
    else:
        args = QUICK_PYTEST_ARGS.copy()
        suite = "quick-local-deterministic"
        scope_note = "Quick deterministic conformance smoke suite only; not a full repository health check. Use mode='full' for all tests."
    if output_path:
        out = _safe_conformance_report_path(output_path, repo_root=root)
    else:
        out = root / "skillopt_conformance_report.json"
    commands = [
        _run([sys.executable, "-m", "compileall", "-q", "hermes_skillopt", "tests"], root, timeout),
        _run([sys.executable, "-m", "pytest", *args], root, timeout),
    ]
    payload = {
        "schema_version": CONFORMANCE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(root),
        "mode": mode,
        "suite": suite,
        "pytest_args": args,
        "scope_note": scope_note,
        "quick_is_full_repo_health": False,
        "external_services_required": False,
        "commands": commands,
        "passed": all(c["passed"] for c in commands),
        "alignment_checks": {
            "microsoft_skillopt_alignment": [
                "trainable skill state remains a Hermes SKILL.md document",
                "target executor is frozen and provenance-fingerprinted",
                "optimizer proposes bounded edits staged behind validation gates",
                "benchmark/eval packs use train/val/test split governance",
            ],
            "hermes_divergences": [
                "no upstream code execution or vendoring",
                "live skill writes require explicit adopt/rollback guards",
                "mock/sample provenance is review-only and non-adoptable",
            ],
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"success": payload["passed"], "report_path": str(out), "report": payload}
