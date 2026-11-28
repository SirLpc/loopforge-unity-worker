# Windows 与 LoopForge 端到端验收

执行环境使用专用低权限 Windows 账号及独立 Git 远端和 Unity 验收工程。验收配置的 base_branch 指向该独立项目或已批准的测试分支。

## 自动化检查

- 安装 requirements-dev.txt 后运行 python -m unittest discover -s tests -v。Git 集成在当前 Windows 环境需要沙箱外运行，所有推送指向测试创建的本地 bare 仓库。Codex 与 Unity 子进程由测试夹具模拟。
- 以下命令以本机 Unity Editor 运行两个测试模式，确认故意加入的断言失败和语法错误都被阻断。脚本生成全新的 .tmp-test-unity-* 工程，日志和 XML 单独保留。Test Framework 依赖需要 Package Manager 可用。

  ~~~~powershell
  .\.venv\Scripts\python.exe tests/windows_unity_acceptance.py 'C:\UnityVersions\2022.3.62f3\Editor\Unity.exe'
  ~~~~

- 若当前只准备了编译环境，可用 --compile-only 验证正常和故障编译。其他平台上的普通 Python 测试不等同于 Unity 测试。

编译验收使用 Unity 2022.3 的 BatchMode 导入及退出证据。EditMode 和 PlayMode 使用 Test Framework 的 -runTests、-testPlatform 和 -testResults，分别校验 XML。 [Unity 2022.3 Editor 参数](https://docs.unity3d.com/2022.3/Documentation/Manual/EditorCommandLineArguments.html) [Unity Test Framework 1.1 参数](https://docs.unity3d.com/Packages/com.unity.test-framework@1.1/manual/reference-command-line.html)

## Windows Worker 签名直接合入验收

在已安装 `requirements-dev.txt`、Windows Git 和 Unity Test Framework 依赖的独立检出中运行以下命令。每次运行会新建一个 `.tmp-test-unity-*` 目录，保留 bare 远端、工程、Worker 数据库、编译日志和 XML。生成的临时签名密钥仅用于本次进程内的 Worker HTTP 服务。脚本在首次推送前核对两个检出的所有有效拉取和推送地址，并在夹具每次传输前复查。Git 配置将地址改写到其他位置或添加额外目的地时，验收会停止。

~~~~powershell
.\.venv\Scripts\python.exe tests/windows_unity_acceptance.py 'C:\UnityVersions\2022.3.62f3\Editor\Unity.exe' --direct-merge
~~~~

脚本通过 Worker API 受理两个任务。每个任务都在发布测试通过的检查点后，令独立检出修改同一行 C# 并推进 `main`。规划、实现、冲突解法和独立评审使用确定性夹具。Git 与 Unity 使用本机可执行文件。每个 Editor 进程的默认超时是 240 秒，慢速机器可加 `--timeout 1200`。

- 第一个合并必须保留双方改动。候选重新编译，并在 EditMode 和 PlayMode 各运行一个引用实际业务代码的定向断言。脚本读取候选 XML，检查合并提交的双亲、签名绑定的任务 SHA、bare 远端目标 SHA 和槽位归还结果。
- 第二个解法故意丢掉任务的改动，候选 EditMode 必须报告断言失败。脚本检查 bare 远端 `main` 仍指向合并前的基线，任务分支和失败现场的租约均保留。每个任务结束时输出 JSON，可据其中的任务和作业 ID 核对日志。

## LoopForge 与真实工具

- [ ] 用专用账号启动 Worker，从 Windows、WSL2、LoopForge Docker 脚本分别获取 /health。非法 Token 返回 401。已有 MVP smoke 幂等并有真实 Unity 日志。
- [ ] 在测试 SOP 提交一个小需求，审阅计划后运行 develop。Worker 取得并持续持有租约，第二个任务准备时收到 409。重复 POST 返回同一 Job。
- [ ] 查看计划对应的 Git SHA、任务分支、Agent 会话、编译日志、两种测试 XML 和独立审查。验证 Library 中既有文件和缓存仍可使用。
- [ ] 用测试任务故意引入一处 C# 语法错误。在隔离的验收配置设置 max_fixes=0，观察失败固定停在 compile，无法审批或交付。编译退出码与原始日志留存。
- [ ] 修复后运行新的验证。制造一个测试断言失败，确认失败、零测试或缺失 XML 都会阻断交付。检查 license、网络、Package Manager 故障不会触发自动改业务代码。
- [ ] 在 Worker 等待审批时重启服务，确认租约、报告和 revision 均可查询。模拟运行中异常重启，确认 recovery_required 且新任务准备受阻。专用账号核对进程树、项目锁、Git 操作和目标分支后执行 Operator 恢复。
- [ ] 在已批准的验收远端验证提交与正常 push。审批前 commit 返回 409。审批后故意改动一个文件会使当前审批失效。批准的版本交付到 ai/task-*，基础分支维持原 SHA。
- [ ] 对审批后的任务取消或制造可控超时。状态查询确认进程停下后检查租约及现场。完成或归档后 release，下一任务按基础分支重新准备且看不到前一任务的未提交文件。
- [ ] 如启用了 PR/MR 适配器，在验收远端显式批准 create_pr，确认链接指向正确任务分支和目标分支。

## 八节点功能 SOP 验收

1. 准备包含定向 EditMode 和 PlayMode 测试的独立 Unity 工程，并配置专用 Runner、Windows Worker 命名池和 Git 验收远端。记录远端基础分支 SHA。按[八节点功能交付接入说明](../integrations/loopforge/README.md#八节点功能交付)配置 Worker、中继和现有项目凭据。用 `--staged-feature --preflight` 核对池和能力，用 `--staged-feature` 预览现有功能 SOP，保留 `base_revision` 和备份。
2. 在验收项目用 `--publish --expected-revision <base_revision>` 发布。回读发布版本，核对八节点及连线、唯一人工验收、验收打回开发实现、沙箱、两项凭据引用和项目入口。确认发布前的 revision 仍可在版本历史中查阅。
3. 提交一项可由两个测试模式覆盖的小改动。在 `unity-scope.json` 和 `unity-planning.json` 中核对同一任务、池、槽位及基础 SHA，从该提交核实 `design.md` 引用的文件及行号。检查每轮编译产物和独立评审均对应当前 Git 树。评审通过后，从 `unity-verification.json` 和 `test-report.md` 找到 `verification.test_job_id` 对应的 Worker 产物，打开实际的 EditMode 和 PlayMode XML，核对所选用例的数量和结果。
4. 到人工验收后，打开「待审交付物」里的测试报告，核对报告开头的任务分支、完整 SHA、远端分支头和测试 Git 树。填写多行意见并打回，确认 Worker 保留原任务、槽位和基础 SHA，并在同一任务分支追加下一轮编译提交。核对新一轮测试和 SHA，再批准验收。确认交付记录引用已验收的提交，基础分支保持原 SHA。
5. 使用未配托管适配器的验收 Worker 时，核对 `unity-delivery.json` 和 `delivery.md` 中的 `mr_status=pending`，以及 Worker 返回的槽位归还结果。核对同一运行的节点状态、审批记录、测试 XML、交付记录和远端引用。将运行证据和回退快照保存在受保护的本地目录。

正式切换前，核对新版 Worker 和中继能继续处理旧任务，等待旧服务的活动 Job 到达终态。使用现有启动脚本切换到已验证的版本，沿用状态和租约。按相同步骤重新预览并发布目标功能 SOP，再比对发布版与候选图，并检查既有运行仍保留启动时的快照。回退所需 revision 取自该次发布前的预览。

## 本机证据

- Unity 2022.3.62f3 与 Unity Test Framework 1.1.33 已在独立工程实测。通过正常编译、EditMode 1 个测试、PlayMode 1 个测试。故意失败的断言被分类为 tests_failed，故意的 C# 语法错误被分类为 compile_failed。
- 2026-11-28，直接合入验收在 Unity 2022.3.62f3 上跑完两次签名交付。正确解法的候选 `318812753374cc24801111c855e9f32284c176a7` 通过编译、EditMode 和 PlayMode 各一个定向用例；Worker 确认远端指向该提交并归还槽位。第二个候选的 EditMode 得到 1，预期值为 2，Worker 保留现场租约，远端 `main` 保持在本次交付的基线 `1a6419d066d6e0201cf8b616190df9d2cb8933f1`。
- Codex CLI 0.172.0 的 MXC 权限探针已实测，包含工程内写入、Git 元数据写入阻断、工程外读取阻断和独立审查目录只读。该探针不调用模型。elevated 后备模式需在目标账号的预配环境重新验证。
- Windows 临时 Worker 已通过同一 LoopForge Docker 镜像及 Compose 网络的 `/health` 调用。
- 隔离功能 SOP 通过 LoopForge Runner、签名中继和原生 Windows Worker 跑完八个节点。设计引用已在同一租约的基础提交核实。第一次人工验收打回后，Worker 收到完整意见。第二轮的真实 Unity EditMode 和 PlayMode XML 各通过一个定向用例。一次批准后，隔离的 bare Git 远端任务分支指向交付记录中的提交，其 Git 树与本轮通过测试的树一致，基础分支 SHA 保持原值，Worker 归还槽位。
- 在同一隔离项目回读了发布版，已核对节点重试设置和返工设置，并确认发布前版本的历史快照和项目设置保留。
- 上述功能 SOP 的 Codex 定型、方案、实现和评审输出均来自确定性夹具。Git 推送使用隔离的 bare 远端，交付状态为 `mr_status=pending`。真实模型调用与托管远端的 MR 创建仍待验证。
