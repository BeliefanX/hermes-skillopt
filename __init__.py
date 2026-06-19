from __future__ import annotations

import json
from typing import Any, Callable

from hermes_skillopt import core
from hermes_skillopt import multi_agent

try:
    from tools.registry import tool_error, tool_result
except Exception:  # local CLI/tests outside Hermes core
    def tool_result(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    def tool_error(message: str, **extra: Any) -> str:
        return json.dumps({"success": False, "error": message, **extra}, ensure_ascii=False, indent=2)


def _schema(description: str, props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"description": description, "parameters": {"type": "object", "properties": props or {}, "required": required or []}}

COMMON_HOME = {"hermes_home": {"type": "string", "description": "Optional HERMES_HOME override; defaults to current profile home (~/.hermes)."}}
ADOPT_PROPS = {"run_id": {"type": "string"}, "force": {"type": "boolean", "default": False}, "confirmation": {"type": "string", "description": "Typed confirmation; must exactly equal ADOPT <run_id> unless non_interactive_override=true."}, "non_interactive_override": {"type": "boolean", "default": False, "description": "Deliberate CI/test override; core gate checks still apply."}}
ROLLBACK_PROPS = {"run_id": {"type": "string"}, "force": {"type": "boolean", "default": False}, "confirmation": {"type": "string", "description": "Typed confirmation; must exactly equal ROLLBACK <run_id> unless non_interactive_override=true."}, "non_interactive_override": {"type": "boolean", "default": False, "description": "Deliberate CI/test override; core guard checks still apply."}}
FULL_PROPS = {
    **COMMON_HOME,
    "skill": {"type": "string"},
    "query": {"type": "string"},
    "eval_file": {"type": "string", "description": "Optional curated replay/eval JSONL/JSON under HERMES_HOME. Defaults to skillopt/evals/<skill>.jsonl or skill-dir/evals/*.jsonl."},
    "lookback_days": {"type": "integer", "default": 14},
    "limit": {"type": "integer", "default": 50},
    "iterations": {"type": "integer", "default": 1},
    "edit_budget": {"type": "integer", "default": 3},
    "candidate_count": {"type": "integer", "default": 1},
    "backend": {"type": "string", "enum": ["auto", "hermes", "mock"], "default": "auto", "description": "Back-compat alias for optimizer_backend."},
    "optimizer_backend": {"type": "string", "enum": ["auto", "hermes", "mock"], "default": "auto"},
    "allow_mock": {"type": "boolean", "default": False},
    "target_executor": {"type": "string", "enum": ["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"], "default": "auto"},
    "target_backend": {"type": "string", "enum": ["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"], "default": "auto"},
    "gate_mode": {"type": "string", "enum": ["soft", "hard", "mixed", "strict"], "default": "strict", "description": "Strict is the production-capable default; soft/mixed and mock provenance are review-only; judge explanation is advisory only."},
    "resume_run_id": {"type": "string", "description": "Opt-in reuse of a completed checkpointed full-run when input/config/provenance fingerprints match."},
    "force": {"type": "boolean", "default": False},
}
OPTIMIZE_PROPS = {**FULL_PROPS, "intent": {"type": "string", "enum": ["smoke", "review", "production"], "default": "review", "description": "smoke/review are staged review-only presets; production fails fast unless strict, no mock, and explicit eval_file are present."}}

SCHEMAS = {
    "hermes_skillopt_status": _schema("Show SkillOpt plugin status, discovered skill count, recent staged runs, lineage summaries, and stale/incomplete checkpoint rows.", COMMON_HOME),
    "hermes_skillopt_scout": _schema("Read-only notification-ready SkillOpt scout summary: skills/eval-pack inventory, recent staged runs, artifact hygiene, advisory curator metadata, cron-safe recommendation, and exact safe next commands. Never runs full_run/optimize/adopt/rollback/fetch; optional output is guarded report-only JSON.", {**COMMON_HOME, "skill": {"type": "string"}, "limit": {"type": "integer", "default": 5}, "stale_after_hours": {"type": "number", "default": 24.0}, "output": {"type": "string", "description": "Optional guarded .json report path; default returns JSON only and writes nothing."}}),
    "hermes_skillopt_doctor": _schema("Read-only SkillOpt readiness/guided UX report: profile paths, skill/eval readiness, recent runs, upstream/parity posture, production checklist, and next actions. Never runs full_run/adopt/rollback/fetch.", {**COMMON_HOME, "skill": {"type": "string"}}),
    "hermes_skillopt_dry_run": _schema("Create a legacy safe staged SkillOpt proposal/diff. Does not modify the target skill.", {**COMMON_HOME, "skill": {"type": "string"}, "goal": {"type": "string"}, "session_search": {"type": "string"}, "use_llm": {"type": "boolean", "default": False}}),
    "hermes_skillopt_run": _schema("Run Hermes-native SkillOpt core adapter when mode='full' (default): trainable SKILL.md state, frozen target executor, optimizer bounded edits, held-out validation gate; stages best proposal for explicit review/adopt only.", {**FULL_PROPS, "mode": {"type": "string", "enum": ["full", "legacy"], "default": "full"}, "goal": {"type": "string"}, "session_search": {"type": "string"}, "use_llm": {"type": "boolean", "default": False}}),
    "hermes_skillopt_full_run": _schema("Run full core pipeline: load skill state, build eval tasks, frozen target eval, optimizer reflect/edit, strict validation/test gates, hard production-failure blocking, and staged artifacts only.", FULL_PROPS),
    "hermes_skillopt_optimize": _schema("Guided staged-only optimization alias with intent presets. smoke/review clearly remain review-only; production requires strict/no-mock/explicit eval_file and still never auto-adopts.", OPTIMIZE_PROPS),
    "hermes_skillopt_batch_preflight": _schema("Read-only deterministic validation of a staged-only SkillOpt batch plan; enforces budget and rejects adopt/writeback fields.", {**COMMON_HOME, "plan": {"type": "object", "description": "Inline batch plan object. Use CLI for JSON path input."}}, ["plan"]),
    "hermes_skillopt_batch_run": _schema("Run a preflighted SkillOpt batch under staging only; calls full_run with auto_adopt=false and force=false for every job.", {**COMMON_HOME, "plan": {"type": "object", "description": "Inline batch plan object. Use CLI for JSON path input."}}, ["plan"]),
    "hermes_skillopt_fleet_report": _schema("Read-only fleet report over recent single and batch parent/child runs with readiness, advisory skill type, evidence-contract status, grouped eligibility/resume/rollback/lineage, and tamper warnings.", {**COMMON_HOME, "limit": {"type": "integer", "default": 50}, "skill": {"type": "string"}}),
    "hermes_skillopt_fleet_resume_plan": _schema("Read-only fleet resume plan: completed exact-fingerprint reuse only; incomplete partial continuation refused with cleanup/retry guidance.", {**COMMON_HOME, "limit": {"type": "integer", "default": 50}, "skill": {"type": "string"}}),
    "hermes_skillopt_fleet_rollback_plan": _schema("Read-only fleet rollback plan listing safely inferable adopted backups, backup/current-sha guard status, and exact one-run rollback commands; no bulk rollback/writeback.", {**COMMON_HOME, "limit": {"type": "integer", "default": 50}, "skill": {"type": "string"}}),
    "hermes_skillopt_artifact_hygiene_report": _schema("Read-only staging artifact hygiene planner/classifier for complete_verified, complete_tampered, checkpoint_only_recent, stale_incomplete, orphaned batch/child mismatch, and missing manifest/hash mismatch. Never deletes.", {**COMMON_HOME, "limit": {"type": "integer", "default": 200}, "stale_after_hours": {"type": "number", "default": 24.0}}),
    "hermes_skillopt_eval_pack_inventory": _schema("Read-only inventory of discovered skills and matching eval packs, including split completeness and production/review-only reasons.", {**COMMON_HOME, "skill": {"type": "string"}}),
    "hermes_skillopt_eval_pack_doctor": _schema("Focused read-only eval-pack diagnostics and safe next actions; never writes, runs evals, adopts, or fetches.", {**COMMON_HOME, "skill": {"type": "string"}}),
    "hermes_skillopt_eval_pack_autopilot": _schema("Eval-pack autopilot. Defaults to plan/read-only and cron-safe; write_draft=true explicitly writes only a guarded review-only draft pack.", {**COMMON_HOME, "skill": {"type": "string"}, "output": {"type": "string"}, "write_draft": {"type": "boolean", "default": False}, "overwrite": {"type": "boolean", "default": False}}, ["skill"]),
    "hermes_skillopt_eval_pack_scaffold": _schema("Generate a review-only eval-pack scaffold with complete train/validation/test samples; never production evidence.", {**COMMON_HOME, "skill": {"type": "string"}, "output": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, ["skill"]),
    "hermes_skillopt_eval_pack_curate": _schema("Create a canonical curated eval pack from inline tasks. Defaults review-only; production eligibility requires explicit production_policy and compliant execution contract.", {**COMMON_HOME, "skill": {"type": "string"}, "tasks": {"type": "array", "items": {"type": "object"}}, "output": {"type": "string"}, "pack_id": {"type": "string"}, "version": {"type": "string", "default": "curated-v1"}, "production_policy": {"type": "object"}, "eval_execution_contract": {"type": "object"}, "overwrite": {"type": "boolean", "default": False}}, ["skill", "tasks"]),
    "hermes_skillopt_eval_pack_mine_sessions": _schema("Read-only/draft mining of redacted sessions or session-like fixture file into a review-only eval pack; never production-eligible.", {**COMMON_HOME, "skill": {"type": "string"}, "output": {"type": "string"}, "query": {"type": "string"}, "lookback_days": {"type": "integer", "default": 14}, "limit": {"type": "integer", "default": 50}, "session_fixture": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, ["skill"]),
    "hermes_skillopt_eval_pack_ingest_correction": _schema("Ingest a user correction into deterministic review-only regression seed tasks; redacted and never production-eligible.", {**COMMON_HOME, "skill": {"type": "string"}, "correction": {"type": "string"}, "output": {"type": "string"}, "expected_terms": {"type": "array", "items": {"type": "string"}}, "overwrite": {"type": "boolean", "default": False}}, ["skill", "correction"]),
    "hermes_skillopt_eval_pack_ingest_context": _schema("Ingest skill-creation context into deterministic review-only eval seed tasks; redacted and never production-eligible.", {**COMMON_HOME, "skill": {"type": "string"}, "context": {"type": "string"}, "output": {"type": "string"}, "expected_terms": {"type": "array", "items": {"type": "string"}}, "overwrite": {"type": "boolean", "default": False}}, ["skill", "context"]),
    "hermes_skillopt_eval_pack_negative_boundary": _schema("Generate deterministic negative/boundary review-only cases; no model calls, command execution, live skill writes, or production eligibility.", {**COMMON_HOME, "skill": {"type": "string"}, "output": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, ["skill"]),
    "hermes_skillopt_eval_pack_promote": _schema("Promote a draft to a curated review pack by default. Production promotion requires explicit production_policy and eval_execution_contract and never auto-adopts.", {**COMMON_HOME, "skill": {"type": "string"}, "input_path": {"type": "string"}, "output": {"type": "string"}, "production": {"type": "boolean", "default": False}, "production_policy": {"type": "object"}, "eval_execution_contract": {"type": "object"}, "overwrite": {"type": "boolean", "default": False}}, ["skill", "input_path"]),
    "hermes_skillopt_resume_inspect": _schema("Read-only step-level resume inspection: verifies checkpoint/stage fingerprints and artifact hashes; refuses unsafe partial continuation.", {**COMMON_HOME, "run_id": {"type": "string"}}, ["run_id"]),
    "hermes_skillopt_review": _schema("Review a staged SkillOpt run with gate score, accepted/rejected status, paths, artifact refs, and optional diff/report preview. run_id may be omitted or 'latest'; summary returns decision-first slim output; digest returns Telegram-friendly path/hash refs only.", {**COMMON_HOME, "run_id": {"type": "string"}, "latest": {"type": "boolean", "default": False}, "summary": {"type": "boolean", "default": False}, "digest": {"type": "boolean", "default": False, "description": "Return Telegram-friendly slim digest with separated readiness/adoptability fields and artifact refs, not raw report/diff."}, "include_diff_chars": {"type": "integer", "default": 4000}, "slim": {"type": "boolean", "default": False, "description": "When true, omit large diff/report previews and return path/hash artifact references only."}}),
    "hermes_skillopt_adopt": _schema("Adopt a staged proposal into exactly one target SKILL.md in the active Hermes profile only, after sha/path/gate guard and backup.", ADOPT_PROPS, ["run_id"]),
    "hermes_skillopt_rollback": _schema("Rollback an adopted run in the active Hermes profile only, using a validated backup manifest and backup SKILL.md after current-sha guard unless force=true.", ROLLBACK_PROPS, ["run_id"]),
    "hermes_skillopt_upstream_status": _schema("Show Microsoft SkillOpt upstream clone and pinned lock status for the canonical HERMES_HOME clone.", COMMON_HOME),
    "hermes_skillopt_compare_upstream_pin": _schema("Read-only pinned-upstream comparison; reports local clone/lock divergence and never fetches, merges, or writes plugin code.", COMMON_HOME),
    "hermes_skillopt_benchmark_parity_status": _schema("Read-only status surface labeling Hermes-native benchmark mode versus upstream parity; no rollout, adopt, or writeback.", COMMON_HOME),
    "hermes_skillopt_upstream_update": _schema("Fetch/update the canonical pinned Microsoft SkillOpt upstream clone for the active profile and write lock; ignores arbitrary HERMES_HOME overrides and never merges into plugin code.", {"fetch_only": {"type": "boolean", "default": False}}),
    "hermes_skillopt_import_upstream_benchmark": _schema("Safely convert an upstream-style embedded JSON benchmark manifest into a Hermes eval pack; rejects executable/remote/network fields and never imports upstream code.", {**COMMON_HOME, "manifest": {"type": "string"}, "output": {"type": "string"}, "pack_id": {"type": "string"}, "version": {"type": "string"}, "curated": {"type": "boolean", "default": False}, "from_pinned_manifest": {"type": "boolean", "default": False, "description": "Require manifest to be under the canonical pinned upstream clone and label output as pinned_manifest_replay evidence."}, "adapter_level": {"type": "string", "enum": ["json_import_only", "pinned_manifest_replay"], "default": "json_import_only", "description": "Safe adapter evidence level. Full upstream parity execution is intentionally unsupported."}}, ["manifest"]),
    "hermes_skillopt_transfer_eval": _schema("Read-only/report-only staged skill transfer evaluation across target/profile configs with readiness, advisory skill type, and evidence-contract summaries; never writes live skills.", {**COMMON_HOME, "run_id": {"type": "string"}, "skill_file": {"type": "string"}, "eval_file": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string", "enum": ["scorecard", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1"]}}, "profile_homes": {"type": "array", "items": {"type": "string"}}, "output": {"type": "string"}, "allow_live_skill_file": {"type": "boolean", "default": False}}),
    "hermes_skillopt_conformance": _schema("Run local conformance and return JSON; writes a report only when output is supplied. Default mode=quick is a smoke suite, not full repo health; use mode=full for all pytest tests.", {"output": {"type": "string"}, "pytest_args": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "integer", "default": 180}, "mode": {"type": "string", "enum": ["quick", "full"], "default": "quick"}}),
    "hermes_skillopt_handoff_optimize": _schema("Build and score a staged multi-agent delegate_task handoff package. No LLM/network calls and no global prompt auto-adopt.", {"requirements": {"type": "string"}, "worker": {"type": "string"}, "context_budget_chars": {"type": "integer", "default": 6000}}, ["requirements"]),
}

for _tool_name, _metadata in core.TOOL_SAFETY_METADATA.items():
    if _tool_name in SCHEMAS:
        SCHEMAS[_tool_name]["x-hermes-skillopt-safety"] = dict(_metadata)
        SCHEMAS[_tool_name]["safety_group"] = _metadata.get("safety_group")
        SCHEMAS[_tool_name]["risk_level"] = _metadata.get("risk_level")


TOOL_SAFETY_CATALOG = core.tool_safety_catalog()


def _ok(fn: Callable[..., dict[str, Any]], args: dict[str, Any]) -> str:
    try:
        return tool_result(fn(**args))
    except Exception as exc:
        return tool_error(f"hermes-skillopt failed: {type(exc).__name__}: {exc}")


def _full_args(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    return {
        "skill": args.get("skill"),
        "query": args.get("query") or args.get("session_search") or args.get("goal"),
        "eval_file": args.get("eval_file"),
        "lookback_days": int(args.get("lookback_days") or 14),
        "limit": int(args.get("limit") or 50),
        "iterations": int(args.get("iterations") or 1),
        "edit_budget": int(args.get("edit_budget") or 3),
        "candidate_count": int(args.get("candidate_count") or 1),
        "backend": args.get("backend") or "auto",
        "optimizer_backend": args.get("optimizer_backend"),
        "allow_mock": bool(args.get("allow_mock", False)),
        "target_executor": args.get("target_executor") or "auto",
        "target_backend": args.get("target_backend"),
        "gate_mode": args.get("gate_mode") or "strict",
        "resume_run_id": args.get("resume_run_id"),
        "force": bool(args.get("force", False)),
        "hermes_home_path": args.get("hermes_home"),
        "ctx": ctx,
    }


def _handle_status(args: dict, **kw) -> str:
    return _ok(lambda hermes_home=None: core.status(hermes_home), {"hermes_home": args.get("hermes_home")})


def _handle_scout(args: dict, **kw) -> str:
    return _ok(core.scout, {"hermes_home_path": args.get("hermes_home"), "skill": args.get("skill"), "limit": int(args.get("limit") or 5), "stale_after_hours": float(args.get("stale_after_hours") or 24.0), "output_path": args.get("output")})


def _handle_doctor(args: dict, **kw) -> str:
    return _ok(core.doctor, {"hermes_home_path": args.get("hermes_home"), "skill": args.get("skill")})


def _handle_dry_run(args: dict, **kw) -> str:
    ctx = kw.get("ctx") or kw.get("context")
    return _ok(lambda **a: core.dry_run(ctx=ctx, **a), {"skill": args.get("skill"), "goal": args.get("goal"), "session_search": args.get("session_search"), "hermes_home_path": args.get("hermes_home"), "use_llm": bool(args.get("use_llm", False))})


def _handle_run(args: dict, **kw) -> str:
    ctx = kw.get("ctx") or kw.get("context")
    if args.get("auto_adopt") and args.get("force"):
        return tool_error("hermes-skillopt failed: auto_adopt cannot be combined with force")
    if args.get("auto_adopt"):
        return tool_error("hermes-skillopt failed: auto_adopt is disabled; run full-run, review, then adopt explicitly")
    if (args.get("mode") or "full") == "legacy":
        return _handle_dry_run(args, **kw)
    return _ok(core.full_run, _full_args(args, ctx))


def _handle_optimize(args: dict, **kw) -> str:
    ctx = kw.get("ctx") or kw.get("context")
    full_args = _full_args(args, ctx)
    full_args.pop("force", None)
    if "allow_mock" not in args:
        full_args.pop("allow_mock", None)
    if "gate_mode" not in args:
        full_args.pop("gate_mode", None)
    return _ok(core.guided_optimize, {"intent": args.get("intent") or "review", **full_args})


def _handle_review(args: dict, **kw) -> str:
    rid = "latest" if bool(args.get("latest", False)) else (args.get("run_id") or "latest")
    if bool(args.get("digest", False)):
        return _ok(core.review_digest, {"run_id": rid, "hermes_home_path": args.get("hermes_home")})
    if bool(args.get("summary", False)):
        return _ok(core.review_decision_summary, {"run_id": rid, "hermes_home_path": args.get("hermes_home")})
    if rid == "latest":
        return _ok(core.review_latest, {"hermes_home_path": args.get("hermes_home"), "include_diff_chars": int(args.get("include_diff_chars") or 4000), "slim": bool(args.get("slim", False))})
    return _ok(core.review, {"run_id": rid, "hermes_home_path": args.get("hermes_home"), "include_diff_chars": int(args.get("include_diff_chars") or 4000), "slim": bool(args.get("slim", False))})


def _handle_batch_preflight(args: dict, **kw) -> str:
    from hermes_skillopt.batch import batch_preflight
    return _ok(batch_preflight, {"plan": args.get("plan"), "hermes_home_path": args.get("hermes_home")})


def _handle_batch_run(args: dict, **kw) -> str:
    from hermes_skillopt.batch import run_batch
    return _ok(run_batch, {"plan": args.get("plan"), "hermes_home_path": args.get("hermes_home"), "ctx": kw.get("ctx") or kw.get("context")})


def _handle_fleet_report(args: dict, **kw) -> str:
    return _ok(core.fleet_report, {"hermes_home_path": args.get("hermes_home"), "limit": int(args.get("limit") or 50), "skill": args.get("skill")})


def _handle_fleet_resume_plan(args: dict, **kw) -> str:
    return _ok(core.fleet_resume_plan, {"hermes_home_path": args.get("hermes_home"), "limit": int(args.get("limit") or 50), "skill": args.get("skill")})


def _handle_fleet_rollback_plan(args: dict, **kw) -> str:
    return _ok(core.fleet_rollback_plan, {"hermes_home_path": args.get("hermes_home"), "limit": int(args.get("limit") or 50), "skill": args.get("skill")})


def _handle_artifact_hygiene_report(args: dict, **kw) -> str:
    return _ok(core.artifact_hygiene_report, {"hermes_home_path": args.get("hermes_home"), "limit": int(args.get("limit") or 200), "stale_after_hours": float(args.get("stale_after_hours") or 24.0)})


def _handle_eval_pack_inventory(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import eval_pack_inventory
    return _ok(eval_pack_inventory, {"hermes_home_path": args.get("hermes_home"), "skill": args.get("skill")})


def _handle_eval_pack_doctor(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import eval_pack_doctor
    return _ok(eval_pack_doctor, {"hermes_home_path": args.get("hermes_home"), "skill": args.get("skill")})


def _handle_eval_pack_autopilot(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import eval_pack_autopilot
    return _ok(eval_pack_autopilot, {"skill": args.get("skill"), "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "write_draft": bool(args.get("write_draft", False)), "overwrite": bool(args.get("overwrite", False))})


def _handle_eval_pack_scaffold(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import scaffold_eval_pack
    return _ok(scaffold_eval_pack, {"skill": args.get("skill"), "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "overwrite": bool(args.get("overwrite", False))})


def _handle_eval_pack_curate(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import create_curated_eval_pack
    return _ok(create_curated_eval_pack, {"skill": args.get("skill"), "tasks": args.get("tasks"), "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "pack_id": args.get("pack_id"), "version": args.get("version") or "curated-v1", "production_policy": args.get("production_policy"), "eval_execution_contract": args.get("eval_execution_contract"), "overwrite": bool(args.get("overwrite", False))})


def _handle_eval_pack_mine_sessions(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import mine_session_eval_pack
    return _ok(mine_session_eval_pack, {"skill": args.get("skill"), "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "query": args.get("query"), "lookback_days": int(args.get("lookback_days") or 14), "limit": int(args.get("limit") or 50), "session_fixture": args.get("session_fixture"), "overwrite": bool(args.get("overwrite", False))})


def _handle_eval_pack_ingest_correction(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import ingest_user_correction_eval_seed
    return _ok(ingest_user_correction_eval_seed, {"skill": args.get("skill"), "correction": args.get("correction") or "", "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "expected_terms": args.get("expected_terms"), "overwrite": bool(args.get("overwrite", False))})


def _handle_eval_pack_ingest_context(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import ingest_skill_context_eval_seed
    return _ok(ingest_skill_context_eval_seed, {"skill": args.get("skill"), "context": args.get("context") or "", "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "expected_terms": args.get("expected_terms"), "overwrite": bool(args.get("overwrite", False))})


def _handle_eval_pack_negative_boundary(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import generate_negative_boundary_eval_pack
    return _ok(generate_negative_boundary_eval_pack, {"skill": args.get("skill"), "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "overwrite": bool(args.get("overwrite", False))})


def _handle_eval_pack_promote(args: dict, **kw) -> str:
    from hermes_skillopt.eval_packs import promote_eval_pack
    return _ok(promote_eval_pack, {"skill": args.get("skill"), "input_path": args.get("input_path"), "output": args.get("output"), "hermes_home_path": args.get("hermes_home"), "production_policy": args.get("production_policy"), "eval_execution_contract": args.get("eval_execution_contract"), "production": bool(args.get("production", False)), "overwrite": bool(args.get("overwrite", False))})


def _handle_resume_inspect(args: dict, **kw) -> str:
    return _ok(core.inspect_resume_run, {"run_id": args.get("run_id"), "hermes_home_path": args.get("hermes_home")})


def _handle_adopt(args: dict, **kw) -> str:
    rid = (args.get("run_id") or "").strip()
    expected = f"ADOPT {rid}"
    if not bool(args.get("non_interactive_override", False)) and (args.get("confirmation") or "").strip() != expected:
        return tool_error(f"hermes-skillopt failed: type {expected!r} exactly to confirm")
    return _ok(core.adopt, {"run_id": rid, "hermes_home_path": None, "force": bool(args.get("force", False))})


def _handle_rollback(args: dict, **kw) -> str:
    rid = (args.get("run_id") or "").strip()
    expected = f"ROLLBACK {rid}"
    if not bool(args.get("non_interactive_override", False)) and (args.get("confirmation") or "").strip() != expected:
        return tool_error(f"hermes-skillopt failed: type {expected!r} exactly to confirm")
    return _ok(core.rollback, {"run_id": rid, "hermes_home_path": None, "force": bool(args.get("force", False))})


def _handle_upstream_status(args: dict, **kw) -> str:
    return _ok(core.upstream_status, {"hermes_home_path": args.get("hermes_home")})


def _handle_compare_upstream_pin(args: dict, **kw) -> str:
    return _ok(core.compare_upstream_pin, {"hermes_home_path": args.get("hermes_home")})


def _handle_benchmark_parity_status(args: dict, **kw) -> str:
    return _ok(core.benchmark_parity_status, {"hermes_home_path": args.get("hermes_home")})


def _handle_upstream_update(args: dict, **kw) -> str:
    return _ok(core.upstream_update, {"hermes_home_path": None, "repo_path": None, "fetch_only": bool(args.get("fetch_only", False))})


def _handle_import_upstream_benchmark(args: dict, **kw) -> str:
    from hermes_skillopt.benchmark_bridge import import_pinned_upstream_manifest, import_upstream_manifest
    adapter_level = args.get("adapter_level") or "json_import_only"
    common = {"output_path": args.get("output"), "pack_id": args.get("pack_id"), "version": args.get("version"), "sample_pack": not bool(args.get("curated", False)), "hermes_home": args.get("hermes_home")}
    if bool(args.get("from_pinned_manifest", False)) or adapter_level == "pinned_manifest_replay":
        return _ok(import_pinned_upstream_manifest, {"manifest_path": args.get("manifest"), **common})
    return _ok(import_upstream_manifest, {"input_path": args.get("manifest"), **common, "adapter_level": adapter_level})


def _handle_transfer_eval(args: dict, **kw) -> str:
    from hermes_skillopt.transfer import transfer_eval
    return _ok(transfer_eval, {"hermes_home_path": args.get("hermes_home"), "run_id": args.get("run_id"), "skill_file": args.get("skill_file"), "eval_file": args.get("eval_file"), "targets": args.get("targets"), "profile_homes": args.get("profile_homes"), "output_path": args.get("output"), "staged_only": not bool(args.get("allow_live_skill_file", False))})


def _handle_conformance(args: dict, **kw) -> str:
    from hermes_skillopt.conformance import run_conformance
    return _ok(run_conformance, {"output_path": args.get("output"), "pytest_args": args.get("pytest_args"), "timeout": int(args.get("timeout") or 180), "mode": args.get("mode") or "quick"})


def _handle_handoff_optimize(args: dict, **kw) -> str:
    return _ok(multi_agent.optimize_delegate_handoff, {"requirements": args.get("requirements") or "", "worker": args.get("worker"), "context_budget_chars": int(args.get("context_budget_chars") or 6000)})

_TOOLS = (
    ("hermes_skillopt_status", SCHEMAS["hermes_skillopt_status"], _handle_status, "🧰"),
    ("hermes_skillopt_scout", SCHEMAS["hermes_skillopt_scout"], _handle_scout, "🔭"),
    ("hermes_skillopt_doctor", SCHEMAS["hermes_skillopt_doctor"], _handle_doctor, "🩺"),
    ("hermes_skillopt_dry_run", SCHEMAS["hermes_skillopt_dry_run"], _handle_dry_run, "🧪"),
    ("hermes_skillopt_run", SCHEMAS["hermes_skillopt_run"], _handle_run, "🧠"),
    ("hermes_skillopt_full_run", SCHEMAS["hermes_skillopt_full_run"], _handle_run, "🧠"),
    ("hermes_skillopt_optimize", SCHEMAS["hermes_skillopt_optimize"], _handle_optimize, "🎯"),
    ("hermes_skillopt_batch_preflight", SCHEMAS["hermes_skillopt_batch_preflight"], _handle_batch_preflight, "🧾"),
    ("hermes_skillopt_batch_run", SCHEMAS["hermes_skillopt_batch_run"], _handle_batch_run, "🧺"),
    ("hermes_skillopt_fleet_report", SCHEMAS["hermes_skillopt_fleet_report"], _handle_fleet_report, "🛰️"),
    ("hermes_skillopt_fleet_resume_plan", SCHEMAS["hermes_skillopt_fleet_resume_plan"], _handle_fleet_resume_plan, "🧭"),
    ("hermes_skillopt_fleet_rollback_plan", SCHEMAS["hermes_skillopt_fleet_rollback_plan"], _handle_fleet_rollback_plan, "↩️"),
    ("hermes_skillopt_artifact_hygiene_report", SCHEMAS["hermes_skillopt_artifact_hygiene_report"], _handle_artifact_hygiene_report, "🧹"),
    ("hermes_skillopt_eval_pack_inventory", SCHEMAS["hermes_skillopt_eval_pack_inventory"], _handle_eval_pack_inventory, "📦"),
    ("hermes_skillopt_eval_pack_doctor", SCHEMAS["hermes_skillopt_eval_pack_doctor"], _handle_eval_pack_doctor, "🩺"),
    ("hermes_skillopt_eval_pack_autopilot", SCHEMAS["hermes_skillopt_eval_pack_autopilot"], _handle_eval_pack_autopilot, "🛫"),
    ("hermes_skillopt_eval_pack_scaffold", SCHEMAS["hermes_skillopt_eval_pack_scaffold"], _handle_eval_pack_scaffold, "🧱"),
    ("hermes_skillopt_eval_pack_curate", SCHEMAS["hermes_skillopt_eval_pack_curate"], _handle_eval_pack_curate, "📦"),
    ("hermes_skillopt_eval_pack_mine_sessions", SCHEMAS["hermes_skillopt_eval_pack_mine_sessions"], _handle_eval_pack_mine_sessions, "⛏️"),
    ("hermes_skillopt_eval_pack_ingest_correction", SCHEMAS["hermes_skillopt_eval_pack_ingest_correction"], _handle_eval_pack_ingest_correction, "🌱"),
    ("hermes_skillopt_eval_pack_ingest_context", SCHEMAS["hermes_skillopt_eval_pack_ingest_context"], _handle_eval_pack_ingest_context, "🌱"),
    ("hermes_skillopt_eval_pack_negative_boundary", SCHEMAS["hermes_skillopt_eval_pack_negative_boundary"], _handle_eval_pack_negative_boundary, "🚧"),
    ("hermes_skillopt_eval_pack_promote", SCHEMAS["hermes_skillopt_eval_pack_promote"], _handle_eval_pack_promote, "⬆️"),
    ("hermes_skillopt_resume_inspect", SCHEMAS["hermes_skillopt_resume_inspect"], _handle_resume_inspect, "🔎"),
    ("hermes_skillopt_review", SCHEMAS["hermes_skillopt_review"], _handle_review, "🔎"),
    ("hermes_skillopt_adopt", SCHEMAS["hermes_skillopt_adopt"], _handle_adopt, "✅"),
    ("hermes_skillopt_rollback", SCHEMAS["hermes_skillopt_rollback"], _handle_rollback, "↩️"),
    ("hermes_skillopt_upstream_status", SCHEMAS["hermes_skillopt_upstream_status"], _handle_upstream_status, "🌊"),
    ("hermes_skillopt_compare_upstream_pin", SCHEMAS["hermes_skillopt_compare_upstream_pin"], _handle_compare_upstream_pin, "📌"),
    ("hermes_skillopt_benchmark_parity_status", SCHEMAS["hermes_skillopt_benchmark_parity_status"], _handle_benchmark_parity_status, "📊"),
    ("hermes_skillopt_upstream_update", SCHEMAS["hermes_skillopt_upstream_update"], _handle_upstream_update, "⬆️"),
    ("hermes_skillopt_import_upstream_benchmark", SCHEMAS["hermes_skillopt_import_upstream_benchmark"], _handle_import_upstream_benchmark, "🌉"),
    ("hermes_skillopt_transfer_eval", SCHEMAS["hermes_skillopt_transfer_eval"], _handle_transfer_eval, "🔁"),
    ("hermes_skillopt_conformance", SCHEMAS["hermes_skillopt_conformance"], _handle_conformance, "✅"),
    ("hermes_skillopt_handoff_optimize", SCHEMAS["hermes_skillopt_handoff_optimize"], _handle_handoff_optimize, "🤝"),
)


def _bind_plugin_ctx(handler: Callable[..., str], plugin_ctx: Any) -> Callable[..., str]:
    def _handler(args: dict, **kw) -> str:
        if kw.get("ctx") is None:
            kw["ctx"] = plugin_ctx
        return handler(args, **kw)
    return _handler


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset="hermes_skillopt", schema=schema, handler=_bind_plugin_ctx(handler, ctx), emoji=emoji)
