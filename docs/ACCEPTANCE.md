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

## 本机证据

- Unity 2022.3.62f3 与 Unity Test Framework 1.1.33 已在独立工程实测。通过正常编译、EditMode 1 个测试、PlayMode 1 个测试。故意失败的断言被分类为 tests_failed，故意的 C# 语法错误被分类为 compile_failed。
- Codex CLI 0.172.0 的 MXC 权限探针已实测，包含工程内写入、Git 元数据写入阻断、工程外读取阻断和独立审查目录只读。该探针不调用模型。elevated 后备模式需在目标账号的预配环境重新验证。
- Windows 临时 Worker 已通过同一 LoopForge Docker 镜像及 Compose 网络的 /health 调用。SOP 画布、真实模型调用和真实托管远端交付仍需在验收环境走完整流程。
