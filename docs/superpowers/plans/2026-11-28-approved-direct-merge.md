# Unity 功能验收后合入主线实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标** 在 `unity-ai-development-feature` 人工验收后，由 Windows Worker 把已验收的工作提交合入该池的 `main`，并自动处理可验证的 C# 内容冲突。

**架构** Worker 沿用现有槽位、Unity 执行器和签名授权入口。独立的 Git 合并操作计算并保留候选，Worker 作业持久化候选及远端确认结果。中继和安装器根据工作流快照选择合入方式，交付汇总核对合入回执和槽位归还。

**技术栈** Python、FastAPI、SQLite、Git、Codex Windows 沙箱、Unity 2022.3.62f3、LoopForge Script/Approval 节点。

**设计** [Unity 功能交付在人工验收后直接合入主线](../specs/2026-11-28-approved-direct-merge-design.md)

## 全局约束

- 以 Worker 仓库现有测试和交付入口为基础。Worker、Runner 中继和发布脚本留在 Unity Worker 仓库。部署配置和凭证留在现有部署位置。
- 合入方式在工作流发布、任务创建和签名授权时绑定。远端身份及目标取受信任的命名池配置。当前池为 `unity-ai-development`，主线为 `main`。
- 远端原子比较目标 SHA，只允许快进到已验证候选。源 SHA 固定，最后一次源引用复查紧挨目标推送。
- 简单冲突只允许同一路径上的普通 `.cs` 文本内容冲突。按候选限一次自动解冲突，独立评审和原测试范围通过才允许推送。目标并发前进时限一次重建。
- 每组测试使用独立运行目录和本地 bare 远端。Unity EditMode 和 PlayMode 用 `C:\UnityVersions\2022.3.62f3\Editor\Unity.exe`，执行命令用 `sandbox_permissions: "require_escalated"`。
- 原 MR 工作流、签名和任务仍可完成及归还。重试可恢复基础设施故障或核对远端结果。需要修改的阻塞保留完整诊断。

## 评审重点

- Windows 路径含空格或非 ASCII 时，冲突路径按 Git 的 NUL 格式读取，逐个精确暂存，见任务 1。
- 文件内容为 LFS 指针但后缀为 `.cs` 时暂停，见任务 1。
- 回应丢失后主线又前进一步时，凭候选祖先关系补记合入，见任务 2。
- Unity 在验证时写出新 `.meta` 或改动跟踪文件时阻止推送并保留现场，见任务 3。
- 旧快照运行与新流程并存时，签名及任务方式一致，算子各自校验实际回执，见任务 4。

---

### Task 1 — Git 候选与冲突判定

**文件** 新建 `direct_merge.py`、`tests/test_direct_merge.py`。复用 `git_workspace.py` 的命令、安全树和远端绑定方法。保留已有工作分支推送检查。

**接口** `DirectMergeGit(workspace: GitWorkspace)` 提供 `read_target(ctx, task: dict) -> str`、`contains(ctx, ancestor: str, descendant: str) -> bool`、`begin(ctx, task: dict, source_sha: str, base_sha: str) -> MergeCandidate`、`resolve(ctx, candidate: MergeCandidate) -> MergeCandidate`、`assert_clean_candidate(ctx, candidate: MergeCandidate) -> None` 和 `restore(ctx, task: dict, source_sha: str) -> None`。`MergeCandidate` 包含 `source_sha`、`base_sha`、`conflict_paths: tuple[str, ...]`、`commit_sha: str | None`、`tree_sha: str | None`；冲突期间持有非冲突暂存结果供范围核对。由任务 2 调用。

- [ ] 写 `test_clean_candidate_preserves_both_parents_and_source_ref`、`test_content_conflict_accepts_exact_csharp_paths`、`test_rejects_complex_and_out_of_scope_changes`。沿用 `test_workspace.repository` 建独立 bare Git，并用第二检出推进目标。用真实 Git 测试场景或 Prefab、`.meta`、重命名、添加删除、符号链接、二进制文件和 LFS 指针。对空格和中文文件名验证 `self.assertEqual(candidate.conflict_paths, ("Assets/物品 代码.cs",))`。向非冲突文件写内容或保留冲突标记时断言 `StageError`，主线和任务引用保持原 SHA。
- [ ] 运行 `python -m unittest discover -s tests -p test_direct_merge.py -v`，确认因候选功能尚未实现而失败。
- [ ] 实现上述方法。目标从经验证的推送端点读取并获取对象。进入前确认 Worker 槽位干净且处于已验收任务分支。候选使用 Worker 管理的 detached checkout，基于两个固定 SHA 运行 Git 合并。验证未合并条目的三个阶段与同一路径上的原始 blob、文件模式、文本内容和重命名诊断相符。保存 Git 在非冲突路径上的暂存结果，解完后逐路径暂存，确认最终树只改变允许的冲突路径。复用 Unity 工程版本、Git 安全树及 LFS/子模块完整性检查，候选和恢复后的检出都核实实际依赖与所在提交。作业重启后只清理已确认为 Worker 自己创建的操作状态，未知修改留待人工处理。
- [ ] 重跑候选测试和 `python -m unittest discover -s tests -p test_workspace.py -v`，确认均通过后提交该组文件。

### Task 2 — 候选验证、推送与恢复顺序

**文件** 在 `direct_merge.py` 加交付编排和推送方法，在 `tests/test_direct_merge.py` 加 bare Git 并发及故障测试。

**接口** `DirectMergeGit.push(ctx, task: dict, candidate: MergeCandidate) -> None` 负责有条件的目标更新。`land_approved(ctx, *, solve, verify) -> dict` 使用已授权 `ctx.task`。 `solve(ctx, candidate) -> None` 只提供冲突解法。 `verify(ctx, candidate) -> dict` 对同一候选执行编译、评审和测试，并返回验证证据。返回回执含 `mode="direct-merge"`、`task_branch`、`source_sha`、`target_branch`、`base_sha`、`merge_sha`、`target_sha`、`conflict_paths`、`verification`、`status="merged"`。

- [ ] 写 `test_retries_once_when_target_advances_before_push`、`test_reconciles_lost_push_ack_after_a_further_main_commit`、`test_blocks_source_drift_and_rejects_non_fast_forward`、`test_already_landed_reuses_history`。用独立检出或 bare 接收钩子注入故障。断言两次验证对应两次不同的候选 SHA，第二次变动后主线保持外部 SHA。断言重启后找到已记录候选时 `self.assertEqual(receipt["merge_sha"], persisted_candidate)`，远端保持后续提交。覆盖推送拒绝、远端不可核验、目标删除或重写及只推一个目标引用。
- [ ] 运行任务 1 的测试命令，确认新增用例失败。
- [ ] 实现 `land_approved`。在移动检出前保存操作所有权和固定源 SHA，在推送前保存候选 SHA、目标 SHA、验证证据和重建次数。为候选建立 Worker 本地引用直到结果可恢复。树等于验收树且无冲突时复用证据。推送执行前核对远端和源分支，校验两个祖先关系及候选树。目标使用精确 ref lease 配合本地快进证明，按目标 ref 校验所需 LFS 对象，接收端的分支保护照常生效。记录远端实际的目标 SHA。先查已落地历史再决定重试，目标只在已知快进后重建一次。安全恢复原任务检出后完成交付。
- [ ] 重跑任务 1 和任务 2 的测试，确认各故障用例通过后提交。

### Task 3 — Worker 作业、配置与独立验证

**文件** 修改 `worker_config.py`、`worker.py`、`workflow_approval.py`、`development.py`、`pool_scheduler.py`。在 `tests/test_workflow_approval.py`、`tests/test_pool_config.py` 加用例。

**接口** 命名池 `direct_merge: bool = False`，池状态公开此布尔值。带审批密钥的服务声明 `staged-feature-direct-merge` 能力。 `PlanningTaskRequest.delivery_mode` 可省略或设为 `"direct-merge"`。授权信封同名字段只和 `commit_sha` 一起出现。 `DevelopmentWorker.stage_direct_merge(ctx) -> dict` 接入任务 2。保留 `POST /api/v1/tasks/{task_id}/workflow-deliver`，新增的内部 Job action 为 `merge`。

- [ ] 写 `test_direct_delivery_requires_signed_mode_and_pool_opt_in`、`test_direct_delivery_blocks_unity_workspace_drift`、`test_direct_delivery_retries_journal_and_releases_original_checkout`。用现有 `WorkflowDeliveryTests` 夹具完成已验证任务。断言错误签名、任务方式不匹配、普通 action、旧请求 ID 改载荷或池未启用时主线未动。解冲突用例断言独立评审、真实候选 checkout 和原 fixture 参数，分别模拟编译错误、测试失败与 Unity 写入 `.meta`。成功后断言 `self.assertEqual(git(remote, "rev-parse", "main"), task["delivery"]["merge_sha"])`，原报告仍为相同 revision，`release` 作业归还槽位。
- [ ] 运行 `python -m unittest discover -s tests -p test_workflow_approval.py -v`，确认新增用例失败。
- [ ] 实现可省略的签名字段、任务参数和池开关。保持原 JSON 及旧配置签名，运行时校验新池开关，保留原 MR 任务池绑定。在一次事务中受理经过当前测试作业、槽位和已签署 source SHA 校验的 merge。独立验证复用 `UnityExecutor` 的编译及测试参数校验，并在每次 Unity 执行和只读评审前后验证候选树。Codex 写入沿用现有沙箱和独立会话，解法由任务 1 的范围检查约束。将候选检查结果存到合入记录和作业报告，保留原测试与评审。沿用恢复及归还许可，区分基础设施重试、复验失败、已推送回执补记。进程中断时由现有 `recover` 核对进程退出和候选操作归属，再允许恢复对应作业或归还。未知 Git 操作和额外修改保持阻塞。
- [ ] 重跑授权、池配置及开发流程测试。运行 `python -m unittest discover -s tests -p test_pool_config.py -v` 和 `python -m unittest discover -s tests -p test_workflow.py -v`。同时通过再提交。

### Task 4 — 中继、节点和安装器

**文件** 修改 `integrations/loopforge/planning_context.py`、`integrations/loopforge/feature_client.py`、`integrations/loopforge/install_sops.py` 和现有接入文档。测试 `tests/test_planning_context.py`、`tests/test_sop_install.py`、`tests/test_loopforge.py`。

**接口** `staged_feature_workflow(..., *, direct_merge=False)`、`install_staged_feature(..., direct_merge=False)`、`preflight_worker(..., direct_merge=False)`。CLI 的 `--direct-merge` 与 `--staged-feature` 一起使用。新图尾为 `accept → merge → deliver`，`merge` 使用脚本操作 `feature-merge`，归档 `merge-report.md` 与 `unity-delivery.json`。

- [ ] 写 `test_direct_merge_graph_has_one_approval_and_preserves_existing_bindings`、`test_direct_merge_signing_is_bound_to_the_frozen_graph`、`test_direct_merge_receipt_is_required_before_release`。断言旧图仍有 `mr`，新图只开放已验收测试版本的合入授权，混合图、伪通过、旧测试证据及错误合入回执都被拒绝。模拟 Worker 失败，断言节点输出包含可定位的 Job ID 及失败原因。另断言目标池关闭或 Worker 未声明能力时安装器预检失败。
- [ ] 运行 `python -m unittest discover -s tests -p test_planning_context.py -v`、`python -m unittest discover -s tests -p test_sop_install.py -v`、`python -m unittest discover -s tests -p test_loopforge.py -v`，确认新增用例失败。
- [ ] 中继以冻结图的精确节点类型及连线选择授权方式，继续校验已有产物 ID、摘要、验收尝试和测试版本。脚本只在新图创建直合任务并要求受信任 Worker merge 回执，汇总继续通过原操作 ID 机制安全归还。同一节点的复验阻塞保留作业引用，重试只用于可恢复类别。安装器发布前确认模式能力、目标池开关和 `--expected-revision`，保留当前工作流 ID、沙箱、凭据引用和项目入口。
- [ ] 重跑三个测试模块，通过后提交。

### Task 5 — Windows 验收与正式发布

**文件** 扩展 `tests/windows_unity_acceptance.py` 和 `docs/ACCEPTANCE.md`，沿用 `scripts/restart-worker.ps1` 及接入 README 的发布/回退步骤。

- [ ] 在 Windows 新建隔离代码检出和 Python 环境。运行 `python -m unittest discover -s tests -v`。所有普通失败、签名与回归用例通过，检查分支差异及旧运行兼容性。
- [ ] 给 Windows 验收脚本加 `--direct-merge`，用临时 bare Git、同一 C# 代码中的真实内容冲突和可重复的解法夹具，走 Worker 受理到归还路径。运行 `python tests/windows_unity_acceptance.py 'C:\UnityVersions\2022.3.62f3\Editor\Unity.exe' --direct-merge`。核对两种模式的 XML 各执行至少一个定向用例，记录验收 SHA、候选 SHA、远端目标 SHA 及槽位归还。故意失败时主线仍在原基线。记录实测结果并提交验收改动。
- [ ] 在隔离 LoopForge 项目安装新图，对临时远端各执行一次无冲突和单一 C# 冲突交付，通过平台正常验收入口批准隔离测试运行。C# 冲突路径使用实际 Worker Codex 沙箱和 Unity。核对八节点运行、签名、分支、产物、主线 SHA 和槽位归还。保留测试证据。
- [ ] 比对当前正式 Worker 与中继版本和正在运行的作业。按接入文档备份部署配置、记录上一版及 SOP revision，等待 Worker 可以安全重启后先部署兼容版本，再打开目标池的 `direct_merge`。从 Worker API 核实目标池和能力。
- [ ] 使用现有凭证从文件注入。以 `--staged-feature --direct-merge` 预览当前发布版，核对图和返回的 `base_revision`，再带 `--publish --expected-revision <base_revision>` 发布。回读工作流 ID、版本、节点、凭据引用及项目入口。如果发布失败，恢复备份版本并回读。最终报告隔离验收结果与实际发布 revision。
