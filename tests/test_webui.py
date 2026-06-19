from __future__ import annotations

import builtins
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_skillopt import cli, core, webui, webui_api
from hermes_skillopt.webui_server import create_app


def make_skill(home: Path, name="demo", body="Use tools safely.") -> Path:
    p = home / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nname: {name}\ndescription: test\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return p


def test_webui_import_does_not_import_gradio():
    assert "gradio" not in webui.__dict__


def test_require_gradio_is_no_longer_live_path(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "gradio":
            raise ImportError("no gradio in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="FastAPI and Uvicorn"):
        webui.require_gradio()


def test_webui_run_api_forces_staged_only(monkeypatch, tmp_path):
    calls = {}

    def fake_full_run(**kwargs):
        calls.update(kwargs)
        return {"success": True, "run_id": "rid", "status": "staged_best"}

    monkeypatch.setattr(core, "full_run", fake_full_run)
    out = webui_api.run_full({"skill": "demo", "eval_file": "evals/demo.jsonl", "backend": "mock", "optimizer_backend": "mock", "target_executor": "scorecard", "target_backend": "scorecard", "gate_mode": "mixed", "candidate_count": 2, "edit_budget": 4, "allow_mock": True, "home": str(tmp_path)})
    assert out["run_id"] == "rid"
    assert calls["auto_adopt"] is False
    assert calls["force"] is False
    assert calls["hermes_home_path"] == str(tmp_path)
    assert calls["optimizer_backend"] == "mock"
    assert calls["target_executor"] == "scorecard"
    assert calls["target_backend"] == "scorecard"
    assert calls["gate_mode"] == "mixed"
    assert calls["candidate_count"] == 2
    assert calls["edit_budget"] == 4


def test_webui_adopt_and_rollback_require_exact_confirmation(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    rid = run["run_id"]
    with pytest.raises(PermissionError):
        webui_api.adopt(rid, "ADOPT wrong")
    with pytest.raises(PermissionError):
        webui_api.rollback(rid, "ROLLBACK wrong")


def test_webui_writeback_apis_ignore_home_override(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(core, "adopt", lambda *args, **kwargs: calls.append(("adopt", args, kwargs)) or {"success": True})
    monkeypatch.setattr(core, "rollback", lambda *args, **kwargs: calls.append(("rollback", args, kwargs)) or {"success": True})
    assert webui_api.adopt("rid", "ADOPT rid", False)["success"] is True
    assert webui_api.rollback("rid", "ROLLBACK rid", True)["success"] is True
    assert calls == [
        ("adopt", ("rid",), {"hermes_home_path": None, "force": False}),
        ("rollback", ("rid",), {"hermes_home_path": None, "force": True}),
    ]


def test_webui_upstream_update_ignores_home_override(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(core, "upstream_update", lambda **kwargs: calls.append(kwargs) or {"success": True})
    assert webui_api.upstream_update(fetch_only=True)["success"] is True
    assert calls == [{"hermes_home_path": None, "repo_path": None, "fetch_only": True}]


def test_cli_writeback_unsafe_flag_is_explicit(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.core, "adopt", lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True})
    monkeypatch.setattr(sys, "argv", ["hermes-skillopt", "--home", str(tmp_path), "adopt", "rid", "--confirm", "ADOPT rid", "--unsafe-cross-profile-writeback"])
    assert cli.main() == 0
    assert calls == [(("rid", str(tmp_path), False), {"unsafe_cross_profile": True})]
    monkeypatch.setattr(sys, "argv", ["hermes-skillopt", "adopt", "rid", "--unsafe-cross-profile-writeback"])
    assert cli.main() == 2
    assert "requires --home" in capsys.readouterr().err


def test_webui_review_latest_reads_only_staging_artifacts(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    payload = webui_api.review("", str(tmp_path))
    assert run["run_id"] in payload["summary"]
    assert "SkillOpt dry run" in payload["report"]
    assert "SkillOpt Candidate Improvements" in payload["diff"]
    assert "SkillOpt Candidate Improvements" in payload["candidate"]
    for field in ("decision", "adoptable", "blockers", "production_gate", "test_gate", "evidence_class", "artifacts", "next_safe_action"):
        assert field in payload


def test_webui_run_api_production_intent_requires_eval_and_strict_no_mock(tmp_path):
    with pytest.raises(ValueError, match="production intent requires explicit"):
        webui_api.run_full({"intent": "production", "skill": "demo", "home": str(tmp_path), "gate_mode": "strict", "allow_mock": False})
    eval_file = tmp_path / "eval.jsonl"
    eval_file.write_text('{"input":"x","expected":"y"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="allow-mock"):
        webui_api.run_full({"intent": "production", "skill": "demo", "home": str(tmp_path), "eval_file": str(eval_file), "gate_mode": "strict", "allow_mock": True})


def test_webui_eval_pack_api_review_only_doctor_autopilot_promote(tmp_path):
    make_skill(tmp_path, "demo")
    doctor = webui_api.eval_pack_doctor(str(tmp_path), "demo")
    assert doctor["mode"] == "eval_pack_doctor_read_only"
    assert doctor["read_only"] is True
    assert doctor["auto_adopt"] is False

    plan = webui_api.eval_pack_autopilot({"skill": "demo", "home": str(tmp_path)})
    assert plan["mode"] == "eval_pack_autopilot_plan_read_only"
    assert plan["read_only"] is True

    draft = webui_api.eval_pack_autopilot({"skill": "demo", "home": str(tmp_path), "write_draft": True})
    draft_path = Path(draft["draft"]["output_path"])
    assert draft["review_only"] is True
    assert draft["production_eligible"] is False
    assert draft["auto_adopt"] is False
    assert draft_path.exists()

    promoted = webui_api.eval_pack_promote({"skill": "demo", "home": str(tmp_path), "input_path": str(draft_path)})
    assert promoted["mode"] == "eval_pack_promote_curated_review_default"
    assert promoted["production_eligible"] is False
    assert promoted["auto_adopt"] is False
    assert Path(promoted["output_path"]).exists()
    with pytest.raises(ValueError, match="review packs only"):
        webui_api.eval_pack_promote({"skill": "demo", "home": str(tmp_path), "input_path": str(draft_path), "production": True})


def test_cli_webui_propagates_home(monkeypatch, tmp_path):
    calls = {}
    def fake_main(argv):
        calls["argv"] = argv
        return 0

    monkeypatch.setattr(webui, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["hermes-skillopt", "--home", str(tmp_path), "webui", "--port", "9999"])
    assert cli.main() == 0
    assert calls["argv"] == ["--host", "127.0.0.1", "--port", "9999", "--home", str(tmp_path)]


def test_pwa_static_contracts_no_dynamic_private_cache():
    head = webui.pwa_head_html()
    manifest = webui.pwa_manifest()
    sw = webui.service_worker_js()
    assert 'rel="manifest" href="/manifest.webmanifest"' in head
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert '"/api/"' in sw
    assert 'url.pathname === "/"' in sw
    assert 'cache: "no-store"' in sw
    assert "await cache.put(\"/\"" not in sw
    for path in webui.PWA_ASSET_PATHS:
        assert json.dumps(path) in sw


def test_webui_review_rejects_symlink_artifact_escape(tmp_path):
    make_skill(tmp_path, "demo")
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    rid = run["run_id"]
    run_dir = tmp_path / "skillopt" / "staging" / rid
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret must not leak", encoding="utf-8")
    (run_dir / "report.md").unlink()
    (run_dir / "report.md").symlink_to(outside)
    payload = webui_api.review(rid, str(tmp_path))
    assert payload["report"] == ""
    assert "outside secret" not in "\n".join(str(v) for v in payload.values())


@pytest.mark.parametrize("bad_run_id", ["../evil", "nested/evil", "/tmp/evil"])
def test_webui_review_invalid_run_id_is_reported_not_resolved(tmp_path, bad_run_id):
    payload = webui_api.review(bad_run_id, str(tmp_path))
    assert payload["summary"].startswith("Review failed: ValueError: Invalid run_id")
    assert payload["report"] == payload["diff"] == payload["gate"] == payload["candidate"] == payload["rejected"] == ""


def test_fastapi_routes_and_static_assets(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    make_skill(tmp_path, "demo")
    app = create_app(str(tmp_path))
    client = TestClient(app)
    assert client.get("/").status_code in {200, 503}
    status = client.get("/api/status", params={"home": str(tmp_path)})
    assert status.status_code == 200
    assert status.json()["hermes_home"] == str(tmp_path)
    doctor = client.get("/api/doctor", params={"home": str(tmp_path), "skill": "demo"})
    assert doctor.status_code == 200
    assert doctor.json()["mode"] == "read_only_doctor_no_full_run_no_adopt_no_rollback_no_fetch"
    eval_doctor = client.get("/api/eval-pack/doctor", params={"home": str(tmp_path), "skill": "demo"})
    assert eval_doctor.status_code == 200
    assert eval_doctor.json()["mode"] == "eval_pack_doctor_read_only"
    draft = client.post("/api/eval-pack/autopilot", json={"skill": "demo", "write_draft": True, "home": str(tmp_path)})
    assert draft.status_code == 200
    draft_json = draft.json()
    assert draft_json["review_only"] is True
    draft_path = draft_json["draft"]["output_path"]
    promoted = client.post("/api/eval-pack/promote", json={"skill": "demo", "input_path": draft_path, "home": str(tmp_path)})
    assert promoted.status_code == 200
    assert promoted.json()["mode"] == "eval_pack_promote_curated_review_default"
    prod_promote = client.post("/api/eval-pack/promote", json={"skill": "demo", "input_path": draft_path, "production": True, "home": str(tmp_path)})
    assert prod_promote.status_code == 400
    assert "review packs only" in prod_promote.text
    fleet = client.get("/api/fleet/report", params={"home": str(tmp_path), "limit": 5})
    assert fleet.status_code == 200
    assert fleet.json()["mode"].startswith("read_only_report")
    assert client.get("/api/fleet/resume-plan", params={"home": str(tmp_path)}).status_code == 200
    assert client.get("/api/fleet/rollback-plan", params={"home": str(tmp_path)}).status_code == 200
    run = core.dry_run(skill="demo", hermes_home_path=str(tmp_path))
    latest = client.get("/api/review/latest", params={"home": str(tmp_path)})
    assert latest.status_code == 200
    assert latest.json()["run_id"] == run["run_id"]
    summary = client.get("/api/review/summary", params={"home": str(tmp_path), "run_id": run["run_id"]})
    assert summary.status_code == 200
    assert "next_action" in summary.json()
    assert client.get("/manifest.webmanifest").status_code == 200
    manifest_json = client.get("/manifest.json")
    assert manifest_json.status_code == 200
    assert manifest_json.headers["content-type"].startswith("application/manifest+json")
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert '"/api/"' in sw.text
    bad = client.post("/api/adopt", json={"run_id": "rid", "confirmation": "ADOPT wrong", "home": str(tmp_path)})
    assert bad.status_code == 403


def test_fastapi_run_uses_create_app_home_default(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    calls = {}

    def fake_full_run(payload):
        calls.update(payload)
        return {"success": True, "run_id": "rid", "token": "sk-abcdefghijklmnop"}

    monkeypatch.setattr(webui_api, "run_full", fake_full_run)
    client = TestClient(create_app(str(tmp_path)))
    res = client.post("/api/run", json={"skill": "demo", "backend": "mock"})
    assert res.status_code == 200
    assert calls["home"] == str(tmp_path)
    assert res.json()["token"] == "<REDACTED>"


def test_fastapi_error_mapping_and_redaction(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    client = TestClient(create_app(str(tmp_path)))
    monkeypatch.setattr(webui_api, "status", lambda home=None: (_ for _ in ()).throw(ValueError("bad token=sk-abcdefghijklmnop")))
    bad = client.get("/api/status")
    assert bad.status_code == 400
    assert "sk-" not in bad.text
    assert "<REDACTED>" in bad.text

    monkeypatch.setattr(webui_api, "status", lambda home=None: (_ for _ in ()).throw(PermissionError("authorization: bearer-secret-abcdefghijklmnopqrstuvwxyz")))
    denied = client.get("/api/status")
    assert denied.status_code == 403
    assert "bearer-secret" not in denied.text

    monkeypatch.setattr(webui_api, "status", lambda home=None: (_ for _ in ()).throw(RuntimeError(f"boom at {tmp_path} token=sk-abcdefghijklmnop")))
    err = client.get("/api/status")
    assert err.status_code == 500
    assert str(tmp_path) not in err.text
    assert "sk-" not in err.text


def test_web_package_build_contracts():
    pkg = json.loads((Path(__file__).resolve().parents[1] / "web" / "package.json").read_text(encoding="utf-8"))
    assert pkg["scripts"]["typecheck"] == "tsc --noEmit"
    assert "typecheck" in pkg["scripts"]["build"]
    for section in ("dependencies", "devDependencies"):
        for name, version in pkg.get(section, {}).items():
            assert version != "latest", name


def test_mobile_topbar_controls_move_to_sheet_contract():
    repo = Path(__file__).resolve().parents[1]
    css = (repo / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    jsx = (repo / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
    mobile = css[css.index("@media (max-width:900px)"):css.index("@media (max-width:430px)")]
    assert ".topbar{align-items:center;flex-wrap:nowrap;min-height:56px" in mobile
    assert ".topbar>.top-controls{display:none}" in mobile
    assert ".sheet-controls .top-controls{display:grid" in mobile
    assert ".topbar .brand{flex:1;min-width:0;flex-wrap:nowrap}" in mobile
    assert ".topbar .badge" in mobile and "white-space:nowrap" in mobile
    assert "<header className=\"topbar\"" in jsx
    assert "<div className=\"sheet-controls\">{topControls}</div>" in jsx
    assert "aria-label={t.openMenu}" in jsx


def test_webui_phase4_wizard_and_decision_first_contracts():
    repo = Path(__file__).resolve().parents[1]
    jsx = (repo / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
    assert "Smoke" in jsx and "Review-only" in jsx and "Production" in jsx
    assert "strict gate" in jsx and "no mock" in jsx
    assert "staged-only / no auto-adopt" in jsx
    assert "Decision-first review" in jsx
    for field in ("adoptable", "blockers", "production_gate", "test_gate", "evidence_class", "artifacts", "next_safe_action"):
        assert field in jsx
    assert "TextBlock title=\"diff.patch\"" in jsx
    assert "RawBlock title={t.rawReview}" in jsx


def test_webui_eval_pack_one_click_contracts():
    repo = Path(__file__).resolve().parents[1]
    jsx = (repo / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
    server = (repo / "hermes_skillopt" / "webui_server.py").read_text(encoding="utf-8")
    api = (repo / "hermes_skillopt" / "webui_api.py").read_text(encoding="utf-8")
    for label in ("Diagnose eval coverage", "Generate review draft", "Promote draft to curated review pack"):
        assert label in jsx
    assert "review-only / no live skill adopt" in jsx
    assert "production: false" in jsx
    assert '"/api/eval-pack/doctor"' in server
    assert '"/api/eval-pack/autopilot"' in server
    assert '"/api/eval-pack/promote"' in server
    assert "WebUI promotes review packs only" in api


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
        assert "Gradio" not in res.stdout
