# Unity Worker 多目录池 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal** 让多条 LoopForge 需求 SOP 通过同一个 Windows Worker 入口，在指定池的独立 Unity 检出中并行开发，并保留可靠的排队、审批、交付和恢复。

**Architecture** 用一个 SQLite 状态库管理多个命名池的持久目录绑定、Job 和租约。Worker 按目录使用已有 Git、Codex 和 Unity 执行器，LoopForge 通过可信脚本调用同一入口并按任务 ID 接续工作。

**Tech Stack** Windows、Python 3.11+、FastAPI、SQLite、Git for Windows、Unity 2022.3.62f3、现有 LoopForge 脚本与审批节点。

**Spec** [Unity Worker 多目录池设计](../specs/2026-11-25-unity-worker-pools-design.md)

## Global Constraints

- Worker 源码仓库为 `D:\UnityWorkers\service`。部署脚本、正式状态和凭据留在各自的部署目录。实现依次在隔离 Git 工作树完成。
- 一个 Uvicorn 进程。每个槽位至多持有一个任务租约，每个任务至多一个排队或运行中的 Job。Git、Codex、Unity 和网络 I/O 在 SQLite 写事务外执行。
- 保持旧单目录 TOML、既有请求签名、任务/Job ID、产物路径、审批 revision 校验和 `/health`、`/jobs` 行为。目标槽位从第一次分配固定到任务释放及历史查询。
- 目录和工具路径只来自 Worker 配置。HTTP 请求只能用预配置的 `pool_id` 选池。保留普通 Worker 与操作员权限边界。
- 单元与本地集成测试用现有 `unittest`、临时 Git 远端和模拟工具。Windows 真实 Unity EditMode / PlayMode 用 `exec_command` 的 `require_escalated`。项目测试沿用正式 SOP 已审批的夹具范围。

## Review Focus

1. 旧库存在不同阶段的任务、活跃 Job 和已批准 revision 时，升级仍能按原 ID 查询与恢复。任务 2 的迁移测试覆盖。
2. 目录 ID 被重绑、路径通过大小写或 junction 指向其他槽位、池内复制了不同远端时，防止接错需求或写错仓库。任务 1 和任务 2 的绑定测试覆盖。
3. 两个请求争一个槽位、取消撞上首次派发、进程启动失败时，避免双租约和跨任务恢复。任务 3 的并发测试覆盖。
4. Worker 重启前存在排队任务，或仍有无法确认退出的 Editor 时，保持现场与全局 Unity 使用权。任务 3 和任务 4 的重启测试覆盖。
5. SOP 节点使用已冻结的旧脚本，或升级后重连旧 Job 时，避免误把排队当完成、重复接单或签批错目录。任务 5 的客户端测试和任务 6 的灰度覆盖。

## 0. 建立开发基线

- [ ] 用 `superpowers:using-git-worktrees` 在 `D:\UnityWorkers\service-pools` 创建分支 `feat/unity-worker-pools` 的工作树。先核对原仓库状态。把当前受版本控制的工作树差异复制到隔离工作树，核对新旧内容一致。设计和计划文档也放入该分支。
- [ ] 比对 `unity-ai-development/runtime` 与 Worker 源码，保留运行版 `git_workspace.py` 的 Git LFS hook 隔离和 `unity_executor.py` 的 NUnit 失败分类。把部署版 `worker_client.py` 的方案 ID、revision 交接与重连修复合并回通用客户端。加针对性的既有回归用例，避免在目录池改造时丢失这三处修复。保留源工作树已有未提交改动的原状。
- [ ] 使用 `D:\UnityWorkers\service\.venv\Scripts\python.exe -m unittest discover -s tests -v` 建立 Windows 基线。依赖缺失时在隔离开发环境按 `requirements-dev.txt` 安装。记录既有失败和修复归属。
- [ ] 在隔离分支记录基线提交 `chore: reconcile unity worker development baseline`。只纳入已核对的现有 Worker 源码修订、设计与本计划。

后续命令中的 `python` 指同一 Windows 虚拟环境，工作目录为隔离仓库根目录。新测试文件按 `python -m unittest discover -s tests -p <文件名> -v` 单独验证。各任务提交前再跑相关既有测试。

## 1. 配置与目录预检

**文件** `worker_config.py`、`worker.py`、`git_workspace.py`、`pull_requests.py`、`config.example.toml`、`tests/test_pool_config.py`。

**接口** 保留 `Settings` 作为执行参数。增加 `load_service_settings() -> ServiceSettings`，产出 `worker_id`、`logs`、`default_pool`、`legacy_slot`、`max_parallel_unity`、`secrets` 和映射 `pools: dict[str, PoolSettings]`。`PoolSettings` 包含 `id`、`remote_identity`、`settings` 的公共执行参数、选配 `pr` 以及 `slots: tuple[SlotSettings, ...]`。`SlotSettings` 包含 `id`、`pool_id`、`enabled`、`settings: Settings`。每个 `SlotSettings.settings` 持有完整执行参数和该目录自己的 Git 根、Unity 根，以及在 Worker 服务层统一配置的 Codex home。

- [ ] 在 `PoolConfigTests` 写红测 `test_loads_two_named_pools_and_legacy_defaults`，断言显式配置的 `game` 池包含 `worker-01`、`worker-02`，`tools` 池独立，隐式配置的默认池为 `worker_id`，旧入口槽位为 `worker-01`。写 `test_rejects_overlapping_checkouts_and_codex_home_inside_a_checkout`，逐项拒绝重复 ID、嵌套 checkout、解析后同一路径和位于检出内的 Codex home。用 `test_all_slots_and_pools_share_one_worker_codex_home` 核对新增目录和新增池都继承同一登录。写 `test_marks_only_bad_slot_unavailable`，断言锁住的 checkout 或不匹配的远端只影响其所在目录。
- [ ] 运行 `python -m unittest discover -s tests -p test_pool_config.py -v`，预期先因缺少配置接口或预检失败。
- [ ] 提取并复用现有字段校验。显式 TOML 使用 `[pools.<id>]` 与 `[[pools.<id>.slots]]`，保留顶层服务、工具默认值和凭据环境变量。显式模式拒绝含糊的顶层单目录路径或 `UNITY_PROJECT`、`UNITY_GIT_ROOT` 覆盖。验证默认池、legacy 槽位和每池至少有一个配置目录。`max_parallel_unity` 为正整数，默认 1。池内的 `project` 相对 `git_root` 一致。
- [ ] Windows 本地目录用解析路径、大小写规范化和 OS 文件身份检查别名。拒绝跨槽的路径重解析和共享可变项目缓存。用现有 `reserve_project` 给状态目录和可打开的每个 checkout 加进程级锁。增加 `check_slot(slot: SlotSettings, pool: PoolSettings, *, leased: bool) -> str | None`，结构校验在加载时完成，运行预检返回可公开的失败类别。空闲槽预检复用 Git 清洁检查和 Unity 关闭检查，已绑定槽只验证身份及路径以便恢复。旧单目录的 smoke 配置维持对最小 Unity 项目的支持。给每个执行 `Settings` 增加 `expected_remote_identity: str | None`。`GitWorkspace.prepare` 在切分支前用原有 fetch/push 摘要核对这个值。
- [ ] 在 `pull_requests.py` 增加 `HostedPullRequests.from_pool_config(pr) -> HostedPullRequests | None`，池配置包含 `provider`、`api_url`、`repository` 与 `token_env`。兼容原环境配置用于隐式模式或迁移时默认池。把所有启用适配器的凭据加入当前服务已有的统一脱敏逻辑。
- [ ] 重跑 `test_pool_config.py`、`test_mvp.py`、`test_workspace.py` 和 `test_delivery.py`。提交 `feat: configure named unity pools and validate checkouts`。

## 2. 持久身份和旧库迁移

**文件** `task_store.py`、`tests/test_store.py`。暂时保留 `Store(path, worker_id, project)` 的调用兼容，并增加 `Store.open_service(config: ServiceSettings) -> Store`。

**接口** `Store.slot_lease(db, slot_id: str) -> dict | None`、`Store.queued_jobs(db, pool_id: str) -> list[dict]`、`Store.active_job(db, task_id: str) -> dict | None`、`Store.recover_interrupted(job_id: str | None = None) -> None`。任务数据新增 `pool_id: str`、`slot_id: str | None`。Job 数据增加可选 `queued_at`、`queue_deadline_at` 和 `started_at`，使用现有 UTC 时间格式。

- [ ] 用旧版 schema 手工创建数据库夹具，写 `test_migrates_single_slot_without_rewriting_ids_or_signatures`。断言历史任务、排队前未分配任务、旧 smoke Job、一次已审批和一次中断的任务各自保留原 ID、签名和产物名。断言原租约归 `worker-01`，未启动任务 `slot_id is None`，现有审批 revision 与迁移前一致。写 `test_rejects_rebinding_a_slot_or_moving_the_default`、`test_two_slot_leases_and_one_active_job_per_task`，包含 SQL 层违反租约约束时抛 `sqlite3.IntegrityError` 的断言。
- [ ] 运行 `python -m unittest discover -s tests -p test_store.py -v`，观察红测。记录迁移数据库先后版本与请求签名的比较方式。
- [ ] 将 `PRAGMA user_version` 设为 1。对已有库先验证 `identity` 与配置的 `worker_id` 和 `legacy_slot` 项目路径相符，再在一次 `BEGIN IMMEDIATE` 内增加 `tasks.pool_id`、`tasks.slot_id`，回填 JSON 与列，保留 `lease` 与原有 `jobs` 全表。用曾分配证据 `lease`、`started_at` 或旧 smoke 类型区分历史槽位与待接任务。用显式列名写入任务和 Job。创建 `one_slot_lease` 部分唯一索引限制 `lease = 1` 的 `slot_id`，替代 `one_lease`。`one_active_job` 部分唯一索引覆盖 `queued`、`running`。对未分配任务接口和数据层一致拒绝 `lease = 1`。
- [ ] 保存服务 `worker_id`、固定的 `default_pool`、`legacy_slot`。持久池清册保存仓库摘要、分支、相对项目路径和 Unity 版本，槽位清册保存池归属和按平台规范化的 checkout/Unity 根。校验重启时绑定不变。缺失槽位仍有租约时拒绝配置迁移。允许新增槽位和已排空槽位停用或移除，保留历史名称。项目执行配置变更前处理完该池所有未完成任务。旧库错误或未知 schema 版本保留现场并给出可操作的失败原因。
- [ ] 按同池 Job `rowid` 排序，数据库写入和调度同时用已有的 `BEGIN IMMEDIATE`。恢复函数只转换 `running`，传 `job_id` 时只处理指定 Job。保留任务租约及已审批内容。
- [ ] 重跑 `test_store.py`、`test_workflow.py`。如果迁移失败测试读取库，确认事务完整回滚。提交 `feat: persist workspace bindings and migrate worker state`。

## 3. 目录分配、排队和阶段路由

**文件** `pool_scheduler.py`、`development.py`、`worker.py`、`tests/test_pools.py`、`tests/test_workflow.py`。

**接口** `PoolScheduler.admit(db, task, job, queue_wait_seconds: int | None) -> bool` 返回当前是否已获得目录。`PoolScheduler.dispatch() -> None` 在服务启动、接单、释放、取消以及定期唤醒时处理出队和过期。`POST /api/v1/tasks` 增加可选 `pool_id`。`POST /api/v1/tasks/{task_id}/run` 与 `/prepare` 增加可选 `queue_wait_seconds`。

- [ ] 写 API 红测 `test_two_slots_run_and_third_starts_in_fifo_order`。让两条运行阻塞在两个 `threading.Event`，断言槽位和实际 `cwd` 不同。第三条返回 `queued`，无槽位、无 Unity/Git/Codex 调用。释放一个原任务后断言第三条沿用同一 `job_id`，得到已释放目录。在不同池同时存在空闲目录时，第三条仍等待 `game` 池。
- [ ] 写 `test_duplicate_request_and_cancel_racing_dispatch`、`test_deadline_and_restart_preserve_queue_and_binding`、`test_spawn_failure_does_not_recover_other_running_jobs`、`test_approval_and_release_keep_the_assigned_slot`。用任务/Job 断言覆盖幂等重放、超期 Job 的 `queue_timeout`、运行后恢复时 `slot_id` 固定、排队取消无租约、失败释放后容量未增加。用注入时钟、线程屏障和临时数据库消除实际等待。
- [ ] 运行 `python -m unittest discover -s tests -p test_pools.py -v`，预期返回旧 `409` 或缺少状态/配置字段。
- [ ] `create` 校验目标池的 `test_assemblies`，未知池返回 `422`。签名规范化将默认池的 `pool_id` 省略以匹配旧签名，命名非默认池纳入签名。恢复逻辑用已经存储的池，池间不借目录。只在首次 `run/prepare` 可启用排队，整数 1 至 86400。已绑定任务重试时不重新选槽。池内无空闲槽位或存在前序排队时，旧请求维持 `409`。
- [ ] 同一个事务完成过期检查、队列顺序检查、选空闲 `enabled` 槽、Job 状态与租约更新。调度线程按池分配所有可用槽位，处理队首过期和取消，释放时唤醒。使用同一 Worker guard 管理注册启动线程、取消、停机和释放，锁内只处理短状态操作。由单 Job 的异常处理接管线程启动失败。退出时停止接单和派发，等待现有进程树退出或保留恢复所需的租约。
- [ ] 基于任务的 `slot_id` 构造 `RunContext` 与对应的 Git、Codex、Unity、PR 实例。复用已有关联阶段流程。调度服务保持单一 Worker 及状态库。待审批、取消后未整理、失败和待恢复都保留租约。旧 `/jobs` 只用 `legacy_slot`，原访问和重复请求规则照旧。
- [ ] 增加鉴权 `GET /api/v1/pools`，响应 `{"pools": [{"id": "game", "queued_count": 1, "slots": [{"id": "worker-01", "status": "leased", "task_id": "…", "current_stage": "compile"}]}]}`。优先显示恢复状态，空闲禁用为 `draining`，错误为 `unavailable`。任务和 Job 查询仅增加字段或排队状态。默认池的聚合 `/api/v1/workers` 维持原字段，只有一个活跃任务时沿用 `task_id`、`current_stage`。`/health` 保持 legacy 槽位响应含义。
- [ ] 跑 `test_pools.py`、`test_workflow.py`、`test_mvp.py` 和 `test_artifacts.py`，重复运行并发测试至少 10 次。提交 `feat: schedule unity tasks on isolated workspaces`。

## 4. Unity 编辑器并发与故障隔离

**文件** `task_store.py`、`pool_scheduler.py`、`unity_executor.py`、`development.py`、`tests/test_unity.py`、`tests/test_pools.py`。

**接口** `PoolScheduler.unity_permit(ctx)` 上下文管理器。持久表 `unity_permits(job_id PRIMARY KEY REFERENCES jobs(id))` 记录占用；限制来自 `ServiceSettings.max_parallel_unity`。执行器仅在调用真正 Unity 进程时进入管理器，`UnityExecutor(settings)` 的直接测试仍可独立使用。

- [ ] 写 `test_two_tasks_pass_git_and_codex_with_one_editor_permit`，断言同时有两目录的 Agent 活动、Unity 进程观测的峰值为 1。再用上限 2 断言峰值为 2。写 `test_editor_permit_survives_unverified_exit_and_restart`，第一个 Unity 调用以 `recovery_required` 结束，重启后第二个任务仍可跑 Git/Codex，Unity 阶段等待，确认原进程退出并执行 `recover` 后才能启动。写 `test_cancel_while_waiting_for_editor_does_not_start_process`。
- [ ] 运行 `python -m unittest discover -s tests -p test_pools.py -v`，预期旧实现可能同时启动两个 Editor 或找不到并发使用权。
- [ ] 启动前事务内获取使用权，拿不到时在事务外可取消地等待，该等待计入 Job 执行时长。保留 ProcessRunner 现有进程树清理。正常退出或确定已退出的错误释放使用权，`recovery_required` 与进程重启保留使用权，待操作员核验后和恢复状态一起释放。已知未启动 Unity 的输入检查放在获取使用权之前，等待后再检查项目锁。使用权释放唤醒等待者。恢复、清洁及重试校验只访问所属槽位。
- [ ] 在启动预检无法分配的槽位上验证修复后重启可恢复，运行中挂起的槽位仍可观测，其他目录继续执行。用现有 Codex Windows 沙箱探针加一个兄弟检出路径读写边界用例，确认执行权限只覆盖所属检出，审查工作区隔离。保持 Unity 工具的现有 Job Object 约束。
- [ ] 跑 `test_pools.py`、`test_unity.py`、`test_process.py`、`test_codex.py`。提交 `feat: enforce recoverable unity editor concurrency`。

## 5. SOP 客户端与部署适配器

**文件** `integrations/loopforge/worker_client.py`、`integrations/loopforge/README.md`、`README.md`、`tests/test_loopforge.py`。正式 `install_unity_ai.py` 单独在部署适配器维护。`unity-task.json` 和交付产物沿用现有字段，追加 `pool_id`、`slot_id`、`base_commit`。

- [ ] 写 `test_polls_queued_to_running_with_separate_deadlines`，模拟 queued → running → succeeded，断言独立预算及最终产物槽位正确。写 `test_reconnects_to_preupgrade_job_without_posting_again`、`test_queue_timeout_requires_new_attempt`、`test_delivery_rejects_mismatched_pool_or_slot` 和 `test_plan_cannot_choose_the_pool`。保留旧产物缺失新字段的正向测试。
- [ ] 运行 `python -m unittest discover -s tests -p test_loopforge.py -v`，验证现有客户端把 `queued` 误当终态等失败。
- [ ] 增加仅供 `create/develop` 的 `--pool`，从可信脚本参数传入 API，方案文本不控制池。增加 `--queue-wait-seconds` 给 `develop/run/prepare`。轮询把 `queued` 与 `running` 都视为非终态，按首次观察到已分配启动执行预算，提交后立即写任务/Job 产物。重新连接优先查已有同操作 Job，以请求 ID 幂等读回结果。过期排队 Job 不隐式重新入队，显式 `--attempt N` 才生成新操作 ID。沿用 SIGTERM 的取消和已批准 revision 核验。交付和释放同时比对产物中出现的池及槽位 ID。
- [ ] 准备池模式运行说明、配置示例和运维状态解释。部署适配器从正式命名池读取测试白名单，静态带 `--pool unity-ai-development --queue-wait-seconds 18000 --wait-seconds 18000`。升级发布门用 `GET /api/v1/pools` 核对 `worker-01` 和 `worker-02`。证据用配置真实路径区分两套预热的 Git、Codex、Unity 基线。保留原三条需求 SOP 及执行子 SOP 的快照和凭据绑定。
- [ ] 重跑 `test_loopforge.py` 和 `python -m unittest discover -s tests -v`。将 `render develop` 生成的脚本在临时 SOP 环境启动，验证和旧 JSON 产物的交接。提交 `feat: integrate pooled worker jobs with loopforge sops`。

## 6. Windows 验收与正式启用

**证据** 每路记录 `run_id`、`task_id`、`job_id`、`pool_id`、`slot_id`、基础提交、终态、revision、日志与测试 XML。确认并行时间窗和空闲接续时间，证据留本机 Worker 运维目录。

- [ ] 在模拟工具及两份临时检出上完成全流程，先开 `game` 池两个槽，再用临时第二池验证独立排队。用审查批准、实际 commit、向临时远端 push、release 验证两个任务完整走通。用 `git diff --check` 和 Windows 全量测试验证准备部署的同一提交。
- [ ] 计算 D 盘实际可用容量和克隆、LFS、首次导入峰值。将现有受信任仓库独立检出到 `D:\UnityWorkers\worker-02`，按项目规则初始化依赖与 Git LFS，继承 Worker 服务已有的 Codex 登录。用 `D:\UnityWorkers\worker-02\client` 的 `-projectPath` 做基线编译和已批准 EditMode/PlayMode 定向测试。`worker-01` 也通过当前基线核验。Unity 测试经沙箱外执行授权。
- [ ] 用离线状态库备份演练迁移和 Worker 重启。等待正式 Worker 的运行中 Job 到达安全检查点，保存当前服务程序、配置、可恢复的 SQLite 备份和已发布子 SOP 定义，保留任务产物。把原目录配置为 `unity-ai-development/worker-01` 与 `legacy_slot`。加入 `worker-02` 并先设 `max_parallel_unity = 1`。启动新版，核对既有任务、批准 revision、旧 `/health` 以及两目录状态。
- [ ] 先验证新 Worker 服务，再发布已准备的执行子 SOP。启动两条对现有项目无冲突的可回滚验收需求，第三条排队。观察两路 `-projectPath`、Git 分支、Codex 执行时间、日志归属、单个 Editor 使用权。用临时远端验收审批与交付。在正式项目选择已获批准的实际需求验证交付，或取消并安全释放验收任务。释放第一个槽位后观察第三条原 Job 启动。用清理后的独立临时池额外演练目录故障、恢复、重启和排队取消。
- [ ] 两个正式目录用受控输入实测同时开 Editor，验证授权、进程树和报告。只有双开稳定且本机资源充足时把 `max_parallel_unity` 调到 2。否则维持 1，记录为什么编译和测试串行以及两条 SOP 并行时的实际验收结果。
- [ ] 容量回退时先排空 `worker-02`，禁用其新接单并继续使用已迁移的库。保留旧运行的任务与产物。只有新版尚未接受新操作时才可恢复切换前快照和旧程序。交接本机配置、SOP 发布版本、测试结果以及已确认的实际并发。
