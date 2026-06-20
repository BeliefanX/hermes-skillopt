# Usage policy

This policy summarizes the current runtime-evidence, cron-safe UX, adoption, WebUI safety, curator-boundary, and upstream-parity semantics for `hermes-skillopt`.

## Product and curator boundary

`hermes-skillopt` complements Hermes' native curator. The curator owns skill lifecycle, archive state, consolidation, hub/bundled/pinned ownership, and native sidecars. SkillOpt owns staged eval evidence, artifact review, and adoption recommendations only. It reads native sidecars best-effort for advisory metadata and adopt guards; it does not write or replace them.

## Decision vocabulary

Use the effective readiness schema rather than a bare `adoptable=true` claim:

- `manifest_adoptable` / `raw_adoptable`: what a staged manifest says before rechecking runtime evidence and guards.
- `production_adoptable`: effective writeback readiness after artifact verification, strict curated validation/test gates, evidence ledger readiness, reviewer gate, and native guards.
- `review_only`: evidence may inform review/authoring but cannot authorize production writeback.
- `blockers`: reasons preventing production adoption.
- `next_safe_action`: the recommended human action; it is guidance, not automation.

Surfaces may keep `adoptable` for backward compatibility, but docs and reports should interpret it as `production_adoptable` only when accompanied by the readiness schema and no blockers.

## Runtime evidence and production eligibility

Production adoption is intentionally strict. A run is production-eligible only when hashed staged artifacts and adopt-time rechecks show all required evidence:

- frozen target config and target backend fingerprints;
- provider, model, toolset, tool-policy, session, and runtime fingerprints;
- isolated runtime proof and permission policy with task-provided commands disabled;
- transcript/trajectory artifact or fingerprint;
- scoring derived from execution output rather than only static `SKILL.md` text;
- explicit real Hermes runtime invocation/evidence;
- no `task_commands_executed=true` row;
- deterministic reviewer gate approval, curated validation/test gates, provenance/current/proposed SHA checks, and native Hermes adopt guard clearance.

Missing evidence, non-production internal runners, or any task command execution downgrades/blocks production eligibility. Current sandbox/fixed/internal, static, scorecard, replay-only, and disabled `live-readonly` paths are review-only unless a future explicit compliant evidence contract proves otherwise. Session-mined, direct/generated, fallback, synthetic, scaffold, sample, and correction/context seed evidence also remains review-only unless explicitly curated into a production eval pack with a compliant execution contract.

Most skills may have no production-capable pack yet. Production eligibility requires an explicit curated `hermes-curated-eval-pack-v1` with non-leaking train/validation/test splits, production policy, and an adoption-eligible execution contract; scaffold/sample/static/imported JSON packs are coverage starters or review artifacts, not proof.

## Cron-safe and manual surfaces

Only these tools are cron-safe defaults:

- `scout` / `scout --digest`
- `doctor` / `doctor --digest`
- `eval-pack-inventory` / `eval-pack-inventory --digest`
- `eval-pack-doctor` / `eval-pack-doctor --digest`

`review --digest` is digest-only/manual for an already-staged run; it is not a scheduled default. Do not schedule `status`, `review`, `full-run`, `optimize`, `adopt`, `rollback`, `upstream-update`, cleanup, eval-pack draft/promotion/skeleton writes, skill-quality skeleton creation, or any live writeback path.

## Safe optimization and adoption UX

Optimization is staged-only. Smoke/review/production intent runs write review artifacts, never live skills. Production intent still remains review-only unless strict curated validation/test evidence and complete runtime evidence are present.

Live writeback requires explicit `adopt <run_id> --confirm "ADOPT <run_id>"`; rollback requires explicit rollback confirmation and a verified rollback handle/backup manifest. Adopt and rollback perform readback verification before reporting success. `force` does not bypass artifact, provenance, runtime-evidence, reviewer, production/test, proposed-SHA, native conflict, or native metadata drift guards.

WebUI writeback endpoints are local-loopback only by default. If the server is bound to a non-loopback host, `/api/adopt` and `/api/rollback` are disabled unless the operator starts with `--unsafe-writeback-on-nonlocal-host`; even then, exact typed confirmation and all core adopt/rollback guards remain required. The WebUI home override is not honored for writeback/upstream-update paths.

## Upstream and benchmark parity

This plugin does not claim Microsoft SkillOpt full benchmark parity. Upstream support is limited to pinned clone/lock status and safe JSON/pinned-manifest import/conversion bridges. `benchmark`, `eval-only`, transfer eval, conformance, imported manifests, and sandbox/fixed-runner frozen-Hermes evidence are local Hermes review/report artifacts only; they are not upstream execution, external performance claims, or production adoption proof.
