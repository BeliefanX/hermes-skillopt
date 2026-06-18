from __future__ import annotations

"""Local deterministic conformance/regression runner."""

import json
import subprocess
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_skillopt.safety import guard_safe_output_path

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


def _safe_conformance_report_path(output_path: str | Path, *, repo_root: Path) -> Path:
    """Resolve a conformance report path without allowing live/runtime writeback targets."""

    return guard_safe_output_path(output_path, kind="conformance", repo_root=repo_root, required_suffix=".json")


def run_conformance(*, repo_root: str | Path | None = None, output_path: str | Path | None = None, pytest_args: Iterable[str] | None = None, timeout: int = 180, mode: str = "quick") -> dict[str, Any]:
    """Run local conformance and optionally emit a JSON report.

    ``mode='quick'`` is the default deterministic smoke/regression suite and is
    intentionally not a full repository health check. Use ``mode='full'`` to run
    all pytest tests after compileall. When ``output_path`` is omitted, no report
    file is written; the report is returned in-memory with ``report_path=None``.
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
    out = _safe_conformance_report_path(output_path, repo_root=root) if output_path else None
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
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"success": payload["passed"], "report_path": str(out) if out is not None else None, "report": payload}
