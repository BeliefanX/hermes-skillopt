# Usage policy

This policy summarizes the current P0-P2 runtime-evidence, cron-safe UX, adoption, and upstream-parity semantics for `hermes-skillopt`.

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

Missing evidence, non-production internal runners, or any task command execution downgrades/blocks production eligibility. Current sandbox/fixed/internal, static, scorecard, replay-only, and disabled `live-readonly` paths are review-only unless a future explicit compliant evidence contract proves otherwise. Session-mined, direct/generated, fallback, synthetic, scaffold, sample, correction/context seed, and legacy dry-run evidence also remains review-only unless explicitly curated into a production contract with all runtime and gate evidence above.

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

## Upstream and benchmark parity

This plugin does not claim Microsoft SkillOpt full benchmark parity. Upstream support is limited to pinned clone/lock status and safe JSON/pinned-manifest import/conversion bridges. `benchmark`, `eval-only`, transfer eval, conformance, imported manifests, and sandbox/fixed-runner frozen-Hermes evidence are local Hermes review/report artifacts only; they are not upstream execution, external performance claims, or production adoption proof.
