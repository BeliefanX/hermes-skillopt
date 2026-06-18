from __future__ import annotations

"""Frozen target evaluator abstractions for current/candidate comparisons.

Default tests and smoke runs use deterministic local scorecard/replay runners.
Curated tasks can opt into a production-safe Hermes sandbox runner that creates
isolated temp HOME/HERMES_HOME/workspace directories, writes a staged skill copy,
runs only the fixed internal sandbox runner, and captures transcript/exit/evidence
with timeouts.  Task-provided commands are blocked by default.
"""

import os
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from hermes_skillopt.env import EvalResult, EvalTask, production_eligibility_for_task


class ScorecardRunner(Protocol):
    @property
    def mode(self) -> str: ...

    def score(self, skill_text: str, task: EvalTask) -> EvalResult: ...


TRACE_SCHEMA_VERSION = "hermes-target-trace-v1"
FROZEN_HERMES_CONTRACT = "frozen_hermes_target_execution_v1"
FROZEN_TARGET_MODEL_ID = "no-live-model-deterministic-target-v1"
FROZEN_PROFILE_POLICY = "isolated-or-readonly-profile-no-live-profile-writes"
FROZEN_TOOL_POLICY = "task-commands-never-executed; fixture-tools-recorded-or-fixed-runner-only"


def _stable_json_sha(data: object) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _preview(text: str, limit: int = 500) -> str:
    return (text or "")[:limit]


def _safe_fixture_list(value: object, *, limit: int = 12) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)[:limit]
    return [value]


def _critical_keyword_missing(low_text: str, task: EvalTask) -> list[str]:
    return [term for term in task.all_required_keywords if not any(v in low_text for v in _variants(term))]


def _critical_marker_missing(low_text: str, task: EvalTask) -> list[str]:
    return [marker for marker in task.required_markers if marker.lower() not in low_text]


def _variants(term: str) -> set[str]:
    term_l = term.lower()
    variants = {term_l}
    if term_l == "verify":
        variants |= {"verification", "verified"}
    if term_l == "test":
        variants |= {"pytest", "tests"}
    if term_l == "guard":
        variants |= {"path guard", "sha", "safety"}
    if term_l == "staged":
        variants |= {"stage", "staging", "staged-only"}
    return variants


@dataclass(frozen=True)
class DeterministicKeywordScorecard:
    """Deterministic/mock fallback scorecard for smoke tasks."""

    mode: str = "deterministic_mock_scorecard"

    def config_parameters(self) -> dict[str, object]:
        return {
            "trace_schema": TRACE_SCHEMA_VERSION,
            "backend_kind": "deterministic_fallback_scorecard",
            "scoring_policy": "deterministic_keyword_scorecard_v1",
            "deterministic": True,
            "review_only_unless_curated_pack": True,
            "judge_can_accept": False,
            "executes_task_commands": False,
            "task_command_policy": "not_supported",
        }

    def score(self, skill_text: str, task: EvalTask) -> EvalResult:
        low = skill_text.lower()
        score = 0.20
        matched: list[str] = []
        expected = task.expected_terms or ("verify", "tool")
        for term in expected:
            if any(v in low for v in _variants(term)):
                score += 0.12
                matched.append(term)
        missing_required_keywords = _critical_keyword_missing(low, task)
        missing_required_markers = _critical_marker_missing(low, task)
        if "skillopt learned rules" in low or "skillopt candidate improvements" in low:
            score += 0.12
            matched.append("learned_rules")
        if "validation" in low and "gate" in low:
            score += 0.10
            matched.append("validation_gate")
        if "bounded" in low and "edit" in low:
            score += 0.08
            matched.append("bounded_edit")
        penalties = [term for term in task.failure_terms if term.lower() in low]
        penalties.extend([m for m in task.forbidden_markers if m.lower() in low])
        score = round(max(0.0, min(1.0, score - 0.15 * len(penalties))), 3)
        failure_tags = [f"missing_required_keyword:{x}" for x in missing_required_keywords] + [f"missing_required_marker:{x}" for x in missing_required_markers] + [f"forbidden_marker:{x}" for x in penalties]
        passed = score >= 0.55 and not failure_tags
        return EvalResult(task.id, score, passed, f"runner={self.mode}; matched={matched}; penalties={penalties}; failure_tags={failure_tags}", {**_result_metadata(task), "runner_label": "deterministic_fallback_review_only", "task_commands_executed": False, "failure_tags": failure_tags, "missing_required_keywords": missing_required_keywords, "missing_required_markers": missing_required_markers, "hard_pass": passed, "soft_score": score})


@dataclass(frozen=True)
class HermesRolloutRunner:
    """Safe deterministic replay runner for curated Hermes eval tasks.

    This runner never executes task-provided commands.  Instead it replays a
    frozen fixture/scorecard into structured trajectory evidence so current and
    candidate skills differ only by skill_text under the same target config.
    """

    mode: str = "hermes_trace_replay_runner_v1"

    def config_parameters(self) -> dict[str, object]:
        return {
            "trace_schema": TRACE_SCHEMA_VERSION,
            "backend_kind": "deterministic_trace_replay",
            "scoring_policy": "deterministic_keywords_assertions_markers_v1",
            "deterministic": True,
            "review_only_unless_curated_pack": True,
            "judge_can_accept": False,
            "executes_task_commands": False,
            "task_command_policy": "record_and_block",
            "fixture_tool_policy": "record_only_never_execute",
        }

    def score(self, skill_text: str, task: EvalTask) -> EvalResult:
        low = skill_text.lower()
        checks: list[tuple[str, bool, float]] = []
        for term in task.expected_terms:
            checks.append((f"expected_keyword:{term}", any(v in low for v in _variants(term)), 1.0))
        for term in task.all_required_keywords:
            checks.append((f"all_required_keyword:{term}", any(v in low for v in _variants(term)), 1.0))
        for assertion in task.assertions:
            name, passed = self._assertion_passed(low, assertion)
            checks.append((name, passed, float(assertion.get("weight", 1.0) or 1.0)))
        for marker in task.required_markers:
            checks.append((f"required_marker:{marker}", marker.lower() in low, 1.0))
        for criterion in task.success_criteria:
            words = criterion.lower().split()
            if 0 < len(words) <= 3:
                checks.append((f"success_criteria:{criterion}", criterion.lower() in low, 0.75))
        if not checks and task.expected_behavior:
            for word in task.expected_behavior.lower().split():
                if len(word) >= 4:
                    checks.append((f"expected_behavior:{word}", word in low, 0.25))
        penalties = [term for term in task.failure_terms if term.lower() in low]
        penalties.extend([m for m in task.forbidden_markers if m.lower() in low])
        missing_required_keywords = _critical_keyword_missing(low, task)
        missing_required_markers = _critical_marker_missing(low, task)
        command_blocked = task.fixtures.get("command") is not None or task.metadata.get("command") is not None
        critical_failures = [f"missing_required_keyword:{x}" for x in missing_required_keywords] + [f"missing_required_marker:{x}" for x in missing_required_markers] + [f"forbidden_marker:{x}" for x in penalties]
        failure_tags = list(dict.fromkeys((["task_provided_command_blocked"] if command_blocked else []) + critical_failures + [f"penalty:{p}" for p in penalties]))
        score, passed_names, failed_names = _score_checks(checks, penalties)
        trajectory = self._trajectory(skill_text, task, checks, penalties, passed_names, failed_names, score, failure_tags)
        hard_pass = score >= 0.55 and not penalties and not command_blocked and not missing_required_keywords and not missing_required_markers
        return EvalResult(
            task.id,
            score,
            hard_pass,
            f"runner={self.mode}; passed={passed_names}; failed={failed_names}; penalties={penalties}; failure_tags={failure_tags}; judge=not_used_for_acceptance",
            {
                **_result_metadata(task),
                "assertion_count": len(task.assertions),
                "assertion_results": {name: ok for name, ok, _ in checks if name.startswith("assertion:")},
                "judge_present": bool(task.judge),
                "trajectory": trajectory,
                "trace": trajectory,
                "trace_schema": TRACE_SCHEMA_VERSION,
                "trace_fingerprint_sha256": _stable_json_sha(trajectory),
                "failure_tags": failure_tags,
                "missing_required_keywords": missing_required_keywords,
                "missing_required_markers": missing_required_markers,
                "sandbox_command_blocked": command_blocked,
                "task_commands_executed": False,
                "runner_label": "deterministic_replay_review_only_unless_curated_pack",
                "hard_pass": hard_pass,
                "soft_score": score,
            },
        )

    def _trajectory(self, skill_text: str, task: EvalTask, checks: list[tuple[str, bool, float]], penalties: list[str], passed_names: list[str], failed_names: list[str], score: float, failure_tags: list[str]) -> dict[str, object]:
        allowed_tools = set(task.allowed_tools or ())
        tool_calls: list[dict[str, object]] = []
        for idx, raw in enumerate(_safe_fixture_list(task.fixtures.get("tool_calls")), 1):
            if isinstance(raw, dict):
                name = str(raw.get("name") or raw.get("tool") or "fixture_tool")
                args = raw.get("args") or raw.get("arguments") or {}
            else:
                name, args = str(raw), {}
            allowed = not allowed_tools or name in allowed_tools
            tool_calls.append({"id": f"fixture-tool-{idx}", "name": name, "args_preview": _preview(json.dumps(args, ensure_ascii=False, default=str), 400), "allowed": allowed, "executed": False, "blocked_reason": None if allowed else "tool_not_in_allowed_tools"})
        if task.fixtures.get("command") is not None or task.metadata.get("command") is not None:
            tool_calls.append({"id": "task-command", "name": "task_provided_command", "args_preview": _preview(str(task.fixtures.get("command") or task.metadata.get("command")), 400), "allowed": False, "executed": False, "blocked_reason": "arbitrary_command_execution_disabled"})
        observations = []
        for idx, raw in enumerate(_safe_fixture_list(task.fixtures.get("observations")), 1):
            observations.append({"id": f"fixture-observation-{idx}", "content_preview": _preview(json.dumps(raw, ensure_ascii=False, default=str) if not isinstance(raw, str) else raw, 800)})
        observations.extend({"id": f"check-{i}", "check": name, "passed": ok, "weight": weight} for i, (name, ok, weight) in enumerate(checks, 1))
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "task_id": task.id,
            "messages": [
                {"role": "user", "content_preview": _preview(task.prompt, 800)},
                {"role": "assistant", "content_preview": _preview(skill_text, 800), "skill_sha256": _stable_json_sha({"skill_text": skill_text})},
            ],
            "tool_calls": tool_calls,
            "observations": observations,
            "scores": {"score": score, "passed_checks": passed_names, "failed_checks": failed_names, "penalties": penalties},
            "failure_tags": failure_tags,
            "replay_deterministic": True,
            "task_commands_executed": False,
        }

    def _assertion_passed(self, low_skill: str, assertion: dict[str, object]) -> tuple[str, bool]:
        typ = str(assertion.get("type") or assertion.get("op") or "contains").lower()
        value = str(assertion.get("value") or assertion.get("text") or assertion.get("keyword") or "").lower()
        if typ in {"contains", "keyword", "must_contain"}:
            return f"assertion:{typ}:{value}", bool(value and value in low_skill)
        if typ in {"not_contains", "forbidden", "must_not_contain"}:
            return f"assertion:{typ}:{value}", bool(value and value not in low_skill)
        if typ == "all_keywords":
            raw_values = assertion.get("values") or assertion.get("keywords") or []
            values = [raw_values] if isinstance(raw_values, str) else list(raw_values) if isinstance(raw_values, (list, tuple, set)) else []
            vals = [str(v).lower() for v in values]
            return f"assertion:all_keywords:{','.join(vals)}", bool(vals and all(v in low_skill for v in vals))
        return f"assertion:unsupported:{typ}", False


CommandRunner = Callable[[list[str], Path, dict[str, str], float], tuple[int, str]]


def subprocess_command_runner(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=str(cwd), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return proc.returncode, proc.stdout


@dataclass(frozen=True)
class HermesSandboxRunner:
    """Production-safe sandbox executor MVP.

    The runner creates an isolated temp profile/home/workspace, writes SKILL.md
    into the sandbox only, and executes a fixed internal runner.  Task-provided
    fixture/metadata commands are deliberately rejected: without an OS-level
    sandbox, arbitrary shell commands could still access the live filesystem.
    """

    command_runner: CommandRunner | None = None
    mode: str = "hermes_sandbox_executor_mvp"

    def config_parameters(self) -> dict[str, object]:
        return {
            "trace_schema": TRACE_SCHEMA_VERSION,
            "backend_kind": "sandbox_fixed_internal_runner",
            "fixed_internal_runner": "python -m hermes_skillopt.sandbox_runner",
            "task_command_policy": "reject_before_subprocess",
            "fixture_tool_policy": "fixed_runner_only_no_task_shell",
            "deterministic": False,
            "review_only_unless_curated_pack": True,
            "isolated_home": True,
            "live_profile_writes": False,
            "frozen_hermes_contract": FROZEN_HERMES_CONTRACT,
            "provider": "local-subprocess",
            "model": "fixed-internal-sandbox-runner",
            "toolset": ["hermes_skillopt.sandbox_runner"],
            "session_policy": "ephemeral_temp_session_per_task",
            "path": os.defpath,
        }

    def score(self, skill_text: str, task: EvalTask) -> EvalResult:
        if task.fixtures.get("command") is not None or task.metadata.get("command") is not None:
            transcript = "SANDBOX_COMMAND_BLOCKED: task-provided commands are not allowed by the sandbox executor MVP"
            metadata = {
                **_result_metadata(task),
                "exit_code": 126,
                "transcript_preview": transcript,
                "sandbox_isolated": True,
                "sandbox_command_blocked": True,
                "live_profile_writes": False,
                "frozen_hermes_contract": FROZEN_HERMES_CONTRACT,
                "model_fingerprint": {"provider": "local-subprocess", "model": "fixed-internal-sandbox-runner", "fingerprint_sha256": _stable_json_sha({"provider": "local-subprocess", "model": "fixed-internal-sandbox-runner"})},
                "profile_fingerprint": {"policy": "temp-isolated-deleted-after-run", "live_profile_writes": False, "fingerprint_sha256": _stable_json_sha({"policy": "temp-isolated-deleted-after-run", "live_profile_writes": False})},
                "toolset_fingerprint": {"toolset": ["hermes_skillopt.sandbox_runner"], "task_commands_allowed": False, "fingerprint_sha256": _stable_json_sha({"toolset": ["hermes_skillopt.sandbox_runner"], "task_commands_allowed": False})},
                "session_fingerprint": {"session_policy": "blocked-before-subprocess", "task_id": task.id, "fingerprint_sha256": _stable_json_sha({"task_id": task.id, "blocked": True})},
                "execution_scoring": "blocked task-provided command; no task shell executed",
                "production_gate_eligible": False,
                "trace_schema": TRACE_SCHEMA_VERSION,
                "failure_tags": ["task_provided_command_blocked"],
                "task_commands_executed": False,
                "runner_label": "sandbox_fixed_runner_review_only_unless_curated_pack",
                "hard_pass": False,
                "soft_score": 0.0,
                "trajectory": {
                    "schema_version": TRACE_SCHEMA_VERSION,
                    "task_id": task.id,
                    "messages": [{"role": "user", "content_preview": _preview(task.prompt, 800)}],
                    "tool_calls": [{"id": "task-command", "name": "task_provided_command", "allowed": False, "executed": False, "blocked_reason": "arbitrary_command_execution_disabled"}],
                    "observations": [{"id": "sandbox-block", "content_preview": transcript}],
                    "scores": {"score": 0.0, "passed_checks": [], "failed_checks": ["sandbox_command_blocked"], "penalties": []},
                    "failure_tags": ["task_provided_command_blocked"],
                    "task_commands_executed": False,
                },
            }
            return EvalResult(task.id, 0.0, False, f"runner={self.mode}; exit=126; blocked=task_provided_command", metadata)

        runner = self.command_runner or subprocess_command_runner
        with tempfile.TemporaryDirectory(prefix="hermes-skillopt-sandbox-") as td:
            root = Path(td)
            sandbox_home = root / "home"
            workspace = root / "workspace"
            profile = sandbox_home / ".hermes"
            skill_dir = profile / "skills" / "sandbox"
            workspace.mkdir(parents=True)
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(skill_text, encoding="utf-8")
            argv = [sys.executable, "-m", "hermes_skillopt.sandbox_runner", str(skill_dir / "SKILL.md")]
            repo_root = str(Path(__file__).resolve().parents[1])
            env = {
                "HOME": str(sandbox_home),
                "HERMES_HOME": str(profile),
                "HERMES_SKILLOPT_SANDBOX": "1",
                "PYTHONPATH": repo_root,
                "PATH": os.defpath,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            try:
                code, transcript = runner(argv, workspace, env, float(task.timeout))
            except subprocess.TimeoutExpired as exc:
                code, transcript = 124, f"TIMEOUT after {task.timeout}s\n{exc.stdout or ''}"
            except Exception as exc:  # pragma: no cover - defensive executor path
                code, transcript = 125, f"SANDBOX_RUNNER_ERROR {type(exc).__name__}: {exc}"
        low_skill = skill_text.lower()
        low_transcript = transcript.lower()
        checks: list[tuple[str, bool, float]] = [("exit_zero", code == 0, 1.0)]
        for term in task.expected_terms:
            checks.append((f"expected_keyword:{term}", any(v in low_skill for v in _variants(term)), 0.75))
        for term in task.all_required_keywords:
            checks.append((f"all_required_keyword:{term}", any(v in low_skill for v in _variants(term)), 1.0))
        for marker in task.required_markers:
            checks.append((f"required_marker:{marker}", marker.lower() in low_transcript or marker.lower() in low_skill, 1.0))
        for assertion in task.assertions:
            name, ok = HermesRolloutRunner()._assertion_passed(low_skill + "\n" + low_transcript, assertion)
            checks.append((name, ok, float(assertion.get("weight", 1.0) or 1.0)))
        penalties = [term for term in task.failure_terms if term.lower() in low_skill or term.lower() in low_transcript]
        penalties.extend([m for m in task.forbidden_markers if m.lower() in low_transcript or m.lower() in low_skill])
        missing_required_keywords = _critical_keyword_missing(low_skill, task)
        missing_required_markers = [marker for marker in task.required_markers if marker.lower() not in low_skill and marker.lower() not in low_transcript]
        critical_failures = [f"missing_required_keyword:{x}" for x in missing_required_keywords] + [f"missing_required_marker:{x}" for x in missing_required_markers] + [f"forbidden_marker:{x}" for x in penalties]
        score, passed_names, failed_names = _score_checks(checks, penalties)
        hard_pass = code == 0 and score >= 0.55 and not penalties and not missing_required_keywords and not missing_required_markers
        metadata = {**_result_metadata(task), "exit_code": code, "transcript_preview": transcript[:4000], "sandbox_isolated": True, "sandbox_command_blocked": False, "live_profile_writes": False, "trace_schema": TRACE_SCHEMA_VERSION, "failure_tags": critical_failures + [f"penalty:{p}" for p in penalties], "missing_required_keywords": missing_required_keywords, "missing_required_markers": missing_required_markers, "task_commands_executed": False, "runner_label": "sandbox_fixed_runner_review_only_unless_curated_pack", "hard_pass": hard_pass, "soft_score": score, "trajectory": {"schema_version": TRACE_SCHEMA_VERSION, "task_id": task.id, "messages": [{"role": "user", "content_preview": _preview(task.prompt, 800)}, {"role": "assistant", "skill_sha256": _stable_json_sha({"skill_text": skill_text}), "content_preview": _preview(skill_text, 800)}], "tool_calls": [{"id": "fixed-sandbox-runner", "name": "hermes_skillopt.sandbox_runner", "allowed": True, "executed": True, "blocked_reason": None}], "observations": [{"id": "sandbox-transcript", "content_preview": transcript[:4000]}], "scores": {"score": score, "passed_checks": passed_names, "failed_checks": failed_names, "penalties": penalties}, "failure_tags": critical_failures + [f"penalty:{p}" for p in penalties], "task_commands_executed": False}}
        metadata.update({
            "frozen_hermes_contract": FROZEN_HERMES_CONTRACT,
            "model_fingerprint": {"provider": "local-subprocess", "model": "fixed-internal-sandbox-runner", "fingerprint_sha256": _stable_json_sha({"provider": "local-subprocess", "model": "fixed-internal-sandbox-runner"})},
            "profile_fingerprint": {"home_policy": "temp-isolated", "hermes_home_policy": "temp-isolated", "workspace_policy": "temp-isolated", "live_profile_writes": False, "fingerprint_sha256": _stable_json_sha({"home": "temp", "hermes_home": "temp", "workspace": "temp", "live_profile_writes": False})},
            "toolset_fingerprint": {"toolset": ["hermes_skillopt.sandbox_runner"], "task_commands_allowed": False, "fingerprint_sha256": _stable_json_sha({"toolset": ["hermes_skillopt.sandbox_runner"], "task_commands_allowed": False})},
            "session_fingerprint": {"session_policy": "ephemeral_temp_session_per_task", "task_id": task.id, "fingerprint_sha256": _stable_json_sha({"session_policy": "ephemeral_temp_session_per_task", "task_id": task.id})},
            "execution_scoring": "exit_code + transcript required_markers/assertions + skill expected terms",
            "passed_checks": passed_names,
            "failed_checks": failed_names,
        })
        metadata["trace_fingerprint_sha256"] = _stable_json_sha(metadata["trajectory"])
        return EvalResult(task.id, score, hard_pass, f"runner={self.mode}; exit={code}; passed={passed_names}; failed={failed_names}; penalties={penalties}; critical_failures={critical_failures}", metadata)


def _score_checks(checks: list[tuple[str, bool, float]], penalties: list[str]) -> tuple[float, list[str], list[str]]:
    total = sum(max(0.0, weight) for _, _, weight in checks)
    passed_weight = sum(max(0.0, weight) for _, ok, weight in checks if ok)
    base = passed_weight / total if total > 0 else 0.0
    score = round(max(0.0, min(1.0, base - 0.20 * len(penalties))), 3)
    return score, [name for name, ok, _ in checks if ok], [name for name, ok, _ in checks if not ok]


@dataclass(frozen=True)
class LiveHermesReadOnlyRunner:
    """Disabled-by-default read-only live Hermes target adapter interface.

    This v1 adapter records the requested model/provider/profile/toolset/session
    fingerprint but does not invoke external services unless a future controlled
    integration explicitly enables it.  Even when configured, it is report-only
    and cannot adopt/writeback.
    """

    provider: str = "disabled"
    model: str = "disabled"
    profile_home: str | None = None
    toolset: tuple[str, ...] = ()
    session_id: str | None = None
    mode: str = "hermes_live_readonly_adapter_v1_disabled"

    def config_parameters(self) -> dict[str, object]:
        payload = {
            "trace_schema": TRACE_SCHEMA_VERSION,
            "backend_kind": "live_hermes_readonly_adapter_interface",
            "enabled": False,
            "report_only": True,
            "production_adopt_allowed": False,
            "provider": self.provider,
            "model": self.model,
            "profile_home": self.profile_home,
            "toolset": list(self.toolset),
            "session_id": self.session_id,
            "live_profile_writes": False,
            "task_command_policy": "never_execute_task_commands",
        }
        return {**payload, "adapter_fingerprint_sha256": _stable_json_sha(payload)}

    def score(self, skill_text: str, task: EvalTask) -> EvalResult:
        cfg = self.config_parameters()
        transcript = "LIVE_HERMES_READONLY_DISABLED: adapter interface recorded fingerprints only; no live invocation performed"
        trajectory = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "task_id": task.id,
            "messages": [{"role": "user", "content_preview": _preview(task.prompt, 800)}],
            "tool_calls": [],
            "observations": [{"id": "live-readonly-disabled", "content_preview": transcript}],
            "scores": {"score": 0.0, "passed_checks": [], "failed_checks": ["live_adapter_disabled"], "penalties": []},
            "task_commands_executed": False,
        }
        return EvalResult(
            task.id,
            0.0,
            False,
            f"runner={self.mode}; disabled=true; report_only=true",
            {
                **_result_metadata(task),
                "runner_label": "live_hermes_readonly_disabled_report_only",
                "target_adapter_config": cfg,
                "model_fingerprint": {"provider": self.provider, "model": self.model},
                "profile_fingerprint": {"profile_home": self.profile_home, "fingerprint_sha256": _stable_json_sha({"profile_home": self.profile_home})},
                "toolset_fingerprint": {"toolset": list(self.toolset), "fingerprint_sha256": _stable_json_sha(list(self.toolset))},
                "session_fingerprint": {"session_id": self.session_id, "fingerprint_sha256": _stable_json_sha({"session_id": self.session_id})},
                "live_profile_writes": False,
                "production_gate_eligible": False,
                "production_adopt_allowed": False,
                "task_commands_executed": False,
                "hard_pass": False,
                "soft_score": 0.0,
                "trace_schema": TRACE_SCHEMA_VERSION,
                "trajectory": trajectory,
                "trace_fingerprint_sha256": _stable_json_sha(trajectory),
            },
        )


@dataclass(frozen=True)
class TargetBackendConfig:
    """Explicit frozen target backend identity for provenance/artifacts."""

    executor: str
    target_config_id: str = "frozen-hermes-trace-replay-v2"
    requested_executor: str = "auto"
    role: str = "frozen_current_candidate_evaluator_no_editing"
    model_id: str = FROZEN_TARGET_MODEL_ID
    profile_policy: str = FROZEN_PROFILE_POLICY
    tool_policy: str = FROZEN_TOOL_POLICY
    parameters: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": "skillopt-target-backend-config-v2",
            "executor": self.executor,
            "target_config_id": self.target_config_id,
            "requested_executor": self.requested_executor,
            "role": self.role,
            "model_id": self.model_id,
            "profile_policy": self.profile_policy,
            "tool_policy": self.tool_policy,
            "live_tool_execution_allowed": False,
            "task_provided_commands_allowed": False,
            "parameters": self.parameters,
        }
        return {**payload, "fingerprint_sha256": _stable_json_sha(payload)}


@dataclass(frozen=True)
class TargetExecutor:
    runner: ScorecardRunner | None = None
    target_config_id: str = "frozen-hermes-trace-replay-v2"
    requested_executor: str = "auto"

    def __post_init__(self) -> None:
        if self.runner is None:
            object.__setattr__(self, "runner", HermesRolloutRunner())

    @property
    def mode(self) -> str:
        assert self.runner is not None
        return self.runner.mode

    @property
    def config(self) -> TargetBackendConfig:
        parameters = getattr(self.runner, "config_parameters", lambda: {})()
        return TargetBackendConfig(executor=self.mode, target_config_id=self.target_config_id, requested_executor=self.requested_executor, parameters=dict(parameters or {}))

    def _weighted_pass_rate(self, results: list[EvalResult], tasks: list[EvalTask]) -> float:
        total_weight = sum(max(0.0, float(task.weight)) for task in tasks)
        if not results or total_weight <= 0:
            return 0.0
        passed = sum(max(0.0, float(task.weight)) for result, task in zip(results, tasks) if result.passed)
        return round(passed / total_weight, 6)

    def _trajectory_index(self, results: list[EvalResult]) -> dict[str, object]:
        items: list[dict[str, object]] = []
        for result in results:
            metadata = result.metadata or {}
            trajectory = metadata.get("trajectory") or metadata.get("trace")
            item: dict[str, object] = {
                "task_id": result.task_id,
                "score": result.score,
                "passed": result.passed,
                "evidence_preview": _preview(result.evidence, 800),
                "failure_tags": list(metadata.get("failure_tags") or []),
                "trace_fingerprint_sha256": metadata.get("trace_fingerprint_sha256") or (_stable_json_sha(trajectory) if trajectory else None),
                "has_trajectory": bool(trajectory),
            }
            if trajectory:
                item["trajectory"] = trajectory
            items.append(item)
        return {"schema_version": TRACE_SCHEMA_VERSION, "items": items, "fingerprint_sha256": _stable_json_sha(items)}

    def evaluate(self, skill_text: str, tasks: list[EvalTask], label: str = "skill") -> dict[str, object]:
        assert self.runner is not None
        results = [self.runner.score(skill_text, task) for task in tasks]
        total_weight = sum(max(0.0, float(task.weight)) for task in tasks)
        weighted = sum(r.score * max(0.0, float(task.weight)) for r, task in zip(results, tasks))
        mean = round(weighted / total_weight, 3) if results and total_weight > 0 else 0.0
        hard_score = self._weighted_pass_rate(results, tasks)
        splits = sorted({task.split for task in tasks})
        eligibility = [production_eligibility_for_task(task).as_dict() for task in tasks]
        target_config = self.config.as_dict()
        contract_checks: list[dict[str, object]] = []
        for task, result in zip(tasks, results):
            contract_raw = (task.metadata or {}).get("eval_execution_contract")
            contract = contract_raw if isinstance(contract_raw, dict) else {}
            classification = str(contract.get("classification") or "")
            metadata = result.metadata or {}
            missing_runtime: list[str] = []
            if classification == "frozen_hermes_target_execution_v1":
                if not target_config.get("fingerprint_sha256"):
                    missing_runtime.append("target_config_fingerprint")
                if metadata.get("frozen_hermes_contract") != FROZEN_HERMES_CONTRACT:
                    missing_runtime.append("frozen_hermes_contract_marker")
                for field_name in ("model_fingerprint", "profile_fingerprint", "toolset_fingerprint", "session_fingerprint"):
                    value = metadata.get(field_name)
                    if not isinstance(value, dict) or not value.get("fingerprint_sha256"):
                        missing_runtime.append(field_name)
                if metadata.get("sandbox_isolated") is not True:
                    missing_runtime.append("isolated_runtime")
                if not (metadata.get("trajectory") or metadata.get("trace")):
                    missing_runtime.append("trajectory_or_transcript")
                if not metadata.get("trace_fingerprint_sha256"):
                    missing_runtime.append("trace_fingerprint_sha256")
                if not metadata.get("transcript_preview"):
                    missing_runtime.append("transcript_preview")
                if not metadata.get("execution_scoring"):
                    missing_runtime.append("execution_scoring")
                if metadata.get("live_profile_writes") is not False:
                    missing_runtime.append("live_profile_writes_false")
                if metadata.get("task_commands_executed") is not False:
                    missing_runtime.append("task_commands_executed_false")
            contract_checks.append({"task_id": task.id, "classification": classification or None, "runtime_evidence_complete": not missing_runtime, "missing_runtime_evidence": missing_runtime})
        production_gate_eligible = bool(results) and all(bool(row.get("eligible")) for row in eligibility) and all(bool(row.get("runtime_evidence_complete")) for row in contract_checks)
        regression_cases = [r.task_id for r in results if not r.passed]
        trajectory_index = self._trajectory_index(results)
        evaluation_scope = "production_curated_pack_eligible" if production_gate_eligible else "review_only_deterministic_fallback"
        return {
            "label": label,
            "executor": self.mode,
            "target_config_id": self.target_config_id,
            "target_backend_config": target_config,
            "target_fingerprint_sha256": target_config["fingerprint_sha256"],
            "trace_schema": TRACE_SCHEMA_VERSION,
            "score": mean,
            "soft_score": mean,
            "hard_score": hard_score,
            "hard_pass_rate": hard_score,
            "split_score": mean,
            "split": splits[0] if len(splits) == 1 else "mixed" if splits else None,
            "splits": splits,
            "num_tasks": len(results),
            "total_weight": round(total_weight, 3),
            "production_gate_eligible": production_gate_eligible,
            "evaluation_scope": evaluation_scope,
            "eval_execution_contract_checks": contract_checks,
            "adoption_policy": "production adoption allowed only when every task is explicit curated-pack production eligible and eval execution contract/runtime evidence gates pass; otherwise review-only evidence",
            "production_eligibility_reasons": eligibility,
            "regression_cases": regression_cases,
            "trajectory_index": trajectory_index,
            "trajectory_fingerprint_sha256": trajectory_index["fingerprint_sha256"],
            "production_score": mean if production_gate_eligible else None,
            "review_only_score": None if production_gate_eligible else mean,
            "score_ledger": {"production_curated_score": mean if production_gate_eligible else None, "review_only_score": None if production_gate_eligible else mean, "production_gate_eligible": production_gate_eligible, "evaluation_scope": evaluation_scope},
            "result_summary": [{"task_id": r.task_id, "score": r.score, "soft_score": r.score, "passed": r.passed, "hard_pass": r.passed, "failure_tags": list((r.metadata or {}).get("failure_tags") or []), "passed_checks": list((r.metadata or {}).get("passed_checks") or (((r.metadata or {}).get("trajectory") or {}).get("scores", {}) if isinstance((r.metadata or {}).get("trajectory"), dict) else {}).get("passed_checks") or []), "failed_checks": list((r.metadata or {}).get("failed_checks") or (((r.metadata or {}).get("trajectory") or {}).get("scores", {}) if isinstance((r.metadata or {}).get("trajectory"), dict) else {}).get("failed_checks") or []), "trace_fingerprint_sha256": (r.metadata or {}).get("trace_fingerprint_sha256")} for r in results],
            "results": [r.__dict__ for r in results],
        }


def _result_metadata(task: EvalTask) -> dict[str, object]:
    return {
        "source": task.source,
        "prompt": task.prompt,
        "split": task.split,
        "weight": task.weight,
        "success_criteria": task.success_criteria,
        "expected_behavior": task.expected_behavior,
        "allowed_tools": task.allowed_tools,
        "all_required_keywords": task.all_required_keywords,
        "required_markers": task.required_markers,
        "forbidden_markers": task.forbidden_markers,
        "timeout": task.timeout,
        "fixtures": task.fixtures,
        "scorecard_explicit": bool(task.metadata.get("scorecard_explicit")),
        "production_gate_eligible": bool(task.metadata.get("production_gate_eligible")),
        "task_origin": task.metadata.get("task_origin") or task.source,
        "production_eligibility": production_eligibility_for_task(task).as_dict(),
    }
