from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import core


def make_skill(home: Path, name: str = "demo") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\nUse tools safely.\n", encoding="utf-8")
    return p


def load_plugin_module():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_skillopt_plugin_guided_ux", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def active_tmp_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_doctor_is_read_only_for_fresh_home(tmp_path):
    out = core.doctor(hermes_home_path=str(tmp_path))

    assert out["success"] is True
    assert out["mode"] == "read_only_doctor_no_full_run_no_adopt_no_rollback_no_fetch"
    assert out["upstream_parity_posture"]["full_parity_claim"] is False
    assert "production" in out["recommended_next_action"]
    assert not (tmp_path / "skillopt").exists()


def test_guided_optimize_production_fails_fast_without_explicit_eval(tmp_path):
    make_skill(tmp_path)

    with pytest.raises(ValueError, match="production intent requires explicit --eval-file"):
        core.guided_optimize(intent="production", skill="demo", hermes_home_path=str(tmp_path), backend="mock", allow_mock=True)

    assert not (tmp_path / "skillopt").exists()


def test_guided_optimize_smoke_is_review_only_and_staged(tmp_path):
    make_skill(tmp_path)

    out = core.guided_optimize(intent="smoke", skill="demo", hermes_home_path=str(tmp_path))

    assert out["success"] is True
    assert out["intent"] == "smoke"
    assert out["auto_adopt"] is False
    assert out["adoptable"] is False
    assert "review-only" in out["review_only_label"]
    assert (Path(out["run_dir"]) / "manifest.json").exists()


def test_review_latest_and_summary(tmp_path):
    make_skill(tmp_path)
    out = core.guided_optimize(intent="smoke", skill="demo", hermes_home_path=str(tmp_path))

    latest = core.review_latest(hermes_home_path=str(tmp_path), slim=True)
    summary = core.review_decision_summary("latest", hermes_home_path=str(tmp_path))

    assert latest["run_id"] == out["run_id"]
    assert latest["slim"] is True
    assert summary["run_id"] == out["run_id"]
    assert summary["decision"] in {"review_only_not_adoptable", "not_ready_rejected_or_incomplete", "ready_for_explicit_adopt"}
    assert summary["production_gate_eligible"] is False
    assert summary["test_gate_eligible"] is False
    assert summary["evidence_class"] == "review_only_or_not_ready"
    assert summary["blockers"] == summary["not_adoptable_reasons"]
    assert summary["next_action"]
    assert summary["next_safe_action"] == summary["next_action"]


def test_cli_adopt_requires_typed_confirmation_before_core_gate(tmp_path):
    cmd = [sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "adopt", "missing-run", "--yes-i-understand-skillopt-adopt"]
    bypass = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert bypass.returncode != 2

    no_confirm = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "adopt", "missing-run"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert no_confirm.returncode == 2
    assert "ADOPT missing-run" in no_confirm.stderr


def test_plugin_registers_doctor_optimize_and_enforces_adopt_confirmation(monkeypatch):
    plugin = load_plugin_module()

    assert "hermes_skillopt_doctor" in plugin.SCHEMAS
    assert "hermes_skillopt_optimize" in plugin.SCHEMAS
    assert plugin.SCHEMAS["hermes_skillopt_optimize"]["parameters"]["properties"]["intent"]["enum"] == ["smoke", "review", "production"]
    assert "confirmation" in plugin.SCHEMAS["hermes_skillopt_adopt"]["parameters"]["properties"]
    rollback_confirm = plugin.SCHEMAS["hermes_skillopt_rollback"]["parameters"]["properties"]["confirmation"]
    assert "ROLLBACK <run_id>" in rollback_confirm["description"]

    raw = plugin._handle_adopt({"run_id": "abc"})
    denied = json.loads(raw)
    assert denied.get("success") is False or "error" in denied
    assert "ADOPT abc" in denied.get("error", json.dumps(denied))

    seen = {}

    def fake_adopt(**kw):
        seen["kw"] = kw
        return {"success": True}

    monkeypatch.setattr(plugin.core, "adopt", fake_adopt)
    raw = plugin._handle_adopt({"run_id": "abc", "confirmation": "ADOPT abc"})
    assert json.loads(raw)["success"] is True
    assert seen["kw"]["run_id"] == "abc"

    raw = plugin._handle_rollback({"run_id": "abc"})
    denied_rollback = json.loads(raw)
    assert denied_rollback.get("success") is False or "error" in denied_rollback
    assert "ROLLBACK abc" in denied_rollback.get("error", json.dumps(denied_rollback))


def test_cli_optimize_production_refusal_is_clear(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "optimize", "--intent", "production", "--skill", "demo"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 2
    assert "optimize refused: production intent requires explicit --eval-file" in proc.stderr


def test_cli_optimize_review_defaults_to_review_only_mock_allowed(tmp_path):
    make_skill(tmp_path)

    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--home", str(tmp_path), "optimize", "--intent", "review", "--skill", "demo"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["success"] is True
    assert payload["intent"] == "review"
    assert payload["auto_adopt"] is False
    assert payload["adoptable"] is False
    assert "review-only" in payload["review_only_label"]


def test_cli_help_smoke_lists_guided_commands(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "hermes_skillopt.cli", "--help"], cwd=Path(__file__).resolve().parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 0
    assert "doctor" in proc.stdout
    assert "optimize" in proc.stdout
