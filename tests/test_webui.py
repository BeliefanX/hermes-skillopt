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
    out = webui.run_full_callback("demo", "query", 14, 5, 1, 3, "mock", True, str(tmp_path))
    assert calls["auto_adopt"] is False
    assert calls["force"] is False
    assert "staged only" in out[0].lower()


def test_webui_adopt_and_rollback_require_exact_confirmation(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    rid = run["run_id"]
    assert "refused" in webui.adopt_callback(rid, "ADOPT wrong", False, str(tmp_path))
    assert "refused" in webui.rollback_callback(rid, "ROLLBACK wrong", False, str(tmp_path))


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
