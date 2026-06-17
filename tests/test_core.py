from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import core
from hermes_skillopt.env import EvalTask
from hermes_skillopt.optimizer import OptimizerBackend, analyze_rollout_reflections, summarize_rejected_edits


@pytest.fixture(autouse=True)
def active_tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def make_skill(home: Path, name="demo", body="Use tools safely.") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return p


def full_run_with_deterministic_prod_optimizer(*args, **kwargs):
    """Deterministic test hook that is not production mock provenance."""

    class DeterministicProdBackend(core.LLMBackend):
        def __init__(self, *a, **k): pass
        mode = "hermes"
        def json(self, prompt, schema_hint, repair_path=None):
            kind = schema_hint.get("kind")
            if kind == "reflect":
                return {"recurring_defects": ["insufficient verification after edits"], "missing_rules": ["state expected artifacts and run tests before final"]}
            if kind == "edit":
                return {"edits": [{"op": "append", "text": "\n\n## SkillOpt Learned Rules\n\n- Verify changes with the most relevant command or test before reporting completion.\n- Preserve rollback safety and blocker handling.\n"}], "reasoning": "deterministic production-test hook"}
            if kind == "gate":
                return {"current_score": 0.45, "candidate_score": 0.82, "accepted": True, "rationale": "candidate adds verification rules"}
            return {}

    old = core.LLMBackend
    core.LLMBackend = DeterministicProdBackend  # type: ignore[assignment]
    try:
        kwargs["backend"] = "hermes"
        kwargs["allow_mock"] = False
        return core.full_run(*args, **kwargs)
    finally:
        core.LLMBackend = old  # type: ignore[assignment]


def test_skill_discovery_frontmatter(tmp_path):
    p = make_skill(tmp_path, "demo")
    skills = core.discover_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "demo"
    assert skills[0].path == p


def test_dry_run_stages_files_and_diff(tmp_path):
    make_skill(tmp_path, "demo")
    out = core.dry_run(skill="demo", goal="be safer", hermes_home_path=str(tmp_path))
    assert out["success"] is True
    run_dir = Path(out["run_dir"])
    for fn in ["manifest.json", "original_SKILL.md", "proposed_SKILL.md", "diff.patch", "report.md", "evidence.json"]:
        assert (run_dir / fn).exists()
    diff = (run_dir / "diff.patch").read_text(encoding="utf-8")
    assert "SkillOpt Candidate Improvements" in diff
    original = (run_dir / "original_SKILL.md").read_text(encoding="utf-8")
    proposed = (run_dir / "proposed_SKILL.md").read_text(encoding="utf-8")
    assert proposed.startswith("---\nname: demo")
    assert original != proposed
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["adoptable"] is False


def test_adopt_sha_guard_and_rollback(tmp_path):
    skill = make_skill(tmp_path, "demo")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_id = out["run_id"]
    skill.write_text(skill.read_text(encoding="utf-8") + "\nexternal edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha"):
        core.adopt(run_id, hermes_home_path=str(tmp_path))
    adopt = core.adopt(run_id, hermes_home_path=str(tmp_path), force=True)
    assert Path(adopt["backup_dir"]).exists()
    assert "SkillOpt Learned Rules" in skill.read_text(encoding="utf-8")
    rb = core.rollback(run_id, hermes_home_path=str(tmp_path))
    assert rb["status"] == "rolled_back"
    assert "external edit" in skill.read_text(encoding="utf-8")


def test_adopt_rejects_tampered_proposed_artifact_without_writing_target(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_dir = Path(out["run_dir"])
    (run_dir / "proposed_SKILL.md").write_text(original + "\ntampered staged artifact\n", encoding="utf-8")

    with pytest.raises(ValueError, match="proposed_SKILL.md sha"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)

    assert skill.read_text(encoding="utf-8") == original
    assert not any((tmp_path / "skillopt" / "backups").iterdir())


def test_adopt_rejects_cross_profile_home_without_unsafe_confirmation(tmp_path):
    other_home = tmp_path.parent / f"other-profile-{tmp_path.name}"
    skill = make_skill(other_home, "demo")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(other_home)
    out = core.full_run(skill="demo", hermes_home_path=str(other_home), eval_file=str(eval_path), backend="mock", allow_mock=True)

    with pytest.raises(ValueError, match="outside the active Hermes profile home"):
        core.adopt(out["run_id"], hermes_home_path=str(other_home))

    assert skill.read_text(encoding="utf-8") == original


def test_cross_profile_writeback_requires_explicit_unsafe_confirmation(tmp_path):
    other_home = tmp_path.parent / f"offline-profile-{tmp_path.name}"
    skill = make_skill(other_home, "demo")
    eval_path = write_eval_file(other_home)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(other_home), eval_file=str(eval_path))

    adopt = core.adopt(out["run_id"], hermes_home_path=str(other_home), unsafe_cross_profile=True)
    assert adopt["status"] == "adopted"
    rollback = core.rollback(out["run_id"], hermes_home_path=str(other_home), unsafe_cross_profile=True)
    assert rollback["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8").startswith("---\nname: demo")


def test_absolute_run_id_rejected(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(ValueError, match="Invalid run_id"):
        core.resolve_run_dir(tmp_path, "/tmp/evil")


def test_parent_traversal_run_id_rejected(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(ValueError, match="Invalid run_id"):
        core.resolve_run_dir(tmp_path, "../evil")


def test_tampered_manifest_skill_path_outside_home_rejected(tmp_path):
    make_skill(tmp_path, "demo")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_dir = Path(out["run_dir"])
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outside = tmp_path.parent / "outside_SKILL.md"
    outside.write_text("outside unchanged", encoding="utf-8")
    manifest["skill_path"] = str(outside)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="skill_path"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path))
    assert outside.read_text(encoding="utf-8") == "outside unchanged"


def test_rollback_refuses_current_skill_changed_after_adopt_unless_force(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_id = out["run_id"]
    core.adopt(run_id, hermes_home_path=str(tmp_path))
    skill.write_text(skill.read_text(encoding="utf-8") + "\nuser edit after adopt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="adopted state"):
        core.rollback(run_id, hermes_home_path=str(tmp_path))
    rb = core.rollback(run_id, hermes_home_path=str(tmp_path), force=True)
    assert rb["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8") == original


def test_rollback_refuses_tampered_backup_dir_outside_profile_without_writing_target(tmp_path):
    skill = make_skill(tmp_path, "demo")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_id = out["run_id"]
    core.adopt(run_id, hermes_home_path=str(tmp_path))
    adopted_text = skill.read_text(encoding="utf-8")

    outside_backup = tmp_path.parent / f"outside-backup-{tmp_path.name}"
    outside_backup.mkdir()
    (outside_backup / "SKILL.md").write_text("malicious external rollback content", encoding="utf-8")

    manifest_path = Path(out["run_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backup_dir"] = str(outside_backup)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="backup_dir"):
        core.rollback(run_id, hermes_home_path=str(tmp_path))

    assert skill.read_text(encoding="utf-8") == adopted_text


def test_rollback_refuses_backup_without_manifest_even_if_staging_sha_matches(tmp_path):
    skill = make_skill(tmp_path, "demo")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_id = out["run_id"]
    core.adopt(run_id, hermes_home_path=str(tmp_path))
    adopted_text = skill.read_text(encoding="utf-8")

    malicious_backup = tmp_path / "skillopt" / "backups" / "malicious-no-manifest"
    malicious_backup.mkdir(parents=True)
    malicious_text = "malicious rollback content"
    (malicious_backup / "SKILL.md").write_text(malicious_text, encoding="utf-8")

    manifest_path = Path(out["run_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backup_dir"] = str(malicious_backup)
    manifest["original_sha256"] = core.sha256_text(malicious_text)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Backup manifest missing"):
        core.rollback(run_id, hermes_home_path=str(tmp_path))

    assert skill.read_text(encoding="utf-8") == adopted_text


def test_secret_redaction():
    text = "api_key=sk-abcdefghijklmnopqrstuvwxyz token: gho_abcdefghijklmnopqrstuvwxyz password=hunter2"
    red = core.redact_secrets(text)
    assert "sk-" not in red
    assert "gho_" not in red
    assert "hunter2" not in red
    assert "<REDACTED>" in red


def test_upstream_status_no_network(tmp_path):
    out = core.upstream_status(hermes_home_path=str(tmp_path))
    assert out["success"] is True
    assert out["clone_exists"] is False
    assert out["upstream_url"].endswith("microsoft/SkillOpt.git")
    assert out["clone_path"] == str((tmp_path / "skillopt" / "upstream" / "SkillOpt").resolve())
    lock_data = json.loads((core.PLUGIN_ROOT / "skillopt_upstream.lock").read_text(encoding="utf-8"))
    assert out["current_lock_pin"] == lock_data["pinned_commit"]
    assert out["last_reviewed_upstream_commit"] == lock_data["last_reviewed_upstream_commit"]
    seams = {row["seam"] for row in out["feature_matrix"]}
    assert {
        "trainer_loop",
        "reflection_prompts",
        "skill_aware_reflection",
        "aggregate_clip",
        "gate",
        "artifact_resume",
        "benchmarks_tests",
        "benchmark_bridge",
        "transfer_eval",
        "conformance",
    } <= seams
    p3 = {row["seam"]: row for row in out["feature_matrix"] if row["seam"] in {"benchmark_bridge", "transfer_eval", "conformance"}}
    assert p3["benchmark_bridge"]["status"] == "p3_local_adapter"
    assert p3["transfer_eval"]["status"] == "p3_report_only"
    assert p3["conformance"]["status"] == "p3_local_contract"
    assert out["delta_checklist"] == out["feature_matrix"]


def test_upstream_status_reads_enriched_lock_metadata(tmp_path):
    lock = core.PLUGIN_ROOT / "skillopt_upstream.lock"
    original = lock.read_text(encoding="utf-8")
    try:
        lock.write_text(json.dumps({"pinned_commit": "pin", "last_reviewed_upstream_commit": "reviewed"}), encoding="utf-8")
        out = core.upstream_status(hermes_home_path=str(tmp_path))
        assert out["current_lock_pin"] == "pin"
        assert out["last_reviewed_upstream_commit"] == "reviewed"
    finally:
        lock.write_text(original, encoding="utf-8")


def test_upstream_status_rejects_noncanonical_repo_path_by_default(tmp_path):
    with pytest.raises(ValueError, match="canonical clone"):
        core.upstream_status(hermes_home_path=str(tmp_path), repo_path=str(tmp_path / "other"))


def test_upstream_status_internal_allow_repo_path_escape_hatch(tmp_path):
    other = tmp_path / "other"
    out = core.upstream_status(hermes_home_path=str(tmp_path), repo_path=str(other), allow_repo_path=True)
    assert out["success"] is True
    assert out["clone_path"] == str(other.resolve())
    assert out["clone_exists"] is False


def make_state_db(home: Path, secret: str = "api_key=sk-abcdefghijklmnopqrstuvwxyz") -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE messages (created_at TEXT, role TEXT, content TEXT)")
    con.execute("INSERT INTO messages VALUES (?, ?, ?)", ("2099-01-01T00:00:00+00:00", "user", f"demo skill failed with pytest error; {secret}"))
    con.execute("INSERT INTO messages VALUES (?, ?, ?)", ("2099-01-01T00:00:01+00:00", "assistant", "demo skill fixed, tests passed and verified"))
    con.commit(); con.close()


def test_harvest_from_temp_state_db_and_redaction(tmp_path):
    skill_path = make_skill(tmp_path, "demo")
    make_state_db(tmp_path)
    skill = core.find_skill(tmp_path, "demo")
    snippets = core.harvest_sessions(tmp_path, skill, query="demo", limit=5)
    assert snippets
    joined = json.dumps(snippets)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in joined
    assert "<REDACTED>" in joined


def test_split_deterministic_train_val_test(tmp_path):
    items = [{"id": f"i{i}", "evidence": str(i)} for i in range(8)]
    a = core.split_items(items)
    b = core.split_items(list(reversed(items)))
    assert a == b
    assert a["train"] and a["val"] and a["test"]


def test_full_run_mock_accepted_artifacts_and_review(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    make_state_db(tmp_path)
    out = core.full_run(skill="demo", query="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, iterations=2)
    assert out["status"] == "staged_best"
    run_dir = Path(out["run_dir"])
    for fn in ["manifest.json", "original_SKILL.md", "current_SKILL.md", "best_skill.md", "proposed_SKILL.md", "diff.patch", "report.md", "evidence.json", "train_items.jsonl", "val_items.jsonl", "test_items.jsonl", "reflections.json", "candidate_edits.json", "gate_results.json", "rejected_edits.jsonl", "target_binding.json", "provenance_binding.json"]:
        assert (run_dir / fn).exists(), fn
    assert "SkillOpt Learned Rules" in (run_dir / "best_skill.md").read_text(encoding="utf-8")
    review = core.review(out["run_id"], hermes_home_path=str(tmp_path))
    assert review["gate"]["candidate_score"] > review["gate"]["current_score"]
    assert review["accepted"] is True
    assert out["adoptable"] is False
    assert "mock optimizer" in "\n".join(out["not_adoptable_reasons"])
    with pytest.raises(ValueError, match="review-only"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)


def test_adopt_rejects_manifest_scrubbed_mock_provenance_even_with_recomputed_fingerprint(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    original_live = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "staged_best"
    assert manifest["production_gate_eligible"] is True
    assert manifest["test_gate_eligible"] is True
    assert manifest["adoptable"] is False

    clean_optimizer_config = dict(manifest["optimizer_backend_config"])
    clean_optimizer_config.update({"backend": "hermes", "requested_backend": "hermes", "allow_mock": False})
    manifest.update({
        "adoptable": True,
        "backend": "hermes",
        "optimizer_backend": "hermes",
        "optimizer_backend_config": clean_optimizer_config,
        "optimizer_config": clean_optimizer_config,
        "production_eligibility_reasons": [],
    })

    from hermes_skillopt.env import EvalTask, is_production_gate_task

    def rows(name: str) -> list[dict]:
        return [json.loads(line) for line in (run_dir / manifest["files"][name]).read_text(encoding="utf-8").splitlines() if line.strip()]

    def task_from_row(row: dict) -> EvalTask:
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

    tasks = {split: [task_from_row(r) for r in rows(split)] for split in ("train", "val", "test")}
    evidence = json.loads((run_dir / manifest["files"]["evidence"]).read_text(encoding="utf-8"))
    test_results = json.loads((run_dir / manifest["files"]["test_results"]).read_text(encoding="utf-8"))
    production_gate_available = bool([t for t in tasks["val"] if is_production_gate_task(t)]) and bool(evidence.get("production_gate_eligible"))
    production_test_results = [r for r in (test_results.get("results") or []) if isinstance(r, dict) and isinstance(r.get("metadata"), dict) and r["metadata"].get("production_gate_eligible")]
    test_gate_eligible = bool(production_test_results) and all(float(r.get("score", 0.0)) >= 0.55 and bool(r.get("passed")) for r in production_test_results)
    manifest["production_eval_policy"] = core._production_eval_policy(evidence, production_gate_available, test_gate_eligible)
    manifest["provenance_fingerprint"] = core._provenance_fingerprint(
        eval_file_used=evidence.get("eval_file"),
        tasks=tasks,
        backend_mode="hermes",
        target_executor_mode=str(manifest.get("target_executor")),
        target_config_id=str(manifest.get("target_config_id")),
        production_gate_available=production_gate_available,
        home=tmp_path,
        skill_relpath=manifest.get("skill_relpath"),
        original_sha256=core.sha256_text((run_dir / manifest["files"]["original"]).read_text(encoding="utf-8")),
        proposed_sha256=core.sha256_text((run_dir / manifest["files"]["proposed"]).read_text(encoding="utf-8")),
        optimizer_config=clean_optimizer_config,
        target_config=manifest.get("target_backend_config"),
        gate_policy=manifest.get("gate_policy"),
        production_eval_policy=manifest["production_eval_policy"],
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Mock/non-production optimizer provenance"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)
    assert skill.read_text(encoding="utf-8") == original_live


def test_rejected_gate_writes_rejected_buffer(tmp_path):
    make_skill(tmp_path, "demo", body="Verify tests, guard paths, redact secrets, handle error tool failures, rollback safely, artifact report.")
    # no-op append makes candidate equal current after strip? Use direct gate/apply path via monkey patch.
    class BadBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no useful edits"}
            return {"current_score": .9, "candidate_score": .1, "rationale": "bad"}
    old = core.LLMBackend
    core.LLMBackend = lambda *a, **k: BadBackend()  # type: ignore[assignment]
    try:
        out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    finally:
        core.LLMBackend = old  # type: ignore[assignment]
    assert out["status"] == "rejected"
    run_dir = Path(out["run_dir"])
    rej = run_dir / "rejected_edits.jsonl"
    assert rej.exists() and rej.read_text(encoding="utf-8").strip()
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["files"]["rejected_edits"] == "rejected_edits.jsonl"
    slow_meta = json.loads((run_dir / "slow_meta.json").read_text(encoding="utf-8"))
    assert slow_meta["artifact_role"] == "optimizer_memory_only_not_deployable_skill"
    assert slow_meta["deployed_skill_boundary"]["live_skill_content_included"] is False
    assert slow_meta["deployed_skill_boundary"]["optimizer_memory_must_not_be_deployed_as_skill"] is True
    assert slow_meta["epoch_stability_signals"]["rejected_candidate_count"] >= 1
    assert slow_meta["epoch_stability_signals"]["epochs"]
    assert (run_dir / "proposed_SKILL.md").read_text(encoding="utf-8") == (run_dir / "original_SKILL.md").read_text(encoding="utf-8")
    assert not (run_dir / "best_skill.md").exists()
    assert (run_dir / "diff.patch").read_text(encoding="utf-8") == ""


def test_full_run_resume_reuses_completed_checkpoint_and_refuses_mismatch(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    assert (run_dir / "checkpoint.json").exists()

    resumed = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True, resume_run_id=out["run_id"])
    assert resumed["resumed"] is True
    assert resumed["resume_reused"] is True
    assert resumed["run_id"] == out["run_id"]
    inspection = resumed["resume_inspection"]
    assert inspection["safe_reuse_completed"] is True
    assert inspection["partial_continuation_available"] is False
    assert all(s["fingerprints_present"] for s in inspection["stages"])

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True, edit_budget=2, resume_run_id=out["run_id"])


def test_resume_checkpoint_relative_eval_file_hashes_profile_local_file(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    eval_rel = eval_path.relative_to(tmp_path).as_posix()

    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=eval_rel, backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    input_payload = checkpoint["input"]
    original_sha = input_payload["eval_file_sha256"]

    assert input_payload["eval_file"] == str(eval_path.resolve())
    assert original_sha == core.sha256_file(eval_path)

    eval_path.write_text(
        json.dumps({"id": "v2", "prompt": "changed validation", "expected_keywords": ["changed"], "split": "validation"}) + "\n",
        encoding="utf-8",
    )
    assert core.sha256_file(eval_path) != original_sha

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=eval_rel, backend="mock", allow_mock=True, resume_run_id=out["run_id"])


def test_resume_checkpoint_default_eval_file_refuses_stale_actual_input(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "v1", "prompt": "default validation replay", "expected_keywords": ["verify"], "split": "validation"},
        {"id": "tr1", "prompt": "default train replay", "expected_keywords": ["tool"], "split": "train"},
        {"id": "te1", "prompt": "default test replay", "expected_keywords": ["rollback"], "split": "test"},
    ])
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    checkpoint = json.loads((Path(out["run_dir"]) / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["input"]["eval_file"] == str(eval_path.resolve())
    original_sha = checkpoint["input"]["eval_file_sha256"]

    write_eval_file(tmp_path, rows=[
        {"id": "v2", "prompt": "changed held-out validation replay", "expected_keywords": ["changed"], "split": "validation"},
        {"id": "tr2", "prompt": "changed train replay", "expected_keywords": ["tool"], "split": "train"},
        {"id": "te2", "prompt": "changed test replay", "expected_keywords": ["rollback"], "split": "test"},
    ])
    assert core.sha256_file(eval_path) != original_sha

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, resume_run_id=out["run_id"])


def test_resume_inspection_refuses_incomplete_or_unfingerprinted_stage(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    checkpoint["status"] = "running"
    (run_dir / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    stage = run_dir / "stages" / "001_rollout.json"
    row = json.loads(stage.read_text(encoding="utf-8"))
    row.pop("input_sha256", None)
    stage.write_text(json.dumps(row), encoding="utf-8")

    inspection = core.inspect_resume_run(out["run_id"], hermes_home_path=str(tmp_path))
    assert inspection["safe_reuse_completed"] is False
    assert inspection["partial_continuation_available"] is False
    assert any("missing input/output fingerprint" in reason for reason in inspection["refusal_reasons"])
    assert any("partial-stage continuation is refused" in reason for reason in inspection["refusal_reasons"])


def test_rejected_step_buffer_reused_by_next_candidate(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    prompts: list[str] = []

    class BufferBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        edit_calls = 0
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                prompts.append(prompt)
                BufferBackend.edit_calls += 1
                if BufferBackend.edit_calls == 1:
                    return {"edits": [{"op": "delete", "old": "missing-anchor"}], "reasoning": "bad anchor"}
                return {"edits": [{"op": "append", "text": "\n\n## SkillOpt Learned Rules\n\n- verify blocker rollback\n"}], "reasoning": "uses buffer"}
            return {"current_score": .1, "candidate_score": .9, "rationale": "explain only"}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: BufferBackend())
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, candidate_count=2)
    assert out["status"] == "staged_best"
    assert len(prompts) == 2
    assert "delete anchor must be unique" in prompts[1] or "non_unique_anchor" in prompts[1]


def test_protected_region_rejection_and_slow_meta_artifact(tmp_path):
    from hermes_skillopt.bounded_edit import validate_bounded_edits

    skill_text = "---\nname: demo\n---\n# demo\n\n<!-- skillopt:protected:start -->\nDo not change safety.\n<!-- skillopt:protected:end -->\n\n<!-- skillopt:allowed:start -->\nAllowed body.\n<!-- skillopt:allowed:end -->\n"
    result = validate_bounded_edits(skill_text, [{"op": "replace", "old": "Do not change safety.", "new": "weaken safety"}])
    assert result.ok is False
    assert any(r.get("reason") == "outside_allowed_region" or r.get("reason") == "protected_section" for r in result.rejected_edits)

    make_skill(tmp_path, "demo", body="Use tools safely.")
    live_before = (tmp_path / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    slow = json.loads((run_dir / "slow_meta.json").read_text(encoding="utf-8"))
    assert slow["mode"] == "evidence_only_no_live_write"
    assert slow["optimizer_memory_mode"] == "optimizer_only_evidence_no_live_write"
    assert slow["artifact_role"] == "optimizer_memory_only_not_deployable_skill"
    assert slow["normal_gate_required_for_any_write"] is True
    assert "optimizer_rejected_memory" in slow
    assert (tmp_path / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == live_before
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "slow_meta" in manifest["files"]
    assert "history" in manifest["files"]
    history = json.loads((run_dir / "history.json").read_text(encoding="utf-8"))
    assert history["schema_version"] == "skillopt-history-v1"
    assert history["parent_sha256"] == manifest["original_sha256"]
    assert history["proposed_sha256"] == manifest["proposed_sha256"]
    assert history["timeline"]
    assert history["candidates"]
    assert all("parent_sha256" in c and "accept_reject_reasons" in c for c in history["candidates"])
    core.verify_artifact_hashes(run_dir, manifest)


def test_append_cannot_replace_protected_heading_or_mutate_allowed_markers():
    from hermes_skillopt.bounded_edit import validate_bounded_edits

    skill_text = "---\nname: demo\n---\n# demo\n\n## Safety\n\nDo not weaken.\n\n## Notes\n\nAllowed notes.\n"
    result = validate_bounded_edits(skill_text, [{"op": "append", "text": "\n\n## Safety\n\nweaken safety\n"}])
    assert result.ok is False
    assert any(r.get("reason") == "protected_append" for r in result.rejected_edits)

    marked = "---\nname: demo\n---\n# demo\n\n<!-- skillopt:allowed:start -->\nAllowed body.\n<!-- skillopt:allowed:end -->\n"
    outside_append = validate_bounded_edits(marked, [{"op": "append", "text": "\n\n## New Section\n\noutside\n"}])
    marker_append = validate_bounded_edits("---\nname: demo\n---\n# demo\n", [{"op": "append", "text": "\n<!-- skillopt:allowed:start -->\nmove marker\n"}])
    assert outside_append.ok is False
    assert any(r.get("reason") == "outside_allowed_region" for r in outside_append.rejected_edits)
    assert marker_append.ok is False
    assert any(r.get("reason") == "allowed_region_marker_mutation" for r in marker_append.rejected_edits)


def test_auto_backend_requires_mock_permission_without_ctx(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(RuntimeError, match="allow_mock"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="auto", allow_mock=False)


def test_plugin_registration_includes_full_tool_schema():
    import importlib.util
    spec = importlib.util.spec_from_file_location("skillopt_plugin", Path(__file__).resolve().parents[1] / "__init__.py")
    plugin = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(plugin)
    names = []
    class Ctx:
        def register_tool(self, **kw): names.append((kw["name"], kw["schema"]))
    plugin.register(Ctx())
    assert "hermes_skillopt_full_run" in [n for n, _ in names]
    run_schema = dict(names)["hermes_skillopt_run"]
    assert "iterations" in run_schema["properties"]
    assert "backend" in run_schema["properties"]
    assert "eval_file" in run_schema["properties"]
    assert "auto_adopt" not in run_schema["properties"]
    update_schema = dict(names)["hermes_skillopt_upstream_update"]
    assert "repo_path" not in update_schema["properties"]
    status_schema = dict(names)["hermes_skillopt_upstream_status"]
    assert "repo_path" not in status_schema["properties"]
    adopt_schema = dict(names)["hermes_skillopt_adopt"]
    rollback_schema = dict(names)["hermes_skillopt_rollback"]
    assert "hermes_home" not in adopt_schema["properties"]
    assert "hermes_home" not in rollback_schema["properties"]


def test_plugin_writeback_handlers_ignore_home_override(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("skillopt_plugin_writeback", Path(__file__).resolve().parents[1] / "__init__.py")
    assert spec and spec.loader
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    calls = []
    monkeypatch.setattr(plugin.core, "adopt", lambda **kwargs: calls.append(("adopt", kwargs)) or {"success": True})
    monkeypatch.setattr(plugin.core, "rollback", lambda **kwargs: calls.append(("rollback", kwargs)) or {"success": True})

    plugin._handle_adopt({"run_id": "rid", "hermes_home": "/tmp/other", "force": True})
    plugin._handle_rollback({"run_id": "rid", "hermes_home": "/tmp/other", "force": True})
    assert calls == [
        ("adopt", {"run_id": "rid", "hermes_home_path": None, "force": True}),
        ("rollback", {"run_id": "rid", "hermes_home_path": None, "force": True}),
    ]


def test_plugin_yaml_provides_tools_matches_registered_tools():
    import importlib.util

    repo = Path(__file__).resolve().parents[1]
    plugin_yaml = repo / "plugin.yaml"
    provided = []
    in_tools = False
    for raw in plugin_yaml.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line == "provides_tools:":
            in_tools = True
            continue
        if in_tools and line.startswith("- "):
            provided.append(line[2:].strip())
        elif in_tools and line:
            break

    spec = importlib.util.spec_from_file_location("skillopt_plugin_yaml_check", repo / "__init__.py")
    plugin = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(plugin)
    registered = []
    class Ctx:
        def register_tool(self, **kw): registered.append(kw["name"])
    plugin.register(Ctx())

    assert "hermes_skillopt_handoff_optimize" in provided
    assert "hermes_skillopt_handoff_optimize" in registered
    assert provided == registered


def write_eval_file(home: Path, name: str = "demo", rows: list[dict] | None = None) -> Path:
    rows = rows or [
        {"id": "v1", "prompt": "held-out validation replay", "expected_keywords": ["verify", "blocker"], "forbidden_keywords": ["fabricate"], "split": "validation", "weight": 2, "production_gate_eligible": True},
        {"id": "tr1", "prompt": "train replay", "expected_keywords": ["tool"], "split": "train"},
        {"id": "te1", "prompt": "test replay", "success_criteria": ["rollback"], "expected_keywords": ["rollback"], "split": "test", "production_gate_eligible": True},
    ]
    needs_production_pack = any(bool(r.get("production_gate_eligible")) for r in rows)
    suffix = ".json" if needs_production_pack else ".jsonl"
    p = home / "skillopt" / "evals" / f"{name}{suffix}"
    p.parent.mkdir(parents=True, exist_ok=True)
    if needs_production_pack:
        split_alias = {"validation": "val", "val": "val", "train": "train", "test": "test"}
        present = {split_alias.get(str(r.get("split", "validation")).lower(), "val") for r in rows}
        supplement = []
        if "train" not in present:
            supplement.append({"id": "auto-train", "prompt": "auto train support", "expected_keywords": ["tool"], "split": "train"})
        if "val" not in present:
            supplement.append({"id": "auto-val", "prompt": "auto validation support", "expected_keywords": ["verify"], "split": "validation", "production_gate_eligible": True})
        if "test" not in present:
            supplement.append({"id": "auto-test", "prompt": "auto held-out test support", "expected_keywords": ["rollback"], "split": "test", "production_gate_eligible": True})
        payload = {
            "schema_version": "hermes-curated-eval-pack-v1",
            "pack_id": f"{name}-test-pack",
            "version": "test",
            "require_complete_splits": True,
            "production_policy": {"allow_production_adoption": True, "reviewed_by": "unit-test"},
            "tasks": rows + supplement,
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_eval_only_smoke_writes_report_without_training_or_adoption_side_effects(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely. verify blocker rollback tool")
    eval_path = write_eval_file(tmp_path)

    out = core.eval_only(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), target_executor="scorecard")

    assert out["success"] is True
    assert out["status"] == "eval_only_complete"
    assert out["adoptable"] is False
    run_dir = Path(out["run_dir"])
    assert (run_dir / "eval_report.json").exists()
    assert (run_dir / "evaluated_SKILL.md").exists()
    assert not (run_dir / "proposed_SKILL.md").exists()
    assert not (run_dir / "best_skill.md").exists()
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert report["mode"] == "eval_only_no_training_no_adoption"
    assert report["eval_file"] == str(eval_path.resolve())
    assert report["task_counts"] == {"train": 1, "val": 1, "test": 1}
    assert "split_results" in report
    assert manifest["files"]["eval_report"] == "eval_report.json"

    proc = subprocess.run(
        [sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "eval-only", "--skill", "demo", "--eval-file", str(eval_path), "--target-executor", "scorecard"],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout
    cli_out = json.loads(proc.stdout)
    assert cli_out["status"] == "eval_only_complete"
    assert cli_out["adoptable"] is False


def test_curated_eval_file_loaded_and_candidate_improvement_stages_best(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    current_eval = json.loads((run_dir / "current_validation_results.json").read_text(encoding="utf-8"))
    candidate_eval = json.loads((run_dir / "candidate_validation_results.json").read_text(encoding="utf-8"))
    assert out["status"] == "staged_best"
    assert manifest["eval_file"] == str(eval_path)
    assert manifest["curated_task_count"] == 3
    assert manifest["task_counts"]["val"] >= 1
    assert current_eval["results"][0]["task_id"] == "v1"
    assert candidate_eval["score"] > current_eval["score"]
    assert (run_dir / "best_skill.md").exists()


def test_review_returns_phase3_report_fields(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    review = core.review(out["run_id"], hermes_home_path=str(tmp_path))
    assert review["artifact_integrity"] == "verified"
    assert "report_fields" in review
    assert review["report_fields"]["timeline"]["status"] == review["status"]
    assert "eligibility" in review["report_fields"]
    assert "split_scores" in review["report_fields"]
    assert "candidate_comparison" in review["report_fields"]
    assert "regression_cases" in review["report_fields"]
    assert "provenance_security" in review["report_fields"]
    assert review["report_fields"]["provenance_security"]["artifact_integrity"] == "verified"


def test_curated_eval_json_object_file_supported(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = tmp_path / "skillopt" / "evals" / "demo.json"
    eval_path.parent.mkdir(parents=True, exist_ok=True)
    eval_path.write_text(json.dumps({"tasks": [{"id": "vjson", "prompt": "json validation", "expected_keywords": ["verify"], "split": "validation"}]}, indent=2), encoding="utf-8")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    current_eval = json.loads((run_dir / "current_validation_results.json").read_text(encoding="utf-8"))
    assert current_eval["results"][0]["task_id"] == "vjson"


def test_long_success_criteria_is_metadata_not_full_semantic_judge(tmp_path):
    from hermes_skillopt.env import load_eval_tasks

    eval_path = write_eval_file(tmp_path, rows=[{
        "id": "long-criteria",
        "prompt": "long natural-language criterion",
        "success_criteria": "The answer should carefully analyze tradeoffs, mention rollback only when relevant, and provide a nuanced operational plan.",
        "split": "validation",
    }])

    task = load_eval_tasks(eval_path)[0]
    assert task.success_criteria
    assert task.expected_terms == ()


def test_curated_eval_file_no_candidate_improvement_does_not_stage_best(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    eval_path = write_eval_file(tmp_path, rows=[{"id": "v1", "prompt": "validation", "expected_keywords": ["uniquegold"], "split": "validation"}])

    class NoEditBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no-op"}
            return {"accepted": True, "rationale": "aux only"}

    old = core.LLMBackend
    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: NoEditBackend())
    try:
        out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    finally:
        core.LLMBackend = old  # type: ignore[assignment]
    run_dir = Path(out["run_dir"])
    assert out["status"] == "rejected"
    assert not (run_dir / "best_skill.md").exists()
    gate_data = json.loads((run_dir / "gate_results.json").read_text(encoding="utf-8"))
    assert gate_data["gates"][0]["accepted"] is False
    assert gate_data["gates"][0]["current_eval"]["results"][0]["task_id"] == "v1"


def test_eval_file_path_guard_and_missing_errors(tmp_path):
    make_skill(tmp_path, "demo")
    with pytest.raises(FileNotFoundError, match="eval_file not found"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file="missing.jsonl", backend="mock", allow_mock=True)
    outside = tmp_path.parent / f"outside-eval-{tmp_path.name}.jsonl"
    outside.write_text('{"id":"v","prompt":"x","expected_keywords":["x"],"split":"validation"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="eval_file"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(outside), backend="mock", allow_mock=True)


def test_full_run_auto_adopt_disabled_even_for_temp_home(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="auto_adopt is disabled"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, auto_adopt=True)
    with pytest.raises(ValueError, match="auto_adopt cannot be combined with force"):
        core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True, auto_adopt=True, force=True)
    assert skill.read_text(encoding="utf-8") == original


def test_validation_gate_ignores_judge_acceptance_when_scores_regress():
    from hermes_skillopt.gate import ValidationGate

    decision = ValidationGate().decide(
        1,
        {"score": 0.9, "results": []},
        {"score": 0.1, "results": []},
        "current",
        "candidate",
        judge={"accepted": True, "candidate_score": 1.0, "rationale": "LLM likes it"},
    )
    assert decision.accepted is False
    assert decision.current_score == 0.9
    assert decision.candidate_score == 0.1


def test_full_run_staged_best_manifest_records_core_abstractions(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert out["status"] == "staged_best"
    assert (run_dir / "best_skill.md").exists()
    assert manifest["core_abstraction"]["skill_document"] == "trainable_state"
    assert manifest["core_abstraction"]["target_agent_model"] == "frozen_scorecard_replay_executor"
    assert manifest["core_abstraction"]["validation_gate"].startswith("sole_acceptance_gate")
    assert manifest["adoptable"] is False
    assert manifest["production_gate_eligible"] is False
    stage_dir = run_dir / "stages"
    assert {p.name.split("_", 1)[1] for p in stage_dir.glob("001_*.json")} == {
        "rollout.json", "reflect.json", "aggregate.json", "select.json", "update.json", "evaluate.json"
    }


def test_extended_eval_schema_and_hermes_replay_runner(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path, rows=[{
        "id": "v-replay",
        "prompt": "replay validation",
        "expected_behavior": "candidate mentions verification and blockers",
        "assertions": [{"type": "contains", "value": "verify"}, {"type": "contains", "value": "blocker"}],
        "judge": "hermes_replay_assertions",
        "allowed_tools": ["terminal"],
        "timeout": 10,
        "fixtures": {"error.txt": "tool failed"},
        "success_criteria": ["verify", "blocker"],
        "expected_keywords": ["verify", "blocker"],
        "split": "validation",
    }, {"id": "tr", "prompt": "train", "split": "train", "expected_keywords": ["tool"]}])
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    candidate_eval = json.loads((run_dir / "candidate_validation_results.json").read_text(encoding="utf-8"))
    assert manifest["target_executor"] == "hermes_trace_replay_runner_v1"
    assert manifest["target_config_id"] == "frozen-hermes-trace-replay-v2"
    assert candidate_eval["target_backend_config"]["parameters"]["trace_schema"] == "hermes-target-trace-v1"
    assert candidate_eval["results"][0]["metadata"].get("assertion_results") or candidate_eval["results"][0]["metadata"].get("assertion_count") == 2
    assert candidate_eval["results"][0]["metadata"]["trajectory"]["messages"]


def test_rejected_edit_history_enters_reflection_prompt(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    old_run = tmp_path / "skillopt" / "staging" / "old-demo"
    old_run.mkdir(parents=True)
    (old_run / "manifest.json").write_text(json.dumps({"skill_name": "demo"}), encoding="utf-8")
    (old_run / "rejected_edits.jsonl").write_text(json.dumps({"iteration": 1, "reasoning": "do not repeat me"}) + "\n", encoding="utf-8")
    prompts = []

    class CaptureBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            prompts.append(prompt)
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no-op"}
            return {"accepted": False, "rationale": "aux only"}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: CaptureBackend())
    core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    assert any("REJECTED_EDIT_HISTORY" in p and "do not repeat me" in p for p in prompts)


def test_rejected_edit_history_skips_missing_malformed_and_mismatched_manifests(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    staging = tmp_path / "skillopt" / "staging"
    cases = {
        "missing-manifest": (None, "missing manifest contam"),
        "malformed-manifest": ("{not json", "malformed manifest contam"),
        "list-manifest": (json.dumps([{"skill_name": "demo"}]), "list manifest contam"),
        "wrong-skill": (json.dumps({"skill_name": "other"}), "wrong skill contam"),
        "valid-demo": (json.dumps({"skill_name": "demo"}), "valid history"),
    }
    for dirname, (manifest_text, rejected_reason) in cases.items():
        run = staging / dirname
        run.mkdir(parents=True)
        if manifest_text is not None:
            (run / "manifest.json").write_text(manifest_text, encoding="utf-8")
        (run / "rejected_edits.jsonl").write_text(json.dumps({"iteration": 1, "reasoning": rejected_reason}) + "\n", encoding="utf-8")

    prompts = []

    class CaptureBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            prompts.append(prompt)
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [], "reasoning": "no-op"}
            return {"accepted": False, "rationale": "aux only"}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: CaptureBackend())
    core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    joined = "\n".join(prompts)
    assert "valid history" in joined
    assert "missing manifest contam" not in joined
    assert "malformed manifest contam" not in joined
    assert "list manifest contam" not in joined
    assert "wrong skill contam" not in joined


def test_legacy_dry_run_manifest_refuses_adopt_even_with_force(tmp_path):
    skill = make_skill(tmp_path, "demo")
    original = skill.read_text(encoding="utf-8")
    out = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    with pytest.raises(ValueError, match="review-only"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)
    assert skill.read_text(encoding="utf-8") == original


def test_full_run_with_explicit_curated_scorecard_is_adoptable(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    original = skill.read_text(encoding="utf-8")
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    assert out["adoptable"] is True
    adopt = core.adopt(out["run_id"], hermes_home_path=str(tmp_path))
    assert adopt["status"] == "adopted"
    assert "SkillOpt Learned Rules" in skill.read_text(encoding="utf-8")
    rb = core.rollback(out["run_id"], hermes_home_path=str(tmp_path))
    assert rb["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8") == original


def test_mixed_validation_nonproduction_improvement_cannot_make_adoptable(monkeypatch, tmp_path):
    from hermes_skillopt.env import EvalTask, HermesSkillEnv

    make_skill(tmp_path, "demo", body="Use tools safely. uniquegold")
    eval_source = str(tmp_path / "skillopt" / "evals" / "demo.jsonl")

    def mixed_tasks(self):
        curated = EvalTask(
            id="curated-stable",
            prompt="curated production validation",
            source=eval_source,
            expected_terms=("uniquegold",),
            split="val",
            metadata={"scorecard_explicit": True, "production_gate_eligible": True},
        )
        mined = EvalTask(
            id="session-lift",
            prompt="session mined review-only validation",
            source="session-mined",
            expected_terms=("verify",),
            split="val",
            metadata={"production_gate_eligible": False},
        )
        train = EvalTask(id="train", prompt="train", source="curated-fallback", expected_terms=("tool",), split="train")
        test = EvalTask(id="test", prompt="test", source="synthetic", expected_terms=("rollback",), split="test")
        return {"train": [train], "val": [curated, mined], "test": [test]}, {
            "snippets": [],
            "items": [],
            "abstraction": "environment/benchmark",
            "eval_file": eval_source,
            "curated_task_count": 1,
            "task_counts": {"train": 1, "val": 2, "test": 1},
            "production_gate_task_count": 1,
            "production_gate_eligible": True,
        }

    monkeypatch.setattr(HermesSkillEnv, "build_tasks", mixed_tasks)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path))
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    gate_data = json.loads((run_dir / "gate_results.json").read_text(encoding="utf-8"))
    assert out["status"] == "staged_best"
    assert out["adoptable"] is False
    assert manifest["adoptable"] is False
    assert manifest["production_gate_eligible"] is False
    assert gate_data["best_gate"]["accepted"] is True
    assert gate_data["production_best_gate"] is None
    with pytest.raises(ValueError, match="Only adoptable"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path))


def test_stage_recorder_writes_all_six_stage_artifacts(tmp_path):
    from hermes_skillopt.trainer import StageRecorder

    stages = StageRecorder(tmp_path)
    stages.rollout(1, {"score": 0.1, "num_tasks": 1, "executor": "mock"})
    stages.reflect(1, {"recurring_defects": []}, 0)
    stages.aggregate(1, {"recurring_defects": []}, 3)
    stages.select(1, {"edits": [{"op": "append"}], "bounded": True})
    stages.update(1, "abc123", True)
    stages.evaluate(1, {"score": 0.2}, {"score": 0.3}, {"accepted": True})
    assert {p.name for p in (tmp_path / "stages").glob("001_*.json")} == {
        "001_rollout.json", "001_reflect.json", "001_aggregate.json", "001_select.json", "001_update.json", "001_evaluate.json"
    }
    for p in (tmp_path / "stages").glob("001_*.json"):
        row = json.loads(p.read_text(encoding="utf-8"))
        assert row["schema_version"] == "skillopt-stage-v1"
        assert row["input_sha256"]
        assert row["output_sha256"]


def test_discover_skills_rejects_symlink_escape_before_read(tmp_path):
    outside = tmp_path.parent / f"outside-skill-{tmp_path.name}.md"
    outside.write_text("---\nname: escaped\n---\nsecret outside profile\n", encoding="utf-8")
    link_dir = tmp_path / "skills" / "escaped"
    link_dir.mkdir(parents=True)
    (link_dir / "SKILL.md").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        core.discover_skills(tmp_path)


def test_upstream_update_rejects_noncanonical_repo_path(tmp_path):
    with pytest.raises(ValueError, match="canonical clone"):
        core.upstream_update(hermes_home_path=str(tmp_path), repo_path=str(tmp_path / "other"), fetch_only=True)


def test_cli_help_commands_smoke():
    repo = Path(__file__).resolve().parents[1]
    for cmd in (
        [sys.executable, "-m", "hermes_skillopt.cli", "--help"],
        [sys.executable, "-m", "hermes_skillopt.cli", "full-run", "--help"],
        [sys.executable, "-m", "hermes_skillopt.cli", "webui", "--help"],
        [sys.executable, "-m", "hermes_skillopt.cli", "handoff-optimize", "--help"],
    ):
        proc = subprocess.run(cmd, cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        assert proc.returncode == 0
        assert "usage:" in proc.stdout.lower()
        if "full-run" in cmd:
            assert "--eval-file" in proc.stdout
            assert "--dry-run" not in proc.stdout
            assert "--target-executor" in proc.stdout
            assert "--target-backend" in proc.stdout
            assert "--optimizer-backend" in proc.stdout
            assert "--gate-mode" in proc.stdout
            assert "--candidate-count" in proc.stdout


def test_backend_separation_configs_and_fingerprints(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(
        skill="demo",
        hermes_home_path=str(tmp_path),
        eval_file=str(eval_path),
        backend="auto",
        optimizer_backend="mock",
        allow_mock=True,
        target_backend="scorecard",
        gate_mode="mixed",
    )
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    prov = manifest["provenance_fingerprint"]

    assert manifest["optimizer_backend_config"]["backend"] == "mock"
    assert manifest["optimizer_backend_config"]["requested_backend"] == "mock"
    assert manifest["target_backend_config"]["executor"] == "deterministic_mock_scorecard"
    assert manifest["target_backend_config"]["requested_executor"] == "scorecard"
    assert manifest["gate_policy"]["mode"] == "mixed"
    assert prov["optimizer_backend_config"] == manifest["optimizer_backend_config"]
    assert prov["target_backend_config"] == manifest["target_backend_config"]
    assert prov["gate_policy"] == manifest["gate_policy"]
    assert prov["optimizer_fingerprint_sha256"]
    assert prov["target_fingerprint_sha256"] == manifest["target_backend_config"]["fingerprint_sha256"]
    assert prov["gate_policy_fingerprint_sha256"]
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["input"]["target_config_id"] == manifest["target_config_id"]
    assert checkpoint["input"]["target_trace_schema"] == manifest["target_backend_config"]["parameters"]["trace_schema"]
    gate = manifest["gate"]
    assert gate["current_eval"]["target_fingerprint_sha256"] == gate["candidate_eval"]["target_fingerprint_sha256"]
    assert gate["current_eval"]["target_backend_config"] == gate["candidate_eval"]["target_backend_config"]


def test_validation_gate_hard_soft_mixed_modes_ignore_llm_override():
    from hermes_skillopt.gate import ValidationGate

    current = {
        "score": 0.5,
        "results": [
            {"score": 0.8, "passed": True, "metadata": {"weight": 1}},
            {"score": 0.2, "passed": False, "metadata": {"weight": 1}},
        ],
    }
    candidate_soft_only = {
        "score": 0.6,
        "results": [
            {"score": 0.9, "passed": True, "metadata": {"weight": 1}},
            {"score": 0.3, "passed": False, "metadata": {"weight": 1}},
        ],
    }
    candidate_hard = {
        "score": 0.55,
        "results": [
            {"score": 0.6, "passed": True, "metadata": {"weight": 1}},
            {"score": 0.5, "passed": True, "metadata": {"weight": 1}},
        ],
    }
    regressed = {"score": 0.1, "results": [{"score": 0.1, "passed": False, "metadata": {"weight": 1}}]}

    assert ValidationGate(gate_mode="soft").decide(1, current, candidate_soft_only, "a", "b").accepted is True
    assert ValidationGate(gate_mode="hard").decide(1, current, candidate_soft_only, "a", "b").accepted is False
    hard_decision = ValidationGate(gate_mode="hard").decide(1, current, candidate_hard, "a", "b")
    assert hard_decision.accepted is True
    mixed_decision = ValidationGate(gate_mode="mixed").decide(1, current, candidate_hard, "a", "b")
    assert mixed_decision.accepted is True
    rejected = ValidationGate(gate_mode="soft").decide(1, current, regressed, "a", "b", judge={"accepted": True})
    assert rejected.accepted is False
    assert rejected.as_dict()["metric_summary"]["soft_delta"] < 0
    assert "explanation-only" in rejected.as_dict()["acceptance_rule"]


def test_validation_gate_hard_mixed_report_and_block_per_task_regression():
    from hermes_skillopt.gate import ValidationGate

    current = {"score": 0.5, "results": [
        {"task_id": "keep", "score": 1.0, "passed": True, "metadata": {"weight": 1}},
        {"task_id": "gain1", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
        {"task_id": "gain2", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
    ]}
    candidate = {"score": 0.8, "results": [
        {"task_id": "keep", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
        {"task_id": "gain1", "score": 1.0, "passed": True, "metadata": {"weight": 1}},
        {"task_id": "gain2", "score": 1.0, "passed": True, "metadata": {"weight": 1}},
    ]}

    hard = ValidationGate(gate_mode="hard").decide(1, current, candidate, "a", "b")
    mixed = ValidationGate(gate_mode="mixed").decide(1, current, candidate, "a", "b")
    assert hard.accepted is False
    assert mixed.accepted is False
    assert hard.metric_summary is not None
    assert hard.metric_summary["hard_delta"] > 0
    assert hard.metric_summary["per_task_regressions"][0]["task_id"] == "keep"
    assert "previously passing task regressed" in hard.rationale
    assert "hard pass-rate regressed" in mixed.rationale


def test_validation_gate_strict_rejects_soft_improved_hard_or_per_task_regression():
    from hermes_skillopt.gate import ValidationGate

    current = {"score": 0.5, "results": [
        {"task_id": "keep", "score": 1.0, "passed": True, "metadata": {"weight": 1}},
        {"task_id": "miss", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
    ]}
    hard_regressed = {"score": 0.7, "results": [
        {"task_id": "keep", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
        {"task_id": "miss", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
    ]}
    per_task_regressed = {"score": 0.8, "results": [
        {"task_id": "keep", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
        {"task_id": "miss", "score": 1.0, "passed": True, "metadata": {"weight": 1}},
    ]}

    hard_decision = ValidationGate(gate_mode="strict").decide(1, current, hard_regressed, "a", "b")
    per_task_decision = ValidationGate(gate_mode="strict").decide(1, current, per_task_regressed, "a", "b")

    assert hard_decision.accepted is False
    assert "hard weighted pass-rate regressed" in hard_decision.rationale
    assert per_task_decision.accepted is False
    assert per_task_decision.metric_summary is not None
    assert per_task_decision.metric_summary["hard_delta"] == 0.0
    assert per_task_decision.metric_summary["per_task_regressions"][0]["task_id"] == "keep"
    assert "previously passing task regressed" in per_task_decision.rationale
    assert per_task_decision.as_dict()["metric_policy"]["mode"] == "strict"
    assert "soft" not in per_task_decision.as_dict()["acceptance_rule"].split("deterministic ", 1)[1].split(" metric", 1)[0]


def test_validation_gate_strict_accepts_soft_improvement_with_hard_nonregression():
    from hermes_skillopt.gate import ValidationGate

    current = {"score": 0.5, "results": [
        {"task_id": "keep", "score": 1.0, "passed": True, "metadata": {"weight": 1}},
        {"task_id": "miss", "score": 0.0, "passed": False, "metadata": {"weight": 1}},
    ]}
    candidate = {"score": 0.6, "results": [
        {"task_id": "keep", "score": 1.0, "passed": True, "metadata": {"weight": 1}},
        {"task_id": "miss", "score": 0.2, "passed": False, "metadata": {"weight": 1}},
    ]}

    decision = ValidationGate(gate_mode="strict", min_delta=0.05).decide(1, current, candidate, "a", "b")

    assert decision.accepted is True
    payload = decision.as_dict()
    assert payload["metric_policy"]["mode"] == "strict"
    assert payload["metric_policy"]["requested_mode"] == "strict"
    assert payload["metric_summary"]["soft_delta"] == 0.1
    assert payload["metric_summary"]["hard_delta"] == 0.0
    assert payload["metric_summary"]["strict"]["hard_nonregression"] is True
    assert "deterministic strict metric gate" in payload["acceptance_rule"]


def test_validation_gate_unsupported_modes_still_error():
    from hermes_skillopt.gate import ValidationGate

    with pytest.raises(ValueError, match="unsupported gate mode"):
        ValidationGate(gate_mode="lenient").decide(1, {"score": 0.1}, {"score": 0.2}, "a", "b")


def test_review_report_records_policy_fingerprints_and_per_task_delta(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    review = core.review(out["run_id"], hermes_home_path=str(tmp_path))

    assert manifest["production_eval_policy"]["policy_version"] == "production-eval-schema-v1"
    assert manifest["production_eval_policy"]["policy_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["eval_file_sha256"] == core.sha256_file(eval_path)
    assert manifest["provenance_fingerprint"]["fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["schema_version"] == "skillopt-provenance-v2"
    assert manifest["provenance_fingerprint"]["algorithm_version"] == manifest["algorithm_version"] == core.ALGORITHM_VERSION
    assert manifest["optimizer_backend_config"]["prompt_fingerprints"]
    assert manifest["optimizer_backend_config"]["prompt_fingerprint_sha256"] == manifest["provenance_fingerprint"]["optimizer_prompt_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["optimizer_prompt_fingerprints"][0]["prompt_sha256"]
    assert manifest["optimizer_backend_config"]["sampling"]["deterministic"] is True
    assert "optimizer_prompt_fingerprint" in report
    assert "algorithm_version" in report
    assert manifest["provenance_fingerprint"]["plugin_repo"]["repo_path"] == str(core.PLUGIN_ROOT.resolve())
    assert "commit" in manifest["provenance_fingerprint"]["plugin_repo"]
    assert "sha256" in manifest["provenance_fingerprint"]["upstream_lock"]
    assert manifest["provenance_fingerprint"]["eval_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["optimizer_config"] == manifest["optimizer_config"]
    assert manifest["provenance_fingerprint"]["optimizer_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["target_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["profile"]["hermes_home"] == str(tmp_path.resolve())
    assert manifest["provenance_fingerprint"]["profile_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["skill"] == {
        "skill_relpath": "skills/demo/SKILL.md",
        "original_sha256": manifest["original_sha256"],
        "proposed_sha256": manifest["proposed_sha256"],
    }
    assert manifest["provenance_fingerprint"]["skill_fingerprint_sha256"]
    assert manifest["provenance_fingerprint"]["production_eval_policy_fingerprint_sha256"] == manifest["production_eval_policy"]["policy_fingerprint_sha256"]
    assert manifest["per_task_delta"] and manifest["per_task_delta"][0]["task_id"] == "v1"
    assert review["per_task_delta"] == manifest["per_task_delta"]
    assert "baseline/current/candidate/best/test" in report
    assert "not_adoptable_checklist" in report
    assert "provenance_fingerprint" in report
    assert "production_eval_policy_fingerprint" in report
    assert "optimizer_fingerprint" in report
    assert "profile_fingerprint" in report


def test_manifest_only_tamper_cannot_make_nonproduction_run_adoptable(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "v1", "prompt": "validation", "expected_keywords": ["verify", "blocker"], "split": "validation", "production_gate_eligible": False},
        {"id": "tr1", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "te1", "prompt": "test", "expected_keywords": ["rollback"], "split": "test", "production_gate_eligible": False},
    ])
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "staged_best"
    assert manifest["adoptable"] is False
    assert manifest["production_gate_eligible"] is False

    manifest["adoptable"] = True
    manifest["production_gate_eligible"] = True
    manifest["test_gate_eligible"] = True
    manifest["production_gate"] = {
        "accepted": True,
        "current_score": 0.0,
        "candidate_score": 1.0,
        "rationale": "manifest-only tamper",
    }
    manifest["production_eval_policy"] = dict(manifest["production_eval_policy"], production_gate_available=True, test_gate_eligible=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="review-only"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)

    assert skill.read_text(encoding="utf-8") == original


def test_eval_record_can_opt_out_of_production_adopt(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "v1", "prompt": "validation", "expected_keywords": ["verify", "blocker"], "split": "validation", "production_gate_eligible": False},
        {"id": "tr1", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "te1", "prompt": "test", "expected_keywords": ["rollback"], "split": "test"},
    ])
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    assert out["status"] == "staged_best"
    assert out["adoptable"] is False
    assert out["production_gate_eligible"] is False
    with pytest.raises(ValueError, match="Only adoptable"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path))


def test_force_cannot_bypass_missing_production_gate(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "v1", "prompt": "validation", "expected_keywords": ["verify", "blocker"], "split": "validation", "production_gate_eligible": False},
        {"id": "tr1", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "te1", "prompt": "test", "expected_keywords": ["rollback"], "split": "test", "production_gate_eligible": False},
    ])
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    assert out["status"] == "staged_best"
    assert out["adoptable"] is False
    with pytest.raises(ValueError, match="Only adoptable"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)
    assert skill.read_text(encoding="utf-8") == original


def test_fallback_session_synthetic_mock_run_is_review_only_even_with_force(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    original = skill.read_text(encoding="utf-8")
    make_state_db(tmp_path)
    out = core.full_run(skill="demo", query="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    assert out["status"] == "staged_best"
    assert out["adoptable"] is False
    assert out["production_gate_eligible"] is False
    with pytest.raises(ValueError, match="review-only"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)
    assert skill.read_text(encoding="utf-8") == original


def test_multi_candidate_rank_select_buffers_rejected_candidates(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)

    class MultiBackend(core.LLMBackend):
        def __init__(self):
            self.edit_prompts: list[str] = []
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": ["need validation"]}
            if schema_hint["kind"] == "edit":
                self.edit_prompts.append(prompt)
                if "CANDIDATE_INDEX=1" in prompt:
                    return {"edits": [{"op": "append", "text": "\n\n## Weak candidate\n\n- mention tool only.\n"}], "reasoning": "weak"}
                return {"edits": [{"op": "append", "text": "\n\n## Strong candidate\n\n- verify blockers and rollback with tool checks.\n"}], "reasoning": "strong"}
            return {"rationale": "aux"}

    backend = MultiBackend()
    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: backend)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True, candidate_count=2)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    rejected = (run_dir / "rejected_edits.jsonl").read_text(encoding="utf-8")
    summary = json.loads((run_dir / "candidate_summary.json").read_text(encoding="utf-8"))

    assert manifest["candidate_count"] == 2
    assert summary["rounds"][0]["selected_candidate_id"] == "candidate-1-2"
    ranked = summary["rounds"][0]["ranked_candidates"]
    assert summary["rounds"][0]["selected_candidate_rationale"]
    assert all("metric_summary" in row and "rejection_reasons" in row and "rank" in row for row in ranked)
    assert any(row["selected"] is True for row in ranked)
    assert "candidate-1-1" in rejected and "selection_rejection" in rejected
    assert len(backend.edit_prompts) == 2
    assert "candidate-1-1" in backend.edit_prompts[1]
    assert "Weak candidate" in backend.edit_prompts[1]
    assert "Strong candidate" in (run_dir / "best_skill.md").read_text(encoding="utf-8")


def test_optimizer_reflection_separates_success_failure_and_labels_lapses():
    tasks = [
        EvalTask(id="skill-miss", prompt="p", expected_terms=("verify",), split="train"),
        EvalTask(id="tool-lapse", prompt="p", expected_terms=("tool",), split="train"),
        EvalTask(id="ok", prompt="p", expected_terms=("tool",), split="train"),
    ]
    current_eval = {
        "results": [
            {"task_id": "skill-miss", "score": 0.2, "passed": False, "feedback": "failed=['expected_keyword:verify']"},
            {"task_id": "tool-lapse", "score": 0.0, "passed": False, "feedback": "timeout blocked tool error", "metadata": {"sandbox_command_blocked": True}},
            {"task_id": "ok", "score": 0.8, "passed": True, "feedback": "passed=['expected_keyword:tool']"},
        ]
    }

    reflection = analyze_rollout_reflections(tasks, current_eval)

    assert [r["label"] for r in reflection["failure_reflections"]] == ["skill_defect", "execution_lapse"]
    assert reflection["success_reflections"][0]["task_id"] == "ok"
    assert reflection["reflection_counts"] == {"skill_defect": 1, "execution_lapse": 1, "success": 1}


def test_optimizer_budget_clip_and_rejected_filter_are_deterministic(tmp_path):
    current = "---\nname: demo\n---\n# demo\n\nBase.\n"
    rejected_edit = {"op": "append", "text": "\n\n## Bad prior\n\n- avoid repeating this.\n"}
    rejected_context = summarize_rejected_edits([{"iteration": 0, "candidate_id": "old", "edits": [rejected_edit], "gate": {"rejection_reasons": ["non-selected"]}}])

    class Backend:
        mode = "hermes"
        def json(self, prompt, schema_hint, repair_path=None):
            assert "REJECTED_EDIT_HISTORY" in prompt
            return {
                "edits": [
                    rejected_edit,
                    {"op": "append", "text": "\n\n## First kept\n\n- verify blocker.\n"},
                    {"op": "append", "text": "\n\n## Clipped\n\n- rollback.\n"},
                ],
                "reasoning": "fixture",
            }

    opt = OptimizerBackend(Backend(), edit_budget=1)
    cand1 = opt.propose_candidate({"recurring_defects": ["x"]}, current, tmp_path, 1, 1, rejected_context=rejected_context)
    cand2 = opt.propose_candidate({"recurring_defects": ["x"]}, current, tmp_path, 1, 1, rejected_context=rejected_context)

    assert cand1.text == cand2.text
    assert len(cand1.edits) == 1
    assert "First kept" in cand1.text
    assert "Bad prior" not in cand1.text
    aggregate = cand1.validation["aggregate"]
    assert aggregate["filtered_rejected_count"] == 1
    assert aggregate["clipped_count"] == 1
    assert [r["reason"] for r in aggregate["rejected"]] == ["previously_rejected", "edit_budget_clip"]


def test_production_gate_aware_selection_prefers_adoptable_candidate(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "prod-val", "prompt": "production validation", "expected_keywords": ["prodgold"], "split": "validation", "production_gate_eligible": True},
        {"id": "generic-one", "prompt": "generic validation one", "expected_keywords": ["alphaone"], "split": "validation", "production_gate_eligible": False},
        {"id": "generic-two", "prompt": "generic validation two", "expected_keywords": ["alphatwo"], "split": "validation", "production_gate_eligible": False},
        {"id": "train", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "prod-test", "prompt": "production test", "expected_keywords": ["prodgold"], "split": "test", "production_gate_eligible": True},
    ])

    class ProductionAwareBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "hermes"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": ["choose production-safe edit"]}
            if schema_hint["kind"] == "edit":
                if "CANDIDATE_INDEX=1" in prompt:
                    return {"edits": [{"op": "append", "text": "\n\n## Generic candidate\n\n- alphaone alphatwo validation gate.\n"}], "reasoning": "generic only"}
                return {"edits": [{"op": "append", "text": "\n\n## Production candidate\n\n- prodgold production gate.\n"}], "reasoning": "production safe"}
            return {"rationale": "aux"}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: ProductionAwareBackend())
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="hermes", allow_mock=False, candidate_count=2)
    run_dir = Path(out["run_dir"])
    summary = json.loads((run_dir / "candidate_summary.json").read_text(encoding="utf-8"))
    ranked = summary["rounds"][0]["ranked_candidates"]
    rejected = (run_dir / "rejected_edits.jsonl").read_text(encoding="utf-8")

    assert summary["rounds"][0]["selected_candidate_id"] == "candidate-1-2"
    assert ranked[0]["production_accepted"] is False
    assert ranked[1]["production_accepted"] is True
    assert ranked[0]["candidate_score"] > ranked[1]["candidate_score"]
    assert out["adoptable"] is True
    assert "candidate-1-1" in rejected and "selection_rejection" in rejected
    assert "Production candidate" in (run_dir / "best_skill.md").read_text(encoding="utf-8")


def test_full_run_review_adopt_rollback_e2e_and_adopt_tamper_guards(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    original = skill.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    review = core.review(out["run_id"], hermes_home_path=str(tmp_path))
    assert review["adoptable"] is True
    adopt = core.adopt(out["run_id"], hermes_home_path=str(tmp_path))
    assert adopt["status"] == "adopted"
    assert skill.read_text(encoding="utf-8") != original
    rollback = core.rollback(out["run_id"], hermes_home_path=str(tmp_path))
    assert rollback["status"] == "rolled_back"
    assert skill.read_text(encoding="utf-8") == original

    out2 = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_dir = Path(out2["run_dir"])
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["production_gate_eligible"] = False
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="production eligible"):
        core.adopt(out2["run_id"], hermes_home_path=str(tmp_path))


def test_adopt_rejects_manifest_retarget_even_with_force(tmp_path):
    skill = make_skill(tmp_path, "demo", body="Use tools safely.")
    other = make_skill(tmp_path, "other", body="Other skill unchanged.")
    other_original = other.read_text(encoding="utf-8")
    eval_path = write_eval_file(tmp_path)
    out = full_run_with_deterministic_prod_optimizer(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path))
    run_dir = Path(out["run_dir"])
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skill_name"] = "other"
    manifest["skill_relpath"] = "skills/other/SKILL.md"
    manifest["skill_path"] = str(other)
    manifest["original_sha256"] = core.sha256_text(other_original)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="verified artifacts"):
        core.adopt(out["run_id"], hermes_home_path=str(tmp_path), force=True)
    assert other.read_text(encoding="utf-8") == other_original
    assert skill.read_text(encoding="utf-8") != (run_dir / "proposed_SKILL.md").read_text(encoding="utf-8")


def test_heldout_test_results_and_artifact_hashes_are_recorded_and_verified(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path)
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert (run_dir / "test_results.json").exists()
    assert manifest["test_gate_eligible"] is True
    assert manifest["artifact_sha256"]["test_results"] == core.sha256_file(run_dir / "test_results.json")
    assert core.review(out["run_id"], hermes_home_path=str(tmp_path))["artifact_integrity"] == "verified"
    (run_dir / "test_results.json").write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Artifact hash mismatch for test_results"):
        core.review(out["run_id"], hermes_home_path=str(tmp_path))


def test_sandbox_target_executor_is_isolated_and_records_transcript(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "train", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "val", "prompt": "validation", "expected_keywords": ["verify", "blocker"], "split": "validation", "executor": "sandbox", "required_markers": ["SANDBOX_OK"]},
        {"id": "test", "prompt": "test", "expected_keywords": ["rollback"], "split": "test", "executor": "sandbox", "required_markers": ["SANDBOX_OK"]},
    ])
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True, target_executor="sandbox")
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    test_results = json.loads((run_dir / "test_results.json").read_text(encoding="utf-8"))
    assert manifest["target_executor"] == "hermes_sandbox_executor_mvp"
    meta = test_results["results"][0]["metadata"]
    assert meta["sandbox_isolated"] is True
    assert meta["live_profile_writes"] is False
    assert meta["sandbox_command_blocked"] is False
    assert "SANDBOX_OK" in meta["transcript_preview"]


def test_sandbox_executor_blocks_task_command_live_profile_write(tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")
    live_write = tmp_path / "skills" / "demo" / "PWNED_BY_SANDBOX_COMMAND.txt"
    malicious = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(live_write)!r}).write_text('pwned', encoding='utf-8'); print('SANDBOX_OK')",
    ]
    eval_path = write_eval_file(tmp_path, rows=[
        {"id": "train", "prompt": "train", "expected_keywords": ["tool"], "split": "train"},
        {"id": "malicious-val", "prompt": "validation", "expected_keywords": ["verify"], "split": "validation", "executor": "sandbox", "fixtures": {"command": malicious}, "required_markers": ["SANDBOX_OK"]},
        {"id": "test", "prompt": "test", "expected_keywords": ["rollback"], "split": "test", "executor": "sandbox", "required_markers": ["SANDBOX_OK"]},
    ])

    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), eval_file=str(eval_path), backend="mock", allow_mock=True, target_executor="sandbox")
    run_dir = Path(out["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    candidate_eval = json.loads((run_dir / "candidate_validation_results.json").read_text(encoding="utf-8"))
    meta = candidate_eval["results"][0]["metadata"]

    assert not live_write.exists()
    assert manifest["adoptable"] is False
    assert manifest["production_gate_eligible"] is False
    assert candidate_eval["production_gate_eligible"] is False
    assert candidate_eval["results"][0]["passed"] is False
    assert meta["exit_code"] == 126
    assert meta["sandbox_command_blocked"] is True
    assert meta["live_profile_writes"] is False
    assert meta["production_gate_eligible"] is False
    assert "SANDBOX_COMMAND_BLOCKED" in meta["transcript_preview"]


def test_invalid_optimizer_edit_is_rejected_and_review_only(monkeypatch, tmp_path):
    make_skill(tmp_path, "demo", body="Use tools safely.")

    class InvalidBackend(core.LLMBackend):
        def __init__(self): pass
        mode = "mock"
        def json(self, prompt, schema_hint, repair_path=None):
            if schema_hint["kind"] == "reflect":
                return {"recurring_defects": []}
            if schema_hint["kind"] == "edit":
                return {"edits": [{"op": "replace", "old": "---\nname: demo", "new": "---\nname: pwn"}], "reasoning": "bad"}
            return {"accepted": True}

    monkeypatch.setattr(core, "LLMBackend", lambda *a, **k: InvalidBackend())
    out = core.full_run(skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)
    run_dir = Path(out["run_dir"])
    assert out["status"] == "rejected"
    rejected = (run_dir / "rejected_edits.jsonl").read_text(encoding="utf-8")
    assert "protected_frontmatter" in rejected
    assert (run_dir / "candidate_1_edit_validation_rejected.json").exists()
