# Release notes

## 0.1.0 P0-P3 implementation snapshot

- Documents current Phase0-3 behavior: staged-only Hermes SkillOpt adapter, eval-only fixed-skill scoring, provenance v2, separated optimizer/target backends, soft/hard/mixed/strict gate modes, conservative resume checkpoints, history/lineage, slow_meta evidence, EnvAdapter benchmark/session foundation, benchmark bridge, transfer eval, conformance, and WebUI review surfaces.
- Records upstream hygiene: Microsoft SkillOpt remains a pinned external clone/lock; update commands clone/fetch/pin metadata only and do not merge plugin code or write live skills.
- Clarifies conformance semantics: strict validation improvement, bounded edit validation, train/val/test isolation, rejected edit buffers, production-only curated eval gates, and completed-run-only resume reuse.
- Adds docs/CLI/plugin metadata consistency tests so help text, plugin schemas, `plugin.yaml`, and `pyproject.toml` stay aligned.
