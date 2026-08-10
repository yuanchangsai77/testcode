# testcode 路线图

## 文档职责

本文档只维护：

- 当前可用基线
- 尚未完成的能力
- 推荐优先级
- 下一阶段验收标准

已经完成的功能不在这里保留施工步骤或原始待办。当前行为进入对应功能文档；历史方案、
阶段提交和实施过程由 Git 记录。专项设计也不在本文重复定义：

- 总体分层见[总体架构](architecture.md)
- 模型—工具循环见[Agent 执行循环](core/agent-loop.md)
- 授权和内容检查见[执行安全](core/execution-safety.md)
- 项目规则、探测和测试解析见[项目感知](core/project-awareness.md)
- Skill、MCP、能力仓库和 TUI 分别见对应专题文档

版本快照位于 `docs/versions/`，只记录发布时点的不可变能力边界。新的快照只在项目实际
准备发布新版本时创建，不以文档整理本身作为版本升级依据。

## 当前基线

状态核对日期：2026-08-10。

当前 runtime 已具备：

- 单轮与多轮 CLI、会话保存和恢复。
- OpenAI-compatible 与 stub 模型客户端、原生 tool calls 和 JSON fallback。
- 文件、搜索、Shell、测试、patch 和只读 Git 工具。
- `readonly`、`confirm`、`auto` 模式，审批、危险命令识别和凭据写入阻断。
- 项目规则、相关 workspace 摘要、显式 context、项目探测和默认测试命令解析。
- Skill metadata 扫描、工具箱展示、instructions 显式激活和跨轮保留；不再维护独立的
  trigger 自动注入路径。
- MCP 三种 transport、懒 discovery、缓存、重连、tool/resource 协议入口和按需激活。
- 能力仓库目录、manifest、激活/释放、scope、预算和冲突预检，以及按需激活的本地
  Subagent 工具箱和用户 `/capabilities`、`/skill` 入口。
- 原生 inline TUI、会话内编辑历史、审批、中断和运行时输入。
- run 事件与详情日志。

当前完整测试为 422 个用例，覆盖 engine、model、policy、tools、context、Skill、MCP、
能力仓库、CLI 和 TUI 的关键路径。

## P0：长任务连续性与上下文预算

这是当前最高优先级。目标是让单 Agent 的长编码任务可中断、可恢复、可验证，同时避免
prompt 随历史无限增长。

### P0.1 Context packaging

- 在 context loaders 和 prompt builder 之间建立独立 `ContextPackager`。
- 输入包括 conversation、tool results、Skill、显式 context、checkpoint 摘要和来源
  引用。
- 第一版只做稳定分组、来源标记、字符统计和总量限制，再逐步加入摘要策略。
- 超预算时显式记录裁剪和省略，不允许静默丢失关键上下文。

### P0.2 Session checkpoint

- 保存任务阶段、关键工具结果摘要、最近失败、最近验证结果和 active capabilities。
- 保存文件 read state：path、mtime、hash；恢复后判断 patch 是否仍安全。
- 中断、模型错误、工具错误和审批拒绝时都生成可恢复状态。
- 完整事件和大输出保留在 cold archive，checkpoint 只保存恢复所需的最小事实。

### P0.3 Task plan 与恢复

- 引入 `pending`、`in_progress`、`blocked`、`verified`、`done` 的轻量状态。
- run summary 展示当前阶段、完成事项、阻塞原因、验证状态和下一步。
- resume prompt 使用 checkpoint 恢复包，不依赖模型猜测完整历史。
- 修改任务结束时明确给出测试通过、测试失败或未运行测试的原因。

## P1：Skill 完整能力

当前 workflow instructions 与本地工具已统一进入 `LocalToolboxSource`；Skill 只负责磁盘
发现、版本和覆盖。剩余工作是把磁盘 Skill 从“指令文件”扩展为受控资源包。

- 为 `references/`、`assets/`、`scripts/` 建立独立索引和按需加载。
- 为 Skill 内容设置独立预算、来源引用、裁剪和摘要。
- Skill script 必须转换为普通工具动作，经过同一 policy、审批和日志路径。
- 增加 `/skill` 或等价交互入口。
- 为同名覆盖、版本冲突和来源优先级提供用户可见诊断。

## P2：MCP 兼容性与资源上下文

MCP 最小主链路已经完成，不再规划第二套 transport 或全量工具注册路径。

- 扩充公网 MCP server 兼容性测试和错误诊断。
- 完善 capability traits 与风险映射。
- 将 resource descriptor 纳入候选上下文选择。
- resource 正文经过长度限制、敏感信息保护和 `ContextPackager` 后才能进入 prompt。
- 为 MCP resources/prompts 建立与工具箱一致的叶子激活语义。

## P3：交互体验、分发与质量门禁

### 交互与配置

- 模型增量 streaming。
- 统一 overlay 栈。
- 持久化跨进程输入历史。
- `testcode config get/set/list/path`。
- 日志查询命令与更清晰的失败诊断摘要。

### 分发与质量

- zsh/fish completion 和 release checklist。
- CI 执行测试、lint 和必要的类型检查。
- 增加 ruff；是否引入 mypy/pyright 由实际复杂度决定。
- 对 SSH、tmux、Windows Terminal、窄屏和组合字符补充兼容性验证。

## P4：Subagent、Team 与 A2A

本地 subagent 的会话基础设施提前实施，但不绕过 P0 尚未完成的 checkpoint、任务状态和上下文
预算。首阶段建立独立子会话、会话镜像、集群关系、公共状态空间和进程内并发 runner，具体契约见
[Subagent 会话集群](core/subagent-session-clusters.md)。跨进程常驻调度和故障续跑仍需依赖 P0 的
checkpoint 与恢复能力。

- 本地 subagent 使用独立 session、tool history、run id、权限和 context budget。
- 子会话可继承主会话、使用新配置或从会话镜像仓库启动。
- parent 与子会话不直连，只通过有来源的结构化公共状态交换有界结果。
- 主 Agent 可批量创建子会话，并通过独立 runtime 并发执行所有 ready 成员。
- 并发公共状态写入使用文件锁和原子替换；workspace 修改仍需 patch 前 hash 或文件锁。
- Team 在本地状态结构稳定后再扩展到远程 A2A。
- 远程修改必须以 patch/artifact 回到本地审批流。

## 暂不优先

- 自动创建或修改虚拟环境。
- 完整 review 产品模式。
- session rename/tag/fork。
- 大规模测试目录重构。
- 与现有 `run_tests` 重叠的诊断工具。
- 让模型永久安装新能力。

## 下一阶段验收标准

完成 P0 后应满足：

- 长任务可以中断、保存和恢复，并清楚说明恢复点。
- resume 后有最小恢复包、任务状态、关键结果摘要和可回查来源。
- 恢复后的文件修改能验证 read state，失效时明确要求重新读取。
- prompt 只包含预算内的 hot context 和 warm summary，完整事实保存在 cold archive。
- 裁剪、摘要和省略均可观察。
- 每次修改任务都给出明确验证结论。
- 现有短任务、Skill、MCP、审批和 TUI 行为保持兼容。
