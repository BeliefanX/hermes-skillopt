# Release notes

## Current release: dd0e43f — runtime evidence and UX policy hardening

This changelog records the current release state. Long-lived policy lives in `docs/usage-policy.md`, conformance rules in `CONFORMANCE.md`, upstream/parity policy in `UPSTREAM.md`, and the implementation map in `docs/architecture.md`.

### Runtime evidence and adoption gates

- Adoption-capable runs default to strict gating and must pass curated validation and held-out test eligibility, artifact/provenance/current-SHA checks, native Hermes conflict guards, and reviewer/runtime evidence gates.
- `frozen-hermes` / `frozen_hermes_target_execution_v1` currently routes through constrained sandbox/fixed internal runner evidence with isolated runtime fingerprints, transcript/trajectory scoring evidence, and task commands disabled. It is review-only until real Hermes runtime invocation proof is available.
- Target evidence summaries treat `task_commands_executed=true`, missing real runtime evidence, missing real runtime invocation, or non-production internal runners as incomplete evidence rather than production-ready proof.
- Reports and review surfaces distinguish production-curated score from review-only score, include score provenance, per-task deltas, assertion/expected-term changes, and held-out sensitivity warnings.

### P0-P2 cleanup and safety fixes

- `scout` handles mixed inventory gaps as reportable readiness gaps instead of crashing; CLI scout remains read-only and writes no report unless `--output` is supplied.
- Conformance returns JSON with `report_path: null` by default and writes a guarded report only with explicit `--output`; it no longer creates a default repo-root report.
- Eval-pack factory/import writes validate before atomic replacement, and shared safe output guards cover benchmark import, transfer eval, and conformance reports.
- Bounded edit validation checks inserted/replacement text for protected headings, protected markers, and allowed-region boundary mutations.
- Artifact hygiene rows classify verified/tampered/checkpoint/stale/orphaned states with `partial_continuation_available: false` and safe next actions.

### UX, cron-safe diagnostics, and review surfaces

- `eval-pack-workflow` is a read-only authoring summary; `skill-readiness-queue` / `skill-queue` is a read-only prioritization surface; `skill-quality` / `skill-lint` is read-only unless explicitly creating a guarded review-only eval skeleton.
- The cron-safe default diagnostic surfaces are limited to `scout --digest`, `doctor --digest`, `eval-pack-inventory --digest`, and `eval-pack-doctor --digest`. Existing-run `review --digest` is digest-only/manual, not a scheduled default.
- Guided CLI/plugin/WebUI surfaces keep smoke/review/production intent staged-only. WebUI run APIs pass `auto_adopt=false` and `force=false`; typed adopt/rollback still delegate to core guards.
- Decision summaries and digests expose production/test gate status, evidence class, blockers, artifact refs, score provenance, and next safe action without requiring large report/diff previews.

### Eval packs, batch/fleet, upstream, and CI semantics

- Eval-pack inventory, doctor, scaffold, curate, session-mining, correction/context seeds, negative/boundary generation, and promotion distinguish review-only drafts/sample packs from production-capable curated packs with explicit policy and adoption-eligible execution contracts.
- Bundled static review packs are named `examples/evals/*static_review_v1.json`; they are review/training fixtures and cannot authorize production adoption.
- Batch preflight validates data-only plans, budget caps, backend/target/gate policy, production-intent requirements, and forbidden writeback fields before any job runs. Batch run remains staged-only and calls child `full_run` with no auto-adopt/force.
- Fleet report, resume-plan, and rollback-plan are read-only planning surfaces with readiness, skill-type, evidence-contract, rollbackability, and safely readable backup/current-SHA status. Rollback remains exact one-run only.
- Upstream benchmark support remains import-only/data-only: `json_import_only` and `pinned_manifest_replay` are supported evidence levels; `pinned_upstream_execution` and `parity_evidence_complete` remain unsupported/future. Upstream update commands clone/fetch/pin metadata only and do not merge code or write live skills.

### Documentation cleanup

- The current architecture document is `docs/architecture.md`; obsolete gap-document naming has been removed.
- Release notes now summarize the latest changelog only instead of duplicating the long-lived runtime, cron, adoption, upstream, and conformance policies.
