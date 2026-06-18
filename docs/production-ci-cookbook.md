# Production and CI cookbook

This cookbook describes the current Phase0-Phase5 safe workflow for `hermes-skillopt`. It is intentionally conservative: no recipe auto-adopts, no recipe claims Microsoft SkillOpt upstream benchmark parity, and local sandbox/frozen-Hermes evidence is scope-labeled Hermes-native MVP evidence unless a future real adapter supplies stronger proof.

## Principles

- Treat `SKILL.md` as the only trainable state.
- Run `scout`, `doctor`, and eval-pack inventory before optimization.
- Use `optimize --intent smoke` for wiring only and `--intent review` for authoring loops.
- Use `optimize --intent production` only with an explicit curated production eval pack, strict gate, non-mock optimizer, and eligible validation/test splits.
- Review with `review latest --summary` or `review latest --digest` before any writeback. Summary/digest separate validation, production-best, and held-out-test gates; expose evidence class, blockers, score provenance, artifact refs, and next safe action.
- Adopt only with an exact typed confirmation (`ADOPT <run_id>`). Core adopt re-verifies hashed artifacts, production/test eligibility, provenance, proposed skill SHA, and current live skill SHA.
- Do not call local `benchmark`, `eval-only`, import, transfer, conformance, scorecard, or frozen-Hermes sandbox reports upstream parity or external benchmark performance.

## 1. Read-only readiness

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" doctor --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" scout --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" eval-pack-inventory --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" benchmark-parity-status
```

Expected interpretation:

- `doctor` and `scout` are read-only: no eval execution/full-run, no fetch, no adopt, no rollback, no write.
- `scout` is suitable for notifications. Its `cron_recommendation` is scout-only (`auto_adopt_from_cron: false`) and must not be expanded into scheduled optimize/adopt/rollback.
- Inventory should show a valid explicit curated pack with train/validation/test coverage, versioned pack id/version/fingerprint, and a production-eligible execution contract before production intent.
- Parity status should remain no-full-parity unless future code adds real upstream execution evidence.

## 2. Smoke check

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" optimize --intent smoke --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" review latest --summary
```

Use this for wiring, CLI/WebUI smoke, and staged artifact sanity. Smoke may use mock/review-only evidence and must not be considered adoption proof.

## 3. Review authoring loop

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" \
  optimize --intent review \
  --skill my-skill \
  --eval-file skillopt/evals/my-skill-review.json \
  --target-executor replay

python3 -m hermes_skillopt.cli --home "$HERMES_HOME" review latest --summary
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" review latest --digest
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" review latest --slim
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" fleet-report --skill my-skill
```

Review runs are staged-only. Static, sample, synthetic, session-mined, deterministic scorecard-only, and report-only replay evidence remains review-only even when a soft score improves.

`--digest` is the preferred slim notification body: it includes decision, adoptability, production/test gate flags, review-only status, score provenance, eval-pack identity, blockers/warnings, next safe action, and report/diff refs without large previews.

## 4. Production candidate run

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" \
  optimize --intent production \
  --skill my-skill \
  --eval-file skillopt/evals/my-skill-curated-production.json \
  --optimizer-backend hermes \
  --target-executor frozen-hermes \
  --gate-mode strict

python3 -m hermes_skillopt.cli --home "$HERMES_HOME" review latest --summary
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" review latest --digest
```

Production intent requirements enforced by code:

- explicit `--eval-file`
- no mock optimizer / `--allow-mock` false
- `--gate-mode strict`
- staged-only / no auto-adopt

Adoptability additionally requires eligible curated validation and held-out test tasks, no production hard-failed rows, complete required runtime evidence for adoption-eligible frozen-target contracts, verified artifacts, and provenance consistency. Missing runtime evidence downgrades production eligibility even if a scorecard or skill-text-only score improves.

If review/digest shows a held-out test score but also warns that `heldout_test_sensitivity` is missing, keep the score caveated and do not turn it into an external performance or upstream parity claim.

## 5. Explicit adopt

Only after the review summary says the run is ready and you intend live writeback:

```bash
RUN_ID="<reviewed-run-id>"
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" adopt "$RUN_ID" --confirm "ADOPT $RUN_ID"
```

Do not put this in ordinary CI. If a maintenance script uses the non-interactive confirmation flag, keep it behind a separate human-approved deployment gate and still treat core gate failures as hard blockers.

## 6. CI recipe

Quick CI smoke:

```bash
python3 -m compileall -q hermes_skillopt tests
python3 -m pytest -q tests/test_guided_ux.py tests/test_phase2_env_adapter.py tests/test_core.py::test_validation_gate_rejects_production_soft_gain_with_candidate_hard_failure tests/test_webui.py
python3 -m hermes_skillopt.cli scout --output skillopt/reports/scout.json
python3 -m hermes_skillopt.cli conformance --mode quick --output skillopt/reports/conformance-quick.json
python3 -m hermes_skillopt.cli artifact-hygiene-report --limit 200
```

Full local confidence:

```bash
python3 -m pytest -q
python3 -m hermes_skillopt.cli conformance --mode full --output skillopt/reports/conformance-full.json
```

CI evidence labels:

- `conformance --mode quick`: local smoke/regression subset, not full repo health.
- `conformance --mode full`: local adapter test run, not upstream benchmark parity.
- `scout`: read-only notification summary and safe next commands; no scheduled adoption.
- `artifact-hygiene-report`: read-only cleanup guidance; never deletes or resumes.
- `benchmark`/`eval-only`: fixed-skill local report only; always non-adoptable.

## 6a. Scheduled scout only

If you want scheduled monitoring, schedule only scout and route the JSON to your notifier:

```bash
hermes-skillopt --home "$HERMES_HOME" scout --output skillopt/reports/scout.json
```

Do not schedule `optimize`, `adopt`, `rollback`, `upstream-update`, or cleanup commands from scout output. Treat `next_actions` and `safe_next_commands` as human-review prompts.

## 7. WebUI workflow

```bash
python3 -m hermes_skillopt.cli webui --host 127.0.0.1 --port 7860
```

Use the WebUI scout/status views for read-only readiness, the guided wizard for staged smoke/review/production runs, and the review console/API for decision summaries, digests, and artifacts. Server-side APIs keep scout/review read-only, `run_full` staged-only with `auto_adopt=false`/`force=false`, typed adopt/rollback confirmations, and ignore the WebUI home override for writeback/upstream update paths. WebUI default `gate_mode: soft` is review-oriented; use production intent/strict curated evidence for production candidates.

## 8. Runtime evidence and no-overclaim checklist

Before reporting a production candidate, verify:

- `review --summary` has `production_gate_eligible: true`, `test_gate_eligible: true`, and no blockers.
- `review --summary`/`--digest` score provenance points to the intended target executor/backend, optimizer backend, eval pack id/version/fingerprint, and `score_source: production_curated_eval_pack`.
- Eval inventory readiness shows `hermes-skillopt-readiness-adoptability-v1` gates as production eligible for the pack; advisory package metadata (`references/`, `templates/`, `scripts/`, `assets/`) was reviewed if relevant but not treated as adoption authority.
- Frozen-Hermes evidence includes explicit class/scope, target config fingerprint, provider/model/toolset/session fingerprints, permissions with task commands disabled, isolated runtime evidence, transcript/trajectory, and execution-scoring evidence.
- `scorecard` or static skill-text-only evidence is not called true frozen Hermes execution.
- Task-provided commands are blocked by default and blocked-command rows are not production-gate eligible.
- Production hard-fails override soft score gains.
- Reports avoid claims of Microsoft upstream benchmark execution, upstream result equivalence, arbitrary live Hermes command execution, or external performance parity.
