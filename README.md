# hermes-skillopt

`hermes-skillopt` 是一个 standalone Hermes 插件，用于在 **不修改 Hermes core** 的前提下，对当前 profile 的 `SKILL.md` 做安全、可审查、可回滚的优化。

## 当前实现：SkillOpt-inspired Hermes-native adapter

默认 `run/full-run` 是 SkillOpt-inspired Hermes-native adapter，借鉴 Microsoft SkillOpt 的核心抽象，但不是 Microsoft 官方完整 trainer port：

- **skill document = trainable state**：当前 profile 下的 `SKILL.md` 是唯一可训练参数。
- **target agent/model = frozen executor**：`TargetExecutor` 在同一冻结条件下分别评测 current/candidate skill；支持 deterministic replay/scorecard，以及 production-safe `hermes_sandbox_executor_mvp`（隔离 temp HOME/HERMES_HOME/workspace、受控 command runner、captured transcript/exit/timeout、无 live profile writes）。
- **optimizer model = reflection + bounded skill edit**：optimizer 只基于 rollout evidence 反思并生成 `append`/`replace`/`delete`/`insert_after` 等 bounded edits，不能直接写 profile。
- **environment/benchmark = scorecard/replay eval field**：`HermesSkillEnv` 从 curated/synthetic/session-mined tasks 构造 train/validation/test；只有显式 curated scorecard 可作为 adopt gate，fallback/synthetic 仅 review-only。
- **validation gate + held-out test = acceptance evidence**：train 只驱动 reflection；held-out validation gate 用 `candidate_score > current_score` 选择 best；final best 会再跑 held-out test 并写 `test_results.json`。生产 adopt 还要求显式 curated validation gate 与 curated/no-regression test eligibility；fallback/synthetic/session-mined/mock-only run 只能 review-only。

完整流程：load skill state → build train/validation/test tasks → evaluate current with frozen target → optimizer reflects/proposes bounded edits from rollout evidence → apply candidate → evaluate candidate on held-out validation → validation gate compares current vs candidate → if improved stage `best_skill.md` → user review/adopt/rollback。

Hermes staged safety 是外壳：full-run 只写 `$HERMES_HOME/skillopt/staging/<run-id>/`；`adopt`/`rollback` 必须显式、可逆、带 path/sha/manifest gate guard，并限制在当前 active `$HERMES_HOME/skills`。生产 tool/WebUI 的 live writeback 不接受任意 `hermes_home` override；CLI 只有带 `--unsafe-cross-profile-writeback --home ...` 的显式离线维护确认才允许跨 active profile 写回。生产 tool/CLI 不提供 auto-adopt。

Artifacts：每个 run 写入 `manifest.json`, `original_SKILL.md`, `current_SKILL.md`, `proposed_SKILL.md`, `diff.patch`, `report.md`, `evidence.json`, `train_items.jsonl`, `val_items.jsonl`, `test_items.jsonl`, `current_validation_results.json`, `candidate_validation_results.json`, `test_results.json`, `reflections.json`, `candidate_edits.json`, `gate_results.json`, `rejected_edits.jsonl`；只有 validation gate 通过时才额外 stage `best_skill.md`。Manifest 记录重要 artifact 的 sha256；`review`/`adopt`/`rollback` 会重新校验 artifact integrity。

## Curated replay/eval benchmark

Full-run 支持 curated replay scorecards，让 environment/benchmark 更接近 SkillOpt-Sleep 的 held-out eval：

- CLI: `full-run --eval-file skillopt/evals/demo.jsonl`；plugin/WebUI 同名参数 `eval_file`。
- 默认查找顺序：`$HERMES_HOME/skillopt/evals/<skill-name>.jsonl`，然后 skill 目录下第一个 `evals/*.jsonl`。
- `eval_file` 可用 JSONL 或 JSON（list 或 `{ "tasks": [...] }`），显式路径可为绝对路径或相对 `$HERMES_HOME`，但必须 resolve 成 `$HERMES_HOME` 内普通文件；会拒绝不存在文件、path traversal、symlink escape。
- 当前 skill 与 candidate skill 使用同一个 frozen `TargetExecutor` 跑同一组 validation tasks；`ValidationGate` 仍只接受 `candidate_score > current_score`。LLM judge/heuristic 只记录解释，不能绕过 gate。

最小 task schema：

```json
{"id":"v1","prompt":"held-out validation replay","expected_keywords":["verify","blocker"],"forbidden_keywords":["fabricate"],"split":"validation","weight":2}
```

字段：`id`, `prompt`, `success_criteria` 或 `expected_keywords`, `forbidden_keywords`, `required_markers`/`forbidden_markers`（工具/action/transcript marker）, `split` (`train`/`validation`/`test`), optional `weight`, `timeout`, `fixtures`, `executor` (`sandbox` opt-in)。`validation` split 在 artifacts 中写为 `val_items.jsonl`。

评分语义：当前确定性 scorer 主要按 `expected_keywords`/`required_markers` 命中加分、按 `forbidden_keywords`/`forbidden_markers` 命中扣分；这是可靠 curated eval 的推荐写法。`success_criteria` 会保留到 metadata/evidence，便于人工审查和后续 judge 说明；只有在未提供 `expected_keywords` 时，系统才会从很短的 criteria 中抽取简单词项作为弱 fallback。长自然语言 `success_criteria` **不会** 被当作完整语义 judge，也不能替代显式断言。

Sandbox executor 示例：

```json
{"id":"v-sandbox","prompt":"sandbox replay","expected_keywords":["verify"],"required_markers":["SANDBOX_OK"],"executor":"sandbox","split":"validation","timeout":10}
```

## 为什么不是 fork microsoft/SkillOpt 整仓？

- 插件代码和 upstream 研究/实现解耦，减少未来冲突。
- Microsoft SkillOpt 作为 pinned external upstream clone 跟踪；更新只写 lock，不自动合并/采用。
- 当前实现是 **SkillOpt-inspired Hermes-native adapter with six-stage trainer path**：`SixStageSkillOptTrainer` 负责 rollout→reflect→aggregate→select→update→evaluate/gate 与 final test evidence；`core.full_run()` 是 safety shell/artifact/adoptability coordinator。它不是 Microsoft 官方完整 trainer port；Hermes staged safety/review/adopt/rollback/profile isolation 和 multi-agent handoff 是外层安全壳。
- Hermes 集成面保持很小：`plugin.yaml` + `register(ctx)` + toolset。

## 安装到本机 default profile

```bash
cd /Users/fanxuxin/Hermes_Sync/Default/hermes-skillopt
bash scripts/install_local.sh
```

这会创建：

```text
~/.hermes/plugins/hermes-skillopt -> /Users/fanxuxin/Hermes_Sync/Default/hermes-skillopt
```

已运行中的 Telegram Hermes 会话可能需要重启/重载后才会看到新工具。

## 工具

Toolset: `hermes_skillopt`

- `hermes_skillopt_status`: 状态、技能数量、最近 staged runs。
- `hermes_skillopt_run`: 默认 `mode="full"`，执行完整 SkillOpt cycle；`mode="legacy"` 可使用旧 dry-run。
- `hermes_skillopt_full_run`: 明确执行完整 cycle。
- `hermes_skillopt_dry_run`: legacy staged proposal，不写目标技能。
- `hermes_skillopt_review`: 查看 run 状态、gate score、accepted/rejected、diff/report 路径和摘要。
- `hermes_skillopt_adopt`: sha/path guard 通过后，只写 active profile 的目标 `SKILL.md`，并创建备份；tool schema 不接受 `hermes_home` override。
- `hermes_skillopt_rollback`: 在 active profile 内通过已校验的 backup manifest 和备份 `SKILL.md` 恢复（无 staged original fallback）；tool schema 不接受 `hermes_home` override。
- `hermes_skillopt_upstream_status`: 查看 canonical HERMES_HOME upstream clone/lock 状态；生产 tool/CLI/WebUI 不接受任意 `repo_path`。
- `hermes_skillopt_upstream_update`: clone/fetch/pin canonical upstream；不合并插件代码。
- `hermes_skillopt_handoff_optimize`: 生成/评分 Hermes `delegate_task` 的 multi-agent dispatcher→worker handoff 包；无 LLM/network 调用，不自动修改全局 prompt/skill。

Full-run 参数：

- `skill`, `query`, `eval_file`, `lookback_days`, `limit`, `iterations`, `edit_budget`
- `backend`: `auto|hermes|mock`，默认 `auto`
- `allow_mock`: `auto` 且 Hermes `ctx.llm` 不可用时，只有显式 true 才允许 mock（用于 CLI/tests/smoke）
- `target_executor`: `auto|replay|sandbox|scorecard`；`sandbox` 使用隔离 temp profile/home/workspace 并捕获 transcript/exit/evidence
- `force`: 仅用于显式 adopt/rollback current-sha guard override；不能绕过 validation/production/test/profile/artifact gates，也不能与 auto-adopt 组合（生产 tool/CLI 已禁用 auto-adopt）

## 本地 CLI

```bash
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke status
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke full-run --skill demo --query demo --backend mock --allow-mock --iterations 2
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke review <run_id>
python3 -m hermes_skillopt.cli handoff-optimize "Goal: reduce delegate_task rework; Acceptance: concise evidence and tests"
```

## Multi-agent delegate_task handoff optimization

First-version multi-agent support is deliberately scoped to dispatcher/worker handoff packaging, not global single-agent prompt rewriting. It can deterministically build and score a staged handoff template with:

- dispatcher policy and bounded context package (`goal`, `scope`, `acceptance`, `verification`)
- slim worker output contract (`status`, `changed_files`, `key_evidence`, risks, next step)
- reviewer acceptance rubric, retry/escalation rules, and metrics (`context_size`, `rework_risk`, `acceptance_omissions`)
- `staged_only=true` and `no_global_auto_adopt=true`

## Hermes-native WebUI (optional Gradio)

WebUI 是可选依赖，普通 plugin import / Hermes tool registration 不需要安装 Gradio。安装后可用 CLI 或 module 方式启动：

```bash
python3 -m pip install 'hermes-skillopt[webui]'
python3 -m hermes_skillopt.webui --host 127.0.0.1 --port 7860
# or
python3 -m hermes_skillopt.cli webui --host 127.0.0.1 --port 7860
```

WebUI 暴露的是 Hermes-specific workflow，而不是 upstream generic training config：

- **Status**：当前 `HERMES_HOME`、skills count、最近 staged runs。
- **Full run**：`skill/query/eval_file/lookback/limit/iterations/edit_budget/backend/allow_mock` 控件；始终 staged-only，`auto_adopt` 在 WebUI 中固定为 false。
- **Review artifacts**：只读取 `$HERMES_HOME/skillopt/staging/<run_id>/` 内的 `report.md`、`diff.patch`、`gate_results.json`、`proposed_SKILL.md`、`rejected_edits.jsonl` 等固定 artifact，不提供任意文件浏览。
- **Adopt / Rollback**：必须显式输入 `ADOPT <run_id>` 或 `ROLLBACK <run_id>` 才会执行；仍使用 core 的 path/sha guards 和 backup/rollback 机制。WebUI live writeback 固定使用 active profile，忽略 HERMES_HOME override，避免浏览器界面成为跨 profile 写回入口。
- **Upstream**：可查看/更新 pinned Microsoft SkillOpt upstream clone；只更新 lock/clone，不自动合并插件代码。

若未安装 Gradio，启动会给出明确安装提示，不影响测试和 Hermes 插件加载。

## LLM backend

- 插件 runtime 优先使用 Hermes `ctx.llm.complete_structured`，其次 `ctx.llm.complete`。
- CLI/tests 可以注入或使用 `backend=mock --allow-mock`。
- `backend=auto` 在没有 Hermes ctx 且未 `allow_mock` 时会清晰报错，不会 silent deterministic fake pretending full。
- 所有 LLM 输入会先 redaction；JSON 输出 parse 失败会写 `llm_*_repair_*.json` artifact 后失败。

## 安全模型

- 默认只在 `$HERMES_HOME/skillopt/staging/<run-id>/` 生成 staged artifacts。
- Full run 不会 auto-adopt；只有显式 `adopt` 才可能写目标 skill。
- Adopt 只接受 full-run 产物：`status == staged_best`、`adoptable == true`、`gate.accepted == true`、`production_gate_eligible == true`、`test_gate_eligible == true`，且 manifest artifact hashes 重新校验通过；legacy dry-run/fallback/synthetic/session-mined/mock-only runs 均为 review-only。
- Adopt 前校验 current skill sha256 是否等于 staged original；除非显式 `force=true`。
- Adopt 前备份到 `$HERMES_HOME/skillopt/backups/<timestamp-run-id>/`。
- Rollback 只通过已校验的 backup manifest 和备份 `SKILL.md` 恢复，并带 current-sha guard。
- session evidence 和 LLM artifact 均做常见 token/key/password/Authorization 脱敏。
- 路径 guard 限制 manifest target 必须在当前 active `$HERMES_HOME/skills` 下；`adopt`/`rollback` 默认拒绝对非 active home 的 live writeback，`force=true` 只绕过 sha guard，不绕过 validation/production eligibility/profile isolation。CLI 的 `--unsafe-cross-profile-writeback --home ...` 仅用于显式离线维护，并有回归测试覆盖。
