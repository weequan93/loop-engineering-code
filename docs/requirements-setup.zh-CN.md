# Setup 阶段的需求采集 agent

开发预设中的 `requirements_reviewer` 在 setup 阶段承担独立的需求采集任务。
用户可以提供想法、现有 docs 或 spec 草案。它先读资料与真实问答记录，
再询问缺少的关键产品信息，生成 `.loop/spec.md`，交给协调者组织开发。
角色库仍是 16 个角色；需求采集是需求角色的专门前置任务。

## 只准备指令，用当前 coding host 对话

在框架目录执行：

```bash
.venv/bin/python scripts/loop.py setup /绝对路径/你的项目
```

这个命令补齐开发预设并给出 `.loop/setup.md` 入口，不启动模型。
在项目的 coding host 中说：

> 读取 `.loop/setup.md`，以需求采集 agent 的身份先读 README 和 docs。
> 根据现有文档准备 spec，只问资料中没有答案的关键问题。先完成需求，
> 再交给协调者；现在不要编写产品代码。

普通 `init --scenario development` 在缺少 spec 时也指向该入口。
现有自定义指令和非空 spec 会保留。不要对活动团队修改配置；已有团队
应使用它的真实 team ID 继续。本模式的对话、编辑和调用由实际 host 管理，
不等同于下面 CLI 控制器的记账和恢复保证。

## 通过 CLI 实际调用需求 agent

以 macare 为例，在框架目录执行：

```bash
.venv/bin/python scripts/loop.py setup /Users/super/Documents/ai/macare --state-dir /Users/super/Documents/ai/loop-states/macare --adapter codex
```

该命令实际调用已安装并登录的 Codex CLI。其他已支持的 command、provider
和 Desktop host 也可以接受 setup 请求，各自保留原有能力和记账限制。
私有 state 必须在业务项目外。只有最初的想法时可以添加
`--brief "面向谁使用、首先解决什么问题"`；这必须是用户的实际意图。

CLI 先读取项目上下文和有界文本资料：README、根目录文档、docs/doc/
documentation 下的 Markdown 等，以及 profile 的 `context_paths`。
根 `AGENTS.md` 和 `LOOP.md` 保持为必需指令。二进制、图片、受排除的秘密文件
不会被当作已阅读的需求资料。超出上下文预算的资料会记录为未包含，不能引用
它们来宣称需求完整；PDF、远程文档和超大规格需要单独的读取集成或准备文本。

用户先查看输出中的 `team_id`、`pending_questions`、`stage` 和
`requirements_setup`。没有足够资料时，agent 每次最多提出 3 个重要问题，
指令要求它不编造产品目标；控制器校验结构、来源引用和阻塞问题，不能从
这些校验单独证明需求语义完整。

## 回答并继续同一轮 setup

把下面占位符替换为输出中的真实 ID：

```bash
.venv/bin/python scripts/loop.py team-answer 实际团队ID --question 实际问题ID --answer "你的实际答案" --state-dir /Users/super/Documents/ai/loop-states/macare
.venv/bin/python scripts/loop.py setup --team-id 实际团队ID --state-dir /Users/super/Documents/ai/loop-states/macare --adapter codex
```

草案位于 `.loop/spec-draft.md`。存在未回答的阻塞问题时，已有正式 spec
保持原样；需求准备完成后，控制器将结果写入 `.loop/spec.md`。
来源引用只能使用实际提供给该次请求的文档、brief 或回答。
setup 使用只返回结构化结果的独立请求，agent 没有直接写业务代码的权限。

返回 `requirements_setup.state: READY` 和 `stage: INTAKE` 时，setup 停止。
这是供用户审阅的需求交接，不表示项目已开发、测试或验收通过。

## 开始开发团队

查看 spec 后，用同一个 team ID：

```bash
.venv/bin/python scripts/loop.py team-run --team-id 实际团队ID --state-dir /Users/super/Documents/ai/loop-states/macare --adapter codex
```

需求采集、问答后的续跑和开发共用原团队的操作次数、时间和用量记录。
默认墙钟限制包含等待回答的时间；续跑不重置额度。Codex、通用 command
和 Desktop 费用保持未知，不能承诺硬金额上限。

直接对空 spec 使用 `team-run 项目路径` 也会先进入需求采集，并在 spec
准备完成后停止；再次用同一 ID 才继续开发。已有非空 spec 的原流程保留。

已准备的 workflow 不能通过 setup 覆写需求；活动团队在同一 state 中会
阻止新的 setup。使用多个 state 时，操作者仍需确认没有其他控制器拥有该项目。
更改冻结的 brief、指令或契约需要停止旧轮次并准备新轮次。

尚未生成开发任务的旧 `INTAKE` 团队也可用 `setup --team-id` 进入需求采集，
保留此前协调调用和问答的记账。已经执行任务、存在未核对操作、处于暂停/终止
状态或有未安装文件日志时会拒绝转换。不能借 setup 重置额度或修改已冻结的任务。

setup 沿用团队的预算、暂停/取消、响应缓存、未知效果核对和文件日志。
文件安装中断可从保存的 old/new 哈希恢复；用户更改目标文件会阻止覆盖。
真实模型的需求理解质量仍需单独验收，离线 fake 响应只能验证这些机制。
