# Release notes

## 0.1.0 P0-P3 implementation snapshot

- Documents current Phase0-3 behavior: staged-only Hermes SkillOpt-inspired adapter, eval-only/benchmark fixed-skill scoring with `hermes-native-benchmark-report-v1`, deterministic trainer batch metadata, provenance v2, separated optimizer/target backends, default `strict` gate mode, conservative resume checkpoints, history/lineage, slow_meta evidence, EnvAdapter benchmark/session foundation, benchmark bridge, transfer eval, conformance, and WebUI review surfaces.
- Records latest eval hardening tasks 1-5:
  - upstream benchmark import now has a safe output path guard plus validate-before-replace writes;
  - adoption-capable runs default to strict gate mode, while soft/mixed are explicit review/non-production choices;
  - bundled static/keyword eval packs, including historical `*production_v1.json` example filenames, are review-only/non-adoption;
  - eval execution contract classifications distinguish static/report-only evidence from adoption-eligible curated replay contracts and future `frozen_hermes_target_execution_v1` evidence requirements;
  - upstream benchmark parity is import-only supported; true upstream benchmark execution remains unsupported until adapters and frozen-target evidence exist.
- Records upstream hygiene: Microsoft SkillOpt remains a pinned external clone/lock; update commands clone/fetch/pin metadata only and do not merge plugin code or write live skills.
- Clarifies conformance semantics: strict validation improvement, bounded edit validation, train/val/test isolation, rejected edit buffers, production-only curated eval gates, and completed-run-only resume reuse.
- Adds docs/CLI/plugin metadata consistency tests so help text, plugin schemas, `plugin.yaml`, and `pyproject.toml` stay aligned.
- Clarifies remaining limitations: local Hermes-native reports and bundled safety/tool-use static review eval seeds do not claim Microsoft SkillOpt parity, external benchmark scores, real cross-model transfer, production adoption eligibility, or universal certification.


## Track B P0-P2

- Added read-only upstream pin comparison and benchmark/parity status surfaces.
- Added disabled-by-default live Hermes read-only target adapter with provenance fingerprints.
- Added JSON benchmark adapter v1 and eval-pack governance diagnostics.
- Added adopt/rollback lock and writeback audit events.
- Surfaced parity/gate/provenance/lineage in CLI/WebUI/report artifacts.
- Updated docs to distinguish Hermes-native benchmark mode from upstream parity; no upstream parity overclaim.
