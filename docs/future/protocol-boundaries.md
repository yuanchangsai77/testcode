# 未来平台协议边界

## 文档职责

本文回答“哪里必须有协议、哪里只需要内部接口、哪些语义必须稳定”。它不冻结最终 JSON、Protobuf、
数据库字段或 transport；这些要在原型、性能测试和目标平台验证后进入 reference 文档。

## 1. 协议的四个层次

每个跨进程边界都应分开考虑：

1. **领域语义**：设备、任务、事件或授权表达什么。
2. **消息表示**：JSON、Protobuf 或其他编码。
3. **传输**：HTTP、WebSocket、gRPC、MQTT、IPC 等。
4. **安全 Profile**：身份、加密、授权、重放保护和审计。

长期优先稳定领域语义和兼容规则。表示、传输和部署可以根据吞吐、延迟、电量、平台 API 与网络
条件调整。

## 2. 哪些边界需要协议

| 边界 | 需要什么 | 原因 |
| --- | --- | --- |
| 同进程 Core/Layer | 内部接口与类型，不需要 wire protocol | 避免为模块化支付网络复杂度 |
| Agent Plane ↔ Model Plane | 网络协议 | 独立进程、Provider 替换和访问治理 |
| Agent Plane ↔ Device Plane | 能力目录/任务 API | 独立状态所有者和潜在远程部署 |
| Device Control ↔ Device Agent | 设备控制协议 | 注册、租约、任务、取消、结果和重连 |
| Agent ↔ 独立 Agent | A2A 或兼容适配 | 长任务、消息、artifact 和异步状态 |
| 服务 ↔ Artifact Store | 数据访问协议或同机接口 | 大型数据生命周期独立于控制消息 |
| Dashboard ↔ 独立服务 | 管理/查询 API | UI 不直接访问数据库 |
| Runtime ↔ 本地插件 | Plugin/Capability SDK | 通常同进程，不强制网络化 |
| Adapter ↔ 厂商设备 | 厂商或平台协议 | 差异封装在 Adapter 内，不上漏 |
| Model Plane ↔ Provider | Provider 原生协议 | 由 Model Plane 适配，不暴露给 Agent |

如果两个组件最终部署在同一进程，使用接口即可；未来拆进程时在同一语义契约外增加 transport
adapter，不应提前把所有内部调用远程化。

## 3. 通用消息最小语义

跨进程控制消息通常需要以下语义，字段名仅为说明：

- 协议/契约版本。
- 唯一消息标识，用于确认和去重。
- 消息类型。
- 发送方身份引用。
- 关联标识，例如 workflow、task 或 conversation。
- 产生时间与权威顺序/revision；不能只靠不同节点的墙上时钟决定先后。
- 需要重试时使用有明确发行方、作用域和保留期的幂等标识。
- 有界 payload 或 Artifact 引用。
- trace 关联。
- 安全上下文、Policy/授权版本或签名引用，不内嵌长期秘密。

不是所有消息都必须机械包含全部字段；每个协议 Profile 只选择其恢复、安全和审计需要的最小集。
执行竞态、deadline、lease 和迟到结果的规范语义由[运行时领域](runtime-model.md#81-竞态时间与恢复语义)
维护，各 wire protocol 只负责无损表达。

## 4. Agent Plane ↔ Model Plane

`[当前推荐]` 复用 API Control Board 已有 OpenAI-compatible 接口，之后按真实需求补充 streaming。必须
稳定的语义只有：

- 模型别名/模型请求目标。
- 有序消息与工具定义。
- tool call 标识、参数和 tool result 的无损往返。
- 请求身份、额度边界、超时和错误分类。
- trace 关联与用量结果。

具体 URL、header 名、stream 格式和 Provider 字段由 Model Plane reference 文档管理。数据分类和
是否允许云端在 Agent Policy 阶段先过滤；Model Plane 只能在允许的 Provider 集合内路由。

## 5. Agent Plane ↔ Device Plane

该边界需要两组契约：

- 能力目录：列出可发现能力、兼容版本、风险/副作用提示和实现状态。
- 执行接口：提交、查询、取消执行，并返回结构化结果或 Artifact 引用。

Agent Plane 看到的是能力和执行引用，不读取 Device Registry 内部表，也不直接选择厂商命令。
设备能力激活后继续适配成统一 Tool。

## 6. Device Control ↔ Device Agent

这是必须单独设计的设备控制协议。最小稳定语义包括：

- 设备身份、注册/撤销和所支持协议版本。
- 能力列表及兼容版本。
- 心跳/租约和有时间语义的动态状态。
- Device Execution 的身份、能力、幂等标识、约束和 deadline。
- 接受/拒绝、进度、结果、失败和取消确认。
- revision/去重与断线恢复位置。
- Artifact 准备和可用引用。

`[当前推荐]` 设备主动建立安全出站连接。`[候选]` HTTPS + 长连接作为首个实现，MQTT、gRPC、轮询或厂商网关均
可由性能、电量和平台限制替换。传输改变不应改变上面的领域语义。

## 7. MCP 的边界

MCP 继续用于外部工具和资源的发现与调用，并通过现有能力仓库按需打开 manifest、激活少量叶子
能力。MCP tool 激活后仍经过统一 policy、approval 和 logging。

以下情况可以直接使用 MCP Adapter，不必创造新协议：

- 一个外部服务主要暴露请求—响应式工具。
- 资源可以按索引和引用按需读取。
- 不需要平台维护独立设备身份、租约或物理执行状态。

以下语义不要求 MCP 承担：

- Device Agent 注册、心跳、资源状态和任务 lease。
- 物理动作的现实效果验证。
- 实时媒体会话和大型数据传输。
- 独立 Agent 的长任务、协作和授权等待。

如果设备厂商已经提供 MCP server，可以作为设备能力的一种 Adapter 接入；Device Plane 仍拥有
设备身份和执行状态。MCP 的具体 wire 字段由 MCP 规范和
[当前 MCP 集成设计](../extensions/mcp-integration.md)管理，本蓝图不重复定义。

## 8. Agent ↔ Agent（A2A）

独立 Agent 之间优先复用 A2A 的 Agent Card、Message、Task、Artifact、状态更新和异步能力。
具体 Agent 技能由其 Agent Card/manifest 动态声明，不由平台枚举。首个原型以固定的
[A2A 1.0.0 规范](https://a2a-protocol.org/v1.0.0/specification/)为设计基线；进入实现时必须在
`docs/reference/` 记录实际支持版本、扩展和状态映射，不能让浮动的 `main` 分支静默改变契约。

本平台只定义必要映射：

- A2A Task 权威状态属于远程 Agent。
- 本地 Workflow 保存 task id、context id、版本和状态投影。
- `TASK_STATE_AUTH_REQUIRED` 映射为本地等待授权状态。
- 外部状态与本地状态不可互相覆盖历史，映射失败必须可见。
- 向远程 Agent 发送的 Experience/Preference 只是不超过任务用途、受众和期限的最小投影。
- 远程返回的方法和总结只能映射为带来源的本地 Experience Candidate，不能直接修改 Experience、
  Profile 或发布 Skill。

Approval Challenge、Approval Receipt、action digest 和用户认证器签名属于平台授权扩展，不假定是 A2A
标准字段。挑战可以通过 A2A extension 或引用告知，但敏感凭据通过独立安全通道直接交给请求方。

## 9. Event Delivery

Event 协议用于后台来源向 Event Inbox 投递，不允许来源直接操作 UI。最小语义包括：

- event identity、type、source。
- created/expiry 时间。
- priority 和 dedupe 语义。
- 有界摘要或正文 Artifact 引用。
- 可选 workflow/conversation 关联。

`[当前推荐]` 至少一次交付，消费者幂等。是否使用 Webhook、消息代理、A2A push、长连接或本地队列由
部署决定。同一 source stream 需要可恢复顺序，跨来源不要求全局排序。

## 10. Artifact 与实时媒体

Artifact 协议最少表达：

- 稳定内容身份。
- 媒体类型、大小和完整性。
- 数据分类和允许的信任域。
- 可用位置或短期访问授权。
- 过期与保留策略。

`[候选]` 具体 URI scheme、对象存储和传输工具。控制消息不携带无界正文。

实时媒体不等同于 Artifact。直播需要会话身份、短期密钥、来源/接收方、最大时长、媒体约束和
终止语义；`[候选]` WebRTC、局域网流或边缘分析后只返回 Observation。

## 11. Authorization 协议

授权协议必须能证明“谁在何时批准了哪个不可变动作给哪个执行方”，关键语义包括：

- approval identity。
- 原始 workflow/task 和请求 Agent。
- 用户主体和签发方。
- 规范化 action digest 与资源范围。
- audience。
- nonce、签发时间、过期时间和一次性消费。
- 所需/实际 assurance 与 policy 版本。
- 可验证签名和撤销/消费结果。

字段名、签名格式和凭据体系后续选择。若认证器使用生物识别，它只在受信终端本地解锁密钥，不进入
消息正文。

## 12. 版本与兼容

- 能力以可追溯发行者、稳定命名域、内容摘要和 major compatibility 表达；minor 能力通过协商或
  可选字段扩展，撤销或发行者变化不能伪装成普通 minor 更新。
- 消费方忽略明确允许忽略的未知可选字段，拒绝未知必需语义。
- 协议版本、能力版本和 Agent/Device 软件版本分别管理。
- 外部协议保留原始状态和错误信息，内部映射失败不得静默降级。
- 每个跨进程边界必须有契约测试、旧版本样本和故障样本。

## 13. 何时冻结字段

只有同时满足以下条件，字段才进入 `docs/reference/`：

1. 至少一个真实生产者和一个真实消费者已经实现。
2. 完成断线、重复、乱序、取消和版本不兼容测试。
3. 字段确实用于恢复、安全、兼容或审计，而不是“以后可能有用”。
4. 性能数据证明表示和传输满足目标设备。
5. 已定义兼容、弃用和迁移策略。
