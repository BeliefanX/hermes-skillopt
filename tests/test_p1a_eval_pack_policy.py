from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_skillopt.env import (
    EVAL_PACK_SCHEMA_VERSION,
    EvalTask,
    is_production_gate_task,
    load_eval_pack,
    production_eligibility_for_task,
)


def _pack(*, allow_production: bool = True, sample_pack: bool = False, task_origin: str = "curated") -> dict:
    return {
        "schema_version": EVAL_PACK_SCHEMA_VERSION,
        "pack_id": "prod-curated-pack",
        "version": "2026.06.17",
        "task_origin": task_origin,
        "sample_pack": sample_pack,
        "require_complete_splits": True,
        "production_policy": {
            "allow_production_adoption": allow_production,
            "reviewed_by": "unit-test",
            "notes": "curated frozen eval pack fixture",
        },
        "tasks": [
            {
                "id": "train-001",
                "split": "train",
                "prompt": "Train on tool-use safety.",
                "expected_keywords": ["tool", "verify"],
                "production_gate_eligible": False,
            },
            {
                "id": "val-001",
                "split": "validation",
                "prompt": "Validate grounded tool-use before adoption.",
                "expected_keywords": ["grounded", "tool", "verify"],
                "forbidden_keywords": ["fabricate"],
                "production_gate_eligible": True,
            },
            {
                "id": "test-001",
                "split": "test",
                "prompt": "Held-out test covers rollback and manifest guards.",
                "expected_keywords": ["rollback", "manifest", "guard"],
                "production_gate_eligible": True,
            },
        ],
    }


def _write_pack(tmp_path: Path, payload: Any, name: str = "pack.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_explicit_curated_v1_pack_policy_allows_validation_gate_and_binds_provenance(tmp_path: Path):
    path = _write_pack(tmp_path, _pack())

    tasks, metadata = load_eval_pack(path)
    by_id = {task.id: task for task in tasks}

    assert metadata.schema_version == EVAL_PACK_SCHEMA_VERSION
    assert metadata.split_counts == {"train": 1, "val": 1, "test": 1}
    assert metadata.eval_file_sha256
    assert metadata.production_policy["allow_production_adoption"] is True
    assert metadata.production_policy_fingerprint_sha256 == metadata.production_policy["policy_fingerprint_sha256"]
    assert metadata.production_eligible_task_count == 2

    val_task = by_id["val-001"]
    test_task = by_id["test-001"]
    assert production_eligibility_for_task(val_task).eligible is True
    assert is_production_gate_task(val_task) is True
    assert production_eligibility_for_task(test_task).eligible is True
    assert is_production_gate_task(test_task) is False  # held-out test gates final pass, not candidate selection

    for task in (val_task, test_task):
        assert task.metadata["explicit_curated_eval_pack"] is True
        assert task.metadata["eval_pack_production_allowed"] is True
        assert task.metadata["eval_pack_fingerprint_sha256"] == metadata.fingerprint_sha256
        assert task.metadata["eval_pack_file_sha256"] == metadata.eval_file_sha256
        assert task.metadata["eval_pack_policy_fingerprint_sha256"] == metadata.production_policy_fingerprint_sha256


def test_curated_v1_pack_without_policy_opt_in_is_review_only(tmp_path: Path):
    path = _write_pack(tmp_path, _pack(allow_production=False))

    tasks, metadata = load_eval_pack(path)
    assert metadata.production_policy["allow_production_adoption"] is False
    assert metadata.production_eligible_task_count == 0
    assert all(not production_eligibility_for_task(task).eligible for task in tasks)
    assert all(not is_production_gate_task(task) for task in tasks)


def test_legacy_json_and_sample_pack_cannot_become_production_by_manifest_flags(tmp_path: Path):
    legacy_path = _write_pack(
        tmp_path,
        [
            {"id": "train", "split": "train", "prompt": "train", "expected_keywords": ["tool"]},
            {"id": "val", "split": "validation", "prompt": "val", "expected_keywords": ["verify"], "production_gate_eligible": True},
            {"id": "test", "split": "test", "prompt": "test", "expected_keywords": ["rollback"], "production_gate_eligible": True},
        ],
        name="legacy.json",
    )
    legacy_tasks, legacy_meta = load_eval_pack(legacy_path)
    assert legacy_meta.schema_version == "legacy-json-eval-list-v1"
    assert legacy_meta.production_eligible_task_count == 0
    assert all(not production_eligibility_for_task(task).eligible for task in legacy_tasks)

    sample_path = _write_pack(tmp_path, _pack(allow_production=True, sample_pack=True, task_origin="sample-eval-pack"), name="sample.json")
    sample_tasks, sample_meta = load_eval_pack(sample_path)
    assert sample_meta.production_policy["allow_production_adoption"] is False
    assert sample_meta.production_eligible_task_count == 0
    assert all(not is_production_gate_task(task) for task in sample_tasks)


def test_v1_pack_requires_complete_splits_and_rejects_split_leakage(tmp_path: Path):
    missing_test = _pack()
    missing_test["tasks"] = missing_test["tasks"][:2]
    with pytest.raises(ValueError, match="must include train/val/test"):
        load_eval_pack(_write_pack(tmp_path, missing_test, name="missing-test.json"))

    missing_val = _pack()
    missing_val["tasks"] = [task for task in missing_val["tasks"] if task["split"] != "validation"]
    with pytest.raises(ValueError, match="must include train/val/test"):
        load_eval_pack(_write_pack(tmp_path, missing_val, name="missing-val.json"))

    leaked = _pack()
    leaked["tasks"][2]["prompt"] = leaked["tasks"][1]["prompt"]
    with pytest.raises(ValueError, match="reuses an identical prompt"):
        load_eval_pack(_write_pack(tmp_path, leaked, name="leaked.json"))


def test_v1_pack_rejects_declared_fingerprint_tampering(tmp_path: Path):
    payload = _pack()
    payload["fingerprint_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        load_eval_pack(_write_pack(tmp_path, payload, name="tampered.json"))


def test_fallback_synthetic_and_session_mined_tasks_remain_non_production_even_with_flags():
    tasks = [
        EvalTask("fallback", "p", source="curated-fallback", split="val", expected_terms=("verify",), metadata={"task_origin": "curated-fallback", "scorecard_explicit": True, "production_gate_eligible": True}),
        EvalTask("synthetic", "p", source="synthetic", split="val", expected_terms=("verify",), metadata={"task_origin": "synthetic", "scorecard_explicit": True, "production_gate_eligible": True}),
        EvalTask("session", "p", source="session-mined", split="val", expected_terms=("verify",), metadata={"task_origin": "session-mined", "scorecard_explicit": True, "production_gate_eligible": True}),
    ]

    for task in tasks:
        decision = production_eligibility_for_task(task)
        assert decision.eligible is False
        assert not is_production_gate_task(task)
        assert any("review-only" in reason or "non-production" in reason for reason in decision.reasons)


def test_bundled_production_eval_packs_are_loadable_and_gate_eligible():
    examples_dir = Path(__file__).resolve().parents[1] / "examples" / "evals"
    pack_paths = sorted(examples_dir.glob("hermes_*_production_v1.json"))

    assert len(pack_paths) >= 2
    for path in pack_paths:
        tasks, metadata = load_eval_pack(path)
        assert metadata.schema_version == EVAL_PACK_SCHEMA_VERSION
        assert metadata.split_counts["train"] >= 1
        assert metadata.split_counts["val"] >= 1
        assert metadata.split_counts["test"] >= 1
        assert metadata.production_policy["allow_production_adoption"] is True
        assert metadata.production_eligible_task_count >= 2
        assert metadata.fingerprint_sha256
        assert metadata.production_policy_fingerprint_sha256
        assert any(is_production_gate_task(task) for task in tasks if task.split == "val")
        assert all(not is_production_gate_task(task) for task in tasks if task.split == "train")
