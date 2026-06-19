from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from hermes_skillopt import core

PLACEHOLDER_RE = re.compile(r"(?i)\b(todo|tbd|placeholder|lorem ipsum|fill me in|coming soon|fixme|xxx)\b")
SECTION_PATTERNS = {
    "instructions": re.compile(r"(?im)^#{1,3}\s*(instructions?|usage|behavior|guidelines?)\b|\b(when asked|you should|use this skill|follow these)\b"),
    "triggers": re.compile(r"(?im)^#{1,3}\s*(triggers?|when to use|activation)\b|\b(use when|trigger|when the user|applicable when)\b"),
    "steps": re.compile(r"(?im)^#{1,3}\s*(steps?|workflow|process|procedure)\b|(?:^|\n)\s*(?:[-*]|\d+[.)])\s+"),
    "pitfalls": re.compile(r"(?im)^#{1,3}\s*(pitfalls?|avoid|caveats?|failure modes?|do not)\b|\b(do not|avoid|never|pitfall|caveat)\b"),
    "verification": re.compile(r"(?im)^#{1,3}\s*(verification|verify|tests?|quality|done when)\b|\b(verify|test|check|confirm|validate)\b"),
}


def _resolve_skill(home: Path, *, skill: str | None = None, skill_path: str | Path | None = None) -> core.Skill:
    if skill_path:
        path = Path(skill_path).expanduser().resolve(strict=True)
        if path.name != "SKILL.md":
            raise ValueError("skill_path must point to a SKILL.md file")
        text = core.read_text(path)
        fm = core._skill_frontmatter_map(text)
        name = str(fm.get("name") or path.parent.name)
        try:
            rel = str(path.relative_to(home.resolve()))
        except Exception:
            rel = str(path)
        return core.Skill(name=name, path=path, relpath=rel, sha256=core.sha256_text(text))
    return core.find_skill(home, skill)


def _meaningful_text(text: str) -> str:
    body = text
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        if end != -1:
            body = body[end + 4 :]
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    return body.strip()


def _secret_findings(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pat in core.SECRET_PATTERNS:
            if pat.search(line):
                findings.append({"line": lineno, "redacted": core.redact_secrets(line.strip())[:180]})
                break
    return findings[:20]


def _eval_summary(home: Path, skill_name: str) -> dict[str, Any]:
    try:
        from hermes_skillopt.eval_packs import eval_pack_inventory

        inv = eval_pack_inventory(hermes_home_path=str(home), skill=skill_name)
        rows = [r for r in (inv.get("skills") or []) if isinstance(r, dict)]
        row = rows[0] if rows else {}
        command_home = shlex.quote(str(home))
        skill_q = shlex.quote(skill_name)
        if not row.get("has_eval_pack"):
            next_cmd = f"hermes-skillopt --home {command_home} eval-pack-scaffold --skill {skill_q}"
        else:
            next_cmd = f"hermes-skillopt --home {command_home} eval-pack-doctor --skill {skill_q} --digest"
        return {
            "available": True,
            "has_eval_pack": bool(row.get("has_eval_pack")),
            "production_eligible": bool(row.get("production_eligible")),
            "review_only": bool(row.get("review_only")) if row else True,
            "split_complete": bool(row.get("split_complete")),
            "invalid_eval_pack_count": int(row.get("invalid_eval_pack_count") or 0),
            "recommended_next_action": row.get("recommended_next_action") or ("create review-only eval scaffold" if not row.get("has_eval_pack") else "inspect eval pack"),
            "safe_eval_skeleton_command": next_cmd,
            "inventory_row": row,
        }
    except Exception as exc:
        return {"available": False, "has_eval_pack": False, "production_eligible": False, "review_only": True, "reason": f"{type(exc).__name__}: {core.redact_secrets(str(exc))}", "safe_eval_skeleton_command": f"hermes-skillopt --home {shlex.quote(str(home))} eval-pack-scaffold --skill {shlex.quote(skill_name)}"}


def skill_quality_report(
    *,
    hermes_home_path: str | None = None,
    skill: str | None = None,
    skill_path: str | Path | None = None,
    create_eval_skeleton: bool = False,
    output: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Read-only skill quality/lint report by default; optional skeleton writes review-only eval pack only."""

    home = core.hermes_home(hermes_home_path)
    sk = _resolve_skill(home, skill=skill, skill_path=skill_path)
    before_sha = core.sha256_file(sk.path)
    text = core.read_text(sk.path)
    fm = core._skill_frontmatter_map(text)
    body = _meaningful_text(text)
    body_words = re.findall(r"[A-Za-z0-9_'-]+", body)
    placeholder_lines = [i for i, line in enumerate(text.splitlines(), 1) if PLACEHOLDER_RE.search(line)]
    secrets = _secret_findings(text)
    section_hits = {name: bool(pat.search(body)) for name, pat in SECTION_PATTERNS.items()}

    blockers: list[str] = []
    warnings: list[str] = []
    if not fm.get("name"):
        blockers.append("frontmatter name is missing")
    if not fm.get("description") or len(fm.get("description", "").strip()) < 12:
        blockers.append("frontmatter description is missing or too short")
    if len(body_words) < 30:
        blockers.append("instructions are too short to guide reliable behavior")
    missing_sections = [k for k, ok in section_hits.items() if not ok]
    for section in missing_sections:
        warnings.append(f"missing or weak {section} guidance")
    if len(missing_sections) >= 3:
        blockers.append("skill lacks several meaningful guidance areas: " + ", ".join(missing_sections))
    if placeholder_lines:
        blockers.append("placeholder/TODO-only content detected")
    if body and len(body_words) <= 12 and placeholder_lines:
        blockers.append("skill appears placeholder-only")
    if secrets:
        blockers.append("secret-looking strings detected; remove credentials before optimization")

    try:
        package_support = core._safe_skill_package_support(sk, home)
    except Exception as exc:
        package_support = {"schema_version": "hermes-skillopt-skill-package-support-v1", "advisory_only": True, "content_included": False, "warnings": [f"package scan unavailable: {type(exc).__name__}: {exc}"]}
    try:
        native = core.native_skill_metadata_snapshot(home, sk)
    except Exception as exc:
        native = {"schema_version": "hermes-native-skill-metadata-snapshot-v1", "read_only": True, "labels": ["unavailable"], "signals": {}, "error": f"{type(exc).__name__}: {exc}"}
    eval_readiness = _eval_summary(home, sk.name)

    score = 100
    score -= 18 * len(blockers)
    score -= 5 * len(warnings)
    score = max(0, min(100, score))
    passed_basics = not blockers and bool(fm.get("name")) and bool(fm.get("description")) and len(body_words) >= 30
    result: dict[str, Any] = {
        "success": True,
        "schema_version": "hermes-skillopt-skill-quality-v1",
        "mode": "skill_quality_read_only" if not create_eval_skeleton else "skill_quality_with_review_only_eval_skeleton",
        "read_only": not bool(create_eval_skeleton),
        "live_skill_writes": False,
        "auto_adopt": False,
        "review_only": True,
        "hermes_home": str(home),
        "skill": {"name": sk.name, "path": str(sk.path), "relpath": sk.relpath, "sha256_before": before_sha},
        "frontmatter": {"name": fm.get("name"), "description": fm.get("description"), "keys": sorted(fm.keys())},
        "quality": {"passed_basics": passed_basics, "score": score, "blockers": sorted(set(blockers)), "warnings": sorted(set(warnings)), "section_hits": section_hits, "body_word_count": len(body_words)},
        "placeholder_detection": {"found": bool(placeholder_lines), "lines": placeholder_lines[:20]},
        "secret_scan": {"found": bool(secrets), "findings": secrets, "redacted": True},
        "native_metadata_advisory": native,
        "package_support_summary": {"content_included": False, "support_total_file_count": package_support.get("total_file_count"), "support_dirs_present": [k for k, v in (package_support.get("support_dirs") or {}).items() if isinstance(v, dict) and v.get("present")], "warnings": package_support.get("warnings") or []},
        "package_support": package_support,
        "eval_pack_readiness": eval_readiness,
        "safe_next_actions": [eval_readiness.get("safe_eval_skeleton_command"), "Improve SKILL.md frontmatter/instructions/triggers/steps/pitfalls/verification hints; then rerun skill-quality."],
        "boundary": "SkillOpt complements Hermes curator; curator owns lifecycle/archive/consolidation. This report is diagnostic and never auto-adopts or mutates live SKILL.md.",
    }
    if create_eval_skeleton:
        from hermes_skillopt.eval_packs import scaffold_eval_pack

        result["eval_skeleton"] = scaffold_eval_pack(skill=sk.name, output=output, hermes_home_path=str(home), overwrite=overwrite)
        result["read_only"] = False
        result["review_only"] = True
        result["live_skill_writes"] = False
    after_sha = core.sha256_file(sk.path)
    result["skill"]["sha256_after"] = after_sha
    result["live_skill_unchanged"] = before_sha == after_sha
    return result


def skill_quality_digest(report: dict[str, Any]) -> dict[str, Any]:
    raw_q = report.get("quality")
    raw_ev = report.get("eval_pack_readiness")
    raw_skill = report.get("skill")
    q: dict[str, Any] = raw_q if isinstance(raw_q, dict) else {}
    ev: dict[str, Any] = raw_ev if isinstance(raw_ev, dict) else {}
    skill_info: dict[str, Any] = raw_skill if isinstance(raw_skill, dict) else {}
    lines = [
        f"Hermes SkillOpt skill-quality digest: {skill_info.get('name')}",
        f"passed_basics: {q.get('passed_basics')} | score: {q.get('score')}",
        f"read_only: {report.get('read_only')} | auto_adopt: {report.get('auto_adopt')} | live_skill_writes: {report.get('live_skill_writes')}",
        f"eval_pack: has={ev.get('has_eval_pack')} production_eligible={ev.get('production_eligible')} split_complete={ev.get('split_complete')}",
        "boundary: scheduled usage is diagnostic only and never auto-adopts; SkillOpt complements curator lifecycle ownership.",
    ]
    blockers = q.get("blockers") or []
    warnings = q.get("warnings") or []
    if blockers:
        lines.append("blockers: " + "; ".join(str(b) for b in blockers[:6]))
    if warnings:
        lines.append("warnings: " + "; ".join(str(w) for w in warnings[:5]))
    if ev.get("safe_eval_skeleton_command"):
        lines.append("safe_eval_skeleton_command: " + str(ev.get("safe_eval_skeleton_command")))
    return {"success": True, "schema_version": "hermes-skillopt-skill-quality-digest-v1", "read_only": report.get("read_only"), "auto_adopt": False, "digest": "\n".join(lines), "summary": report}
