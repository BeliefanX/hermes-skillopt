from __future__ import annotations

import builtins
import json
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


def test_webui_i18n_helpers_cover_primary_safety_copy():
    assert webui.ui_text("en", "run_button") == "Run full cycle (staged only)"
    assert webui.ui_text("zh", "run_button") == "运行完整周期（仅暂存）"
    assert "安全模型" in webui.safety_cards_markdown("zh")
    assert "typed confirmations" in webui.safety_cards_markdown("en")
    hero = webui.hero_markdown("en")
    assert '<h1>Hermes SkillOpt</h1>' in hero
    assert "skillopt-subtitle" in hero
    assert "# Hermes SkillOpt" not in hero
    updates = webui.language_updates(type("FakeGr", (), {})(), "zh")
    assert "用于审查暂存" in updates[0]
    assert "# Hermes SkillOpt" not in updates[0]
    assert updates[23]["placeholder"] == "Type: ADOPT <run_id>"
    assert updates[27]["placeholder"] == "Type: ROLLBACK <run_id>"



def test_webui_code_component_does_not_emit_unsupported_diff_language():
    seen = []

    class FakeGr:
        @staticmethod
        def Code(**kwargs):
            seen.append(kwargs)
            return kwargs

    webui.make_code_component(FakeGr, label="diff.patch", language="diff")
    webui.make_code_component(FakeGr, label="gate.json", language="json")
    assert seen[0] == {"label": "diff.patch"}
    assert seen[1] == {"label": "gate.json", "language": "json"}


def test_webui_pwa_head_manifest_and_mobile_css_are_present():
    head = webui.pwa_head_html()
    manifest = webui.pwa_manifest()
    assert 'rel="manifest" href="/manifest.webmanifest"' in head
    assert 'rel="apple-touch-icon" href="/icons/apple-touch-icon.png"' in head
    assert 'name="mobile-web-app-capable" content="yes"' in head
    assert 'name="apple-mobile-web-app-status-bar-style" content="black-translucent"' in head
    assert "viewport-fit=cover" in head
    assert 'navigator.serviceWorker.register("/sw.js", { scope: "/" })' in head
    assert manifest["display"] == "standalone"
    assert manifest["id"] == "/?hermes-skillopt-pwa"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512", "180x180"}
    assert "env(safe-area-inset-top)" in webui.WEBUI_CSS
    assert "overflow-wrap: anywhere" in webui.WEBUI_CSS


def test_webui_mobile_css_clamps_real_gradio_widths_without_only_hiding_overflow():
    css = webui.WEBUI_CSS
    assert "overflow-x: hidden" in css
    assert "max-width: min(1180px, 100vw)" in css
    assert "@media (max-width: 860px)" in css
    for selector in (
        "gradio-app",
        ".gradio-container",
        ".gradio-container .main",
        ".gradio-container main.contain",
        ".gradio-container .wrap",
        ".gradio-container .column",
        ".gradio-container .block",
        ".skillopt-shell",
        ".skillopt-hero",
        ".skillopt-cards",
        ".skillopt-card",
    ):
        assert selector in css
    assert "width: min(100%, 100vw) !important" in css
    assert "max-width: 100vw !important" in css
    assert "min-width: 0 !important" in css
    assert "overflow-x: auto" in css
    assert "overflow-wrap: anywhere !important" in css


def test_webui_mobile_polish_css_covers_settings_tabs_and_light_markdown():
    css = webui.WEBUI_CSS
    assert ".skillopt-settings-row" in css
    assert "@media (max-width: 640px)" in css
    assert ".skillopt-settings-row { flex-direction: column !important; }" in css
    assert ".gradio-container .tabs [role=\"tablist\"]" in css
    assert ".gradio-container div[role=\"tablist\"]" in css
    assert ".tabs [role=\"tablist\"]" in css
    assert ".tabs .tab-container[role=\"tablist\"]" in css
    assert ".tab-wrapper [role=\"tablist\"]" in css
    assert "div[role=\"tablist\"]" in css
    assert ".tab-nav" in css
    assert "display: flex !important" in css
    assert "overflow-x: auto !important" in css
    assert "flex-wrap: nowrap !important" in css
    assert "scroll-snap-type: x proximity" in css
    assert "scrollbar-width: none" in css
    assert ".gradio-container button[role=\"tab\"]" in css
    assert "flex: 0 0 auto !important" in css
    assert "width: auto !important" in css
    assert "min-width: max-content !important" in css
    assert "max-width: none !important" in css
    assert "white-space: nowrap !important" in css
    assert "text-overflow: clip !important" in css
    assert "overflow: visible !important" in css
    assert "justify-content: flex-start !important" in css
    assert ".gradio-container [role=\"tab\"] *" in css
    assert ".skillopt-status-md" in css
    assert ".skillopt-result-md" in css
    assert "--skillopt-code-bg: #f1eee8" in css
    assert "background: var(--skillopt-code-bg) !important" in css
    assert "word-break: break-word !important" in css


def test_webui_mobile_tab_override_beats_desktop_segmented_grid_compression():
    css = webui.WEBUI_CSS
    desktop_tab_rule = '.gradio-container .tabs [role="tablist"] > button[role="tab"], .gradio-container .tab-nav button[role="tab"], .gradio-container div[role="tablist"] > button[role="tab"] { width: 100% !important; min-width: 0 !important; overflow: hidden !important; text-overflow: ellipsis !important; }'
    assert desktop_tab_rule in css
    media_start = css.index("@media (max-width: 640px)")
    assert media_start > css.index(desktop_tab_rule)
    mobile_css = css[media_start : css.index("@media (max-width: 520px)")]
    assert "display: flex !important" in mobile_css
    assert "grid-template-columns: none !important" in mobile_css
    assert "overflow-x: auto !important" in mobile_css
    assert "flex-wrap: nowrap !important" in mobile_css
    assert "justify-content: flex-start !important" in mobile_css
    assert '.gradio-container button[role="tab"]' in mobile_css
    assert '.gradio-container [role="tab"]' in mobile_css
    assert '} button[role="tab"], [role="tab"],' in mobile_css
    assert ' } [role="tab"] *, .gradio-container [role="tab"] * {' in mobile_css
    assert "flex: 0 0 auto !important" in mobile_css
    assert "width: auto !important" in mobile_css
    assert "min-width: max-content !important" in mobile_css
    assert "max-width: none !important" in mobile_css
    assert "overflow: visible !important" in mobile_css
    assert "text-overflow: clip !important" in mobile_css
    assert '.gradio-container [role="tab"] * { min-width: max-content !important; max-width: none !important; white-space: nowrap !important; overflow: visible !important; text-overflow: clip !important; }' in mobile_css


def test_webui_light_ops_console_css_themes_real_gradio_surfaces():
    css = webui.WEBUI_CSS
    assert "--skillopt-bg: #f6f3ee" in css
    assert "color-scheme: light" in css
    assert "--skillopt-bg: #0b0c0f" not in css
    assert "color-scheme: dark" not in css
    for selector in (
        ".gradio-container .wrap",
        ".gradio-container .form",
        ".gradio-container .block",
        ".gradio-container .panel",
        ".gradio-container .tabs",
        ".gradio-container .tabitem",
        ".gradio-container textarea",
        ".gradio-container input",
        ".gradio-container select",
        ".gradio-container .cm-editor",
    ):
        assert selector in css
    assert "--skillopt-panel: rgba(255,255,255,.76)" in css
    assert ".gradio-container .wrap, .gradio-container .form, .gradio-container .block, .gradio-container .panel { background: transparent !important" in css
    assert "background: var(--skillopt-panel-solid) !important" in css
    assert "background: var(--skillopt-accent-soft) !important" in css


def test_webui_polish_avoids_generic_wrap_panels_and_hides_loader_shells():
    css = webui.WEBUI_CSS
    assert ".gradio-container .wrap.center.full.hide" in css
    assert ".gradio-container .hide" in css
    assert ".gradio-container .loader-container" in css
    assert "display: none !important" in css
    assert ".gradio-container .wrap, .gradio-container .form, .gradio-container .block, .gradio-container .panel { background: transparent !important" in css
    assert ".gradio-container .wrap, .gradio-container .form, .gradio-container .block, .gradio-container .panel { background: var(--skillopt-panel)" not in css
    assert "textarea, input, .wrap" not in css


def test_webui_does_not_style_generic_prose_as_card_panel():
    css = webui.WEBUI_CSS
    panel_rules = [
        rule for rule in css.splitlines()
        if "border: 1px solid var(--skillopt-border)" in rule
        and "background: rgba(255,255,255,.72)" in rule
        and "border-radius: 16px" in rule
    ]
    assert panel_rules == [
        ".skillopt-status-md.prose, .skillopt-status-md .prose, .skillopt-result-md.prose, .skillopt-result-md .prose { border: 1px solid var(--skillopt-border) !important; background: rgba(255,255,255,.72) !important; border-radius: 16px !important; padding: 14px 16px !important; }"
    ]
    assert ".gradio-container .prose" not in panel_rules[0]
    assert ".skillopt-hero {" in css and "background: linear-gradient" in css
    assert ".skillopt-card {" in css and "background: var(--skillopt-panel)" in css


def test_webui_blocks_kwargs_injects_pwa_head_when_supported():
    class FakeBlocks:
        def __init__(self, *, title=None, css=None, js=None, head=None):
            pass

    class FakeGr:
        Blocks = FakeBlocks

    kwargs = webui.blocks_kwargs(FakeGr)
    assert kwargs["title"] == "Hermes SkillOpt"
    assert kwargs["css"] == webui.WEBUI_CSS
    assert kwargs["js"] == webui.pwa_startup_js()
    assert kwargs["head"] == webui.pwa_head_html()


def test_webui_pwa_startup_js_patches_real_gradio_head_defaults():
    js = webui.pwa_startup_js()
    assert 'ensureLink("manifest", "/manifest.webmanifest")' in js
    assert 'link.getAttribute("href") !== href) link.remove()' in js
    assert 'ensureLink("apple-touch-icon", "/icons/apple-touch-icon.png")' in js
    assert 'ensureMeta("viewport", "width=device-width, initial-scale=1, viewport-fit=cover")' in js
    assert 'ensureMeta("theme-color"' in js
    assert 'navigator.serviceWorker.register("/sw.js", { scope: "/" })' in js


def test_webui_launch_kwargs_injects_pwa_head_and_startup_js_when_supported():
    class FakeApp:
        def launch(self, *, server_name=None, server_port=None, share=None, inbrowser=None, css=None, js=None, head=None):
            pass

    kwargs = webui.launch_kwargs(FakeApp(), host="127.0.0.1", port=7861, share=False, browser=False)
    assert kwargs["css"] == webui.WEBUI_CSS
    assert kwargs["js"] == webui.pwa_startup_js()
    assert kwargs["head"] == webui.pwa_head_html()


def test_webui_launch_webui_attaches_pwa_routes_after_gradio_builds_server_app(monkeypatch):
    class FakeRoute:
        def __init__(self, path):
            self.path = path

    class FakeFastAPI:
        def __init__(self):
            self.routes = []
            self.calls = []

        def add_api_route(self, path, endpoint, **kwargs):
            self.routes.append(FakeRoute(path))
            self.calls.append((path, endpoint, kwargs))

    class FakeApp:
        def __init__(self):
            self.server_app = None
            self.launch_kwargs = None
            self.blocked = False

        def launch(self, *, server_name=None, server_port=None, share=None, inbrowser=None, prevent_thread_lock=False):
            self.launch_kwargs = {
                "server_name": server_name,
                "server_port": server_port,
                "share": share,
                "inbrowser": inbrowser,
                "prevent_thread_lock": prevent_thread_lock,
            }
            self.server_app = FakeFastAPI()

        def block_thread(self):
            self.blocked = True

    app = FakeApp()
    webui.launch_webui(app, host="127.0.0.1", port=7862, share=False, browser=False)
    assert app.launch_kwargs is not None
    assert app.launch_kwargs["prevent_thread_lock"] is True
    assert app.blocked is True
    assert app.server_app is not None
    paths = {call[0] for call in app.server_app.calls}
    assert "/manifest.webmanifest" in paths
    assert "/sw.js" in paths


def test_webui_attach_pwa_routes_accepts_concrete_fastapi_like_app():
    class FakeRoute:
        def __init__(self, path):
            self.path = path

    class FakeFastAPI:
        def __init__(self):
            self.routes = []
            self.calls = []

        def add_api_route(self, path, endpoint, **kwargs):
            self.routes.append(FakeRoute(path))
            self.calls.append((path, endpoint, kwargs))

    fastapi_app = FakeFastAPI()
    assert webui.attach_pwa_routes(fastapi_app) is True
    assert "/offline.html" in {call[0] for call in fastapi_app.calls}


def test_webui_service_worker_safe_cache_invariants():
    sw = webui.service_worker_js()
    assert "STATIC_ASSETS" in sw
    for path in webui.PWA_ASSET_PATHS:
        assert json.dumps(path) in sw
    assert 'url.pathname === "/"' in sw
    assert '"/api/"' in sw
    assert '"/run/"' in sw
    assert '"/queue/"' in sw
    assert 'request.method !== "GET"' in sw
    assert "url.origin !== self.location.origin" in sw
    assert "response.ok" in sw
    assert 'cache: "no-store"' in sw
    assert "await cache.put(url.pathname" in sw
    assert "await cache.put(\"/\"" not in sw
    assert "caches.match(\"/offline.html\")" in sw


def test_webui_offline_fallback_has_no_stale_private_data_and_no_store_headers():
    html = webui.offline_html()
    lowered = html.lower()
    assert "offline" in lowered
    for forbidden in ("run_id", "hermes_home", "skillopt/staging", "status:", "artifact_sha256"):
        assert forbidden not in lowered
    assert webui.pwa_response_headers()["Cache-Control"] == "no-store"
    assert webui.pwa_response_headers(static_asset=True)["Cache-Control"].startswith("public")


def test_webui_generated_icon_pngs_are_deterministic_and_valid():
    icon_a = webui.pwa_icon_png(192)
    icon_b = webui.pwa_icon_png(192)
    assert icon_a == icon_b
    assert icon_a.startswith(b"\x89PNG\r\n\x1a\n")
    assert webui.pwa_icon_png(180).startswith(b"\x89PNG")
    assert webui.pwa_icon_png(512).startswith(b"\x89PNG")
    with pytest.raises(ValueError):
        webui.pwa_icon_png(128)


def test_webui_attach_pwa_routes_registers_expected_paths():
    class FakeRoute:
        def __init__(self, path):
            self.path = path

    class FakeFastAPI:
        def __init__(self):
            self.routes = []
            self.calls = []

        def add_api_route(self, path, endpoint, **kwargs):
            self.routes.append(FakeRoute(path))
            self.calls.append((path, endpoint, kwargs))

    class FakeApp:
        def __init__(self):
            self.app = FakeFastAPI()

    app = FakeApp()
    assert webui.attach_pwa_routes(app) is True
    paths = {call[0] for call in app.app.calls}
    assert {
        "/manifest.webmanifest",
        "/sw.js",
        "/offline.html",
        "/favicon.svg",
        "/icons/skillopt-icon-192.png",
        "/icons/skillopt-icon-512.png",
        "/icons/apple-touch-icon.png",
    } <= paths
    assert getattr(app, "_skillopt_pwa_routes_attached") is True


def test_webui_build_app_uses_home_default_for_initial_status(monkeypatch, tmp_path):
    seen = {"status_home": None, "textbox_values": [], "code_kwargs": [], "load_js": None}

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

        def load(self, *args, **kwargs):
            seen["load_js"] = kwargs.get("js")
            return None

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

        @staticmethod
        def Code(*args, **kwargs):
            seen["code_kwargs"].append(kwargs)
            return Component(*args, **kwargs)

    monkeypatch.setattr(webui, "require_gradio", lambda: FakeGr)
    monkeypatch.setattr(webui, "status_markdown", fake_status)
    app = webui.build_app(str(tmp_path))
    assert isinstance(app, Context)
    assert seen["status_home"] == str(tmp_path)
    assert str(tmp_path) in seen["textbox_values"]
    assert {"label": "diff.patch"} in seen["code_kwargs"]
    assert all(kwargs.get("language") != "diff" for kwargs in seen["code_kwargs"])
    assert seen["load_js"] == webui.pwa_startup_js()


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
