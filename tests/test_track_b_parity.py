from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_skillopt import core
from hermes_skillopt.env import JsonEvalPackBenchmarkAdapter
from hermes_skillopt.target import LiveHermesReadOnlyRunner


def _write_skill_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "hermes"
    skill_dir = home / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_text("verify validation gate bounded edit evidence staged rollback\n", encoding="utf-8")
    eval_dir = home / "skillopt" / "evals"
    eval_dir.mkdir(parents=True)
    pack = {
        "schema_version": "hermes-curated-eval-pack-v1",
        "pack_id": "track-b-pack",
        "version": "1.0.0",
        "sample_pack": True,
        "tasks": [
            {"id": "train-1", "split": "train", "prompt": "train", "expected_terms": ["verify"], "task_origin": "sample-eval-pack"},
            {"id": "val-1", "split": "val", "prompt": "val", "expected_terms": ["validation"], "task_origin": "sample-eval-pack"},
            {"id": "test-1", "split": "test", "prompt": "test", "expected_terms": ["staged"], "task_origin": "sample-eval-pack"},
        ],
    }
    eval_file = eval_dir / "demo.json"
    eval_file.write_text(json.dumps(pack), encoding="utf-8")
    return home, eval_file


def test_benchmark_parity_status_is_read_only_labelled(tmp_path: Path):
    out = core.benchmark_parity_status(str(tmp_path / "home"))
    assert out["success"] is True
    assert out["mode"] == "read_only_report_only_no_rollout_no_adopt"
    assert "not an upstream SkillOpt benchmark result" in out["parity_label"]
    assert "no Microsoft SkillOpt upstream benchmark parity" in out["reporting_boundary"]
    assert "not upstream execution parity" in out["upstream_benchmark_parity"]["import_only_bridge"]["parity_label"]
    assert out["hermes_benchmark_mode"]["production_gate"].startswith("only explicit curated")


def test_compare_upstream_pin_reports_without_claiming_merge(tmp_path: Path):
    out = core.compare_upstream_pin(str(tmp_path / "home"))
    assert out["success"] is True
    assert out["mode"] == "read_only_report_only_no_fetch_no_merge"
    assert out["true_upstream_execution_supported"] is False
    assert "no Microsoft SkillOpt upstream benchmark parity" in out["parity_label"]
    assert any("not vendored" in item for item in out["safety_invariants"])


def test_upstream_status_boundary_is_offline_status_only(tmp_path: Path):
    out = core.upstream_status(str(tmp_path / "home"))
    assert out["success"] is True
    assert out["mode"] == "offline_status_only_no_fetch_no_benchmark_execution"
    assert out["unsupported_true_upstream_execution"]["supported"] is False
    assert "no Microsoft SkillOpt upstream benchmark parity" in out["parity_label"]


def test_json_benchmark_adapter_and_live_readonly_eval(tmp_path: Path):
    home, eval_file = _write_skill_home(tmp_path)
    adapter = JsonEvalPackBenchmarkAdapter(eval_file)
    tasks, meta, governance = adapter.load()
    assert meta.pack_id == "track-b-pack"
    assert governance["leakage_diagnostics"]["passed"] is True
    assert {k: len(v) for k, v in tasks.items()} == {"train": 1, "val": 1, "test": 1}

    out = core.eval_only(skill="demo", eval_file=str(eval_file.relative_to(home)), hermes_home_path=str(home), target_executor="live-readonly")
    assert out["success"] is True
    report = json.loads(Path(out["eval_report_path"]).read_text(encoding="utf-8"))
    assert report["target_executor"] == "hermes_live_readonly_adapter_v1_disabled"
    assert report["benchmark_adapter"]["loader"]["writes_live_skills"] is False
    assert report["eval_pack_governance"]["leakage_diagnostics"]["passed"] is True


def test_live_runner_records_fingerprints_and_cannot_pass():
    from hermes_skillopt.env import EvalTask

    runner = LiveHermesReadOnlyRunner(provider="p", model="m", profile_home="/tmp/profile", toolset=("web",), session_id="s")
    result = runner.score("skill", EvalTask(id="t", prompt="prompt", split="val"))
    assert result.passed is False
    assert result.metadata["production_adopt_allowed"] is False
    assert result.metadata["target_adapter_config"]["report_only"] is True
    assert result.metadata["model_fingerprint"] == {"provider": "p", "model": "m"}


def test_writeback_lock_blocks_and_audits(tmp_path: Path):
    home = tmp_path / "home"
    core.ensure_dirs(home)
    lock = home / "skillopt" / "writeback.lock"
    lock.write_text("busy", encoding="utf-8")
    with pytest.raises(RuntimeError, match="writeback is in progress"):
        core.adopt("missing-run", hermes_home_path=str(home), unsafe_cross_profile=True)
    audit = home / "skillopt" / "writeback_audit.jsonl"
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["outcome"] == "blocked_lock_busy"
