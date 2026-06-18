from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

from hermes_skillopt import core
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


def test_upstream_import_invalid_manifest_does_not_create_output(tmp_path):
    manifest = tmp_path / "missing-test-split.json"
    manifest.write_text(json.dumps({"tasks": [
        {"id": "train", "split": "train", "prompt": "Train task", "expected_terms": ["train"]},
        {"id": "val", "split": "val", "prompt": "Val task", "expected_terms": ["val"]},
    ]}), encoding="utf-8")
    out = tmp_path / "should-not-exist.json"

    with pytest.raises(ValueError, match="must include train/val/test tasks"):
        import_upstream_manifest(manifest, out)

    assert not out.exists()


def test_upstream_import_invalid_manifest_preserves_existing_output(tmp_path):
    manifest = tmp_path / "missing-test-split.json"
    manifest.write_text(json.dumps({"tasks": [
        {"id": "train", "split": "train", "prompt": "Train task", "expected_terms": ["train"]},
        {"id": "val", "split": "val", "prompt": "Val task", "expected_terms": ["val"]},
    ]}), encoding="utf-8")
    out = tmp_path / "existing-pack.json"
    sentinel = "sentinel: keep this existing file intact\n"
    out.write_text(sentinel, encoding="utf-8")

    with pytest.raises(ValueError, match="must include train/val/test tasks"):
        import_upstream_manifest(manifest, out)

    assert out.read_text(encoding="utf-8") == sentinel


def test_upstream_import_success_atomically_replaces_existing_output(tmp_path):
    manifest = tmp_path / "valid-upstream.json"
    manifest.write_text(json.dumps({"tasks": [
        {"id": "train", "split": "train", "prompt": "Train task", "expected_terms": ["train"]},
        {"id": "val", "split": "val", "prompt": "Val task", "expected_terms": ["val"]},
        {"id": "test", "split": "test", "prompt": "Test task", "expected_terms": ["test"]},
    ]}), encoding="utf-8")
    out = tmp_path / "existing-pack.json"
    out.write_text("old contents\n", encoding="utf-8")

    result = import_upstream_manifest(manifest, out)

    assert result["success"] is True
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["pack_id"] == "valid-upstream"
    assert payload["tasks"][0]["id"] == "train"


def test_upstream_import_output_guard_blocks_live_and_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manifest = tmp_path / "valid-upstream.json"
    manifest.write_text(json.dumps({"tasks": [
        {"id": "train", "split": "train", "prompt": "Train task", "expected_terms": ["train"]},
        {"id": "val", "split": "val", "prompt": "Val task", "expected_terms": ["val"]},
        {"id": "test", "split": "test", "prompt": "Test task", "expected_terms": ["test"]},
    ]}), encoding="utf-8")
    skill = make_skill(tmp_path)

    blocked = [
        skill,
        tmp_path / "plugins" / "tool.json",
        tmp_path / "cron" / "job.json",
        tmp_path / "memories" / "memory.json",
    ]
    for out in blocked:
        with pytest.raises(ValueError, match="output_path"):
            import_upstream_manifest(manifest, out)


def test_upstream_import_output_guard_allows_explicit_eval_staging(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manifest = tmp_path / "valid-upstream.json"
    manifest.write_text(json.dumps({"tasks": [
        {"id": "train", "split": "train", "prompt": "Train task", "expected_terms": ["train"]},
        {"id": "val", "split": "val", "prompt": "Val task", "expected_terms": ["val"]},
        {"id": "test", "split": "test", "prompt": "Test task", "expected_terms": ["test"]},
    ]}), encoding="utf-8")
    out = tmp_path / "skillopt" / "evals" / "safe-pack.json"

    result = import_upstream_manifest(manifest, out)

    assert result["success"] is True
    assert out.exists()


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


def test_upstream_import_rejects_remote_network_fields_and_values(tmp_path):
    for name, payload in {
        "endpoint": {"tasks": [{"id": "x", "split": "train", "prompt": "x", "expected_terms": ["x"], "endpoint": "/v1/run"}]},
        "url_value": {"tasks": [{"id": "x", "split": "train", "prompt": "x", "expected_terms": ["x"], "metadata": {"reference": "https://example.invalid/bench.json"}}]},
    }.items():
        manifest = tmp_path / f"{name}.json"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="executable/remote fields"):
            import_upstream_manifest(manifest)


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


def test_transfer_eval_report_output_cannot_target_live_skills(tmp_path):
    skill = make_skill(tmp_path)
    eval_pack = write_eval_pack(tmp_path)
    staged = tmp_path / "staged_SKILL.md"
    staged.write_text(skill.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="report-only"):
        transfer_eval(hermes_home_path=str(tmp_path), skill_file=str(staged), eval_file=str(eval_pack), output_path=str(skill), staged_only=False)

    with pytest.raises(ValueError, match="report-only"):
        transfer_eval(hermes_home_path=str(tmp_path), skill_file=str(staged), eval_file=str(eval_pack), output_path=str(tmp_path / "skills" / "demo" / "transfer-report.json"), staged_only=False)


def test_benchmark_parity_status_does_not_claim_true_upstream_execution(tmp_path):
    result = core.benchmark_parity_status(str(tmp_path))

    assert result["success"] is True
    assert result["parity_label"] == "Hermes-native benchmark mode; not an upstream SkillOpt benchmark result"
    assert result["unsupported_parity_levels"]["true_upstream_benchmark_execution"].startswith("unsupported")
    assert result["upstream_benchmark_parity"]["true_benchmark_execution"]["supported"] is False
    assert result["supported_parity_levels"]["hermes_eval_pack_replay"] == "supported_native_not_upstream_parity"


def test_review_slim_returns_artifact_refs_without_large_previews(tmp_path):
    run_id = "20260618-review-slim"
    run_dir = tmp_path / "skillopt" / "staging" / run_id
    run_dir.mkdir(parents=True)
    diff = "diff --git a/SKILL.md b/SKILL.md\n" + "+line\n" * 2000
    report = "# Report\n" + "trace line\n" * 2000
    (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "status": "staged_best",
        "skill_name": "demo",
        "files": {"diff": "diff.patch", "report": "report.md"},
        "artifact_sha256": {
            "diff": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "report": hashlib.sha256(report.encode("utf-8")).hexdigest(),
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = core.review(run_id, hermes_home_path=str(tmp_path), slim=True)

    assert result["slim"] is True
    assert result["diff_preview"] == ""
    assert result["report_summary"] == ""
    assert result["artifact_refs"]["diff"]["path"].endswith("diff.patch")
    assert result["artifact_refs"]["diff"]["sha256"] == hashlib.sha256(diff.encode("utf-8")).hexdigest()
    assert result["artifact_refs"]["report"]["bytes"] == len(report.encode("utf-8"))


def test_conformance_report_generation(tmp_path):
    report_path = tmp_path / "conformance.json"
    result = run_conformance(repo_root=Path(__file__).resolve().parents[1], output_path=report_path, pytest_args=["tests/test_p3.py::test_transfer_eval_defaults_to_staged_input"], timeout=60)

    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "hermes-skillopt-conformance-v1"
    assert report["mode"] == "quick"
    assert report["quick_is_full_repo_health"] is False
    assert "not necessarily a full repository health" in report["scope_note"]
    assert report["external_services_required"] is False
    assert len(report["commands"]) == 2
    assert all(command["output_tail_sha256"] for command in report["commands"])
    assert all("output_tail_chars" in command and "output_truncated" in command for command in report["commands"])
    assert result["success"] is True


def test_conformance_report_output_cannot_target_live_runtime_paths(tmp_path, monkeypatch):
    import hermes_skillopt.conformance as conformance

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(conformance, "_run", lambda cmd, cwd, timeout: {"cmd": cmd, "returncode": 0, "passed": True, "output_tail": "", "output_tail_sha256": hashlib.sha256(b"").hexdigest()})

    with pytest.raises(ValueError, match="report-only"):
        conformance.run_conformance(repo_root=Path(__file__).resolve().parents[1], output_path=tmp_path / "skills" / "demo" / "conformance.json")


def test_conformance_full_mode_uses_all_tests_when_no_pytest_override(tmp_path, monkeypatch):
    import hermes_skillopt.conformance as conformance

    calls = []

    def fake_run(cmd, cwd, timeout):
        calls.append(cmd)
        return {"cmd": cmd, "returncode": 0, "passed": True, "output_tail": ""}

    monkeypatch.setattr(conformance, "_run", fake_run)
    result = conformance.run_conformance(repo_root=Path(__file__).resolve().parents[1], output_path=tmp_path / "full.json", mode="full")
    assert result["report"]["suite"] == "full-local-pytest"
    assert result["report"]["pytest_args"] == ["tests"]
    assert calls[1][-1] == "tests"


def test_plugin_metadata_matches_registered_tools_and_p3_tools_present():
    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("skillopt_plugin_root", repo / "__init__.py")
    assert spec and spec.loader
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    provided = [line.strip()[2:] for line in (repo / "plugin.yaml").read_text(encoding="utf-8").splitlines() if line.strip().startswith("- ")]
    registered = [name for name, _schema, _handler, _emoji in plugin._TOOLS]
    assert provided == registered
    assert {"hermes_skillopt_import_upstream_benchmark", "hermes_skillopt_transfer_eval", "hermes_skillopt_conformance"}.issubset(registered)
    assert "hermes_home" not in plugin.SCHEMAS["hermes_skillopt_upstream_update"]["parameters"]["properties"]


def test_p3_seam_matrix_entries_are_recorded():
    lock = json.loads((Path(__file__).resolve().parents[1] / "skillopt_upstream.lock").read_text(encoding="utf-8"))
    seams = lock["p3_seam_matrix"]
    assert set(seams) >= {"benchmark_bridge", "transfer_eval", "conformance", "webui_writeback"}
    assert "JSON/schema-only" in seams["benchmark_bridge"]
    assert "report-only/read-only" in seams["transfer_eval"]
