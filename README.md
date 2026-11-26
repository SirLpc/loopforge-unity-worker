# LoopForge × Windows Unity Worker

Windows Worker 在常驻 Unity 工程中执行 Git 同步、Codex 开发、Unity 编译和测试、独立审查及审批交付。LoopForge 管理需求、方案和人工审批，以 HTTP 调度 Worker。每单固定使用分配到的常驻工程及其 Library、Git 对象、LFS 与依赖缓存。支持单目录，也可配置多个命名池。

## 从克隆部署到 Windows

使用专用的 Windows 账号。准备 Python 3.11+、Git for Windows、项目指定版本的 Unity Editor 和 Windows 原生 `codex.exe`。如果游戏仓库使用 Git LFS 或子模块，先准备相应工具和远端权限。

1. 克隆这个 Worker 仓库。为每个槽位独立克隆游戏仓库，按游戏项目的说明还原依赖、完成首次导入，再关闭这些工程的 Editor。
2. 在 Worker 仓库根目录初始化。带 `-Pool` 会采用[两槽位模板](config.pools.example.toml)，省略则采用[单工程模板](config.example.toml)。重复初始化会保留已有配置。Python Launcher 不可用时传 `-PythonExe`。
3. 在 `config.toml` 填写本机的 Unity、Git 检出、日志和 Codex 路径。进入游戏检出前核对每份目录的基础分支和 Git 远端。在配置的 `CODEX_HOME` 登录 Codex。
4. 设置两个 Token 并启动。需要从 LoopForge 容器访问时，按下文配置监听地址和防火墙规则。

~~~~powershell
.\scripts\initialize-worker.ps1 -Pool
notepad .\config.toml
$env:UNITY_WORKER_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Worker Token' -AsSecureString)).Password
$env:UNITY_WORKER_APPROVAL_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Operator Token' -AsSecureString)).Password
.\start_worker.ps1 -ConfigPath .\config.toml
~~~~

启动脚本直接运行当前 Git 检出的 Worker 代码。配置和状态留在本机。实际 Token、DPAPI 文件、Codex 登录目录、SQLite、任务产物和游戏检出应放在源码提交之外。

修改 Worker 代码后，在同一 Windows 登录会话的另一个 PowerShell 中执行 `.\scripts\restart-worker.ps1`。如果启动时指定了 `-Port`，重启时传相同的端口。Worker 暂停接收新的 Job，等正在运行的 Job 完成后以原配置和凭据重启，排队中的 Job 由新进程继续调度。已有实例需要先用新版 `start_worker.ps1` 启动一次。

两个 Token 各使用至少 32 个 ASCII 字符的独立随机值。Worker Token 交给受信任的 LoopForge SOP。Operator Token 留在授权审批者和 Windows 服务端。只部署旧 smoke 时可省略 Operator Token 和 Codex 配置。服务支持原有变量 UNITY_EXE、UNITY_PROJECT、UNITY_WORKER_LOGS、UNITY_WORKER_TOKEN、UNITY_TIMEOUT_SECONDS，环境变量覆盖 TOML。

Codex 配置使用 Windows 原生 codex.exe。npm 安装的 PowerShell 和 CMD 包装器可以用来查询版本，Worker 进程使用 vendor 目录内的可执行文件。将 UNITY_CODEX_HOME 或 codex_home 指到专用且已初始化的目录。用专用账号在该 CODEX_HOME 登录一次，或为服务注入专用 OPENAI_API_KEY / CODEX_API_KEY。该 Worker 的所有池和槽位共用这份登录，每个任务仍启动独立 Codex 会话。每次服务首次用到 Codex 时检测 --version、exec --help 和 sandbox --help。0.172.0 的 MXC 实现及审查权限探针已实测。默认使用 mxc，允许在管理员已预配的机器上选择 elevated。权限探针失败时任务会停止。

Unity 位于仓库子目录时，`project` 指向 Unity 工程，`git_root` 指向独立 Git 检出根。为项目在首次启动前恢复其固定版本依赖。`ignored_cache_paths` 可登记 Git 已忽略的具体依赖缓存路径。`allowed_tracked_outputs` 可登记基础树中的具体历史产物，任务修改这些文件时会被阻断。

配置 `[test_assemblies]` 后，每个开发任务必须提供 `test_selection`。键为 `editmode` 或 `playmode`，值为包含 `assembly` 和 `fixtures` 的列表。填写完整测试类名，由 Worker 生成程序集和测试筛选参数，并在 XML 中核对每个夹具实际运行。省略 `[test_assemblies]` 时沿用既有测试模式。

启动脚本默认监听 127.0.0.1。WSL2 NAT 部署用 Ubuntu 的 ip -4 route show default 确定 Windows 网关，在启动脚本传入 -BindAddress <网关IP> 并将防火墙来源限制到 WSL2/Docker 所需网络。配置受信任链路或 TLS。Docker Desktop 的 host.docker.internal 也可通过健康探针验证。生产运行一个服务实例且 --workers 1，状态目录固定。Windows 内核对象独占状态目录和每个 checkout，SQLite 租约将任务绑定到一个目录。只有 Worker 使用这些工程执行 Git 及 Unity 操作。

## 目录池

1. 为同一个受信任仓库预备至少一份独立 Git 检出。每份使用私有的 `.git` 和 Unity `Library`/`Temp`。通过预热确认 Unity 版本、依赖、Git LFS 和选定测试均可用。顶层 `codex_home` 或 `UNITY_CODEX_HOME` 指向所有槽位共用的登录目录，位置在所有检出和 Worker 状态目录之外。
2. 按 [两槽位模板](config.pools.example.toml) 设置 `default_pool`、`legacy_slot`、`[pools.<id>]` 与各 `slots`。显式池启用每个目录的预检。若 `GET /api/v1/pools` 显示 `unavailable`，检查该 checkout 的远端、Unity 版本、路径、Editor 锁和 Git 清洁度，修复后重启复检。
3. `remote_identity` 是 UTF-8 `git remote get-url origin`、一个 NUL 字节、`git remote get-url --push origin` 的 SHA-256 小写十六进制。仅比较摘要。命名池需固定仓库、基础分支、Unity 版本和相对工程路径。第二个仓库用独立的池 ID。工作流用可信 `--pool` 参数选池。
4. `max_parallel_unity` 默认 1，限制该 Worker 管理的 Editor 进程。多个任务的 Git、Codex 和审查可以重叠。确认宿主授权和容量后可调高。退出状态不明时保留 Editor 名额，核验进程后用 `recover` 释放。
5. 改版前停止接单并备份数据库和运行配置。保留原目录作为 `legacy_slot`，正式 `default_pool` 为原 Worker ID。数据库升级保留原任务、Job、审批与产物。设置槽位 `enabled = false` 暂停新分配，已绑定任务继续完成。排空后可以从配置移除非默认槽位。更换仓库或目录用新的池或槽位 ID。

命名池准备可选 `[pools.<id>.pr]`，填写 `provider`、`api_url`、`repository`、`token_env`。`token_env` 仅指服务进程的凭据环境变量名。没有池专用适配器时只有默认池继承现有宿主 PR/MR 配置。

### 查看磁盘用量、归还槽位和清理缓存

在 Windows PowerShell 5.1 中运行 `scripts/workspace-maintenance.ps1`。指定 `-WorkerUrl` 或设置 `UNITY_WORKER_URL`，并使用 Operator Token。单工程和命名池用法相同，先从 `Status` 查实际槽位 ID。

~~~~powershell
$env:UNITY_WORKER_URL = 'http://127.0.0.1:8765'
$env:UNITY_WORKER_APPROVAL_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Operator Token' -AsSecureString)).Password
.\scripts\workspace-maintenance.ps1 Status
.\scripts\workspace-maintenance.ps1 Measure -SlotId worker-01
.\scripts\workspace-maintenance.ps1 Measure -State
.\scripts\workspace-maintenance.ps1 Release -TaskId '<task UUID>'
.\scripts\workspace-maintenance.ps1 Clean -SlotId worker-01 -Cache Library
~~~~

也可以在工作区之外保存 DPAPI 凭据文件。用运行脚本的同一 Windows 账号在同一台机器上创建文件。传 `-CredentialDirectory` 时，脚本优先读取该目录下的 DPAPI 文件。

~~~~powershell
$workerCredentials = Join-Path $env:LOCALAPPDATA 'UnityWorker\credentials'
New-Item -ItemType Directory -Path $workerCredentials -Force | Out-Null
Read-Host 'Operator Token' -AsSecureString | ConvertFrom-SecureString |
    Set-Content -LiteralPath (Join-Path $workerCredentials 'UNITY_WORKER_APPROVAL_TOKEN.dpapi')
.\scripts\workspace-maintenance.ps1 Status -CredentialDirectory $workerCredentials
~~~~

- 默认的 `Status` 列出池、槽位、任务与阶段，以及各槽位和 Worker 状态目录所在卷的可用空间与总容量。总览无需递归扫描。相同卷可能出现在多行，容量不能相加。
- `Measure` 按需递归扫描。槽位统计分为 `.git`、工程的 `Library`、`Temp`、`Obj` 和其余文件。状态目录包含日志、SQLite 和任务产物。统计的是文件的逻辑字节数，实际磁盘空间以卷容量为准。目录在扫描期间可能变化，脚本会报告跳过的符号链接、重解析点和读取错误。
- `Release` 归还任务持有的槽位。Worker 会检查任务状态、Git 工作区以及是否还有 Unity 进程需要恢复。记下输出的 Job ID。如果用 Ctrl+C 停止等待，或轮询超时，可继续通过 `GET /api/v1/jobs/{job_id}` 查询，Worker 仍会处理已受理的 Job。归还成功后，排队任务可能立即获得该槽位。
- `Clean` 只处理配置槽位的 Unity 工程直属 `Library`、`Temp` 或 `Obj`，每次只选一个。脚本先预览路径与预计回收的逻辑字节数，扫描完整且缓存存在时才能输入 `YES` 确认。清理 `Library` 后，下次打开工程或运行 Unity 会重新导入。Worker 会再次核对租约、工程路径、Editor 锁及 Git 状态。若预览后槽位已被其他任务占用，清理会被拒绝。维护期间请勿从外部运行 Unity 或修改该 checkout。
- 清理响应中断时，先用 `Status` 检查槽位，再重新预览目标缓存。若清理失败并报告 `.unity-worker-cleanup-*` 遗留目录，槽位会暂停接单。根据 `Clean` 返回或 `Status` 列出的路径检查遗留目录，核对它只包含原缓存数据并确认没有进程占用，再移出该目录或处理其中的数据。随后从原 Windows 登录会话运行 `scripts\restart-worker.ps1`。如果启动时指定了 `-Port`，重启时传相同的端口。最后用 `Status` 确认槽位通过复检。

### 迁移本机旧部署

先运行 `scripts/initialize-worker.ps1` 安装新仓库的依赖。停止旧 Worker 接单，等队列和执行中的 Job 排空，再停服并备份旧配置和完整状态目录。用原账号从新仓库执行 `start_worker.ps1`，将 `-ConfigPath` 指向原配置，保留配置里的 `logs`、槽位 ID、游戏检出路径和 Codex home。已有 DPAPI 文件时传 `-CredentialDirectory` 指向它们所在的目录。保持原监听地址和端口即可沿用现有 SOP 地址。用[预检命令](integrations/loopforge/README.md)核对服务，再确认旧任务、审批和产物仍可查询。需要回退时先停掉新 Worker，再用原启动入口恢复。

## 开发与交付

1. 人工在 LoopForge 审批技术方案，提交需求、方案和验收标准。
2. POST /api/v1/tasks 创建持久任务。POST /api/v1/tasks/{task_id}/run 取得租约并执行开发验证。也可逐个运行 prepare、implement、compile、test、review 和 fix。
3. Worker 增量获取配置的基础分支，在原工程建立 ai/task-<UUID>。编译、每个启用的测试模式和独立审查共同验证同一 Git 树。只有确定的编译错误、测试断言或审查修改请求进入自动返工，默认最多三轮。
4. 任务进入 awaiting_approval 后，审阅报告、变更和测试证据。授权者记录审批，指定这一版 revision。Commit Job 会再次比对当前 Git 树，安全暂存明确的文件清单并普通推送任务分支。
5. 完成后运行 release。失败和取消任务也保留租约及现场，工作区整理完成后由操作员释放。池中的空闲目录可以继续接单。

### API 约定

所有路径使用 Bearer Token。旧入口 GET /health、POST /jobs、GET /jobs/{job_id} 和 loopforge_smoke.py 维持原有请求格式。旧 Job 状态现在可跨重启查询。/health 的 busy_or_blocked 同时反映开发任务租约或遗留 active.lock。

| 方法和路径 | 用途 |
| --- | --- |
| GET /api/v1/workers | 查询 Worker ID 和默认池的状态 |
| GET /api/v1/pools | 查询各池排队数、目录状态及当前任务 |
| GET /api/v1/storage | Operator Token。查询各池、槽位和状态目录的卷容量，以及清理遗留路径 |
| GET /api/v1/storage/state | Operator Token。按需扫描 Worker 状态目录 |
| GET /api/v1/storage/slots/{slot_id} | Operator Token。按需扫描指定槽位。传 `?cache=Library`、`Temp` 或 `Obj` 只预览指定缓存 |
| POST /api/v1/storage/slots/{slot_id}/clean | Operator Token。请求体需包含 `cache`（`Library`、`Temp` 或 `Obj`）和 `confirmed=true`。同步返回结果 |
| POST /api/v1/tasks | 需要 request_id、requirement、plan、acceptance_criteria 字符串数组。可传 workflow_run_id、project_rules、pool_id |
| GET /api/v1/tasks/{task_id} | 任务状态、阶段、失败类型、审查、审批和交付 |
| POST /api/v1/tasks/{task_id}/{action} | 固定操作 run、prepare、implement、compile、test、review、fix、pr、cancel、release |
| POST /api/v1/tasks/{task_id}/commit | 需要 request_id 和签批时的 revision。两者和当前批准在同一事务内核对 |
| POST /api/v1/tasks/{task_id}/approve | Operator Token。需要 request_id、revision、approver。可指定 commit_message、create_pr |
| POST /api/v1/tasks/{task_id}/recover | Operator Token。需要 request_id、confirmed_stopped=true、note |
| GET /api/v1/jobs/{job_id} | 轮询 Job 的 queued、running、succeeded、failed、cancelled、recovery_required |
| GET /api/v1/tasks/{task_id}/artifacts | 取任务已登记的产物路径 |
| GET /api/v1/tasks/{task_id}/artifacts/{name} | 下载指定 Job 完成后留存的产物 |
| GET /api/v1/tasks/{task_id}/logs?name=...&offset=0&limit=8192 | 按明确的日志名查询。将 next_offset 带入下一个请求 |

每个任务操作传唯一 UUID request_id。原样重试会返回同一 Job，变更目标或有效负载会返回 409。失败后明确重新执行安全阶段时，使用新的 request_id。并发冲突、非法阶段和缺少当前版本批准也返回 409。cancel 返回受理结果，随后轮询原 Job，直到确认终态。创建任务只入库，在 prepare 或 run 时分配目录。提供 `queue_wait_seconds`（1 至 86400）时等候同池目录。Job 过期记录 `queue_timeout`，需明确用新请求 ID 重试。省略此字段时仍使用立即接单或 `409`。一经分配，任务、日志、审批及后续操作始终使用原 `slot_id`。

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

维护脚本可单独验证。测试使用本机回环 HTTP 模拟服务、测试 Token 和临时 DPAPI 文件。手工验收时，给测试 Worker 配置独立回环端口、临时状态目录和可丢弃的 Git 检出，再用 `-WorkerUrl` 指向该端口。可以放置已被 Git 忽略的测试缓存目录，验证预览、拒绝确认及确认后只清理选中的目录。

~~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_workspace_maintenance_script.py' -v
~~~~

LoopForge 的凭据绑定、方案产物、脚本节点和双审批配置见 [SOP 接入](integrations/loopforge/README.md)。实施步骤和测试覆盖记录见 [实施计划](IMPLEMENTATION_PLAN.md)。
