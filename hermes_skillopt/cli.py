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
    p.add_argument("--edit-budget", type=int, default=3)
    p.add_argument("--candidate-count", type=int, default=1, help="Conservative multi-candidate count per iteration; selects best strict validation improver")
    p.add_argument("--backend", choices=["auto", "hermes", "mock"], default="auto", help="Back-compat alias for --optimizer-backend")
    p.add_argument("--optimizer-backend", choices=["auto", "hermes", "mock"], help="Explicit optimizer backend for reflection/bounded edits")
    p.add_argument("--allow-mock", action="store_true")
    p.add_argument("--target-executor", choices=["auto", "replay", "sandbox", "scorecard"], default="auto", help="Frozen evaluator mode; sandbox uses isolated temp HOME/HERMES_HOME/workspace")
    p.add_argument("--target-backend", choices=["auto", "replay", "sandbox", "scorecard"], help="Explicit target backend alias for --target-executor")
    p.add_argument("--gate-mode", choices=["soft", "hard", "mixed", "strict"], default="soft", help="Deterministic metric gate; default soft preserves strict weighted score improvement")
    p.add_argument("--force", action="store_true")
    p.add_argument("--resume-run-id", help="Opt-in resume/reuse of a prior checkpointed full-run when input/config/provenance fingerprints match")


def main() -> int:
    p = argparse.ArgumentParser(prog="hermes-skillopt")
    p.add_argument("--home", dest="home")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("dry-run"); d.add_argument("--skill"); d.add_argument("--goal"); d.add_argument("--session-search"); d.add_argument("--use-llm", action="store_true")
    fr = sub.add_parser("full-run"); add_full_args(fr)
    run = sub.add_parser("run"); run.add_argument("--mode", choices=["full", "legacy"], default="full"); run.add_argument("--goal"); run.add_argument("--session-search"); run.add_argument("--use-llm", action="store_true"); add_full_args(run)
    r = sub.add_parser("review"); r.add_argument("run_id")
    ri = sub.add_parser("resume-inspect", help="Read-only checkpoint/stage fingerprint inspection; never replays partial stages"); ri.add_argument("run_id")
    a = sub.add_parser("adopt"); a.add_argument("run_id"); a.add_argument("--force", action="store_true"); a.add_argument("--unsafe-cross-profile-writeback", action="store_true", help="Allow --home to differ from active HERMES_HOME for offline maintenance only")
    rb = sub.add_parser("rollback"); rb.add_argument("run_id"); rb.add_argument("--force", action="store_true"); rb.add_argument("--unsafe-cross-profile-writeback", action="store_true", help="Allow --home to differ from active HERMES_HOME for offline maintenance only")
    sub.add_parser("upstream-status")
    uu = sub.add_parser("upstream-update"); uu.add_argument("--fetch-only", action="store_true")
    ubi = sub.add_parser("import-upstream-benchmark", help="Safely convert an upstream-style JSON benchmark manifest into a Hermes eval pack")
    ubi.add_argument("manifest"); ubi.add_argument("--output"); ubi.add_argument("--pack-id"); ubi.add_argument("--version"); ubi.add_argument("--curated", action="store_true", help="Mark imported pack as curated instead of sample/review-only")
    te = sub.add_parser("transfer-eval", help="Read-only staged/proposed skill transfer evaluation across target/profile configs")
    te.add_argument("--run-id"); te.add_argument("--skill-file"); te.add_argument("--eval-file"); te.add_argument("--target", action="append", choices=["scorecard", "replay", "sandbox"], dest="targets"); te.add_argument("--profile-home", action="append", dest="profile_homes"); te.add_argument("--output"); te.add_argument("--allow-live-skill-file", action="store_true", help="Allow explicit --skill-file input; still never writes live skills")
    conf = sub.add_parser("conformance", help="Run deterministic local conformance suite and write a JSON report")
    conf.add_argument("--output"); conf.add_argument("--pytest-arg", action="append", dest="pytest_args"); conf.add_argument("--timeout", type=int, default=180)
    ho = sub.add_parser("handoff-optimize"); ho.add_argument("requirements"); ho.add_argument("--worker"); ho.add_argument("--context-budget-chars", type=int, default=6000)
    web = sub.add_parser("webui", help="Launch the optional Gradio Hermes SkillOpt WebUI")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=7860)
    web.add_argument("--share", action="store_true")
    web.add_argument("--browser", action="store_true")
    web.add_argument("--home", dest="web_home", help="HERMES_HOME override for WebUI defaults and callbacks")
    args = p.parse_args()
    if args.cmd == "status":
        out = core.status(args.home)
    elif args.cmd == "dry-run":
        out = core.dry_run(args.skill, args.goal, args.session_search, args.home, use_llm=args.use_llm)
    elif args.cmd == "full-run" or (args.cmd == "run" and args.mode == "full"):
        out = core.full_run(skill=args.skill, query=getattr(args, "query", None) or getattr(args, "session_search", None) or getattr(args, "goal", None), lookback_days=args.lookback_days, limit=args.limit, iterations=args.iterations, edit_budget=args.edit_budget, candidate_count=getattr(args, "candidate_count", 1), backend=args.backend, optimizer_backend=getattr(args, "optimizer_backend", None), allow_mock=args.allow_mock, force=args.force, hermes_home_path=args.home, eval_file=getattr(args, "eval_file", None), target_executor=getattr(args, "target_executor", "auto"), target_backend=getattr(args, "target_backend", None), gate_mode=getattr(args, "gate_mode", "soft"), resume_run_id=getattr(args, "resume_run_id", None))
    elif args.cmd == "run" and args.mode == "legacy":
        out = core.dry_run(args.skill, args.goal, args.session_search, args.home, use_llm=args.use_llm)
    elif args.cmd == "review":
        out = core.review(args.run_id, args.home)
    elif args.cmd == "resume-inspect":
        out = core.inspect_resume_run(args.run_id, hermes_home_path=args.home)
    elif args.cmd == "adopt":
        if args.unsafe_cross_profile_writeback and not args.home:
            print("--unsafe-cross-profile-writeback requires --home", file=sys.stderr)
            return 2
        out = core.adopt(args.run_id, args.home, args.force, unsafe_cross_profile=args.unsafe_cross_profile_writeback)
    elif args.cmd == "rollback":
        if args.unsafe_cross_profile_writeback and not args.home:
            print("--unsafe-cross-profile-writeback requires --home", file=sys.stderr)
            return 2
        out = core.rollback(args.run_id, args.home, args.force, unsafe_cross_profile=args.unsafe_cross_profile_writeback)
    elif args.cmd == "upstream-status":
        out = core.upstream_status(args.home)
    elif args.cmd == "upstream-update":
        out = core.upstream_update(args.home, None, args.fetch_only)
    elif args.cmd == "import-upstream-benchmark":
        from hermes_skillopt.benchmark_bridge import import_upstream_manifest
        out = import_upstream_manifest(args.manifest, args.output, pack_id=args.pack_id, version=args.version, sample_pack=not args.curated)
    elif args.cmd == "transfer-eval":
        from hermes_skillopt.transfer import transfer_eval
        out = transfer_eval(hermes_home_path=args.home, run_id=args.run_id, skill_file=args.skill_file, eval_file=args.eval_file, targets=args.targets, profile_homes=args.profile_homes, output_path=args.output, staged_only=not args.allow_live_skill_file)
    elif args.cmd == "conformance":
        from hermes_skillopt.conformance import run_conformance
        out = run_conformance(output_path=args.output, pytest_args=args.pytest_args, timeout=args.timeout)
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
