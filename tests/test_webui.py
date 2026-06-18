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
    out = webui_api.run_full({"skill": "demo", "eval_file": "evals/demo.jsonl", "backend": "mock", "allow_mock": True, "home": str(tmp_path)})
    assert out["run_id"] == "rid"
    assert calls["auto_adopt"] is False
    assert calls["force"] is False
    assert calls["hermes_home_path"] == str(tmp_path)


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
    monkeypatch.setattr(sys, "argv", ["hermes-skillopt", "--home", str(tmp_path), "adopt", "rid", "--unsafe-cross-profile-writeback"])
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
