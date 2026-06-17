from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_skillopt.benchmark_bridge import import_upstream_manifest
from hermes_skillopt.conformance import run_conformance
from hermes_skillopt.transfer import transfer_eval


def make_skill(home: Path, name: str = "demo") -> Path:
    path = home / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: demo\ndescription: test\n---\n# demo\n\nUse tools and verify results.\n", encoding="utf-8")
    return path


def write_eval_pack(home: Path) -> Path:
    pack = {
        "schema_version": "hermes-curated-eval-pack-v1",
        "pack_id": "transfer-pack",
        "version": "1.0",
        "sample_pack": True,
        "tasks": [
            {"id": "train-1", "split": "train", "prompt": "Use tools safely", "expected_terms": ["tool", "verify"]},
            {"id": "val-1", "split": "val", "prompt": "Report verification", "expected_terms": ["verify"]},
            {"id": "test-1", "split": "test", "prompt": "Mention blockers", "expected_terms": ["blocker", "verify"]},
        ],
    }
    path = home / "skillopt" / "evals" / "transfer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack), encoding="utf-8")
    return path


def test_upstream_manifest_imports_to_hermes_eval_pack(tmp_path):
    upstream = {
        "benchmark_id": "upstream-demo",
        "version": "2026.06",
        "sample_pack": True,
        "splits": {
            "train": [{"task_id": "u-train", "instruction": "Use a tool", "keywords": ["tool"]}],
            "validation": [{"task_id": "u-val", "input": "Verify the result", "answers": ["verify"]}],
            "test": [{"task_id": "u-test", "prompt": "Report a blocker", "expected_terms": ["blocker"]}],
        },
    }
    manifest = tmp_path / "upstream.json"
    manifest.write_text(json.dumps(upstream), encoding="utf-8")
    out = tmp_path / "hermes-pack.json"

    result = import_upstream_manifest(manifest, out)

    assert result["success"] is True
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "hermes-curated-eval-pack-v1"
    assert payload["upstream_bridge"]["safe_adapter"] == "json-only-no-code-execution"
    assert result["report"]["split_counts"] == {"train": 1, "val": 1, "test": 1}
    assert result["report"]["sample_pack"] is True
    assert result["report"]["production_eligible_task_count"] == 0


def test_upstream_import_rejects_executable_fields_and_leakage(tmp_path):
    executable = tmp_path / "exec.json"
    executable.write_text(json.dumps({"tasks": [{"id": "x", "split": "train", "prompt": "x", "expected_terms": ["x"], "command": "rm -rf /"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="executable/remote fields"):
        import_upstream_manifest(executable)

    leaking = tmp_path / "leak.json"
    leaking.write_text(json.dumps({"tasks": [
        {"id": "same", "split": "train", "prompt": "same prompt", "expected_terms": ["a"]},
        {"id": "same", "split": "val", "prompt": "different", "expected_terms": ["b"]},
        {"id": "t", "split": "test", "prompt": "test prompt", "expected_terms": ["c"]},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="leaks task id"):
        import_upstream_manifest(leaking)


def test_transfer_eval_is_read_only_and_fingerprinted(tmp_path):
    skill = make_skill(tmp_path)
    original = skill.read_text(encoding="utf-8")
    eval_pack = write_eval_pack(tmp_path)
    staged = tmp_path / "staged_SKILL.md"
    staged.write_text(original + "\n- Always verify and report blocker status.\n", encoding="utf-8")
    report_path = tmp_path / "transfer-report.json"

    result = transfer_eval(
        hermes_home_path=str(tmp_path),
        skill_file=str(staged),
        eval_file=str(eval_pack),
        targets=("scorecard", "replay"),
        profile_homes=(str(tmp_path), str(tmp_path / "other-profile")),
        output_path=str(report_path),
        staged_only=False,
    )

    assert skill.read_text(encoding="utf-8") == original
    report = result["report"]
    assert report["mode"] == "report-only-read-only"
    assert report["live_skill_writeback"] is False
    assert report["report_fingerprint_sha256"]
    assert len(report["evaluations"]) == 4
    assert {e["target"] for e in report["evaluations"]} == {"scorecard", "replay"}
    assert all(e["profile_fingerprint"]["fingerprint_sha256"] for e in report["evaluations"])
    assert all(e["target_fingerprint_sha256"] for e in report["evaluations"])
    assert report_path.exists()


def test_transfer_eval_defaults_to_staged_input(tmp_path):
    make_skill(tmp_path)
    eval_pack = write_eval_pack(tmp_path)
    with pytest.raises(ValueError, match="staged/report-only"):
        transfer_eval(hermes_home_path=str(tmp_path), eval_file=str(eval_pack), skill_file=None)


def test_conformance_report_generation(tmp_path):
    report_path = tmp_path / "conformance.json"
    result = run_conformance(repo_root=Path(__file__).resolve().parents[1], output_path=report_path, pytest_args=["tests/test_p3.py::test_transfer_eval_defaults_to_staged_input"], timeout=60)

    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "hermes-skillopt-conformance-v1"
    assert report["external_services_required"] is False
    assert len(report["commands"]) == 2
    assert result["success"] is True
