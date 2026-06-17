from __future__ import annotations

"""Read-only transfer evaluation for staged skills/eval packs across targets."""

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from hermes_skillopt.env import EvalTask, load_eval_pack
from hermes_skillopt.target import DeterministicKeywordScorecard, HermesRolloutRunner, HermesSandboxRunner, TargetExecutor

TRANSFER_REPORT_SCHEMA_VERSION = "hermes-skillopt-transfer-eval-v1"


def _stable_sha(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _profile_fingerprint(home: Path) -> dict[str, Any]:
    payload = {"hermes_home": str(home.resolve()), "skills_dir": str((home / "skills").resolve())}
    return {**payload, "fingerprint_sha256": _stable_sha(payload)}


def _runner_for_target(target: str):
    if target == "scorecard":
        return DeterministicKeywordScorecard()
    if target == "sandbox":
        return HermesSandboxRunner()
    if target == "replay":
        return HermesRolloutRunner()
    raise ValueError(f"unsupported transfer target {target!r}; expected scorecard|replay|sandbox")


def _read_jsonl_tasks(path: Path) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if not isinstance(rec, dict):
            raise ValueError(f"task artifact {path} line {i} is not an object")
        tasks.append(
            EvalTask(
                id=str(rec.get("id") or f"task-{i}"),
                prompt=str(rec.get("prompt") or ""),
                source=str(rec.get("source") or "staged-artifact"),
                expected_behavior=str(rec.get("expected_behavior") or ""),
                assertions=tuple(rec.get("assertions") or ()),
                judge=str(rec.get("judge") or "keyword_scorecard"),
                allowed_tools=tuple(rec.get("allowed_tools") or ()),
                timeout=float(rec.get("timeout") or 30.0),
                fixtures=dict(rec.get("fixtures") or {}),
                expected_terms=tuple(rec.get("expected_terms") or ()),
                failure_terms=tuple(rec.get("failure_terms") or ()),
                required_markers=tuple(rec.get("required_markers") or ()),
                forbidden_markers=tuple(rec.get("forbidden_markers") or ()),
                split=str(rec.get("split") or "validation"),
                weight=float(rec.get("weight") or 1.0),
                success_criteria=tuple(rec.get("success_criteria") or ()),
                metadata=dict(rec.get("metadata") or {}),
            )
        )
    return tasks


def _load_staged_tasks(run_dir: Path) -> tuple[list[EvalTask], dict[str, Any]]:
    files = {"val": run_dir / "val_items.jsonl", "test": run_dir / "test_items.jsonl"}
    tasks: list[EvalTask] = []
    split_counts: dict[str, int] = {}
    for split, path in files.items():
        if path.exists() and path.is_file() and not path.is_symlink():
            part = _read_jsonl_tasks(path)
            tasks.extend(part)
            split_counts[split] = len(part)
    if not tasks:
        raise ValueError("staged run has no val_items.jsonl/test_items.jsonl; pass eval_file")
    meta = {
        "source": "staged-run-artifacts",
        "task_count": len(tasks),
        "split_counts": split_counts,
        "fingerprint_sha256": _stable_sha([t.__dict__ for t in tasks]),
    }
    return tasks, meta


def _resolve_skill_text(*, home: Path, run_id: str | None, skill_file: str | None, staged_only: bool) -> tuple[str, dict[str, Any], Path | None]:
    if run_id:
        from hermes_skillopt import core  # lazy import avoids core import cycle at module import time

        run_dir = core.resolve_run_dir(home, run_id)
        manifest = core.load_manifest(run_dir)
        core.verify_artifact_hashes(run_dir, manifest)
        proposed = run_dir / "proposed_SKILL.md"
        if not proposed.exists() or proposed.is_symlink():
            raise ValueError("staged run missing safe proposed_SKILL.md")
        text = proposed.read_text(encoding="utf-8")
        return text, {"source": "staged_run", "run_id": run_id, "run_dir": str(run_dir), "proposed_sha256": hashlib.sha256(text.encode()).hexdigest(), "manifest_status": manifest.get("status")}, run_dir
    if staged_only and not skill_file:
        raise ValueError("transfer_eval defaults to staged/report-only; pass run_id or an explicit staged skill_file")
    if not skill_file:
        raise ValueError("skill_file is required when run_id is not provided")
    path = Path(skill_file).expanduser().resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise ValueError("skill_file must resolve to a regular file")
    text = path.read_text(encoding="utf-8")
    return text, {"source": "skill_file", "path": str(path), "sha256": hashlib.sha256(text.encode()).hexdigest()}, None


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_report_output_path(output_path: str, *, home: Path) -> Path:
    """Resolve a report output path without allowing live skill writeback targets."""

    out = Path(output_path).expanduser().resolve()
    skills_dir = (home / "skills").resolve()
    if out.name == "SKILL.md" or _is_relative_to(out, skills_dir):
        raise ValueError("transfer_eval output_path is report-only and may not target live skills or HERMES_HOME/skills")
    if out.exists() and (out.is_symlink() or not out.is_file()):
        raise ValueError("transfer_eval output_path must be a regular report file")
    return out


def transfer_eval(*, hermes_home_path: str | None = None, run_id: str | None = None, skill_file: str | None = None, eval_file: str | None = None, targets: Iterable[str] | None = None, profile_homes: Iterable[str] | None = None, output_path: str | None = None, staged_only: bool = True) -> dict[str, Any]:
    """Evaluate staged/proposed skill text across target/profile configs without writing live skills."""

    from hermes_skillopt import core

    home = core.hermes_home(hermes_home_path)
    target_names = tuple(targets or ("scorecard",))
    profile_paths = tuple(Path(p).expanduser().resolve() for p in (profile_homes or (str(home),)))
    skill_text, skill_source, run_dir = _resolve_skill_text(home=home, run_id=run_id, skill_file=skill_file, staged_only=staged_only)
    if eval_file:
        eval_path = Path(eval_file).expanduser()
        eval_path = eval_path if eval_path.is_absolute() else home / eval_path
        tasks, meta = load_eval_pack(eval_path.resolve(strict=True))
        eval_meta = meta.as_dict()
    elif run_dir:
        tasks, eval_meta = _load_staged_tasks(run_dir)
    else:
        raise ValueError("eval_file is required when staged task artifacts are not available")

    task_splits = sorted({t.split for t in tasks})
    evaluations: list[dict[str, Any]] = []
    for profile_home in profile_paths:
        profile_fp = _profile_fingerprint(profile_home)
        for target_name in target_names:
            executor = TargetExecutor(runner=_runner_for_target(target_name), requested_executor=target_name)
            result = executor.evaluate(skill_text, list(tasks), label=f"transfer:{target_name}")
            evaluations.append(
                {
                    "profile_home": str(profile_home),
                    "profile_fingerprint": profile_fp,
                    "target": target_name,
                    "target_config": executor.config.as_dict(),
                    "target_fingerprint_sha256": result.get("target_fingerprint_sha256"),
                    "score": result.get("score"),
                    "num_tasks": result.get("num_tasks"),
                    "splits": result.get("splits"),
                    "production_gate_eligible": result.get("production_gate_eligible"),
                    "regression_cases": result.get("regression_cases"),
                    "result": result,
                }
            )
    payload = {
        "schema_version": TRANSFER_REPORT_SCHEMA_VERSION,
        "mode": "report-only-read-only",
        "live_skill_writeback": False,
        "staged_only": bool(staged_only),
        "hermes_home": str(home),
        "skill_source": skill_source,
        "skill_fingerprint_sha256": hashlib.sha256(skill_text.encode("utf-8")).hexdigest(),
        "eval_pack": eval_meta,
        "task_count": len(tasks),
        "task_splits": task_splits,
        "targets": list(target_names),
        "profiles": [str(p) for p in profile_paths],
        "evaluations": evaluations,
    }
    payload["report_fingerprint_sha256"] = _stable_sha(payload)
    if output_path:
        out = _safe_report_output_path(output_path, home=home)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        payload["output_path"] = str(out)
    return {"success": True, "report": payload}
