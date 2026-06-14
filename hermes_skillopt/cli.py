from __future__ import annotations

import argparse
import json
from hermes_skillopt import core


def main() -> int:
    p = argparse.ArgumentParser(prog="hermes-skillopt")
    p.add_argument("--home", dest="home")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("dry-run"); d.add_argument("--skill"); d.add_argument("--goal"); d.add_argument("--session-search"); d.add_argument("--use-llm", action="store_true")
    r = sub.add_parser("review"); r.add_argument("run_id")
    a = sub.add_parser("adopt"); a.add_argument("run_id"); a.add_argument("--force", action="store_true")
    rb = sub.add_parser("rollback"); rb.add_argument("run_id")
    sub.add_parser("upstream-status")
    uu = sub.add_parser("upstream-update"); uu.add_argument("--repo-path"); uu.add_argument("--fetch-only", action="store_true")
    args = p.parse_args()
    if args.cmd == "status": out = core.status(args.home)
    elif args.cmd == "dry-run": out = core.dry_run(args.skill, args.goal, args.session_search, args.home, use_llm=args.use_llm)
    elif args.cmd == "review": out = core.review(args.run_id, args.home)
    elif args.cmd == "adopt": out = core.adopt(args.run_id, args.home, args.force)
    elif args.cmd == "rollback": out = core.rollback(args.run_id, args.home)
    elif args.cmd == "upstream-status": out = core.upstream_status(args.home)
    elif args.cmd == "upstream-update": out = core.upstream_update(args.home, args.repo_path, args.fetch_only)
    else: raise SystemExit(2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
