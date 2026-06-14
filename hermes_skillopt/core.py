from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_URL = "https://github.com/microsoft/SkillOpt.git"
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:sk|ghp|gho|xox[baprs])-[-A-Za-z0-9_]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b"),
]


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_secrets(text: str) -> str:
    out = text
    out = SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=<REDACTED>", out)
    for pat in SECRET_PATTERNS[1:]:
        out = pat.sub("<REDACTED>", out)
    return out


def hermes_home(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(raw).expanduser().resolve()


def ensure_dirs(home: Path) -> dict[str, Path]:
    base = home / "skillopt"
    dirs = {"base": base, "staging": base / "staging", "backups": base / "backups", "upstream": base / "upstream"}
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def parse_frontmatter_name(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    fm = text[4:end]
    for line in fm.splitlines():
        if line.strip().startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"\'') or None
    return None


@dataclass
class Skill:
    name: str
    path: Path
    relpath: str
    sha256: str


def discover_skills(home: Path) -> list[Skill]:
    skills_dir = home / "skills"
    if not skills_dir.exists():
        return []
    found: list[Skill] = []
    for p in sorted(skills_dir.rglob("SKILL.md")):
        try:
            text = read_text(p)
        except UnicodeDecodeError:
            continue
        name = parse_frontmatter_name(text) or p.parent.name
        found.append(Skill(name=name, path=p, relpath=str(p.relative_to(home)), sha256=sha256_text(text)))
    return found


def find_skill(home: Path, skill: str | None) -> Skill:
    skills = discover_skills(home)
    if not skills:
        raise ValueError(f"No skills found under {home / 'skills'}")
    if not skill:
        if len(skills) == 1:
            return skills[0]
        raise ValueError("skill is required when multiple skills exist")
    q = skill.strip().lower()
    matches = [s for s in skills if s.name.lower() == q or s.path.parent.name.lower() == q or s.relpath.lower() == q]
    if not matches:
        matches = [s for s in skills if q in s.name.lower() or q in s.relpath.lower()]
    if not matches:
        raise ValueError(f"Skill not found: {skill}")
    if len(matches) > 1:
        raise ValueError(f"Ambiguous skill {skill}: " + ", ".join(s.relpath for s in matches[:8]))
    return matches[0]


def _frontmatter_split(text: str) -> tuple[str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            after = end + len("\n---")
            if after < len(text) and text[after] == "\n":
                after += 1
            return text[:after], text[after:]
    return "", text


def evidence_from_state(home: Path, query: str | None = None, limit: int = 6) -> list[str]:
    db = home / "state.db"
    if not db.exists():
        return []
    snippets: list[str] = []
    try:
        con = sqlite3.connect(str(db))
        cur = con.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
            text_cols = [c for c in cols if any(k in c.lower() for k in ("content", "message", "text", "prompt", "response"))]
            for col in text_cols[:2]:
                if query:
                    rows = cur.execute(f"SELECT {col} FROM {table} WHERE {col} LIKE ? LIMIT ?", (f"%{query}%", limit)).fetchall()
                else:
                    rows = cur.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
                for (val,) in rows:
                    if val:
                        snippets.append(redact_secrets(str(val).replace("\n", " ")[:500]))
                        if len(snippets) >= limit:
                            return snippets
    except Exception:
        return snippets
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass
    return snippets


def propose_skill(original: str, evidence: list[str] | None = None, goal: str | None = None, ctx: Any = None, use_llm: bool = False) -> tuple[str, str]:
    fm, body = _frontmatter_split(original)
    if use_llm and ctx is not None and getattr(getattr(ctx, "llm", None), "complete", None):
        try:
            prompt = "Improve this Hermes SKILL.md conservatively. Preserve YAML frontmatter exactly; return full markdown.\n\n" + original[:12000]
            completed = ctx.llm.complete(prompt)  # best-effort plugin facade
            text = completed if isinstance(completed, str) else getattr(completed, "text", "")
            if text and "---" in text[:10] and sha256_text(text) != sha256_text(original):
                return text, "llm"
        except Exception:
            pass
    bullets = []
    if goal:
        bullets.append(f"- Candidate rule: when this skill is invoked, explicitly check the user's goal: {goal.strip()[:240]}")
    if evidence:
        bullets.append("- Evidence-informed TODO: review recent redacted session snippets and convert recurring successful patterns into stable instructions.")
        for i, snip in enumerate(evidence[:3], 1):
            bullets.append(f"  - Redacted snippet {i}: {snip[:220]}")
    if not bullets:
        bullets = [
            "- Candidate rule: add a short self-check before final answers: confirm scope, required tools, and any safety constraints.",
            "- Candidate rule: prefer staging/dry-run behavior before mutating user files or profile state.",
            "- TODO: replace these candidates with validated project-specific patterns after review.",
        ]
    appendix = "\n\n## SkillOpt Candidate Improvements (staged)\n\n" + "\n".join(bullets) + "\n"
    marker = "## SkillOpt Candidate Improvements (staged)"
    if marker in body:
        body = re.sub(r"\n\n## SkillOpt Candidate Improvements \(staged\)\n.*\Z", appendix, body, flags=re.S)
        proposed = fm + body
    else:
        proposed = fm + body.rstrip() + appendix
    return proposed, "deterministic"


def make_diff(original: str, proposed: str, relpath: str) -> str:
    return "".join(difflib.unified_diff(original.splitlines(True), proposed.splitlines(True), fromfile=f"a/{relpath}", tofile=f"b/{relpath}"))


def load_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads(read_text(run_dir / "manifest.json"))


def save_manifest(run_dir: Path, data: dict[str, Any]) -> None:
    write_text(run_dir / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def resolve_run_dir(home: Path, run_id: str) -> Path:
    p = ensure_dirs(home)["staging"] / run_id
    if not p.exists():
        raise ValueError(f"run_id not found: {run_id}")
    return p


def dry_run(skill: str | None = None, goal: str | None = None, session_search: str | None = None, hermes_home_path: str | None = None, ctx: Any = None, use_llm: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    target = find_skill(home, skill)
    original = read_text(target.path)
    evidence = evidence_from_state(home, session_search or goal)
    proposed, engine = propose_skill(original, evidence, goal, ctx, use_llm)
    rid = now_id() + "-" + target.name.replace("/", "-")
    run_dir = dirs["staging"] / rid
    run_dir.mkdir(parents=True, exist_ok=False)
    diff = make_diff(original, proposed, target.relpath)
    manifest = {
        "run_id": rid,
        "status": "staged",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_path": str(target.path),
        "skill_relpath": target.relpath,
        "original_sha256": sha256_text(original),
        "proposed_sha256": sha256_text(proposed),
        "engine": engine,
        "files": {"original": "original_SKILL.md", "proposed": "proposed_SKILL.md", "diff": "diff.patch", "report": "report.md", "evidence": "evidence.json"},
    }
    write_text(run_dir / "original_SKILL.md", original)
    write_text(run_dir / "proposed_SKILL.md", proposed)
    write_text(run_dir / "diff.patch", diff)
    write_text(run_dir / "evidence.json", json.dumps({"snippets": evidence}, ensure_ascii=False, indent=2) + "\n")
    report = f"# SkillOpt dry run\n\n- run_id: {rid}\n- skill: {target.name}\n- path: {target.path}\n- engine: {engine}\n- original_sha256: {manifest['original_sha256']}\n- proposed_sha256: {manifest['proposed_sha256']}\n- changed: {bool(diff)}\n- evidence_snippets: {len(evidence)}\n\n## Diff preview\n\n```diff\n{diff[:4000]}\n```\n"
    write_text(run_dir / "report.md", report)
    save_manifest(run_dir, manifest)
    return {"success": True, "run_id": rid, "status": "staged", "run_dir": str(run_dir), "skill": target.name, "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "changed": bool(diff)}


def status(hermes_home_path: str | None = None) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    runs = []
    for m in sorted(dirs["staging"].glob("*/manifest.json"), reverse=True)[:20]:
        try:
            d = json.loads(read_text(m))
            runs.append({k: d.get(k) for k in ("run_id", "status", "skill_name", "created_at", "engine")})
        except Exception:
            pass
    return {"success": True, "hermes_home": str(home), "skills_count": len(discover_skills(home)), "staging": str(dirs["staging"]), "backups": str(dirs["backups"]), "recent_runs": runs}


def review(run_id: str, hermes_home_path: str | None = None, include_diff_chars: int = 4000) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    diff = read_text(run_dir / "diff.patch") if (run_dir / "diff.patch").exists() else ""
    report = read_text(run_dir / "report.md") if (run_dir / "report.md").exists() else ""
    return {"success": True, "run_id": run_id, "status": m.get("status"), "skill": m.get("skill_name"), "run_dir": str(run_dir), "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "diff_preview": diff[:include_diff_chars], "report_summary": report[:1200]}


def adopt(run_id: str, hermes_home_path: str | None = None, force: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    target = Path(m["skill_path"])
    if not target.exists():
        raise ValueError(f"target skill missing: {target}")
    current = read_text(target)
    current_sha = sha256_text(current)
    if current_sha != m.get("original_sha256") and not force:
        raise ValueError("Current skill sha does not match staged original; pass force=true to override")
    proposed = read_text(run_dir / "proposed_SKILL.md")
    backup_dir = dirs["backups"] / f"{now_id()}-{run_id}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    write_text(backup_dir / "SKILL.md", current)
    write_text(backup_dir / "manifest.json", json.dumps({"run_id": run_id, "skill_path": str(target), "sha256": current_sha}, ensure_ascii=False, indent=2) + "\n")
    write_text(target, proposed)
    m.update({"status": "adopted", "adopted_at": datetime.now(timezone.utc).isoformat(), "backup_dir": str(backup_dir), "adopted_sha256": sha256_text(proposed)})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "adopted", "skill_path": str(target), "backup_dir": str(backup_dir)}


def rollback(run_id: str, hermes_home_path: str | None = None) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    target = Path(m["skill_path"])
    backup_dir = Path(m.get("backup_dir") or "")
    if backup_dir and (backup_dir / "SKILL.md").exists():
        restored = read_text(backup_dir / "SKILL.md")
    elif (run_dir / "original_SKILL.md").exists():
        restored = read_text(run_dir / "original_SKILL.md")
    else:
        raise ValueError("No backup/original available for rollback")
    write_text(target, restored)
    m.update({"status": "rolled_back", "rolled_back_at": datetime.now(timezone.utc).isoformat(), "rolled_back_sha256": sha256_text(restored)})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "rolled_back", "skill_path": str(target)}


def _git(args: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.strip()


def upstream_status(hermes_home_path: str | None = None, repo_path: str | None = None) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    clone = Path(repo_path).expanduser().resolve() if repo_path else ensure_dirs(home)["upstream"] / "SkillOpt"
    lock = PLUGIN_ROOT / "skillopt_upstream.lock"
    lock_data = {}
    if lock.exists():
        try:
            lock_data = json.loads(read_text(lock))
        except Exception:
            lock_data = {"raw": read_text(lock)}
    exists = (clone / ".git").exists()
    commit = None
    if exists:
        code, out = _git(["rev-parse", "HEAD"], clone)
        commit = out if code == 0 else None
    return {"success": True, "upstream_url": UPSTREAM_URL, "clone_path": str(clone), "clone_exists": exists, "current_commit": commit, "lock": lock_data}


def upstream_update(hermes_home_path: str | None = None, repo_path: str | None = None, fetch_only: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    clone = Path(repo_path).expanduser().resolve() if repo_path else ensure_dirs(home)["upstream"] / "SkillOpt"
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        code, out = _git(["clone", "--depth", "1", UPSTREAM_URL, str(clone)], timeout=300)
        if code != 0:
            raise RuntimeError(out)
    else:
        code, out = _git(["fetch", "origin", "main", "--tags"], clone, timeout=300)
        if code != 0:
            raise RuntimeError(out)
        if not fetch_only:
            _git(["checkout", "main"], clone)
            code, out = _git(["pull", "--ff-only", "origin", "main"], clone, timeout=300)
            if code != 0:
                raise RuntimeError(out)
    code, commit = _git(["rev-parse", "HEAD"], clone)
    if code != 0:
        raise RuntimeError(commit)
    lock_data = {"upstream_url": UPSTREAM_URL, "clone_path": str(clone), "pinned_commit": commit, "updated_at": datetime.now(timezone.utc).isoformat(), "note": "Pinned external upstream only; no plugin code merged automatically."}
    write_text(PLUGIN_ROOT / "skillopt_upstream.lock", json.dumps(lock_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"success": True, **lock_data}
