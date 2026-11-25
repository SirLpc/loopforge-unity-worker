# LoopForge × Windows Unity Worker

Windows Worker 在常驻 Unity 工程中执行 Git 同步、Codex 开发、Unity 编译和测试、独立审查及审批交付。LoopForge 管理需求、方案和人工审批，以 HTTP 调度 Worker。每次执行使用同一工程、Library、Git 对象、LFS 与 Package 缓存。

## Windows 部署

在专用的低权限 Windows 账号下运行 Worker。准备已完成首次导入的 NTFS Unity 工程，关闭该工程的 Editor。配置 Git 提交身份、远端凭据及分支保护。Worker 的代码和状态目录放在 Unity 工程之外，限制同机其他账号的写权限。

服务需要 Python 3.11+。从服务目录运行：

~~~~powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.toml config.toml
notepad config.toml
$env:UNITY_WORKER_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Worker Token' -AsSecureString)).Password
$env:UNITY_WORKER_APPROVAL_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Operator Token' -AsSecureString)).Password
.\start_worker.ps1 -ConfigPath .\config.toml
~~~~

两个 Token 各使用至少 32 个 ASCII 字符的独立随机值。Worker Token 交给受信任的 LoopForge SOP。Operator Token 留在授权审批者和 Windows 服务端。只部署旧 smoke 时可省略 Operator Token 和 Codex 配置。服务支持原有变量 UNITY_EXE、UNITY_PROJECT、UNITY_WORKER_LOGS、UNITY_WORKER_TOKEN、UNITY_TIMEOUT_SECONDS，环境变量覆盖 TOML。

Codex 配置使用 Windows 原生 codex.exe。npm 安装的 PowerShell 和 CMD 包装器可以用来查询版本，Worker 进程使用 vendor 目录内的可执行文件。将 UNITY_CODEX_HOME 或 codex_home 指到专用且已初始化的目录。用专用账号和该 CODEX_HOME 完成 CLI 登录，或为服务注入专用 OPENAI_API_KEY / CODEX_API_KEY。每次服务首次用到 Codex 时检测 --version、exec --help 和 sandbox --help。0.172.0 的 MXC 实现及审查权限探针已实测。默认使用 mxc，允许在管理员已预配的机器上选择 elevated。权限探针失败时任务会停止。

Unity 位于仓库子目录时，`project` 指向 Unity 工程，`git_root` 指向独立 Git 检出根。为项目在首次启动前恢复其固定版本依赖。`ignored_cache_paths` 可登记 Git 已忽略的具体依赖缓存路径。`allowed_tracked_outputs` 可登记基础树中的具体历史产物，任务修改这些文件时会被阻断。

配置 `[test_assemblies]` 后，每个开发任务必须提供 `test_selection`。键为 `editmode` 或 `playmode`，值为包含 `assembly` 和 `fixtures` 的列表。填写完整测试类名，由 Worker 生成程序集和测试筛选参数，并在 XML 中核对每个夹具实际运行。省略 `[test_assemblies]` 时沿用既有测试模式。

启动脚本默认监听 127.0.0.1。WSL2 NAT 部署用 Ubuntu 的 ip -4 route show default 确定 Windows 网关，在启动脚本传入 -BindAddress <网关IP> 并将防火墙来源限制到 WSL2/Docker 所需网络。配置受信任链路或 TLS。Docker Desktop 的 host.docker.internal 也可通过健康探针验证。生产运行一个服务实例且 --workers 1，状态目录固定。实例互斥由 Windows 内核对象和数据库租约共同保护。只有 Worker 使用该工程执行 Git 及 Unity 操作。

## 开发与交付

1. 人工在 LoopForge 审批技术方案，提交需求、方案和验收标准。
2. POST /api/v1/tasks 创建持久任务。POST /api/v1/tasks/{task_id}/run 取得租约并执行开发验证。也可逐个运行 prepare、implement、compile、test、review 和 fix。
3. Worker 增量获取配置的基础分支，在原工程建立 ai/task-<UUID>。编译、每个启用的测试模式和独立审查共同验证同一 Git 树。只有确定的编译错误、测试断言或审查修改请求进入自动返工，默认最多三轮。
4. 任务进入 awaiting_approval 后，审阅报告、变更和测试证据。授权者记录审批，指定这一版 revision。Commit Job 会再次比对当前 Git 树，安全暂存明确的文件清单并普通推送任务分支。
5. 完成后运行 release。失败和取消任务也保留租约及现场，工作区整理完成后由操作员释放。下一任务仍使用相同的工程目录。

### API 约定

所有路径使用 Bearer Token。旧入口 GET /health、POST /jobs、GET /jobs/{job_id} 和 loopforge_smoke.py 维持原有请求格式。旧 Job 状态现在可跨重启查询。/health 的 busy_or_blocked 同时反映开发任务租约或遗留 active.lock。

| 方法和路径 | 用途 |
| --- | --- |
| GET /api/v1/workers | 查询 Worker ID、租约及执行阶段 |
| POST /api/v1/tasks | 需要 request_id、requirement、plan、acceptance_criteria 字符串数组。可传 workflow_run_id、project_rules |
| GET /api/v1/tasks/{task_id} | 任务状态、阶段、失败类型、审查、审批和交付 |
| POST /api/v1/tasks/{task_id}/{action} | 固定操作 run、prepare、implement、compile、test、review、fix、pr、cancel、release |
| POST /api/v1/tasks/{task_id}/commit | 需要 request_id 和签批时的 revision。两者和当前批准在同一事务内核对 |
| POST /api/v1/tasks/{task_id}/approve | Operator Token。需要 request_id、revision、approver。可指定 commit_message、create_pr |
| POST /api/v1/tasks/{task_id}/recover | Operator Token。需要 request_id、confirmed_stopped=true、note |
| GET /api/v1/jobs/{job_id} | 轮询 Job 的 running、succeeded、failed、cancelled、recovery_required |
| GET /api/v1/tasks/{task_id}/artifacts | 取任务已登记的产物路径 |
| GET /api/v1/tasks/{task_id}/artifacts/{name} | 下载指定 Job 完成后留存的产物 |
| GET /api/v1/tasks/{task_id}/logs?name=...&offset=0&limit=8192 | 按明确的日志名查询。将 next_offset 带入下一个请求 |

每个操作传唯一 UUID request_id。原样重试会返回同一 Job，变更目标或有效负载会返回 409。失败后明确重新执行安全阶段时，使用新的 request_id。并发冲突、非法阶段和缺少当前版本批准也返回 409。cancel 返回受理结果，随后轮询原 Job，直到确认终态。创建任务只入库，在 prepare 或 run 时竞争租约。

### 工作区与恢复

- 已存在的未知本地文件、非缓存忽略文件、未完成 Git 操作以及不匹配的项目版本会阻断准备。为工程正确配置 Git ignore，Library、Temp、Logs、Obj、UserSettings、IDE 缓存可本地保留。部署使用标准完整 Git checkout。
- LFS 仓库按基础树的 .gitattributes 识别。为主仓库和子模块准备 Git LFS 与过滤器。默认复用已初始化的子模块，检查其中的本地文件和 Git 状态。操作员可显式启用新子模块初始化。任务分支的传输配置、子模块指针及链接变更需要走人工交付。
- 报告中的 revision 是由明确文件清单生成的 Git 树。编译或测试中 Unity 若新写出 .meta、依赖锁文件等改动，检查留存改动后重新 compile。提交时再次校验实际树。Git 差异超出 max_diff_bytes 时保留现场供人工处理。
- 普通失败保持分支、未提交修改和日志。操作员可在该任务分支本地归档，或移出未知文件，再调用 release。旧进程未确认退出时保持 recovery_required。检查进程树、项目锁、Git 状态和远端任务分支，记录核验事实后调用 recover，再按状态执行 release 或重试已记录 SHA 的 push。旧 MVP 留下 active.lock 时，停服并核验以上条件后由操作员处置该文件。
- 工具子进程由 Windows Job Object 管理。系统服务、第三方 broker、同机外部 Editor 及 Git 客户端仍需按部署约束管理。没有确认进程停止的 Job 保留租约。
- 日志及 XML 位于状态目录。下载与轮询会掩码当前服务的已知 Token。专用账号及状态目录 ACL 用于保护原始工具日志、代码差异和任务数据。Unity 会以 Worker 账号执行项目中的 Editor 和测试代码。部署仅供受信任的项目、代码和 SOP 调用方使用，并将账号权限及网络范围限制在本任务所需资源。
- 可选交付适配器通过 UNITY_PR_PROVIDER=github 或 gitlab、UNITY_PR_API_URL、UNITY_PR_REPOSITORY、UNITY_PR_TOKEN 配置。GitHub 默认 API 为 https://api.github.com，GitLab 填站点的 HTTPS /api/v4。审批时显式选择 create_pr。未启用适配器时以基础分支、任务分支和 Commit SHA 手动发起 PR/MR。适配器请求失败可在释放前用新 request_id 重试 pr。

## 测试和 LoopForge

~~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~~

Git for Windows 本地传输集成测试需要 Windows 沙箱外的普通权限执行。完整测试使用本地临时 bare Git 远端及模拟 CLI。真实 Unity 验收和故障恢复步骤见 [端到端验收清单](docs/ACCEPTANCE.md)。

LoopForge 的凭据绑定、方案产物、脚本节点和双审批配置见 [SOP 接入](integrations/loopforge/README.md)。实施步骤和测试覆盖记录见 [实施计划](IMPLEMENTATION_PLAN.md)。
