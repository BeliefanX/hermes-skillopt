from __future__ import annotations

import json
from pathlib import Path

from hermes_skillopt import core
from hermes_skillopt import cli
from hermes_skillopt.eval_packs import eval_pack_workflow_summary, generate_negative_boundary_eval_pack, promote_eval_pack, scaffold_eval_pack


def make_skill(home: Path, name: str, body: str = "Use tools safely.") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return p


def test_generated_scaffold_and_negative_pack_metadata_remain_non_production(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_skill(tmp_path, "demo")

    scaffold = scaffold_eval_pack(skill="demo", hermes_home_path=str(tmp_path))
    assert scaffold["review_only"] is True
    assert scaffold["production_eligible"] is False
    payload = scaffold["eval_pack"]
    assert payload["provenance"]["review_only"] is True
    assert payload["production_policy"]["allow_production_adoption"] is False
    assert all(t["production_gate_eligible"] is False for t in payload["tasks"])
    assert all(t.get("provenance", {}).get("review_only") is True for t in payload["tasks"])
    assert scaffold["report"]["production_eligible"] is False
    assert "sample-eval-pack" in scaffold["report"]["non_production_origins"]

    neg = generate_negative_boundary_eval_pack(skill="demo", hermes_home_path=str(tmp_path))
    assert neg["review_only"] is True
    assert neg["production_eligible"] is False
    assert neg["report"]["production_eligible"] is False
    assert set(neg["report"]["non_production_origins"]) >= {"negative-case", "boundary-case"}


def test_eval_pack_workflow_and_promotion_are_structured_review_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_skill(tmp_path, "demo")
    draft = scaffold_eval_pack(skill="demo", hermes_home_path=str(tmp_path))

    workflow = eval_pack_workflow_summary(hermes_home_path=str(tmp_path), skill="demo")
    assert workflow["success"] is True
    assert workflow["read_only"] is True
    assert workflow["webui_production_one_click"] is False
    assert workflow["workflow"][0]["production_eligible"] is False
    assert "strict production optimize/review remains separate" in "; ".join(workflow["workflow"][0]["requirements_for_production"])

    promoted = promote_eval_pack(skill="demo", input_path=draft["output_path"], hermes_home_path=str(tmp_path))
    assert promoted["mode"] == "eval_pack_promote_curated_review_default"
    assert promoted["review_only"] is True
    assert promoted["production_eligible"] is False
    assert promoted["promotion_requirements"]["production_requires_explicit_policy_contract"] is True
    assert promoted["promotion_requirements"]["no_webui_production_one_click"] is True


def test_skill_readiness_queue_blocks_native_guard_and_cli_is_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_skill(tmp_path, "alpha")
    make_skill(tmp_path, "pinned", body="---\npinned: true\n---\nPinned body")
    usage = {
        "alpha": {"usage_count": 7, "state": "active", "last_used": "2026-01-01T00:00:00Z"},
        "pinned": {"usage_count": 99, "pinned": True},
    }
    (tmp_path / "skills" / ".usage.json").write_text(json.dumps(usage), encoding="utf-8")

    out = core.skill_readiness_queue(str(tmp_path), limit=10)
    assert out["read_only"] is True
    assert out["auto_adopt"] is False
    rows = {r["skill"]: r for r in out["queue"]}
    assert rows["alpha"]["priority_score"] > 0
    assert rows["alpha"]["safe_next_command"].split()[0] == "hermes-skillopt"
    forbidden = " ".join(r["safe_next_command"] for r in out["queue"])
    assert " full-run" not in forbidden and " optimize" not in forbidden and " adopt" not in forbidden
    assert rows["pinned"]["blocked_by_native_guard"] is True
    assert rows["pinned"]["priority_band"] == "blocked"

    monkeypatch.setattr("sys.argv", ["hermes-skillopt", "--home", str(tmp_path), "skill-readiness-queue", "--limit", "2"])
    assert cli.main() == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["schema_version"] == "hermes-skillopt-skill-readiness-queue-v1"

    monkeypatch.setattr("sys.argv", ["hermes-skillopt", "--home", str(tmp_path), "eval-pack-workflow", "--skill", "alpha"])
    assert cli.main() == 0
    cli_wf = json.loads(capsys.readouterr().out)
    assert cli_wf["schema_version"] == "hermes-skillopt-eval-pack-workflow-summary-v1"
