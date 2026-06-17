from __future__ import annotations

"""Safe bridge from upstream-style benchmark manifests to Hermes eval packs.

This module only parses JSON data. It never imports modules, evals code, follows
network references, or executes benchmark-defined commands.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_skillopt.env import EVAL_PACK_SCHEMA_VERSION, load_eval_pack

UPSTREAM_BRIDGE_SCHEMA_VERSION = "hermes-upstream-benchmark-bridge-v1"
FORBIDDEN_EXECUTION_FIELDS = {
    "code",
    "script",
    "command",
    "commands",
    "shell",
    "python",
    "entrypoint",
    "module",
    "callable",
    "function",
    "dockerfile",
    "image",
    "url",
    "remote_url",
}
_SPLIT_ALIASES = {"validation": "val", "val": "val", "train": "train", "test": "test", "dev": "val", "eval": "val"}


@dataclass(frozen=True)
class UpstreamImportReport:
    source_path: str
    output_path: str | None
    pack_id: str
    version: str
    fingerprint_sha256: str
    source_fingerprint_sha256: str
    task_count: int
    split_counts: dict[str, int]
    production_eligible_task_count: int
    sample_pack: bool
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _stable_sha(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _walk_forbidden(value: Any, path: str = "$", found: list[str] | None = None) -> list[str]:
    found = found if found is not None else []
    if isinstance(value, dict):
        for key, child in value.items():
            key_s = str(key)
            child_path = f"{path}.{key_s}"
            if key_s.lower() in FORBIDDEN_EXECUTION_FIELDS:
                found.append(child_path)
            _walk_forbidden(child, child_path, found)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{i}]", found)
    return found


def _task_records_from_manifest(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("tasks"), list):
        return [dict(t) if isinstance(t, dict) else t for t in data["tasks"]]
    splits = data.get("splits") or data.get("split_manifests")
    if isinstance(splits, dict):
        records: list[dict[str, Any]] = []
        for split_name, items in splits.items():
            if not isinstance(items, list):
                raise ValueError(f"split {split_name!r} must be a list of task objects")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"split {split_name!r} contains a non-object task")
                rec = dict(item)
                rec.setdefault("split", split_name)
                records.append(rec)
        return records
    raise ValueError("upstream manifest must contain either tasks: [...] or splits: {train|val|test: [...]} data")


def _normalise_upstream_task(record: dict[str, Any], index: int, *, source_name: str, sample_pack: bool) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError(f"upstream task #{index} must be an object")
    prompt = str(record.get("prompt") or record.get("input") or record.get("instruction") or "").strip()
    if not prompt:
        raise ValueError(f"upstream task #{index} missing prompt/input/instruction")
    raw_split = str(record.get("split") or "validation").strip().lower()
    split = _SPLIT_ALIASES.get(raw_split)
    if split is None:
        raise ValueError(f"upstream task #{index} has invalid split: {raw_split}")
    task_id = str(record.get("id") or record.get("task_id") or f"upstream-{index}").strip()
    expected_terms = record.get("expected_terms") or record.get("expected_keywords") or record.get("keywords") or record.get("answers") or []
    assertions = record.get("assertions") or []
    if isinstance(expected_terms, str):
        expected_terms = [expected_terms]
    if not isinstance(assertions, list):
        raise ValueError(f"upstream task {task_id} assertions must be a list")
    if not expected_terms and not assertions and not record.get("expected_behavior"):
        raise ValueError(f"upstream task {task_id} missing deterministic expected_terms/assertions/expected_behavior")
    allowed_tools = record.get("allowed_tools") or []
    if isinstance(allowed_tools, str):
        allowed_tools = [allowed_tools]
    if not isinstance(allowed_tools, list):
        raise ValueError(f"upstream task {task_id} allowed_tools must be a list/string")
    return {
        "id": task_id,
        "prompt": prompt,
        "split": split,
        "expected_terms": expected_terms,
        "expected_behavior": str(record.get("expected_behavior") or ""),
        "assertions": assertions,
        "allowed_tools": allowed_tools,
        "failure_terms": record.get("forbidden_terms") or record.get("forbidden_keywords") or record.get("failure_terms") or [],
        "weight": record.get("weight", 1.0),
        "task_origin": "upstream-benchmark-sample" if sample_pack else "upstream-benchmark-curated",
        "upstream_source": source_name,
        "upstream_task_fingerprint_sha256": _stable_sha(record),
        "production_gate_eligible": bool(record.get("production_gate_eligible", False)) and not sample_pack,
    }


def import_upstream_manifest(input_path: str | Path, output_path: str | Path | None = None, *, pack_id: str | None = None, version: str | None = None, sample_pack: bool | None = None) -> dict[str, Any]:
    """Convert an upstream-style JSON benchmark manifest to a Hermes eval pack.

    The importer validates schema, split completeness/leakage (through
    ``load_eval_pack``), sample/production eligibility, and fingerprints. It only
    accepts embedded JSON task data; references to code/commands/URLs are refused.
    """

    path = Path(input_path).expanduser().resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("upstream manifest must be a JSON object")
    forbidden = _walk_forbidden(data)
    if forbidden:
        raise ValueError("upstream manifest contains executable/remote fields: " + ", ".join(forbidden[:8]))
    source_fp = _stable_sha(data)
    sample = bool(data.get("sample_pack", True if sample_pack is None else sample_pack)) if sample_pack is None else bool(sample_pack)
    records = _task_records_from_manifest(data)
    tasks = [_normalise_upstream_task(r, i, source_name=path.name, sample_pack=sample) for i, r in enumerate(records, 1)]
    payload = {
        "schema_version": EVAL_PACK_SCHEMA_VERSION,
        "pack_id": pack_id or str(data.get("pack_id") or data.get("benchmark_id") or data.get("name") or path.stem),
        "version": version or str(data.get("version") or "upstream-imported"),
        "sample_pack": sample,
        "task_origin": "sample-eval-pack" if sample else "curated",
        "upstream_bridge": {
            "schema_version": UPSTREAM_BRIDGE_SCHEMA_VERSION,
            "source_path": str(path),
            "source_fingerprint_sha256": source_fp,
            "safe_adapter": "json-only-no-code-execution",
        },
        "tasks": tasks,
    }
    payload["fingerprint_sha256"] = _stable_sha(payload)

    # Reuse Hermes eval-pack validation for complete splits, leakage and eligibility.
    tmp_path = Path(output_path).expanduser().resolve() if output_path else None
    validation_path = tmp_path or (path.parent / f".{path.stem}.hermes-eval-pack.tmp.json")
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        _, meta = load_eval_pack(validation_path)
    finally:
        if output_path is None:
            validation_path.unlink(missing_ok=True)
    if output_path:
        out_path = validation_path
    else:
        out_path = None
    report = UpstreamImportReport(
        source_path=str(path),
        output_path=str(out_path) if out_path else None,
        pack_id=meta.pack_id,
        version=meta.version,
        fingerprint_sha256=meta.fingerprint_sha256,
        source_fingerprint_sha256=source_fp,
        task_count=meta.task_count,
        split_counts=meta.split_counts,
        production_eligible_task_count=meta.production_eligible_task_count,
        sample_pack=sample,
        warnings=("sample_pack disables production_gate_eligible tasks",) if sample else (),
    )
    return {"success": True, "eval_pack": payload, "report": report.as_dict()}
