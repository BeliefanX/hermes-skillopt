# UPSTREAM

`hermes-skillopt` is a standalone Hermes plugin repository. It tracks Microsoft SkillOpt as a pinned external upstream reference, not as vendored production code and not as a fork that is auto-merged into the plugin.

## Current relationship to Microsoft SkillOpt

- Local plugin: Hermes toolset, CLI, WebUI, staged artifacts, bounded edits, profile-safe adopt/rollback, tests.
- Upstream reference: `https://github.com/microsoft/SkillOpt.git`, cloned/fetched under `$HERMES_HOME/skillopt/upstream/SkillOpt` when requested.
- Lock file: `skillopt_upstream.lock` records the upstream URL, clone path, pinned commit, last-reviewed upstream commit, and update time.
- Current lock pin: `86bad36ffe511b7022a6c735930056c14124b960`.
- Last-reviewed upstream commit: `86bad36ffe511b7022a6c735930056c14124b960` (reviewed against the Hermes seam matrix below; pin updates do not merge code automatically).

The adapter implements SkillOpt-inspired concepts in Hermes terms:

- `SKILL.md` is the trainable state.
- `SixStageSkillOptTrainer` performs rollout → reflect → aggregate → select → update → evaluate/gate.
- `TargetExecutor` runs frozen replay/sandbox/scorecard evaluation; `frozen-hermes` / `frozen_hermes_target_execution_v1` currently routes to the constrained sandbox MVP with isolated runtime/provider/model/toolset/session evidence.
- `OptimizerBackend` emits bounded edits only.
- `HermesSkillEnv` builds curated/session/fallback train/validation/test tasks, with bundled static review seed packs under `examples/evals/` for review/training evidence only.
- Hermes safety remains outside the trainer: staged-only artifacts, read-only scout/review/digest surfaces, explicit review/adopt/rollback, artifact hashes, path/SHA guards, active-profile isolation.

This is still not Microsoft’s official trainer package. Upstream changes must be reviewed and ported deliberately in small Hermes-safe changes.

## Upstream status and update commands

```bash
python3 -m hermes_skillopt.cli upstream-status
python3 -m hermes_skillopt.cli compare-upstream-pin
python3 -m hermes_skillopt.cli benchmark-parity-status
python3 -m hermes_skillopt.cli upstream-update --fetch-only
python3 -m hermes_skillopt.cli upstream-update
bash scripts/update_upstream.sh
```

Semantics:

- `upstream-status` is local/status-only. It reads the canonical clone and lock, reports pin/dirty/ahead/behind/diverged style status when possible, and does not fetch from the network.
- `compare-upstream-pin` is also read-only/no-fetch and compares the canonical local clone against `skillopt_upstream.lock` and already-fetched refs.
- `benchmark-parity-status` is a label/report surface for adapter levels and Hermes-native benchmark coverage. It does not run rollouts, fetch, import upstream Python, execute benchmark commands, or write skills.
- `upstream-update --fetch-only` refreshes the canonical clone without adopting upstream code.
- `upstream-update` can refresh clone/lock metadata. It does not merge files into this plugin and does not adopt any Hermes skill.
- The production tool/CLI/WebUI upstream surface does not accept arbitrary repo paths; it uses the canonical `$HERMES_HOME/skillopt/upstream/SkillOpt` location.

## Upstream seam matrix / delta checklist

`upstream-status` returns this matrix under `feature_matrix`/`delta_checklist`; `skillopt_upstream.lock` also records the current Phase0-Phase5 lock-time seams (`benchmark_bridge`, `transfer_eval`, `conformance`, `webui_writeback`, guided UX, artifact hygiene). Keep both in sync when porting upstream ideas.

- `trainer_loop`: upstream rollout/reflection/update/evaluate loop is adapted as `SixStageSkillOptTrainer` rollout → reflect → aggregate → select → update → evaluate with six stage artifacts and staged-only writes.
- `reflection_prompts`: upstream reflection prompting is adapted as redacted `OptimizerBackend.reflect` prompts with rejected-history context and prompt SHA-256 provenance.
- `skill_aware_reflection`: upstream skill-aware analysis is adapted as deterministic `skill_defect` vs `execution_lapse` labels over Hermes `EvalTask` evidence.
- `aggregate_clip`: upstream edit aggregation is adapted as `aggregate_edit_proposals` merge/dedupe/rejected-filter/edit-budget clipping.
- `gate`: upstream candidate validation is adapted as deterministic `soft|hard|mixed|strict` metric gates; LLM judge text is explanation-only and hard/mixed gates include per-task regression metadata.
- `artifact_resume`: upstream artifacts/checkpoints are adapted as manifest/checkpoint/artifact hashes with completed-run-only resume and profile/path/SHA guards.
- `benchmarks_tests`: upstream benchmark ideas are adapted as Hermes eval packs plus explicit curated validation/test policy; current bundled `examples/evals/` packs are static review fixtures and cannot authorize adoption.
- `benchmark_report`: CLI `benchmark` aliases eval-only and writes a reproducible local `hermes-native-benchmark-report-v1` with skill/eval/target fingerprints and read-only safety flags.
- `benchmark_bridge`: upstream-style benchmark manifests are accepted only as inert JSON data and converted to Hermes eval packs; executable/remote benchmark fields are rejected.
- `transfer_eval`: cross-target/profile checks are read-only deterministic transfer reports over staged/proposed skill text with readiness/skill-type/evidence-contract summaries, not live cross-model training, upstream parity, or writeback.
- `fleet_ux`: fleet/rollback reports group local Hermes runs by skill/type/readiness/adoptability/rollbackability and expose per-run rollback guard status; they are reporting surfaces, not upstream orchestration parity.
- `conformance`: local compile/pytest reports define this adapter's regression contract without requiring upstream checkout, external services, or network access. It returns JSON by default and writes a file only when an explicit guarded `--output` path is supplied.
- `safe_outputs`: import/transfer/conformance report writers share a safe output path guard that blocks live skills/plugins/config/memory/cron/runtime paths, plugin/repo source paths, non-regular outputs, wrong suffixes, and symlink escapes.
- `guided_review_ux`: `scout`, `doctor`, `optimize --intent`, `review latest --summary`, `review --digest`, WebUI scout/wizard/review console, and CLI/WebUI typed adopt confirmation are Hermes safety surfaces, not upstream parity claims.
- `readiness_review_schema`: inventory/review/WebUI/scout use `hermes-skillopt-readiness-adoptability-v1`, `hermes-skillopt-readiness-matrix-v1`, score provenance, and slim artifact refs to separate review-only acceptance from production/test adoptability.
- `skill_package_awareness`: local Hermes skills may have `references/`, `templates/`, `scripts/`, and `assets/` summarized as advisory path/hash/count metadata only; upstream parity and adoption authority are unchanged.
- `artifact_hygiene`: staging hygiene reports classify local artifact state for cleanup planning only; they do not delete, resume, adopt, or rollback.

## Upstream benchmark adapter levels

`benchmark-parity-status` and imported pack provenance use explicit adapter levels rather than a single parity claim:

- `json_import_only` — supported today. Converts a local upstream-style JSON manifest with embedded `tasks`/`splits` into a Hermes eval pack. It is data-only, rejects executable/remote fields, and claims no upstream execution parity.
- `pinned_manifest_replay` — supported today for JSON manifests under the canonical pinned upstream clone only. It records pinned commit/manifest/conversion provenance, but still performs data conversion only; no upstream code or benchmark runner is executed.
- `pinned_upstream_execution` — unsupported/future. This would require a pinned, bounded, no-live-write execution adapter with evidence equivalent to the Hermes frozen-target contract.
- `parity_evidence_complete` — unsupported/future. This would require comparable pinned upstream execution evidence plus mapped Hermes eval evidence; it is not available on this branch.

Consequently, `benchmark`, `eval-only`, `import-upstream-benchmark`, transfer eval, and sandbox-backed `frozen-hermes` results are local Hermes evidence. They must not be described as Microsoft SkillOpt upstream benchmark parity or external performance results.

Supported local parity levels are intentionally limited to Hermes-native eval-pack replay plus JSON/pinned-manifest conversion evidence. Unsupported levels remain `pinned_upstream_execution`, `parity_evidence_complete`, arbitrary live Hermes command execution, and any claim that local transfer/fleet reports prove Microsoft SkillOpt benchmark behavior.

## Curated eval and gate alignment

The current adapter’s production adoption gates depend on local curated eval files, not on upstream code:

- Explicit JSON/JSONL eval files under `$HERMES_HOME` can provide production validation and test tasks only when they are `hermes-curated-eval-pack-v1`, opt in through production policy, are not sample/static/keyword packs, and declare an adoption-eligible eval execution contract.
- Eval inventory recognizes versioned pack metadata (`pack_id`, `version`, fingerprint) and conservative name-derived versioned pack files, then reports the unified readiness/adoptability schema; inventory readiness is discovery evidence only and does not authorize adoption by itself.
- Validation adoption evidence requires eligible curated validation tasks and strict candidate improvement.
- Held-out test eligibility requires eligible curated test tasks passing threshold.
- Fallback, synthetic, session-mined, sample/static keyword packs, report-only replay contracts, and legacy dry-run evidence remains review-only.
- Sandbox eval is isolated and blocks task-provided commands by default.
- Frozen-Hermes target execution is currently an MVP alias for the sandbox runner. It can provide isolated runtime/transcript/scoring evidence for Hermes-native evals, but it is not Microsoft upstream benchmark execution, not parity certification, and not arbitrary live agent command execution.
- Bundled seed packs `examples/evals/hermes_tool_use_production_v1.json` and `examples/evals/hermes_skill_safety_production_v1.json` are currently static review packs (`sample_pack: true`, `allow_production_adoption: false`, `production_gate_eligible: false`) despite historical filenames; they cannot satisfy production/adoption gates.

## Conformance semantics

Hermes conformance is defined by local tests and the staged artifact contract, not by blindly matching upstream implementation details:

- **Strict validation improvement:** generic validation adoption requires deterministic metric improvement. `strict` is the default for adoption-capable runs and requires soft improvement plus hard weighted pass-rate/per-task non-regression unless `hard_regression_allowed` is explicitly set. `soft`, `hard`, and `mixed` are explicit review/non-production choices; production hard-fail rows, test gates, and evidence-contract gates always take precedence. LLM/judge text is evidence only.
- **Bounded edits:** optimizers may emit only bounded `append`/`replace`/`delete`/`insert_after` edits validated against the current skill text. Rejected and non-selected edits are preserved in `rejected_edits.jsonl` for reflection/history, not silently applied.
- **Protected skill regions:** bounded edit validation rejects mutations of protected headings/markers and allowed-region marker boundaries; new inserted/replaced text is checked, not only the old target span.
- **Train/val/test isolation:** train evidence informs reflection, validation selects candidates, and held-out test evidence is evaluated after selection. Only explicit curated validation/test tasks can make production adoption eligible.
- **Rejected buffer:** invalid, non-improving, or non-selected candidates remain staged in summaries/rejected buffers for audit and later reflection; they cannot become live writes without a new passing run.
- **Resume semantics:** `checkpoint.json` records a `skillopt-checkpoint-v1` input hash over profile, skill SHA, eval SHA, backend, gate, and budget settings. `resume_run_id` reuses only completed runs after artifact verification and fingerprint match; safe partial-stage replay is intentionally unavailable.
- **Provenance v2:** manifests record `skillopt-provenance-v2` with plugin repo/commit, upstream lock, eval/task SHA, optimizer_backend config, target_backend config, gate policy, profile, skill, and production eval policy fingerprints. Stage artifacts also record deterministic batch metadata (`skillopt-deterministic-batch-v1`) with stable batch IDs, seed `0`, stable-order note, and input fingerprints.

## Intentional divergence from upstream

- Standalone Hermes plugin, not a fork/vendor drop and not Microsoft’s official SkillOpt package.
- `SKILL.md` is the only trainable state; Hermes core prompts, plugin code, and upstream clones are not rewritten by optimization runs.
- Optimizer backend and target backend are separated so candidate generation cannot alter the frozen evaluator.
- Production adoption is narrower than generic optimization: explicit curated validation plus held-out curated test gates are required, and static/keyword/sample/report-only packs cannot authorize adoption.
- Sandbox/frozen-Hermes support is a constrained Hermes review/eval MVP that blocks task-provided commands; it is not an arbitrary command executor or real upstream benchmark runner.
- Upstream update commands clone/fetch/pin metadata only and do not merge code, write skills, or auto-port changes.
- `benchmark`/`eval-only` reports and benchmark bridge imports do not execute upstream benchmark code or assert external benchmark parity; safe `json_import_only` and data-only `pinned_manifest_replay` conversion are supported, while `pinned_upstream_execution` and `parity_evidence_complete` remain unsupported. Transfer eval does not create real cross-model claims. Reports distinguish production-curated scores from review-only scores and surface per-task deltas/ledger evidence rather than collapsing all evidence into a single benchmark claim.
- `scout` and `review --digest` are notification/review conveniences only: they surface safe next actions, score provenance, gate separation, and artifact refs, but never fetch upstream, run optimizers, adopt, rollback, schedule cron, or claim parity. Scheduled automation should be limited to read-only `scout`, `doctor`, `eval-pack-inventory`, and `review --digest` surfaces.

## Upstream diff/status workflow

1. Run `python3 -m hermes_skillopt.cli upstream-status` to inspect the current local lock/clone without network fetch.
2. Run `python3 -m hermes_skillopt.cli upstream-update --fetch-only` (or `bash scripts/update_upstream.sh`) when you explicitly want to refresh the canonical upstream clone/lock.
3. Review upstream changes outside live profiles, choose a small Hermes-safe idea to port, and implement it in local modules with tests.
4. Re-run conformance tests. Do not auto-merge upstream files into this plugin and do not treat an upstream pin change as a behavior change by itself.

## Safe porting policy

When bringing ideas from upstream:

1. Update/fetch the pinned upstream clone and inspect the diff.
2. Port only a small, reviewed algorithm/eval idea into local Hermes modules.
3. Preserve staged-only behavior, active-profile isolation, bounded edit validation, artifact hashes, and explicit adopt/rollback gates.
4. Add or update tests using temporary `HERMES_HOME` fixtures.
5. Run:

```bash
python3 -m pytest -q
python3 -m compileall -q hermes_skillopt tests
```

Do not replace the Hermes safety shell with upstream training paths, and do not let upstream update commands mutate plugin code or live skills.


## Current upstream pin/parity policy

`compare-upstream-pin` compares the local canonical clone with `skillopt_upstream.lock` and locally fetched `origin/main` only. It does not fetch. `upstream-update --fetch-only` is the explicit refresh path. No upstream Microsoft SkillOpt code is vendored or blindly merged into this Hermes-native adapter. `benchmark-parity-status` deliberately reports **no full upstream parity claim**: `json_import_only` and `pinned_manifest_replay` adapter levels are supported, while `pinned_upstream_execution` and `parity_evidence_complete` remain unsupported. The local sandbox-backed frozen-Hermes MVP provides Hermes evidence only and does not certify Microsoft SkillOpt benchmark parity.
