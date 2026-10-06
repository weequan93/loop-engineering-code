# 持续执行监督器

`host-supervise` 补上原生对话结束后的执行接力。它是独立后台进程：启动一个专用 Codex CLI 会话，每轮结束后读取同一团队的真实状态，仍有可执行任务就恢复这个会话。普通 `$loop-engineering` 对话和 MCP 工具不会自行启动它。

控制器仍决定任务准入、原检查、正式评审、交接和完成。监督器不重建团队、不重置账本、不放宽验收，不将模型的结束回复当作完成。已登记且已授权的项目续批队列也会继续推进；没有队列时，只完成当前批次。

Skill 定义读文档、需求确认、专业分工、检查和修复的方法；原生宿主执行这些工作。
跨回合接续由宿主的持续目标能力或后台监督器负责，Skill 文件本身不会唤醒已退出的宿主。
支持原生持续目标的宿主可以直接承担接续，不需要同时再启动第二个协调者。
本框架目前的后台入口是单次显式启动，尚未自动接入 App 的原生持续目标；
后续普通任务和已授权续批由同一监督器推进，不应要求用户逐项调用脚本。

## 对现有项目启动一次

先在原宿主中停止正在执行的主任务及其子 agent，确认原写入和长操作的实际状态。关闭窗口或过期心跳不足以证明进程已停止。保留原团队、回答、草稿和历史。`--host-idle` 是操作者完成此项核对后的声明，监督器不能独立证明另一宿主的所有进程已经停止。

在 Loop 框架目录执行：

```bash
.venv/bin/python scripts/loop.py host-supervise /Users/super/Documents/ai/macare \
  --state-dir /Users/super/Documents/ai/loop-states/macare \
  --team-id team-22d34201e8ef4358aecde6aaa1719884 \
  --host-idle
```

默认后台运行，输出 `supervisor_id`、启动进程编号和日志路径。不要同时在原项目对话重新派发写入任务。监督器使用新的专用会话，后续轮次恢复其保存的 UUID；不会使用 `--last` 或向原 App/CLI 对话发送消息。可用 `--foreground` 在终端前台运行。进程可跨越聊天轮次和终端关闭；当前版本没有系统启动服务，重启机器后需要检查状态并恢复。

不需要再次执行 `host-install` 来驱动已有冻结项目。每轮以配置覆盖注入当前 Loop MCP 后端和私有草稿位置，保留项目中的技能、配置和冻结候选。额外写目录仅包含草稿及本轮输出，不开放整个私有状态目录；账本操作通过控制器完成。这仍不代表 OS 读取隔离。Codex 的实际登录、项目信任、规则、MCP 许可及宿主功能仍需可用。缺少权限会成为真实阻塞，不绕过沙箱或审批。

框架代码更新后，监督器会在宿主轮次已经回收、没有未核对派发的边界记录
`RECONNECT_REQUIRED`，释放原进程锁和 MCP 服务，再以同一监督器 ID 替换进程，
加载当前代码。原专用会话、期限、轮数、用量、草稿和冻结合同都保留，不要求用户
再回复“继续”。每个原监督器最多自动重连三次；反复部署变动、暂停、到期或未知
副作用仍保留阻塞。直接嵌入 Python 的调用者应在关闭原服务后调用
`reconnect_supervisor`；仅调用 `NativeSupervisor.run` 会返回需要重连的检查点。
框架开发须先完成离线验证；重连本身不证明新代码或真实模型质量已经验证。

进程身份观察固定 `ps` 的英文区域设置，并兼容历史账本中月/日顺序不同的日期。
开始时间或进程组不同仍拒绝匹配；权限不足不会被当成已停止或可接管。

默认监督器生命期为 28,800 秒，最多 100 轮，每轮上限 14,400 秒，等待间隔 5 秒。用 `--max-wall-seconds`、`--max-turns`、`--turn-timeout-seconds` 和 `--poll-seconds` 在第一次启动时声明所需上限。生命期包括等待用户和外部证据的时间，暂停、重连、恢复不会重新计算期限。它是单独的宿主执行边界，不扩充团队/项目控制器的额度。原长检查必须能放进所声明的单轮和剩余生命期；不足时应先准备具体预算方案。

## 原验证环境的审批接入

默认 `--approval-mode never` 保留原行为；越过沙箱的操作直接失败。
经实际操作者授权可选择 `--approval-mode auto-review`，使用当前官方
`--approve-for-me` 路由，仍为 `workspace-write`。每项请求由官方审批器判断，
框架不提供全权限、绕过标志、自定义放行政策或忽略规则。
启动前的有界 CLI 帮助检查只确认参数可用，不证明具体命令、进程复核或清理被允许。
昂贵检查前仍须在实际验证环境完成原有的有界能力预检；失败记录和清理守卫保持有效。

已有监督器通过 `host-supervisor-runtime` 在已回收边界调整同一个账本：
参数包含原 ID、观察到的 `--expected-revision`、`--expected-candidate`、唯一
`--update-id`、实际 `--authorization`、`--approval-mode` 和 `--host-idle`。
它校验无在途操作或专业写入，不改变会话、期限、轮数、用量、任务和原失败。
重复相同更新只返回已记录结果；不同请求、旧候选、暂停、过期或未解决效果拒绝。
调整本身不启动模型；确认原效果后用同一个 ID 的恢复入口接续。
这是操作者生命周期入口，不是让专业 agent 批准自己权限的 MCP 工具。

## 看进度和回答问题

agent-fabric 使用同一个入口。停止它的旧写入协调者和子 agent 后，在框架目录执行：

```bash
.venv/bin/python scripts/loop.py host-supervise /Users/super/Documents/ai/agent-fabric \
  --state-dir /Users/super/Documents/ai/loop-states/agent-fabric \
  --team-id team-a34a6700edb14a66827f5adf16491840 \
  --host-idle
```

这里使用已登记续批队列的根团队，监督器会跟随实际当前子批次。
符合原返工条件的退回交接可继续；真实就绪结果取决于当前候选、效果、权限和剩余额度。
监督器的默认八小时生命期与项目控制器额度分别生效，不能用其中一个绕过另一个。

将启动结果里的实际 ID 替换下面的 `SUPERVISOR_ID`：

```bash
.venv/bin/python scripts/loop.py host-supervisor-status /Users/super/Documents/ai/macare \
  --state-dir /Users/super/Documents/ai/loop-states/macare \
  --supervisor-id SUPERVISOR_ID
```

状态包含当前团队、轮数、已知事件 token、剩余生命期、最近结果、待核对操作，以及本机监督器进程观察。`dashboard_url` 是监督器运行期间的进度链接，`dashboard_path` 保留最后的页面文件；进程退出后原临时链接可能不可用，可查看页面文件。Loop 进度页新增“持续执行监督器”。保存的 `RUNNING` 不保证进程仍活着；状态查询也不会续期或启动模型。

启动时要求协调者先读文档，将范围、环境、权限、预算和真实验收入口的关键问题集中在需求/计划阶段，计划冻结前调用预检。已确认的选择不重复询问。问题写入原团队后，监督器保持 `WAITING`，用户在原项目的 Loop 入口提交实际回答即可；回答改变控制器路由后会自动接着工作，无需再说“继续”。这个入口可以提供回答，但不能同时重新启动另一个写入协调者。

缺少正式评估者或真人判断时保持未验收并等待实际接入。模型发现控制器命令内部的外部能力缺口时，可以提交具体阻塞报告；这只是宿主声明，不是检查证据。独立工程和依赖已就绪的工作应先完成，机器操作不能标为真人 VoiceOver 验收。补齐实际材料后，原状态变化可触发继续；尚未体现在控制器里的外部变化可使用明确的恢复操作通知监督器。

接收方退回交接时，已启用且未耗尽原 `native_rework` 的团队会通过
`loop_handoff_rework` 回到原任务返工，监督器随后继续派发、原检查和新交接。
旧退回原因、原子任务和用量全部保留。未启用返工、无编辑权限或有未核对副作用
时保持阻塞。详细规则见[交接退回恢复](native-rework-verification.md#rejected-handoff-recovery)。

## 暂停、取消与恢复

```bash
.venv/bin/python scripts/loop.py host-supervisor-control /Users/super/Documents/ai/macare \
  --state-dir /Users/super/Documents/ai/loop-states/macare \
  --supervisor-id SUPERVISOR_ID --action pause
```

`pause` 停止监督器拥有的活动宿主进程组，保留草稿与待核对日志；`cancel` 是监督器终止信号，不能恢复。它们不改写原团队合同，取消监督器也不将原团队标为验收完成。原团队/项目的暂停和取消信号同样阻止新派发。

没有在途操作时，`--action resume` 可重新启动同一监督器，保留原轮数、用量、期限。若原团队也已暂停，须按原入口明确恢复团队；恢复监督器不会代替那项决定。

宿主超时、输出不完整、错误会话、未知副作用或进程崩溃会保留 `RECOVERY_REQUIRED`。检查原宿主、工作副本、检查进程和原操作日志，确认没有仍在写入的进程，再使用：

```bash
.venv/bin/python scripts/loop.py host-supervisor-reconcile /Users/super/Documents/ai/macare \
  --state-dir /Users/super/Documents/ai/loop-states/macare \
  --supervisor-id SUPERVISOR_ID \
  --note "填写实际核对了哪些进程、草稿与已有副作用" --continue
```

原宿主进程组仍活着或无法观察时拒绝核对。丢失的派发按原保留上限计入宿主耗时，token 覆盖保留未知；不会清零轮数或期限。尚未生成会话 ID 的首次崩溃，核对后只能开启新的专用宿主上下文，原团队和控制器操作仍需恢复。多个监督器用项目设备/文件身份锁防止并发派发；操作者仍负责其他宿主和不同系统用户的实际写入。

连续三轮没有控制器、实际候选或草稿进展会停止空转，保留诊断。修复真实问题后可明确恢复，原总上限仍生效。预算耗尽不会通过重新建账本规避。

旧版本已经停在 `BLOCKED` 的监督器不会自行加载上述逻辑。核对它没有在途宿主、
实际旧进程已退出且原项目没有额外写入者后，通过 `host-supervisor-control --action resume`
恢复原 ID，不创建新的监督器或重置期限。

## 当前验证范围

支持 POSIX 平台上的 Codex CLI。Codex 官方提供[非交互执行与会话恢复](https://learn.chatgpt.com/docs/non-interactive-mode)；当前适配依据本机 CLI 帮助构造参数，实际发布的 CLI/登录/MCP 组合仍需独立实测。Claude Code、Claude Desktop 和纯 App 对话目前没有此常驻驱动器，继续使用原生对话方式。

离线测试使用可注入的模拟宿主、真实本地子进程和原控制器命令检查，覆盖续跑、独立后台进程、问题等待、真人证据缺失、项目续批、取消、崩溃核对、重复派发拒绝、无进展和账本篡改。它不证明真实模型会始终遵循分工或正确识别每个语义阻塞。

监督器能停止自己拥有的 CLI 进程组。脱离该组的外部进程、原控制器独立检查进程和其他宿主仍按原日志核对，不宣称完整 OS 包容。CLI 报告的 token 只覆盖收到的事件，不是全部子 agent、独立评审或产品调用总额，费用无法完整计算。本地模式继续属于受监督执行；不支持硬费用保证的宿主不得冒充具备该能力。

本仓库开发仍先实现与离线验证，再做真实宿主评估。未来 live evaluation 必须先通过完整离线验证和 `scripts/check_readiness.py --for-live-evaluation`；库存通过不会自动启动任何模型。
