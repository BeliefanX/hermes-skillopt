# Production and CI cookbook

This cookbook describes the current Phase0-Phase5 safe workflow for `hermes-skillopt`. It is intentionally conservative: no recipe auto-adopts, no recipe claims Microsoft SkillOpt upstream benchmark parity, and current sandbox/frozen-Hermes evidence is review-only/non-production unless a future real Hermes runtime adapter supplies the required proof.

## Principles

- Treat `SKILL.md` as the only trainable state.
- Run `scout`, `doctor`, and eval-pack inventory before optimization.
- Use `optimize --intent smoke` for wiring only and `--intent review` for authoring loops.
- Use `optimize --intent production` only with an explicit curated production eval pack, strict gate, non-mock optimizer, and eligible validation/test splits.
- Review with `review latest --summary` or `review latest --digest` before any writeback. Summary/digest separate validation, production-best, and held-out-test gates; expose evidence class, blockers, score provenance, artifact refs, and next safe action.
- Adopt only with an exact typed confirmation (`ADOPT <run_id>`). Core adopt re-verifies hashed artifacts, production/test eligibility, complete frozen runtime evidence, reviewer gate, provenance hashes, proposed skill SHA, current live skill SHA, and the native Hermes conflict/fingerprint guard.
- Do not call local `benchmark`, `eval-only`, import, transfer, conformance, scorecard, static/replay/sandbox/live-disabled, or frozen-Hermes sandbox/fixed-runner reports upstream parity, external benchmark performance, or production adoption proof.
- Do not treat SkillOpt as a curator replacement: Hermes curator owns lifecycle/archive/consolidation, while SkillOpt reads native metadata sidecars best-effort and owns staged eval evidence/adoption recommendations only.

## 1. Read-only readiness

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" doctor --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" scout --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" eval-pack-inventory --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" eval-pack-workflow --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" skill-readiness-queue --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" skill-quality --skill my-skill --digest
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" benchmark-parity-status
```

Expected interpretation:

- `doctor` and `scout` are read-only: no eval execution/full-run, no fetch, no adopt, no rollback, no write.
- `scout` is suitable for notifications. Without `--output`, it returns JSON and writes no report. Its `cron_recommendation` is scout-only (`auto_adopt_from_cron: false`) and must not be expanded into scheduled optimize/adopt/rollback.
- Inventory should show a valid explicit curated pack with train/validation/test coverage, versioned pack id/version/fingerprint, and a production-eligible execution contract before production intent. `eval-pack-workflow` and `skill-readiness-queue` are authoring/readiness summaries only: they rank gaps and safe next commands, but do not create production evidence. `skill-quality --digest` is a read-only/lint digest; an eval skeleton from `--create-eval-skeleton` is review-only and not production evidence. Native metadata shown by status/scout/review is diagnostic guard input; hub/bundled/pinned/archived/curator-managed skills remain blocked for adopt by default.
- Parity status should remain no-full-parity unless future code adds real upstream execution evidence.

## 2. Smoke check

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" optimize --intent smoke --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" review latest --summary
```

Use this for wiring, CLI/WebUI smoke, and staged artifact sanity. Smoke may use mock/review-only evidence and must not be considered adoption proof.

## 2a. Eval-pack authoring autopilot and seeds

Use eval-pack doctor/autopilot to plan coverage before writing anything:

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" eval-pack-doctor --skill my-skill
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" eval-pack-autopilot --skill my-skill
```

Default autopilot is plan/read-only (`mode: eval_pack_autopilot_plan_read_only`) and returns a plan plus doctor diagnostics. It is useful for humans, but the scheduled/default notification set should stay to the four cron-safe digest surfaces (`scout --digest`, `doctor --digest`, `eval-pack-inventory --digest`, and `eval-pack-doctor --digest`) rather than review, optimize, or write-producing authoring steps. `review --digest` remains a manual/digest-only surface for an already-staged run. To create a draft, use the explicit review-draft switch:

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" \
  eval-pack-autopilot --skill my-skill --write-draft \
  --output skillopt/evals/my-skill-autopilot-draft.json

python3 -m hermes_skillopt.cli --home "$HERMES_HOME" \
  eval-pack-ingest-correction --skill my-skill \
  --correction "User correction or regression note..." \
  --expected-term verify \
  --output skillopt/evals/my-skill-correction-seed.json

python3 -m hermes_skillopt.cli --home "$HERMES_HOME" \
  eval-pack-ingest-context --skill my-skill \
  --context "Skill creation context or intended behavior..." \
  --expected-term safe \
  --output skillopt/evals/my-skill-context-seed.json

python3 -m hermes_skillopt.cli --home "$HERMES_HOME" \
  eval-pack-negative-boundary --skill my-skill \
  --output skillopt/evals/my-skill-negative-boundary.json
```

These drafts/seeds are deterministic review-only packs: no model calls, no task command execution, no live skill writes, no adoption eligibility. Promote a reviewed draft to a curated review pack with:

```bash
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" \
  eval-pack-promote --skill my-skill \
  --input skillopt/evals/my-skill-autopilot-draft.json \
  --output skillopt/evals/my-skill-curated-review.json
```

Production promotion is deliberately not one-click: CLI production promotion requires `--production`, `--production-policy`, and `--eval-execution-contract`, and still does not adopt or write live skills. The WebUI promotion button is review-only by design and refuses production promotion.

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

Adoptability additionally requires eligible curated validation and held-out test tasks, no production hard-failed rows, complete required runtime evidence for adoption-eligible frozen-target contracts, reviewer gate approval, verified artifacts, current SHA/proposed SHA/provenance-hash consistency, and an allowed native Hermes adopt guard. Current sandbox/fixed internal runners are review-only because they lack real Hermes runtime invocation proof; missing runtime evidence downgrades production eligibility even if a scorecard or skill-text-only score improves.

If review/digest shows a held-out test score but also warns that `heldout_test_sensitivity` is missing, keep the score caveated and do not turn it into an external performance or upstream parity claim.

## 5. Explicit adopt

Only after the review summary says the run is ready and you intend live writeback:

```bash
RUN_ID="<reviewed-run-id>"
python3 -m hermes_skillopt.cli --home "$HERMES_HOME" adopt "$RUN_ID" --confirm "ADOPT $RUN_ID"
```

`--force`, where available, only addresses current-SHA drift; it does not bypass artifact/provenance/evidence gates, reviewer gate, production/test gates, proposed-SHA checks, or native Hermes conflict guards. Do not put this in ordinary CI. If a maintenance script uses the non-interactive confirmation flag, keep it behind a separate human-approved deployment gate and still treat core gate failures as hard blockers.

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
- `conformance` writes no file unless `--output <report.json>` is explicit; there is no default repo-root `skillopt_conformance_report.json`.
- `scout`: read-only notification summary and safe next commands; no scheduled adoption.
- `artifact-hygiene-report`: read-only cleanup guidance; never deletes or resumes.
- `benchmark`/`eval-only`: fixed-skill local report only; always non-adoptable.

## 6a. Scheduled read-only surfaces only

If you want scheduled monitoring, schedule only the four cron-safe read-only notification/diagnostic digest surfaces (`scout --digest`, `doctor --digest`, `eval-pack-inventory --digest`, and `eval-pack-doctor --digest`) and route JSON/digest output to your notifier. `review --digest` is for manual review of an already-staged run, not a scheduled default. Use explicit guarded `--output` only when you intentionally want a report file:

```bash
hermes-skillopt --home "$HERMES_HOME" scout --output skillopt/reports/scout.json
```

Do not schedule `optimize`, `full-run`, `adopt`, `rollback`, `upstream-update`, cleanup commands, or eval-pack draft/promotion writes (`--write-draft`, seed writers, `eval-pack-promote`) from scout/doctor/autopilot output. Treat `next_actions`, `safe_next_commands`, and eval-pack `safe_commands` as human-review prompts.

## 7. WebUI workflow

```bash
python3 -m hermes_skillopt.cli webui --host 127.0.0.1 --port 7860
```

Use the WebUI scout/status views for read-only readiness, eval-pack workflow/doctor/autopilot for coverage planning, skill readiness queue for high-value candidates, skill-quality for read-only lint/digest (or explicit review-only skeleton creation), the explicit draft action for review-only pack generation, one-click eval-pack promotion for review packs only, the guided wizard for staged smoke/review/production runs, and the review console/API for decision summaries, evidence maturity, native metadata/adopt guards, digests, and artifacts. Server-side APIs keep scout/doctor/review/eval-pack workflow/doctor/readiness-queue/quality-without-skeleton/autopilot-plan read-only, `run_full` staged-only with `auto_adopt=false`/`force=false`, typed adopt/rollback confirmations, and ignore the WebUI home override for writeback/upstream update paths. WebUI default `gate_mode: soft` is review-oriented; use production intent/strict curated evidence for production candidates.

## 8. Runtime evidence and no-overclaim checklist

Before reporting a production candidate, verify:

- `review --summary` has `production_gate_eligible: true`, `test_gate_eligible: true`, and no blockers.
- `review --summary`/`--digest` score provenance points to the intended target executor/backend, optimizer backend, eval pack id/version/fingerprint, and `score_source: production_curated_eval_pack`.
- Eval inventory readiness shows `hermes-skillopt-readiness-adoptability-v1` gates as production eligible for the pack; advisory package metadata (`references/`, `templates/`, `scripts/`, `assets/`) was reviewed if relevant but not treated as adoption authority.
- `evidence_ledger` reports `production_runtime_ready: true`, `eval_level: production`, and no blockers; otherwise treat the run as review-only even when scores improved.
- Frozen-Hermes evidence includes explicit class/scope, target config fingerprint, provider/model/toolset/session/runtime fingerprints, permissions with task commands disabled, isolated runtime evidence, transcript/trajectory, execution-scoring evidence, and explicit real Hermes runtime invocation proof. If review shows `non_production_internal_runner` or missing real-runtime proof, treat it as review-only.
- `scorecard`, static skill-text-only evidence, and fixed internal sandbox runner output are not called adoption-eligible frozen Hermes execution.
- Task-provided commands are blocked by default and blocked-command rows are not production-gate eligible.
- Production hard-fails override soft score gains.
- Reports avoid claims of Microsoft upstream benchmark execution, upstream result equivalence, arbitrary live Hermes command execution, or external performance parity.
