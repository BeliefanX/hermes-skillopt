from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from hermes_skillopt import core
from hermes_skillopt.eval_packs import eval_pack_doctor_digest, eval_pack_inventory_digest
from hermes_skillopt.skill_quality import skill_quality_digest, skill_quality_report


DEFAULT_CRON_SAFE_TOOLS = {
    "hermes_skillopt_scout",
    "hermes_skillopt_doctor",
    "hermes_skillopt_eval_pack_inventory",
    "hermes_skillopt_eval_pack_doctor",
}


def make_skill(home: Path, name: str = "demo", text: str | None = None) -> Path:
    path = home / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or f"""---
name: {name}
description: A useful demo skill with enough detail for linting.
---
# Instructions
Use this skill when the user asks for a careful demo workflow.

# Triggers
Use when demo analysis, step-by-step validation, or safe tool use is needed.

# Steps
1. Inspect the request and relevant files.
2. Use tools only when needed.
3. Report concise evidence.

# Pitfalls
Do not fabricate results or skip validation.

# Verification
Verify outputs with tests or explicit checks and report blockers.
""", encoding="utf-8")
    return path


def load_plugin_module():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_skillopt_plugin_p2_p3", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_only_notification_digests_include_boundary_and_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_skill(tmp_path)

    scout_digest = core.notification_digest("scout", core.scout(str(tmp_path), skill="demo"))
    doctor_digest = core.notification_digest("doctor", core.doctor(str(tmp_path), skill="demo"))
    inv_digest = eval_pack_inventory_digest(hermes_home_path=str(tmp_path), skill="demo")
    epdoc_digest = eval_pack_doctor_digest(hermes_home_path=str(tmp_path), skill="demo")

    for digest in (scout_digest, doctor_digest, inv_digest, epdoc_digest):
        assert digest["read_only"] is True
        assert digest["auto_adopt"] is False
        assert digest["max_digest_chars"] <= 3500
        assert len(digest["digest"]) <= digest["max_digest_chars"]
        assert "scheduled usage is diagnostic only and never auto-adopts" in digest["digest"]
        assert "read_only: True | auto_adopt: False" in digest["digest"]
        assert "no writeback/fetch" in digest["digest"]
        assert "no adopt/rollback" in digest["digest"]
        assert "safe next action" in digest["digest"]
        assert digest["summary"]["read_only"] is True
        assert digest["summary"]["auto_adopt"] is False
        assert digest["summary"].get("live_skill_writes") is not True

    assert inv_digest["surface"] == "eval-pack-inventory"
    assert "eval_matrix:" in inv_digest["digest"]
    assert inv_digest["summary"]["mode"] == "eval_pack_inventory_read_only"
    assert inv_digest["summary"]["skills"][0]["recommended_next_action"]
    assert epdoc_digest["surface"] == "eval-pack-doctor"
    assert "diagnostics:" in epdoc_digest["digest"]
    assert epdoc_digest["summary"]["mode"] == "eval_pack_doctor_read_only"
    assert epdoc_digest["summary"]["recommended_next_action"] in {"write_review_draft_with_explicit_flag", "inspect_or_promote_review_pack"}


def test_notification_digest_bounds_rows_and_preserves_provenance_refs():
    payload = {
        "success": True,
        "mode": "synthetic_read_only",
        "read_only": True,
        "auto_adopt": False,
        "live_skill_writes": False,
        "next_actions": [
            {"priority": "high", "action": f"action-{i}", "reason": "x" * 500}
            for i in range(20)
        ],
        "score_provenance": {"target_executor": "scorecard", "optimizer_backend": "hermes", "score_source": "eval_pack"},
        "artifact_refs": {
            "report": {"path": "/tmp/report.md", "sha256": "a" * 64},
            "diff": {"path": "/tmp/diff.patch", "sha256": "b" * 64},
        },
        "next_safe_action": "inspect artifacts only; do not adopt",
    }

    digest = core.notification_digest("review_like", payload, limit=3)

    assert digest["read_only"] is True
    assert digest["auto_adopt"] is False
    assert len(digest["digest"]) <= digest["max_digest_chars"]
    assert digest["digest"].count("action-") == 3
    assert "action-3" not in digest["digest"]
    assert "score_provenance: executor=scorecard backend=hermes source=eval_pack" in digest["digest"]
    assert "artifact_refs:" in digest["digest"]
    assert "report=/tmp/report.md" in digest["digest"]
    assert "diff=/tmp/diff.patch" in digest["digest"]
    assert "no full-run/optimize" in digest["digest"]
    assert "no writeback/fetch" in digest["digest"]
    assert "no adopt/rollback" in digest["digest"]


def test_cli_digest_flags_for_read_only_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_skill(tmp_path)
    root = Path(__file__).resolve().parents[1]
    for cmd in (["scout", "--skill", "demo", "--digest"], ["doctor", "--skill", "demo", "--digest"], ["eval-pack-inventory", "--skill", "demo", "--digest"], ["eval-pack-doctor", "--skill", "demo", "--digest"]):
        proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), *cmd], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        assert proc.returncode == 0, proc.stderr + proc.stdout
        payload = json.loads(proc.stdout)
        assert payload["auto_adopt"] is False
        assert payload["read_only"] is True
        assert "digest" in payload


def test_plugin_metadata_exposes_digest_and_skill_quality():
    plugin = load_plugin_module()
    for name in ("hermes_skillopt_scout", "hermes_skillopt_doctor", "hermes_skillopt_eval_pack_inventory", "hermes_skillopt_eval_pack_doctor"):
        props = plugin.SCHEMAS[name]["parameters"]["properties"]
        assert props["digest"]["type"] == "boolean"
    assert "hermes_skillopt_skill_quality" in plugin.SCHEMAS
    assert plugin.SCHEMAS["hermes_skillopt_skill_quality"]["x-hermes-skillopt-safety"]["auto_adopt"] is False


def test_tool_safety_catalog_cron_safe_taxonomy_and_plugin_metadata():
    catalog = core.tool_safety_catalog()
    plugin = load_plugin_module()
    tools = catalog["tools"]
    cron_safe = {name for name, meta in tools.items() if meta.get("cron_safe") is True}

    assert cron_safe == DEFAULT_CRON_SAFE_TOOLS
    assert "Schedule only scout, doctor, eval-pack-inventory, and eval-pack-doctor" in catalog["scheduled_default_guidance"]
    assert "review --digest is digest-only/manual" in catalog["scheduled_default_guidance"]
    assert "read-only" in catalog["groups"]["read_only"]["description"].lower()
    assert "Only scout, doctor, eval-pack-inventory, and eval-pack-doctor are cron-safe" in catalog["groups"]["read_only"]["description"]

    for name in DEFAULT_CRON_SAFE_TOOLS:
        meta = tools[name]
        assert meta["safety_group"] == "read_only"
        assert meta["risk_level"] == "low"
        assert meta["writes"] is False
        assert plugin.SCHEMAS[name]["x-hermes-skillopt-safety"]["cron_safe"] is True

    review_meta = tools["hermes_skillopt_review"]
    assert review_meta["safety_group"] == "read_only"
    assert review_meta["cron_safe"] is False
    assert review_meta["cron_mode"] == "digest_only"
    assert plugin.SCHEMAS["hermes_skillopt_review"]["x-hermes-skillopt-safety"]["cron_safe"] is False

    high_risk = {name for name, meta in tools.items() if meta.get("risk_level") == "high"}
    assert {"hermes_skillopt_adopt", "hermes_skillopt_rollback", "hermes_skillopt_upstream_update"}.issubset(high_risk)
    for name in high_risk:
        assert tools[name]["cron_safe"] is False
        assert plugin.SCHEMAS[name]["x-hermes-skillopt-safety"]["cron_safe"] is False

    assert tools["hermes_skillopt_optimize"]["cron_safe"] is False
    assert tools["hermes_skillopt_full_run"]["cron_safe"] is False
    assert tools["hermes_skillopt_run"]["auto_adopt"] is False
    assert tools["hermes_skillopt_full_run"]["writes"] == "staging_only"
    assert tools["hermes_skillopt_optimize"]["writes"] == "staging_only"


def test_skill_quality_placeholder_fails_and_reasonable_passes_no_live_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bad = make_skill(tmp_path, "bad", "---\nname: bad\ndescription: TODO\n---\n# TODO\nplaceholder\napi_key=sk-12345678901234567890\n")
    before = bad.read_text(encoding="utf-8")
    bad_report = skill_quality_report(hermes_home_path=str(tmp_path), skill="bad")
    assert bad_report["quality"]["passed_basics"] is False
    assert any("placeholder" in b for b in bad_report["quality"]["blockers"])
    assert bad_report["secret_scan"]["found"] is True
    assert bad.read_text(encoding="utf-8") == before
    assert bad_report["live_skill_unchanged"] is True

    make_skill(tmp_path, "good")
    good_report = skill_quality_report(hermes_home_path=str(tmp_path), skill="good")
    assert good_report["quality"]["passed_basics"] is True
    assert good_report["read_only"] is True
    assert good_report["auto_adopt"] is False
    assert "safe_eval_skeleton_command" in good_report["eval_pack_readiness"]


def test_skill_quality_eval_skeleton_is_review_only_and_does_not_mutate_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skill_path = make_skill(tmp_path, "demo")
    before = skill_path.read_text(encoding="utf-8")
    out = tmp_path / "skillopt" / "evals" / "demo-quality-scaffold.json"

    report = skill_quality_report(hermes_home_path=str(tmp_path), skill="demo", create_eval_skeleton=True, output=out)

    assert skill_path.read_text(encoding="utf-8") == before
    assert report["live_skill_unchanged"] is True
    assert report["live_skill_writes"] is False
    assert report["eval_skeleton"]["review_only"] is True
    assert report["eval_skeleton"]["production_eligible"] is False
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["production_policy"]["allow_production_adoption"] is False

    digest = skill_quality_digest(report)
    assert digest["auto_adopt"] is False
    assert "scheduled usage is diagnostic only and never auto-adopts" in digest["digest"]
