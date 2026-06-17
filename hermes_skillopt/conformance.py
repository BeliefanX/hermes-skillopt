from __future__ import annotations

"""Local deterministic conformance/regression runner."""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CONFORMANCE_SCHEMA_VERSION = "hermes-skillopt-conformance-v1"


def _run(cmd: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "output_tail": proc.stdout[-8000:],
    }


def run_conformance(*, repo_root: str | Path | None = None, output_path: str | Path | None = None, pytest_args: Iterable[str] | None = None, timeout: int = 180) -> dict[str, Any]:
    """Run compileall plus a deterministic pytest suite and emit a JSON report."""

    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
    args = list(pytest_args or ["tests/test_phase2_env_adapter.py", "tests/test_p3.py"])
    commands = [
        _run([sys.executable, "-m", "compileall", "-q", "hermes_skillopt", "tests"], root, timeout),
        _run([sys.executable, "-m", "pytest", *args], root, timeout),
    ]
    payload = {
        "schema_version": CONFORMANCE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(root),
        "suite": "local-deterministic",
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
    if output_path:
        out = Path(output_path).expanduser().resolve()
    else:
        out = root / "skillopt_conformance_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"success": payload["passed"], "report_path": str(out), "report": payload}
