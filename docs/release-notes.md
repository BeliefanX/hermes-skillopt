# Release notes

## 0.1.0 P0-P3 implementation snapshot

- Documents latest P0/P1/P2 hardening deltas:
  - shared `safety.py` report/eval output guard now covers benchmark import, transfer eval, and conformance reports, blocking live/runtime-sensitive profile/plugin/source paths and symlink escapes;
  - bounded edit validation now checks replacement/insert text for protected headings/markers and allowed-region marker boundary mutations;
  - `frozen-hermes` / `frozen_hermes_target_execution_v1` is a sandbox-backed Hermes target execution MVP with isolated runtime evidence, provider/model/toolset/session fingerprints, transcript/trajectory scoring evidence, task commands disabled, and no live writes;
  - reports distinguish production-curated score from review-only score, include score ledgers/per-task deltas with expected-term/assertion changes, and surface held-out test sensitivity warnings;
  - upstream/benchmark reporting remains import-only/data-only with no Microsoft SkillOpt upstream execution/parity claim, no upstream Python import/network/task commands, and no live skill writes;
  - transfer eval remains report-only/read-only and staged-only history/rejected/slow metadata remains audit evidence.

- Documents current Phase0-3 behavior: staged-only Hermes SkillOpt-inspired adapter, eval-only/benchmark fixed-skill scoring with `hermes-native-benchmark-report-v1`, deterministic trainer batch metadata, provenance v2, separated optimizer/target backends, default `strict` gate mode, hard production validation failure overrides, conservative resume/checkpoint inspection with lineage summaries, history/lineage, slow_meta evidence, EnvAdapter benchmark/session foundation, benchmark bridge, transfer eval, conformance, and WebUI review surfaces.
- Records latest eval hardening tasks 1-5:
  - upstream benchmark import now has a safe output path guard plus validate-before-replace writes;
  - adoption-capable runs default to strict gate mode, while soft/mixed are explicit review/non-production choices and production-eligible validation rows with `passed: false` block staging/adoptability even when soft score improves;
  - critical `all_required_keywords`, `required_markers`, and `forbidden_markers` are hard constraints; `examples/evals/tiktok_seedance_thermal_v4.json` is the curated thermal-v4 fixture for those semantics;
  - bundled static/keyword eval packs, including historical `*production_v1.json` example filenames, are review-only/non-adoption;
  - eval execution contract classifications distinguish static/report-only evidence from adoption-eligible curated replay contracts and the current sandbox-backed `frozen_hermes_target_execution_v1` MVP evidence contract;
  - upstream benchmark bridge is import-only/no-execution; true upstream benchmark execution remains unsupported despite local frozen-Hermes MVP evidence.
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
