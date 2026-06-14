# hermes-skillopt

`hermes-skillopt` 是一个 standalone Hermes 插件，用于在 **不修改 Hermes core** 的前提下，对当前 profile 的 `SKILL.md` 做安全、可审查、可回滚的优化提案。

## 为什么不是 fork microsoft/SkillOpt 整仓？

- 插件代码和 upstream 研究/实现解耦，减少未来冲突。
- Microsoft SkillOpt 作为 pinned external upstream clone 跟踪；更新只写 lock，不自动合并/采用。
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
- `hermes_skillopt_dry_run`: 只生成 staged proposal，不写目标技能。
- `hermes_skillopt_run`: `dry_run` 别名；也不会自动 adopt。
- `hermes_skillopt_review`: 查看 run 状态、diff/report 摘要。
- `hermes_skillopt_adopt`: sha guard 通过后，只写目标 `SKILL.md`，并创建备份。
- `hermes_skillopt_rollback`: 从备份或 staged original 恢复。
- `hermes_skillopt_upstream_status`: 查看 Microsoft SkillOpt clone/lock 状态。
- `hermes_skillopt_upstream_update`: clone/fetch/pin upstream；不合并插件代码。

## 本地 CLI

```bash
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke status
python3 -m hermes_skillopt.cli --home /tmp/hermes-skillopt-smoke dry-run --skill demo --goal "make safer"
```

## 安全模型

- 默认只在 `$HERMES_HOME/skillopt/staging/<run-id>/` 生成文件。
- Dry/run 输出：`manifest.json`, `original_SKILL.md`, `proposed_SKILL.md`, `diff.patch`, `report.md`, `evidence.json`。
- Adopt 前校验当前技能 sha256 是否等于 staged original；除非显式 `force=true`。
- Adopt 前备份到 `$HERMES_HOME/skillopt/backups/<timestamp-run-id>/`。
- Rollback 恢复备份或 staged original。
- session evidence 会做常见 token/key/password/Authorization 脱敏。
- 没有外部 API 依赖；确定性 fallback engine 总是可用于 smoke test。
- 如果未来 Hermes `ctx.llm.complete` 可用，可设置 `use_llm=true` 尝试更好的提案；失败会自动 fallback。

## 已知限制

- 当前 deterministic engine 保守地追加候选规则/TODO，不尝试复杂重写。
- slash command 未注册；插件工具是主接口。
- upstream update 只 clone/fetch/pin，不自动把 upstream 代码变成插件逻辑。
