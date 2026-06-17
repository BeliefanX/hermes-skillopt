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
            "scoring_policy": "deterministic_keyword_scorecard_v1",
            "judge_can_accept": False,
            "executes_task_commands": False,
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
        score = round(max(0.0, min(1.0, score - 0.15 * len(penalties))), 3)
        return EvalResult(task.id, score, score >= 0.55, f"runner={self.mode}; matched={matched}; penalties={penalties}", _result_metadata(task))


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
            "scoring_policy": "deterministic_keywords_assertions_markers_v1",
            "judge_can_accept": False,
            "executes_task_commands": False,
            "task_command_policy": "record_and_block",
        }

    def score(self, skill_text: str, task: EvalTask) -> EvalResult:
        low = skill_text.lower()
        checks: list[tuple[str, bool, float]] = []
        for term in task.expected_terms:
            checks.append((f"expected_keyword:{term}", any(v in low for v in _variants(term)), 1.0))
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
        command_blocked = task.fixtures.get("command") is not None or task.metadata.get("command") is not None
        failure_tags = list(dict.fromkeys((["task_provided_command_blocked"] if command_blocked else []) + [f"penalty:{p}" for p in penalties]))
        score, passed_names, failed_names = _score_checks(checks, penalties)
        trajectory = self._trajectory(skill_text, task, checks, penalties, passed_names, failed_names, score, failure_tags)
        return EvalResult(
            task.id,
            score,
            score >= 0.55 and not penalties and not command_blocked,
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
                "sandbox_command_blocked": command_blocked,
                "task_commands_executed": False,
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
            "fixed_internal_runner": "python -m hermes_skillopt.sandbox_runner",
            "task_command_policy": "reject_before_subprocess",
            "isolated_home": True,
            "live_profile_writes": False,
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
                "production_gate_eligible": False,
                "trace_schema": TRACE_SCHEMA_VERSION,
                "failure_tags": ["task_provided_command_blocked"],
                "task_commands_executed": False,
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
        for marker in task.required_markers:
            checks.append((f"required_marker:{marker}", marker.lower() in low_transcript or marker.lower() in low_skill, 1.0))
        for assertion in task.assertions:
            name, ok = HermesRolloutRunner()._assertion_passed(low_skill + "\n" + low_transcript, assertion)
            checks.append((name, ok, float(assertion.get("weight", 1.0) or 1.0)))
        penalties = [term for term in task.failure_terms if term.lower() in low_skill or term.lower() in low_transcript]
        penalties.extend([m for m in task.forbidden_markers if m.lower() in low_transcript or m.lower() in low_skill])
        score, passed_names, failed_names = _score_checks(checks, penalties)
        metadata = {**_result_metadata(task), "exit_code": code, "transcript_preview": transcript[:4000], "sandbox_isolated": True, "sandbox_command_blocked": False, "live_profile_writes": False, "trace_schema": TRACE_SCHEMA_VERSION, "failure_tags": [f"penalty:{p}" for p in penalties], "task_commands_executed": False, "trajectory": {"schema_version": TRACE_SCHEMA_VERSION, "task_id": task.id, "messages": [{"role": "user", "content_preview": _preview(task.prompt, 800)}, {"role": "assistant", "skill_sha256": _stable_json_sha({"skill_text": skill_text}), "content_preview": _preview(skill_text, 800)}], "tool_calls": [{"id": "fixed-sandbox-runner", "name": "hermes_skillopt.sandbox_runner", "allowed": True, "executed": True, "blocked_reason": None}], "observations": [{"id": "sandbox-transcript", "content_preview": transcript[:4000]}], "scores": {"score": score, "passed_checks": passed_names, "failed_checks": failed_names, "penalties": penalties}, "failure_tags": [f"penalty:{p}" for p in penalties], "task_commands_executed": False}}
        metadata["trace_fingerprint_sha256"] = _stable_json_sha(metadata["trajectory"])
        return EvalResult(task.id, score, code == 0 and score >= 0.55 and not penalties, f"runner={self.mode}; exit={code}; passed={passed_names}; failed={failed_names}; penalties={penalties}", metadata)


def _score_checks(checks: list[tuple[str, bool, float]], penalties: list[str]) -> tuple[float, list[str], list[str]]:
    total = sum(max(0.0, weight) for _, _, weight in checks)
    passed_weight = sum(max(0.0, weight) for _, ok, weight in checks if ok)
    base = passed_weight / total if total > 0 else 0.0
    score = round(max(0.0, min(1.0, base - 0.20 * len(penalties))), 3)
    return score, [name for name, ok, _ in checks if ok], [name for name, ok, _ in checks if not ok]


@dataclass(frozen=True)
class TargetBackendConfig:
    """Explicit frozen target backend identity for provenance/artifacts."""

    executor: str
    target_config_id: str = "frozen-hermes-trace-replay-v2"
    requested_executor: str = "auto"
    role: str = "frozen_current_candidate_evaluator_no_editing"
    parameters: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": "skillopt-target-backend-config-v2",
            "executor": self.executor,
            "target_config_id": self.target_config_id,
            "requested_executor": self.requested_executor,
            "role": self.role,
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

    def evaluate(self, skill_text: str, tasks: list[EvalTask], label: str = "skill") -> dict[str, object]:
        assert self.runner is not None
        results = [self.runner.score(skill_text, task) for task in tasks]
        total_weight = sum(max(0.0, float(task.weight)) for task in tasks)
        weighted = sum(r.score * max(0.0, float(task.weight)) for r, task in zip(results, tasks))
        mean = round(weighted / total_weight, 3) if results and total_weight > 0 else 0.0
        production_gate_eligible = bool(results) and all(bool(r.metadata.get("production_gate_eligible")) for r in results)
        splits = sorted({task.split for task in tasks})
        eligibility = [production_eligibility_for_task(task).as_dict() for task in tasks]
        regression_cases = [r.task_id for r in results if not r.passed]
        target_config = self.config.as_dict()
        return {
            "label": label,
            "executor": self.mode,
            "target_config_id": self.target_config_id,
            "target_backend_config": target_config,
            "target_fingerprint_sha256": target_config["fingerprint_sha256"],
            "trace_schema": TRACE_SCHEMA_VERSION,
            "score": mean,
            "split_score": mean,
            "split": splits[0] if len(splits) == 1 else "mixed" if splits else None,
            "splits": splits,
            "num_tasks": len(results),
            "total_weight": round(total_weight, 3),
            "production_gate_eligible": production_gate_eligible,
            "production_eligibility_reasons": eligibility,
            "regression_cases": regression_cases,
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
        "required_markers": task.required_markers,
        "forbidden_markers": task.forbidden_markers,
        "timeout": task.timeout,
        "fixtures": task.fixtures,
        "scorecard_explicit": bool(task.metadata.get("scorecard_explicit")),
        "production_gate_eligible": bool(task.metadata.get("production_gate_eligible")),
        "task_origin": task.metadata.get("task_origin") or task.source,
        "production_eligibility": production_eligibility_for_task(task).as_dict(),
    }
