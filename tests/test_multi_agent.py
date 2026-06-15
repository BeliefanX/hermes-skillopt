from __future__ import annotations

import json
import subprocess
import sys

from hermes_skillopt import multi_agent


def test_generated_handoff_package_contains_required_delegate_fields():
    result = multi_agent.optimize_delegate_handoff(
        """Improve delegate_task handoff
Scope:
- optimize dispatcher to worker package only
Acceptance:
- includes explicit acceptance checks
Verification:
- run deterministic tests
"""
    )

    assert result["kind"] == "multi_agent_delegate_task_handoff"
    assert result["dispatcher_policy"]["mode"] == "dispatcher_worker_handoff"
    assert result["no_global_auto_adopt"] is True
    package = result["context_package"]
    for field in ("goal", "scope", "acceptance", "verification", "slim_output"):
        assert package[field]
    assert "required_fields" in package["slim_output"]
    assert "changed_files" in package["slim_output"]["required_fields"]


def test_acceptance_criteria_heading_does_not_parse_prefix_as_item():
    result = multi_agent.optimize_delegate_handoff(
        """Implement a small change
Acceptance Criteria:
- A
Verification:
- pytest
"""
    )

    assert result["context_package"]["acceptance"] == ["A"]


def test_scoring_penalizes_missing_acceptance_and_verbose_raw_logs():
    good = multi_agent.score_handoff({
        "goal": "x",
        "scope": ["bounded"],
        "acceptance": ["done when tests pass"],
        "verification": ["pytest"],
        "slim_output": {"required_fields": ["status"]},
    })
    bad = multi_agent.score_handoff("please do the thing and paste the full log/raw log/entire output transcript")

    assert bad.acceptance_omissions > good.acceptance_omissions
    assert bad.verbose_log_risk > good.verbose_log_risk
    assert bad.rework_risk > good.rework_risk
    assert bad.total_score < good.total_score


def test_scoring_respects_context_budget_chars():
    content = "x" * 1500
    base_package = {
        "goal": "x",
        "scope": ["bounded"],
        "acceptance": ["done when tests pass"],
        "verification": ["pytest"],
        "slim_output": {"required_fields": ["status"]},
        "requirements": content,
    }

    tight = multi_agent.score_handoff({**base_package, "context_budget_chars": 1000})
    defaultish = multi_agent.score_handoff({**base_package, "context_budget_chars": 6000})

    assert tight.context_size_chars == defaultish.context_size_chars
    assert tight.context_size_score < defaultish.context_size_score
    assert defaultish.context_size_score == 1.0


def test_recommendations_include_reviewer_escalation_and_retry_behavior():
    result = multi_agent.optimize_delegate_handoff("Implement feature without details")

    recs = "\n".join(result["recommendations"]).lower()
    assert "reviewer gate" in recs
    assert "escalate" in recs
    assert "retry" in recs
    assert result["escalation_rules"]["max_retries"] == 1
    assert result["reviewer_rubric"]["required_checks"]


def test_first_version_is_not_global_single_agent_auto_adopt():
    result = multi_agent.optimize_delegate_handoff("Optimize a handoff")

    assert result["dispatcher_policy"]["mode"] == "dispatcher_worker_handoff"
    assert result["dispatcher_policy"]["no_global_auto_adopt"] is True
    assert result["staged_only"] is True
    assert "global" in "\n".join(result["recommendations"]).lower()


def test_cli_handoff_optimize_outputs_json_without_llm_or_network():
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_skillopt.cli", "handoff-optimize", "Goal: reduce rework\nAcceptance:\n- report tests", "--worker", "coder"],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["context_package"]["acceptance"]
    assert payload["dispatcher_policy"]["default_worker"] == "coder"
