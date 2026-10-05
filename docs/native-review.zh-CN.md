# 原生团队的独立评审

原生对话里的专业 agent 可以读代码、提出安全问题和检查需求覆盖；这些咨询结果
仍然是宿主报告。正式 `review` 检查需要执行评审、绑定当前候选，并导入注册
reviewer 的签名结果。仅创建 reviewer key 不会启动评审器。

`loop_progress` 的 `verification_preflight` 在开发前列出每项正式检查的入口：

- `controller_command`：由原控制器运行项目命令。
- `registered_executor`：已配置匹配类型的浏览器或 HTTP 检查。
- `registered_review`：已明确登记独立 Codex 评审执行器。
- `signed_import`：可以导入外部评估器的签名，但尚无自动执行入口。
- `missing_authority`：缺少当前检查所需的有效评估身份。

登记事实不证明检查通过，也不证明外部服务已连接。真实检查结果仍需对应当前
任务、候选、环境和保留的产物。配置、可执行文件或能力有问题时，进度页显示
具体原因；可以继续已授权且不依赖该评估的工作，终验仍保持待完成。

## 一次性登记

已有 Codex CLI 的项目可在 Loop 仓库执行：

```bash
.venv/bin/python scripts/loop.py host-review-register /Users/super/Documents/ai/macare \
  --state-dir /Users/super/Documents/ai/loop-states/macare \
  --adapter codex --timeout-seconds 180 --max-attempts 3
```

这个命令只登记私有宿主配置，不调用模型，不修改项目任务、engine、spec 或
已有团队。可用 `--executable` 指定 CLI；只有用户已指定模型时才增加 `--model`。
CLI 更新导致登记的执行身份失效时，核对更新后重新登记。

登记按项目路径和私有状态目录生效。macare 的登记不会连接 agent-fabric；为
另一个项目登记时，两处路径都必须换成该项目实际使用的路径。

检查配置：

```bash
.venv/bin/python scripts/loop.py host-review-status /Users/super/Documents/ai/macare \
  --state-dir /Users/super/Documents/ai/loop-states/macare
```

重新连接项目的 `loop-native` MCP，回到原来的聊天，直接说：

> 使用 $loop-engineering，继续当前已确认的 R0。先检查保存的进度和正式评估
> 入口，保留已有成果与历史。按依赖完成剩余安全评审、代码评审和需求验收；
> 通过已登记的执行器运行真实检查。遇到发现就安排对应专业角色处理，在 R0
> 报告后停止，并持续显示进度。

主线程随后通过 `loop_evaluation_run` 执行匹配的正式 `review`，无需用户逐项
复制评审命令。尚未登记的评估仍走签名导入；`human`、GUI、性能负载和其他
产物检查必须使用各自的真实评估入口，代码评审不能代替它们。

## 实际执行与恢复

团队任务之间的依赖与单个 native 任务的内部阶段图是两种配置。新计划在冻结
前交叉校验 task 和 engine；分阶段任务省略 engine 时，准备层提供覆盖全部最终
criteria 的 `delivery` 阶段。显式图必须有效；加入独立评审 criterion 时，准备层
在原有效图之后添加只读评审阶段，保留原阶段。

旧版本已接受“分阶段任务 + 空内部图”的批次，`loop_progress` 会显示
`plan_configuration`，并把下一步指向 `loop_repair_plan`。这项恢复只支持从未
创建子运行的任务；即使初始化失败已保留任务编号，也会继续使用该编号。
修复只补内部阶段、更新冻结 engine 摘要并保留可恢复日志，不改任务契约、
检查、预算、团队依赖、历史签名和已交付成果。显式非空错误图、已经启动的
子运行、冻结资料冲突及暂停/取消均拒绝这种修复。

宿主尚未重新加载工具时，可在 Loop 仓库执行一次：

```bash
.venv/bin/python scripts/loop.py host-plan-repair /path/to/project \
  --state-dir /path/to/private-state --team-id <existing-team-id>
```

加 `--dry-run` 只检查是否符合恢复条件。恢复不调用模型；中断后重用原日志，
有用户文件变化则保留冲突。已接收的文档交付不会被重新建立的任务记录覆盖。
部分写入后暂停时，用户明确恢复会先核对并完成原修复日志，再解除暂停；
输入或源码冲突不会解除暂停，取消仍不可恢复。

每次评审使用新的只读 Codex CLI 上下文和当前候选的独立副本。控制器保留
原检查步骤、需求、任务规则与实际检查产物；执行器不接收签名密钥，也不把
实现者的自评当作评审结论。结构化结果经过验证后，由宿主保存产物、签名并
导入，原来的检查门禁和交接继续决定任务是否完成。

超时、暂停、取消和崩溃不会重置子任务预算。已保存的结果在恢复时导入，避免
重复调用；不确定的执行必须先核对原进程。失败和未明确完成的评审保持可见，
不能自动变为通过。登记的尝试次数限制及子任务剩余时间约束实际执行。

重复查询当前的失败或未明确完成结果不会再次调用模型。先处理实际发现或
缺少的材料，再明确进行有界重试；主线程可在 `loop_evaluation_run` 中传入
`retry=true`，获得新请求和新上下文。尝试次数和原子任务预算继续累计，已通过
的当前评审不会被重复付费执行。

CLI 不能在调用前保证 token 或金额上限，所以有硬花费上限的任务会拒绝此
入口。控制器记录能读到的实际用量，并明确其余未知用量；宿主聊天和原生
子 agent 的用量不在这个评审账本内。

本地副本和新上下文仍共享宿主系统权限。登记与签名是宿主声明的身份和执行
记录，不是操作系统隔离或第三方身份认证。当前自动评审入口支持 Codex CLI；
Claude 原生开发团队也可接这个独立评审器，或继续使用注册外部签名导入。
离线测试使用确定性 CLI 替身和本地进程；真实模型效果需要单独的现场验证。
