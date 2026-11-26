# LoopForge SOP 接入

本机 Ubuntu Compose 运行 loopforge-platform:local，源码工作树为 2ad5659be8844fe4a4dfd12253b3584d9e4f1d14。该工作树和官方 main 已核对 Python/Shell 脚本、项目凭据、上游产物挂载和人工审批机制。容器标签不包含镜像构建提交，升级后用下面的 health 和产物探针重新核对。 [LoopForge 官方 README](https://github.com/Tencent/LoopForge) [WorkflowNode API 契约](https://github.com/Tencent/LoopForge/blob/main/docs/api/openapi.yaml)

## 项目和凭据

1. 在 LoopForge 新建受信任的项目，保留空仓库绑定。脚本执行环境选 generic-docker 或已验证可访问 Windows Worker 的现有 Docker 沙箱。先运行内置 script & approval 演示以确认节点类型、权限和人工审批状态。
2. 以 Windows 主机在 Docker 容器内的可达地址设置 UNITY_WORKER_URL。可以在沙箱环境设置 URL，或用项目凭据中的自定义变量一起交付。将普通 UNITY_WORKER_TOKEN 保存为项目凭据，复制或建立项目 SOP 并勾选需要的凭据。ProjectCredentialIDs 是 SOP 范围。把凭据绑定到仅含受信任脚本与人工门的执行 SOP。需求分析和方案 Agent 在凭据未绑定的流程中运行。旧版缺少项目凭据时，用受保护的沙箱环境或外部安全注入。
3. Operator Token 保存在人工审批者的客户端及 Windows Worker。交付脚本使用普通 Token，只将已经人工写入 Worker 的审批推进到 commit。 [凭据解析源码](https://github.com/Tencent/LoopForge/blob/main/internal/engine/project_credentials.go) [项目凭据 UI 源码](https://github.com/Tencent/LoopForge/blob/main/web/app/project-credentials.js)

## 从仓库安装 SOP

从 Worker 仓库根目录运行。`--worker-url` 使用 LoopForge 容器能访问的地址。`--probe-url` 可单独指定本机能访问的 Worker 地址。下面的预检只读取 Worker 的注册状态，结果写入本机 `state/loopforge/worker-preflight.json`。

~~~~powershell
$workerUrl = 'http://WINDOWS-HOST:8765'
$probeUrl = 'http://127.0.0.1:8765'
.\.venv\Scripts\python.exe -m integrations.loopforge.install_sops --preflight --config .\config.toml --worker-url $workerUrl --probe-url $probeUrl
~~~~

准备目标 LoopForge 项目的 Owner API Key，安全注入到环境变量 `UNITY_FORMAL_STUDIO_TOKEN`。按目标项目设置 `--project-id`、`--api-url`，自定义脚本环境用 `--sandbox-profile`。省略 `--pool` 会使用 Worker 配置的默认池。

~~~~powershell
.\.venv\Scripts\python.exe -m integrations.loopforge.install_sops --config .\config.toml --project-id GAME-PROJECT --api-url 'http://LOOPFORGE-HOST:8090' --worker-url $workerUrl --probe-url $probeUrl
~~~~

默认保存草稿，已存在的凭据和已发布 SOP 保留原设置。按[端到端验收清单](../../docs/ACCEPTANCE.md)确认 Unity 编译、定向测试和产物交接后，追加 `--publish` 更新并发布目标项目中由本安装器管理的 SOP，设置项目默认入口。安装器会重查 Worker、目标池和 LoopForge 执行器。操作员验证交付 revision 后用 `scripts/approve-delivery.ps1` 签批。它使用本机的 Operator Token，或从 `-CredentialDirectory` 读取对应 DPAPI 文件。

### 启用 Windows 提前规划

在运行专用 LoopForge Docker Runner 的 Linux 或 WSL 主机上克隆同一仓库。用能读取该 Runner 的 Docker 容器元数据及工作目录的账号运行以下中继。将平台 API Token 和独立的至少 32 字符签名密钥保存在该账号可读的受保护文件中。工作目录指向 Runner 主机上实际挂载到容器 `/workspace` 的根目录，下面应有 `runs/<run-id>/workspace`。

~~~~bash
python3 -m integrations.loopforge.planning_context \
  --project-id GAME-PROJECT \
  --platform-url http://127.0.0.1:8090 \
  --platform-token-file "$HOME/.config/unity-worker/loopforge-token" \
  --context-token-file "$HOME/.config/unity-worker/planning-context.key" \
  --runner-workspaces /path/to/dedicated-runner/workspaces
~~~~

让中继随 Runner 常驻，项目 ID 与安装器保持一致。在运行安装器的进程里通过 `UNITY_PLANNING_CONTEXT_TOKEN` 注入同一签名密钥，再追加 `--windows-planning`。中继向当前容器的工作目录写入短时有效的签名记录，用于绑定需求、审批状态和当前计划产物的摘要。用测试 SOP 验证规划、打回重做和审批后交接，再更新正式项目。

## 生成 Script 节点内容

在服务源代码目录用 python3 integrations/loopforge/worker_client.py render <命令> [参数] 生成节点正文，将输出完整复制进 SOP 的 Script 节点，语言选 Python。每个节点使用同一客户端实现。LoopForge 将 Python 节点正文交给 python3 -c，stdout 出现在节点输出。JSON 文件由节点产物定义收集。根目录执行，工作流运行目录按节点依赖挂载上游产物。 [脚本执行源码](https://github.com/Tencent/LoopForge/blob/main/internal/engine/node_execute.go) [产物挂载源码](https://github.com/Tencent/LoopForge/blob/main/internal/engine/artifacts.go)

~~~~bash
python3 integrations/loopforge/worker_client.py render health
python3 integrations/loopforge/worker_client.py render develop \
  --input '.agentflow/inputs/**/unity-plan.json' --output unity-task.json
python3 integrations/loopforge/worker_client.py render deliver \
  --from-artifact '.agentflow/inputs/**/unity-task.json' --output unity-delivery.json
python3 integrations/loopforge/worker_client.py render release \
  --from-artifact '.agentflow/inputs/**/unity-delivery.json' --output unity-release.json
~~~~

运行目录中的上游 JSON 路径由 LoopForge 使用 .agentflow/inputs/<节点控制键>/<产物名称>.json 生成。产物名称设置为 unity-plan、unity-task、unity-delivery、unity-release，保存路径填写相应的 .json 文件名，媒体类型 application/json。节点确保能解析到唯一产物。若已知完整路径，把生成命令中的通配路径换成实际路径。也可将批准的 JSON 放入执行节点的运行目录，让 --input 指向该文件。审批后的 deliver 可在可信客户端通过 --task-id 继续。

## 节点顺序及审批

### Windows Worker 提前接手

开发类 SOP 调用 `POST /api/v1/planning-tasks`，传 `request_id`、`workflow_run_id`、`requirement`、`planning_profile` 和可选 `pool_id`。`planning_profile` 支持 `feature`、`ui` 和 `issue-fix`。节点调用 `POST /api/v1/tasks/{task_id}/plan`，提供阶段请求 ID、可选 `queue_wait_seconds` 和返工意见 `feedback`。Worker 分配目录，在固定基础提交上只读调查，并按需直接运行 Unity BatchMode。成功的任务进入 `awaiting_plan_approval`，报告保存在 `planning` 和任务产物中。

LoopForge 人工审批后，可信脚本调用 `POST /api/v1/tasks/{task_id}/accept-plan`，提供阶段请求 ID 及获批报告的 `slot_id`、`base_commit` 和 `plan_digest`。`run` 接续同一任务。方案返工保留目录并生成新的摘要。取消时等待进程终止，再用 `release` 检查并释放目录。交付版本使用独立的操作员签批流程。

池内槽位 ID 和路径均由 Worker 配置提供。`max_parallel_unity` 控制全机 Editor 并发，独立于池可接任务的数量。审批等待期间只保留目录租约。正式启用更高上限前，在两份独立的验收工程上用 `tests/windows_batch_overlap.py` 核对真实进程重叠、每份测试 XML 和内存。

### 既有方案交接

| 顺序 | 节点 | 输入和输出 |
| --- | --- | --- |
| 1 | 需求分析和技术方案 | 生成 unity-plan.json，见示例。每个新需求运行生成独立 request_id，重试原计划时保持相同 ID。能获取实际 LoopForge 运行 ID 时填写 workflow_run_id |
| 2 | 人工审批 | 审阅方案和验收标准。将批准的 unity-plan.json 交给执行 SOP |
| 3 | develop | 读取方案，调用 create 和 run，自动执行 Git、Codex、Unity、Review 与有限返工。声明 unity-task.json 产物 |
| 4 | 人工交付审批 | 根据 unity-task.json 中的任务、revision 和 artifact_reference 核对报告。在可信客户端记录 Worker 审批，再在 LoopForge 审批门批准 |
| 5 | deliver | 消费 develop 的 unity-task.json，验证 Worker 已批准同一 revision，调用 commit，声明 unity-delivery.json |
| 6 | release | 消费交付产物，运行安全清洁检查，声明 unity-release.json |

将需求分析与方案留在无 Windows 凭据的 SOP，方案节点把 unity-plan.json 写到运行根目录。该工作树的子 SOP 复用父运行的 workspace，可让通过方案审批后的子 SOP 以 --input unity-plan.json 读取该文件。启用此连线时核对子 SOP 中能读到同一文件及是否注入了自己的项目凭据。若采用独立运行则交接批准的文件，或用可信 Script 节点放进该运行的根目录。 [子 SOP 源码](https://github.com/Tencent/LoopForge/blob/main/internal/engine/sop_node.go)

最小方案文件如下：

~~~~json
{
  "request_id": "176df8c2-816e-44d9-85df-22b76f2ae837",
  "workflow_run_id": "replace-with-actual-loopforge-run-id",
  "requirement": "在独立 Unity 验收工程中修改问候脚本",
  "plan": "只修改问候脚本及其对应测试，遵循工程 AGENTS.md",
  "acceptance_criteria": ["问候文本与需求一致", "EditMode 和 PlayMode 验证通过"]
}
~~~~

授权者可在可信客户端用 Operator Token 记录审批。revision 取自 develop 的 stage_result.revision，任务 ID 取自 task_id。需要 PR/MR 时在已配置适配器的 Worker 使用 --create-pr。

~~~~bash
python3 integrations/loopforge/worker_client.py approve \
  --task-id '<task_id>' --revision '<revision>' --approver '<操作者>'
~~~~

需要逐阶段 SOP 时，用同一 render 命令生成 create、prepare、implement、compile、test、review、fix 的节点脚本。每个脚本通过 --from-artifact 读取上一份 JSON，并为 --output 设置唯一文件名和产物名称。develop 节点处理自动返工。拆分流程需要显式处理失败边和重试。脚本节点的产物只交接任务标识、状态、修改清单、审查结论及 Windows 产物引用。

## 目录池和排队

```bash
python3 integrations/loopforge/worker_client.py render develop \
  --input '.agentflow/inputs/**/unity-plan.json' --output unity-task.json \
  --pool game --queue-wait-seconds 18000 --wait-seconds 18000
```

把 `game` 换成 Worker 已配置的池 ID，由可信执行脚本固定。用户方案继续存放需求和测试范围。`unity-task.json` 的 `pool_id`、`slot_id` 和 `base_commit` 用于核对目录绑定和开发基线。排队期间 `slot_id` 为空。交付用原任务 ID、目录标识和审批 revision 接续。

先升级 Worker，再发布支持排队的执行脚本。单目录入口和已运行的旧 Job 保持可查询。`--queue-wait-seconds` 与 `--wait-seconds` 分别控制目录等待和执行轮询。`queue_timeout` 后需要新的 `--attempt`。对断线的原节点重跑时复用同一任务和 Job。

## 超时、重试和取消

Worker 与 LoopForge 各自配置超时。为 Git、Codex、Unity 和可能的三次返工预留时间。客户端可用 --wait-seconds 或 UNITY_WORKER_WAIT_SECONDS 调整，--follow 将脱敏日志附加到 SOP 输出。稳定请求 ID 让同一节点重试复用原 Job。确认可安全重跑时用 --attempt 1 等新编号。

节点停止时，脚本尽力请求 cancel。容器强制终止或断网时，用已打印的 task_id 和 job_id 查询 Windows 服务。在 Worker 确认原 Job 进入终态以前保留租约。失败处理通过 status 或 failure 导出状态，必要时调用 cancel，等操作员归档后再 release。

~~~~bash
python3 integrations/loopforge/worker_client.py failure --task-id '<task_id>'
python3 integrations/loopforge/worker_client.py cancel --task-id '<task_id>'
python3 integrations/loopforge/worker_client.py release --task-id '<task_id>'
~~~~
