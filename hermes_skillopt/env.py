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


@dataclass(frozen=True)
class EvalPackMetadata:
    """Identity, split governance, policy, and fingerprints for an explicit eval pack."""

    pack_id: str
    version: str
    schema_version: str
    path: str | None
    fingerprint_sha256: str
    eval_file_sha256: str | None
    task_count: int
    split_counts: dict[str, int]
    production_eligible_task_count: int
    production_policy: dict[str, Any] = field(default_factory=dict)
    production_policy_fingerprint_sha256: str | None = None
    heldout_policy: str = "validation selects candidates; held-out test is final gate only"

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


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
NON_PRODUCTION_ORIGINS = {"synthetic", "curated-fallback", "session-mined", "dream", "session_mined", "builtin-benchmark", "sample-eval-pack"}
PRODUCTION_EVAL_POLICY_VERSION = "production-eval-schema-v1"
EVAL_PACK_SCHEMA_VERSION = "hermes-curated-eval-pack-v1"
EXPLICIT_CURATED_EVAL_PACK_CONTRACT = {
    "schema_version": EVAL_PACK_SCHEMA_VERSION,
    "required_top_level_fields": ["schema_version", "pack_id", "version", "tasks"],
    "required_splits": ["train", "val", "test"],
    "production_policy": "production_policy.allow_production_adoption must be true before any val/test task can gate production",
    "task_scorecard": "each production task must set production_gate_eligible=true and include deterministic expected terms/assertions/markers/ground_truth",
    "review_only_origins": sorted(NON_PRODUCTION_ORIGINS),
}


def production_eligibility_for_task(task: EvalTask) -> ProductionEligibility:
    """Return production adoption eligibility under the curated eval pack contract."""
    reasons: list[str] = []
    origin = str(task.metadata.get("task_origin") or task.source)
    schema_version = str(task.metadata.get("eval_pack_schema_version") or "")
    if task.split not in {"val", "test"}:
        reasons.append("split is not val/test")
    if not str(task.source).endswith((".json", ".jsonl")):
        reasons.append("task is not from an explicit eval file")
    if schema_version != EVAL_PACK_SCHEMA_VERSION:
        reasons.append("task is not from hermes-curated-eval-pack-v1")
    if not bool(task.metadata.get("explicit_curated_eval_pack")):
        reasons.append("missing explicit curated eval pack provenance")
    if not bool(task.metadata.get("eval_pack_production_allowed")):
        reasons.append("eval pack production policy does not allow adoption")
    if not task.metadata.get("eval_pack_fingerprint_sha256") or not task.metadata.get("eval_pack_policy_fingerprint_sha256"):
        reasons.append("missing eval pack/policy fingerprint provenance")
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


def _task_from_record(record: dict[str, Any], source: str, index: int, pack_meta: dict[str, Any] | None = None) -> EvalTask:
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
    pack_meta = pack_meta or {}
    origin = str(record.get("task_origin") or pack_meta.get("task_origin") or "curated")
    sample_pack = origin == "sample-eval-pack" or bool(pack_meta.get("sample_pack"))
    explicit_curated_pack = bool(pack_meta.get("schema_version") == EVAL_PACK_SCHEMA_VERSION and pack_meta.get("pack_id") and pack_meta.get("version"))
    pack_production_allowed = bool(pack_meta.get("production_policy", {}).get("allow_production_adoption"))
    production_flag = bool(production_flag) and explicit_scorecard and explicit_curated_pack and pack_production_allowed and not sample_pack
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
            "production_gate_eligible": production_flag,
            "production_eval_schema_policy": PRODUCTION_EVAL_POLICY_VERSION,
            "task_origin": origin,
            **({
                "eval_pack_id": pack_meta.get("pack_id"),
                "eval_pack_version": pack_meta.get("version"),
                "eval_pack_fingerprint_sha256": pack_meta.get("fingerprint_sha256"),
                "eval_pack_file_sha256": pack_meta.get("eval_file_sha256"),
                "eval_pack_schema_version": pack_meta.get("schema_version"),
                "eval_pack_policy_fingerprint_sha256": pack_meta.get("production_policy_fingerprint_sha256"),
                "eval_pack_production_allowed": pack_production_allowed,
                "explicit_curated_eval_pack": explicit_curated_pack,
                "eval_pack_sample": bool(pack_meta.get("sample_pack")),
            } if pack_meta else {}),
        },
    )


def _eval_pack_fingerprint(data: dict[str, Any]) -> str:
    comparable = {k: v for k, v in data.items() if k not in {"fingerprint_sha256", "fingerprint"}}
    return hashlib.sha256(json.dumps(comparable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _stable_json_fingerprint(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _production_policy_from_pack(pack_payload: dict[str, Any], schema_version: str) -> dict[str, Any]:
    """Normalize the pack-level policy that authorizes production-gate use.

    Contract: v1 curated packs are first-class only when they declare pack
    identity/version, complete train/val/test splits, and explicitly opt in via
    production_policy.allow_production_adoption=true. Legacy JSON/JSONL,
    sample packs, synthetic/fallback/session-mined origins remain review-only.
    """

    raw_policy_candidate = pack_payload.get("production_policy")
    raw_policy: dict[str, Any] = dict(raw_policy_candidate) if isinstance(raw_policy_candidate, dict) else {}
    allow = bool(raw_policy.get("allow_production_adoption", pack_payload.get("allow_production_adoption", False)))
    complete_splits_required = schema_version == EVAL_PACK_SCHEMA_VERSION or bool(pack_payload.get("require_complete_splits"))
    sample_pack = bool(pack_payload.get("sample_pack"))
    origin = str(pack_payload.get("task_origin") or ("sample-eval-pack" if sample_pack else "curated"))
    allowed = bool(
        schema_version == EVAL_PACK_SCHEMA_VERSION
        and allow
        and complete_splits_required
        and not sample_pack
        and origin not in NON_PRODUCTION_ORIGINS
    )
    policy = {
        "policy_version": PRODUCTION_EVAL_POLICY_VERSION,
        "contract": EXPLICIT_CURATED_EVAL_PACK_CONTRACT,
        "allow_production_adoption": allowed,
        "declared_allow_production_adoption": allow,
        "requires_complete_splits": complete_splits_required,
        "requires_explicit_scorecard": True,
        "requires_task_production_flag": True,
        "origin": origin,
        "sample_pack": sample_pack,
        "refusal_reasons": [],
    }
    if schema_version != EVAL_PACK_SCHEMA_VERSION:
        policy["refusal_reasons"].append("schema_version is not hermes-curated-eval-pack-v1")
    if not allow:
        policy["refusal_reasons"].append("production_policy.allow_production_adoption is not true")
    if sample_pack or origin in NON_PRODUCTION_ORIGINS:
        policy["refusal_reasons"].append("pack origin is review-only/non-production")
    policy.update({k: v for k, v in raw_policy.items() if k not in policy})
    policy["policy_fingerprint_sha256"] = _stable_json_fingerprint({k: v for k, v in policy.items() if k != "policy_fingerprint_sha256"})
    return policy


def _validate_eval_pack_contract(pack_payload: dict[str, Any], schema_version: str, pack_id: str) -> None:
    if schema_version != EVAL_PACK_SCHEMA_VERSION:
        return
    missing = [field for field in EXPLICIT_CURATED_EVAL_PACK_CONTRACT["required_top_level_fields"] if field not in pack_payload]
    if missing:
        raise ValueError(f"eval pack {pack_id} missing required v1 fields: {', '.join(missing)}")
    tasks = pack_payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"eval pack {pack_id} tasks must be a non-empty list")


def _eval_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_eval_pack_tasks(tasks: list[EvalTask], pack_id: str) -> dict[str, int]:
    split_counts = {"train": 0, "val": 0, "test": 0}
    seen_ids: dict[str, str] = {}
    seen_prompts: dict[str, str] = {}
    for task in tasks:
        split = _SPLIT_ALIASES.get(task.split, task.split)
        if split not in split_counts:
            raise ValueError(f"eval pack {pack_id} task {task.id} has invalid split: {task.split}")
        split_counts[split] += 1
        if task.id in seen_ids and seen_ids[task.id] != split:
            raise ValueError(f"eval pack {pack_id} leaks task id {task.id!r} across {seen_ids[task.id]} and {split}")
        seen_ids[task.id] = split
        prompt_fp = hashlib.sha256(task.prompt.strip().lower().encode("utf-8")).hexdigest()
        if prompt_fp in seen_prompts and seen_prompts[prompt_fp] != split:
            raise ValueError(f"eval pack {pack_id} reuses an identical prompt across {seen_prompts[prompt_fp]} and {split}")
        seen_prompts[prompt_fp] = split
    missing = [name for name, count in split_counts.items() if count <= 0]
    if missing:
        raise ValueError(f"eval pack {pack_id} must include train/val/test tasks; missing: {', '.join(missing)}")
    return split_counts


def load_eval_pack(path: Path) -> tuple[list[EvalTask], EvalPackMetadata]:
    text = path.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    pack_payload: dict[str, Any] = {}
    if path.suffix.lower() == ".jsonl":
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            records.append(json.loads(line))
        pack_payload = {"schema_version": "legacy-jsonl-eval-file-v1", "pack_id": path.stem, "version": "unversioned", "tasks": records}
    else:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            pack_payload = data
            records = data["tasks"]
        elif isinstance(data, list):
            records = data
            pack_payload = {"schema_version": "legacy-json-eval-list-v1", "pack_id": path.stem, "version": "unversioned", "tasks": records}
        else:
            raise ValueError("eval JSON must be a list or {'tasks': [...]} object")
    pack_id = str(pack_payload.get("pack_id") or pack_payload.get("id") or path.stem).strip() or path.stem
    version = str(pack_payload.get("version") or pack_payload.get("pack_version") or "unversioned")
    schema_version = str(pack_payload.get("schema_version") or (EVAL_PACK_SCHEMA_VERSION if pack_payload.get("pack_id") or pack_payload.get("version") or pack_payload.get("pack_version") else "legacy-json-eval-file-v1"))
    _validate_eval_pack_contract(pack_payload, schema_version, pack_id)
    fingerprint = _eval_pack_fingerprint(pack_payload)
    file_sha256 = _eval_file_sha256(path)
    production_policy = _production_policy_from_pack(pack_payload, schema_version)
    declared_fp = pack_payload.get("fingerprint_sha256") or pack_payload.get("fingerprint")
    if declared_fp and str(declared_fp) != fingerprint:
        raise ValueError(f"eval pack {pack_id} fingerprint mismatch")
    pack_meta = {
        "pack_id": pack_id,
        "version": version,
        "schema_version": schema_version,
        "fingerprint_sha256": fingerprint,
        "eval_file_sha256": file_sha256,
        "production_policy": production_policy,
        "production_policy_fingerprint_sha256": production_policy.get("policy_fingerprint_sha256"),
        "task_origin": pack_payload.get("task_origin") or ("sample-eval-pack" if pack_payload.get("sample_pack") else "curated"),
        "sample_pack": bool(pack_payload.get("sample_pack")),
    }
    tasks = [_task_from_record(r, str(path), i, pack_meta) for i, r in enumerate(records, 1)]
    if schema_version == EVAL_PACK_SCHEMA_VERSION or pack_payload.get("require_complete_splits"):
        split_counts = _validate_eval_pack_tasks(tasks, pack_id)
    else:
        split_counts = {s: sum(1 for t in tasks if _SPLIT_ALIASES.get(t.split, t.split) == s) for s in ("train", "val", "test")}
    metadata = EvalPackMetadata(
        pack_id=pack_id,
        version=version,
        schema_version=schema_version,
        path=str(path),
        fingerprint_sha256=fingerprint,
        eval_file_sha256=file_sha256,
        task_count=len(tasks),
        split_counts=split_counts,
        production_eligible_task_count=sum(1 for t in tasks if production_eligibility_for_task(t).eligible),
        production_policy=production_policy,
        production_policy_fingerprint_sha256=production_policy.get("policy_fingerprint_sha256"),
    )
    return tasks, metadata


def load_eval_tasks(path: Path) -> list[EvalTask]:
    return load_eval_pack(path)[0]


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
        curated_tasks: list[EvalTask] = []
        eval_pack_metadata: dict[str, Any] | None = None
        if eval_path:
            curated_tasks, eval_pack = load_eval_pack(eval_path)
            eval_pack_metadata = eval_pack.as_dict()
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
            "eval_pack": eval_pack_metadata,
            "eval_pack_id": eval_pack_metadata.get("pack_id") if eval_pack_metadata else None,
            "eval_pack_version": eval_pack_metadata.get("version") if eval_pack_metadata else None,
            "eval_pack_fingerprint_sha256": eval_pack_metadata.get("fingerprint_sha256") if eval_pack_metadata else None,
            "eval_pack_split_counts": eval_pack_metadata.get("split_counts") if eval_pack_metadata else None,
            "split_governance": {
                "train": "optimizer reflection/update evidence only",
                "validation": "candidate selection and deterministic inner gate",
                "test": "held-out final gate; never used for candidate selection",
                "no_leakage": "eval pack validator rejects duplicate ids/prompts across splits for v1 packs",
            },
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
