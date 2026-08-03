# 未来平台总体架构

## 文档职责

本文定义未来平台的目标边界、横向 Plane、垂直 Layer、主要运行时组件和状态所有权。协议选择见
[协议边界](protocol-boundaries.md)，领域生命周期见[运行时领域](runtime-model.md)，安全与授权见
[安全与授权](security-and-authorization.md)。

## 1. 目标与非目标

### 目标

- 复用 `testcode` 已有能力仓库、工具、安全、日志和会话基础设施。
- 将 API Control Board 作为独立 Model Plane，而不是在 Agent Runtime 中重复 Provider 接入。
- 让本地工具、MCP、远程 Agent 和设备能力共享稳定的能力语义。
- 允许能力、实体和工作流通过 manifest、Plugin、Profile 与发现服务动态加入，策略通过独立且可验证
  的 Policy Source 加入，而不是写死在 Planner 或 Orchestrator 中。
- 支持持续对话、后台工作流、主动事件、跨终端确认和故障恢复。
- 设备、模型和传输实现可以按性能、隐私和平台现实替换。
- 从单机与模拟器逐步演进，不以最终目录结构驱动早期实现。

### 非目标

- 不把三个 Plane 实现成三个巨大 Core。
- 不要求所有设备运行同一种语言或完整 daemon。
- 不让 MCP 承担设备租约、物理状态和大数据传输的全部语义。
- 不把 A2A 当成用户身份系统或原始凭据传输通道。
- 不在蓝图阶段固定最终字段、数据库、消息中间件或部署数量。

## 2. 当前项目定位

### Agent Plane：`testcode`

当前 `testcode` 是 LLM 驱动的 CLI workbench，已有工具执行、能力仓库、Skill、MCP、安全审批、
会话和可观察性。未来主要向持续运行时、可恢复 Workflow、事件与远程能力扩展。

### Model Plane：API Control Board

`~/test` 已提供 OpenAI/Anthropic 兼容入口、协议转换、Provider 路由、key pool、子密钥、额度、健康
检查和桌面看板。它继续只负责模型治理，不拥有 Agent Workflow 或设备任务。

### Device Plane：未来 Device Fabric

Device Plane 负责设备注册、能力、租约、调度、执行、状态和数据协调。第一版可以是 `testcode`
旁的模块或轻量服务；只有独立常驻、共享访问或伸缩需求出现后才拆成独立部署。

## 3. 横向 Plane

```text
用户 / IDE / 自动化入口
          │
          ▼
┌─────────────────────────────────────────────────────┐
│ Agent Plane                                         │
│ Interaction / Conversation / Workflow / Policy      │
│ Knowledge / Profile / Capability / Recovery          │
└──────────────┬───────────────────────┬──────────────┘
               │ model request         │ capability/task
               ▼                       ▼
┌─────────────────────────┐  ┌────────────────────────┐
│ Model Plane             │  │ Device Plane           │
│ Protocol / Routing      │  │ Registry / Scheduler   │
│ Provider / Key / Quota  │  │ Task / Artifact Coord. │
└─────────────┬───────────┘  └────────────┬───────────┘
              ▼                           ▼
      Cloud / Local Models        Device Agents / Gateways
```

三个 Plane 可以由一个桌面程序启动和展示，但仍保持独立状态所有权。模型路由和设备调度虽然都叫
“路由”，领域约束不同，不共享同一个调度器。

## 4. 垂直 Layer

```text
L6 Experience      CLI、语音、Mobile、Desktop、Web、IDE
L5 Composition     Project Profile、经验集合、场景组合、长期偏好
L4 Intelligence    意图、规划、经验提取/检索、推理、摘要
L3 Orchestration   Conversation、Workflow、事件、Knowledge、策略、路由和调度
L2 Contract        Tool、Capability、Execution、Experience、Preference、Artifact、Event、Approval
L1 Runtime/Adapter Tool Executor、Store/Index、Provider、A2A、设备和平台 Adapter
L0 Infrastructure  文件、数据库、索引、进程、网络、模型、CPU/GPU、传感器和执行器
```

上层依赖下层的稳定抽象；结果和事件可以向上返回，底层不能反向依赖具体产品场景。跨 Plane 只在
明确边界使用网络协议，同进程 Layer 之间优先使用语言级接口和类型。

| Layer | Agent Plane | Model Plane | Device Plane |
| --- | --- | --- | --- |
| L6 | CLI、语音、持续会话 | 模型控制台 | 设备与任务看板 |
| L5 | Profile、经验集合、场景组合 | 路由配置 | 环境/设备 Profile 投影 |
| L4 | Planner、经验提取/检索、解释与摘要 | 模型能力选择建议 | 感知逻辑的领域编排 |
| L3 | Conversation、Workflow、Event、Knowledge、Policy | Provider 路由、额度 | Registry、Scheduler、Task |
| L2 | 通用能力、执行、经验和偏好契约 | Model Request/Result | Device、Capability、Artifact、Observation |
| L1 | Tool/MCP/A2A、Store/Index Adapter | 协议和 Provider Adapter | Device Runtime、Platform Adapter、Plugin |
| L0 | 文件、数据库、索引、Shell、进程 | 本地/云模型和网络 | 传感器、执行器、加速器、操作系统 |

## 5. 主要运行域

Core 是逻辑职责，不默认等于服务。为避免 Core 爆炸，目标架构收敛为以下运行域：

### Interaction Runtime

- Conversation：持续会话、话题分支和模型切换。
- Attention：打断、延后、合并和静默决策。
- Presence：当前活跃终端、交互模态和可达性。

### Orchestration Runtime

- Planner：把目标转成能力步骤，不能直接扩大权限。
- Workflow：管理依赖、暂停、恢复、验证和后台执行。
- Event Inbox：接收、去重、过期和投递后台事件。

### Trust Runtime

- Policy：综合操作风险、数据边界、副作用和认证强度。
- Authorization：挑战、用户确认、受限回执、撤销和审计。
- Identity projection：使用身份系统的受控投影，不保存原始生物特征。

### Environment Runtime

- Device coordination：设备能力、状态与执行。
- World State：可扩展实体关系和带有效期的事实/观察。
- Perception：有界感知会话、处理流水线和数据边界。

Environment Runtime 不预定义“家庭”或某种硬件；业务 Plugin 可以注册新的实体类型、关系、观察器
和动作能力。AI 只在已注册 schema 与 Policy 允许范围内组合它们。

### Knowledge Runtime

- Experience：管理候选、验证、发布、检索、反馈、取代和撤销。
- Personalization：产生和管理推断偏好候选，并通过 Profile Store 使用已确认的用户/项目偏好；
  不直接把候选写成有效 Profile，也不把偏好当作授权。
- Retrieval：按目标、范围、信任域和预算生成带来源的只读上下文投影。
- Promotion：将验证充分的经验提交到独立的 Skill/Plugin 发布与准入流程。

Knowledge Runtime 属于 Agent Plane。Model Plane 只提供可替换的提取、向量化和排序计算；Device
Plane 和远程 Agent 只提供带来源的证据或候选，不能直接改写本地经验与偏好。权威语义见
[经验与偏好](experience-and-preference.md)。

### Foundation

- Contracts、Adapters、State/Recovery、Security、Observability、Transport。
- 横切能力必须有明确所有者，Foundation 不等于共享数据库。

## 6. 状态所有权

`[不变量]` 权威所有者是唯一能推进该类状态版本的逻辑职责，不等于必须单独部署的服务。其他组件
只能通过契约取得投影；缓存失效、断线重连或迟到消息都不能反向改写权威状态。

| 状态 | 权威写入者 | 其他组件持有什么 | 投影失效或刷新方式 |
| --- | --- | --- | --- |
| Conversation、话题关系和会话上下文引用 | Agent Plane / Interaction Runtime | 有界摘要和 Workflow 引用 | 按 conversation revision 恢复，旧投影不得覆盖新话题状态 |
| Workflow、Step、ExecutionAttempt、checkpoint | Agent Plane / Orchestration Runtime | 有界执行摘要和外部执行引用 | 按 checkpoint/revision 恢复，旧投影不得覆盖新终态 |
| Event Inbox、事件投递与去重状态 | Agent Plane / Orchestration Runtime | 投递结果和事件引用 | 按 event id、source revision 和 expiry 去重/过期 |
| Subscription、Attention Decision、Presence | Agent Plane / Interaction Runtime | 当前交互视图 | Profile、终端或交互状态变化时重新计算 |
| 用户、Agent、设备的身份凭据状态与撤销 | Trust Runtime / Identity Store | 最小身份投影 | 短期缓存并检查版本、有效期和撤销状态 |
| 设备注册、在线状态、lease、Device Execution | Device Plane | 身份引用；Agent Plane 保存执行引用 | 按设备 revision/lease 刷新，身份撤销立即使注册失效 |
| Approval Challenge、Receipt 消费状态 | Trust Runtime / Authorization Runtime | task 只保存引用和验证结果 | 每次消费查询权威状态；过期、撤销或已消费即失效 |
| Capability 声明 | 签名 Manifest 的发布源 | Capability Warehouse 保存准入和激活投影 | 按发行者、稳定 id、版本和内容摘要更新 |
| Capability 准入、激活与可见性 | Agent Plane / Capability Warehouse | Planner 获得当前只读候选视图 | manifest、Policy 或 activation revision 改变时重建 |
| Provider、key pool、模型用量 | Model Plane | 模型别名和请求结果 | 通过 Model API 查询，不复制凭据或额度账本 |
| A2A Task | 远程 A2A Server | 本地 Workflow 保存带版本的状态投影 | 通过远端版本或状态流刷新，映射失败保持可见 |
| 环境实体关系、有效 Observation | Environment Runtime / World State | Workflow 保存所用版本和引用 | 按 observation expiry/revision 重新查询或观察 |
| Artifact 内容、完整性和保留生命周期 | Artifact Store | 内容标识、分类、授权引用和校验结果 | 内容不可变；授权、位置和保留状态独立刷新 |
| Artifact 访问决策 | Trust Runtime / Authorization Runtime | 短期、受众绑定的访问能力 | 到期、撤销、消费或策略变化后失效 |
| 明确 Preference 与 Profile | Agent Plane / Profile Store | 带来源和版本的只读 Profile 投影 | 以 Profile revision 重建；撤销或作用范围变化后旧投影失效 |
| Experience 及其验证、取代和撤销状态 | Agent Plane / Knowledge Runtime | 带版本与来源的检索投影 | 按 experience revision 重建索引；旧反馈不得恢复已撤销记录 |
| 推断偏好候选 | Agent Plane / Knowledge Runtime | Profile 管理入口中的候选视图 | 经用户确认后写入 Profile；拒绝、过期或冲突后失效 |
| Policy 声明 | 各声明来源（系统、组织、项目或设备本地策略） | Trust Runtime 持有经过验证的只读投影 | 按发行者、作用域和 revision 刷新；来源撤销后立即失效 |
| Policy Decision | Trust Runtime / Policy | 决策引用、关键依据和适用期限 | 动作、主体、环境、Profile 或 Policy 变化时重新判定 |

Identity Store 对“这个主体是否可信、是否已撤销”权威；Device Plane 对“这个可信设备当前是否注册、
在线以及执行到哪一步”权威。两者不能合并成一个含糊的设备状态。任何服务都不得直接读写另一
所有者的数据库。

Profile Store 只拥有已经生效的偏好与组合声明，不拥有组织安全规则或设备硬限制。每个 Policy
来源只推进自己声明的版本；Trust Runtime 组合这些只读投影形成 Policy View 并作出 Decision，不能
反向改写来源。设备本地 Policy 可以进一步收紧中央决策，不能被中央 Profile 放宽。

## 7. 部署视图

### 本机开发

```text
testcode
  → local API Control Board
  → in-process 或 local Device Control
  → Device Simulator / Linux Agent
```

### 多信任域部署示意

```text
Mobile Client + Local Authenticator
        │
        ▼
Personal Assistant Runtime
├── Model Plane → 本地模型 / 私有计算 / 允许的外部 Provider
├── A2A → Domain Agents
└── Relay/Rendezvous → Domain Gateway → Device/Service Nodes
```

Assistant Runtime 拥有会话和工作流；Relay/Rendezvous 只解决可达和消息投递，不拥有业务状态。
`[当前推荐]` Domain Gateway 主动建立安全出站连接，不公开内部节点端口。跨域网络失效时，本地域安全
策略仍然工作，高风险远程动作不得自动降级。`[候选]` 该整体拓扑允许根据延迟、成本和可靠性调整。

### 仓库策略

当前保持 Python `testcode` 与 Node/Electron API Control Board 两个仓库和独立进程。未来组件先
按清晰模块边界实现；只有生命周期、信任域、伸缩或发布节奏不同后再拆服务。是否进入 monorepo
由真实统一发布和共享协议压力决定。

## 8. 待验证决策

以下均为 `[候选]`，不是架构不变量：

- Personal Assistant Runtime 的长期部署位置和高可用方式。
- Device Control 首选长连接、消息代理或轮询。
- 实时媒体使用 WebRTC、厂商流协议还是边缘推理后只返回 Observation。
- Artifact Store 使用本地文件、共享存储还是对象存储。
- World State 使用关系模型、文档模型还是事件投影。
- 每个运行域最终是模块、进程还是独立服务。
- Dashboard 是否继续复用现有 Electron 壳。

这些选择应由基准测试、故障注入、目标设备限制和实际部署数据决定。
