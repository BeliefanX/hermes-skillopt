from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _plugin_module():
    spec = importlib.util.spec_from_file_location("hermes_skillopt_plugin_root", REPO / "__init__.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin_yaml_tools() -> list[str]:
    tools: list[str] = []
    in_tools = False
    for line in (REPO / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("provides_tools:"):
            in_tools = True
            continue
        if in_tools and line.startswith("  - "):
            tools.append(line.removeprefix("  - ").strip())
    return tools


def _pyproject_value(key: str) -> str:
    prefix = f"{key} = "
    for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip().strip('"')
    raise AssertionError(f"missing pyproject key: {key}")


def test_plugin_yaml_pyproject_and_registered_tools_are_consistent():
    plugin = _plugin_module()
    pyproject_text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    yaml_text = (REPO / "plugin.yaml").read_text(encoding="utf-8")

    assert _pyproject_value("name") == "hermes-skillopt"
    assert f"version: {_pyproject_value('version')}" in yaml_text
    assert 'hermes-skillopt = "hermes_skillopt.cli:main"' in pyproject_text
    assert _plugin_yaml_tools() == [name for name, *_ in plugin._TOOLS]


def test_cli_help_and_plugin_schema_expose_phase4_surface():
    plugin = _plugin_module()
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_skillopt.cli", "full-run", "--help"],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    assert proc.returncode == 0
    for option in ("--optimizer-backend", "--target-backend", "--gate-mode", "--candidate-count", "--resume-run-id"):
        assert option in proc.stdout
    assert "default strict" in proc.stdout

    full_props = plugin.SCHEMAS["hermes_skillopt_full_run"]["parameters"]["properties"]
    for prop in ("optimizer_backend", "target_backend", "gate_mode", "candidate_count", "resume_run_id"):
        assert prop in full_props
    assert full_props["gate_mode"]["default"] == "strict"


def test_docs_reference_current_phase4_invariants():
    combined = "\n".join(
        (REPO / path).read_text(encoding="utf-8")
        for path in ("README.md", "UPSTREAM.md", "docs/architecture-gap.md", "docs/release-notes.md")
    )
    combined_lower = combined.lower()
    for phrase in (
        "skillopt-provenance-v2",
        "optimizer_backend",
        "target_backend",
        "hard",
        "mixed",
        "checkpoint.json",
        "slow_meta.json",
        "envadapter",
        "intentional divergence",
        "does not merge",
    ):
        assert phrase in combined_lower