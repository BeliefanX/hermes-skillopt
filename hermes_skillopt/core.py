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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_skillopt.bounded_edit import apply_bounded_edits, frontmatter_split

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_URL = "https://github.com/microsoft/SkillOpt.git"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:sk|ghp|gho|xox[baprs])-[-A-Za-z0-9_]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b"),
]


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_secrets(text: str) -> str:
    out = text or ""
    out = SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=<REDACTED>", out)
    for pat in SECRET_PATTERNS[1:]:
        out = pat.sub("<REDACTED>", out)
    return out


def hermes_home(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(raw).expanduser().resolve()


def active_hermes_home() -> Path:
    """Return the active runtime Hermes profile home, ignoring per-call overrides."""
    return hermes_home(None)


def guard_writeback_home(home: Path, *, unsafe_cross_profile: bool = False) -> None:
    """Prevent adopt/rollback from writing a non-active profile by default."""
    active = active_hermes_home()
    if home == active:
        return
    if unsafe_cross_profile:
        return
    raise ValueError(
        "Refusing live skill writeback outside the active Hermes profile home; "
        f"requested {home}, active {active}. Set HERMES_HOME to the target profile "
        "or use the explicit unsafe cross-profile CLI confirmation for offline maintenance."
    )


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
    for line in text[4:end].splitlines():
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
    home = home.resolve()
    skills_dir = (home / "skills").resolve()
    if not skills_dir.exists():
        return []
    found: list[Skill] = []
    for p in sorted(skills_dir.rglob("SKILL.md")):
        resolved = p.resolve()
        if p.is_symlink() or not _is_relative_to(resolved, skills_dir):
            raise ValueError(f"Skill path escapes HERMES_HOME/skills: {p}")
        try:
            text = read_text(resolved)
        except UnicodeDecodeError:
            continue
        name = parse_frontmatter_name(text) or p.parent.name
        found.append(Skill(name=name, path=resolved, relpath=str(resolved.relative_to(home)), sha256=sha256_text(text)))
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
    return frontmatter_split(text)


def make_diff(original: str, proposed: str, relpath: str) -> str:
    return "".join(difflib.unified_diff(original.splitlines(True), proposed.splitlines(True), fromfile=f"a/{relpath}", tofile=f"b/{relpath}"))


def load_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads(read_text(run_dir / "manifest.json"))


def save_manifest(run_dir: Path, data: dict[str, Any]) -> None:
    write_text(run_dir / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_hashes(run_dir: Path, files: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, rel in files.items():
        if key == "manifest":
            continue
        p = (run_dir / rel).resolve()
        if not _is_relative_to(p, run_dir.resolve()) or not p.is_file() or p.is_symlink():
            continue
        out[key] = sha256_file(p)
    return out


def verify_artifact_hashes(run_dir: Path, manifest: dict[str, Any]) -> None:
    expected = manifest.get("artifact_sha256") or {}
    files = manifest.get("files") or {}
    if not expected:
        raise ValueError("Manifest missing artifact_sha256 integrity map")
    if not isinstance(expected, dict) or not isinstance(files, dict):
        raise ValueError("Manifest artifact integrity fields are invalid")
    for key, digest in expected.items():
        rel = files.get(key)
        if not isinstance(rel, str) or not isinstance(digest, str):
            raise ValueError("Manifest artifact integrity entry is invalid")
        p = (run_dir / rel).resolve()
        if not _is_relative_to(p, run_dir.resolve()) or p.is_symlink() or not p.is_file():
            raise ValueError(f"Artifact {key} is missing or unsafe")
        if sha256_file(p) != digest:
            if key == "proposed":
                raise ValueError("Staged proposed_SKILL.md sha does not match manifest artifact_sha256; refusing to adopt")
            raise ValueError(f"Artifact hash mismatch for {key}")


def _artifact_path(run_dir: Path, manifest: dict[str, Any], key: str) -> Path:
    files = manifest.get("files") or {}
    hashes = manifest.get("artifact_sha256") or {}
    rel = files.get(key)
    if not isinstance(rel, str) or key not in hashes:
        raise ValueError(f"Manifest missing required hashed artifact: {key}")
    p = (run_dir / rel).resolve()
    if not _is_relative_to(p, run_dir.resolve()) or p.is_symlink() or not p.is_file():
        raise ValueError(f"Artifact {key} is missing or unsafe")
    return p


def _read_hashed_json(run_dir: Path, manifest: dict[str, Any], key: str) -> Any:
    return json.loads(read_text(_artifact_path(run_dir, manifest, key)))


def _read_hashed_jsonl(run_dir: Path, manifest: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in read_text(_artifact_path(run_dir, manifest, key)).splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _manifest_equal(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        label = "production eligible" if field == "production_gate_eligible" else field
        raise ValueError(f"Manifest {label} does not match verified artifacts; refusing to adopt")


def _adopt_time_artifact_crosscheck(run_dir: Path, manifest: dict[str, Any]) -> None:
    """Re-derive adopt eligibility from hashed artifacts, not mutable manifest fields."""

    required = ("gate_results", "test_results", "val", "test", "candidate_summary", "evidence", "proposed")
    for key in required:
        _artifact_path(run_dir, manifest, key)

    gate_results = _read_hashed_json(run_dir, manifest, "gate_results")
    test_results = _read_hashed_json(run_dir, manifest, "test_results")
    val_items = _read_hashed_jsonl(run_dir, manifest, "val")
    test_items = _read_hashed_jsonl(run_dir, manifest, "test")
    candidate_summary_artifact = _read_hashed_json(run_dir, manifest, "candidate_summary")
    evidence = _read_hashed_json(run_dir, manifest, "evidence")

    best_gate = gate_results.get("best_gate") if isinstance(gate_results, dict) else None
    production_best_gate = gate_results.get("production_best_gate") if isinstance(gate_results, dict) else None
    _manifest_equal(best_gate, manifest.get("gate"), "gate")
    _manifest_equal(production_best_gate, manifest.get("production_gate"), "production_gate")
    _manifest_equal(test_results, manifest.get("test_results"), "test_results")
    _manifest_equal(candidate_summary_artifact.get("rounds") if isinstance(candidate_summary_artifact, dict) else None, manifest.get("candidate_summary"), "candidate_summary")

    from hermes_skillopt.env import EvalTask, is_production_gate_task

    def task_from_row(row: dict[str, Any]) -> EvalTask:
        return EvalTask(
            id=str(row.get("id", "")),
            prompt=str(row.get("prompt", "")),
            source=str(row.get("source", "")),
            expected_behavior=str(row.get("expected_behavior", "")),
            assertions=tuple(row.get("assertions") or ()),
            judge=str(row.get("judge", "keyword_scorecard")),
            allowed_tools=tuple(row.get("allowed_tools") or ()),
            timeout=float(row.get("timeout", 30.0)),
            fixtures=dict(row.get("fixtures") or {}),
            expected_terms=tuple(row.get("expected_terms") or ()),
            failure_terms=tuple(row.get("failure_terms") or ()),
            required_markers=tuple(row.get("required_markers") or ()),
            forbidden_markers=tuple(row.get("forbidden_markers") or ()),
            split=str(row.get("split", "validation")),
            weight=float(row.get("weight", 1.0)),
            success_criteria=tuple(row.get("success_criteria") or ()),
            metadata=dict(row.get("metadata") or {}),
        )

    task_rows = {
        "train": _read_hashed_jsonl(run_dir, manifest, "train") if "train" in (manifest.get("artifact_sha256") or {}) else [],
        "val": val_items,
        "test": test_items,
    }
    eval_tasks = {split: [task_from_row(r) for r in rows] for split, rows in task_rows.items()}
    production_val_tasks = [t for t in eval_tasks["val"] if is_production_gate_task(t)]
    production_gate_available = bool(production_val_tasks) and bool(evidence.get("production_gate_eligible"))
    recomputed_production_gate_eligible = production_gate_available and bool(production_best_gate and production_best_gate.get("accepted") is True)
    production_test_results = [r for r in (test_results.get("results") or []) if isinstance(r, dict) and isinstance(r.get("metadata"), dict) and r["metadata"].get("production_gate_eligible")]
    recomputed_test_gate_eligible = bool(production_test_results) and all(float(r.get("score", 0.0)) >= 0.55 and bool(r.get("passed")) for r in production_test_results)
    recomputed_policy = _production_eval_policy(evidence, production_gate_available, recomputed_test_gate_eligible)
    recomputed_provenance = _provenance_fingerprint(
        eval_file_used=evidence.get("eval_file"),
        tasks=eval_tasks,
        backend_mode=str(manifest.get("backend")),
        target_executor_mode=str(manifest.get("target_executor")),
        target_config_id=str(manifest.get("target_config_id")),
        production_gate_available=production_gate_available,
    )

    _manifest_equal(recomputed_production_gate_eligible, manifest.get("production_gate_eligible"), "production_gate_eligible")
    _manifest_equal(recomputed_test_gate_eligible, manifest.get("test_gate_eligible"), "test_gate_eligible")
    _manifest_equal(recomputed_policy, manifest.get("production_eval_policy"), "production_eval_policy")
    _manifest_equal(recomputed_provenance, manifest.get("provenance_fingerprint"), "provenance_fingerprint")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_run_id(run_id: str) -> None:
    if not run_id or not RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise ValueError("Invalid run_id")


def resolve_run_dir(home: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    staging = ensure_dirs(home)["staging"].resolve()
    p = (staging / run_id).resolve()
    if not _is_relative_to(p, staging):
        raise ValueError("Invalid run_id path")
    if not p.exists():
        raise ValueError(f"run_id not found: {run_id}")
    return p


def manifest_skill_path(home: Path, manifest: dict[str, Any]) -> Path:
    manifest_home = manifest.get("hermes_home")
    if manifest_home is not None and manifest_home != str(home):
        raise ValueError("Manifest hermes_home does not match current home")
    relpath = manifest.get("skill_relpath")
    if not isinstance(relpath, str) or not relpath:
        raise ValueError("Manifest missing skill_relpath")
    target = (home / relpath).resolve()
    skills_dir = (home / "skills").resolve()
    if not _is_relative_to(target, skills_dir):
        raise ValueError("Manifest skill_relpath escapes skills directory")
    return target


def guard_manifest_skill_path(home: Path, manifest: dict[str, Any], target: Path) -> None:
    raw = manifest.get("skill_path")
    if raw is None:
        return
    try:
        recorded = Path(str(raw)).expanduser().resolve()
    except Exception as exc:
        raise ValueError("Invalid manifest skill_path") from exc
    if recorded != target:
        raise ValueError("Manifest skill_path does not match skill_relpath target")


def resolve_backup_skill_path(home: Path, manifest: dict[str, Any], run_id: str, target: Path) -> Path:
    if manifest.get("run_id") != run_id:
        raise ValueError("Staging manifest run_id does not match rollback run_id; refusing to rollback")
    raw = manifest.get("backup_dir")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Manifest missing backup_dir; refusing to rollback")
    backups_root = ensure_dirs(home)["backups"].resolve()
    try:
        backup_dir = Path(raw).expanduser().resolve()
    except Exception as exc:
        raise ValueError("Invalid backup_dir; refusing to rollback") from exc
    if not _is_relative_to(backup_dir, backups_root):
        raise ValueError("Manifest backup_dir escapes skillopt backups directory; refusing to rollback")
    if not backup_dir.is_dir():
        raise ValueError("Manifest backup_dir does not exist; refusing to rollback")

    backup_skill = (backup_dir / "SKILL.md").resolve()
    if not _is_relative_to(backup_skill, backup_dir):
        raise ValueError("Backup SKILL.md escapes backup_dir; refusing to rollback")
    if not backup_skill.is_file():
        raise ValueError("Backup SKILL.md missing; refusing to rollback")

    restored = read_text(backup_skill)
    restored_sha = sha256_text(restored)

    backup_manifest_path = (backup_dir / "manifest.json").resolve()
    if not backup_manifest_path.exists():
        raise ValueError("Backup manifest missing; refusing to rollback")
    if not _is_relative_to(backup_manifest_path, backup_dir) or not backup_manifest_path.is_file():
        raise ValueError("Backup manifest escapes backup_dir; refusing to rollback")
    backup_manifest = json.loads(read_text(backup_manifest_path))
    if backup_manifest.get("run_id") != run_id or backup_manifest.get("run_id") != manifest.get("run_id"):
        raise ValueError("Backup manifest run_id does not match staging manifest; refusing to rollback")
    backup_skill_path = backup_manifest.get("skill_path")
    if backup_skill_path is None or Path(str(backup_skill_path)).expanduser().resolve() != target:
        raise ValueError("Backup manifest skill_path does not match target; refusing to rollback")
    backup_relpath = backup_manifest.get("skill_relpath")
    if backup_relpath is not None and backup_relpath != manifest.get("skill_relpath"):
        raise ValueError("Backup manifest skill_relpath does not match staging manifest; refusing to rollback")
    backup_original_sha = backup_manifest.get("original_sha256")
    if not backup_original_sha:
        raise ValueError("Backup manifest missing original_sha256; refusing to rollback")
    if backup_original_sha != restored_sha or backup_manifest.get("sha256") != restored_sha:
        raise ValueError("Backup manifest sha does not match SKILL.md; refusing to rollback")
    if backup_original_sha != manifest.get("original_sha256"):
        raise ValueError("Backup manifest original_sha256 does not match staging manifest; refusing to rollback")
    expected_adopted_sha = manifest.get("adopted_sha256") or manifest.get("proposed_sha256")
    for sha_key in ("proposed_sha256", "adopted_sha256"):
        backup_sha = backup_manifest.get(sha_key)
        if backup_sha is None:
            continue
        if backup_sha != manifest.get(sha_key):
            raise ValueError("Backup manifest adopted/proposed sha does not match staging manifest; refusing to rollback")
        if expected_adopted_sha is not None and backup_sha != expected_adopted_sha:
            raise ValueError("Backup manifest adopted/proposed sha does not match current rollback guard; refusing to rollback")
    return backup_skill


# ---------------- Hermes-native full SkillOpt cycle ----------------

def _jsonl_write(path: Path, rows: list[dict[str, Any]]) -> None:
    write_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def _jsonl_read(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-max(0, int(limit)):]


def load_rejected_edit_history(home: Path, skill_name: str, limit: int = 20) -> list[dict[str, Any]]:
    staging = home / "skillopt" / "staging"
    if not staging.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(staging.glob("*/rejected_edits.jsonl"), key=lambda p: p.stat().st_mtime):
        try:
            manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("skill_name") != skill_name:
                continue
        except Exception:
            continue
        for row in _jsonl_read(path, limit=limit):
            rows.append({"run_id": path.parent.name, **row})
    return rows[-max(0, int(limit)):]


def _safe_sql_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("unsafe sqlite identifier")
    return '"' + name + '"'


def _row_timestamp(row: dict[str, Any]) -> float | None:
    for k, v in row.items():
        lk = k.lower()
        if any(x in lk for x in ("time", "created", "updated", "timestamp", "date")) and v is not None:
            try:
                f = float(v)
                if f > 1e12:
                    f /= 1000.0
                if f > 1e9:
                    return f
            except Exception:
                s = str(v).strip().replace("Z", "+00:00")
                try:
                    return datetime.fromisoformat(s).timestamp()
                except Exception:
                    pass
    return None


def harvest_sessions(home: Path, skill: Skill, query: str | None = None, lookback_days: int = 14, limit: int = 50) -> list[dict[str, Any]]:
    """Harvest recent redacted Hermes session/message fragments from state.db and session files."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(lookback_days)))).timestamp()
    terms = [t.lower() for t in [query, skill.name, skill.path.parent.name] if t]
    out: list[dict[str, Any]] = []

    def maybe_add(source: str, text: str, meta: dict[str, Any] | None = None) -> None:
        if len(out) >= limit or not text:
            return
        flat = redact_secrets(re.sub(r"\s+", " ", text))[:4000]
        hay = flat.lower()
        if terms and not any(t in hay for t in terms):
            return
        out.append({"id": f"h{len(out)+1}", "source": source, "text": flat, "meta": meta or {}})

    db = home / "state.db"
    if db.exists():
        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                qtable = _safe_sql_ident(table)
                cols = [r[1] for r in cur.execute(f"PRAGMA table_info({qtable})").fetchall()]
                text_cols = [c for c in cols if any(k in c.lower() for k in ("content", "message", "text", "prompt", "response", "goal", "error", "tool"))]
                if not text_cols:
                    continue
                select_cols = list(dict.fromkeys(text_cols + [c for c in cols if any(k in c.lower() for k in ("time", "created", "updated", "role", "session"))]))
                rows = cur.execute(f"SELECT {', '.join(_safe_sql_ident(c) for c in select_cols)} FROM {qtable} ORDER BY rowid DESC LIMIT ?", (max(limit * 10, 100),)).fetchall()
                for r in rows:
                    d = dict(r)
                    ts = _row_timestamp(d)
                    if ts is not None and ts < cutoff:
                        continue
                    text = "\n".join(str(d[c]) for c in text_cols if d.get(c))
                    maybe_add(f"state.db:{table}", text, {k: redact_secrets(str(v))[:500] for k, v in d.items() if k not in text_cols and v is not None})
                    if len(out) >= limit:
                        return out
        except Exception as exc:
            out.append({"id": "harvest-warning", "source": "state.db", "text": f"harvest warning: {type(exc).__name__}: {redact_secrets(str(exc))}", "meta": {"warning": True}})
        finally:
            try:
                con.close()  # type: ignore[name-defined]
            except Exception:
                pass
    for root in [home / "sessions", home / "logs"]:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
            if len(out) >= limit:
                return out
            if not p.is_file() or p.suffix.lower() not in (".json", ".jsonl", ".txt", ".md", ".log"):
                continue
            if p.stat().st_mtime < cutoff:
                continue
            try:
                maybe_add(str(p.relative_to(home)), read_text(p)[:6000], {"mtime": p.stat().st_mtime})
            except Exception:
                pass
    return out


def evidence_from_state(home: Path, query: str | None = None, limit: int = 6) -> list[str]:
    dummy = Skill(name=query or "skill", path=home / "skills" / (query or "skill") / "SKILL.md", relpath="", sha256="")
    return [x["text"] for x in harvest_sessions(home, dummy, query=query, limit=limit)]


def mine_items(snippets: list[dict[str, Any]], skill: Skill, query: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for i, snip in enumerate(snippets, 1):
        text = snip["text"]
        lower = text.lower()
        tools = sorted(set(re.findall(r"\b(?:tool|function|terminal|pytest|error|traceback|exception|failed|success)\b", lower)))
        failure = any(w in lower for w in ("error", "traceback", "failed", "exception", "wrong", "bug", "timeout"))
        success = any(w in lower for w in ("success", "passed", "done", "fixed", "works", "verified")) and not failure
        items.append({
            "id": f"item-{i:04d}",
            "source_id": snip.get("id"),
            "user_goal": (query or skill.name),
            "assistant_outcome": "failure_or_gap" if failure else ("success" if success else "unknown"),
            "evidence": text[:2000],
            "tools_errors": tools,
            "skill_relevance": 1.0 if skill.name.lower() in lower or (query and query.lower() in lower) else 0.6,
            "success_hints": ["verified outcome"] if success else [],
            "failure_hints": ["tool/error/verification gap present"] if failure else [],
        })
    if not items:
        items.append({"id": "item-0001", "source_id": None, "user_goal": query or skill.name, "assistant_outcome": "unknown", "evidence": "No recent matching Hermes sessions were found; optimize from skill text only.", "tools_errors": [], "skill_relevance": 0.2, "success_hints": [], "failure_hints": ["missing harvested evidence"]})
    return items


def split_items(items: list[dict[str, Any]], test: bool = True) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(items, key=lambda x: sha256_text(str(x.get("id", "")) + str(x.get("evidence", ""))))
    buckets = {"train": [], "val": [], "test": []}
    for idx, item in enumerate(ordered):
        mod = idx % (5 if test else 4)
        if mod in (0, 1, 2):
            buckets["train"].append(item)
        elif mod == 3:
            buckets["val"].append(item)
        else:
            buckets["test"].append(item)
    if not buckets["val"] and buckets["train"]:
        buckets["val"].append(buckets["train"].pop())
    return buckets


class LLMBackend:
    def __init__(self, backend: str = "auto", allow_mock: bool = False, ctx: Any = None):
        self.backend = backend or "auto"
        self.allow_mock = allow_mock
        self.ctx = ctx
        self.mode = "mock" if self.backend == "mock" else "hermes"
        if self.backend in ("auto", "hermes"):
            llm = getattr(ctx, "llm", None) if ctx is not None else None
            if llm is not None and (getattr(llm, "complete_structured", None) or getattr(llm, "complete", None)):
                self.llm = llm
                self.mode = "hermes"
                return
            if self.backend == "hermes" or not allow_mock:
                raise RuntimeError("Hermes LLM ctx unavailable. Use backend='mock' explicitly or backend='auto' with allow_mock=true for tests/smoke.")
        self.llm = None
        self.mode = "mock"

    def json(self, prompt: str, schema_hint: dict[str, Any], repair_path: Path | None = None) -> dict[str, Any]:
        prompt = redact_secrets(prompt)
        if self.mode == "mock":
            return self._mock(prompt, schema_hint)
        if getattr(self.llm, "complete_structured", None):
            res = self.llm.complete_structured(prompt=prompt, schema=schema_hint)
            if isinstance(res, dict):
                return res
            if hasattr(res, "model_dump"):
                return res.model_dump()
        raw = self.llm.complete(prompt + "\nReturn strict JSON only.")
        text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
        try:
            return json.loads(text)
        except Exception as first:
            m = re.search(r"\{.*\}", text, flags=re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
            if repair_path:
                write_text(repair_path, json.dumps({"error": str(first), "raw_redacted": redact_secrets(text)[:8000]}, ensure_ascii=False, indent=2) + "\n")
            raise ValueError("LLM JSON parse failed") from first

    def _mock(self, prompt: str, schema_hint: dict[str, Any]) -> dict[str, Any]:
        kind = schema_hint.get("kind")
        if kind == "reflect":
            return {"recurring_defects": ["insufficient verification after edits", "unclear handling of tool errors"], "missing_rules": ["state expected artifacts and run tests before final"], "over_broad_rules": [], "verification_gaps": ["no train/val/test gate mentioned"], "reasoning": "mock reflection over redacted harvested items"}
        if kind == "edit":
            return {"edits": [{"op": "append", "text": "\n\n## SkillOpt Learned Rules\n\n- Verify changes with the most relevant command or test before reporting completion.\n- Preserve safety/path guards, backup/rollback reversibility, and avoid mutating real Hermes profile state during smoke tests.\n- When tool errors occur, summarize the blocker and try a bounded alternate path.\n"}], "reasoning": "bounded append based on recurring verification and safety gaps"}
        if kind == "gate":
            return {"current_score": 0.45, "candidate_score": 0.82, "accepted": True, "rationale": "candidate adds concrete verification and safety rules"}
        return {}


def reflect_items(backend: LLMBackend, train: list[dict[str, Any]], current_skill: str, run_dir: Path, iteration: int) -> dict[str, Any]:
    prompt = "Reflect on Hermes skill performance. Redacted train items:\n" + json.dumps(train, ensure_ascii=False)[:12000] + "\nCurrent skill:\n" + current_skill[:8000]
    data = backend.json(prompt, {"kind": "reflect"}, run_dir / f"llm_reflect_repair_{iteration}.json")
    return data


def bounded_edits(backend: LLMBackend, reflections: dict[str, Any], current_skill: str, edit_budget: int, run_dir: Path, iteration: int) -> dict[str, Any]:
    prompt = "Generate bounded edits for SKILL.md. Allowed ops: append, replace, delete, insert_after. Do not edit YAML frontmatter. Max edits: %d. Reflections:\n%s\nSkill:\n%s" % (edit_budget, json.dumps(reflections, ensure_ascii=False), current_skill[:12000])
    data = backend.json(prompt, {"kind": "edit"}, run_dir / f"llm_edit_repair_{iteration}.json")
    edits = data.get("edits") if isinstance(data, dict) else []
    if not isinstance(edits, list):
        edits = []
    data["edits"] = edits[: max(0, int(edit_budget))]
    return data


def _stable_json_sha(data: Any) -> str:
    return sha256_text(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))


def _task_fingerprint(tasks: dict[str, list[Any]]) -> dict[str, Any]:
    rows = []
    for split, split_tasks in sorted(tasks.items()):
        for task in split_tasks:
            rows.append({
                "id": getattr(task, "id", None),
                "split": split,
                "source": getattr(task, "source", None),
                "expected_terms": list(getattr(task, "expected_terms", ())),
                "assertions": list(getattr(task, "assertions", ())),
                "required_markers": list(getattr(task, "required_markers", ())),
                "forbidden_markers": list(getattr(task, "forbidden_markers", ())),
                "weight": getattr(task, "weight", None),
                "production_gate_eligible": bool(getattr(task, "metadata", {}).get("production_gate_eligible")),
            })
    return {"sha256": _stable_json_sha(rows), "task_ids": [r["id"] for r in rows], "task_count": len(rows)}


def _provenance_fingerprint(*, eval_file_used: str | None, tasks: dict[str, list[Any]], backend_mode: str, target_executor_mode: str, target_config_id: str, production_gate_available: bool) -> dict[str, Any]:
    eval_sha = sha256_file(Path(eval_file_used)) if eval_file_used and Path(eval_file_used).is_file() else None
    task_fp = _task_fingerprint(tasks)
    payload = {
        "eval_file": eval_file_used,
        "eval_file_sha256": eval_sha,
        "task_sha256": task_fp["sha256"],
        "backend": backend_mode,
        "target_executor": target_executor_mode,
        "target_config_id": target_config_id,
        "production_gate_available": production_gate_available,
    }
    return {**payload, "fingerprint_sha256": _stable_json_sha(payload), "task_ids": task_fp["task_ids"], "task_count": task_fp["task_count"]}


def _per_task_delta(current_eval: dict[str, Any] | None, candidate_eval: dict[str, Any] | None) -> list[dict[str, Any]]:
    current_rows = {r.get("task_id"): r for r in ((current_eval or {}).get("results") or []) if isinstance(r, dict)}
    out = []
    for cand in ((candidate_eval or {}).get("results") or []):
        if not isinstance(cand, dict):
            continue
        cur = current_rows.get(cand.get("task_id"), {})
        out.append({
            "task_id": cand.get("task_id"),
            "current_score": cur.get("score"),
            "candidate_score": cand.get("score"),
            "delta": round(float(cand.get("score", 0.0)) - float(cur.get("score", 0.0)), 6),
            "current_passed": cur.get("passed"),
            "candidate_passed": cand.get("passed"),
            "production_gate_eligible": bool((cand.get("metadata") or {}).get("production_gate_eligible")),
        })
    return out


def _production_eval_policy(evidence: dict[str, Any], production_gate_available: bool, test_gate_eligible: bool) -> dict[str, Any]:
    return {
        "policy_version": "production-eval-schema-v1",
        "adopt_requires": [
            "eval_file under active HERMES_HOME with hash/path guard",
            "explicit curated validation task with concrete scorecard/assertions",
            "strict candidate improvement on frozen target executor",
            "held-out production-eligible test split passes threshold",
            "fallback/session/synthetic tasks are review-only and cannot authorize adopt",
        ],
        "eval_file": evidence.get("eval_file"),
        "curated_task_count": evidence.get("curated_task_count", 0),
        "production_gate_available": production_gate_available,
        "test_gate_eligible": test_gate_eligible,
    }


def full_run(skill: str | None = None, query: str | None = None, lookback_days: int = 14, limit: int = 50, iterations: int = 1, edit_budget: int = 3, candidate_count: int = 1, backend: str = "auto", allow_mock: bool = False, auto_adopt: bool = False, force: bool = False, hermes_home_path: str | None = None, ctx: Any = None, dry_run: bool = False, eval_file: str | None = None, target_executor: str = "auto") -> dict[str, Any]:
    """Run the Hermes adapter of the SkillOpt core abstraction.

    Pipeline: load trainable SkillState -> build benchmark tasks -> evaluate
    current with a frozen TargetExecutor -> optimizer reflects/proposes bounded
    edits -> evaluate candidate on held-out validation -> ValidationGate accepts
    only strict score improvements -> stage artifacts for user review/adopt.
    """
    if force and auto_adopt:
        raise ValueError("auto_adopt cannot be combined with force")
    if dry_run:
        raise ValueError("full_run(dry_run=True) is no longer supported; use dry_run/legacy mode for review-only proposals")
    if auto_adopt:
        raise ValueError("auto_adopt is disabled in production; run full-run, review artifacts, then call adopt explicitly")
    from hermes_skillopt.env import HermesSkillEnv, is_production_gate_task
    from hermes_skillopt.gate import ValidationGate
    from hermes_skillopt.optimizer import OptimizerBackend
    from hermes_skillopt.state import SkillOptArtifacts, SkillState
    from hermes_skillopt.target import DeterministicKeywordScorecard, HermesRolloutRunner, HermesSandboxRunner, TargetExecutor
    from hermes_skillopt.trainer import SixStageSkillOptTrainer

    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    target = find_skill(home, skill)
    original = read_text(target.path)
    state = SkillState(name=target.name, path=target.path, relpath=target.relpath, text=original, sha256=sha256_text(original), hermes_home=home)
    rid = now_id() + "-" + target.name.replace("/", "-")
    run_dir = dirs["staging"] / rid
    run_dir.mkdir(parents=True, exist_ok=False)
    artifacts = SkillOptArtifacts.for_run(rid, run_dir)
    llm = LLMBackend(backend=backend, allow_mock=allow_mock, ctx=ctx)
    env = HermesSkillEnv(state, query=query, lookback_days=lookback_days, limit=limit, eval_file=eval_file)
    tasks, evidence = env.build_tasks()
    production_val_tasks = [t for t in tasks["val"] if is_production_gate_task(t)]
    production_gate_available = bool(production_val_tasks) and bool(evidence.get("production_gate_eligible"))
    evidence["production_gate_task_count"] = len(production_val_tasks)
    evidence["production_gate_eligible"] = production_gate_available
    if target_executor == "sandbox":
        runner = HermesSandboxRunner()
    elif target_executor == "scorecard":
        runner = DeterministicKeywordScorecard()
    else:
        runner = HermesSandboxRunner() if any((t.metadata.get("executor") == "sandbox" or t.judge == "hermes_sandbox") for split_tasks in tasks.values() for t in split_tasks) else HermesRolloutRunner()
    executor = TargetExecutor(runner=runner)
    optimizer = OptimizerBackend(llm, edit_budget=edit_budget)
    gatekeeper = ValidationGate()
    trainer = SixStageSkillOptTrainer(executor, optimizer, gatekeeper, llm, artifacts, run_dir)

    rejected_history = load_rejected_edit_history(home, target.name)
    current = original

    write_text(artifacts.original, original)
    write_text(artifacts.current, current)
    write_text(artifacts.evidence, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(artifacts.train, [t.__dict__ for t in tasks["train"]])
    _jsonl_write(artifacts.val, [t.__dict__ for t in tasks["val"]])
    _jsonl_write(artifacts.test, [t.__dict__ for t in tasks["test"]])

    trainer_result = trainer.run(original, tasks, iterations, production_val_tasks=production_val_tasks if production_gate_available else [], rejected_history=rejected_history, candidate_count=candidate_count)
    current = trainer_result.current
    best = trainer_result.best
    best_gate = trainer_result.best_gate
    all_reflections = trainer_result.all_reflections
    all_edits = trainer_result.all_edits
    all_gates = trainer_result.all_gates
    all_production_gates = trainer_result.all_production_gates
    candidate_summary = trainer_result.candidate_summary
    rejected = trainer_result.rejected

    final_status = "staged_best" if best != original else "rejected"
    eval_file_used = evidence.get("eval_file")
    validation_summary = best_gate or (all_gates[-1] if all_gates else None)
    production_validation_summary = trainer_result.production_best_gate
    test_results = trainer_result.test_results
    production_test_results = [r for r in (test_results.get("results") or []) if isinstance(r, dict) and isinstance(r.get("metadata"), dict) and r["metadata"].get("production_gate_eligible")]
    test_gate_eligible = bool(production_test_results) and all(float(r.get("score", 0.0)) >= 0.55 and bool(r.get("passed")) for r in production_test_results)
    production_gate_eligible = production_gate_available and bool(
        production_validation_summary.get("accepted") if production_validation_summary else False
    )
    adoptable = final_status == "staged_best" and production_gate_eligible and test_gate_eligible
    adoptability_reasons = []
    if final_status != "staged_best":
        adoptability_reasons.append("no staged_best candidate")
    if not production_gate_eligible:
        adoptability_reasons.append("missing accepted explicit curated production validation gate")
    if not test_gate_eligible:
        adoptability_reasons.append("held-out test split is missing, non-production, or below threshold")
    proposed = best if final_status == "staged_best" else original
    diff = make_diff(original, proposed, target.relpath)
    if final_status == "staged_best":
        write_text(artifacts.best, best)
    write_text(artifacts.proposed, proposed)
    write_text(artifacts.diff, diff)
    write_text(artifacts.reflections, json.dumps(all_reflections, ensure_ascii=False, indent=2) + "\n")
    write_text(artifacts.candidate_edits, json.dumps(all_edits, ensure_ascii=False, indent=2) + "\n")
    write_text(artifacts.gate_results, json.dumps({"gates": all_gates, "best_gate": best_gate, "production_gates": all_production_gates, "production_best_gate": production_validation_summary}, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(artifacts.rejected_edits, rejected)

    current_score = validation_summary.get("current_score") if validation_summary else None
    candidate_score = validation_summary.get("candidate_score") if validation_summary else None
    production_current_score = production_validation_summary.get("current_score") if production_validation_summary else None
    production_candidate_score = production_validation_summary.get("candidate_score") if production_validation_summary else None
    gate_reason = validation_summary.get("rationale") if validation_summary else "none"
    per_task_delta = _per_task_delta((best_gate or {}).get("current_eval") if isinstance(best_gate, dict) else None, (best_gate or {}).get("candidate_eval") if isinstance(best_gate, dict) else None)
    provenance = _provenance_fingerprint(eval_file_used=eval_file_used, tasks=tasks, backend_mode=llm.mode, target_executor_mode=executor.mode, target_config_id=executor.target_config_id, production_gate_available=production_gate_available)
    production_eval_policy = _production_eval_policy(evidence, production_gate_available, test_gate_eligible)
    write_text(artifacts.candidate_summary, json.dumps({"candidate_count": max(1, int(candidate_count)), "rounds": candidate_summary, "per_task_delta": per_task_delta, "selection_policy": "rank candidates on same validation set; when production gates exist, prefer candidates with both generic and production strict improvement"}, ensure_ascii=False, indent=2) + "\n")
    report = f"# Hermes SkillOpt full run\n\n- abstraction: SkillOpt-inspired Hermes adapter (trainable skill state, frozen scorecard/replay target, optimizer bounded edit, benchmark env, validation gate)\n- run_id: {rid}\n- status: {final_status}\n- adoptable: {adoptable}\n- skill: {target.name}\n- backend: {llm.mode}\n- target_executor: {executor.mode}\n- target_config_id: {executor.target_config_id}\n- eval_file: {eval_file_used or 'none'}\n- provenance_fingerprint: {provenance['fingerprint_sha256']}\n- eval_fingerprint: {provenance.get('eval_file_sha256') or 'none'}\n- task_fingerprint: {provenance['task_sha256']}\n- production_eval_policy: {production_eval_policy['policy_version']}\n- curated_task_count: {evidence.get('curated_task_count', 0)}\n- production_gate_eligible: {production_gate_eligible}\n- production_gate_task_count: {len(production_val_tasks)}\n- harvested_fragments: {len(evidence.get('snippets', []))}\n- train/val/test: {len(tasks['train'])}/{len(tasks['val'])}/{len(tasks['test'])}\n- baseline/current/candidate/best/test: original_sha={sha256_text(original)[:12]}, current_sha={sha256_text(current)[:12]}, candidate_sha={sha256_text(proposed)[:12]}, best_sha={sha256_text(best)[:12]}, test_score={test_results.get('score')}\n- iterations: {max(1, int(iterations))}\n- candidate_count_per_iteration: {max(1, int(candidate_count))}\n- six_stage_trainer_artifacts: stages/NNN_rollout|reflect|aggregate|select|update|evaluate.json\n- rejected_history_count: {len(rejected_history)}\n- validation_scores: current={current_score}, candidate={candidate_score}\n- production_validation_scores: current={production_current_score}, candidate={production_candidate_score}\n- heldout_test_score: {test_results.get('score')}\n- test_gate_eligible: {test_gate_eligible}\n- not_adoptable_reasons: {', '.join(adoptability_reasons) or 'none'}\n- not_adoptable_checklist: production_validation={production_gate_eligible}; heldout_test={test_gate_eligible}; staged_best={final_status == 'staged_best'}\n- gate_reason: {gate_reason}\n- acceptance_gate: candidate production curated validation score must strictly improve for adoptable; session/fallback/synthetic validation is review-only evidence\n- best_gate: {json.dumps(best_gate, ensure_ascii=False) if best_gate else 'none'}\n- production_best_gate: {json.dumps(production_validation_summary, ensure_ascii=False) if production_validation_summary else 'none'}\n- per_task_delta: {json.dumps(per_task_delta, ensure_ascii=False)}\n- changed: {bool(diff)}\n\n## Multi-candidate rank/select\n\n```json\n{json.dumps(candidate_summary, ensure_ascii=False, indent=2)[:4000]}\n```\n\n## Diff preview\n\n```diff\n{diff[:4000]}\n```\n"
    write_text(artifacts.report, report)
    manifest = {
        "run_id": rid,
        "status": final_status,
        "adoptable": adoptable,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_path": str(target.path),
        "skill_relpath": target.relpath,
        "original_sha256": sha256_text(original),
        "proposed_sha256": sha256_text(proposed),
        "engine": "hermes-native-skillopt-core-adapter",
        "backend": llm.mode,
        "target_executor": executor.mode,
        "target_config_id": executor.target_config_id,
        "candidate_count": max(1, int(candidate_count)),
        "rejected_history_count": len(rejected_history),
        "eval_file": eval_file_used,
        "provenance_fingerprint": provenance,
        "production_eval_policy": production_eval_policy,
        "per_task_delta": per_task_delta,
        "candidate_summary": candidate_summary,
        "task_counts": evidence.get("task_counts", {k: len(v) for k, v in tasks.items()}),
        "curated_task_count": evidence.get("curated_task_count", 0),
        "validation_current_score": current_score,
        "validation_candidate_score": candidate_score,
        "production_validation_current_score": production_current_score,
        "production_validation_candidate_score": production_candidate_score,
        "production_gate_eligible": production_gate_eligible,
        "production_gate_task_count": len(production_val_tasks),
        "test_score": test_results.get("score"),
        "test_gate_eligible": test_gate_eligible,
        "production_eligibility_reasons": adoptability_reasons,
        "gate_reason": gate_reason,
        "core_abstraction": {
            "skill_document": "trainable_state",
            "target_agent_model": "frozen_scorecard_replay_executor",
            "optimizer_model": "reflection_plus_bounded_edit",
            "environment_benchmark": "hermes_curated_replay_session_synthetic_tasks",
            "validation_gate": "sole_acceptance_gate_candidate_score_gt_current_score_plus_curated_production_and_test_eligibility",
            "hermes_outer_shell": "staged_safety_adopt_rollback_profile_isolation",
        },
        "gate": best_gate,
        "production_gate": production_validation_summary,
        "test_results": test_results,
        "files": artifacts.manifest_files(include_best=final_status == "staged_best"),
    }
    manifest["artifact_sha256"] = artifact_hashes(run_dir, manifest["files"])
    save_manifest(run_dir, manifest)
    result = {"success": True, "run_id": rid, "status": final_status, "adoptable": adoptable, "production_gate_eligible": production_gate_eligible, "test_gate_eligible": test_gate_eligible, "not_adoptable_reasons": adoptability_reasons, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(artifacts.diff), "report_path": str(artifacts.report), "gate": best_gate, "production_gate": production_validation_summary, "test_results": test_results, "per_task_delta": per_task_delta, "candidate_summary": candidate_summary, "provenance_fingerprint": provenance, "changed": bool(diff), "eval_file": eval_file_used, "task_counts": evidence.get("task_counts", {k: len(v) for k, v in tasks.items()}), "current_score": current_score, "candidate_score": candidate_score, "production_current_score": production_current_score, "production_candidate_score": production_candidate_score, "gate_reason": gate_reason}
    return result


def propose_skill(original: str, evidence: list[str] | None = None, goal: str | None = None, ctx: Any = None, use_llm: bool = False) -> tuple[str, str]:
    fm, body = _frontmatter_split(original)
    bullets = []
    if goal:
        bullets.append(f"- Candidate rule: when this skill is invoked, explicitly check the user's goal: {goal.strip()[:240]}")
    if evidence:
        bullets.append("- Evidence-informed TODO: review recent redacted session snippets and convert recurring successful patterns into stable instructions.")
        for i, snip in enumerate(evidence[:3], 1):
            bullets.append(f"  - Redacted snippet {i}: {snip[:220]}")
    if not bullets:
        bullets = ["- Candidate rule: add a short self-check before final answers: confirm scope, required tools, and safety constraints."]
    appendix = "\n\n## SkillOpt Candidate Improvements (staged)\n\n" + "\n".join(bullets) + "\n"
    return fm + body.rstrip() + appendix, "deterministic-legacy"


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
    manifest = {"run_id": rid, "status": "staged", "adoptable": False, "review_only": True, "created_at": datetime.now(timezone.utc).isoformat(), "hermes_home": str(home), "skill_name": target.name, "skill_path": str(target.path), "skill_relpath": target.relpath, "original_sha256": sha256_text(original), "proposed_sha256": sha256_text(proposed), "engine": engine, "files": {"original": "original_SKILL.md", "proposed": "proposed_SKILL.md", "diff": "diff.patch", "report": "report.md", "evidence": "evidence.json"}}
    write_text(run_dir / "original_SKILL.md", original)
    write_text(run_dir / "proposed_SKILL.md", proposed)
    write_text(run_dir / "diff.patch", diff)
    write_text(run_dir / "evidence.json", json.dumps({"snippets": evidence}, ensure_ascii=False, indent=2) + "\n")
    write_text(run_dir / "report.md", f"# SkillOpt dry run\n\n- run_id: {rid}\n- skill: {target.name}\n- engine: {engine}\n- changed: {bool(diff)}\n\n```diff\n{diff[:4000]}\n```\n")
    manifest["artifact_sha256"] = artifact_hashes(run_dir, manifest["files"])
    save_manifest(run_dir, manifest)
    return {"success": True, "run_id": rid, "status": "staged", "adoptable": False, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "changed": bool(diff)}


def status(hermes_home_path: str | None = None) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    runs = []
    for m in sorted(dirs["staging"].glob("*/manifest.json"), reverse=True)[:20]:
        try:
            d = json.loads(read_text(m))
            runs.append({k: d.get(k) for k in ("run_id", "status", "skill_name", "created_at", "engine", "backend", "adoptable", "production_gate_eligible", "test_gate_eligible", "target_executor")})
        except Exception:
            pass
    return {"success": True, "hermes_home": str(home), "skills_count": len(discover_skills(home)), "staging": str(dirs["staging"]), "backups": str(dirs["backups"]), "recent_runs": runs}


def review(run_id: str, hermes_home_path: str | None = None, include_diff_chars: int = 4000) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, m)
    diff = read_text(run_dir / "diff.patch") if (run_dir / "diff.patch").exists() else ""
    report = read_text(run_dir / "report.md") if (run_dir / "report.md").exists() else ""
    gate = m.get("gate") or (json.loads(read_text(run_dir / "gate_results.json")).get("best_gate") if (run_dir / "gate_results.json").exists() else None)
    return {"success": True, "run_id": run_id, "status": m.get("status"), "adoptable": m.get("adoptable") is True, "production_gate_eligible": m.get("production_gate_eligible") is True, "test_gate_eligible": m.get("test_gate_eligible") is True, "not_adoptable_reasons": m.get("production_eligibility_reasons") or [], "skill": m.get("skill_name"), "gate": gate, "production_gate": m.get("production_gate"), "test_results": m.get("test_results"), "per_task_delta": m.get("per_task_delta") or [], "candidate_summary": m.get("candidate_summary") or [], "provenance_fingerprint": m.get("provenance_fingerprint"), "production_eval_policy": m.get("production_eval_policy"), "accepted": m.get("status") in ("staged_best", "accepted", "adopted"), "artifact_integrity": "verified", "run_dir": str(run_dir), "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "diff_preview": diff[:include_diff_chars], "report_summary": report[:1200]}


def adopt(run_id: str, hermes_home_path: str | None = None, force: bool = False, unsafe_cross_profile: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    guard_writeback_home(home, unsafe_cross_profile=unsafe_cross_profile)
    dirs = ensure_dirs(home)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, m)
    if m.get("status") != "staged_best" or m.get("adoptable") is not True:
        raise ValueError("Only adoptable full-run staged_best manifests may be adopted; legacy/fallback/dry-run proposals are review-only")
    _adopt_time_artifact_crosscheck(run_dir, m)
    gate = m.get("gate")
    if not isinstance(gate, dict) or gate.get("accepted") is not True:
        raise ValueError("Manifest missing accepted validation gate; refusing to adopt")
    if m.get("production_gate_eligible") is not True:
        raise ValueError("Manifest validation gate is not production eligible; refusing to adopt")
    if m.get("test_gate_eligible") is not True:
        raise ValueError("Manifest held-out test gate is not production eligible; refusing to adopt")
    policy = m.get("production_eval_policy")
    provenance = m.get("provenance_fingerprint")
    if not isinstance(policy, dict) or policy.get("policy_version") != "production-eval-schema-v1":
        raise ValueError("Manifest missing production eval schema policy; refusing to adopt")
    if not isinstance(provenance, dict) or not provenance.get("fingerprint_sha256"):
        raise ValueError("Manifest missing provenance fingerprint; refusing to adopt")
    target = manifest_skill_path(home, m)
    guard_manifest_skill_path(home, m, target)
    if not target.exists():
        raise ValueError(f"target skill missing: {target}")
    current = read_text(target)
    current_sha = sha256_text(current)
    if current_sha != m.get("original_sha256") and not force:
        raise ValueError("Current skill sha does not match staged original; pass force=true to override")
    proposed = read_text(run_dir / "proposed_SKILL.md")
    expected_proposed_sha = m.get("proposed_sha256")
    if not expected_proposed_sha:
        raise ValueError("Manifest missing proposed_sha256; refusing to adopt")
    actual_proposed_sha = sha256_text(proposed)
    if actual_proposed_sha != expected_proposed_sha:
        raise ValueError("Staged proposed_SKILL.md sha does not match manifest proposed_sha256; refusing to adopt")
    backup_dir = dirs["backups"] / f"{now_id()}-{run_id}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    proposed_sha = sha256_text(proposed)
    write_text(backup_dir / "SKILL.md", current)
    write_text(backup_dir / "manifest.json", json.dumps({"run_id": run_id, "skill_path": str(target), "skill_relpath": m.get("skill_relpath"), "sha256": current_sha, "original_sha256": current_sha, "proposed_sha256": proposed_sha, "adopted_sha256": proposed_sha}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_text(target, proposed)
    m.update({"status": "adopted", "adopted_at": datetime.now(timezone.utc).isoformat(), "backup_dir": str(backup_dir), "original_sha256": current_sha, "adopted_sha256": proposed_sha})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "adopted", "skill_path": str(target), "backup_dir": str(backup_dir)}


def rollback(run_id: str, hermes_home_path: str | None = None, force: bool = False, unsafe_cross_profile: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    guard_writeback_home(home, unsafe_cross_profile=unsafe_cross_profile)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    verify_artifact_hashes(run_dir, m)
    target = manifest_skill_path(home, m)
    guard_manifest_skill_path(home, m, target)
    expected_sha = m.get("adopted_sha256") or m.get("proposed_sha256")
    if not force:
        if not expected_sha:
            raise ValueError("No adopted sha available for rollback guard; pass force=true to override")
        if not target.exists():
            raise ValueError("Current skill missing; pass force=true to rollback")
        current_sha = sha256_text(read_text(target))
        if current_sha != expected_sha:
            raise ValueError("Current skill sha does not match adopted state; pass force=true to rollback")
    backup_skill = resolve_backup_skill_path(home, m, run_id, target)
    restored = read_text(backup_skill)
    write_text(target, restored)
    m.update({"status": "rolled_back", "rolled_back_at": datetime.now(timezone.utc).isoformat(), "rolled_back_sha256": sha256_text(restored)})
    save_manifest(run_dir, m)
    return {"success": True, "run_id": run_id, "status": "rolled_back", "skill_path": str(target)}


def _git(args: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.strip()


def upstream_status(
    hermes_home_path: str | None = None,
    repo_path: str | None = None,
    *,
    allow_repo_path: bool = False,
) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    canonical = (ensure_dirs(home)["upstream"] / "SkillOpt").resolve()
    if repo_path is not None:
        requested = Path(repo_path).expanduser().resolve()
        if requested != canonical and not allow_repo_path:
            raise ValueError(f"upstream_status repo_path is restricted to canonical clone: {canonical}")
        clone = requested
    else:
        clone = canonical
    lock = PLUGIN_ROOT / "skillopt_upstream.lock"
    lock_data = {}
    if lock.exists():
        try:
            lock_data = json.loads(read_text(lock))
        except Exception:
            lock_data = {"raw": read_text(lock)}
    exists = (clone / ".git").exists()
    commit = None
    upstream_diff = {"semantic_status": "not_cloned", "ahead": None, "behind": None, "dirty": None, "note": "No network is used by status; run upstream-update/fetch-only to refresh the local clone."}
    if exists:
        code, out = _git(["rev-parse", "HEAD"], clone)
        commit = out if code == 0 else None
        code, dirty = _git(["status", "--porcelain"], clone)
        code_ab, ab = _git(["rev-list", "--left-right", "--count", "HEAD...origin/main"], clone)
        ahead = behind = None
        semantic = "local_unknown"
        if code_ab == 0 and ab:
            parts = ab.split()
            if len(parts) >= 2:
                ahead, behind = int(parts[0]), int(parts[1])
                semantic = "up_to_date" if ahead == 0 and behind == 0 else ("behind_origin" if ahead == 0 else "ahead_of_origin" if behind == 0 else "diverged")
        upstream_diff = {"semantic_status": semantic, "ahead": ahead, "behind": behind, "dirty": bool(dirty.strip()) if code == 0 else None, "note": "Compared against locally fetched origin/main only; status does not fetch or require network."}
    return {"success": True, "upstream_url": UPSTREAM_URL, "clone_path": str(clone), "clone_exists": exists, "current_commit": commit, "lock": lock_data, "upstream_diff": upstream_diff}


def upstream_update(hermes_home_path: str | None = None, repo_path: str | None = None, fetch_only: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    canonical = (ensure_dirs(home)["upstream"] / "SkillOpt").resolve()
    if repo_path is not None:
        requested = Path(repo_path).expanduser().resolve()
        if requested != canonical:
            raise ValueError(f"upstream_update repo_path is restricted to canonical clone: {canonical}")
    clone = canonical
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
