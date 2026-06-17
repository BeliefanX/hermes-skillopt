# Release notes

## 0.1.0 P0-P3 implementation snapshot

- Documents current Phase0-3 behavior: staged-only Hermes SkillOpt adapter, curated production seed eval packs, eval-only/benchmark fixed-skill scoring with `hermes-native-benchmark-report-v1`, deterministic trainer batch metadata, provenance v2, separated optimizer/target backends, soft/hard/mixed/strict gate modes, conservative resume checkpoints, history/lineage, slow_meta evidence, EnvAdapter benchmark/session foundation, benchmark bridge, transfer eval, conformance, and WebUI review surfaces.
- Records upstream hygiene: Microsoft SkillOpt remains a pinned external clone/lock; update commands clone/fetch/pin metadata only and do not merge plugin code or write live skills.
- Clarifies conformance semantics: strict validation improvement, bounded edit validation, train/val/test isolation, rejected edit buffers, production-only curated eval gates, and completed-run-only resume reuse.
- Adds docs/CLI/plugin metadata consistency tests so help text, plugin schemas, `plugin.yaml`, and `pyproject.toml` stay aligned.
- Clarifies remaining limitations: local Hermes-native reports and bundled safety/tool-use eval seeds do not claim Microsoft SkillOpt parity, external benchmark scores, real cross-model transfer, or universal production certification.


## Track B P0-P2

- Added read-only upstream pin comparison and benchmark/parity status surfaces.
- Added disabled-by-default live Hermes read-only target adapter with provenance fingerprints.
- Added JSON benchmark adapter v1 and eval-pack governance diagnostics.
- Added adopt/rollback lock and writeback audit events.
- Surfaced parity/gate/provenance/lineage in CLI/WebUI/report artifacts.
- Updated docs to distinguish Hermes-native benchmark mode from upstream parity; no upstream parity overclaim.
