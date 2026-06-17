# Hermes SkillOpt P3 Conformance Spec

This document is the P3 conformance contract for the standalone Hermes SkillOpt plugin. It records where the adapter intentionally aligns with Microsoft SkillOpt concepts, where Hermes diverges for safety, and what remains out of scope.

## Scope

P3 adds deterministic, local, no-credential tooling for:

- Safe upstream-style benchmark manifest import into Hermes curated eval pack format.
- Read-only staged transfer evaluation across target/profile/backend configs.
- Continuous local conformance/regression execution and machine-readable reports.

No P3 command vendors Microsoft SkillOpt, imports upstream Python modules, executes benchmark code, or requires external services.

## Microsoft SkillOpt alignment

Hermes maps the core SkillOpt abstraction as follows:

- Trainable state: a Hermes `SKILL.md` document.
- Environment/benchmark: Hermes eval packs with explicit train/val/test split governance.
- Rollout/target model: frozen target executor (`scorecard`, `replay`, or `sandbox`) with backend config fingerprints.
- Reflection/update: optimizer proposes bounded edits; candidate text is staged.
- Evaluation gate: validation/test scorecards decide whether a candidate is review-worthy or adoptable.
- Artifacts/checkpoints: every staged run records manifest, task/eval, target, optimizer, profile, and provenance fingerprints.

## Intentional Hermes divergences

Hermes preserves the outer safety shell even when adapting upstream benchmark concepts:

- Upstream benchmark bridge is JSON-data-only. It rejects executable or remote fields such as `code`, `script`, `command`, `module`, `entrypoint`, `url`, and container/image fields.
- Imported sample packs are review-only by default. Production gate eligibility is disabled unless a caller explicitly imports as curated and tasks also satisfy Hermes production eval policy.
- Eval pack validation rejects invalid splits and leakage by duplicate ids/prompts across train/val/test splits.
- Transfer evaluation is report-only/read-only: it evaluates staged/proposed skill text or an explicit file and never writes live skills.
- Cross-profile evaluation records profile fingerprints but does not mutate target profiles.
- Conformance tooling runs local deterministic `compileall` plus pytest commands and does not depend on network, credentials, upstream checkout, or live Hermes services.

## P3 commands/modules

- `hermes_skillopt.benchmark_bridge.import_upstream_manifest(...)`
  - Inputs: upstream-style JSON manifest with embedded `tasks` or `splits`.
  - Output: Hermes `hermes-curated-eval-pack-v1` payload and optional output file.
  - Validates: schema, no executable/remote fields, deterministic scorecard fields, split completeness, leakage, sample/prod eligibility, fingerprint.

- `hermes_skillopt.transfer.transfer_eval(...)`
  - Inputs: staged `run_id` or explicit staged `skill_file`, eval pack/staged task artifacts, target list, profile list.
  - Output: `hermes-skillopt-transfer-eval-v1` report with profile/backend/target fingerprints.
  - Default posture: staged/report-only; no live skill writeback.

- `hermes_skillopt.conformance.run_conformance(...)`
  - Runs: `python -m compileall -q hermes_skillopt tests` and deterministic pytest args.
  - Output: `hermes-skillopt-conformance-v1` JSON report.

CLI equivalents (console script after editable install, or `python3 -m hermes_skillopt.cli ...` from the repo):

- `hermes-skillopt import-upstream-benchmark MANIFEST --output PACK.json`
- `hermes-skillopt transfer-eval --run-id RUN --target scorecard --target replay --output report.json`
- `hermes-skillopt conformance --output conformance.json`

## Known limitations

- The bridge supports common upstream-style JSON manifests, not arbitrary upstream repository benchmark loaders.
- Split manifest support is embedded JSON only; file references are intentionally not followed in P3.
- Transfer evaluation uses existing deterministic target executors; it does not provision live external model/backend services or establish real cross-model results.
- Conformance reports local adapter health only; they do not certify Microsoft SkillOpt parity or external benchmark performance.
