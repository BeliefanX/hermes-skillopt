from __future__ import annotations

"""Eval-pack inventory and review-only scaffold helpers."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from hermes_skillopt import core
from hermes_skillopt.env import EVAL_PACK_SCHEMA_VERSION, load_eval_pack, production_eligibility_for_task
from hermes_skillopt.safety import guard_safe_output_path
from hermes_skillopt.skill_types import classify_skill_type


def _candidate_eval_paths(home: Path, skill: core.Skill) -> list[Path]:
    names = {skill.name, skill.path.parent.name, Path(skill.relpath).parent.name}
    out: list[Path] = []
    for name in names:
        for suffix in (".json", ".jsonl"):
            out.append(home / "skillopt" / "evals" / f"{name}{suffix}")
    local_eval_dir = skill.path.parent / "evals"
    if local_eval_dir.is_dir():
        out.extend(sorted(local_eval_dir.glob("*.json")))
        out.extend(sorted(local_eval_dir.glob("*.jsonl")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in out:
        try:
            r = p.expanduser().resolve(strict=False)
        except Exception:
            r = p
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def _pack_report(path: Path) -> dict[str, Any]:
    try:
        tasks, meta = load_eval_pack(path)
    except Exception as exc:
        return {"path": str(path), "exists": path.exists(), "valid": False, "error_type": type(exc).__name__, "error": core.redact_secrets(str(exc)), "production_eligible": False, "review_only": True, "missing_reasons": ["eval pack failed validation"]}
    split_counts = dict(meta.split_counts)
    missing = [s for s in ("train", "val", "test") if int(split_counts.get(s) or 0) <= 0]
    prod_tasks = [t for t in tasks if production_eligibility_for_task(t).eligible]
    reasons = list((meta.production_policy or {}).get("refusal_reasons") or [])
    if missing:
        reasons.append("missing complete splits: " + ", ".join(missing))
    if not prod_tasks:
        reasons.append("no production-eligible val/test tasks")
    production_eligible = not missing and bool(prod_tasks) and bool((meta.production_policy or {}).get("allow_production_adoption"))
    return {
        "path": str(path),
        "exists": True,
        "valid": True,
        "pack_id": meta.pack_id,
        "version": meta.version,
        "schema_version": meta.schema_version,
        "fingerprint_sha256": meta.fingerprint_sha256,
        "task_count": meta.task_count,
        "split_counts": split_counts,
        "production_eligible_task_count": len(prod_tasks),
        "production_eligible": production_eligible,
        "review_only": not production_eligible,
        "missing_reasons": sorted(set(str(r) for r in reasons if r)),
        "sample_pack": bool((meta.production_policy or {}).get("sample_pack")),
        "eval_execution_contract": meta.eval_execution_contract,
    }


def _pack_split_complete(pack: dict[str, Any]) -> bool:
    split_counts_raw = pack.get("split_counts")
    split_counts: dict[str, Any] = split_counts_raw if isinstance(split_counts_raw, dict) else {}
    return bool(pack.get("valid")) and all(int(split_counts.get(s) or 0) > 0 for s in ("train", "val", "test"))


def _execution_contract_bucket(pack: dict[str, Any]) -> str:
    contract_raw = pack.get("eval_execution_contract")
    contract: dict[str, Any] = contract_raw if isinstance(contract_raw, dict) else {}
    classification = str(contract.get("classification") or "invalid_or_missing_contract")
    if not pack.get("valid"):
        return "invalid_pack"
    return classification


def _recommended_next_action(*, packs: list[dict[str, Any]], invalid_packs: list[dict[str, Any]]) -> str:
    if not packs:
        return "scaffold_review_eval_pack"
    if invalid_packs:
        return "fix_invalid_eval_pack"
    if any(p.get("production_eligible") for p in packs):
        return "ready_for_strict_eval_run"
    incomplete = [p for p in packs if not _pack_split_complete(p)]
    if incomplete:
        return "complete_train_val_test_splits"
    static = [p for p in packs if _execution_contract_bucket(p) in {"static_keyword_scorecard", "static_review_only"}]
    if static:
        return "replace_static_review_pack_with_curated_replay_or_frozen_target_contract"
    return "curate_production_policy_and_explicit_val_test_scorecards"


def _readiness_matrix(entries: list[dict[str, Any]]) -> dict[str, Any]:
    invalid_reasons: dict[str, int] = {}
    split_completeness = {"complete": 0, "incomplete": 0, "invalid": 0, "no_pack": 0}
    execution_contract_buckets: dict[str, int] = {}
    no_pack = 0
    only_review_only = 0
    production_eligible = 0
    invalid_pack_count = 0
    per_skill_next_action: dict[str, str] = {}
    for entry in entries:
        packs_raw = entry.get("eval_packs")
        packs: list[dict[str, Any]] = packs_raw if isinstance(packs_raw, list) else []
        per_skill_next_action[str(entry.get("skill"))] = str(entry.get("recommended_next_action"))
        if not packs:
            no_pack += 1
            split_completeness["no_pack"] += 1
        elif entry.get("production_eligible"):
            production_eligible += 1
        skill_has_invalid = False
        skill_has_complete = False
        skill_has_incomplete = False
        for pack in packs:
            bucket = _execution_contract_bucket(pack)
            execution_contract_buckets[bucket] = execution_contract_buckets.get(bucket, 0) + 1
            if not pack.get("valid"):
                invalid_pack_count += 1
                skill_has_invalid = True
                reason = str(pack.get("error") or pack.get("error_type") or "invalid eval pack")
                invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
            elif _pack_split_complete(pack):
                skill_has_complete = True
            else:
                skill_has_incomplete = True
        if packs:
            if skill_has_invalid:
                split_completeness["invalid"] += 1
            elif skill_has_complete:
                split_completeness["complete"] += 1
            elif skill_has_incomplete:
                split_completeness["incomplete"] += 1
            if not skill_has_invalid and not entry.get("production_eligible") and entry.get("review_only"):
                only_review_only += 1
    return {
        "schema_version": "hermes-skillopt-readiness-matrix-v1",
        "total_skills": len(entries),
        "no_pack_count": no_pack,
        "only_review_only_count": only_review_only,
        "production_eligible_count": production_eligible,
        "invalid_pack_count": invalid_pack_count,
        "invalid_pack_reasons": invalid_reasons,
        "split_completeness": split_completeness,
        "execution_contract_buckets": execution_contract_buckets,
        "recommended_next_action_by_skill": per_skill_next_action,
        "safety_invariants": {
            "read_only_inventory": True,
            "auto_adopt": False,
            "live_skill_writes": False,
            "sample_static_session_mined_data_can_gate_production": False,
        },
    }


def eval_pack_inventory(*, hermes_home_path: str | None = None, skill: str | None = None) -> dict[str, Any]:
    """Read-only report of discovered skills, matching eval packs, and readiness."""

    home = core.hermes_home(hermes_home_path)
    skills = core.discover_skills(home)
    if skill:
        selected = [core.find_skill(home, skill)]
    else:
        selected = skills
    entries: list[dict[str, Any]] = []
    for sk in selected:
        candidates = _candidate_eval_paths(home, sk)
        existing = [p for p in candidates if p.is_file()]
        packs = [_pack_report(p) for p in existing]
        invalid_packs = [p for p in packs if not p.get("valid")]
        pack_missing_reasons = sorted({str(r) for p in packs for r in (p.get("missing_reasons") or []) if r})
        missing_reasons = pack_missing_reasons or ([] if packs else ["no matching eval pack found"])
        recommendation = _recommended_next_action(packs=packs, invalid_packs=invalid_packs)
        entries.append({
            "skill": sk.name,
            "skill_relpath": sk.relpath,
            "skill_sha256": sk.sha256,
            "skill_type": classify_skill_type(sk),
            "candidate_eval_paths": [str(p) for p in candidates],
            "eval_packs": packs,
            "has_eval_pack": bool(packs),
            "production_eligible": any(p.get("production_eligible") for p in packs),
            "review_only": bool(packs) and not any(p.get("production_eligible") for p in packs),
            "invalid_eval_pack_count": len(invalid_packs),
            "split_complete": any(_pack_split_complete(p) for p in packs),
            "execution_contract_buckets": sorted({_execution_contract_bucket(p) for p in packs}),
            "recommended_next_action": recommendation,
            "recommendation": recommendation,
            "missing_reasons": missing_reasons,
        })
    matrix = _readiness_matrix(entries)
    return {
        "success": True,
        "mode": "eval_pack_inventory_read_only",
        "hermes_home": str(home),
        "skill_count": len(entries),
        "skills": entries,
        "readiness_matrix": matrix,
        # Readable top-level aliases retained in addition to the structured matrix.
        "total_skills": matrix["total_skills"],
        "no_pack_count": matrix["no_pack_count"],
        "only_review_only_count": matrix["only_review_only_count"],
        "production_eligible_count": matrix["production_eligible_count"],
        "invalid_pack_count": matrix["invalid_pack_count"],
    }


def scaffold_eval_pack(*, skill: str, output: str | Path | None = None, hermes_home_path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Write a review-only eval-pack scaffold with complete train/val/test samples."""

    home = core.hermes_home(hermes_home_path)
    sk = core.find_skill(home, skill)
    out_raw = output or (home / "skillopt" / "evals" / f"{sk.path.parent.name}-scaffold.json")
    # Resolve before the shared guard so /var vs /private/var aliases on macOS do
    # not fail the guard's lexical+resolved HERMES_HOME checks.
    out = guard_safe_output_path(Path(out_raw).expanduser().resolve(strict=False), kind="eval pack scaffold", hermes_home=home, required_suffix=".json")
    if out.exists() and not overwrite:
        raise ValueError(f"eval pack scaffold output already exists: {out}")
    payload = {
        "schema_version": EVAL_PACK_SCHEMA_VERSION,
        "pack_id": f"{sk.path.parent.name}-review-scaffold",
        "version": "scaffold-review-only-v1",
        "sample_pack": True,
        "task_origin": "sample-eval-pack",
        "require_complete_splits": True,
        "production_policy": {
            "allow_production_adoption": False,
            "review_only": True,
            "refusal_reasons": ["scaffold is sample/review-only and contains no production evidence"],
        },
        "eval_execution_contract": {
            "classification": "static_review_only",
            "evidence": {},
        },
        "scaffold_notice": "Review-only starter pack. Replace sample prompts/expected terms with curated evidence before considering production gates.",
        "tasks": [
            {"id": "train-sample-1", "split": "train", "prompt": f"Sample training case for {sk.name}: describe expected safe behavior.", "expected_terms": ["sample"], "task_origin": "sample-eval-pack", "production_gate_eligible": False},
            {"id": "val-sample-1", "split": "validation", "prompt": f"Sample validation case for {sk.name}: verify behavior without live writes.", "expected_terms": ["sample"], "task_origin": "sample-eval-pack", "production_gate_eligible": False},
            {"id": "test-sample-1", "split": "test", "prompt": f"Sample held-out test case for {sk.name}: ensure rollback/blocker handling is stated.", "expected_terms": ["sample"], "task_origin": "sample-eval-pack", "production_gate_eligible": False},
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.scaffold.", suffix=".tmp.json", dir=out.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _, meta = load_eval_pack(tmp)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    report = _pack_report(out)
    return {"success": True, "mode": "eval_pack_scaffold_review_only", "output_path": str(out), "skill": sk.name, "review_only": True, "production_eligible": False, "eval_pack": payload, "report": report}


# Canonical curated/session-mined eval-pack factories.
def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    comparable = {k: v for k, v in payload.items() if k not in {"fingerprint", "fingerprint_sha256"}}
    return hashlib.sha256(json.dumps(comparable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _write_validated_eval_pack(payload: dict[str, Any], out: Path, *, overwrite: bool, tmp_tag: str, require_production_eligible: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    if out.exists() and not overwrite:
        raise ValueError(f"eval pack output already exists: {out}")
    payload = dict(payload)
    payload["fingerprint_sha256"] = _payload_fingerprint(payload)
    text = _canonical_json(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.{tmp_tag}.", suffix=".tmp.json", dir=out.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tasks, meta = load_eval_pack(tmp)
        if require_production_eligible:
            tmp_report = _pack_report(tmp)
            if not tmp_report.get("production_eligible"):
                raise ValueError("explicit production curated eval pack is not production eligible after validation: " + "; ".join(tmp_report.get("missing_reasons") or []))
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    report = _pack_report(out)
    return payload, {"report": report, "metadata": meta.as_dict(), "task_count": len(tasks)}


def create_curated_eval_pack(
    *,
    skill: str,
    tasks: list[dict[str, Any]],
    output: str | Path | None = None,
    hermes_home_path: str | None = None,
    pack_id: str | None = None,
    version: str = "curated-v1",
    production_policy: dict[str, Any] | None = None,
    eval_execution_contract: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a canonical curated eval pack, defaulting to review-only."""

    home = core.hermes_home(hermes_home_path)
    sk = core.find_skill(home, skill)
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("curated eval pack tasks must be a non-empty list")
    normalized_tasks: list[dict[str, Any]] = []
    for i, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            raise ValueError(f"curated eval pack task #{i} must be an object")
        rec = dict(task)
        rec.setdefault("id", f"curated-{i:04d}")
        rec.setdefault("task_origin", "curated")
        normalized_tasks.append(rec)

    policy = dict(production_policy or {})
    declared_allow = bool(policy.get("allow_production_adoption", False))
    if declared_allow:
        non_production = {"synthetic", "curated-fallback", "session-mined", "session_mined", "dream", "builtin-benchmark", "sample-eval-pack", "static-review-eval-pack", "static-keyword-scorecard", "keyword-scorecard"}
        origins = {str(t.get("task_origin") or "curated") for t in normalized_tasks}
        blocked = origins & non_production
        if blocked:
            raise ValueError("production curated eval pack cannot use review-only/non-production origins: " + ", ".join(sorted(blocked)))
    else:
        policy.setdefault("allow_production_adoption", False)
        policy.setdefault("review_only", True)
        reasons = list(policy.get("refusal_reasons") or [])
        reasons.append("curated factory default is review-only until explicit production_policy.allow_production_adoption=true")
        policy["refusal_reasons"] = sorted(set(str(r) for r in reasons if r))
        for rec in normalized_tasks:
            rec["production_gate_eligible"] = False

    contract = dict(eval_execution_contract or {})
    contract.setdefault("classification", "deterministic_replay_contract_compliant" if declared_allow else "deterministic_replay_report_only")
    payload = {
        "schema_version": EVAL_PACK_SCHEMA_VERSION,
        "pack_id": pack_id or f"{sk.path.parent.name}-curated",
        "version": version,
        "sample_pack": False,
        "task_origin": "curated",
        "require_complete_splits": True,
        "production_policy": policy,
        "eval_execution_contract": contract,
        "canonical_json": True,
        "factory": "hermes_skillopt.create_curated_eval_pack",
        "tasks": normalized_tasks,
    }
    out_raw = output or (home / "skillopt" / "evals" / f"{sk.path.parent.name}-curated.json")
    out = guard_safe_output_path(Path(out_raw).expanduser().resolve(strict=False), kind="curated eval pack", hermes_home=home, required_suffix=".json")
    payload, validation = _write_validated_eval_pack(payload, out, overwrite=overwrite, tmp_tag="curated", require_production_eligible=declared_allow)
    report = validation["report"]
    return {"success": True, "mode": "curated_eval_pack_factory", "output_path": str(out), "skill": sk.name, "review_only": not bool(report.get("production_eligible")), "production_eligible": bool(report.get("production_eligible")), "eval_pack": payload, **validation}


def _load_session_fixture(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path).expanduser().resolve(strict=True)
    if p.suffix.lower() == ".jsonl":
        raw = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        raw = data.get("sessions") or data.get("snippets") or data if isinstance(data, dict) else data
    else:
        raw = [{"id": p.stem, "source": str(p.name), "text": p.read_text(encoding="utf-8")}]
    if not isinstance(raw, list):
        raise ValueError("session fixture must be a list or contain sessions/snippets list")
    snippets: list[dict[str, Any]] = []
    for i, item in enumerate(raw, 1):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("content") or item.get("message") or item.get("prompt") or "")
            source = str(item.get("source") or p.name)
            sid = str(item.get("id") or f"fixture-{i}")
        else:
            text = str(item)
            source = p.name
            sid = f"fixture-{i}"
        if text.strip():
            snippets.append({"id": sid, "source": source, "text": core.redact_secrets(text), "meta": {"fixture_path": str(p), "redacted": True}})
    return snippets


def mine_session_eval_pack(
    *,
    skill: str,
    output: str | Path | None = None,
    hermes_home_path: str | None = None,
    query: str | None = None,
    lookback_days: int = 14,
    limit: int = 50,
    session_fixture: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Harvest/read session-like text into a draft review-only eval pack."""

    home = core.hermes_home(hermes_home_path)
    sk = core.find_skill(home, skill)
    snippets = _load_session_fixture(session_fixture) if session_fixture else core.harvest_sessions(home, sk, query=query, lookback_days=lookback_days, limit=limit)
    redacted_snippets: list[dict[str, Any]] = []
    for s in snippets[: max(1, int(limit))]:
        meta_raw = s.get("meta")
        meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
        redacted_snippets.append({**s, "text": core.redact_secrets(str(s.get("text") or ""))[:4000], "meta": {**meta, "redacted": True}})
    snippets = redacted_snippets
    items = core.mine_items(snippets, sk, query=query)
    splits = core.split_items(items, test=True)
    tasks: list[dict[str, Any]] = []
    fallback_item = items[0] if items else {"id": "empty", "user_goal": query or sk.name, "evidence": "No matching sessions found."}
    for split in ("train", "val", "test"):
        rows = splits.get(split) or [fallback_item]
        for idx, item in enumerate(rows, 1):
            evidence = core.redact_secrets(str(item.get("evidence") or ""))[:1200]
            prompt = f"Review session-mined evidence for {sk.name} ({split} case {idx}): {str(item.get('user_goal') or query or sk.name)}"
            tasks.append({
                "id": f"session-mined-{split}-{idx:03d}",
                "split": split,
                "prompt": prompt,
                "expected_terms": ["verify", "tool", "evidence"],
                "failure_terms": ["auto adopt without review", "skip validation", "fabricate"],
                "task_origin": "session-mined",
                "production_gate_eligible": False,
                "provenance": {"source_id": item.get("source_id"), "redacted": True, "session_fixture": bool(session_fixture)},
                "non_production_reasons": ["session-mined tasks are draft/review-only", "redacted transcript evidence is not curated production ground truth"],
                "fixtures": {"redacted_evidence_excerpt": evidence},
            })
    payload = {
        "schema_version": EVAL_PACK_SCHEMA_VERSION,
        "pack_id": f"{sk.path.parent.name}-session-mined-draft",
        "version": "session-mined-draft-v1",
        "sample_pack": False,
        "task_origin": "session-mined",
        "require_complete_splits": True,
        "production_policy": {"allow_production_adoption": False, "review_only": True, "refusal_reasons": ["session-mined draft packs are review-only and cannot gate production", "requires human curation into a separate curated production pack"]},
        "eval_execution_contract": {"classification": "deterministic_replay_report_only", "evidence": {"redacted": True}},
        "provenance": {"mode": "read_only_session_to_eval_mining", "snippet_count": len(snippets), "redaction": "core.redact_secrets applied", "session_fixture": str(session_fixture) if session_fixture else None},
        "non_production_reasons": ["origin=session-mined", "allow_production_adoption=false", "production_gate_eligible=false for every task"],
        "tasks": tasks,
    }
    out_raw = output or (home / "skillopt" / "evals" / f"{sk.path.parent.name}-session-mined-draft.json")
    out = guard_safe_output_path(Path(out_raw).expanduser().resolve(strict=False), kind="session-mined eval pack", hermes_home=home, required_suffix=".json")
    payload, validation = _write_validated_eval_pack(payload, out, overwrite=overwrite, tmp_tag="session-mined")
    return {"success": True, "mode": "session_to_eval_mining_review_only", "output_path": str(out), "skill": sk.name, "review_only": True, "production_eligible": False, "snippet_count": len(snippets), "item_count": len(items), "eval_pack": payload, **validation}
