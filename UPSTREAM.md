# UPSTREAM

`hermes-skillopt` is a standalone Hermes plugin repository. It tracks Microsoft SkillOpt as a pinned external upstream reference, not as vendored production code and not as a fork that is auto-merged into the plugin.

## Current relationship to Microsoft SkillOpt

- Local plugin: Hermes toolset, CLI, WebUI, staged artifacts, bounded edits, profile-safe adopt/rollback, tests.
- Upstream reference: `https://github.com/microsoft/SkillOpt.git`, cloned/fetched under `$HERMES_HOME/skillopt/upstream/SkillOpt` when requested.
- Lock file: `skillopt_upstream.lock` records the upstream URL, clone path, pinned commit, and update time.
- Current lock pin: `86bad36ffe511b7022a6c735930056c14124b960`.

The adapter implements SkillOpt-inspired concepts in Hermes terms:

- `SKILL.md` is the trainable state.
- `SixStageSkillOptTrainer` performs rollout → reflect → aggregate → select → update → evaluate/gate.
- `TargetExecutor` runs frozen replay/sandbox/scorecard evaluation.
- `OptimizerBackend` emits bounded edits only.
- `HermesSkillEnv` builds curated/session/fallback train/validation/test tasks.
- Hermes safety remains outside the trainer: staged-only artifacts, explicit review/adopt/rollback, artifact hashes, path/SHA guards, active-profile isolation.

This is still not Microsoft’s official trainer package. Upstream changes must be reviewed and ported deliberately in small Hermes-safe changes.

## Upstream status and update commands

```bash
python3 -m hermes_skillopt.cli upstream-status
python3 -m hermes_skillopt.cli upstream-update --fetch-only
python3 -m hermes_skillopt.cli upstream-update
bash scripts/update_upstream.sh
```

Semantics:

- `upstream-status` is local/status-only. It reads the canonical clone and lock, reports pin/dirty/ahead/behind/diverged style status when possible, and does not fetch from the network.
- `upstream-update --fetch-only` refreshes the canonical clone without adopting upstream code.
- `upstream-update` can refresh clone/lock metadata. It does not merge files into this plugin and does not adopt any Hermes skill.
- The production tool/CLI/WebUI upstream surface does not accept arbitrary repo paths; it uses the canonical `$HERMES_HOME/skillopt/upstream/SkillOpt` location.

## Curated eval and gate alignment

The current adapter’s production adoption gates depend on local curated eval files, not on upstream code:

- Explicit JSON/JSONL eval files under `$HERMES_HOME` can provide production validation and test tasks.
- Validation adoption evidence requires eligible curated validation tasks and strict candidate improvement.
- Held-out test eligibility requires eligible curated test tasks passing threshold.
- Fallback, synthetic, session-mined, and legacy dry-run evidence remains review-only.
- Sandbox eval is isolated and blocks task-provided commands by default.

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
