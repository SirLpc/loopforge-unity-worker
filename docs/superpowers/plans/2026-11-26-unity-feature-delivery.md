# Unity 功能交付 SOP Implementation Plan

> **For agentic workers** Use superpowers:subagent-driven-development or superpowers:executing-plans. Run each task's failing test, implementation, passing test and review before continuing.

**Goal** 将 Unity-AI-Development 现有的 Unity 功能交付 SOP 改为按节点调用 Unity Worker，复用同一工程租约，测试后做一次人工验收。

**Architecture** Worker 持久化任务阶段、已验证 Git 树和交付状态。LoopForge 安装器生成八个节点。Runner 中继验证运行上下文和平台验收，Worker 消费短时授权并执行交付。

**Tech Stack** Python 3.11+、FastAPI、SQLite、unittest、Windows Codex、Unity 2022.3.62f3、LoopForge HTTP API。

**Spec** [Unity 功能交付 SOP 设计](../specs/2026-11-26-unity-feature-delivery-design.md)

## Global Constraints

- 从需求定型到归还使用同一任务、工程槽位、任务分支和基础提交。
- 业务节点依次为需求定型、方案设计、开发实现、独立评审、测试验证、人工验收、发起 MR、交付汇总。
- Plan 引用核对真实 Git 源码，验证选定的 Unity 夹具和实际 XML。验收绑定当前验证版本和测试作业。
- 功能任务模式创建时固定。旧任务和其他 SOP 继续走原有执行入口。LoopForge 核心和游戏源码不在改造范围内。
- Unity 2022.3.62f3 EditMode / PlayMode 在沙箱外执行。真实验收使用独立工程、Worker 状态和临时 Git 远端。
- 凭据用部署现有保护机制注入。仅发布现有 feature SOP，记录可回退的 revision。

## Baseline

在隔离工作树用现有 Windows Python 环境运行 `python -m unittest discover -s tests -v`，记录基线。每项任务同一环境完成红绿验证。

## File Structure

- `worker.py` 定义任务模式、请求模型和 Worker HTTP 生命周期入口。
- `development.py` 负责任务阶段、计划冻结、返工、测试验收及提交作业事务。
- `planning.py` 与 `codex_executor.py` 分别负责只读的需求定型、源码方案和结构化会话。
- `workflow_approval.py` 负责带版本的签名授权及业务字段校验，使用标准库。
- `integrations/loopforge/planning_context.py` 读取 LoopForge 节点、产物和批准，发布可信上下文。
- `integrations/loopforge/worker_client.py` 渲染可独立执行的脚本、驱动 Worker、发布节点报告。
- `integrations/loopforge/install_sops.py` 生成和保存功能 SOP，`pull_requests.py` 复用 Worker 适配器。
- `tests/` 复用现有 Worker、Git、Unity 与 LoopForge 夹具。接入和验收步骤补充在集成 README 和验收文档。

## Review Focus

- 已征用的槽位、基础提交或上游产物在节点重连时变更，由任务 1 和 4 覆盖。
- Unity 在测试过程中写入代码或资产，验收不可继续，由任务 2 覆盖。
- 旧批准重放或已批准的工作树变化，不能创建新的交付作业或推送，由任务 3 覆盖。
- Git 推送已成功且 MR API 暂时失败，恢复只重试 MR，由任务 3 和 4 覆盖。
- 保存新版本期间用户编辑同一 SOP，安装器报告 revision 冲突，由任务 5 覆盖。

### Task 1 · Worker 征用与只读方案

**Files** `worker.py`、`development.py`、`planning.py`、`codex_executor.py`、`tests/test_planning.py`。

**Interfaces** `PlanningTaskRequest.workflow_mode: Literal["staged-feature"] | None`。新增 `POST /api/v1/tasks/{id}/clarify`，沿用 `StageRequest`。Worker 暴露 `task["clarification"]`。Worker 的 `task_id`、`pool_id`、`slot_id`、`base_commit`、`workflow_run_id` 与 `planning.plan_digest` 供后续节点绑定。

- [ ] 增加 `test_staged_clarify_and_plan_share_the_checkout`，用已有 Git 夹具验证状态和真实源码引用。

  ```python
  self.assertEqual((planned["slot_id"], planned["base_commit"]), (clarified["slot_id"], clarified["base_commit"]))
  self.assertEqual(planned["planning"]["source_evidence"][0]["path"], "hello.txt")
  self.assertEqual(self.action(task_id, "implement").status_code, 409)  # 冻结前
  ```

- [ ] 运行 `python -m unittest discover -s tests -p test_planning.py -v`，确认新路径在生产改动前失败。
- [ ] 在 Worker 申请现有队列和槽位，使用 `CodexExecutor.clarify(ctx, briefing) -> dict` 生成结构化目标、范围、验收和未决事项。复用只读 Codex 权限。在同一基础提交由现有 `plan` 生成并核实源码引用，沿用 `accept-plan` 核对任务、槽位、基础提交和摘要。
- [ ] 重新运行规划测试，覆盖同一请求重连、错槽位、错基础提交及旧方案审批。
- [ ] 将该任务及测试独立提交。

### Task 2 · 实现、独立评审、测试和返工

**Files** `worker.py`、`development.py`、`tests/test_workflow.py`、`tests/test_planning.py`。

**Interfaces** `DevelopmentWorker.next_stage(task)` 为 staged-feature 选择 `implement → compile → review → test`。成功测试记录 `verification`，含 `revision`、`test_job_id` 和 `implementation_attempt`。新增 `POST /api/v1/tasks/{id}/rework`，收 `request_id`、`workflow_run_id`、`implementation_attempt`、`feedback`。

- [ ] 增加 `test_staged_review_precedes_tests_and_rework_invalidates_evidence`，覆盖以下断言和编译、Unity 错误路径。

  ```python
  self.assertEqual(self.action(task_id, "test").status_code, 409)  # 独立评审前
  self.assertEqual(tested["verification"]["revision"], tested["revision"])
  self.assertIsNone(reworked["verification"])
  self.assertEqual(reworked["slot_id"], tested["slot_id"])
  ```

- [ ] 运行 `python -m unittest discover -s tests -p test_workflow.py -v`，确认新路径失败。
- [ ] 按任务模式选择阶段。复用编译快照、独立审查会话和 Unity Test Framework 校验。审查与测试都校验同一 revision。返工请求原子进入现有修改阶段并清空旧证据与批准，受已有返工上限限制。恢复基础设施失败的原阶段，修改树变化回到编译并重新评审。
- [ ] 重跑 `test_workflow.py` 与 `test_planning.py`，确认 staged-feature 和旧的先测试后评审路径通过。
- [ ] 将该任务及测试独立提交。

### Task 3 · 绑定本轮验收的交付

**Files** 新建 `workflow_approval.py`、`tests/test_workflow_approval.py`。修改 `worker.py`、`development.py`、`worker_config.py`、`start_worker.ps1`、`pull_requests.py` 及相应 Worker 测试。

**Interfaces** `sign_approval(claims, key, now) -> dict`，`verify_approval(envelope, key, now) -> dict`。授权包含 `project_id`、`workflow_id`、`workflow_run_id`、`accept_attempt`、`task_id`、`pool_id`、`slot_id`、`base_commit`、`revision`、`test_job_id` 和批准与测试产物摘要。 `POST /api/v1/tasks/{id}/workflow-deliver` 接受 `request_id` 和 `approval`，返回已持久化的 commit 或 pr 作业。Worker 由 `UNITY_WORKER_WORKFLOW_APPROVAL_KEY` 读取独立密钥，注册状态在可用时报告 staged-feature 能力。

- [ ] 增加 `test_delivery_requires_current_loopforge_approval` 和 `test_mr_retry_reuses_pushed_commit`，验证敏感窗口和恢复。

  ```python
  self.assertEqual(self.workflow_deliver(task_id, old_proof).status_code, 409)
  self.assertEqual(self.workflow_deliver(task_id, bad_signature).status_code, 403)
  self.assertEqual(retried["commit_sha"], pushed["commit_sha"])
  self.assertEqual(pushed["delivery"]["task_branch"], tested["task_branch"])
  ```

- [ ] 运行 `python -m unittest discover -s tests -p test_workflow_approval.py -v`，确认入口与验证测试在实现前失败。
- [ ] 实现独立密钥、短时授权、业务字段绑定和同事务批准与作业入队。匹配重复请求时返回原 Job。新动作验证授权有效期、测试作业和现存租约。既有 staged-feature 提交和 MR 通过此路径，普通 Worker Token 不能走旧入口绕过。
- [ ] Worker 复用实际工作树校验、正常推送和幂等 MR 适配器。没有适配器时标明待创建。适配器失败时记录已推送的提交并允许只重试 MR。
- [ ] 运行 `test_workflow_approval.py`、`test_workflow.py` 和 `test_delivery.py`，验证前后 Git tree 与目标分支、重复请求、签名和过期授权均符合预期。
- [ ] 将该任务及测试独立提交。

### Task 4 · Runner 中继和分阶段脚本

**Files** `integrations/loopforge/planning_context.py`、`integrations/loopforge/worker_client.py`、`tests/test_planning_context.py`、`tests/test_loopforge.py`。

**Interfaces** `feature_context(run, run_id, project_id, artifact_reader, approval_key, now) -> dict`。中继的 `--approval-key-file` 只在受信任主机读取授权密钥。脚本动作 `feature-clarify`、`feature-design`、`feature-implement`、`feature-review`、`feature-test`、`feature-mr`、`feature-delivery`。Worker 引用产物为 `unity-scope.json`、`unity-planning.json`、`unity-implementation.json`、`unity-review.json`、`unity-verification.json`、`unity-delivery.json`。

- [ ] 增加 `test_feature_signs_only_current_approval_and_test_artifact` 与 `test_feature_node_reconnect_uses_existing_job`。

  ```python
  self.assertEqual(context["approval"]["claims"]["test_job_id"], test_job_id)
  with self.assertRaises(ContextError):
      feature_context(run_with_stale_test_attempt, run_id, project_id, artifact_reader, approval_key, now)
  self.assertEqual(worker_side_effects.count("commit"), 1)
  ```

- [ ] 运行 `python -m unittest discover -s tests -p test_planning_context.py -v` 和 `python -m unittest discover -s tests -p test_loopforge.py -v`，确认新测试失败。
- [ ] 扩展现有容器身份和运行白名单。从平台按产物 ID 读正文并校对 SHA-256，关联当前节点 attempt。仅在实际人工验收成功且 MR 节点执行中签发交付授权。旧的方案审批中继按原流程运行。
- [ ] 脚本使用可信上下文和 Worker 返回的唯一任务，追踪或恢复既有 Job，保留操作 ID，生成各节点 Markdown 及状态 JSON。只有当前实现、评审和测试结果可以交接。汇总节点输出实际验证结果和归还状态。
- [ ] 重跑中继和客户端测试，覆盖同 run 旧测试产物、其他 run 产物、并发 Worker 作业、已推送但 MR 重试及 release 重连。
- [ ] 将该任务及测试独立提交。

### Task 5 · 画布、安装和回退

**Files** `integrations/loopforge/install_sops.py`、`tests/test_sop_install.py`、`integrations/loopforge/README.md`。

**Interfaces** `staged_feature_workflow(target, worker_credential, context_credential, pool_id) -> dict`。命令 `install_sops --staged-feature` 只处理 `<project>-feature`，与 `--publish` 配合发布。

- [ ] 增加 `test_staged_feature_has_reference_order_and_one_gate` 与 `test_staged_publish_preserves_project_bindings_on_conflict`。

  ```python
  self.assertEqual([n["id"] for n in workflow["nodes"]],
                   ["clarify", "design", "implement", "review", "test", "accept", "mr", "deliver"])
  self.assertEqual([(n["id"], n.get("rejectTo")) for n in workflow["nodes"] if n["type"] == "approval"],
                   [("accept", "implement")])
  self.assertEqual(workflow_writes, ["garden-feature"])
  ```

- [ ] 运行 `python -m unittest discover -s tests -p test_sop_install.py -v`，确认功能入口和画布测试失败。
- [ ] 生成同名同 ID 八节点图，声明 `requirement.md`、`design.md`、`implementation.md`、`review.md`、`test-report.md`、`mr-body.md` 与 `delivery.md`。复用目标项目的 Worker 凭据引用、计划上下文凭据引用和沙箱。安装器通过功能 SOP 的普通 API 保存与发布。保存发布前的 SOP revision 使用现有版本检查，保持项目入口和其他图。Worker 能力预检允许正常已租用的槽位，后续作业按现有池队列等待。
- [ ] 重跑安装器测试，确认唯一审批、脚本能解析、同 ID 发布以及 revision 冲突处理。记录凭据文件与部署顺序。
- [ ] 将该任务及测试独立提交。

### Task 6 · 端到端验收、发布和核对

**Files** `docs/ACCEPTANCE.md`，验收需要时扩展现有测试夹具。部署凭据、回退快照和验收日志保存在本地受保护目录。

- [ ] 在 Windows 原生 Python 环境运行 `python -m unittest discover -s tests -v`，读取完整结果。
- [ ] 在隔离验收工程运行 `python tests/windows_unity_acceptance.py <本机 Unity.exe>`，另用功能 SOP 验证同租约的基线引用、评审后实际 EditMode/PlayMode XML、验收打回、一次批准后的正确分支 SHA、MR 待创建和槽位归还。对 Unity 命令使用沙箱外执行。
- [ ] 在 LoopForge 验收实例核对真实 Runner 与 Worker 的任务、产物及批准交接。记录通过和受阻项目及各自证据。
- [ ] 验证新 Worker 和中继同时兼容旧任务。等待旧服务的活动 Job 到达终态，再用验证通过的发布工作树与现有启动脚本切换 Worker。沿用原状态和租约。核对项目绑定、目标 SOP 原 revision、池能力与凭据，再发布 review 通过的功能 SOP。
- [ ] 重新读取发布版本并比较图、边、唯一验收、凭据引用和项目入口。确认历史运行保留快照。记录保留的回退 revision。
