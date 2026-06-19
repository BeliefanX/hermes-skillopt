from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from hermes_skillopt import core
from hermes_skillopt.eval_packs import eval_pack_doctor_digest, eval_pack_inventory_digest
from hermes_skillopt.skill_quality import skill_quality_digest, skill_quality_report


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
        assert "scheduled usage is diagnostic only and never auto-adopts" in digest["digest"]
        assert "read_only: True | auto_adopt: False" in digest["digest"]


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
