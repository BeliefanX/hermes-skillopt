from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import webui
from hermes_skillopt import core
from hermes_skillopt import cli


def make_skill(home: Path, name="demo", body="Use tools safely.") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return p


def test_webui_import_does_not_import_gradio():
    assert hasattr(webui, "build_app")
    # The module must be importable and callbacks usable without requiring gradio.
    assert "gradio" not in webui.__dict__


def test_require_gradio_missing_has_install_hint(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "gradio":
            raise ImportError("no gradio in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="pip install gradio"):
        webui.require_gradio()


def test_webui_run_callback_forces_staged_only(monkeypatch, tmp_path):
    calls = {}

    def fake_full_run(**kwargs):
        calls.update(kwargs)
        rd = tmp_path / "skillopt" / "staging" / "rid"
        rd.mkdir(parents=True)
        (rd / "manifest.json").write_text('{"run_id":"rid","status":"staged_best","hermes_home":"%s","skill_name":"demo","skill_relpath":"skills/demo/SKILL.md"}' % tmp_path, encoding="utf-8")
        (rd / "report.md").write_text("# report", encoding="utf-8")
        (rd / "diff.patch").write_text("diff --git a b", encoding="utf-8")
        return {"success": True, "run_id": "rid", "status": "staged_best"}

    monkeypatch.setattr(core, "full_run", fake_full_run)
    out = webui.run_full_callback("demo", "query", "evals/demo.jsonl", 14, 5, 1, 3, 1, "mock", True, str(tmp_path))
    assert calls["eval_file"] == "evals/demo.jsonl"
    assert calls["auto_adopt"] is False
    assert calls["force"] is False
    assert "staged only" in out[0].lower()


def test_webui_adopt_and_rollback_require_exact_confirmation(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    rid = run["run_id"]
    assert "refused" in webui.adopt_callback(rid, "ADOPT wrong", False, str(tmp_path))
    assert "refused" in webui.rollback_callback(rid, "ROLLBACK wrong", False, str(tmp_path))


def test_webui_writeback_callbacks_ignore_home_override(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(core, "adopt", lambda *args, **kwargs: calls.append(("adopt", args, kwargs)) or {"success": True})
    monkeypatch.setattr(core, "rollback", lambda *args, **kwargs: calls.append(("rollback", args, kwargs)) or {"success": True})

    assert "Adopt complete" in webui.adopt_callback("rid", "ADOPT rid", False, str(tmp_path / "other"))
    assert "Rollback complete" in webui.rollback_callback("rid", "ROLLBACK rid", True, str(tmp_path / "other"))
    assert calls == [
        ("adopt", ("rid",), {"hermes_home_path": None, "force": False}),
        ("rollback", ("rid",), {"hermes_home_path": None, "force": True}),
    ]


def test_webui_upstream_update_ignores_home_override(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(core, "upstream_update", lambda **kwargs: calls.append(kwargs) or {"success": True})
    assert "success" in webui.upstream_update_markdown(str(tmp_path / "other"), fetch_only=True)
    assert calls == [{"hermes_home_path": None, "repo_path": None, "fetch_only": True}]


def test_cli_writeback_unsafe_flag_is_explicit(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.core, "adopt", lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True})

    monkeypatch.setattr(sys, "argv", ["hermes-skillopt", "--home", str(tmp_path), "adopt", "rid", "--unsafe-cross-profile-writeback"])
    assert cli.main() == 0
    assert calls == [(("rid", str(tmp_path), False), {"unsafe_cross_profile": True})]

    monkeypatch.setattr(sys, "argv", ["hermes-skillopt", "adopt", "rid", "--unsafe-cross-profile-writeback"])
    assert cli.main() == 2
    assert "requires --home" in capsys.readouterr().err


def test_webui_review_latest_reads_only_staging_artifacts(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    summary, report, diff, gate, candidate, rejected = webui.review_payload("", str(tmp_path))
    assert run["run_id"] in summary
    assert "SkillOpt dry run" in report
    assert "SkillOpt Candidate Improvements" in diff
    assert "SkillOpt Candidate Improvements" in candidate


def test_cli_webui_propagates_home(monkeypatch, tmp_path):
    calls = {}

    def fake_main(argv):
        calls["argv"] = argv
        return 0

    monkeypatch.setattr(webui, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["hermes-skillopt", "--home", str(tmp_path), "webui", "--port", "9999"])
    assert cli.main() == 0
    assert calls["argv"] == ["--host", "127.0.0.1", "--port", "9999", "--home", str(tmp_path)]


def test_webui_build_app_uses_home_default_for_initial_status(monkeypatch, tmp_path):
    seen = {"status_home": None, "textbox_values": []}

    def fake_status(home=None):
        seen["status_home"] = home
        return f"status for {home}"

    class Component:
        def __init__(self, *args, **kwargs):
            if "value" in kwargs:
                seen["textbox_values"].append(kwargs["value"])

        def click(self, *args, **kwargs):
            return None

    class Context(Component):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeGr:
        Blocks = Context
        Tabs = Context
        Tab = Context
        Row = Context
        Markdown = Component
        Textbox = Component
        Button = Component
        Slider = Component
        Dropdown = Component
        Checkbox = Component
        Code = Component

    monkeypatch.setattr(webui, "require_gradio", lambda: FakeGr)
    monkeypatch.setattr(webui, "status_markdown", fake_status)
    app = webui.build_app(str(tmp_path))
    assert isinstance(app, Context)
    assert seen["status_home"] == str(tmp_path)
    assert str(tmp_path) in seen["textbox_values"]


def test_webui_review_rejects_symlink_artifact_escape(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    rid = run["run_id"]
    run_dir = tmp_path / "skillopt" / "staging" / rid
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret must not leak", encoding="utf-8")
    (run_dir / "report.md").unlink()
    (run_dir / "report.md").symlink_to(outside)
    summary, report, diff, gate, candidate, rejected = webui.review_payload(rid, str(tmp_path))
    assert rid in summary
    assert report == ""
    assert "outside secret" not in "\n".join([summary, report, diff, gate, candidate, rejected])


def test_webui_artifact_path_guard_uses_allowlist(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (run_dir / "secret.txt").write_text("do not expose", encoding="utf-8")
    assert webui._read_artifact_limited(run_dir, "manifest.json") == "{}"
    assert webui._safe_artifact_path(run_dir, "secret.txt") is None
    assert webui._read_artifact_limited(run_dir, "secret.txt") == ""


def test_webui_report_summary_observability_fields(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "checkpoint.json").write_text('{"status":"complete","completed_stages":["rollout","reflect","evaluate"]}', encoding="utf-8")
    (run_dir / "rejected_edits.jsonl").write_text('{"iteration":1,"candidate":2,"reason":"regression","task_id":"t1","score_delta":-0.1,"edit":"remove guard"}\n', encoding="utf-8")
    manifest = {
        "run_id": "rid",
        "status": "rejected",
        "skill_name": "demo",
        "adoptable": False,
        "production_gate_eligible": False,
        "test_gate_eligible": True,
        "production_eligibility_reasons": ["missing accepted explicit curated production validation gate"],
        "split_scores": {"validation": {"current": 0.5, "candidate": 0.6}, "heldout_test": {"best": 0.7}},
        "candidate_comparison": [{"rank": 1, "accepted": False}],
        "regression_cases": ["t1"],
        "provenance_fingerprint": {"fingerprint_sha256": "abc"},
        "production_eval_policy": {"policy_version": "production-eval-schema-v1"},
        "artifact_sha256": {"manifest": "sha"},
        "optimizer_backend": "mock",
        "target_executor": "hermes_replay_runner_mvp",
        "gate_policy": {"mode": "strict"},
    }
    data = webui.report_summary_data(manifest, run_dir)
    md = webui.report_summary_markdown(data)
    assert data["timeline"]["completed_stages"] == ["rollout", "reflect", "evaluate"]
    assert data["eligibility"]["adoptable"] is False
    assert data["provenance_security"]["production_eval_policy"] == "production-eval-schema-v1"
    assert data["rejected_edits"]["preview"][0]["reason"] == "regression"
    assert "split_scores" in md
    assert "candidate_comparison_count: 1" in md
    assert "missing accepted explicit curated production validation gate" in md


def test_webui_review_surfaces_observability_json_and_rejected_preview(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    rid = run["run_id"]
    run_dir = tmp_path / "skillopt" / "staging" / rid
    manifest = core.load_manifest(run_dir)
    manifest.update({
        "split_scores": {"validation": {"current": 0.4, "candidate": 0.5}},
        "candidate_comparison": [{"rank": 1}],
        "regression_cases": ["case-a"],
        "production_eligibility_reasons": ["held-out test split is missing, non-production, or below threshold"],
    })
    core.save_manifest(run_dir, manifest)
    (run_dir / "checkpoint.json").write_text('{"status":"complete","completed_stages":["rollout","evaluate"]}', encoding="utf-8")
    (run_dir / "rejected_edits.jsonl").write_text('{"reason":"bad edit","edit":"leak SECRET_TOKEN=abc"}\n', encoding="utf-8")
    summary, report, diff, gate, candidate, rejected = webui.review_payload(rid, str(tmp_path))
    assert "Observability report summary" in summary
    assert "completed_stages: rollout, evaluate" in summary
    assert "Exportable observability JSON" in gate
    assert "candidate_comparison" in gate
    assert "Rejected edit explorer preview" in rejected
    assert "abc" not in rejected


@pytest.mark.parametrize("bad_run_id", ["../evil", "nested/evil", "/tmp/evil"])
def test_webui_review_invalid_run_id_is_reported_not_resolved(tmp_path, bad_run_id):
    summary, report, diff, gate, candidate, rejected = webui.review_payload(bad_run_id, str(tmp_path))
    assert summary.startswith("Review failed: ValueError: Invalid run_id")
    assert (report, diff, gate, candidate, rejected) == ("", "", "", "", "")


def test_cli_and_module_webui_help_smoke():
    repo = Path(__file__).resolve().parents[1]
    for cmd in (
        [sys.executable, "-m", "hermes_skillopt.webui", "--help"],
        [sys.executable, "-m", "hermes_skillopt.cli", "webui", "--help"],
    ):
        res = subprocess.run(cmd, cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        assert res.returncode == 0
        assert "--port" in res.stdout
        assert "--home" in res.stdout
