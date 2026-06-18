# Hermes SkillOpt P3 Conformance Spec

This document is the current P0/P1/P2/P3 conformance contract for the standalone Hermes SkillOpt plugin. It records where the adapter intentionally aligns with Microsoft SkillOpt concepts, where Hermes diverges for safety, and what remains out of scope.

## Scope

P3 adds deterministic, local, no-credential tooling for:

- Reviewed Hermes static review seed eval packs plus safe upstream-style benchmark manifest import into Hermes curated eval pack format.
- Read-only staged transfer evaluation across target/profile/backend configs.
- Continuous local conformance/regression execution and machine-readable reports.

No command in this adapter vendors Microsoft SkillOpt, imports upstream Python modules, executes benchmark code, or requires external services.

## Microsoft SkillOpt alignment

Hermes maps the core SkillOpt abstraction as follows:

- Trainable state: a Hermes `SKILL.md` document.
- Environment/benchmark: Hermes eval packs with explicit train/val/test split governance; bundled examples under `examples/evals/` are static review fixtures and cannot production-adopt.
- Rollout/target model: frozen target executor (`scorecard`, `replay`, `sandbox`, or `frozen-hermes`/`frozen_hermes_target_execution_v1`) with backend config fingerprints. The frozen-Hermes path is currently a sandbox-backed MVP with isolated runtime/provider/model/toolset/session evidence, transcript/trajectory evidence, task commands disabled, and no live profile writes.
- Reflection/update: optimizer proposes bounded edits; candidate text is staged.
- Evaluation gate: validation/test scorecards decide whether a candidate is review-worthy or adoptable; any hard-failed production-eligible validation row blocks production gate acceptance/adoptability regardless of soft weighted-score improvement or gate mode. Critical `all_required_keywords`/`required_markers` and `forbidden_markers` are hard pass/fail constraints, not soft weights.
- Artifacts/checkpoints: every staged run records manifest, task/eval, target, optimizer, profile, and provenance fingerprints; stage artifacts include deterministic batch metadata.

## Intentional Hermes divergences

Hermes preserves the outer safety shell even when adapting upstream benchmark concepts:

- Upstream benchmark bridge is JSON-data-only. It rejects executable or remote fields such as `code`, `script`, `command`, `module`, `entrypoint`, `url`, and container/image fields.
- Imported sample packs are review-only by default. Production gate eligibility is disabled unless a caller explicitly imports as curated, the pack is not static/keyword/sample/report-only, an adoption-eligible eval execution contract is declared, and tasks also satisfy Hermes production eval policy.
- Static keyword/text scorecard packs (`static_keyword_scorecard`, `static_review_only`, `static-review-eval-pack`, `keyword-scorecard`) are always review-only/non-adoption evidence, including the bundled example packs whose filenames still contain `production_v1`.
- Eval pack validation rejects invalid splits, missing required splits for versioned curated packs, declared fingerprint tampering, and leakage by duplicate ids/prompts across train/val/test splits.
- Transfer evaluation is report-only/read-only: it evaluates staged/proposed skill text or an explicit file and never writes live skills.
- Cross-profile evaluation records profile fingerprints but does not mutate target profiles.
- Conformance tooling runs local deterministic `compileall` plus pytest commands and does not depend on network, credentials, upstream checkout, or live Hermes services.
- Import, transfer, and conformance report writers share `guard_safe_output_path`: `.json` outputs cannot target live skills/plugins/config/memory/cron/runtime paths, plugin/repo source paths, non-regular files, wrong suffixes, or symlink escapes.
- Bounded edit validation checks replacement/inserted text for protected headings/markers and allowed-region marker boundary mutations, not just the matched old text.
- Score artifacts distinguish production-curated evidence from review-only evidence through `production_curated_score`, `review_only_score`, per-task delta rows, expected-term/assertion change details, and held-out test sensitivity warnings.
- Resume tooling is inspection-first: incomplete/stale checkpoints are reported with stage/artifact fingerprints and cleanup guidance, but partial-stage continuation is unavailable because replaying from the middle could skip gates or adoptability checks.

## P0/P1/P2/P3 commands/modules

- `python3 -m hermes_skillopt.cli batch-preflight PLAN.json`
  - Read-only validation of `hermes-skillopt-batch-plan-v1` data.
  - Enforces budget (`max_jobs`, `max_total_iterations`, `max_total_candidates`), integer fields, backend/target/gate enums, production-intent `skill`+`eval_file`, and rejects writeback fields (`auto_adopt`, `force`, `adopt`, `rollback`, etc.).
  - Emits `hermes-skillopt-batch-run-v1` preflight data and does not write or run jobs.

- `python3 -m hermes_skillopt.cli batch-run PLAN.json`
  - Runs only after preflight; creates a batch parent staging directory with `preflight.json`, `jobs.json`, `summary.json`, `report.md`, and `manifest.json`.
  - Child jobs call `full_run` with `auto_adopt=false` and `force=false`; no batch path adopts or writes live skills.

- `python3 -m hermes_skillopt.cli fleet-report|fleet-resume-plan|fleet-rollback-plan`
  - Read-only fleet inspection over recent single-run and batch parent/child artifacts.
  - Fleet report groups by skill, advisory skill type, readiness, adoptability, and rollbackability; rows include readiness and evidence-contract summaries.
  - Resume plan reports only completed exact-fingerprint reuse guidance; partial-stage continuation is unavailable.
  - Rollback plan lists per-run rollbackable backups, verified backup/current-SHA status where safely readable, and exact one-run commands; there is no bulk rollback/writeback.

- `python3 -m hermes_skillopt.cli eval-pack-inventory|eval-pack-scaffold`
  - Inventory surfaces real coverage and gaps per skill: candidate eval paths, existing valid/invalid packs, split counts, production-eligible task counts, review-only status, and missing reasons.
  - Inventory includes the readiness matrix and advisory skill-type classification.
  - Scaffold creates a safe review-only `hermes-curated-eval-pack-v1` starter with complete train/validation/test samples, `sample_pack: true`, `allow_production_adoption: false`, and static-review-only execution contract. It is not curated evidence.

- `python3 -m hermes_skillopt.cli eval-pack-curate|eval-pack-mine-sessions`
  - Curate is the canonical factory for local task JSON; outputs are review-only unless explicit production policy and an adoption-eligible execution contract are supplied.
  - Session mining redacts/mines sessions or fixtures into draft review-only packs; session-mined evidence cannot authorize production adoption.

- `python3 -m hermes_skillopt.cli benchmark --skill SKILL --eval-file PACK.json`
  - Alias for eval-only fixed-skill scoring.
  - Output: staging run with `eval_report.json`, `benchmark_report.json`, `report.md`, `manifest.json`, and `adoptable: false`.
  - Validates/records: explicit eval file, skill/eval/target fingerprints, split scorecard summary, and read-only safety flags (`optimizer_training: false`, `adoption_side_effects: false`, `task_provided_commands_allowed: false`).
  - Reports production-curated and review-only score buckets separately; review-only evidence does not become a production benchmark claim.
  - Limitation: local Hermes-native report MVP only; no upstream benchmark parity claim.

- `hermes_skillopt.benchmark_bridge.import_upstream_manifest(...)`
  - Inputs: upstream-style JSON manifest with embedded `tasks` or `splits`.
  - Output: Hermes `hermes-curated-eval-pack-v1` payload and optional output file.
  - Validates: schema, no executable/remote fields, shared safe `.json` output path guard, validate-before-replace write flow, deterministic scorecard fields, split completeness, leakage, sample/prod eligibility, fingerprint.

- `hermes_skillopt.transfer.transfer_eval(...)`
  - Inputs: staged `run_id` or explicit staged `skill_file`, eval pack/staged task artifacts, target list, profile list.
  - Output: `hermes-skillopt-transfer-eval-v1` report with profile/backend/target fingerprints plus advisory skill type, readiness, and eval/target evidence-contract status, written only through the shared safe report path guard when an output file is requested.
  - Default posture: staged/report-only/read-only; no live skill writeback and no external/live model performance claim.

- `hermes_skillopt.conformance.run_conformance(...)`
  - Modes: `quick` (default deterministic smoke/regression suite) and `full` (all local pytest tests).
  - Important: quick mode is not a full repository health check and must not be reported as one.
  - Runs: `python -m compileall -q hermes_skillopt tests` plus mode-selected/custom pytest args.
  - Output: `hermes-skillopt-conformance-v1` JSON report with `mode`, `pytest_args`, and `scope_note`, written only through the shared safe report path guard when an output file is requested.

- `hermes_skillopt.core.review(..., slim=True)`
  - Verifies staged artifact hashes before returning run data.
  - Returns `artifact_refs` with diff/report path, SHA-256, byte size, and preview length; slim mode intentionally omits large `diff_preview`/`report_summary` payloads.
  - Includes `artifact_lineage` summaries for skill hashes, eval pack identity/fingerprint, target/provenance fingerprints, and tracked artifact state.

CLI equivalents (console script after editable install, or `python3 -m hermes_skillopt.cli ...` from the repo):

- `hermes-skillopt benchmark --skill SKILL --eval-file PACK.json`
- `hermes-skillopt import-upstream-benchmark MANIFEST --output PACK.json`
- `hermes-skillopt transfer-eval --run-id RUN --target scorecard --target replay --output report.json`
- `hermes-skillopt conformance --output conformance.json`

Hermes plugin tool equivalents registered in `plugin.yaml`:

- `hermes_skillopt_import_upstream_benchmark`
- `hermes_skillopt_transfer_eval`
- `hermes_skillopt_conformance`

## Known limitations

- The bundled `examples/evals/*production_v1.json` files are static review fixtures, not production certification suites for any skill.
- Eval-pack inventory/scaffold intentionally exposes that many skills have no true curated pack yet. Do not treat scaffold/sample/static packs as production evidence.
- Batch/fleet surfaces are orchestration/reporting helpers only; batch never adopts, and fleet resume/rollback commands never perform resume/rollback/delete/writeback themselves.
- `benchmark`/`eval-only` reports are local fixed-skill reports and do not establish Microsoft SkillOpt benchmark parity or external model performance.
- The bridge supports common upstream-style JSON manifests, not arbitrary upstream repository benchmark loaders. Current upstream bridge support is `json_import_only` plus `pinned_manifest_replay` for data-only manifests under the canonical pinned clone; `pinned_upstream_execution` and `parity_evidence_complete` are unsupported/future. True upstream benchmark execution is unsupported even though the Hermes-native frozen-Hermes sandbox MVP can produce local isolated runtime evidence.
- Split manifest support is embedded JSON only; file references are intentionally not followed in P3.
- Transfer evaluation uses existing deterministic target executors; it does not provision live external model/backend services or establish real cross-model results.
- Conformance reports local adapter health only; they do not certify Microsoft SkillOpt parity or external benchmark performance.

## Deterministic trainer metadata

Each six-stage trainer artifact under `stages/` records `schema_version: skillopt-stage-v1` plus `deterministic_batch` metadata using `batch_schema: skillopt-deterministic-batch-v1`. The metadata includes a stable `batch_id` (`iter-NNN-stage`), iteration, stage, seed `0`, a stable input-order/deterministic-rank note, and input SHA-256. This documents replayability of stage inputs/ordering; it does not imply stochastic upstream trainer parity or external model determinism.


## Additional current conformance points

- P0 status surfaces: `compare-upstream-pin` and `benchmark-parity-status` are read-only/report-only.
- P1 target adapter: `LiveHermesReadOnlyRunner` is a disabled/report-only interface, not an implemented live Hermes runner. `frozen_hermes_target_execution_v1` is currently implemented only through the constrained sandbox MVP and must include frozen target config, provider/model/toolset/session fingerprints, isolated runtime, permissions, transcript/trajectory artifact, and execution-based scoring. It is not upstream parity or arbitrary live agent command execution.
- P1 benchmark adapter: `JsonEvalPackBenchmarkAdapter` owns safe JSON eval-pack loading plus governance diagnostics.
- P1 writeback safety: adopt/rollback use `skillopt/writeback.lock` and audit JSONL events.
- P2 governance/UX: manifests/reports/WebUI expose eval pack governance, parity labels, gate/provenance/lineage, and remain staged/read-only by default.
