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


def _heuristic_score(skill_text: str, items: list[dict[str, Any]]) -> float:
    low = skill_text.lower()
    score = 0.2
    for term, pts in [("verify", .14), ("test", .12), ("tool", .08), ("error", .08), ("safety", .08), ("guard", .08), ("rollback", .06), ("artifact", .06), ("secret", .05), ("redact", .05)]:
        if term in low:
            score += pts
    if "skillopt learned rules" in low:
        score += .12
    if items and any(i.get("failure_hints") for i in items):
        score += .05 if ("error" in low or "verify" in low) else 0
    return round(min(score, 1.0), 3)


def gate_candidate(backend: LLMBackend, current: str, candidate: str, val: list[dict[str, Any]], run_dir: Path, iteration: int) -> dict[str, Any]:
    hcur, hcand = _heuristic_score(current, val), _heuristic_score(candidate, val)
    data: dict[str, Any] = {}
    try:
        data = backend.json("Judge current vs candidate on validation items. Return JSON scores.\nVAL=" + json.dumps(val, ensure_ascii=False)[:10000], {"kind": "gate"}, run_dir / f"llm_gate_repair_{iteration}.json")
    except Exception as exc:
        data = {"judge_error": str(exc)}
    cur = float(data.get("current_score", hcur)) if isinstance(data, dict) else hcur
    cand = float(data.get("candidate_score", hcand)) if isinstance(data, dict) else hcand
    # blend with heuristic to prevent mock/LLM from accepting no-op regressions
    cur = round((cur + hcur) / 2, 3)
    cand = round((cand + hcand) / 2, 3)
    accepted = bool(candidate != current and cand > cur)
    return {"iteration": iteration, "current_score": cur, "candidate_score": cand, "accepted": accepted, "rationale": data.get("rationale", "heuristic+LLM validation gate"), "heuristic": {"current": hcur, "candidate": hcand}, "judge": data}


def full_run(skill: str | None = None, query: str | None = None, lookback_days: int = 14, limit: int = 50, iterations: int = 1, edit_budget: int = 3, backend: str = "auto", allow_mock: bool = False, auto_adopt: bool = False, force: bool = False, hermes_home_path: str | None = None, ctx: Any = None, dry_run: bool = False) -> dict[str, Any]:
    home = hermes_home(hermes_home_path)
    dirs = ensure_dirs(home)
    target = find_skill(home, skill)
    original = read_text(target.path)
    rid = now_id() + "-" + target.name.replace("/", "-")
    run_dir = dirs["staging"] / rid
    run_dir.mkdir(parents=True, exist_ok=False)
    llm = LLMBackend(backend=backend, allow_mock=allow_mock, ctx=ctx)

    snippets = harvest_sessions(home, target, query=query, lookback_days=lookback_days, limit=limit)
    items = mine_items(snippets, target, query=query)
    splits = split_items(items, test=True)
    current = original
    best = original
    best_gate: dict[str, Any] | None = None
    all_reflections: list[dict[str, Any]] = []
    all_edits: list[dict[str, Any]] = []
    all_gates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    write_text(run_dir / "original_SKILL.md", original)
    write_text(run_dir / "current_SKILL.md", current)
    write_text(run_dir / "evidence.json", json.dumps({"snippets": snippets, "items": items}, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(run_dir / "train_items.jsonl", splits["train"])
    _jsonl_write(run_dir / "val_items.jsonl", splits["val"])
    _jsonl_write(run_dir / "test_items.jsonl", splits["test"])

    for it in range(1, max(1, int(iterations)) + 1):
        reflections = reflect_items(llm, splits["train"], current, run_dir, it)
        reflections["iteration"] = it
        edit_plan = bounded_edits(llm, reflections, current, edit_budget, run_dir, it)
        edit_plan["iteration"] = it
        candidate = apply_bounded_edits(current, edit_plan.get("edits", []))
        write_text(run_dir / f"candidate_{it}_SKILL.md", candidate)
        write_text(run_dir / f"candidate_{it}_edits.json", json.dumps(edit_plan, ensure_ascii=False, indent=2) + "\n")
        gate = gate_candidate(llm, current, candidate, splits["val"], run_dir, it)
        if gate["accepted"]:
            current = candidate
            best = candidate
            best_gate = gate
            status_value = "accepted"
        else:
            status_value = "rejected"
            rec = {"iteration": it, "gate": gate, "edits": edit_plan.get("edits", []), "reasoning": edit_plan.get("reasoning")}
            rejected.append(rec)
        all_reflections.append(reflections)
        all_edits.append(edit_plan)
        all_gates.append(gate | {"status": status_value})
        write_text(run_dir / "current_SKILL.md", current)

    final_status = "staged_best" if best != original else "rejected"
    proposed = best
    diff = make_diff(original, proposed, target.relpath)
    write_text(run_dir / "best_skill.md", best)
    write_text(run_dir / "proposed_SKILL.md", proposed)
    write_text(run_dir / "diff.patch", diff)
    write_text(run_dir / "reflections.json", json.dumps(all_reflections, ensure_ascii=False, indent=2) + "\n")
    write_text(run_dir / "candidate_edits.json", json.dumps(all_edits, ensure_ascii=False, indent=2) + "\n")
    write_text(run_dir / "gate_results.json", json.dumps({"gates": all_gates, "best_gate": best_gate}, ensure_ascii=False, indent=2) + "\n")
    _jsonl_write(run_dir / "rejected_edits.jsonl", rejected)

    report = f"# Hermes SkillOpt full run\n\n- run_id: {rid}\n- status: {final_status}\n- skill: {target.name}\n- backend: {llm.mode}\n- harvested_fragments: {len(snippets)}\n- train/val/test: {len(splits['train'])}/{len(splits['val'])}/{len(splits['test'])}\n- iterations: {max(1, int(iterations))}\n- best_gate: {json.dumps(best_gate, ensure_ascii=False) if best_gate else 'none'}\n- changed: {bool(diff)}\n\n## Diff preview\n\n```diff\n{diff[:4000]}\n```\n"
    write_text(run_dir / "report.md", report)
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
        "engine": "hermes-native-full-skillopt",
        "backend": llm.mode,
        "gate": best_gate,
        "files": {"original": "original_SKILL.md", "current": "current_SKILL.md", "best": "best_skill.md", "proposed": "proposed_SKILL.md", "diff": "diff.patch", "report": "report.md", "evidence": "evidence.json", "train": "train_items.jsonl", "val": "val_items.jsonl", "test": "test_items.jsonl", "reflections": "reflections.json", "candidate_edits": "candidate_edits.json", "gate_results": "gate_results.json", "rejected_edits": "rejected_edits.jsonl"},
    }
    save_manifest(run_dir, manifest)
    result = {"success": True, "run_id": rid, "status": final_status, "run_dir": str(run_dir), "skill": target.name, "diff_path": str(run_dir / "diff.patch"), "report_path": str(run_dir / "report.md"), "gate": best_gate, "changed": bool(diff)}
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
    backup_dir = dirs["backups"] / f"{now_id()}-{run_id}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    write_text(backup_dir / "SKILL.md", current)
    write_text(backup_dir / "manifest.json", json.dumps({"run_id": run_id, "skill_path": str(target), "sha256": current_sha}, ensure_ascii=False, indent=2) + "\n")
    write_text(target, proposed)
    m.update({"status": "adopted", "adopted_at": datetime.now(timezone.utc).isoformat(), "backup_dir": str(backup_dir), "adopted_sha256": sha256_text(proposed)})
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
