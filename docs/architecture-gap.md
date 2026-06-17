# Hermes SkillOpt architecture gap

This repository is a Hermes-safe adapter inspired by Microsoft SkillOpt, not a fork of upstream. Upstream remains pinned as metadata/clone under `$HERMES_HOME/skillopt/upstream/SkillOpt`; production code should not merge or shadow upstream wholesale.

## Current positioning

- Trainable state: one Hermes `SKILL.md` document.
- Frozen target: the same replay/scorecard task set and target config is used for current and candidate.
- Optimizer: reflection plus bounded edits; it never accepts its own edits.
- Gate: validation score must strictly improve; adopt/rollback are explicit and guarded.
- Shell: staged-only artifacts, path/sha guards, active-profile isolation, no auto-adopt.

## Upstream pin / no-fork strategy

- Keep upstream as canonical reference at `https://github.com/microsoft/SkillOpt.git`.
- Current lock pin: `skillopt_upstream.lock` = `86bad36ffe511b7022a6c735930056c14124b960`.
- Local upstream clone (when present): `/Users/fanxuxin/.hermes/skillopt/upstream/SkillOpt`.
- Record pin/clone status via existing upstream commands.
- Port only small, reviewed ideas that fit Hermes safety boundaries.
- Do not replace Hermes profile safety with upstream training code paths.

## Validation commands

- `python3 -m pytest -q`
- `python3 -m compileall hermes_skillopt __init__.py`
- Optional status check: `python3 -m hermes_skillopt.cli upstream-status`

## Gaps

P0:
- Real replay runner was missing; keyword scorecard alone could not exercise Hermes-like tasks.
- Eval schema was too narrow for replay prompts, expected behavior, assertions, fixtures, allowed tools, judges, timeouts, and success criteria.
- Rejected edits were written but not fed back into later optimizer context.
- The trainer loop was implicit instead of six explicit artifacted stages.

P1:
- Replay MVP is declarative and sandboxed; it is not yet a full Hermes gateway/session executor.
- Curated replay set is small; needs 10-30 stable tasks per important skill family.
- Semantic judging remains limited; production eligibility should require explicit scorecards or reviewed replay assertions.

P2:
- Richer failure clustering across runs.
- Better visualization of stage artifacts in WebUI.
- Optional upstream diff reports when new SkillOpt releases land.

## Roadmap

1. Keep staged-only write semantics and active-profile isolation unchanged.
2. Expand curated replay tasks in `$HERMES_HOME/skillopt/evals/*.jsonl` using the extended schema.
3. Replace the MVP replay assertion runner with a real Hermes sandbox executor only when it can prove no profile writeback.
4. Promote rejected edit history into first-class optimizer memory and WebUI review.
5. Grow six-stage artifacts into stable interfaces for diagnostics and future orchestration.
