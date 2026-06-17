# hermes-skillopt

`hermes-skillopt` is a standalone Hermes plugin for safe, staged optimization of a Hermes profile `SKILL.md`. It is **not** a fork or full port of Microsoft SkillOpt; it is a Hermes-native adapter that keeps Hermes core unchanged and keeps all candidate changes reviewable and rollbackable.

## What it is

- **Trainable state:** one Hermes skill document (`$HERMES_HOME/skills/.../SKILL.md`).
- **Frozen target executor:** evaluates the current skill and each candidate under the same replay/scorecard/sandbox target config.
- **Optimizer:** reflects on rollout evidence and proposes bounded skill edits only. It does not write the live profile.
- **Environment/benchmark:** builds train/validation/test tasks from curated eval files, session-mined snippets, and fallback synthetic tasks.
- **Gate:** validation must strictly improve, and production adoption additionally requires explicit curated validation and held-out test eligibility.
- **Safety shell:** full runs write only staged artifacts; live writes require explicit `adopt`; `rollback` restores from guarded backups.

## What it is not

- Not Microsoft’s official SkillOpt trainer/package.
- Not an arbitrary command runner. Sandbox eval blocks task-provided commands by default.
- Not an auto-adopter. Production tool/CLI/WebUI flows do not auto-adopt.
- Not a way to production-adopt fallback, synthetic, session-mined, or legacy dry-run proposals.
- Not a benchmark-results claim. Benchmark bridge and transfer eval produce local deterministic artifacts/reports; they do not prove parity with Microsoft SkillOpt benchmarks or external model performance.
- `full-run --dry-run` does not exist; use `dry-run`/`run --mode legacy` only for review-only legacy proposals.

## Install

From this repo:

```bash
cd /Users/fanxuxin/Hermes_Sync/Default/hermes-skillopt
bash scripts/install_local.sh
```

This creates a symlink at `${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-skillopt`. Restart/reload any already-running Hermes session if it does not pick up the plugin.

Optional editable Python install for CLI/WebUI development:

```bash
python3 -m pip install -e '.[dev]'
python3 -m pip install -e '.[webui]'
```

## Tools

Toolset: `hermes_skillopt`

- `hermes_skillopt_status`: profile, skill count, recent staged runs.
- `hermes_skillopt_run`: defaults to `mode="full"`; `mode="legacy"` calls the legacy review-only dry-run path.
- `hermes_skillopt_full_run`: executes the current six-stage SkillOpt-inspired lifecycle.
- `hermes_skillopt_resume_inspect`: read-only checkpoint/stage fingerprint inspection; completed-run reuse only, no partial replay.
- `hermes_skillopt_dry_run`: legacy staged proposal; review-only.
- `hermes_skillopt_review`: verifies artifact hashes and returns gate/adoptability status, report/diff paths, and previews.
- `hermes_skillopt_adopt`: explicit live writeback after all guards pass; no `hermes_home` override in the tool schema.
- `hermes_skillopt_rollback`: explicit restore from verified backup manifest/SKILL.md; no `hermes_home` override in the tool schema.
- `hermes_skillopt_upstream_status`: local pinned upstream clone/lock status; no network fetch.
- `hermes_skillopt_upstream_update`: clone/fetch/pin Microsoft SkillOpt upstream metadata only; does not merge plugin code.
- `hermes_skillopt_handoff_optimize`: deterministic multi-agent `delegate_task` dispatcher→worker handoff package optimizer; staged output only.

P3 import/transfer/conformance surfaces are CLI/module commands, not Hermes plugin tools in `plugin.yaml`: `import-upstream-benchmark`, `transfer-eval`, and `conformance`.

Important full-run parameters:

- `skill`, `query`, `eval_file`, `lookback_days`, `limit`, `iterations`, `edit_budget`, `candidate_count`
- `backend`: `auto|hermes|mock` back-compat alias for the optimizer backend
- `optimizer_backend`: `auto|hermes|mock`; controls reflection/bounded edit proposal generation
- `allow_mock`: required before `backend=auto` may fall back to mock outside Hermes.
- `target_executor` / `target_backend`: `auto|replay|sandbox|scorecard`; controls the frozen evaluator, separate from the optimizer backend
- `gate_mode`: `soft|hard|mixed|strict`; deterministic metric policy, with LLM/judge text kept explanation-only
- `resume_run_id`: opt-in reuse of a completed checkpointed run only when the stored input/config/provenance fingerprint matches
- `force`: only affects adopt/rollback current-sha guard behavior where exposed; it does not bypass artifact, profile, validation, production, or test gates.

CLI help confirms the supported surface:

```bash
python3 -m hermes_skillopt.cli --help
python3 -m hermes_skillopt.cli full-run --help
python3 -m hermes_skillopt.cli resume-inspect --help
```

## Full-run lifecycle

`full_run()` uses `SixStageSkillOptTrainer` and writes a complete run directory under `$HERMES_HOME/skillopt/staging/<run-id>/`:

1. **Rollout:** evaluate the current skill with a frozen target executor.
2. **Reflect:** build optimizer reflections from train/eval evidence and rejected-edit history.
3. **Aggregate:** turn reflections into one or more bounded edit proposals (`candidate_count`, default 1).
4. **Select:** validate bounded edits, evaluate each candidate on the same validation set, rank strict improvements, select the best improvement, and buffer rejected/non-selected candidates.
5. **Update:** apply the selected bounded edit to a candidate copy only.
6. **Evaluate/gate:** keep the best strict improvement, then evaluate final best on held-out test.

Core artifacts include:

- `manifest.json`, `report.md`, `diff.patch`
- `original_SKILL.md`, `current_SKILL.md`, `proposed_SKILL.md`, and `best_skill.md` only when a best candidate exists
- `evidence.json`, `train_items.jsonl`, `val_items.jsonl`, `test_items.jsonl`
- `current_validation_results.json`, `candidate_validation_results.json`, `test_results.json`
- `reflections.json`, `candidate_edits.json`, `candidate_summary.json`, `rejected_edits.jsonl`, `gate_results.json`, `slow_meta.json`
- `target_binding.json`, `provenance_binding.json`, `history.json` for target/profile binding, optimizer/target/gate provenance, and candidate lineage/explainability
- `checkpoint.json` with `skillopt-checkpoint-v1` input fingerprint; resume currently reuses only completed runs and refuses partial-stage replay
- `stages/NNN_rollout|reflect|aggregate|select|update|evaluate.json`

`manifest.json` records SHA-256 hashes for staged artifacts plus `skillopt-provenance-v2`: plugin repo/commit, upstream lock, eval/task fingerprint, optimizer_backend/target_backend configs, gate policy, profile/skill fingerprints, and production eval policy fingerprint. `review`, `adopt`, and `rollback` re-check artifact integrity before trusting the run. At adopt time, SkillOpt also reloads the verified `gate_results.json`, `test_results.json`, `val_items.jsonl`, `test_items.jsonl`, `candidate_summary.json`, `evidence.json`, and `proposed_SKILL.md` artifacts and independently re-derives production/test eligibility, production eval policy, and provenance fingerprint; manifest-only edits cannot make a review-only or non-production run adoptable.

`history.json` is an audit artifact, not a training database. It records each ranked candidate's parent skill hash, selected/accepted/rejected status, gate summaries, production-gate summaries, and rejection reasons so later reflection can inspect lineage without silently applying rejected edits.

Read-only resume inspection is available with:

```bash
python3 -m hermes_skillopt.cli resume-inspect RUN_ID
```

It verifies checkpoint/stage fingerprints and manifest hashes. It reports whether a completed run is safe to reuse; incomplete checkpoints are refused rather than replayed from the middle of the six-stage lifecycle.

## Eval schema and production eligibility

Curated evals may be JSONL, JSON (`[...]` or `{ "tasks": [...] }`), or a versioned eval pack (`{ "schema_version": "hermes-curated-eval-pack-v1", "pack_id": "...", "version": "...", "tasks": [...] }`). An explicit `eval_file` must resolve to a regular file inside the active `$HERMES_HOME`; default discovery checks `$HERMES_HOME/skillopt/evals/<skill-name>.jsonl` and then `evals/*.jsonl` under the skill directory.

A sample Hermes-native pack template is bundled at `hermes_skillopt/eval_packs/hermes_native_core_v1.json` and covers tool-use correctness, delegation/handoff, file editing safety, research grounding/no fabrication, profile isolation, and adopt/rollback safety. Bundled/sample packs are review-only; copy a reviewed pack under `$HERMES_HOME/skillopt/evals/` and remove `sample_pack`/set curated task metadata intentionally before using it as production-gate input.

Minimal task:

```json
{"id":"v1","split":"validation","prompt":"held-out validation replay","expected_keywords":["verify","blocker"],"forbidden_keywords":["fabricate"],"weight":2}
```

Supported fields include:

- Required: `prompt`; optional `id`.
- Splits: `train`, `validation`/`val`, `test`.
- Scoring/assertion fields: `expected_keywords`/`expected_terms`, `forbidden_keywords`/`failure_terms`, `assertions`, `required_markers`, `forbidden_markers`, `success_criteria`, `expected_behavior`, optional `ground_truth_score` metadata.
- Metadata/execution fields: `judge`, `allowed_tools`, `timeout`, `fixtures`, `weight`, `executor`, `production_gate_eligible`/`production_gate` (set false to opt out of production adopt even when the scorecard is explicit).

Production eval schema policy (`production-eval-schema-v1`) is recorded into `manifest.json` and `report.md` with eval pack id/version/fingerprint, split governance, and a provenance fingerprint over eval file SHA, eval pack identity, task fingerprint, backend, target executor, and target config. `checkpoint.json` also records eval pack identity for completed-run resume matching. `review` returns that fingerprint plus per-task validation deltas.

Production adoption gates are intentionally narrow:

- Only explicit curated eval-file tasks can satisfy production gates.
- Production validation requires eligible curated validation tasks and strict candidate improvement.
- Production test eligibility requires held-out curated test results passing threshold.
- When multiple candidates are evaluated and production gate tasks exist, selection prefers a candidate with both generic validation strict improvement and production validation strict improvement; generic-only improvements remain staged/reviewable but are not allowed to crowd out an adoptable production candidate.
- Fallback, synthetic, session-mined, and legacy dry-run evidence is review-only and cannot be production-adopted.
- LLM/judge text is evidence only; it cannot override validation/test gates.

## Target executors and sandbox safety

`--target-executor` supports:

- `auto`: chooses sandbox when tasks request `metadata.executor == "sandbox"` or `judge == "hermes_sandbox"`; otherwise replay.
- `replay`: declarative Hermes replay/assertion runner.
- `sandbox`: production-safe sandbox executor MVP.
- `scorecard`: deterministic keyword scorecard.

Sandbox mode creates a temporary isolated HOME/HERMES_HOME/workspace, writes `SKILL.md` inside that sandbox, runs a fixed internal runner, captures transcript/exit/timeout, and does not write the live profile. Task-provided commands in `fixtures.command` or `metadata.command` are blocked with `SANDBOX_COMMAND_BLOCKED` and are not production-gate eligible. Do not document or rely on sandbox as an arbitrary shell executor.

## EnvAdapter, benchmarks, sessions, and sleep foundation

`EnvAdapter` is the narrow Hermes-native contract for loaders, rollout metadata, scorer metadata, and production eligibility policy. `HermesEnvAdapter` wraps `HermesSkillEnv` and records split policy metadata (`hermes-skillopt-train-val-test-v1`). The built-in benchmarks (`delegation-handoff`, `tool-use-replay`, `skill-authoring-review`) provide train/val/test scaffolding but are marked non-production. Session-mined and fallback/synthetic tasks remain useful review evidence and future sleep/data-mining foundation; they are intentionally isolated from production adoption gates unless replaced by explicit curated eval-file tasks.

## Benchmark bridge, transfer eval, and conformance

P3 adds deterministic local CLI/module utilities around the staged artifact contract:

```bash
python3 -m hermes_skillopt.cli import-upstream-benchmark MANIFEST.json --output PACK.json
python3 -m hermes_skillopt.cli transfer-eval --run-id RUN_ID --target scorecard --target replay --output transfer.json
python3 -m hermes_skillopt.cli conformance --output conformance.json
```

- `import-upstream-benchmark` converts common upstream-style JSON manifests with embedded `tasks` or `splits` into a Hermes `hermes-curated-eval-pack-v1` JSON pack. It rejects executable/remote fields such as commands, code, entrypoints, modules, URLs, containers, and images. Imported packs are sample/review-only unless `--curated` is explicitly supplied and individual tasks still satisfy the production eval policy.
- `transfer-eval` evaluates a staged run's proposed skill (or, with `--allow-live-skill-file`, an explicit skill file) across selected deterministic target executors and optional profile homes. It is read-only/report-only and records target/profile fingerprints; it does not adopt or mutate skills.
- `conformance` runs local deterministic checks (`compileall` and pytest args) and writes a `hermes-skillopt-conformance-v1` JSON report. It does not contact upstream, run external benchmark code, or require live Hermes services.

These utilities are useful for local regression evidence. They do not claim Microsoft benchmark parity, real cross-model transfer, or production performance improvements unless you supply and verify those evals yourself.

## Adopt and rollback

`adopt(run_id)` writes the live skill only when all of these are true:

- run manifest status is `staged_best` and `adoptable == true`
- validation gate accepted
- `production_gate_eligible == true`
- `test_gate_eligible == true`
- staged artifact hashes verify, and adopt-time cross-checks re-derive gate/test eligibility, production policy, provenance, candidate summary, and proposed skill SHA from those hashed artifacts
- target path resolves under the active profile `skills/`
- current live skill SHA matches the staged original SHA unless an explicit force path is used
- staged proposed skill SHA matches the manifest

Adopt creates `$HERMES_HOME/skillopt/backups/<timestamp-run-id>/` containing the previous `SKILL.md` and backup manifest. `rollback(run_id)` restores only from that verified backup and checks run id, target path, relative path, original/adopted/proposed SHA, and current live SHA unless forced.

Production tool/WebUI live writeback uses the active profile. CLI cross-profile writeback requires explicit offline-maintenance flags and remains guarded.

## WebUI

The optional Gradio WebUI is Hermes-specific:

```bash
python3 -m pip install -e '.[webui]'
python3 -m hermes_skillopt.webui --host 127.0.0.1 --port 7860
# or
python3 -m hermes_skillopt.cli webui --host 127.0.0.1 --port 7860
```

Tabs/actions: status, full run, review artifacts, adopt, rollback, upstream. Artifact review reads only fixed files in the selected staging directory. Adopt/rollback require typed confirmation and still call the core guards. If Gradio is missing, plugin import and tests still work.

The WebUI is an observability/review surface, not an auto-adopter: it displays report/diff/gate/candidate/rejected artifacts from staging and delegates all live-write decisions to the same guarded core functions used by the CLI/plugin tools.

## Upstream tracking

Microsoft SkillOpt is tracked as a pinned external upstream clone/lock, not vendored into this plugin. See `UPSTREAM.md` and `skillopt_upstream.lock`.

```bash
python3 -m hermes_skillopt.cli upstream-status
python3 -m hermes_skillopt.cli upstream-update --fetch-only
bash scripts/update_upstream.sh
```

Updating upstream refreshes clone/lock metadata only. It does not merge upstream code, change plugin behavior, or adopt any skill.

## Multi-agent handoff optimizer

`handoff-optimize` builds a deterministic dispatcher→worker handoff package for Hermes `delegate_task` workflows:

- normalized goal/scope/acceptance/verification context
- worker output contract (`status`, changed files, evidence, risks, next step)
- reviewer rubric and retry/escalation rules
- metrics such as context size, acceptance omissions, and rework risk

It is staged-only and does not rewrite global prompts or skills.

## Testing

```bash
python3 -m pytest -q
python3 -m compileall -q hermes_skillopt tests
python3 -m hermes_skillopt.cli full-run --help
python3 -m hermes_skillopt.cli handoff-optimize --help
```
