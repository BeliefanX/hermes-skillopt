# Hermes SkillOpt architecture

This document describes the current P0-P3 architecture on this branch. It intentionally replaces obsolete historical gap lists with a current-state map: what is implemented, what is deliberately constrained for Hermes safety, and what remains limited.

## Boundaries

`hermes-skillopt` is a Hermes-safe adapter inspired by Microsoft SkillOpt. It does not modify Hermes core, does not vendor upstream SkillOpt code, and does not auto-adopt generated skill changes.

The only trainable object is a target `SKILL.md` under the active Hermes profile. All optimization output is staged under `$HERMES_HOME/skillopt/staging/<run-id>/` until an explicit guarded `adopt` call.

## Main modules

- `core.py`: orchestration, status/review/adopt/rollback, eval-only/benchmark fixed-skill reports, artifact hashing, upstream status/update wrappers, profile/path guards.
- `env.py`: eval-file resolution, curated/session/fallback task construction, production-gate eligibility checks.
- `trainer.py`: six-stage rollout/reflect/aggregate/select/update/evaluate loop and final held-out test evaluation.
- `optimizer.py`: LLM/mock reflection and bounded edit proposal generation.
- `bounded_edit.py`: bounded `append`/`replace`/`delete`/`insert_after` edit validation and application.
- `target.py`: deterministic scorecard, replay runner, production-safe sandbox executor, and frozen `TargetExecutor` wrapper.
- `gate.py`: deterministic validation gate policies (`soft|hard|mixed|strict`) with score improvement and per-task regression checks depending on mode.
- `webui.py`: optional Gradio UI for Hermes-specific status/full-run/review/adopt/rollback/upstream workflows.
- `multi_agent.py`: deterministic multi-agent handoff optimizer for `delegate_task` dispatcher→worker packages.
- `benchmark_bridge.py`: safe JSON-only upstream-style benchmark manifest importer into Hermes eval-pack format.
- `transfer.py`: read-only staged/proposed skill transfer evaluation across deterministic targets/profile homes.
- `conformance.py`: local compile/pytest conformance runner that writes machine-readable reports.

## Full-run flow

`core.full_run()` coordinates the safety shell around `SixStageSkillOptTrainer`:

1. Resolve active `HERMES_HOME`, discover target skill, read original `SKILL.md`.
2. Build train/validation/test tasks from curated evals, session-mined evidence, or fallback tasks.
3. Select separate optimizer and target backends: `optimizer_backend=auto|hermes|mock`; `target_backend`/`target_executor=auto|replay|sandbox|scorecard`.
4. Write initial staged artifacts.
5. Run the six trainer stages:
   - rollout current skill
   - reflect on evidence and rejected edit history
   - aggregate bounded edit proposals (`candidate_count` conservative multi-candidate support)
   - select/rank strict-improvement candidates on the same validation set and record rejected/non-selected candidates
   - update a candidate copy
   - evaluate and gate on held-out validation
6. Evaluate the final best candidate on held-out test.
7. Write report, diff, stage JSON artifacts, rejected edits, `slow_meta.json` evidence, gate results, manifest, checkpoint, and artifact SHA-256 map.
8. Mark the run adoptable only if production validation and held-out test gates are eligible and passing.

`full_run(dry_run=True)` is rejected by code; CLI has no `full-run --dry-run` option. Legacy `dry-run`/`run --mode legacy` remains review-only.

`eval_only()`/CLI `eval-only` is a separate fixed-skill scoring path. CLI `benchmark` is an alias for the same read-only report generator. These commands require an explicit eval file, write `evaluated_SKILL.md`, `eval_report.json`, `benchmark_report.json`, `report.md`, and `manifest.json` under a staging run with `status == "eval_only_complete"`, and are always `adoptable: false`. They have no optimizer/training/candidate-selection side effects and cannot production-adopt. `benchmark_report.json` uses `hermes-native-benchmark-report-v1` with skill/eval/target fingerprints, read-only safety flags, and split scorecard summary; it is not an upstream benchmark parity report.

## Artifact model

Run directories contain the current/proposed skill copies, eval task JSONL files, validation/test results, reflections, candidate edits, candidate rank/select summary, rejected edits, `slow_meta.json`, `target_binding.json`, `provenance_binding.json`, `history.json`, gate results, report, diff, manifest, `checkpoint.json`, and per-stage JSON under `stages/`. Stage records use `skillopt-stage-v1` and include deterministic batch metadata (`skillopt-deterministic-batch-v1`, stable `batch_id`, seed `0`, stable input ordering/ranking note, input/output fingerprints).

The manifest stores SHA-256 hashes for staged files. `review`, `adopt`, and `rollback` verify these hashes before trusting artifacts. `best_skill.md` exists only when a candidate beats validation and is staged as best. `report.md` and `review` include baseline/current/candidate/best/test scores, per-task deltas, not-adoptable reasons/checklist, and `skillopt-provenance-v2` over eval/task SHA, plugin repo, pinned upstream lock, optimizer_backend config, target_backend config, gate policy, profile/skill fingerprints, and production eval policy. `history.json` records candidate lineage, selected/accepted/rejected status, gate summaries, and rejection reasons for audit/reflection; it is not a live-write source.

Resume is deliberately conservative: `checkpoint.json` stores a `skillopt-checkpoint-v1` input/config fingerprint. `resume_run_id` can reuse a completed run only after artifact verification and exact fingerprint match; incomplete checkpoints are refused rather than partially replayed.

## Eval and adoption gates

Task schema supports `prompt`, split, expected/forbidden keywords, assertions, markers, success criteria, expected behavior, allowed tools, fixtures, timeout, judge, weight, executor metadata, and an explicit `production_gate_eligible`/`production_gate` opt-out flag. Production schema policy is recorded as `production-eval-schema-v1` in manifests. Versioned `hermes-curated-eval-pack-v1` packs require train/validation/test splits, reject split leakage, and validate declared fingerprints when present.

Production adoption is intentionally stricter than generic validation:

- The production validation gate can only use explicit curated eval-file validation tasks.
- Eligible tasks must carry explicit scoring/assertion signal such as keywords, markers, assertions, expected behavior, failure terms, or ground truth metadata.
- The final candidate must strictly improve production validation score over current.
- Any hard-failed production-eligible validation row blocks production gate acceptance/adoptability, even when soft weighted score improves and regardless of gate mode.
- Held-out curated test tasks must pass threshold.
- Fallback, synthetic, session-mined, and legacy dry-run evidence cannot make a run production-adoptable.
- LLM judge text is explanatory evidence only and cannot bypass gates.

Gate modes are deterministic metric policies: `soft` requires weighted-score improvement, `hard` requires stricter pass-rate improvement, `mixed` requires soft improvement with hard non-regression, and `strict` requires a non-no-op soft improvement, hard weighted pass-rate non-regression, and no previously passing task failures unless `hard_regression_allowed` is explicitly set. All modes keep LLM/judge output explanation-only.

## EnvAdapter and evidence foundation

`EnvAdapter` is the contract between Hermes evidence and the trainer. It exposes task splits, rollout metadata, scorer metadata, and production eligibility decisions. Built-in benchmarks and session-mined/synthetic tasks provide train/validation/test scaffolding and slow/sleep-style evidence, but they are non-production unless represented by explicit curated eval-file scorecards. Reviewed seed packs now live in `examples/evals/hermes_tool_use_production_v1.json` and `examples/evals/hermes_skill_safety_production_v1.json`; they are production-eligible curated packs for Hermes safety/tool-use domains, not universal certification suites.

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

## P0-P3 closure map

Current code closes the earlier architecture gaps in these bounded ways:

- P0/P1 core abstraction: `SKILL.md` is explicit trainable state; optimizer and target executor are separate; candidate edits are bounded and staged; curated production eval packs and validation/test gates drive adoption status.
- P1/P2 observability: full runs produce per-stage artifacts, report/diff, candidate summaries, rejected buffers, provenance v2, target/provenance bindings, history/lineage, and conservative completed-run resume inspection.
- P2 safety gates: adoption re-checks artifact hashes and independently re-derives production/test eligibility from hashed artifacts; mock/fallback/session/synthetic/legacy evidence remains review-only.
- P0/P1 reporting and P3 integration utilities: eval-only/benchmark writes reproducible Hermes-native benchmark reports; benchmark bridge imports safe JSON manifests into eval packs; transfer eval is read-only across deterministic targets/profile homes; and conformance writes local compile/pytest reports.

Closed does not mean externally benchmarked. This repository currently provides local deterministic contracts and fixtures, not verified Microsoft SkillOpt parity, external benchmark scores, or real cross-model transfer results.

## Upstream strategy

Microsoft SkillOpt is tracked through `skillopt_upstream.lock` and the canonical clone under `$HERMES_HOME/skillopt/upstream/SkillOpt`. Upstream status/update commands refresh metadata and pinning only; they do not merge code or alter live skills.

## Current limitations

- Replay/sandbox/scorecard scoring is deterministic and assertion-oriented; it is not a full Hermes gateway/session simulator.
- `benchmark`/`eval-only` reports are local fixed-skill reports only; benchmark bridge imports JSON manifests only and does not execute upstream benchmark loaders, follow file references, clone repositories, or validate parity with Microsoft benchmark suites.
- Transfer evaluation uses existing deterministic target executors; it does not provision live external model/backend services or establish real cross-model performance.
- Production-quality adoption depends on maintaining explicit curated validation and test evals for each important skill.
- Semantic LLM judging is not an acceptance authority.
- WebUI is optional and intentionally constrained to fixed Hermes workflow artifacts.
