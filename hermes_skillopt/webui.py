from __future__ import annotations

"""Hermes-native WebUI for hermes-skillopt.

Gradio is intentionally imported lazily so normal plugin imports/tests do not
require the optional web UI dependency.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hermes_skillopt import core

INSTALL_HINT = (
    "Gradio is required for the hermes-skillopt WebUI. Install the optional "
    "dependency with: python3 -m pip install 'hermes-skillopt[webui]' or "
    "python3 -m pip install gradio"
)
MAX_TEXT_CHARS = 20_000


def require_gradio():
    try:
        import gradio as gr  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by tests via monkeypatch
        raise RuntimeError(INSTALL_HINT) from exc
    return gr


def _json(data: Any) -> str:
    return core.redact_secrets(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _safe_artifact_path(run_dir: Path, filename: str) -> Path | None:
    """Return a safe fixed artifact path under run_dir, rejecting symlink escapes."""
    if Path(filename).name != filename:
        return None
    base = run_dir.resolve()
    path = base / filename
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    if path.is_symlink() or not core._is_relative_to(resolved, base) or not resolved.is_file():
        return None
    return resolved


def _read_artifact_limited(run_dir: Path, filename: str, limit: int = MAX_TEXT_CHARS) -> str:
    path = _safe_artifact_path(run_dir, filename)
    if path is None:
        return ""
    return core.redact_secrets(path.read_text(encoding="utf-8", errors="replace")[:limit])


def _run_dir(home: str | None, run_id: str) -> Path:
    return core.resolve_run_dir(core.hermes_home(home), run_id)


def latest_run_id(home: str | None = None) -> str:
    st = core.status(home)
    runs = st.get("recent_runs") or []
    if not runs:
        return ""
    return str(runs[0].get("run_id") or "")


def status_markdown(home: str | None = None) -> str:
    st = core.status(home)
    lines = [
        "## hermes-skillopt status",
        f"- success: {st.get('success')}",
        f"- hermes_home: `{st.get('hermes_home')}`",
        f"- skills_count: {st.get('skills_count')}",
        f"- staging: `{st.get('staging')}`",
        f"- backups: `{st.get('backups')}`",
        "",
        "### Recent staged runs",
    ]
    runs = st.get("recent_runs") or []
    if not runs:
        lines.append("- none")
    for r in runs[:10]:
        lines.append(
            "- `{run_id}` — {status} — adoptable={adoptable} prod_gate={prod} test_gate={test} — {skill} — {engine}{backend} — {created}".format(
                run_id=r.get("run_id") or "",
                status=r.get("status") or "unknown",
                adoptable=r.get("adoptable"),
                prod=r.get("production_gate_eligible"),
                test=r.get("test_gate_eligible"),
                skill=r.get("skill_name") or "unknown-skill",
                engine=r.get("engine") or "unknown-engine",
                backend=("/" + str(r.get("backend"))) if r.get("backend") else "",
                created=r.get("created_at") or "",
            )
        )
    return "\n".join(lines)


def review_payload(run_id: str | None = None, home: str | None = None) -> tuple[str, str, str, str, str, str]:
    rid = (run_id or "").strip() or latest_run_id(home)
    if not rid:
        return "No staged runs found.", "", "", "", "", ""
    try:
        rd = _run_dir(home, rid)
        manifest_text = _read_artifact_limited(rd, "manifest.json")
        if not manifest_text:
            raise ValueError("manifest.json missing or unsafe")
        manifest = json.loads(manifest_text)
        gate_text = _read_artifact_limited(rd, "gate_results.json")
        gate_data = manifest.get("gate")
        if gate_text:
            try:
                gate_data = json.loads(gate_text).get("best_gate")
            except Exception:
                gate_data = gate_text
        report = _read_artifact_limited(rd, "report.md")
        diff = _read_artifact_limited(rd, "diff.patch")
        gate = gate_text or _json(gate_data)
        candidate = _read_artifact_limited(rd, "proposed_SKILL.md") or _read_artifact_limited(rd, "best_skill.md")
        rejected = _read_artifact_limited(rd, "rejected_edits.jsonl")
        summary = [
            f"## Review `{rid}`",
            f"- status: {manifest.get('status')}",
            f"- skill: {manifest.get('skill_name')}",
            f"- adoptable: {manifest.get('adoptable')}",
            f"- production_gate_eligible: {manifest.get('production_gate_eligible')}",
            f"- test_gate_eligible: {manifest.get('test_gate_eligible')}",
            f"- not_adoptable_reasons: {manifest.get('production_eligibility_reasons') or []}",
            f"- validation_scores: current={manifest.get('validation_current_score')} candidate={manifest.get('validation_candidate_score')}",
            f"- production_scores: current={manifest.get('production_validation_current_score')} candidate={manifest.get('production_validation_candidate_score')}",
            f"- test_score: {manifest.get('test_score')}",
            f"- evaluator: {manifest.get('target_executor')} / {manifest.get('target_config_id')}",
            f"- accepted_for_adopt: {manifest.get('status') in ('staged_best', 'accepted', 'adopted') and manifest.get('adoptable') is True}",
            f"- run_dir: `{rd}`",
            f"- diff_path: `{rd / 'diff.patch'}`",
            f"- report_path: `{rd / 'report.md'}`",
        ]
        return "\n".join(summary), report, diff, gate, candidate, rejected
    except Exception as exc:
        return f"Review failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}", "", "", "", "", ""


def run_full_callback(
    skill: str | None,
    query: str | None,
    eval_file: str | None,
    lookback_days: int,
    limit: int,
    iterations: int,
    edit_budget: int,
    backend: str,
    allow_mock: bool,
    home: str | None,
) -> tuple[str, str, str, str, str, str, str]:
    """Run full cycle, always staged-only from the WebUI."""
    try:
        out = core.full_run(
            skill=skill or None,
            query=query or None,
            eval_file=eval_file or None,
            lookback_days=int(lookback_days),
            limit=int(limit),
            iterations=int(iterations),
            edit_budget=int(edit_budget),
            backend=backend or "auto",
            allow_mock=bool(allow_mock),
            auto_adopt=False,
            force=False,
            hermes_home_path=home or None,
        )
        rid = str(out.get("run_id") or "")
        summary = "## Full run complete (staged only)\n\n" + _json(out) + "\n\nNo skill was adopted. Use the Adopt tab with explicit confirmation if desired."
        return (summary, *review_payload(rid, home))
    except Exception as exc:
        return (f"Full run failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}", "", "", "", "", "", "")


def adopt_callback(run_id: str, confirmation: str, force: bool, home: str | None) -> str:
    rid = (run_id or "").strip()
    expected = f"ADOPT {rid}"
    if not rid:
        return "Adopt refused: run_id is required."
    if (confirmation or "").strip() != expected:
        return f"Adopt refused: type `{expected}` exactly to confirm."
    try:
        return "Adopt complete:\n\n```json\n" + _json(core.adopt(rid, hermes_home_path=None, force=bool(force))) + "\n```"
    except Exception as exc:
        return f"Adopt failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def rollback_callback(run_id: str, confirmation: str, force: bool, home: str | None) -> str:
    rid = (run_id or "").strip()
    expected = f"ROLLBACK {rid}"
    if not rid:
        return "Rollback refused: run_id is required."
    if (confirmation or "").strip() != expected:
        return f"Rollback refused: type `{expected}` exactly to confirm."
    try:
        return "Rollback complete:\n\n```json\n" + _json(core.rollback(rid, hermes_home_path=None, force=bool(force))) + "\n```"
    except Exception as exc:
        return f"Rollback failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def upstream_status_markdown(home: str | None = None) -> str:
    try:
        return "```json\n" + _json(core.upstream_status(hermes_home_path=home or None)) + "\n```"
    except Exception as exc:
        return f"Upstream status failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def upstream_update_markdown(home: str | None = None, fetch_only: bool = False) -> str:
    try:
        return "```json\n" + _json(core.upstream_update(hermes_home_path=home or None, repo_path=None, fetch_only=bool(fetch_only))) + "\n```"
    except Exception as exc:
        return f"Upstream update failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def build_app(home_default: str | None = None):
    gr = require_gradio()
    with gr.Blocks(title="Hermes SkillOpt WebUI") as app:
        gr.Markdown("# 🧠 Hermes SkillOpt WebUI")
        gr.Markdown(
            "Hermes-native adapter for the SkillOpt core pipeline: SKILL.md is trainable state, "
            "the target executor is frozen, optimizer backends only propose bounded edits, and a "
            "held-out validation gate is the sole acceptance gate. Hermes staged safety/adopt/rollback/profile isolation remains the outer shell."
        )
        home = gr.Textbox(label="HERMES_HOME override for staged read/run operations (optional)", placeholder="Defaults to active HERMES_HOME/~/.hermes; Adopt/Rollback always use active profile", value=home_default or "")

        with gr.Tabs():
            with gr.Tab("Status"):
                status_out = gr.Markdown(value=status_markdown(home_default))
                refresh = gr.Button("Refresh status")
                refresh.click(status_markdown, inputs=[home], outputs=[status_out])

            with gr.Tab("Full run (staged only)"):
                with gr.Row():
                    skill = gr.Textbox(label="Skill name/path", placeholder="Required if multiple skills exist")
                    query = gr.Textbox(label="Query/session search", placeholder="Optional")
                eval_file = gr.Textbox(label="Curated eval file (optional, staged-only)", placeholder="JSONL/JSON under HERMES_HOME; use expected/forbidden keywords for deterministic scoring")
                with gr.Row():
                    lookback = gr.Slider(0, 90, value=14, step=1, label="Lookback days")
                    limit = gr.Slider(1, 200, value=50, step=1, label="Harvest limit")
                    iterations = gr.Slider(1, 8, value=1, step=1, label="Iterations")
                    edit_budget = gr.Slider(0, 16, value=3, step=1, label="Edit budget")
                with gr.Row():
                    backend = gr.Dropdown(["auto", "hermes", "mock"], value="auto", label="Backend")
                    allow_mock = gr.Checkbox(value=False, label="Allow mock fallback (smoke/tests only)")
                run_btn = gr.Button("Run full cycle (staged only)", variant="primary")
                run_status = gr.Markdown()

            with gr.Tab("Review artifacts"):
                review_run_id = gr.Textbox(label="Run ID", placeholder="Blank = latest staged run")
                review_btn = gr.Button("Review")
                review_summary = gr.Markdown()
                report = gr.Markdown(label="report.md")
                diff = gr.Code(label="diff.patch", language="diff")
                gate = gr.Code(label="gate/candidate summary", language="json")
                candidate = gr.Code(label="proposed_SKILL.md", language="markdown")
                rejected = gr.Code(label="rejected_edits.jsonl", language="json")

            with gr.Tab("Adopt"):
                gr.Markdown("Adopt writes only to the active Hermes profile. The HERMES_HOME override textbox is ignored for live writeback.")
                adopt_run_id = gr.Textbox(label="Run ID")
                adopt_confirm = gr.Textbox(label="Confirmation", placeholder="Type: ADOPT <run_id>")
                adopt_force = gr.Checkbox(value=False, label="Force sha guard override")
                adopt_btn = gr.Button("Adopt staged proposal", variant="stop")
                adopt_out = gr.Markdown()

            with gr.Tab("Rollback"):
                gr.Markdown("Rollback writes only to the active Hermes profile. The HERMES_HOME override textbox is ignored for live writeback.")
                rollback_run_id = gr.Textbox(label="Run ID")
                rollback_confirm = gr.Textbox(label="Confirmation", placeholder="Type: ROLLBACK <run_id>")
                rollback_force = gr.Checkbox(value=False, label="Force sha guard override")
                rollback_btn = gr.Button("Rollback adopted run", variant="stop")
                rollback_out = gr.Markdown()

            with gr.Tab("Upstream"):
                gr.Markdown("Upstream status/update use the canonical clone under HERMES_HOME only.")
                fetch_only = gr.Checkbox(value=True, label="Fetch only")
                upstream_out = gr.Markdown()
                with gr.Row():
                    up_status_btn = gr.Button("Upstream status")
                    up_update_btn = gr.Button("Update/fetch pinned upstream")

        run_btn.click(
            run_full_callback,
            inputs=[skill, query, eval_file, lookback, limit, iterations, edit_budget, backend, allow_mock, home],
            outputs=[run_status, review_summary, report, diff, gate, candidate, rejected],
        )
        review_btn.click(review_payload, inputs=[review_run_id, home], outputs=[review_summary, report, diff, gate, candidate, rejected])
        adopt_btn.click(adopt_callback, inputs=[adopt_run_id, adopt_confirm, adopt_force, home], outputs=[adopt_out])
        rollback_btn.click(rollback_callback, inputs=[rollback_run_id, rollback_confirm, rollback_force, home], outputs=[rollback_out])
        up_status_btn.click(upstream_status_markdown, inputs=[home], outputs=[upstream_out])
        up_update_btn.click(upstream_update_markdown, inputs=[home, fetch_only], outputs=[upstream_out])
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_skillopt.webui", description="Launch the Hermes SkillOpt Gradio WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true")
    parser.add_argument("--browser", action="store_true", help="Open a browser after launch")
    parser.add_argument("--home", help="HERMES_HOME override for WebUI defaults and callbacks")
    args = parser.parse_args(argv)
    try:
        app = build_app(args.home)
    except RuntimeError as exc:
        if str(exc) == INSTALL_HINT:
            print(str(exc), file=sys.stderr)
            return 1
        raise
    app.launch(server_name=args.host, server_port=args.port, share=args.share, inbrowser=args.browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
