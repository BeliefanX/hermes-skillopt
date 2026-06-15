# hermes-skillopt

`hermes-skillopt` 是一个 standalone Hermes 插件，用于在 **不修改 Hermes core** 的前提下，对当前 profile 的 `SKILL.md` 做安全、可审查、可回滚的优化。

## 当前实现：Hermes-native SkillOpt full cycle

默认 `run/full-run` 已升级为 Hermes-native、SkillOpt/Sleep-inspired 完整闭环：

- **Harvest**：从当前 `$HERMES_HOME/state.db`、`sessions/`、`logs/` 中提取与目标 skill/query 相关的近期会话片段，支持 `query`、`lookback_days`、`limit`，并对 secrets/token/password 做 redaction。
- **Mine**：把片段转成 task/evidence items：`user_goal`、`assistant_outcome`、tools/errors、skill relevance、success/failure hints。
- **Split**：确定性生成 `train_items.jsonl`、`val_items.jsonl`、`test_items.jsonl`。
- **Reflect**：LLM 分析 train successes/failures，提炼 recurring defects、missing rules、over-broad rules、verification gaps。
- **Edit**：LLM 只生成 bounded edits（`append`/`replace`/`delete`/`insert_after`），保护 YAML frontmatter，不允许整文件无约束重写；受 `edit_budget` 约束。
- **Apply**：生成 candidate skill artifacts。
- **Gate**：在 val items 上用 LLM judge + heuristic evaluator 比较 current vs candidate；只有 candidate 严格更好才 `staged_best`，否则 rejected 并写入 `rejected_edits.jsonl`。
- **Iterations**：支持 `iterations` 1-N 次循环，best/current skill 迭代更新。
- **Artifacts**：每个 run 写入 `manifest.json`, `original_SKILL.md`, `current_SKILL.md`, `best_skill.md`, `proposed_SKILL.md`, `diff.patch`, `report.md`, `evidence.json`, `train_items.jsonl`, `val_items.jsonl`, `test_items.jsonl`, `reflections.json`, `candidate_edits.json`, `gate_results.json`, `rejected_edits.jsonl`。

## 为什么不是 fork microsoft/SkillOpt 整仓？

- 插件代码和 upstream 研究/实现解耦，减少未来冲突。
- Microsoft SkillOpt 作为 pinned external upstream clone 跟踪；更新只写 lock，不自动合并/采用。
- 当前实现是 **Hermes-native SkillOpt-inspired full cycle**；未来可增加 adapter，把 Hermes harvest/mine/gate 映射到 upstream `skillopt_sleep` 风格接口。
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
- `hermes_skillopt_adopt`: sha/path guard 通过后，只写目标 `SKILL.md`，并创建备份。
- `hermes_skillopt_rollback`: 从备份或 staged original 恢复。
- `hermes_skillopt_upstream_status`: 查看 Microsoft SkillOpt clone/lock 状态。
- `hermes_skillopt_upstream_update`: clone/fetch/pin upstream；不合并插件代码。

Full-run 参数：

- `skill`, `query`, `lookback_days`, `limit`, `iterations`, `edit_budget`
- `backend`: `auto|hermes|mock`，默认 `auto`
- `allow_mock`: `auto` 且 Hermes `ctx.llm` 不可用时，只有显式 true 才允许 mock（用于 CLI/tests/smoke）
- `auto_adopt`: 默认 false；为 true 时仍必须通过 sha/path guard
- `force`: 用于 adopt guard override

## 本地 CLI

```bash
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke status
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke full-run --skill demo --query demo --backend mock --allow-mock --iterations 2
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke review <run_id>
```

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
- **Full run**：`skill/query/lookback/limit/iterations/edit_budget/backend/allow_mock` 控件；始终 staged-only，`auto_adopt` 在 WebUI 中固定为 false。
- **Review artifacts**：只读取 `$HERMES_HOME/skillopt/staging/<run_id>/` 内的 `report.md`、`diff.patch`、`gate_results.json`、`proposed_SKILL.md`、`rejected_edits.jsonl` 等固定 artifact，不提供任意文件浏览。
- **Adopt / Rollback**：必须显式输入 `ADOPT <run_id>` 或 `ROLLBACK <run_id>` 才会执行；仍使用 core 的 path/sha guards 和 backup/rollback 机制。
- **Upstream**：可查看/更新 pinned Microsoft SkillOpt upstream clone；只更新 lock/clone，不自动合并插件代码。

若未安装 Gradio，启动会给出明确安装提示，不影响测试和 Hermes 插件加载。

## LLM backend

- 插件 runtime 优先使用 Hermes `ctx.llm.complete_structured`，其次 `ctx.llm.complete`。
- CLI/tests 可以注入或使用 `backend=mock --allow-mock`。
- `backend=auto` 在没有 Hermes ctx 且未 `allow_mock` 时会清晰报错，不会 silent deterministic fake pretending full。
- 所有 LLM 输入会先 redaction；JSON 输出 parse 失败会写 `llm_*_repair_*.json` artifact 后失败。

## 安全模型

- 默认只在 `$HERMES_HOME/skillopt/staging/<run-id>/` 生成 staged artifacts。
- Full run 不会默认 adopt；只有 `auto_adopt=true` 或显式 `adopt` 才写目标 skill。
- Adopt 前校验 current skill sha256 是否等于 staged original；除非显式 `force=true`。
- Adopt 前备份到 `$HERMES_HOME/skillopt/backups/<timestamp-run-id>/`。
- Rollback 恢复备份或 staged original，并带 current-sha guard。
- session evidence 和 LLM artifact 均做常见 token/key/password/Authorization 脱敏。
- 路径 guard 限制 manifest target 必须在当前 `$HERMES_HOME/skills` 下。
