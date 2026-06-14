from __future__ import annotations

import json
from typing import Any, Callable

from hermes_skillopt import core

try:
    from tools.registry import tool_error, tool_result
except Exception:  # local CLI/tests outside Hermes core
    def tool_result(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    def tool_error(message: str, **extra: Any) -> str:
        return json.dumps({"success": False, "error": message, **extra}, ensure_ascii=False, indent=2)


def _schema(description: str, props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "description": description, "properties": props or {}, "required": required or []}

COMMON_HOME = {"hermes_home": {"type": "string", "description": "Optional HERMES_HOME override; defaults to current profile home (~/.hermes)."}}

SCHEMAS = {
    "hermes_skillopt_status": _schema("Show SkillOpt plugin status, discovered skill count, and recent staged runs.", COMMON_HOME),
    "hermes_skillopt_dry_run": _schema("Create a safe staged SkillOpt proposal/diff. Does not modify the target skill.", {**COMMON_HOME, "skill": {"type": "string"}, "goal": {"type": "string"}, "session_search": {"type": "string"}, "use_llm": {"type": "boolean", "default": False}}),
    "hermes_skillopt_run": _schema("Alias of hermes_skillopt_dry_run; stages a proposal only, never auto-adopts.", {**COMMON_HOME, "skill": {"type": "string"}, "goal": {"type": "string"}, "session_search": {"type": "string"}, "use_llm": {"type": "boolean", "default": False}}),
    "hermes_skillopt_review": _schema("Review a staged SkillOpt run with status, paths, and diff/report preview.", {**COMMON_HOME, "run_id": {"type": "string"}, "include_diff_chars": {"type": "integer", "default": 4000}}, ["run_id"]),
    "hermes_skillopt_adopt": _schema("Adopt a staged proposal into exactly one target SKILL.md after sha guard and backup.", {**COMMON_HOME, "run_id": {"type": "string"}, "force": {"type": "boolean", "default": False}}, ["run_id"]),
    "hermes_skillopt_rollback": _schema("Rollback an adopted run using its backup/original file after current-sha guard unless force=true.", {**COMMON_HOME, "run_id": {"type": "string"}, "force": {"type": "boolean", "default": False}}, ["run_id"]),
    "hermes_skillopt_upstream_status": _schema("Show Microsoft SkillOpt upstream clone and pinned lock status.", {**COMMON_HOME, "repo_path": {"type": "string"}}),
    "hermes_skillopt_upstream_update": _schema("Fetch/update pinned Microsoft SkillOpt upstream clone and write lock; never merges into plugin code.", {**COMMON_HOME, "repo_path": {"type": "string"}, "fetch_only": {"type": "boolean", "default": False}}),
}


def _ok(fn: Callable[..., dict[str, Any]], args: dict[str, Any]) -> str:
    try:
        return tool_result(fn(**args))
    except Exception as exc:
        return tool_error(f"hermes-skillopt failed: {type(exc).__name__}: {exc}")


def _handle_status(args: dict, **kw) -> str:
    return _ok(lambda hermes_home=None: core.status(hermes_home), {"hermes_home": args.get("hermes_home")})


def _handle_dry_run(args: dict, **kw) -> str:
    ctx = kw.get("ctx") or kw.get("context")
    return _ok(lambda **a: core.dry_run(ctx=ctx, **a), {"skill": args.get("skill"), "goal": args.get("goal"), "session_search": args.get("session_search"), "hermes_home_path": args.get("hermes_home"), "use_llm": bool(args.get("use_llm", False))})


def _handle_review(args: dict, **kw) -> str:
    return _ok(core.review, {"run_id": args.get("run_id"), "hermes_home_path": args.get("hermes_home"), "include_diff_chars": int(args.get("include_diff_chars") or 4000)})


def _handle_adopt(args: dict, **kw) -> str:
    return _ok(core.adopt, {"run_id": args.get("run_id"), "hermes_home_path": args.get("hermes_home"), "force": bool(args.get("force", False))})


def _handle_rollback(args: dict, **kw) -> str:
    return _ok(core.rollback, {"run_id": args.get("run_id"), "hermes_home_path": args.get("hermes_home"), "force": bool(args.get("force", False))})


def _handle_upstream_status(args: dict, **kw) -> str:
    return _ok(core.upstream_status, {"hermes_home_path": args.get("hermes_home"), "repo_path": args.get("repo_path")})


def _handle_upstream_update(args: dict, **kw) -> str:
    return _ok(core.upstream_update, {"hermes_home_path": args.get("hermes_home"), "repo_path": args.get("repo_path"), "fetch_only": bool(args.get("fetch_only", False))})


_TOOLS = (
    ("hermes_skillopt_status", SCHEMAS["hermes_skillopt_status"], _handle_status, "🧰"),
    ("hermes_skillopt_dry_run", SCHEMAS["hermes_skillopt_dry_run"], _handle_dry_run, "🧪"),
    ("hermes_skillopt_run", SCHEMAS["hermes_skillopt_run"], _handle_dry_run, "🧪"),
    ("hermes_skillopt_review", SCHEMAS["hermes_skillopt_review"], _handle_review, "🔎"),
    ("hermes_skillopt_adopt", SCHEMAS["hermes_skillopt_adopt"], _handle_adopt, "✅"),
    ("hermes_skillopt_rollback", SCHEMAS["hermes_skillopt_rollback"], _handle_rollback, "↩️"),
    ("hermes_skillopt_upstream_status", SCHEMAS["hermes_skillopt_upstream_status"], _handle_upstream_status, "🌊"),
    ("hermes_skillopt_upstream_update", SCHEMAS["hermes_skillopt_upstream_update"], _handle_upstream_update, "⬆️"),
)


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset="hermes_skillopt", schema=schema, handler=handler, emoji=emoji)
