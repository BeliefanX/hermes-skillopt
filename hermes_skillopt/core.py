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


def make_diff(original: str, proposed: str, relpath: str) -> str:
    return "".join(difflib.unified_diff(original.splitlines(True), proposed.splitlines(True), fromfile=f"a/{relpath}", tofile=f"b/{relpath}"))


def load_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads(read_text(run_dir / "manifest.json"))


def save_manifest(run_dir: Path, data: dict[str, Any]) -> None:
    write_text(run_dir / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


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
            return {"edits": [{"op": "append", "text": "\n\n## SkillOpt Learned Rules\n\n- Verify changes with the most relevant command or test before reporting completion.\n- Preserve safety/path guards and avoid mutating real Hermes profile state during smoke tests.\n- When tool errors occur, summarize the blocker and try a bounded alternate path.\n"}], "reasoning": "bounded append based on recurring verification and safety gaps"}
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


def _apply_one(body: str, edit: dict[str, Any]) -> str:
    op = edit.get("op")
    if op == "append":
        text = str(edit.get("text", ""))
        heading = None
        m = re.search(r"^##\s+.+$", text.strip(), flags=re.M)
        if m:
            heading = re.escape(m.group(0).strip())
        if heading and re.search(rf"^({heading})$", body, flags=re.M):
            # Replace an existing generated section instead of duplicating it on later iterations.
            return re.sub(rf"\n*{heading}\n.*?(?=\n##\s|\Z)", "\n\n" + text.strip() + "\n", body, count=1, flags=re.S | re.M)
        return body.rstrip() + text + "\n"
    if op == "replace":
        old = str(edit.get("old", ""))
        new = str(edit.get("new", ""))
        if old and old in body:
            return body.replace(old, new, 1)
        return body
    if op == "delete":
        old = str(edit.get("text") or edit.get("old") or "")
        if old and old in body:
            return body.replace(old, "", 1)
        return body
    if op == "insert_after":
        anchor = str(edit.get("anchor", ""))
        text = str(edit.get("text", ""))
        if anchor and anchor in body:
            return body.replace(anchor, anchor + text, 1)
        return body.rstrip() + text + "\n"
    return body


def apply_bounded_edits(current: str, edits: list[dict[str, Any]]) -> str:
    fm, body = _frontmatter_split(current)
    new_body = body
    for edit in edits:
        new_body = _apply_one(new_body, edit)
    return fm + new_body


def full_run(skill: str | None = None, query: str | None = None, lookback_days: int = 14, limit: int = 50, iterations: int = 1, edit_budget: int = 3, backend: str = "auto", allow_mock: bool = False, auto_adopt: bool = False, force: bool = False, hermes_home_path: str | None = None, ctx: Any = None, dry_run: bool = False, eval_file: str | None = None) -> dict[str, Any]:
    """Run the Hermes adapter of the SkillOpt core abstraction.

    Pipeline: load trainable SkillState -> build benchmark tasks -> evaluate
    current with a frozen TargetExecutor -> optimizer reflects/proposes bounded
    edits -> evaluate candidate on held-out validation -> ValidationGate accepts
    only strict score improvements -> stage artifacts for user review/adopt.
    """
    from hermes_skillopt.env import HermesSkillEnv
    from hermes_skillopt.gate import ValidationGate
    from hermes_skillopt.optimizer import OptimizerBackend
    from hermes_skillopt.state import SkillOptArtifacts, SkillState
    from hermes_skillopt.target import TargetExecutor

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
    executor = TargetExecutor()
    optimizer = OptimizerBackend(llm, edit_budget=edit_budget)
    gatekeeper = ValidationGate()

    current = original
    best = original
    best_gate: dict[str, Any] | None = None
    all_reflections: list[dict[str, Any]] = []
    all_edits: list[dict[str, Any]] = []
    all_gates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    write_text(artifacts.original, original)
    write_text(artifacts.current, current)
    write_text(artifacts.evidence, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(artifacts.train, [t.__dict__ for t in tasks["train"]])
    _jsonl_write(artifacts.val, [t.__dict__ for t in tasks["val"]])
    _jsonl_write(artifacts.test, [t.__dict__ for t in tasks["test"]])

    for it in range(1, max(1, int(iterations)) + 1):
        train_eval = executor.evaluate(current, tasks["train"], label=f"current_train_{it}")
        reflection = optimizer.reflect(tasks["train"], current, train_eval, run_dir, it)
        candidate = optimizer.propose(reflection, current, run_dir, it)
        edit_plan = {"iteration": it, "edits": candidate.edits, "reasoning": candidate.reasoning, "bounded": True}
        write_text(run_dir / f"candidate_{it}_SKILL.md", candidate.text)
        write_text(run_dir / f"candidate_{it}_edits.json", json.dumps(edit_plan, ensure_ascii=False, indent=2) + "\n")

        current_val = executor.evaluate(current, tasks["val"], label=f"current_val_{it}")
        candidate_val = executor.evaluate(candidate.text, tasks["val"], label=f"candidate_val_{it}")
        write_text(artifacts.current_validation_results if it == max(1, int(iterations)) else run_dir / f"current_validation_results_{it}.json", json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
        write_text(artifacts.candidate_validation_results if it == max(1, int(iterations)) else run_dir / f"candidate_validation_results_{it}.json", json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")
        if it != max(1, int(iterations)):
            write_text(artifacts.current_validation_results, json.dumps(current_val, ensure_ascii=False, indent=2) + "\n")
            write_text(artifacts.candidate_validation_results, json.dumps(candidate_val, ensure_ascii=False, indent=2) + "\n")
        judge: dict[str, Any] | None = None
        try:
            judge = llm.json(
                "Explain current vs candidate on validation. Explanation only; cannot accept.\n"
                + json.dumps({"current_eval": current_val, "candidate_eval": candidate_val}, ensure_ascii=False)[:10000],
                {"kind": "gate"},
                run_dir / f"llm_gate_repair_{it}.json",
            )
        except Exception as exc:
            judge = {"judge_error": str(exc)}
        gate = gatekeeper.decide(it, current_val, candidate_val, current, candidate.text, judge=judge).as_dict()
        if gate["accepted"]:
            current = candidate.text
            best = candidate.text
            best_gate = gate
            status_value = "accepted"
        else:
            status_value = "rejected"
            rejected.append({"iteration": it, "gate": gate, "edits": candidate.edits, "reasoning": candidate.reasoning})
        all_reflections.append(reflection)
        all_edits.append(edit_plan)
        all_gates.append(gate | {"status": status_value})
        write_text(artifacts.current, current)

    final_status = "staged_best" if best != original else "rejected"
    proposed = best if final_status == "staged_best" else original
    diff = make_diff(original, proposed, target.relpath)
    if final_status == "staged_best":
        write_text(artifacts.best, best)
    write_text(artifacts.proposed, proposed)
    write_text(artifacts.diff, diff)
    write_text(artifacts.reflections, json.dumps(all_reflections, ensure_ascii=False, indent=2) + "\n")
    write_text(artifacts.candidate_edits, json.dumps(all_edits, ensure_ascii=False, indent=2) + "\n")
    write_text(artifacts.gate_results, json.dumps({"gates": all_gates, "best_gate": best_gate}, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(artifacts.rejected_edits, rejected)

    eval_file_used = evidence.get("eval_file")
    validation_summary = best_gate or (all_gates[-1] if all_gates else None)
    current_score = validation_summary.get("current_score") if validation_summary else None
    candidate_score = validation_summary.get("candidate_score") if validation_summary else None
    gate_reason = validation_summary.get("rationale") if validation_summary else "none"
    report = f"# Hermes SkillOpt full run\n\n- abstraction: SkillOpt core adapter (trainable skill state, frozen target, optimizer bounded edit, benchmark env, validation gate)\n- run_id: {rid}\n- status: {final_status}\n- skill: {target.name}\n- backend: {llm.mode}\n- eval_file: {eval_file_used or 'none'}\n- curated_task_count: {evidence.get('curated_task_count', 0)}\n- harvested_fragments: {len(evidence.get('snippets', []))}\n- train/val/test: {len(tasks['train'])}/{len(tasks['val'])}/{len(tasks['test'])}\n- iterations: {max(1, int(iterations))}\n- validation_scores: current={current_score}, candidate={candidate_score}\n- gate_reason: {gate_reason}\n- acceptance_gate: candidate validation score must be strictly greater than current validation score\n- best_gate: {json.dumps(best_gate, ensure_ascii=False) if best_gate else 'none'}\n- changed: {bool(diff)}\n\n## Diff preview\n\n```diff\n{diff[:4000]}\n```\n"
    write_text(artifacts.report, report)
    manifest = {
        "run_id": rid,
        "status": final_status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hermes_home": str(home),
        "skill_name": target.name,
        "skill_path": str(target.path),
        "skill_relpath": target.relpath,
        "original_sha256": sha256_text(original),
        "proposed_sha256": sha256_text(proposed),
        "engine": "hermes-native-skillopt-core-adapter",
        "backend": llm.mode,
        "eval_file": eval_file_used,
        "task_counts": evidence.get("task_counts", {k: len(v) for k, v in tasks.items()}),
        "curated_task_count": evidence.get("curated_task_count", 0),
        "validation_current_score": current_score,
        "validation_candidate_score": candidate_score,
        "gate_reason": gate_reason,
        "core_abstraction": {
            "skill_document": "trainable_state",
            "target_agent_model": "frozen_executor",
            "optimizer_model": "reflection_plus_bounded_edit",
            "environment_benchmark": "hermes_curated_replay_session_synthetic_tasks",
            "validation_gate": "sole_acceptance_gate_candidate_score_gt_current_score",
            "hermes_outer_shell": "staged_safety_adopt_rollback_profile_isolation",
        },
        "gate": best_gate,
        "files": artifacts.manifest_files(include_best=final_status == "staged_best"),
    }
    save_manifest(run_dir, manifest)
    result = {"success": True, "run_id": rid, "status": final_status, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(artifacts.diff), "report_path": str(artifacts.report), "gate": best_gate, "changed": bool(diff), "eval_file": eval_file_used, "task_counts": evidence.get("task_counts", {k: len(v) for k, v in tasks.items()}), "current_score": current_score, "candidate_score": candidate_score, "gate_reason": gate_reason}
    if auto_adopt and final_status == "staged_best" and not dry_run:
        result["adopt"] = adopt(rid, hermes_home_path=str(home), force=force)
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
    manifest = {"run_id": rid, "status": "staged", "created_at": datetime.now(timezone.utc).isoformat(), "hermes_home": str(home), "skill_name": target.name, "skill_path": str(target.path), "skill_relpath": target.relpath, "original_sha256": sha256_text(original), "proposed_sha256": sha256_text(proposed), "engine": engine, "files": {"original": "original_SKILL.md", "proposed": "proposed_SKILL.md", "diff": "diff.patch", "report": "report.md", "evidence": "evidence.json"}}
    write_text(run_dir / "original_SKILL.md", original)
    write_text(run_dir / "proposed_SKILL.md", proposed)
    write_text(run_dir / "diff.patch", diff)
    write_text(run_dir / "evidence.json", json.dumps({"snippets": evidence}, ensure_ascii=False, indent=2) + "\n")
    write_text(run_dir / "report.md", f"# SkillOpt dry run\n\n- run_id: {rid}\n- skill: {target.name}\n- engine: {engine}\n- changed: {bool(diff)}\n\n```diff\n{diff[:4000]}\n```\n")
    save_manifest(run_dir, manifest)
    return {"success": True, "run_id": rid, "status": "staged", "run_dir": str(run_dir), "skill": target.name, "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "changed": bool(diff)}


def status(hermes_home_path: str | None = None) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    runs = []
    for m in sorted(dirs["staging"].glob("*/manifest.json"), reverse=True)[:20]:
        try:
            d = json.loads(read_text(m))
            runs.append({k: d.get(k) for k in ("run_id", "status", "skill_name", "created_at", "engine", "backend")})
        except Exception:
            pass
    return {"success": True, "hermes_home": str(home), "skills_count": len(discover_skills(home)), "staging": str(dirs["staging"]), "backups": str(dirs["backups"]), "recent_runs": runs}


def review(run_id: str, hermes_home_path: str | None = None, include_diff_chars: int = 4000) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
    diff = read_text(run_dir / "diff.patch") if (run_dir / "diff.patch").exists() else ""
    report = read_text(run_dir / "report.md") if (run_dir / "report.md").exists() else ""
    gate = m.get("gate") or (json.loads(read_text(run_dir / "gate_results.json")).get("best_gate") if (run_dir / "gate_results.json").exists() else None)
    return {"success": True, "run_id": run_id, "status": m.get("status"), "skill": m.get("skill_name"), "gate": gate, "accepted": m.get("status") in ("staged_best", "accepted", "adopted"), "run_dir": str(run_dir), "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "diff_preview": diff[:include_diff_chars], "report_summary": report[:1200]}


def adopt(run_id: str, hermes_home_path: str | None = None, force: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
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


def rollback(run_id: str, hermes_home_path: str | None = None, force: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    run_dir = resolve_run_dir(home, run_id)
    m = load_manifest(run_dir)
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
