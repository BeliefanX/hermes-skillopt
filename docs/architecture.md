# Hermes SkillOpt architecture

This document describes the current architecture on this branch: what is implemented, what is deliberately constrained for Hermes safety, and what remains limited.

## Boundaries

`hermes-skillopt` is a Hermes-safe adapter inspired by Microsoft SkillOpt. It does not modify Hermes core, does not vendor upstream SkillOpt code, does not auto-adopt generated skill changes, and does not replace the Hermes curator. Curator owns lifecycle/archive/consolidation and native skill ownership; SkillOpt owns staged eval evidence and adoption recommendations.

The only trainable object is a target `SKILL.md` under the active Hermes profile. All optimization output is staged under `$HERMES_HOME/skillopt/staging/<run-id>/` until an explicit guarded `adopt` call. Native Hermes sidecars such as `skills/.usage.json`, `.curator_state`, `.hub_manifest.json`, `.bundled_manifest.json`, and `.manifest.json` are read best-effort for advisory metadata and guards only; SkillOpt never writes them.

## Main modules

- `core.py`: orchestration, status/review/adopt/rollback, guided `scout`/`doctor`/`optimize` decision UX, high-value skill readiness queue, review/notification digests, eval-only/benchmark fixed-skill reports, artifact hygiene reports, artifact hashing, upstream status/update wrappers, profile/path guards, score ledgers, held-out sensitivity reporting, read-only native Hermes metadata snapshots, native adopt conflict guard, and evidence maturity ledger.
- `batch.py`: data-only batch preflight and staged-only batch runner with budget enforcement and forbidden writeback field rejection.
- `env.py`: eval-file resolution, curated/session/fallback task construction, production-gate eligibility checks.
- `eval_packs.py`: read-only eval-pack inventory/doctor/workflow summaries, plan-by-default autopilot with explicit review-draft writes, safe review-only scaffold/session/correction/context/negative-boundary seed generation, curated pack factory, and draft promotion. Draft/seed/autopilot/scaffold/workflow outputs are review-only; production promotion requires explicit policy plus execution contract and still never adopts.
- `skill_quality.py`: read-only `skill-quality`/`skill-lint` checks for skill structure and eval readiness; optional eval skeleton creation is explicit, guarded, review-only, and never edits live `SKILL.md`.
- `trainer.py`: six-stage rollout/reflect/aggregate/select/update/evaluate loop and final held-out test evaluation.
- `optimizer.py`: LLM/mock reflection and bounded edit proposal generation.
- `bounded_edit.py`: bounded `append`/`replace`/`delete`/`insert_after` edit validation and application, including protected heading/marker and allowed-region boundary checks on replacement/insert text.
- `target.py`: deterministic scorecard, replay runner, isolated sandbox fixed internal runner, sandbox-routed `frozen-hermes` / `frozen_hermes_target_execution_v1` review path, disabled/report-only `live-readonly` adapter interface, and frozen `TargetExecutor` wrapper. Production adoption requires real Hermes runtime invocation evidence; current sandbox/fixed runners cannot satisfy it.
- `gate.py`: deterministic validation gate policies (`soft|hard|mixed|strict`) with score improvement and per-task regression checks depending on mode.
- `webui.py` / `webui_server.py` / `webui_api.py`: optional React/FastAPI WebUI plus PWA assets for status/scout/full-run/review/fleet/adopt/rollback/upstream workflows; Python API keeps writeback confirmations, read-only scout/review API semantics, and staged-only constraints server-side.
- `multi_agent.py`: deterministic multi-agent handoff optimizer for `delegate_task` dispatcher→worker packages.
- `benchmark_bridge.py`: safe JSON-only upstream-style benchmark manifest importer into Hermes eval-pack format.
- `transfer.py`: read-only staged/proposed skill transfer evaluation across deterministic targets/profile homes.
- `conformance.py`: local compile/pytest conformance runner that returns machine-readable reports and writes a guarded `.json` file only when `--output`/`output_path` is explicitly supplied.
- `safety.py`: shared report/eval output path guard used by benchmark import, transfer eval, and conformance report writers; blocks live profile/runtime/plugin/source paths and symlink escapes.

## Full-run flow

`core.full_run()` coordinates the safety shell around `SixStageSkillOptTrainer`:

1. Resolve active `HERMES_HOME`, discover target skill, read original `SKILL.md`.
2. Build train/validation/test tasks from curated evals, session-mined evidence, or fallback tasks.
3. Select separate optimizer and target backends: `optimizer_backend=auto|hermes|mock`; `target_backend`/`target_executor=auto|replay|sandbox|scorecard|frozen-hermes|frozen_hermes_target_execution_v1|live-readonly`.
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
8. Mark the run adoptable only if production validation and held-out test gates are eligible and passing, complete real frozen-runtime evidence and reviewer gate are present, provenance/current-SHA hashes match, and the native Hermes conflict guard allows the skill.

Use current full-run/staged artifacts for review and explicit adopt; generated runs must pass the staged evidence and reviewer gates before adoption.

`eval_only()`/CLI `eval-only` is a separate fixed-skill scoring path. CLI `benchmark` is an alias for the same read-only report generator. These commands require an explicit eval file, write `evaluated_SKILL.md`, `eval_report.json`, `benchmark_report.json`, `report.md`, and `manifest.json` under a staging run with `status == "eval_only_complete"`, and are always `adoptable: false`. They have no optimizer/training/candidate-selection side effects and cannot production-adopt. `benchmark_report.json` uses `hermes-native-benchmark-report-v1` with skill/eval/target fingerprints, read-only safety flags, and split scorecard summary; it is not an upstream benchmark parity report.

`batch_preflight()` validates `hermes-skillopt-batch-plan-v1` without writes. It enforces budget caps (`max_jobs`, `max_total_iterations`, `max_total_candidates`), integer defaults, backend/target/gate enums, production-intent `skill`/`eval_file`, and forbidden writeback fields. `run_batch()` writes a batch parent staging directory only after preflight, then invokes child `full_run()` calls with `auto_adopt=False` and `force=False`; batch target enum validation currently accepts `auto|replay|sandbox|scorecard|live-readonly|frozen-hermes|frozen_hermes_target_execution_v1`, with frozen aliases limited to sandbox/fixed-runner review evidence until real Hermes runtime proof exists.

Fleet functions (`fleet_report`, `fleet_resume_plan`, `fleet_rollback_plan`) inspect recent run dirs, batch parents/children, checkpoints, and backup state. Fleet report rows include readiness, advisory skill type, and evidence-contract summaries, and the report groups by skill/type/readiness/adoptability/rollbackability. Rollback planning includes safely readable backup/current-SHA status and a per-run command template. They are read-only reporting/planning surfaces: no full-run invocation, no partial resume execution, no deletion/cleanup, no bulk rollback, and no skill writes.

Guided UX functions are also safety-scoped. `scout()` is a notification-ready read-only summary for eval-pack inventory/workflow, high-value readiness queue, package metadata, recent runs, artifact hygiene, safe next commands, and scout-only/no-auto-adopt cron guidance; it never runs optimize/full-run, fetches, adopts, rolls back, or writes live profile state, and CLI scout writes no report unless `--output` is explicit. `doctor()` is read-only readiness reporting. Only scout, doctor, eval-pack inventory, and eval-pack doctor are cron-safe defaults; `review_digest()` is digest-only/manual for an already-staged run, not a scheduled default. Eval-pack inventory, doctor, workflow summaries, and the skill readiness queue are read-only diagnostics; their digest modes are notification text/JSON only. Default autopilot is read-only planning; only explicit draft/seed/promotion/skeleton commands write guarded review-pack files. `skill_quality_report()` is read-only unless explicitly asked to create a guarded review-only eval skeleton. `guided_optimize()` powers CLI/plugin/WebUI intent presets and always disables auto-adopt. `review_decision_summary()` is the decision-first read surface for CLI/plugin/WebUI and exposes validation, production-best, and held-out test gate separation through `hermes-skillopt-readiness-adoptability-v1`. `review_digest()` wraps that summary as `hermes-skillopt-review-digest-v1` for manual Telegram/notification use with score provenance and report/diff path/hash refs only. `artifact_hygiene_report()` classifies stale/tampered/incomplete/orphaned staging artifacts for reviewer cleanup decisions but never deletes or resumes anything. Scheduled automation is limited to read-only notification/diagnostic digests (`scout --digest`, `doctor --digest`, `eval-pack-inventory --digest`, and `eval-pack-doctor --digest`); status/review/optimize/full-run/adopt/rollback/upstream-update, cleanup, default autopilot, skill-quality skeletons, and eval-pack draft/promotion writes are not cron-safe.

## Artifact model

Run directories contain the current/proposed skill copies, eval task JSONL files, validation/test results, reflections, candidate edits, candidate rank/select summary, rejected edits, `slow_meta.json`, `target_binding.json`, `provenance_binding.json`, `history.json`, gate results, report, diff, manifest, `checkpoint.json`, and per-stage JSON under `stages/`. Stage records use `skillopt-stage-v1` and include deterministic batch metadata (`skillopt-deterministic-batch-v1`, stable `batch_id`, seed `0`, stable input ordering/ranking note, input/output fingerprints).

The manifest stores SHA-256 hashes for staged files plus native Hermes metadata fingerprints when available. `review`, `adopt`, and `rollback` verify these hashes before trusting artifacts. `best_skill.md` exists only when a candidate beats validation and is staged as best. `report.md` and `review` include baseline/current/candidate/best/test scores, production-curated vs review-only score ledgers, per-task deltas including expected-term/assertion changes, held-out test sensitivity warnings, not-adoptable reasons/checklist, and `skillopt-provenance-v2` over eval/task SHA, plugin repo, pinned upstream lock, optimizer_backend config, target_backend config, gate policy, profile/skill fingerprints, and production eval policy. Slim notification/status surfaces include `score_provenance` with target executor/backend, optimizer backend, eval pack id/version/path/fingerprint, score source, split labels, score fields, and warnings; a held-out test score without `heldout_test_sensitivity` stays caveated. `history.json` records candidate lineage, selected/accepted/rejected status, gate summaries, and rejection reasons for audit/reflection; it is not a live-write source.

Resume is deliberately conservative: `checkpoint.json` stores a `skillopt-checkpoint-v1` input/config fingerprint. `resume_run_id` can reuse a completed run only after artifact verification and exact fingerprint match; incomplete checkpoints are refused rather than partially replayed. `status`, `resume-inspect`, and hygiene rows expose stale/incomplete checkpoint rows, tracked artifact path/hash state, lineage summaries, `next_safe_action`, and `partial_continuation_available: false`; no code auto-deletes or resumes partial stage output.

## Native Hermes boundary

SkillOpt treats native Hermes metadata as read-only guard input. `native_skill_metadata_snapshot()` reads `.usage.json`, curator state, hub/bundled manifests, and manifest sidecars best-effort, records path/readability/SHA diagnostics, and computes a fingerprint over native signals and records. It does not write or repair these sidecars. Hub-installed, bundled, pinned, archived, and curator-managed skills are blocked/diagnostic-only by default for SkillOpt adoption; `force` does not override this native conflict guard. If native metadata changes between staging and adopt, reviewers must rerun or restage after reviewing the native sidecars.

## Eval and adoption gates

Task schema supports `prompt`, split, expected/forbidden keywords, assertions, markers, success criteria, expected behavior, allowed tools, fixtures, timeout, judge, weight, executor metadata, and an explicit `production_gate_eligible`/`production_gate` opt-out flag. `expected_keywords`/`expected_terms` are weighted soft checks; `all_required_keywords`/`required_keywords`/`must_include_keywords`, `required_markers`, and `forbidden_markers` are critical hard constraints that set `passed: false` when violated. Production schema policy is recorded as `production-eval-schema-v1` in manifests. Versioned `hermes-curated-eval-pack-v1` packs require train/validation/test splits, reject split leakage, and validate declared fingerprints when present.

Production adoption is intentionally stricter than generic validation:

- The production validation gate can only use explicit curated eval-file validation tasks.
- Static/keyword scorecard packs, sample packs, fallback/synthetic/session-mined tasks, legacy JSON/JSONL, and deterministic replay/report-only contracts are review-only even when stale flags claim production eligibility.
- Eligible tasks must carry explicit scoring/assertion signal such as keywords, markers, assertions, expected behavior, failure terms, or ground truth metadata.
- The final candidate must strictly improve production validation score over current.
- Any hard-failed production-eligible validation row blocks production gate acceptance/adoptability, even when soft weighted score improves and regardless of gate mode.
- Held-out curated test tasks must pass threshold.
- Fallback, synthetic, session-mined, and other review-only evidence cannot make a run production-adoptable.
- LLM judge text is explanatory evidence only and cannot bypass gates.
- `evidence_ledger` (`hermes-skillopt-evidence-ledger-v1`) exposes `eval_level`, `evidence_maturity`, `production_runtime_ready`, complete frozen evidence, real-runtime invocation/evidence flags, task-command status, internal-runner status, reviewer-gate status, and blockers. Static/replay/sandbox/live-disabled evidence stays review-only unless complete real Hermes runtime evidence and reviewer approval are present.

Eval execution contract classifications are part of this policy: `static_keyword_scorecard`/`static_review_only` and `deterministic_replay_report_only` are non-adoption evidence; `deterministic_replay_contract_compliant` can be adoption-eligible only inside an explicit curated v1 pack with passing provenance/runtime checks; `frozen_hermes_target_execution_v1` requires frozen target config, provider/model/toolset/session/runtime fingerprints, isolated runtime proof, declared permissions with task commands disabled, transcript/trajectory evidence, execution-based scoring, and explicit real Hermes runtime invocation proof. Current sandbox/replay/scorecard/fixed internal runners fail that real-runtime requirement and remain review-only. This is not upstream benchmark execution/parity or arbitrary live agent command execution. The current `live-readonly` surface is an interface/disabled report path, not a true live Hermes runner.

Gate modes are deterministic metric policies: `strict` is the default for adoption-capable full runs and requires a non-no-op soft improvement, hard weighted pass-rate non-regression, and no previously passing task failures unless `hard_regression_allowed` is explicitly set. `soft`, `hard`, and `mixed` remain explicit review/non-production policy choices (`soft` requires weighted-score improvement, `hard` requires pass-rate improvement, `mixed` requires soft improvement with hard non-regression). All modes keep LLM/judge output explanation-only and none can override production hard-fail/test/evidence gates.

## EnvAdapter and evidence foundation

`EnvAdapter` is the contract between Hermes evidence and the trainer. It exposes task splits, rollout metadata, scorer metadata, and production eligibility decisions. Built-in benchmarks, bundled static review packs, and session-mined/synthetic tasks provide train/validation/test scaffolding and slow/sleep-style evidence, but they are non-production unless replaced by explicit curated eval-file scorecards with an adoption-eligible execution contract. The static seed packs in `examples/evals/hermes_tool_use_static_review_v1.json` and `examples/evals/hermes_skill_safety_static_review_v1.json` are review-only; they use `static-review-eval-pack`/`sample_pack` policy and cannot production-adopt.

`eval-pack-inventory` makes that coverage reality explicit by listing candidate eval paths, existing pack validity, versioned pack id/version/fingerprint, split counts, production-eligible task counts, advisory skill type, unified readiness/adoptability schema, readiness matrix, and missing reasons per skill. It discovers exact/default packs and conservative name-derived versioned packs such as `<skill>-thermal-v4.json`. `eval-pack-doctor` is read-only diagnostics over that inventory and emits safe human next commands. `eval-pack-workflow` adds a read-only authoring summary and promotion checklist without writing packs. `skill-readiness-queue` ranks high-value candidates without running optimize/adopt/rollback. `skill-quality` is read-only by default; explicit eval skeleton creation is guarded review-only. `eval-pack-autopilot` is plan/read-only by default for interactive authoring, but the scheduled notification set should use digest surfaces instead; the explicit `--write-draft`/`write_draft=true` path writes only a generated review-only draft. `eval-pack-scaffold`, correction/context ingestion, negative/boundary generation, skeleton creation, and session mining fill only authoring gaps with review-only packs, not production evidence. `eval-pack-curate` is the curated factory for local task JSON and is review-only unless explicit production policy plus an adoption-eligible execution contract are supplied. `eval-pack-promote` promotes drafts to curated review packs by default; production promotion requires explicit policy/contract, and WebUI promotion exposes review-only one-click promotion. Skill package support (`references/`, `templates/`, `scripts/`, `assets/`) is summarized only as advisory path/hash/count metadata after profile-boundary checks and does not change adoption authority.

## Sandbox executor safety

`HermesSandboxRunner` is an isolated review executor, not a production adoption executor and not an arbitrary command executor. It also backs the current `frozen-hermes` / `frozen_hermes_target_execution_v1` target alias, but because it invokes a fixed internal runner rather than the real Hermes runtime, its evidence is non-production. It creates a temporary isolated HOME/HERMES_HOME/workspace, writes the candidate `SKILL.md` only into that sandbox, invokes a fixed internal runner, and records transcript/exit/timeout metadata plus provider/model/toolset/session fingerprints and trajectory/scoring evidence.

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
- complete frozen target runtime evidence with real Hermes runtime invocation/evidence, no task command execution, and a passing/adoptable reviewer gate
- native Hermes adopt guard allowing the target skill and unchanged native metadata fingerprint; force does not bypass this guard

Rollback restores only from the verified backup directory created by adopt. It validates backup path containment, backup manifest, run id, target path, skill relpath, original/proposed/adopted SHA, and current live SHA unless forced.

## Current implementation map

Current code implements these bounded capabilities:

- P0/P1 core abstraction: `SKILL.md` is explicit trainable state; optimizer and target executor are separate; candidate edits are bounded and staged; curated production eval packs and validation/test gates drive adoption status.
- P1/P2 observability: full runs produce per-stage artifacts, report/diff, candidate summaries, rejected buffers, provenance v2, target/provenance bindings, history/lineage, slim review artifact refs, status lineage summaries, and conservative completed-run resume inspection with stale/incomplete checkpoint reporting.
- P2 safety gates: adoption re-checks artifact hashes and independently re-derives production/test eligibility from hashed artifacts; mock/fallback/session/synthetic/legacy evidence remains review-only; report/eval writers use shared safe output path guards.
- P0/P1 reporting and P3 integration utilities: eval-only/benchmark writes reproducible Hermes-native benchmark reports; benchmark bridge imports safe JSON manifests into eval packs; transfer eval is read-only across deterministic targets/profile homes; and conformance returns local compile/pytest reports with no default file write.
- Phase2/Phase3 orchestration/UX utilities: scout adds read-only notification/cron-safe summaries; batch preflight/run adds staged-only multi-job execution with budgets and policy profiles; fleet report/resume/rollback-plan adds read-only operations dashboards with readiness/type/evidence-contract and rollback guard status; eval-pack inventory/scaffold/curate/session-mining exposes curated-pack coverage status; React/FastAPI WebUI surfaces scout/guided wizard/review console/fleet/upstream status and parity labels while keeping `auto_adopt=false`.
- Phase4/Phase5 guided/runtime-evidence/native-boundary hardening: `doctor`, `optimize --intent`, `review --summary`, CLI/WebUI typed adopt confirmation, artifact hygiene reporting, runtime-evidence contract checks, scorecard-vs-frozen-evidence separation, and production hard-fail overrides are encoded in core surfaces and tests.

Implemented does not mean externally benchmarked. This repository currently provides local deterministic contracts and fixtures, not verified Microsoft SkillOpt parity, external benchmark scores, or real cross-model transfer results.

## Upstream strategy

Microsoft SkillOpt is tracked through `skillopt_upstream.lock` and the canonical clone under `$HERMES_HOME/skillopt/upstream/SkillOpt`. Upstream status/update commands refresh metadata and pinning only; they do not merge code or alter live skills.

## Current limitations

- Replay/sandbox/scorecard scoring is deterministic and assertion-oriented; it is not a full Hermes gateway/session simulator.
- `benchmark`/`eval-only` reports are local fixed-skill reports only; benchmark bridge imports embedded JSON manifests only. Safe `json_import_only` and canonical-clone `pinned_manifest_replay` conversion are supported, but `pinned_upstream_execution` and `parity_evidence_complete` remain unsupported. The sandbox-routed `frozen_hermes_target_execution_v1` path supplies review-only local evidence, not Microsoft benchmark parity or production adoption proof. It does not execute upstream benchmark loaders, follow file references, clone repositories, or validate parity with Microsoft benchmark suites.
- Transfer evaluation uses existing deterministic target executors; it does not provision live external model/backend services or establish real cross-model performance.
- Production-quality adoption depends on maintaining explicit curated validation and test evals for each important skill.
- Semantic LLM judging is not an acceptance authority.
- WebUI is optional and intentionally constrained to fixed Hermes workflow artifacts. Its run API is staged-only (`auto_adopt=false`, `force=false`) and defaults to review-oriented soft gating; production adoption proof still requires strict/non-mock/curated val-test evidence and explicit guarded adopt.


## Current no-parity guardrails

Upstream coverage is explicitly surfaced instead of overclaimed: Hermes reports pinned upstream status and Hermes-native benchmark status separately. Live Hermes target execution is represented by a safe disabled-by-default read-only adapter interface; the implemented frozen-Hermes path is sandbox/fixed-runner review evidence only and lacks real Hermes runtime invocation proof. Benchmark execution is factored behind a JSON eval-pack adapter for report-only/staged use; upstream import is supported only as safe manifest conversion, not true upstream benchmark execution. Optimizer depth is bounded by validation gating, edit budgets, mini-batch candidate accumulation, and rejected-edit memory continuity.
