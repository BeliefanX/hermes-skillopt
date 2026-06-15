from __future__ import annotations

import argparse
import json
from hermes_skillopt import core


def add_full_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--skill")
    p.add_argument("--query")
    p.add_argument("--lookback-days", type=int, default=14)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--iterations", type=int, default=1)
    p.add_argument("--edit-budget", type=int, default=3)
    p.add_argument("--backend", choices=["auto", "hermes", "mock"], default="auto")
    p.add_argument("--allow-mock", action="store_true")
    p.add_argument("--auto-adopt", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")


def main() -> int:
    p = argparse.ArgumentParser(prog="hermes-skillopt")
    p.add_argument("--home", dest="home")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("dry-run"); d.add_argument("--skill"); d.add_argument("--goal"); d.add_argument("--session-search"); d.add_argument("--use-llm", action="store_true")
    fr = sub.add_parser("full-run"); add_full_args(fr)
    run = sub.add_parser("run"); run.add_argument("--mode", choices=["full", "legacy"], default="full"); run.add_argument("--goal"); run.add_argument("--session-search"); run.add_argument("--use-llm", action="store_true"); add_full_args(run)
    r = sub.add_parser("review"); r.add_argument("run_id")
    a = sub.add_parser("adopt"); a.add_argument("run_id"); a.add_argument("--force", action="store_true")
    rb = sub.add_parser("rollback"); rb.add_argument("run_id"); rb.add_argument("--force", action="store_true")
    sub.add_parser("upstream-status")
    uu = sub.add_parser("upstream-update"); uu.add_argument("--repo-path"); uu.add_argument("--fetch-only", action="store_true")
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
        out = core.full_run(skill=args.skill, query=getattr(args, "query", None) or getattr(args, "session_search", None) or getattr(args, "goal", None), lookback_days=args.lookback_days, limit=args.limit, iterations=args.iterations, edit_budget=args.edit_budget, backend=args.backend, allow_mock=args.allow_mock, auto_adopt=args.auto_adopt, force=args.force, dry_run=args.dry_run, hermes_home_path=args.home)
    elif args.cmd == "run" and args.mode == "legacy":
        out = core.dry_run(args.skill, args.goal, args.session_search, args.home, use_llm=args.use_llm)
    elif args.cmd == "review":
        out = core.review(args.run_id, args.home)
    elif args.cmd == "adopt":
        out = core.adopt(args.run_id, args.home, args.force)
    elif args.cmd == "rollback":
        out = core.rollback(args.run_id, args.home, args.force)
    elif args.cmd == "upstream-status":
        out = core.upstream_status(args.home)
    elif args.cmd == "upstream-update":
        out = core.upstream_update(args.home, args.repo_path, args.fetch_only)
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
