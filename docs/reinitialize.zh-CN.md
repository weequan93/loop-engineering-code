# 重新设置开发团队

下面的命令在框架目录执行。`LOOP_PROJECT` 是你的业务项目，`LOOP_SPEC`
是完整需求文件；`LOOP_STATE` 必须在业务项目外，并填写此前实际使用的状态目录。
需求至少说明目标、范围、兼容要求、验收场景和必须执行的质量检查。
当前预设包含[16 个专业 agent 的完整流程](development-agents.zh-CN.md)及自动兼任职责上下文。

```bash
cd /Users/super/Documents/ai/loop-engineering-code
LOOP_PROJECT="/绝对路径/你的项目"
LOOP_SPEC="/绝对路径/spec.md"
LOOP_STATE="/绝对路径/项目外的私有状态目录"
```

## 首次使用

Python 3.11 以上，并安装项目的离线验证依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
codex --version
codex login status
```

未登录时执行 `codex login`。准备好项目目录和实际需求文件后：

```bash
.venv/bin/python scripts/loop.py init "$LOOP_PROJECT" \
  --scenario development --spec "$LOOP_SPEC"
```

## 已有项目重新初始化

先查询旧团队：

```bash
.venv/bin/python scripts/loop.py team-runs --state-dir "$LOOP_STATE"
```

对仍未结束的旧团队，填入它的真实 ID 并取消，等待正在运行的控制器退出：

```bash
.venv/bin/python scripts/loop.py team-cancel 原团队ID --state-dir "$LOOP_STATE"
```

然后重新初始化：

```bash
.venv/bin/python scripts/loop.py reinit "$LOOP_PROJECT" \
  --scenario development --spec "$LOOP_SPEC" --state-dir "$LOOP_STATE"
```

`reinit` 会把旧 `.loop` 配置移入状态目录的 `setup-backups/setup-…/.loop`，
保存需求副本及设置记录，再安装当前开发场景。项目代码、测试、已有 `LOOP.md`
和旧团队的历史、用量记录保留。安装失败会回滚旧配置，并保存未完成的新配置。
输出的 `backup_directory` 是实际备份位置。

未停止的团队、未处理的进程/编辑效果或已取消但仍未清理的操作会阻止重设。
按照其状态进行检查和 reconciliation 后再执行；重新设置不会替旧操作声明成功。
该命令检查你提供的状态目录；使用过其他状态目录时也要先停止其中的控制器。
如果进程在设置中途崩溃，先检查该备份里的 `setup.json`：`ARCHIVED` 表示旧配置
已经保存而安装尚未确认。保留当前新配置，再恢复备份 `.loop` 或重新执行初始化。

## 启动 Codex 团队

可在新团队启动前编辑 `.loop/team-policy.json`，确定并行数、调用次数、返工次数
和时间上限。默认两名并行工作者、40 次操作、3 轮返工、总计一小时。
新开发场景的 `context_max_bytes` 默认为 `120000` 字节。普通 `init` 会保留
已有上限；多角色项目需要完整职责及各阶段证据时，可在冻结前显式调整
`.loop/project.json`。必需上下文超过上限会拒绝请求。
强制 token/金额上限需要能够提供可信计数及定价的 provider；Codex/Desktop 的
费用未知，会拒绝这种硬上限配置。

```bash
.venv/bin/python scripts/loop.py doctor "$LOOP_PROJECT"
.venv/bin/python scripts/loop.py team-run "$LOOP_PROJECT" \
  --state-dir "$LOOP_STATE" --adapter codex
```

协调者读取 spec、选择专业角色、提出关键问题，并准备有实际检查的任务。
`doctor` 对未拆解的 spec 显示“协调者尚需准备任务”属于正常初始化状态。
控制器会记录并输出新 `team_id`，依次调度实现、检查、独立评审和验收。

## 回答问题并继续同一个团队

从输出填写真实团队 ID 和问题 ID：

```bash
LOOP_TEAM_ID="team-实际ID"
LOOP_QUESTION_ID="实际问题ID"
.venv/bin/python scripts/loop.py team-answer "$LOOP_TEAM_ID" \
  --question "$LOOP_QUESTION_ID" --answer "你的实际答案" --state-dir "$LOOP_STATE"
.venv/bin/python scripts/loop.py team-run --team-id "$LOOP_TEAM_ID" \
  --state-dir "$LOOP_STATE" --adapter codex
.venv/bin/python scripts/loop.py team-status "$LOOP_TEAM_ID" --state-dir "$LOOP_STATE"
```

回答与续跑保留累计预算。已有冻结团队的需求/范围改变时，先结束它，再按上面的
重设流程启动经过重新检查的新团队。必需的浏览器、人工、负载或外部制品检查
需要真实执行器；代理给出的说明不会自动变成通过证据。

Claude Desktop 的本地工具配置及启动命令见 [Desktop MCP 接入](desktop-mcp.md)。

依赖准备、浏览器交互验证、HTTP 性能检查和新版文件操作的配置方法见[执行工具与重新设置指南](execution-tools.zh-CN.md)。
