# Hermes SkillOpt architecture

This document describes the current main-branch architecture. It intentionally omits obsolete historical gap lists.

## Boundaries

`hermes-skillopt` is a Hermes-safe adapter inspired by Microsoft SkillOpt. It does not modify Hermes core, does not vendor upstream SkillOpt code, and does not auto-adopt generated skill changes.

The only trainable object is a target `SKILL.md` under the active Hermes profile. All optimization output is staged under `$HERMES_HOME/skillopt/staging/<run-id>/` until an explicit guarded `adopt` call.

## Main modules

- `core.py`: orchestration, status/review/adopt/rollback, artifact hashing, upstream status/update wrappers, profile/path guards.
- `env.py`: eval-file resolution, curated/session/fallback task construction, production-gate eligibility checks.
- `trainer.py`: six-stage rollout/reflect/aggregate/select/update/evaluate loop and final held-out test evaluation.
- `optimizer.py`: LLM/mock reflection and bounded edit proposal generation.
- `bounded_edit.py`: bounded `append`/`replace`/`delete`/`insert_after` edit validation and application.
- `target.py`: deterministic scorecard, replay runner, production-safe sandbox executor, and frozen `TargetExecutor` wrapper.
- `gate.py`: strict validation gate (`candidate_score > current_score`).
- `webui.py`: optional Gradio UI for Hermes-specific status/full-run/review/adopt/rollback/upstream workflows.
- `multi_agent.py`: deterministic multi-agent handoff optimizer for `delegate_task` dispatcher→worker packages.

## Full-run flow

`core.full_run()` coordinates the safety shell around `SixStageSkillOptTrainer`:

1. Resolve active `HERMES_HOME`, discover target skill, read original `SKILL.md`.
2. Build train/validation/test tasks from curated evals, session-mined evidence, or fallback tasks.
3. Select target executor: `auto|replay|sandbox|scorecard`.
4. Write initial staged artifacts.
5. Run the six trainer stages:
   - rollout current skill
   - reflect on evidence and rejected edit history
   - aggregate bounded edit proposals
   - select valid edits / record rejections
   - update a candidate copy
   - evaluate and gate on held-out validation
6. Evaluate the final best candidate on held-out test.
7. Write report, diff, stage JSON artifacts, rejected edits, gate results, manifest, and artifact SHA-256 map.
8. Mark the run adoptable only if production validation and held-out test gates are eligible and passing.

`full_run(dry_run=True)` is rejected by code; CLI has no `full-run --dry-run` option. Legacy `dry-run`/`run --mode legacy` remains review-only.

## Artifact model

Run directories contain the current/proposed skill copies, eval task JSONL files, validation/test results, reflections, candidate edits, rejected edits, gate results, report, diff, manifest, and per-stage JSON under `stages/`.

The manifest stores SHA-256 hashes for staged files. `review`, `adopt`, and `rollback` verify these hashes before trusting artifacts. `best_skill.md` exists only when a candidate beats validation and is staged as best.

## Eval and adoption gates

Task schema supports `prompt`, split, expected/forbidden keywords, assertions, markers, success criteria, expected behavior, allowed tools, fixtures, timeout, judge, weight, and metadata.

Production adoption is intentionally stricter than generic validation:

- The production validation gate can only use explicit curated eval-file validation tasks.
- Eligible tasks must carry explicit scoring/assertion signal such as keywords, markers, assertions, expected behavior, failure terms, or ground truth metadata.
- The final candidate must strictly improve production validation score over current.
- Held-out curated test tasks must pass threshold.
- Fallback, synthetic, session-mined, and legacy dry-run evidence cannot make a run production-adoptable.
- LLM judge text is explanatory evidence only and cannot bypass gates.

## Sandbox executor safety

`HermesSandboxRunner` is a production-safe MVP executor, not an arbitrary command executor. It creates a temporary isolated HOME/HERMES_HOME/workspace, writes the candidate `SKILL.md` only into that sandbox, invokes a fixed internal runner, and records transcript/exit/timeout metadata.

Task-provided commands in `fixtures.command` or `metadata.command` are deliberately blocked with exit code 126 and `SANDBOX_COMMAND_BLOCKED`. Blocked command tasks are not production-gate eligible. The sandbox never writes the live profile.

## Adopt and rollback guards

Adopt requires:

- `status == "staged_best"`
- `adoptable == true`
- accepted validation gate
- `production_gate_eligible == true`
- `test_gate_eligible == true`
- verified artifact hashes
- target path under active profile `skills/`
- current live skill SHA matching staged original unless forced
- proposed skill SHA matching manifest

Rollback restores only from the verified backup directory created by adopt. It validates backup path containment, backup manifest, run id, target path, skill relpath, original/proposed/adopted SHA, and current live SHA unless forced.

## Upstream strategy

Microsoft SkillOpt is tracked through `skillopt_upstream.lock` and the canonical clone under `$HERMES_HOME/skillopt/upstream/SkillOpt`. Upstream status/update commands refresh metadata and pinning only; they do not merge code or alter live skills.

## Current limitations

- Replay/sandbox scoring is deterministic and assertion-oriented; it is not a full Hermes gateway/session simulator.
- Production-quality adoption depends on maintaining explicit curated validation and test evals for each important skill.
- Semantic LLM judging is not an acceptance authority.
- WebUI is optional and intentionally constrained to fixed Hermes workflow artifacts.
