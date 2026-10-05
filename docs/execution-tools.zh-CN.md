# 开发团队执行工具与重新设置

开发预设现在包含 `.loop/execution-tools.json`。它负责依赖准备、浏览器交互验证和有界 HTTP 性能检查。默认配置为空，不会自动安装软件、访问网站或启动模型。已有项目的普通 `init` 保留旧配置；需要导出新版完整预设时使用 `reinit`。

## 重新初始化

在框架目录运行以下命令，将示例绝对路径替换成自己的项目、私有状态目录和需求文件。状态目录必须在业务项目之外，旧历史与用量会保留。

```bash
cd /Users/super/Documents/ai/loop-engineering-code
.venv/bin/python scripts/loop.py team-runs --state-dir /绝对路径/私有状态
# 若有未完成的旧团队，先查看、取消；有未解决的进程或安装操作时先检查并 reconcile。
.venv/bin/python scripts/loop.py team-cancel TEAM_ID --state-dir /绝对路径/私有状态
.venv/bin/python scripts/loop.py reinit /绝对路径/业务项目 \
  --scenario development --spec /绝对路径/spec.md --state-dir /绝对路径/私有状态
.venv/bin/python scripts/loop.py doctor /绝对路径/业务项目
```

`reinit` 将旧 `.loop` 归档到私有状态目录，保留业务源码、旧任务历史和累计预算。它会拒绝尚未解决的团队进程、文件变更、依赖安装或验证器操作。[重新设置指南](reinitialize.zh-CN.md)说明查看和恢复归档的方法。

完成下面需要的工具配置后再启动新团队：

```bash
.venv/bin/python scripts/loop.py team-run /绝对路径/业务项目 \
  --adapter codex --state-dir /绝对路径/私有状态
```

运行时按 agent 提出的问题提供答案。Claude Desktop 的接入见 [Desktop MCP 指南](desktop-mcp.md)。缺少真实人工判断、扫描工具或执行环境时，该项验收保持待处理。

## 依赖准备

在新团队启动前编辑业务项目的 `.loop/execution-tools.json`。例如使用已安装的 npm 和已有锁文件：

```json
{
  "schema_version": "1.0",
  "provision": {
    "inputs": ["package.json", "package-lock.json"],
    "output_dirs": ["node_modules"],
    "steps": [{"id": "npm-ci", "argv": ["npm", "ci", "--ignore-scripts"], "cwd": ".", "timeout_seconds": 120}],
    "max_output_files": 20000,
    "max_output_bytes": 268435456
  },
  "executors": []
}
```

确认锁文件存在，输出目录被 `.loop/project.json` 的 `snapshot.exclude` 排除。实际安装命令、退出码、耗时与日志写入私有状态；每个工作副本独立准备，检查副本获得经过哈希和文件模式验证的依赖复制。依赖或锁文件的身份参与验证环境摘要。已完成的安装不会无条件重复，经过批准的锁文件变更可触发新安装并保留旧记录。

安装不得修改业务源码或锁文件。安装器的真实行为仍由本机操作系统权限决定，修改检查是事后验证。失败、中断或未知效果不会自动重试；`status` 和 `team-status` 提供操作 ID 与日志。检查并停止旧进程，恢复被安装器改动的源码，再使用：

```bash
.venv/bin/python scripts/loop.py reconcile RUN_ID --action-id PROCESS_ID \
  --note '已检查安装日志、恢复源码并确认旧进程组停止' --state-dir /绝对路径/私有状态
.venv/bin/python scripts/loop.py resume RUN_ID --state-dir /绝对路径/私有状态
```

启动前就失败的安装使用记录中的 `id`；已启动的安装使用 `action_id`。重试次数、输出文件数、字节数、单步时限和累计任务时间都有上限。监视进程日志与事后文件扫描不能保证磁盘使用从不瞬时超出阈值。

Python 虚拟环境应使用 `python3 -m venv --copies .venv`，检查命令优先 `.venv/bin/python -m pytest`。框架拒绝指向声明输出目录之外的依赖链接；依赖外部包缓存的 pnpm 链接、系统 Python 链接和包含绝对旧路径的环境可能无法搬迁。带绝对 shebang 的入口脚本需要改用复制环境的解释器。受保护容器使用预先准备的镜像依赖；本机安装器不跨越容器边界。

## 浏览器交互

将下面项目加入配置的 `executors`，填入本机已安装且可信的 Node、Playwright 包目录和浏览器可执行文件的绝对路径。示例路径需要替换；框架不会自动下载浏览器。

```json
{
  "kind": "browser",
  "check_id": "ui-flow",
  "timeout_seconds": 30,
  "node": "/绝对路径/node",
  "playwright_module": "/绝对路径/node_modules/playwright",
  "browser_executable": "/绝对路径/浏览器可执行文件",
  "document": "web/index.html",
  "url": null,
  "allowed_origins": [],
  "steps": [
    {"action": "fill", "selector": "#name", "value": "示例用户"},
    {"action": "click", "selector": "#submit", "value": null},
    {"action": "assert_text", "selector": "#status", "value": "保存成功"}
  ],
  "viewport": {"width": 1280, "height": 800}
}
```

`document` 使用当前候选的 HTML 文档，适合独立页面。框架复制并验证文档后，用 `page.setContent` 加载。复杂 SPA、相对资源和后台 API 使用已运行的测试服务：设置 `document: null`、`url` 和完整 `allowed_origins`；本机启动服务可使用已有项目流程。URL 服务响应属于外部状态，当前版本不能证明该服务部署的代码就是候选代码。需要此关联时，另设实际构建、部署身份或人工验收检查，不把 URL 检查当成代码身份的证明。

支持 `click`、`fill`、`press`、`assert_text`、`assert_visible`、`assert_count`。文字断言要求精确文本，数量断言使用十进制字符串。至少一个断言必需；只有点击或截图不能通过。每项配置指定一个视口，移动端可另设一个检查 ID。

执行器创建独立、无持久用户配置的无头浏览器，保存断言观察、PNG 截图与 Playwright trace。页面请求仅允许声明的 HTTP(S) origin；重定向的目标也须声明，WebSocket 被关闭，Service Worker 和下载被禁用。越过目标限制的请求使检查失败。此路由规则不是操作系统网络隔离，也不固定 DNS、远端服务状态或浏览器全部库文件。

本机路径、执行器脚本、Node/浏览器二进制和 Playwright 版本文件参与环境摘要；包目录的全部依赖闭包未被穷尽识别。缺 SDK、浏览器启动失败、时限耗尽、捕获缺失等返回无法判定。截图和 trace 保留实际交互证据，视觉设计是否合格仍使用独立评审或人工判断。API 依据：[Playwright BrowserContext](https://playwright.dev/docs/api/class-browsercontext)、[Page](https://playwright.dev/docs/api/class-page)。

## HTTP 性能检查

另一个 `executors` 项目示例：

```json
{
  "kind": "http_load", "check_id": "api-load", "timeout_seconds": 20,
  "url": "http://127.0.0.1:8080/health", "allowed_origins": ["http://127.0.0.1:8080"],
  "requests": 100, "concurrency": 4, "requests_per_second": 20,
  "request_timeout_ms": 1000, "max_response_bytes": 65536, "expected_status": 200,
  "thresholds": {"max_p95_ms": 200, "max_errors": 0, "min_successes": 100}
}
```

仅使用已授权的测试目标。它执行 GET 请求，禁止自动重定向和环境代理，最多 1000 请求、8 并发、100 请求/秒、120 秒总时限，单个响应最多 1 MiB。阈值必须提前声明；保留逐请求状态、耗时和错误，以及 p50/p95/p99、实际请求数和阈值结论。延迟以整数微秒保存，配置阈值单位为毫秒；总耗时包含速率调度。父进程根据样本重新核对指标，缺样本、错状态、超阈值不能靠声明通过。

这是有界 HTTP 工作负载，不能覆盖长期浸泡、分布式压力、浏览器性能分析或完整安全扫描。GET 接口的远端副作用仍须由操作者确认；框架不猜测生产目标。实现使用标准库 [urllib.request](https://docs.python.org/3/library/urllib.request.html)。

## 任务与验收绑定

浏览器配置的 `check_id` 必须匹配任务的 `interaction` 检查；HTTP 配置的 `check_id` 必须匹配 `artifact` 检查。任务的 `procedure` 和 `criteria` 应说明实际交互或性能阈值。native engine 必须登记相应 `interaction` 或 `artifact` evaluator key；团队协调器会根据真实任务类型登记私有权限。没有匹配的检查、执行器或权限就拒绝执行。模型收到工具配置与可用检查清单，但不能代替执行器签发证据。

团队执行会自动完成匹配检查；手动 native run 可运行：

```bash
.venv/bin/python scripts/loop.py evaluation-run RUN_ID --check-id ui-flow --state-dir /绝对路径/私有状态
.venv/bin/python scripts/loop.py resume RUN_ID --state-dir /绝对路径/私有状态
.venv/bin/python scripts/loop.py verify RUN_ID --state-dir /绝对路径/私有状态
```

执行时固定候选、任务、检查、工具计划和环境。进程受取消信号与剩余时限管理。签名只由私有宿主包装层产生，子进程收不到密钥。已签名且缓存的结果在导入中断后可以恢复；未知效果保持 `RUNNING` 并要求检查后 reconcile。工件改动、工作副本改动、错候选、错误指标或目录越界使导入失败。最终集成后重新运行检查，不复用工作分支证据证明交付代码。

工具配置也参与团队选择依据与冻结输入：启动后改变配置会阻止继续执行，必须重新准备明确的新团队。人工验收、独立代码审查和必要的安全检查仍按各自真实证据要求执行。本机安装及这两类执行器目前只支持监督下的 local runtime；`unattended` 与 Docker 自动路由会拒绝。

## 删除与二进制文件

默认 AgentStep `0.2` 保持完整 UTF-8 文件写入。需要删除或二进制资产时，在任务扩展中明确设置：

```json
{"agent_step_version": "0.3"}
```

这属于任务的 `extensions` 字段，不替换其他任务字段。AgentStep `0.3` 的每项变更都包含 `path`、`expected_sha256`、`operation`、`encoding`、`content`。写入使用 `operation: "write"` 与 `utf-8` 或规范 `base64`；删除使用 `operation: "delete"`、旧文件哈希、`encoding: null`、`content: null`。二进制解码后最多 1 MiB，并受任务总变更字节上限约束。

生成 schema、提案校验、文件日志和并行集成都使用同一版本。删除和二进制写入仍受写入范围、受保护测试、旧内容哈希、整组预检和旧/新哈希恢复控制。用户后来重新创建或改动文件会停止恢复；恢复不会覆盖它。符号链接、文件模式变更和 Git 子模块仍不属于这版变更协议。

框架当前实现已完成这些支持路径的离线验证；真实模型能力、真实 Desktop 聊天兼容性和生产部署性能分别验证，不由角色文件或离线替身证明。
