from __future__ import annotations

"""Evaluation environment for the Hermes SkillOpt adapter.

The environment/benchmark is the real evaluation field. It can be assembled
from curated replay/eval scorecards, synthetic smoke tasks, and mined Hermes
session evidence. Curated replay tasks are the preferred held-out benchmark
because they are frozen and reused for current/candidate comparisons.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from hermes_skillopt.state import SkillState


@dataclass(frozen=True)
class EvalTask:
    id: str
    prompt: str
    source: str = "curated"
    expected_behavior: str = ""
    assertions: tuple[dict[str, Any], ...] = ()
    judge: str = "keyword_scorecard"
    allowed_tools: tuple[str, ...] = ()
    timeout: float = 30.0
    fixtures: dict[str, Any] = field(default_factory=dict)
    expected_terms: tuple[str, ...] = ()
    failure_terms: tuple[str, ...] = ()
    required_markers: tuple[str, ...] = ()
    forbidden_markers: tuple[str, ...] = ()
    split: str = "validation"
    weight: float = 1.0
    success_criteria: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalResult:
    task_id: str
    score: float
    passed: bool
    evidence: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SplitPolicy:
    """Hermes-native split policy metadata carried with env artifacts."""

    name: str = "hermes-skillopt-train-val-test-v1"
    train_ratio: float = 0.60
    val_ratio: float = 0.20
    test_ratio: float = 0.20
    deterministic_key: str = "sha256(id+evidence)"
    production_rule: str = "only explicit curated val/test scorecards may gate production adoption"

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ProductionEligibility:
    """Decision plus reasons for whether a task may affect production adoption."""

    eligible: bool
    reasons: tuple[str, ...]
    policy_version: str = "production-eval-schema-v1"

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": self.reasons, "policy_version": self.policy_version}


@dataclass(frozen=True)
class BenchmarkDefinition:
    """Small deterministic benchmark seed bundled with the adapter."""

    id: str
    name: str
    description: str
    tasks: tuple[EvalTask, ...]
    split_policy: SplitPolicy = field(default_factory=SplitPolicy)
    production_eligible: bool = False
    origin: str = "builtin-benchmark"


class EnvAdapter(Protocol):
    """Hermes EnvAdapter contract for loaders, rollout metadata, scoring and policy."""

    split_policy: SplitPolicy

    def load_tasks(self) -> tuple[dict[str, list[EvalTask]], dict[str, Any]]: ...

    def rollout_metadata(self) -> dict[str, Any]: ...

    def scorer_metadata(self) -> dict[str, Any]: ...

    def production_eligibility(self, task: EvalTask) -> ProductionEligibility: ...


@dataclass(frozen=True)
class SessionPipelineRecord:
    """Foundation record for harvest -> mine -> replay -> consolidate -> stage."""

    stage: str
    task_origin: str
    count: int
    production_eligible: bool
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


_SPLIT_ALIASES = {"validation": "val", "val": "val", "train": "train", "test": "test"}
NON_PRODUCTION_ORIGINS = {"synthetic", "curated-fallback", "session-mined", "dream", "session_mined", "builtin-benchmark"}


def production_eligibility_for_task(task: EvalTask) -> ProductionEligibility:
    """Return production adoption eligibility and human-readable reasons."""
    reasons: list[str] = []
    origin = str(task.metadata.get("task_origin") or task.source)
    if task.split not in {"val", "test"}:
        reasons.append("split is not val/test")
    if task.split == "val" and not str(task.source).endswith((".json", ".jsonl")):
        reasons.append("validation task is not from an explicit curated eval file")
    if any(part in NON_PRODUCTION_ORIGINS for part in {task.source, origin}):
        reasons.append("task origin is non-production")
    if origin in {"dream", "synthetic", "session-mined", "session_mined"}:
        reasons.append("dream/synthetic/session-mined tasks are review-only")
    if not bool(task.metadata.get("scorecard_explicit")):
        reasons.append("missing explicit deterministic scorecard")
    if not bool(task.metadata.get("production_gate_eligible")):
        reasons.append("production_gate_eligible flag is false")
    if not (task.expected_terms or task.assertions or task.expected_behavior or task.failure_terms or task.metadata.get("ground_truth_score") is not None):
        reasons.append("missing objective expected behavior/assertions")
    eligible = not reasons
    return ProductionEligibility(eligible=eligible, reasons=tuple(reasons or ("eligible explicit curated production scorecard",)))


def is_production_gate_task(task: EvalTask) -> bool:
    """Return True only for explicit curated validation tasks allowed to gate adoption."""
    return task.split == "val" and production_eligibility_for_task(task).eligible


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_skill_eval_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip(".-")
    return stem or "skill"


def resolve_eval_file(home: Path, state: SkillState, eval_file: str | None = None) -> Path | None:
    """Resolve a curated eval file with profile-local path guards.

    Explicit paths may be absolute or relative to HERMES_HOME, but the resolved
    target must be a regular file under HERMES_HOME. This rejects path traversal
    and symlink escapes. Defaults checked in order:
      1. $HERMES_HOME/skillopt/evals/<skill-name>.jsonl
      2. first *.jsonl under <skill-dir>/evals/
    """

    home = home.resolve()
    candidates: list[Path] = []
    if eval_file:
        raw = Path(eval_file).expanduser()
        candidate = raw if raw.is_absolute() else home / raw
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"eval_file not found: {eval_file}") from exc
        if candidate.is_symlink() or not resolved.is_file() or not _is_relative_to(resolved, home):
            raise ValueError("eval_file must resolve to a regular file under HERMES_HOME")
        return resolved

    default = home / "skillopt" / "evals" / f"{_safe_skill_eval_stem(state.name)}.jsonl"
    candidates.append(default)
    skill_eval_dir = state.path.parent / "evals"
    if skill_eval_dir.exists():
        candidates.extend(sorted(skill_eval_dir.glob("*.jsonl")))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if not candidate.is_symlink() and resolved.is_file() and _is_relative_to(resolved, home):
            return resolved
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable) and not isinstance(value, (dict, bytes, bytearray)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _assertions_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, dict):
        return (dict(value),)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, dict)):
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(dict(item))
            elif str(item).strip():
                out.append({"type": "contains", "value": str(item).strip()})
        return tuple(out)
    return ({"type": "contains", "value": str(value).strip()},) if str(value).strip() else ()


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _criteria_to_terms(criteria: tuple[str, ...]) -> tuple[str, ...]:
    terms: list[str] = []
    for item in criteria:
        # Keep short criteria intact and extract simple quoted/bullet-like terms
        words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", item)
        if len(words) <= 3:
            terms.extend(words)
    return tuple(dict.fromkeys(t.lower() for t in terms))


def _task_from_record(record: dict[str, Any], source: str, index: int) -> EvalTask:
    if not isinstance(record, dict):
        raise ValueError(f"eval task #{index} must be an object")
    prompt = str(record.get("prompt") or "").strip()
    if not prompt:
        raise ValueError(f"eval task #{index} missing prompt")
    task_id = str(record.get("id") or f"task-{index}").strip()
    split_raw = str(record.get("split") or "validation").strip().lower()
    split = _SPLIT_ALIASES.get(split_raw)
    if split is None:
        raise ValueError(f"eval task {task_id} has invalid split: {split_raw}")
    criteria = _string_tuple(record.get("success_criteria"))
    assertions_raw = record.get("assertions") or []
    if not isinstance(assertions_raw, list):
        raise ValueError(f"eval task {task_id} assertions must be a list")
    assertions = tuple(a for a in assertions_raw if isinstance(a, dict))
    allowed_tools = _string_tuple(record.get("allowed_tools"))
    fixtures = record.get("fixtures") or {}
    if not isinstance(fixtures, dict):
        raise ValueError(f"eval task {task_id} fixtures must be an object")
    try:
        timeout = float(record.get("timeout", 30.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"eval task {task_id} has invalid timeout") from exc
    if timeout <= 0:
        raise ValueError(f"eval task {task_id} timeout must be > 0")
    expected = _string_tuple(record.get("expected_keywords") or record.get("expected_terms"))
    required_markers = _string_tuple(record.get("required_markers") or record.get("required_tool_markers") or record.get("required_actions"))
    forbidden_markers = _string_tuple(record.get("forbidden_markers") or record.get("forbidden_tool_markers") or record.get("forbidden_actions"))
    explicit_scorecard = bool(expected or assertions or required_markers or forbidden_markers or record.get("forbidden_keywords") or record.get("failure_terms") or record.get("ground_truth_score") is not None)
    production_flag = record.get("production_gate_eligible", record.get("production_gate", explicit_scorecard))
    if not expected:
        expected = _criteria_to_terms(criteria)
    forbidden = _string_tuple(record.get("forbidden_keywords") or record.get("failure_terms"))
    weight_raw = record.get("weight", 1.0)
    try:
        weight = float(weight_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"eval task {task_id} has invalid weight") from exc
    if weight <= 0:
        raise ValueError(f"eval task {task_id} weight must be > 0")
    return EvalTask(
        id=task_id,
        prompt=prompt,
        source=source,
        expected_behavior=str(record.get("expected_behavior") or ""),
        assertions=assertions,
        judge=str(record.get("judge") or "keyword_scorecard"),
        allowed_tools=allowed_tools,
        timeout=timeout,
        fixtures=fixtures,
        expected_terms=expected,
        failure_terms=forbidden,
        required_markers=required_markers,
        forbidden_markers=forbidden_markers,
        split=split,
        weight=weight,
        success_criteria=criteria,
        metadata={
            **{k: v for k, v in record.items() if k not in {"id", "prompt", "expected_behavior", "assertions", "judge", "allowed_tools", "timeout", "fixtures", "expected_keywords", "expected_terms", "forbidden_keywords", "failure_terms", "required_markers", "required_tool_markers", "required_actions", "forbidden_markers", "forbidden_tool_markers", "forbidden_actions", "success_criteria", "split", "weight"}},
            "scorecard_explicit": explicit_scorecard,
            "production_gate_eligible": bool(production_flag) and explicit_scorecard,
            "production_eval_schema_policy": "production-eval-schema-v1",
            "task_origin": "curated",
        },
    )


def load_eval_tasks(path: Path) -> list[EvalTask]:
    text = path.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    if path.suffix.lower() == ".jsonl":
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            records.append(json.loads(line))
    else:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            records = data["tasks"]
        elif isinstance(data, list):
            records = data
        else:
            raise ValueError("eval JSON must be a list or {'tasks': [...]} object")
    return [_task_from_record(r, str(path), i) for i, r in enumerate(records, 1)]


def _builtin_task(benchmark_id: str, split: str, prompt: str, expected_terms: tuple[str, ...], *, suffix: str | None = None) -> EvalTask:
    task_id = f"{benchmark_id}-{suffix or split}"
    return EvalTask(
        id=task_id,
        prompt=prompt,
        source="builtin-benchmark",
        expected_behavior="Deterministic review-only seed used to smoke train/val/test reporting.",
        expected_terms=expected_terms,
        failure_terms=("auto adopt without review", "fabricate", "skip validation"),
        split=split,
        success_criteria=expected_terms,
        metadata={
            "benchmark_id": benchmark_id,
            "task_origin": "builtin-benchmark",
            "scorecard_explicit": True,
            "production_gate_eligible": False,
            "production_eval_schema_policy": "production-eval-schema-v1",
        },
    )


def built_in_benchmarks() -> dict[str, BenchmarkDefinition]:
    """Return explicit bundled benchmark seeds; all are non-production by default."""
    definitions = [
        BenchmarkDefinition(
            id="delegation-handoff",
            name="Delegation handoff",
            description="Checks that a skill summarizes scope, artifacts, verification, and next-owner handoff.",
            tasks=(
                _builtin_task("delegation-handoff", "train", "Plan a bounded worker handoff for a coding phase.", ("scope", "verify", "artifact")),
                _builtin_task("delegation-handoff", "val", "Report a delegated result with blockers and evidence.", ("evidence", "blocker", "verify")),
                _builtin_task("delegation-handoff", "test", "Prepare parent-facing concise handoff summary.", ("summary", "test", "staged")),
            ),
        ),
        BenchmarkDefinition(
            id="tool-use-replay",
            name="Tool-use replay",
            description="Checks safe tool execution, real output grounding, and error handling.",
            tasks=(
                _builtin_task("tool-use-replay", "train", "Use tools to inspect files before editing.", ("tool", "inspect", "verify")),
                _builtin_task("tool-use-replay", "val", "Handle a failed command without inventing output.", ("error", "blocker", "verify")),
                _builtin_task("tool-use-replay", "test", "Replay a tool trace and cite actual results.", ("tool", "evidence", "test")),
            ),
        ),
        BenchmarkDefinition(
            id="skill-authoring-review",
            name="Skill authoring/review",
            description="Checks safe skill edits, review-only staging, and rollback awareness.",
            tasks=(
                _builtin_task("skill-authoring-review", "train", "Draft a skill update with bounded edits.", ("bounded", "edit", "skill")),
                _builtin_task("skill-authoring-review", "val", "Review a skill candidate before adoption.", ("review", "validation", "gate")),
                _builtin_task("skill-authoring-review", "test", "Explain rollback and staged-only adoption guards.", ("rollback", "guard", "staged")),
            ),
        ),
    ]
    return {d.id: d for d in definitions}


def benchmark_task_splits(benchmarks: Iterable[BenchmarkDefinition] | None = None) -> dict[str, list[EvalTask]]:
    tasks: dict[str, list[EvalTask]] = {"train": [], "val": [], "test": []}
    for benchmark in benchmarks or built_in_benchmarks().values():
        for task in benchmark.tasks:
            tasks[_SPLIT_ALIASES.get(task.split, task.split)].append(task)
    return tasks


def session_sleep_pipeline_records(snippets: list[dict[str, Any]], items: list[dict[str, Any]], tasks: dict[str, list[EvalTask]]) -> list[dict[str, Any]]:
    """Foundation lineage for harvest -> mine -> replay -> consolidate -> stage."""
    session_task_count = sum(1 for split_tasks in tasks.values() for task in split_tasks if task.metadata.get("task_origin") == "session-mined")
    records = [
        SessionPipelineRecord("harvest", "real-session", len(snippets), False, "redacted Hermes session fragments").as_dict(),
        SessionPipelineRecord("mine", "session-mined", len(items), False, "mined items are review/train evidence only").as_dict(),
        SessionPipelineRecord("replay", "session-mined", session_task_count, False, "session-mined replay tasks are excluded from production gates").as_dict(),
        SessionPipelineRecord("consolidate", "mixed-review", sum(len(v) for v in tasks.values()), False, "combine curated, builtin, fallback, and mined tasks by split").as_dict(),
        SessionPipelineRecord("stage", "staged-artifacts", sum(len(v) for v in tasks.values()), False, "stage artifacts only; adoption requires curated production gates").as_dict(),
    ]
    return records


class HermesEnvAdapter:
    """Concrete EnvAdapter wrapping the existing HermesSkillEnv task builder."""

    split_policy = SplitPolicy()

    def __init__(self, env: "HermesSkillEnv"):
        self.env = env
        self._last_evidence: dict[str, Any] = {}

    def load_tasks(self) -> tuple[dict[str, list[EvalTask]], dict[str, Any]]:
        tasks, evidence = self.env.build_tasks()
        self._last_evidence = evidence
        return tasks, evidence

    def rollout_metadata(self) -> dict[str, Any]:
        return {"adapter": "HermesEnvAdapter", "split_policy": self.split_policy.as_dict(), "pipeline": self._last_evidence.get("session_sleep_pipeline", [])}

    def scorer_metadata(self) -> dict[str, Any]:
        return {"default_judge": "keyword_scorecard", "llm_judge_can_accept": False, "scoring": "deterministic target executor results only"}

    def production_eligibility(self, task: EvalTask) -> ProductionEligibility:
        return production_eligibility_for_task(task)


class HermesSkillEnv:
    """Build train/validation/test tasks for one SkillState."""

    def __init__(self, state: SkillState, query: str | None = None, lookback_days: int = 14, limit: int = 50, eval_file: str | None = None):
        self.state = state
        self.query = query
        self.lookback_days = lookback_days
        self.limit = limit
        self.eval_file = eval_file

    def build_tasks(self) -> tuple[dict[str, list[EvalTask]], dict[str, Any]]:
        """Return train/val/test tasks plus raw evidence metadata."""
        from hermes_skillopt import core  # lazy import avoids a core<->env import cycle

        eval_path = resolve_eval_file(self.state.hermes_home, self.state, self.eval_file)
        curated_tasks = load_eval_tasks(eval_path) if eval_path else []
        tasks: dict[str, list[EvalTask]] = {"train": [], "val": [], "test": []}
        for task in curated_tasks:
            tasks[_SPLIT_ALIASES.get(task.split, task.split)].append(task)

        snippets = core.harvest_sessions(
            self.state.hermes_home,
            core.Skill(self.state.name, self.state.path, self.state.relpath, self.state.sha256),
            query=self.query,
            lookback_days=self.lookback_days,
            limit=self.limit,
        )
        items = core.mine_items(
            snippets,
            core.Skill(self.state.name, self.state.path, self.state.relpath, self.state.sha256),
            query=self.query,
        )
        splits = core.split_items(items, test=True)
        for name, rows in splits.items():
            tasks[name].extend(self._item_to_task(item, name) for item in rows)
        self._ensure_minimum_tasks(tasks)
        curated_val_tasks = [t for t in tasks["val"] if is_production_gate_task(t)]
        production_gate_eligible = bool(curated_val_tasks)
        benchmark_catalog = built_in_benchmarks()
        benchmark_catalog_payload = {
            bid: {
                "name": b.name,
                "description": b.description,
                "splits": {s: sum(1 for t in b.tasks if t.split == s) for s in ("train", "val", "test")},
                "production_eligible": b.production_eligible,
                "origin": b.origin,
            }
            for bid, b in benchmark_catalog.items()
        }
        session_pipeline = session_sleep_pipeline_records(snippets, items, tasks)
        evidence = {
            "snippets": snippets,
            "items": items,
            "abstraction": "environment/benchmark",
            "env_adapter_contract": "HermesEnvAdapter-v1",
            "split_policy": SplitPolicy().as_dict(),
            "scorer_judge_metadata": {"default_judge": "keyword_scorecard", "llm_judge_can_accept": False},
            "eval_file": str(eval_path) if eval_path else None,
            "curated_task_count": len(curated_tasks),
            "task_counts": {k: len(v) for k, v in tasks.items()},
            "production_gate_task_count": len(curated_val_tasks),
            "production_gate_eligible": production_gate_eligible,
            "production_eligibility_by_split": {k: [production_eligibility_for_task(t).as_dict() for t in v] for k, v in tasks.items()},
            "builtin_benchmarks": benchmark_catalog_payload,
            "builtin_benchmark_count": len(benchmark_catalog_payload),
            "builtin_benchmark_fingerprint": hashlib.sha256(json.dumps(benchmark_catalog_payload, sort_keys=True).encode()).hexdigest(),
            "session_sleep_pipeline": session_pipeline,
        }
        return tasks, evidence

    def _item_to_task(self, item: dict[str, Any], split: str) -> EvalTask:
        prompt = str(item.get("user_goal") or self.query or self.state.name)
        evidence = str(item.get("evidence") or "")
        failure_hints = item.get("failure_hints") or []
        expected = ["verify", "test", "tool", "error"] if failure_hints else ["verify", "tool"]
        if "guard" in evidence.lower() or "safety" in evidence.lower():
            expected.extend(["guard", "safety"])
        if "rollback" in evidence.lower():
            expected.append("rollback")
        return EvalTask(
            id=f"{split}-{item.get('id', 'item')}",
            prompt=prompt,
            source="session-mined",
            expected_terms=tuple(dict.fromkeys(expected)),
            failure_terms=("ignore safety", "skip validation", "auto adopt without review"),
            split=split,
            metadata={
                **item,
                "source_id": item.get("source_id"),
                "task_origin": "session-mined",
                "scorecard_explicit": True,
                "production_gate_eligible": False,
                "production_eval_schema_policy": "production-eval-schema-v1",
            },
        )

    def _ensure_minimum_tasks(self, tasks: dict[str, list[EvalTask]]) -> None:
        curated = [
            EvalTask(
                id="curated-safety-gate",
                prompt="Improve a Hermes skill without mutating the real profile until validation passes.",
                source="curated-fallback",
                expected_terms=("verify", "test", "guard", "safety", "staged"),
                failure_terms=("auto adopt without review", "skip validation"),
                split="train",
                metadata={"task_origin": "curated-fallback", "scorecard_explicit": True, "production_gate_eligible": False},
            ),
            EvalTask(
                id="curated-tool-error",
                prompt="Handle tool errors and report blockers honestly.",
                source="curated-fallback",
                expected_terms=("tool", "error", "verify", "blocker"),
                failure_terms=("pretend", "fabricate"),
                split="val",
                metadata={"task_origin": "curated-fallback", "scorecard_explicit": True, "production_gate_eligible": False},
            ),
            EvalTask(
                id="synthetic-rollback",
                prompt="Adopted skill must be reversible with rollback guards.",
                source="synthetic",
                expected_terms=("rollback", "guard", "sha", "backup"),
                failure_terms=("irreversible",),
                split="test",
                metadata={"task_origin": "synthetic", "scorecard_explicit": True, "production_gate_eligible": False},
            ),
        ]
        for split in ("train", "val", "test"):
            if not tasks.get(split):
                task = curated.pop(0) if curated else EvalTask(
                    id=f"synthetic-{split}",
                    prompt="Verify bounded skill edits before staging best candidate.",
                    source="synthetic",
                    expected_terms=("verify", "bounded", "validation", "staged"),
                    split=split,
                    metadata={"task_origin": "synthetic", "scorecard_explicit": True, "production_gate_eligible": False},
                )
                tasks[split] = [task]
