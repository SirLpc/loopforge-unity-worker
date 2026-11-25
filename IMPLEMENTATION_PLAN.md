# Unity Worker 实施计划

**目标** 将 LoopForge 的需求编排接到 Windows 常驻 Unity 工程，完成开发、验证、审查和审批交付。

**架构** 保留 FastAPI 的 MVP 路由及 smoke 客户端。SQLite 记录任务、Job、审批和租约。固定的 Git、Codex、Unity 阶段在同一后台执行线程串行运行，文件产物保存在工程外部。

**技术栈** Python 3.11+、FastAPI、SQLite、Git for Windows、Codex CLI、Unity BatchMode、Windows Job Objects。

## MVP 基线

- 现有四个文件为 worker.py、loopforge_smoke.py、README.md 和 requirements.txt。当前目录的 Git 仓库尚无提交。
- 已实现 Bearer Token、健康检查、单进程内请求去重、active.lock 排他、BatchMode 启停、轮询及日志尾部。
- 原有测试依赖人工运行。服务重启会失去内存状态，超时需要手工核验锁，尚无开发、编译证明、Git 或交付流程。
- 跨 WSL2 和 Docker 的连通性沿用用户已经实际确认的部署方式。本次用隔离测试工程和本机测试核验新增能力。

## 全局约束

- 复用一个 Windows NTFS 工程以及 Library、Git、LFS 和 Package 缓存。
- 持久租约覆盖任务阶段及人工等待。发现未知进程或状态时保留租约。失败现场由操作员决定如何归档。
- 使用 Worker 配置中的工程、工具、Git 远端和基础分支。任何提交或推送都需要独立人工作出与变更版本绑定的授权。
- 真实商业工程、生产远端与 LoopForge 上游保持在本次验证范围之外。
- 先加回归测试，每组新增能力先写可观察的测试，再运行对应模块与全套测试。

## P0

- [x] 保留 GET /health、POST /jobs、GET /jobs/{job_id} 及旧 smoke 脚本，补齐鉴权、并发、重复请求、日志与原有锁回归测试。
- [x] 实现配置、本地实例锁和 Windows Job Object 进程树控制。验证超时、取消以及子进程终止。
- [x] 实现 SQLite 任务、Job 和独占租约，校验合法阶段与请求标识。验证运行中重启和等待审批重启。
- [x] 实现常驻 Git 工程预检、增量 fetch、固定基础 SHA、任务分支、LFS 和子模块。验证未知修改阻断、缓存留存及禁止输入路径。
- [x] 通过实际 CLI 帮助协商 Codex 能力，增加 Unity 项目版本和编译证明检查。使用模拟工具测试 prepare → implement → compile。

## P1

- [x] 验证 EditMode、PlayMode XML，包括零测试、测试失败、环境失败。
- [x] 以独立只读会话审查 Diff 与验证报告，并校验 JSON。仅对确定的代码问题自动返工，默认上限三轮。
- [x] 交付审批绑定 Git 树，校验工作区和实际提交树，普通推送任务分支。验证审批撤销、推送失败和重试。
- [x] 提供 Python SOP 客户端、分阶段操作、合并开发验证节点、独立人工交付门和取消流程。

## P2

- [x] 提供恢复检查、增量日志、报告下载、可选 PR/MR 接口和单 Worker 状态元数据。
- [x] 完成中文部署、SOP 配置与实机验收步骤。运行完整测试、静态检查及本机可用的隔离 Unity 验收。

## 审查重点

- 崩溃前已经提交给操作系统的 Job 必须进入 recovery_required 并继续占用租约。
- Git 中已忽略的未知文件也可能跨任务污染，预检和释放都要检查。
- Unity 返回 0、无日志证据或零测试均无法成为开发验证通过的依据。
- 任何审批后变更都会阻断提交和推送，失败推送只重试经校验的原提交。
- 日志及下载必须防止访问其他任务或本地文件，客户端不得传入命令、远端或相对路径来扩大权限。

独立复核发现的恢复绕过、旧 Job ID 冲突、Git 索引隐藏标记、子模块清洁与 LFS 处理、交付版本竞态均已加入针对性验证。专用 Unity 工程验收、Codex 0.172.0 MXC 权限探针及 LoopForge 镜像中 Windows Worker 健康探针见 [端到端验收清单](docs/ACCEPTANCE.md)。

Windows 全套单元与集成回归运行 41 项并通过。新增的 PR/MR 适配器缺失回归在随后三项交付测试中通过。Python 语法、PowerShell 启动脚本语法和示例 TOML 均已检查。真实模型调用、LoopForge 画布全流程及托管远端交付留待专用部署验收。
