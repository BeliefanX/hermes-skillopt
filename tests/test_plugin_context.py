from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_plugin_module():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_skillopt_plugin_under_test", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePluginCtx:
    def __init__(self):
        self.registered = {}
        self.llm = object()

    def register_tool(self, *, name, toolset, schema, handler, emoji):
        self.registered[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "emoji": emoji,
        }


def test_registered_full_run_handler_binds_plugin_ctx(monkeypatch):
    plugin = load_plugin_module()
    ctx = FakePluginCtx()
    seen = {}

    def fake_full_run(**kwargs):
        seen.update(kwargs)
        return {"success": True, "ctx_is_plugin_ctx": kwargs.get("ctx") is ctx}

    monkeypatch.setattr(plugin.core, "full_run", fake_full_run)

    plugin.register(ctx)
    raw = ctx.registered["hermes_skillopt_full_run"]["handler"]({
        "skill": "demo",
        "optimizer_backend": "hermes",
        "allow_mock": False,
    }, ctx=None)

    payload = json.loads(raw)
    assert payload["ctx_is_plugin_ctx"] is True
    assert seen["ctx"] is ctx
    assert seen["optimizer_backend"] == "hermes"
    assert seen["allow_mock"] is False
