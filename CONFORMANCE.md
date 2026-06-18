# Hermes SkillOpt P3 Conformance Spec

This document is the P3 conformance contract for the standalone Hermes SkillOpt plugin. It records where the adapter intentionally aligns with Microsoft SkillOpt concepts, where Hermes diverges for safety, and what remains out of scope.

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
- Rollout/target model: frozen target executor (`scorecard`, `replay`, or `sandbox`) with backend config fingerprints.
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
- Resume tooling is inspection-first: incomplete/stale checkpoints are reported with stage/artifact fingerprints and cleanup guidance, but partial-stage continuation is unavailable because replaying from the middle could skip gates or adoptability checks.

## P0/P1/P3 commands/modules

- `python3 -m hermes_skillopt.cli benchmark --skill SKILL --eval-file PACK.json`
  - Alias for eval-only fixed-skill scoring.
  - Output: staging run with `eval_report.json`, `benchmark_report.json`, `report.md`, `manifest.json`, and `adoptable: false`.
  - Validates/records: explicit eval file, skill/eval/target fingerprints, split scorecard summary, and read-only safety flags (`optimizer_training: false`, `adoption_side_effects: false`, `task_provided_commands_allowed: false`).
  - Limitation: local Hermes-native report MVP only; no upstream benchmark parity claim.

- `hermes_skillopt.benchmark_bridge.import_upstream_manifest(...)`
  - Inputs: upstream-style JSON manifest with embedded `tasks` or `splits`.
  - Output: Hermes `hermes-curated-eval-pack-v1` payload and optional output file.
  - Validates: schema, no executable/remote fields, safe `.json` output path, validate-before-replace write flow, deterministic scorecard fields, split completeness, leakage, sample/prod eligibility, fingerprint.

- `hermes_skillopt.transfer.transfer_eval(...)`
  - Inputs: staged `run_id` or explicit staged `skill_file`, eval pack/staged task artifacts, target list, profile list.
  - Output: `hermes-skillopt-transfer-eval-v1` report with profile/backend/target fingerprints.
  - Default posture: staged/report-only/read-only; no live skill writeback and no external/live model performance claim.

- `hermes_skillopt.conformance.run_conformance(...)`
  - Modes: `quick` (default deterministic smoke/regression suite) and `full` (all local pytest tests).
  - Important: quick mode is not a full repository health check and must not be reported as one.
  - Runs: `python -m compileall -q hermes_skillopt tests` plus mode-selected/custom pytest args.
  - Output: `hermes-skillopt-conformance-v1` JSON report with `mode`, `pytest_args`, and `scope_note`.

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
- `benchmark`/`eval-only` reports are local fixed-skill reports and do not establish Microsoft SkillOpt benchmark parity or external model performance.
- The bridge supports common upstream-style JSON manifests, not arbitrary upstream repository benchmark loaders. Current upstream bridge support is import-only; true upstream benchmark execution is unsupported until adapters and frozen-target evidence exist.
- Split manifest support is embedded JSON only; file references are intentionally not followed in P3.
- Transfer evaluation uses existing deterministic target executors; it does not provision live external model/backend services or establish real cross-model results.
- Conformance reports local adapter health only; they do not certify Microsoft SkillOpt parity or external benchmark performance.

## Deterministic trainer metadata

Each six-stage trainer artifact under `stages/` records `schema_version: skillopt-stage-v1` plus `deterministic_batch` metadata using `batch_schema: skillopt-deterministic-batch-v1`. The metadata includes a stable `batch_id` (`iter-NNN-stage`), iteration, stage, seed `0`, a stable input-order/deterministic-rank note, and input SHA-256. This documents replayability of stage inputs/ordering; it does not imply stochastic upstream trainer parity or external model determinism.


## Additional current conformance points

- P0 status surfaces: `compare-upstream-pin` and `benchmark-parity-status` are read-only/report-only.
- P1 target adapter: `LiveHermesReadOnlyRunner` is a disabled/report-only interface, not an implemented live Hermes runner. Future `frozen_hermes_target_execution_v1` adoption evidence must include frozen target config, provider/model/toolset/session fingerprints, isolated runtime, permissions, transcript/trajectory artifact, and execution-based scoring.
- P1 benchmark adapter: `JsonEvalPackBenchmarkAdapter` owns safe JSON eval-pack loading plus governance diagnostics.
- P1 writeback safety: adopt/rollback use `skillopt/writeback.lock` and audit JSONL events.
- P2 governance/UX: manifests/reports/WebUI expose eval pack governance, parity labels, gate/provenance/lineage, and remain staged/read-only by default.
