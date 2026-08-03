# 未来平台演进与验收

## 文档职责

本文描述未来蓝图的依赖顺序、阶段退出条件和验证方法，不改变 [testcode 当前路线图](../roadmap.md)
的优先级。当前 P0 仍先于设备和 A2A 扩展。

## 1. 演进原则

- 先稳定语义和恢复，再选择分布式 transport。
- 先模拟器和单一纵向闭环，再接入真实物理设备。
- 最小 Profile、Policy 和 Identity 必须早于设备副作用。
- Dashboard 不成为核心运行时的前置条件。
- 每个阶段通过真实生产者/消费者验证后，才冻结 reference 字段。

## 2. 阶段计划

### 阶段 0：当前 Agent Runtime 稳定性

- ContextPackager 与 prompt 预算。
- checkpoint、cold archive 和 read state。
- Workflow/Step 的最小状态与恢复。
- 修改任务的明确验证结论。

退出条件：长任务能中断、恢复和验证，不依赖模型猜测完整历史，并满足 Q-01、Q-02。

### 阶段 1：Model Plane 正式集成

- API Control Board 与 `testcode` 的工具循环契约测试。
- 网关凭据配置、强制认证和回环/非回环监听策略。
- 错误分类、重试、用量和 trace。
- 本地访问显式验证代理变量绕过。

退出条件：至少两个不同 Provider 能完成多轮 tool call，协议转换不破坏工具语义，并满足 Q-03。

### 阶段 2：最小平台契约

- 明确 Conversation、Workflow、Step、ExecutionAttempt、DeviceExecution 和 RemoteAgentTaskRef。
- 最小 Capability、Artifact、Event、Profile、Policy Decision 和 Identity projection。
- 最小 Experience、Preference、Evidence、Retrieval Result 和 Promotion 语义。
- 建立契约版本和兼容测试框架。
- 暂不冻结完整 wire schema。

退出条件：状态所有权唯一，模拟生产者/消费者能处理重复、乱序、取消和未知可选字段，并满足
Q-04、Q-05。

### 阶段 3：设备模拟器与 Linux/GPU 闭环

- Device capability source、Registry、lease 和执行接口。
- Linux Device Agent 或模拟器。
- 一个结构化 GPU 推理能力与至少两个候选执行节点。
- Artifact 引用、结果回收和故障注入。

退出条件：调度选择可解释，重复派发不重复副作用，断线后能查询权威状态，并满足 Q-04、Q-06、
Q-07。

### 阶段 4：持续助手运行时

- Conversation、Workflow、Event Inbox、Attention 和 Presence。
- 每个 Workflow 独立上下文预算与 checkpoint。
- 最小本地 Approval 流程。
- 后台任务只向主会话投递有界摘要。
- 明确偏好写入/撤销，以及 Experience Candidate 到检索投影的最小闭环。

退出条件：用户持续聊天时后台任务独立运行，事件可以延后、合并、去重和过期；偏好和经验在模型
切换后保持版本稳定、可撤销且可追溯，并满足 Q-02、Q-05、Q-09。

### 阶段 5：环境语义、感知与副作用闭环

- 可扩展 World State、Observation 和 Perception Session。
- 选择一个真实或模拟的感知能力与一个有副作用动作能力，不将其写成框架内建类型。
- 动作策略、幂等、效果验证和 reconciliation。
- 多主体共享环境、数据出域和外部内容隔离的最小策略。

退出条件：AI 能从动态 manifest 发现并组合感知与动作能力，同时正确区分事实、观察、推断、命令
和现实效果，并满足 Q-06、Q-07。具体家庭案例作为非规范性验收样例，不成为框架依赖。

### 阶段 6：A2A 与委托授权

- 至少三个动态声明不同技能的独立 Agent。
- A2A Task、异步状态、Artifact 和本地 Workflow 投影。
- 平台 Approval Challenge/Receipt 扩展。
- 受信用户认证器上的强认证、一次性消费、过期和撤销。
- A2A Experience/Preference 最小投影，以及远程结果进入本地候选而非直接发布的验证。

退出条件：远程 Task 等待授权时，用户可以在主会话安全批准或拒绝；原任务恢复且任何模型或中间
Agent 都接触不到可复用凭据；远程内容不能越权读取或直接发布本地经验，并满足 Q-08、Q-09。

### 阶段 7：产品化与扩展

- Dashboard 聚合三个 Plane 的 API，不直连数据库。
- 新平台、设备、服务和 Agent 技能按真实需求通过 Adapter/Plugin 接入。
- 多控制域、共享 Device Plane、高可用和远程 A2A。
- 根据真实发布压力决定服务拆分与 monorepo。

## 3. 统一量化基线

以下是 `[当前推荐]` 的实验室最低基线，不等同于最终产品 SLO。每个阶段开始前必须建立 Target
Profile，记录目标硬件、网络、数据规模以及延迟、吞吐、内存、电量或带宽数值；若修改下列基线，
必须在决策记录中说明测量证据，不能以“基本可用”代替退出条件。

| 编号 | 最低验收基线 |
| --- | --- |
| Q-01 恢复安全 | 100 次中断/恢复注入中，恢复必需状态零静默丢失；read state 失效时 100% 阻止直接修改 |
| Q-02 上下文边界 | 所有 Workflow 均有硬预算；100 次超预算输入中零次无记录溢出，裁剪、摘要和省略均可查询 |
| Q-03 模型契约 | 两个 Provider 各完成至少 20 轮含多次 tool call 的用例；tool id、参数、结果和错误分类零语义丢失 |
| Q-04 状态收敛 | 每类跨边界状态注入至少 1,000 条重复、乱序和迟到消息，终态回退次数为 0 |
| Q-05 事件投递 | 1,000 次重复/重放输入只形成一次逻辑投递；过期事件打扰用户次数为 0 |
| Q-06 副作用幂等 | 同一幂等标识重复派发 100 次，物理或外部副作用最多发生一次；无法判定时全部进入 reconciliation |
| Q-07 数据完整性 | Artifact 截断、摘要不匹配和越域访问样本 100% 被拒绝；未验证物理效果不得报告为已完成 |
| Q-08 授权约束 | replay、过期、撤销、错误 audience、动作或参数变化各至少 100 次，越权成功次数为 0 |
| Q-09 经验与偏好边界 | 100 次跨项目、主体、信任域、撤销和过期检索中越界命中为 0；所有行动引用均可追溯到记录版本和来源 |

任何“零次”安全指标只表示测试语料中的退出门槛，不代表生产风险为零。产品化阶段还必须基于真实
故障率建立持续监控、告警和 SLO。

## 4. 协议冻结门槛

协议候选只有满足以下条件才进入 `docs/reference/`：

- 至少一个真实生产者和消费者。
- 有端到端延迟、吞吐、内存、电量或带宽数据。
- 已验证断线、重连、重复、乱序、取消和版本不兼容。
- 字段用于安全、恢复、兼容或审计，而非预想需求。
- 已定义弃用和迁移方式。

在此之前，蓝图只固定语义，不固定 JSON key、Protobuf tag、数据库列或 transport。

## 5. 契约与故障验证

### 跨边界契约

- Agent ↔ Model：消息、tools、tool calls、tool results、错误、usage。
- Agent ↔ Device：capability、执行、取消、结果和 Artifact 映射。
- Device Control ↔ Agent：注册、租约、revision、重连和幂等。
- Agent ↔ Agent：A2A Task、状态、Artifact 和等待授权映射。
- Conversation ↔ Workflow：有界摘要、话题隔离和模型切换。
- Authorization：action/audience 绑定、一次性消费、过期和撤销。
- World State：Observation 来源、有效期、证据权限和推断边界。
- Knowledge：Experience/Preference 版本、来源、范围、撤销、检索投影和 Skill 晋升边界。

### 故障注入

- Provider 在 tool call 中途失败或返回 429/5xx。
- Device Execution 派发后确认丢失。
- Device Agent 执行中断线、重启或状态乱序。
- Artifact 传输中断或完整性不匹配。
- 低优先级事件和高风险授权事件同时到达。
- 用户切换终端，旧终端产生迟到响应。
- Receipt 重放、过期、audience 错误或动作改变。
- 感知源找不到目标、数据中断或置信度不足。
- 副作用命令收到但现实效果未验证。
- 经验索引迟到更新、记录已撤销、项目版本不匹配或远程 Agent 返回冲突候选。

## 6. 安全验收

- 无凭据、错误凭据、过期凭据和撤销主体均被拒绝。
- 一个 capability 的授权不能扩展到另一个 capability。
- local_domain_only 数据不能被未授权域取得。
- 外部不可信内容不能触发其他能力执行。
- Observation 不能绕过用户意图和 Policy。
- 生物特征不进入模型、A2A、Event 或普通日志。
- 不满足 assurance 的终端不能批准高风险远程动作。
- 用户拒绝、忽略或超时后，主会话与其他 Workflow 保持可用。
- 已撤销、越域或仅为推断候选的经验与偏好不能进入有效行动上下文。

## 7. 主要风险

| 风险 | 控制措施 |
| --- | --- |
| 过早固定字段和 transport | 只固定语义，达到协议冻结门槛后再进入 reference |
| Task/Workflow 概念混淆 | 分离目标、步骤、执行尝试、设备执行和远程任务引用 |
| 模型成为状态所有者 | 持续状态归 Runtime，模型保持可替换 |
| 后台事件频繁打断 | Subscription、Attention、去重、限流和免打扰 |
| 多 Workflow 上下文污染 | 独立 checkpoint/context budget，只共享有界摘要 |
| 视觉推断被当成事实 | 保存来源、有效期、证据与置信度；Policy 独立授权 |
| A2A 委托扩大权限 | 平台授权扩展、audience 绑定、禁止默认转委托 |
| 物理命令成功但现实未发生 | 区分命令、执行、观察和验证，进入 reconciliation |
| 协议和服务数量失控 | 首个纵向闭环只选择一套候选实现，按现实压力再拆分 |

## 8. 目标场景

基础设施首先选择一个真实计算任务验证本机与远程节点调度。完整非规范性样例使用
[异地持续家庭助手](reference-scenario.md)，用于验证多种动态能力、主动事件和远程授权并发运行；
其中任何设备、实体、Agent 技能或策略都不是平台内建要求。
