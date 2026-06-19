# Release notes

## 0.1.0 Phase0-Phase5 implementation snapshot

- Documents native-boundary/eval-maturity implementation updates:
  - SkillOpt explicitly does not replace the Hermes curator: curator owns lifecycle/archive/consolidation and native skill ownership; SkillOpt owns staged eval evidence and adoption recommendations.
  - Review/status/adopt surfaces read Hermes-native `.usage.json`, curator state, hub/bundled manifests, and provenance/manifest sidecars best-effort for diagnostics and guard fingerprints only; SkillOpt writes no native sidecars.
  - Hub-installed, bundled, pinned, archived, and curator-managed skills are blocked/diagnostic-only for SkillOpt adoption by default, and `force` does not bypass the native conflict guard or native metadata fingerprint drift.
  - Evidence ledger fields (`eval_level`, `evidence_maturity`, `production_runtime_ready`, complete frozen evidence, real runtime invocation/evidence, task command, internal-runner, reviewer-gate, blockers) separate review-only static/replay/sandbox/live-disabled evidence from complete production runtime evidence.
  - Adopt requires strict gates, complete frozen runtime evidence, reviewer gate, native conflict guard, current SHA/proposed SHA/provenance hashes, and hashed-artifact crosschecks; cron guidance is narrowed to scout/doctor/inventory/review digest style read-only monitoring.
  - WebUI review exposes evidence maturity/native guard summaries, WebUI runs remain staged-only, and one-click eval-pack promotion remains review-only.

- Documents latest batch/eval/fleet/WebUI/upstream status surfaces:
  - batch preflight validates data-only plans, budget caps, backend/target/gate policy, production-intent requirements, and forbidden writeback fields before any job runs;
  - batch run is staged-only, writes batch parent artifacts, and calls child `full_run` with `auto_adopt=false` and `force=false`;
  - fleet report/resume-plan/rollback-plan are read-only planning surfaces; completed exact-fingerprint reuse remains opt-in, partial continuation is refused, rollback remains per-run only, report rows include readiness/skill-type/evidence-contract status, grouping covers skill/type/readiness/adoptability/rollbackability, and rollback plans include safely readable backup/current-SHA status plus exact one-run commands;
  - eval-pack inventory/scaffold/curate/session-mining exposes missing or review-only coverage instead of implying every skill has a curated pack; scaffold/session-mined output is review-only, and the curated factory is production-capable only with explicit policy plus adoption-eligible execution contract;
  - eval-pack doctor/autopilot/seeding/promotion UX is documented: doctor is read-only, autopilot is plan/read-only by default and only writes guarded review drafts with an explicit flag, correction/context/negative-boundary generators produce review-only seed packs, WebUI one-click promotion is review-only, and production promotion requires explicit policy plus execution contract;
  - React/FastAPI WebUI now exposes fleet/upstream parity surfaces while keeping runs staged-only and defaulting to review-oriented soft gating; production adoption still requires strict/non-mock/curated val-test proof plus explicit guarded adopt;
  - upstream benchmark adapter levels are explicit: `json_import_only` and `pinned_manifest_replay` are supported data-only evidence levels; `pinned_upstream_execution` and `parity_evidence_complete` remain unsupported/future.

- Documents latest guided/runtime/CI hardening deltas:
  - `scout`, `doctor`, `optimize --intent smoke|review|production`, `review latest --summary`, `review --digest`, CLI adopt confirmation, and WebUI scout/wizard/review console APIs are aligned around staged-only/no-auto-adopt behavior;
  - unified readiness/adoptability schema (`hermes-skillopt-readiness-adoptability-v1`) and readiness matrix (`hermes-skillopt-readiness-matrix-v1`) separate validation, production-best, held-out-test, review-only, blockers, warnings, and safe next actions;
  - decision summaries/digests expose production/test gate status, evidence class, blockers, score provenance, artifact refs, and next safe action; digest is notification-friendly and omits raw report/diff previews;
  - missing runtime evidence for adoption-eligible frozen-target contracts downgrades production eligibility; scorecard/static skill-text evidence remains review-only and must not be described as adoption-eligible frozen Hermes execution;
  - sandbox/frozen-Hermes target evidence records explicit evidence class/scope, isolated runtime fingerprints, permissions with task commands disabled, transcript/trajectory, and execution-scoring evidence; task-provided commands are blocked by default; sandbox/fixed internal runner evidence is review-only and cannot production-adopt without real Hermes runtime invocation proof;
  - production hard-failed rows override soft score gains, and artifact hygiene/conformance remain read-only local CI aids with no upstream benchmark parity claim.

- Documents latest P0/P1/P2 hardening deltas:
  - scout mixed-inventory gaps are handled as reportable readiness gaps instead of crashing; scout is cron-safe/read-only and CLI scout writes no report unless `--output` is supplied;
  - conformance now returns JSON with `report_path: null` by default and writes a guarded file only with explicit `--output`; it no longer creates a repo-root `skillopt_conformance_report.json` by default;
  - eval-pack factory/import writes validate before atomic replacement, and target evidence summaries treat `task_commands_executed=true` as incomplete evidence rather than production-ready proof;
  - shared `safety.py` report/eval output guard now covers benchmark import, transfer eval, and conformance reports, blocking live/runtime-sensitive profile/plugin/source paths and symlink escapes;
  - bounded edit validation now checks replacement/insert text for protected headings/markers and allowed-region marker boundary mutations;
  - `frozen-hermes` / `frozen_hermes_target_execution_v1` currently routes through the sandbox/fixed internal runner with isolated runtime evidence, provider/model/toolset/session fingerprints, transcript/trajectory scoring evidence, task commands disabled, and no live writes; this is review-only/non-production until real Hermes runtime invocation proof is available;
  - reports distinguish production-curated score from review-only score, include score ledgers/per-task deltas with expected-term/assertion changes, expose score provenance fields, and surface held-out test sensitivity warnings;
  - artifact hygiene rows classify verified/tampered/checkpoint/stale/orphaned states with `partial_continuation_available: false` and safe next actions;
  - advisory skill package metadata (`references/`, `templates/`, `scripts/`, `assets/`) is surfaced for curator awareness without changing staged-only write scope or adoption authority;
  - upstream/benchmark reporting remains import-only/data-only with no Microsoft SkillOpt upstream execution/parity claim, no upstream Python import/network/task commands, and no live skill writes;
  - transfer eval remains report-only/read-only, now surfaces readiness/skill-type/evidence-contract summaries, and staged-only history/rejected/slow metadata remains audit evidence.

- Documents current Phase0-5 behavior: staged-only Hermes SkillOpt-inspired adapter, eval-only/benchmark fixed-skill scoring with `hermes-native-benchmark-report-v1`, deterministic trainer batch metadata, provenance v2, separated optimizer/target backends, default `strict` gate mode, hard production validation failure overrides, conservative resume/checkpoint inspection with lineage summaries, history/lineage, slow_meta evidence, EnvAdapter benchmark/session foundation, benchmark bridge, transfer eval, conformance, guided CLI/plugin/WebUI review surfaces, artifact hygiene reporting, and runtime-evidence gate checks.
- Records latest eval hardening tasks 1-5:
  - upstream benchmark import now has a safe output path guard plus validate-before-replace writes;
  - adoption-capable runs default to strict gate mode, while soft/mixed are explicit review/non-production choices and production-eligible validation rows with `passed: false` block staging/adoptability even when soft score improves;
  - critical `all_required_keywords`, `required_markers`, and `forbidden_markers` are hard constraints; `examples/evals/tiktok_seedance_thermal_v4.json` is the curated thermal-v4 fixture for those semantics;
  - bundled static/keyword eval packs, including historical `*production_v1.json` example filenames, are review-only/non-adoption;
  - eval execution contract classifications distinguish static/report-only evidence from adoption-eligible curated replay contracts and the `frozen_hermes_target_execution_v1` contract, whose production path requires real Hermes runtime evidence; current sandbox/fixed-runner evidence remains review-only;
  - upstream benchmark bridge is import-only/no-execution; true upstream benchmark execution remains unsupported, and local frozen-Hermes sandbox/fixed-runner evidence is not parity or production proof.
- Records upstream hygiene: Microsoft SkillOpt remains a pinned external clone/lock; update commands clone/fetch/pin metadata only and do not merge plugin code or write live skills.
- Clarifies conformance semantics: strict validation improvement, bounded edit validation, train/val/test isolation, rejected edit buffers, production-only curated eval gates, and completed-run-only resume reuse.
- Adds docs/CLI/plugin metadata consistency tests so help text, plugin schemas, `plugin.yaml`, and `pyproject.toml` stay aligned.
- Clarifies remaining limitations: local Hermes-native reports and bundled safety/tool-use static review eval seeds do not claim Microsoft SkillOpt parity, external benchmark scores, real cross-model transfer, production adoption eligibility, or universal certification.


## Earlier P0-P2 support surfaces

- Added read-only upstream pin comparison and benchmark/parity status surfaces.
- Added disabled-by-default live Hermes read-only target adapter with provenance fingerprints.
- Added JSON benchmark adapter v1 and eval-pack governance diagnostics.
- Added adopt/rollback lock and writeback audit events.
- Surfaced parity/gate/provenance/lineage in CLI/WebUI/report artifacts.
- Updated docs to distinguish Hermes-native benchmark mode from upstream parity; no upstream parity overclaim.
