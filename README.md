# Loop Engineering 2

让编码 agent 自主完成一个**目标**（开发、运维或排查问题），由控制器判定是否完成。支持 Claude Code、Codex，或任何命令行 agent。运行状态随时可见；需要你处理时，会通过桌面通知或 Telegram 提醒你。

```
你批准目标 → agent 规划任务 → agent 一轮一轮执行 → 控制器跑检查 → 独立评审 → 完成
                                  ↑                      │
                                  └─ 失败输出 / 交接记录 ←─┘
```

- **完成由控制器判定**：任务检查、验收命令、评审都由控制器亲自执行，agent 说"做完了"不算。
- **一次只运行一个 agent**：每一轮都是新会话，上下文靠交接记录传递。评审是单独的只读会话，可以换另一个工具来做。
- **三种目标**：`develop`（开发，直到验收通过）、`operate`（按计划跑健康检查，出问题才派 agent 修复）、`investigate`（找出根因，写成报告）。

---

## 0. 准备

- Python ≥ 3.10。仓库自带 `.venv` 时会优先使用它。
- 至少安装并登录一个 agent CLI：`claude` 或 `codex`。
- 项目最好是 git 仓库。

下文中 `LOOP` 指框架路径 `/Users/super/Documents/ai/loop-engineering-code`。接入项目后，在项目目录里一律使用 `.loop/bin/loop`。

## 1. 接入项目（每个项目一次）

```bash
$LOOP/bin/loop --project /path/to/app install
```

这一步会写入 Claude Code 和 Codex 的 MCP 配置（`.mcp.json`、`.codex/config.toml`）、skill 文件、`AGENTS.md` 说明，以及命令行入口 `.loop/bin/loop`。改完配置后，在 Claude Code 或 Codex 里重启会话（或执行 `/mcp`）让配置生效。

可选：在 Claude Code 底部状态栏显示进度：

```bash
.loop/bin/loop install --host claude --statusline
```

## 2. 设置提醒（一次，对所有项目生效）

需要你回答问题、审批、处理阻塞，或任务完成时，Loop 会主动通知你。macOS 桌面通知默认开启。

**Telegram：**

1. 在 Telegram 里找 **@BotFather**，发送 `/newbot`，按提示创建，复制它给的 token。
2. 运行设置向导，按提示粘贴 token，然后给你的 bot 发一条任意消息：

```bash
.loop/bin/loop telegram-setup
```

3. 发一条测试通知：

```bash
.loop/bin/loop notify
```

配置保存在 `~/.config/loop/notify.json`，权限为 600，不在项目里。请不要把 token 发到聊天或提交进 git。

**其他渠道**（ntfy、Slack、邮件）：在 `notify.json` 里加 `"command": ["sh", "-c", "curl -s -d @- ntfy.sh/你的主题"]`，事件 JSON 会通过标准输入传给这个命令。

## 3. 建立目标

**方式 A：在聊天里（推荐）**

在 Claude Code 或 Codex 里打开项目，对它说：

> 用 loop skill：读 docs/plan.md，和我确认验收命令、约束和预算，然后建立目标。

agent 会起草目标，展示给你看。你确认后，它会批准目标。

**方式 B：用命令行**

```bash
.loop/bin/loop goal --title "订单系统 v1" --objective @docs/plan.md --check "tests=npm test" --check "build=npm run build" --adapter codex
```

```bash
.loop/bin/loop approve
```

**方式 C：自己写 JSON**，然后运行 `loop goal --from-file goal.json` 和 `loop approve`：

```json
{
  "kind": "develop",
  "title": "订单系统 v1",
  "objective": "按 docs/plan.md 实现下单、支付回调、查询；测试和构建全部通过。",
  "context": ["docs/plan.md"],
  "constraints": ["不修改 legacy/ 目录", "不提交 git"],
  "acceptance": [
    {"id": "tests", "run": "npm test"},
    {"id": "e2e", "argv": ["npx", "playwright", "test"], "timeout": 1800}
  ],
  "reviews": [
    {"id": "code", "by": "agent", "adapter": "claude", "timeout_minutes": 15, "instructions": "对照方案检查需求覆盖、安全和测试质量"},
    {"id": "ux", "by": "human", "instructions": "手动走一遍下单流程"}
  ],
  "policy": {"max_iterations": 40, "max_hours": 12, "max_cost_usd": 20},
  "agent": {"adapter": "codex"}
}
```

| 字段 | 说明 |
|---|---|
| `acceptance` | **必填**（develop 和 operate）。控制器运行这些命令来判定完成，测试可以还没写。 |
| `reviews` | `by: agent`：只读的独立评审会话，可以指定 `adapter`、`model`、`timeout_minutes`。`by: human`：由你按步骤检查后批准。 |
| `policy` | `max_iterations`（agent 轮数）、`max_hours`、`max_cost_usd`（只对 Claude 生效）、`turn_timeout_minutes`、`approval_required`（必须先问你的操作）。 |
| `agent.adapter` | `codex`、`claude`，或在 `.loop/adapters.json` 里配置的其他工具（见第 9 节）。 |

如果方案里已经拆好了任务，可以直接导入，跳过规划轮：`loop plan --file tasks.json`。文件是一个列表，每项包含 `id`、`title`、`detail`、`depends_on`、`checks`。

### 完整开发流水线（develop 目标默认开启）

```
产品简报(产品) → 需求提取(需求分析师) → 需求审核(产品) → 方案设计(架构师) → 测试设计(测试设计师)
→ 计划+配人(技术负责人) → 资源就绪(资源规划) → 计划审核(计划审核员) → 开发(按角色组队)
→ 分类测试(控制器) → 代码/安全/性能评审 → 最终验收(验收负责人) → 产品验收(产品) → 人工评审
```

每个阶段都有自己的角色说明，写清楚"专注什么 / 先读什么 / 产出什么 / 做到什么算完成"，并且由控制器把关。审核不通过会退回到具体的某一步。完整说明和审核结论见 [docs/workflow-audit.md](docs/workflow-audit.md)。

- **新需求入口**：`loop request "需求描述" --doc docs/xxx.md`，规划 agent 会把它起草成目标，等你批准。
- **团队配置**：技术负责人给任务配 `team`，例如 `[{"role":"backend","count":2},{"role":"tester","count":1}]`。开发这个任务时，调度 agent 会按配置派出子 agent，每个子 agent 拿到自己角色的说明。人数上限用 `agent.team_limit` 设置，默认 4。
- **角色**：`loop roles` 列出全部角色。用 `.loop/roles/<角色>.md` 覆盖或新增角色，用 `.loop/workflows/develop.json` 修改每个阶段由哪个角色负责。
- **跳过阶段**：`"pipeline": {"skip": ["intake", "resources"]}`。不用流水线：`"pipeline": null`。之前已经批准的目标保持原来的流程。

### 不同阶段和角色使用不同的模型

```json
"agent": {
  "adapter": "codex", "model": "gpt-5.1-codex-mini",
  "stages": {
    "requirements":     {"model": "gpt-5.1", "effort": "high"},
    "plan":             {"model": "gpt-5.1", "effort": "high"},
    "plan_review":      {"adapter": "claude", "model": "opus", "effort": "high"},
    "review":           {"adapter": "claude", "model": "opus"},
    "final_acceptance": {"adapter": "claude", "model": "opus", "effort": "xhigh"},
    "planner":          {"model": "gpt-5.1", "effort": "high"}
  },
  "roles": {
    "architect": {"model": "gpt-5.1", "effort": "xhigh"},
    "tester":    {"effort": "medium"}
  }
}
```

- 可配置的阶段：`requirements`、`plan`、`plan_review`、`develop`、`repair`、`review`、`final_acceptance`、`planner`、`remediate`、`report`。
- 角色对应任务上的 `role` 字段：architect、backend、frontend、tester 等，可以自己起名。
- 优先级从高到低：评审自己指定的 → 任务角色 → 阶段 → 目标默认。
- `effort` 可选 minimal / low / medium / high / xhigh / max。Codex 会映射为 `model_reasoning_effort`，Claude 会映射为 `--effort`。
- 每一轮实际用了哪个工具、哪个模型、哪个推理强度，都会显示在看板和 STATUS.md 里。

## 4. 启动

```bash
.loop/bin/loop start
```

runner 在后台运行，关掉终端或聊天后也会继续。只有关机或执行 `loop stop` 才会停。

想在前台看日志就用 `loop run`。只想跑一轮可以用 `loop run --max-turns 1`。

## 5. 查看进度

| 方式 | 命令 |
|---|---|
| 终端实时刷新 | `.loop/bin/loop status --watch` |
| 浏览器看板 | `.loop/bin/loop dashboard`，然后打开 http://127.0.0.1:8765 |
| 文件 | `.loop/STATUS.md`（Claude、Codex 或你自己都可以直接读） |
| runner 日志 | `.loop/goals/<id>/runs/runner.log`，每一轮的完整输出在 `runs/turn-NNNN/` |
| 事件历史 | `.loop/bin/loop log`；`.loop/bin/loop audit` 校验历史有没有被篡改 |

## 6. 收到提醒后怎么做

| 提醒 | 命令 |
|---|---|
| ❓ 问题或 🔐 审批 | `.loop/bin/loop answer <问题ID> "你的回答"`（审批回答 `approve`，拒绝回答 `deny` 并写明原因） |
| ⛔ 阻塞 | 先读 `STATUS.md` 里写的原因和证据，处理好后运行 `.loop/bin/loop unblock --note "改了什么或决定了什么"` |
| 💰 预算用完 | `.loop/bin/loop resume`（追加一份同样的预算），或者先修改 `goal.json` 里的 `policy` 再 `loop approve` |
| 👤 人工评审 | 按问题里的步骤检查一遍，通过回答 `approve`，不通过就写明哪里有问题 |
| ✅ 完成 | 运行 `.loop/bin/loop status` 看结果和报告 |

回答问题后，runner 会自动继续。解除阻塞或追加预算后，需要再运行一次 `.loop/bin/loop start`。

## 7. 暂停、停止、恢复

```bash
.loop/bin/loop pause
```

- `pause`：等当前这一轮做完再停。之后直接 `start` 就能继续，`start` 会自动解除暂停。
- `stop`：立刻停止，当前这一轮会被中断。之后要先 `resume`，再 `start`。

## 8. 修改目标

直接编辑 `.loop/goals/<id>/goal.json`，然后运行 `.loop/bin/loop approve`。改动后必须重新批准才能继续运行，agent 在无人值守时不能替你批准。

同一个项目可以有多个目标：`loop list` 列出所有目标，`loop use <id>` 切换当前目标。

## 9. 目标队列（一个接一个自动做）

把后续的几批工作排成队列。当前目标完成（done）后，runner 会自动开始下一个**已经批准**的目标，并通知你；如果下一个还没批准，就停下来通知你去批准。遇到阻塞、预算用完或失败时也会停下，不会跳过。同一个项目同一时间只会运行一个目标。

```bash
.loop/bin/loop goal --from-file next.json --enqueue
```

```bash
.loop/bin/loop queue
```

```bash
.loop/bin/loop approve --goal <目标ID>
```

- 加 `--enqueue` 的意思是：把目标排进队列，但不切换当前目标。在聊天里起草时，对 `loop_goal_draft` 传 `enqueue: true` 效果相同。
- `loop queue add <id>... [--at N]` 加入队列（`--at` 指定位置），`loop queue remove <id>` 移除，`loop queue clear` 清空。
- 看板上的 Goal queue 卡片和 STATUS.md 都会显示队列，以及每个目标是否已经批准。

### 自动生成下一批目标（规划 agent）

队列里的目标全部完成后，runner 会自动派出一个**规划 agent**。它会读项目文档（路线图、需求台账、规格）、已完成目标的报告和当前队列，起草下一批目标，最多 5 个，并排进队列，然后用 Telegram 通知你去审阅。每个起草的目标都包含验收命令、回归检查和独立评审。

规划 agent 的权限有限：只能起草新目标，不能批准，不能修改已有的目标，也不能改代码。如果文档里写明了到此为止（比如"完成 R1 后停止"），它就不会起草，只告诉你原因。同一个已完成的目标只会自动提议一次。

也可以随时手动触发，并给它额外的方向：

```bash
.loop/bin/loop propose --count 3 --instructions "只做 G2 的身份相关部分"
```

如果不想自动提议，在 `.loop/settings.json` 里写 `{"propose_next": false}`。起草数量用 `propose_count` 调整。每次提议的完整记录（发给它的指令和它的输出）保存在 `.loop/proposals/<时间>/`。

## 10. 其他用法

**运维目标**：`"kind": "operate"`，加上 `"schedule": {"interval_minutes": 15}`，`acceptance` 里填健康检查。一切正常时不会调用模型；健康检查失败才会派 agent 修复，修完再重新检查。

**排查目标**：`"kind": "investigate"`。agent 会记录假设、证据和根因，最后用 `loop_finish` 提交报告。

**接入其他 agent 工具**：在 `.loop/adapters.json` 里配置命令，然后把目标里的 `agent.adapter` 设成对应名字：

```json
{"gemini": {"argv": ["gemini", "-p", "{prompt}"]},
 "aider":  {"argv": ["aider", "--yes", "--message-file", "{prompt_file}"]}}
```

可用占位符：`{prompt}`、`{prompt_file}`、`{project}`、`{mcp_config}`、`{mcp_command}`、`{model}`。

**在聊天里一步步做，不启动 runner**：对 agent 说"用 loop skill 继续当前目标"。它会调用 `loop_next` 拿到当前任务，做完后调用 `loop_task done`，由控制器跑检查。

**从 v1 迁移**：

```bash
.loop/bin/loop migrate-legacy --state-dir /path/to/v1-state --dry-run
```

确认 dry-run 结果没问题后，去掉 `--dry-run` 再运行一次。

## 11. 常见问题

| 现象 | 原因和处理 |
|---|---|
| `start` 之后马上退出，原因是 `Paused.` | 旧版本的问题，现在 `start` 会自动解除暂停。如果仍然出现，先运行 `loop resume`。 |
| Codex 报 `MCP tool call requires approval` | 配置太旧。重新运行一次 `install`，会写入 `default_tools_approval_mode = "approve"`。 |
| Codex 里看不到 loop 工具 | Codex 只为已信任的项目加载项目级配置。先在 Codex 里信任该项目。无人值守 runner 不受影响。 |
| GUI 或 Electron 程序在 agent 里崩溃 | Codex 沙箱不允许 GUI 程序启动。这类检查要交给控制器执行（`loop check`），不要让 agent 在沙箱里直接跑。 |
| `STATUS.md` 显示 `state.json was edited outside the controller` | 状态文件被手动改过。检查没问题后运行 `loop repair --note "..."`。 |
| 想彻底隔离 | Loop 的隔离靠宿主工具的沙箱（Codex workspace-write、Claude 工具白名单）。要硬隔离，请在容器或虚拟机里运行 runner。 |

## 12. 命令速查

所有命令都可以加 `--project <路径>`、`--goal <id>`、`--json`。

| 分类 | 命令 |
|---|---|
| 设置 | `install [--host claude,codex,generic] [--statusline]` · `telegram-setup` · `notify` |
| 目标 | `request "需求" [--doc …]` · `goal … [--enqueue]` · `approve [--goal id]` · `list` · `use <id>` · `queue [add\|remove\|clear]` · `propose [--count N] [--instructions …]` · `roles` |
| 运行 | `start` · `run [--max-turns N]` · `pause` · `resume` · `stop` |
| 查看 | `status [--watch]` · `dashboard [--port]` · `log [-n N]` · `audit` · `statusline` |
| 人工介入 | `answer <id> "…"` · `unblock --note "…"` · `review <id> pass\|fail --findings "…"` · `repair --note "…"` |
| agent 操作 | `next` · `requirements` · `stage-done` · `plan [--final]` · `task start\|done <id>` · `check` · `verify` · `note` · `ask` · `block` · `finish`（与 MCP 工具一一对应） |
| 迁移 | `migrate-legacy --state-dir <dir> [--dry-run]` |

设计说明见 [docs/design.md](docs/design.md)，旧版保留在 [legacy/v1](legacy/v1/README.md)。

## 开发

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

测试用 [假 agent](tests/fixtures/fake_agent.py) 走真实的 MCP 协议，跑完整的自主循环，不调用任何真实模型。
