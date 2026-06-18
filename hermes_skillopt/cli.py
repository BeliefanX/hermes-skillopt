from __future__ import annotations

import argparse
import json
import sys
from hermes_skillopt import core
from hermes_skillopt import multi_agent


def add_full_args(p: argparse.ArgumentParser) -> None:
    p.description = (p.description or "") + " Hermes SkillOpt core adapter: trainable skill state, frozen target executor, optimizer bounded edits, held-out validation gate; staged-only unless explicitly adopted."
    p.add_argument("--skill")
    p.add_argument("--query")
    p.add_argument("--lookback-days", type=int, default=14)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--eval-file", help="Curated replay/eval tasks JSONL/JSON under HERMES_HOME; defaults to skillopt/evals/<skill>.jsonl or skill-dir/evals/*.jsonl")
    p.add_argument("--iterations", type=int, default=1)
    p.add_argument("--edit-budget", type=int, default=3, help="Maximum bounded skill edits per candidate (default: 3)")
    p.add_argument("--candidate-count", type=int, default=1, help="Conservative multi-candidate count per iteration; selects best strict validation improver")
    p.add_argument("--backend", choices=["auto", "hermes", "mock"], default="auto", help="Back-compat alias for --optimizer-backend")
    p.add_argument("--optimizer-backend", choices=["auto", "hermes", "mock"], help="Optimizer LLM backend for reflection/bounded edits. mock is review-only and never adoption-capable.")
    p.add_argument("--allow-mock", action="store_true", help="Permit mock optimizer fallback; any mock provenance is review-only/non-adoptable.")
    p.add_argument("--target-executor", choices=["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"], default="auto", help="Frozen target executor/backend for scoring; judge/LLM explanations are advisory only, not an adoption gate.")
    p.add_argument("--target-backend", choices=["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"], help="Explicit target backend alias for --target-executor")
    p.add_argument("--gate-mode", choices=["soft", "hard", "mixed", "strict"], default="strict", help="Deterministic metric gate; default strict is the production-capable default. soft/mixed are review-only; mock is review-only; judge explanation is advisory only.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--resume-run-id", help="Opt-in resume/reuse of a prior checkpointed full-run when input/config/provenance fingerprints match")


def _full_kwargs(args: argparse.Namespace) -> dict:
    return {
        "skill": args.skill,
        "query": getattr(args, "query", None) or getattr(args, "session_search", None) or getattr(args, "goal", None),
        "lookback_days": args.lookback_days,
        "limit": args.limit,
        "iterations": args.iterations,
        "edit_budget": args.edit_budget,
        "candidate_count": getattr(args, "candidate_count", 1),
        "backend": args.backend,
        "optimizer_backend": getattr(args, "optimizer_backend", None),
        "allow_mock": args.allow_mock,
        "force": args.force,
        "hermes_home_path": args.home,
        "eval_file": getattr(args, "eval_file", None),
        "target_executor": getattr(args, "target_executor", "auto"),
        "target_backend": getattr(args, "target_backend", None),
        "gate_mode": getattr(args, "gate_mode", "strict"),
        "resume_run_id": getattr(args, "resume_run_id", None),
    }


def _adopt_confirmation_ok(args: argparse.Namespace) -> bool:
    if getattr(args, "yes_i_understand_skillopt_adopt", False):
        return True
    expected = f"ADOPT {args.run_id}"
    supplied = getattr(args, "confirm", None)
    if supplied is None and sys.stdin.isatty():
        supplied = input(f"Type {expected!r} to adopt staged SkillOpt run: ")
    if (supplied or "").strip() == expected:
        return True
    print(f"adopt refused: type {expected!r} exactly via --confirm, or use --yes-i-understand-skillopt-adopt for non-interactive CI", file=sys.stderr)
    return False


def main() -> int:
    p = argparse.ArgumentParser(prog="hermes-skillopt")
    p.add_argument("--home", dest="home")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    scout_p = sub.add_parser("scout", help="Read-only notification-ready SkillOpt scout summary; no full_run/optimize/adopt/rollback/fetch")
    scout_p.add_argument("--skill")
    scout_p.add_argument("--limit", type=int, default=5)
    scout_p.add_argument("--stale-after-hours", type=float, default=24.0)
    scout_p.add_argument("--output", help="Optional guarded JSON report path under skillopt/reports or staging; default writes nothing")
    def add_fleet_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--limit", type=int, default=50, help="Max recent staging run directories to inspect (capped at 200)")
        parser.add_argument("--skill", help="Optional skill_name filter")
    d = sub.add_parser("dry-run"); d.add_argument("--skill"); d.add_argument("--goal"); d.add_argument("--session-search"); d.add_argument("--use-llm", action="store_true")
    fr = sub.add_parser("full-run"); add_full_args(fr)
    doc = sub.add_parser("doctor", help="Read-only readiness/guided UX report; no full_run/adopt/rollback/fetch")
    doc.add_argument("--skill")
    opt = sub.add_parser("optimize", help="Guided alias for staged-only full_run with smoke/review/production intent presets; never auto-adopts")
    opt.add_argument("--intent", choices=["smoke", "review", "production"], default="review")
    add_full_args(opt)
    bp = sub.add_parser("batch-preflight", help="Read-only validation of a staged-only SkillOpt batch plan JSON")
    bp.add_argument("plan", help="Batch plan JSON path")
    br = sub.add_parser("batch-run", help="Run a preflighted SkillOpt batch under staging only; never adopts")
    br.add_argument("plan", help="Batch plan JSON path")
    frep = sub.add_parser("fleet-report", help="Read-only fleet report over recent single and batch runs; no resume/rollback/writeback")
    add_fleet_args(frep)
    fres = sub.add_parser("fleet-resume-plan", help="Read-only resume plan; completed exact-fingerprint reuse only, partial continuation refused")
    add_fleet_args(fres)
    frb = sub.add_parser("fleet-rollback-plan", help="Read-only rollback plan; lists per-run rollbackable backups, no bulk rollback/writeback")
    add_fleet_args(frb)
    hyg = sub.add_parser("artifact-hygiene-report", help="Read-only staging artifact hygiene planner; classifies stale/tampered/orphaned run dirs and never deletes")
    hyg.add_argument("--limit", type=int, default=200)
    hyg.add_argument("--stale-after-hours", type=float, default=24.0)
    inv = sub.add_parser("eval-pack-inventory", help="Read-only inventory of skills and matching eval packs")
    inv.add_argument("--skill")
    scaf = sub.add_parser("eval-pack-scaffold", help="Generate a review-only eval-pack scaffold with train/val/test samples")
    scaf.add_argument("--skill", required=True); scaf.add_argument("--output"); scaf.add_argument("--overwrite", action="store_true")
    cur = sub.add_parser("eval-pack-curate", help="Create a safe curated eval pack from a local tasks JSON file; review-only unless explicit production policy is supplied")
    cur.add_argument("--skill", required=True); cur.add_argument("--tasks", required=True, help="Local JSON list or {'tasks':[...]} file"); cur.add_argument("--output"); cur.add_argument("--pack-id"); cur.add_argument("--version", default="curated-v1"); cur.add_argument("--production-policy", help="Optional local JSON policy file"); cur.add_argument("--eval-execution-contract", help="Optional local JSON execution contract file"); cur.add_argument("--overwrite", action="store_true")
    mine = sub.add_parser("eval-pack-mine-sessions", help="Mine redacted sessions/session-like fixtures into a draft review-only eval pack")
    mine.add_argument("--skill", required=True); mine.add_argument("--output"); mine.add_argument("--query"); mine.add_argument("--lookback-days", type=int, default=14); mine.add_argument("--limit", type=int, default=50); mine.add_argument("--session-fixture"); mine.add_argument("--overwrite", action="store_true")
    eo = sub.add_parser("eval-only", help="Read-only fixed-skill evaluation against an explicit curated eval pack; no training/adoption side effects")
    eo.add_argument("--skill"); eo.add_argument("--skill-file"); eo.add_argument("--eval-file", required=True); eo.add_argument("--target-executor", choices=["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"], default="auto"); eo.add_argument("--target-backend", choices=["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"])
    bm = sub.add_parser("benchmark", help="Alias for eval-only that also writes benchmark_report.json with reproducibility fingerprints")
    bm.add_argument("--skill"); bm.add_argument("--skill-file"); bm.add_argument("--eval-file", required=True); bm.add_argument("--target-executor", choices=["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"], default="auto"); bm.add_argument("--target-backend", choices=["auto", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1", "scorecard", "live-readonly"])
    run = sub.add_parser("run"); run.add_argument("--mode", choices=["full", "legacy"], default="full"); run.add_argument("--goal"); run.add_argument("--session-search"); run.add_argument("--use-llm", action="store_true"); add_full_args(run)
    r = sub.add_parser("review", help="Review a staged run. Use run_id, 'latest', --latest, or --summary for decision-first output.")
    r.add_argument("run_id", nargs="?", default="latest")
    r.add_argument("--latest", action="store_true")
    r.add_argument("--summary", action="store_true")
    r.add_argument("--digest", action="store_true", help="Telegram-friendly slim digest with decision fields and artifact refs only")
    r.add_argument("--slim", action="store_true")
    r.add_argument("--include-diff-chars", type=int, default=4000)
    ri = sub.add_parser("resume-inspect", help="Read-only checkpoint/stage fingerprint inspection; never replays partial stages"); ri.add_argument("run_id")
    a = sub.add_parser("adopt"); a.add_argument("run_id"); a.add_argument("--force", action="store_true"); a.add_argument("--confirm", help="Typed confirmation; must exactly equal ADOPT <run_id>"); a.add_argument("--yes-i-understand-skillopt-adopt", action="store_true", help="Deliberate non-interactive override for CI/tests; core gate checks still apply"); a.add_argument("--unsafe-cross-profile-writeback", action="store_true", help="Allow --home to differ from active HERMES_HOME for offline maintenance only")
    rb = sub.add_parser("rollback"); rb.add_argument("run_id"); rb.add_argument("--force", action="store_true"); rb.add_argument("--unsafe-cross-profile-writeback", action="store_true", help="Allow --home to differ from active HERMES_HOME for offline maintenance only")
    sub.add_parser("upstream-status")
    sub.add_parser("compare-upstream-pin", help="Read-only report comparing local clone to pinned upstream lock; no fetch/merge/write")
    sub.add_parser("benchmark-parity-status", help="Read-only label/status for Hermes benchmark mode versus upstream parity; no rollout/adopt")
    uu = sub.add_parser("upstream-update"); uu.add_argument("--fetch-only", action="store_true")
    ubi = sub.add_parser("import-upstream-benchmark", help="Safely convert an upstream-style JSON benchmark manifest into a Hermes eval pack")
    ubi.add_argument("manifest"); ubi.add_argument("--output"); ubi.add_argument("--pack-id"); ubi.add_argument("--version"); ubi.add_argument("--curated", action="store_true", help="Mark imported pack as curated instead of sample/review-only")
    ubi.add_argument("--from-pinned-manifest", action="store_true", help="Require manifest to be a JSON file under the canonical pinned upstream clone and label output as pinned_manifest_replay evidence")
    ubi.add_argument("--adapter-level", choices=["json_import_only", "pinned_manifest_replay"], default="json_import_only", help="Safe adapter evidence level. Full upstream parity execution is intentionally unsupported.")
    te = sub.add_parser("transfer-eval", help="Read-only staged/proposed skill transfer evaluation across target/profile configs")
    te.add_argument("--run-id"); te.add_argument("--skill-file"); te.add_argument("--eval-file"); te.add_argument("--target", action="append", choices=["scorecard", "replay", "sandbox", "frozen-hermes", "frozen_hermes_target_execution_v1"], dest="targets"); te.add_argument("--profile-home", action="append", dest="profile_homes"); te.add_argument("--output"); te.add_argument("--allow-live-skill-file", action="store_true", help="Allow explicit --skill-file input; still never writes live skills")
    conf = sub.add_parser("conformance", help="Run local conformance and write a JSON report. Default mode=quick is a smoke suite, not full repo health; use --mode full for all pytest tests.")
    conf.add_argument("--output"); conf.add_argument("--pytest-arg", action="append", dest="pytest_args"); conf.add_argument("--timeout", type=int, default=180); conf.add_argument("--mode", choices=["quick", "full"], default="quick")
    ho = sub.add_parser("handoff-optimize"); ho.add_argument("requirements"); ho.add_argument("--worker"); ho.add_argument("--context-budget-chars", type=int, default=6000)
    web = sub.add_parser("webui", help="Launch the optional React/FastAPI Hermes SkillOpt WebUI")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=7860)
    web.add_argument("--share", action="store_true")
    web.add_argument("--browser", action="store_true")
    web.add_argument("--home", dest="web_home", help="HERMES_HOME override for WebUI defaults and callbacks")
    args = p.parse_args()
    if args.cmd == "status":
        out = core.status(args.home)
    elif args.cmd == "scout":
        out = core.scout(args.home, skill=args.skill, limit=args.limit, stale_after_hours=args.stale_after_hours, output_path=args.output)
    elif args.cmd == "doctor":
        out = core.doctor(args.home, skill=args.skill)
    elif args.cmd == "dry-run":
        out = core.dry_run(args.skill, args.goal, args.session_search, args.home, use_llm=args.use_llm)
    elif args.cmd == "full-run" or (args.cmd == "run" and args.mode == "full"):
        out = core.full_run(**_full_kwargs(args))
    elif args.cmd == "optimize":
        kw = _full_kwargs(args)
        kw.pop("force", None)
        explicit_allow_mock = "--allow-mock" in sys.argv
        explicit_gate_mode = any(a == "--gate-mode" or a.startswith("--gate-mode=") for a in sys.argv)
        if args.intent in {"smoke", "review"} and not explicit_allow_mock:
            kw.pop("allow_mock", None)
        if args.intent in {"smoke", "review"} and not explicit_gate_mode:
            kw.pop("gate_mode", None)
        try:
            out = core.guided_optimize(intent=args.intent, **kw)
        except ValueError as exc:
            print(f"optimize refused: {exc}", file=sys.stderr)
            return 2
    elif args.cmd == "batch-preflight":
        from hermes_skillopt.batch import batch_preflight
        out = batch_preflight(args.plan, hermes_home_path=args.home)
    elif args.cmd == "batch-run":
        from hermes_skillopt.batch import run_batch
        out = run_batch(args.plan, hermes_home_path=args.home)
    elif args.cmd == "fleet-report":
        out = core.fleet_report(args.home, limit=args.limit, skill=args.skill)
    elif args.cmd == "fleet-resume-plan":
        out = core.fleet_resume_plan(args.home, limit=args.limit, skill=args.skill)
    elif args.cmd == "fleet-rollback-plan":
        out = core.fleet_rollback_plan(args.home, limit=args.limit, skill=args.skill)
    elif args.cmd == "artifact-hygiene-report":
        out = core.artifact_hygiene_report(args.home, limit=args.limit, stale_after_hours=args.stale_after_hours)
    elif args.cmd == "eval-pack-inventory":
        from hermes_skillopt.eval_packs import eval_pack_inventory
        out = eval_pack_inventory(hermes_home_path=args.home, skill=args.skill)
    elif args.cmd == "eval-pack-scaffold":
        from hermes_skillopt.eval_packs import scaffold_eval_pack
        out = scaffold_eval_pack(skill=args.skill, output=args.output, hermes_home_path=args.home, overwrite=args.overwrite)
    elif args.cmd == "eval-pack-curate":
        from pathlib import Path
        from hermes_skillopt.eval_packs import create_curated_eval_pack
        tasks_payload = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
        tasks_obj = tasks_payload.get("tasks") if isinstance(tasks_payload, dict) else tasks_payload
        if not isinstance(tasks_obj, list):
            raise ValueError("--tasks must contain a JSON list or {'tasks': [...]} object")
        policy = json.loads(Path(args.production_policy).read_text(encoding="utf-8")) if args.production_policy else None
        contract = json.loads(Path(args.eval_execution_contract).read_text(encoding="utf-8")) if args.eval_execution_contract else None
        out = create_curated_eval_pack(skill=args.skill, tasks=tasks_obj, output=args.output, hermes_home_path=args.home, pack_id=args.pack_id, version=args.version, production_policy=policy, eval_execution_contract=contract, overwrite=args.overwrite)
    elif args.cmd == "eval-pack-mine-sessions":
        from hermes_skillopt.eval_packs import mine_session_eval_pack
        out = mine_session_eval_pack(skill=args.skill, output=args.output, hermes_home_path=args.home, query=args.query, lookback_days=args.lookback_days, limit=args.limit, session_fixture=args.session_fixture, overwrite=args.overwrite)
    elif args.cmd in {"eval-only", "benchmark"}:
        out = core.eval_only(skill=args.skill, skill_file=args.skill_file, eval_file=args.eval_file, hermes_home_path=args.home, target_executor=args.target_executor, target_backend=args.target_backend)
    elif args.cmd == "run" and args.mode == "legacy":
        out = core.dry_run(args.skill, args.goal, args.session_search, args.home, use_llm=args.use_llm)
    elif args.cmd == "review":
        rid = "latest" if args.latest else args.run_id
        if args.digest:
            out = core.review_digest(rid, args.home)
        elif args.summary:
            out = core.review_decision_summary(rid, args.home)
        elif rid == "latest":
            out = core.review_latest(args.home, include_diff_chars=args.include_diff_chars, slim=args.slim)
        else:
            out = core.review(rid, args.home, include_diff_chars=args.include_diff_chars, slim=args.slim)
    elif args.cmd == "resume-inspect":
        out = core.inspect_resume_run(args.run_id, hermes_home_path=args.home)
    elif args.cmd == "adopt":
        if args.unsafe_cross_profile_writeback and not args.home:
            print("--unsafe-cross-profile-writeback requires --home", file=sys.stderr)
            return 2
        if not _adopt_confirmation_ok(args):
            return 2
        out = core.adopt(args.run_id, args.home, args.force, unsafe_cross_profile=args.unsafe_cross_profile_writeback)
    elif args.cmd == "rollback":
        if args.unsafe_cross_profile_writeback and not args.home:
            print("--unsafe-cross-profile-writeback requires --home", file=sys.stderr)
            return 2
        out = core.rollback(args.run_id, args.home, args.force, unsafe_cross_profile=args.unsafe_cross_profile_writeback)
    elif args.cmd == "upstream-status":
        out = core.upstream_status(args.home)
    elif args.cmd == "compare-upstream-pin":
        out = core.compare_upstream_pin(args.home)
    elif args.cmd == "benchmark-parity-status":
        out = core.benchmark_parity_status(args.home)
    elif args.cmd == "upstream-update":
        out = core.upstream_update(args.home, None, args.fetch_only)
    elif args.cmd == "import-upstream-benchmark":
        from hermes_skillopt.benchmark_bridge import import_pinned_upstream_manifest, import_upstream_manifest
        if args.from_pinned_manifest or args.adapter_level == "pinned_manifest_replay":
            out = import_pinned_upstream_manifest(args.manifest, args.output, pack_id=args.pack_id, version=args.version, sample_pack=not args.curated, hermes_home=args.home)
        else:
            out = import_upstream_manifest(args.manifest, args.output, pack_id=args.pack_id, version=args.version, sample_pack=not args.curated, adapter_level=args.adapter_level, hermes_home=args.home)
    elif args.cmd == "transfer-eval":
        from hermes_skillopt.transfer import transfer_eval
        out = transfer_eval(hermes_home_path=args.home, run_id=args.run_id, skill_file=args.skill_file, eval_file=args.eval_file, targets=args.targets, profile_homes=args.profile_homes, output_path=args.output, staged_only=not args.allow_live_skill_file)
    elif args.cmd == "conformance":
        from hermes_skillopt.conformance import run_conformance
        out = run_conformance(output_path=args.output, pytest_args=args.pytest_args, timeout=args.timeout, mode=args.mode)
    elif args.cmd == "handoff-optimize":
        out = multi_agent.optimize_delegate_handoff(args.requirements, worker=args.worker, context_budget_chars=args.context_budget_chars)
    elif args.cmd == "webui":
        from hermes_skillopt import webui
        launch_args = ["--host", args.host, "--port", str(args.port)]
        web_home = args.web_home or args.home
        if web_home:
            launch_args.extend(["--home", web_home])
        if args.share:
            launch_args.append("--share")
        if args.browser:
            launch_args.append("--browser")
        return webui.main(launch_args)
    else:
        raise SystemExit(2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
