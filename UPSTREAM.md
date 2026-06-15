# UPSTREAM.md

本仓库采用 standalone private repo `BeliefanX/hermes-skillopt`，而不是直接 fork + 重写 `microsoft/SkillOpt`。

## 设计选择

- `hermes-skillopt`: Hermes 插件、测试、CLI、安装脚本、Hermes profile 安全逻辑。
- `microsoft/SkillOpt`: upstream 参考实现/论文代码/未来算法来源，以 pinned external clone 方式跟踪。

这样做的好处：

1. Hermes 插件代码不和 upstream 大量文件混在一起。
2. Upstream 更新时只需 fetch/pin/compare，不会自动冲击本地插件。
3. 将来如果要移植具体算法，可以按小 PR/小模块引入，并保留清晰来源。
4. 如果后续确实需要 fork upstream，也可以再创建 fork，并在本仓库记录 fork URL/commit。

## 当前 SkillOpt 对齐状态

当前代码没有 vendor 整个 upstream package；实现的是 **Hermes-native SkillOpt core adapter**：Hermes 的 `SKILL.md` 是 trainable state，`TargetExecutor` 是 frozen executor，optimizer backend 只做 reflection + bounded skill edit，`HermesSkillEnv` 提供 curated replay/synthetic/session-mined benchmark，`ValidationGate` 以 held-out `candidate_score > current_score` 作为唯一接受门槛。LLM judge 只能辅助说明，不能替代 validation gate。

Phase 2 已加入 curated replay/eval scorecards：`full-run --eval-file` / plugin-WebUI `eval_file` 可加载 JSONL/JSON tasks，默认查找 `$HERMES_HOME/skillopt/evals/<skill-name>.jsonl` 或 skill 目录 `evals/*.jsonl`。Task schema 包含 `id`, `prompt`, `success_criteria` 或 `expected_keywords`, `forbidden_keywords`, `split` (`train`/`validation`/`test`), optional `weight`；可靠确定性评分请优先写显式 `expected_keywords` / `forbidden_keywords`，`success_criteria` 主要保留作 metadata/evidence，当前不是完整语义 judge。显式 eval path 必须 resolve 到当前 `$HERMES_HOME` 内普通文件，防 path traversal/symlink escape。当前仍是 Hermes-native adapter，不是 Microsoft 官方完整 trainer port，但 benchmark/gate 结构更接近 SkillOpt-Sleep 的 replay/held-out eval。

这不是 Microsoft 官方完整 trainer port。Microsoft upstream 仍 pinned for tracking；本仓库保持 standalone，不混改 upstream。Hermes safety（staged-only 默认、显式 adopt/rollback、path/sha guard、profile 隔离）作为外层 shell 保留。

## 更新流程

```bash
cd /Users/fanxuxin/Hermes_Sync/Default/hermes-skillopt
bash scripts/update_upstream.sh
```

默认 clone/fetch 到：

```text
$HERMES_HOME/skillopt/upstream/SkillOpt
```

并写入：

```text
skillopt_upstream.lock
```

lock 内容包含 upstream URL、clone path、pinned commit、更新时间。**更新 upstream 不会自动修改插件代码，也不会自动 adopt 任何 skill。**

## 对比/引入 upstream 的建议

1. 先运行 `hermes_skillopt_upstream_update` 或脚本更新 lock。
2. 在 upstream clone 中阅读变更，选出可移植的小算法/评估逻辑。
3. 在本仓库新建模块或测试，保持 Hermes profile-safe 和 staged-only 默认行为。
4. 通过 pytest + temp `HERMES_HOME` smoke 后再 push。

## 未来如果需要 fork

只有当需要长期维护 Microsoft SkillOpt 源码级 patch 时，才建议 fork 到 `BeliefanX/SkillOpt` 或类似仓库。本插件仓库仍应作为 Hermes 集成主仓库，fork 只作为 vendor/upstream source。
