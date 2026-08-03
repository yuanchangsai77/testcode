# 多模型、多设备 Agent 平台目标架构

## 文档职责

本文是 `testcode`、同目录的 API Control Board（`~/test`）以及未来 Device Fabric 的跨项目目标架构。它负责说明：

- 两个现有项目在未来平台中的定位与边界
- Agent、模型网关和设备控制三个平面如何协作
- 能力、设备、任务、制品和项目 Profile 的统一领域模型
- 控制协议、数据协议、安全、调度、恢复和可观察性要求
- 从当前实现演进到目标架构的阶段顺序与验收标准

本文描述目标状态，不表示所有能力已经实现。当前 `testcode` 行为以
[总体架构](architecture.md)和各专项文档为准；当前优先级以[演进路线图](roadmap.md)为准；
API Control Board 的当前行为以 `~/test/README.md`、`~/test/docs/concepts.md` 和
`~/test/USAGE.md` 为准。

本文不取代具体字段参考。任何进入实现阶段的稳定接口，都应另行写入对应项目的 reference
文档，并通过契约测试保护。

## 1. 结论

平台采用三个相互独立、通过稳定协议协作的运行平面：

```text
Agent Plane
  testcode：理解任务、组织上下文、驱动模型—工具循环、安全执行、任务恢复

Model Plane
  API Control Board：统一模型入口、协议转换、Provider 路由、密钥、额度和健康状态

Device Plane
  Device Control Plane + Device Agents：设备发现、能力注册、调度、远程执行和数据传输
```

核心原则是：

> 设备是能力载体，项目是能力与策略的组合，Agent Runtime 负责形成任务，设备控制面负责选择执行节点，模型网关负责选择模型 Provider。

三个平面可以由同一桌面应用统一展示和启动，但不得因此混合状态所有权或绕过服务边界。

## 2. 当前基线

### 2.1 `testcode`

`testcode` 当前是以大模型驱动的 CLI workbench，已经具备：

- 单轮和多轮会话、模型—工具循环
- OpenAI-compatible 模型客户端
- 内建文件、搜索、Shell、测试、patch 和只读 Git 工具
- 统一工具定义、风险分级、审批和日志
- ContextLoader、ToolProvider、ResourceProvider 扩展边界
- Skill、MCP 和统一能力仓库
- 能力目录、manifest、按需激活、预算和生命周期
- TUI、会话持久化和运行日志

当前缺少的基础能力包括预算化上下文打包、可恢复 checkpoint 和明确任务状态。这些仍是进入
远程 Agent 或设备调度前的最高优先级。

### 2.2 API Control Board（`~/test`）

API Control Board 当前是本地多 Provider 模型代理，已经具备：

- OpenAI Chat、OpenAI Responses 和 Anthropic Messages 兼容入口
- Anthropic Messages 中间格式与双向协议转换
- 固定 Provider 端口和按模型名自动路由
- 内建及动态 Provider 注册
- 上游密钥池、子密钥、额度和用量统计
- 健康检查、模型发现、诊断调用
- Web 控制台和 Electron 桌面壳

它当前管理的是模型请求，不管理 Agent 任务、设备身份或远程执行。

### 2.3 可立即成立的集成

`testcode` 已调用 `/v1/chat/completions`，API Control Board 已暴露同一兼容入口，因此二者
当前可以通过配置直接连接：

```text
testcode OpenAICompatibleModelClient
        → API Control Board /v1/chat/completions
        → selected Provider
```

正式依赖这条链路前，必须用跨项目契约测试验证工具 schema、tool call、tool result、错误和
用量在协议转换前后保持正确。

### 2.4 当前集成缺口

- `testcode` 当前模型配置只有网关地址、模型名和超时，没有客户端 Bearer 凭据字段。
- API Control Board 当前只在请求携带符合本地子密钥格式的 Bearer header 时校验子密钥；
  缺少或不符合该格式的 header 不会触发强制拒绝。
- 两个项目之间尚无独立的黑盒契约测试，不能仅凭接口路径相同推断工具调用完全兼容。
- API Control Board 已有 streaming 路径，`testcode` 当前模型主链路仍使用非流式请求。
- 两个项目分别管理配置和日志，尚未建立统一 trace header 和跨服务诊断入口。

因此，当前连接只应视为本机可信环境下的可用集成；在监听非回环地址、接入设备或多用户之前，
必须先完成认证强制、凭据注入和契约测试。

## 3. 目标与非目标

### 3.1 目标

- 使用统一能力语义承载本地工具、Skill、MCP 和设备能力。
- 让模型 Provider 路由、能力选择和设备调度各自保持单一职责。
- 支持设备动态上线、离线、状态变化和能力版本演进。
- 所有执行能力复用 `testcode` 的 policy、approval、logging 和 prompt discipline。
- 大型数据通过独立数据面传输，任务消息只携带受控引用。
- 中断、超时、断线和重复派发后可以安全恢复。
- 允许统一桌面体验，同时保持服务可独立运行、测试和部署。
- 从单机纵向闭环开始验证，而不是一次建设全部平台和硬件适配器。

### 3.2 非目标

- 不把 `testcode` 改造成模型 Provider 网关。
- 不把 API Control Board 改造成设备调度器或 Agent 执行引擎。
- 不要求所有平台使用同一种实现语言或进程形态。
- 不假设 iOS、手表和嵌入式系统能运行完整通用 daemon。
- 不把 MCP 扩展成所有设备状态和数据传输语义的唯一协议。
- 不允许远程工具绕过本地或设备侧安全策略。
- 不在当前阶段重排成包含大量空模块的最终 monorepo。

## 4. 设计原则

### 4.1 能力优先

调度依赖能力契约，不依赖设备型号。`audio.play` 可以由手机、音响、笔记本或电视实现；
设备类型和平台只作为约束和评分信号。

### 4.2 存在、激活、授权和调度分离

```text
存在于能力仓库
  ≠ 当前对模型可见
  ≠ 已获准执行
  ≠ 已选择某台设备
```

能力仓库决定发现与激活；安全层决定授权；设备调度器决定执行节点。

### 4.3 控制面与数据面分离

注册、心跳、任务、取消和进度属于控制面。视频、音频、模型权重和大结果属于数据面。
控制消息不得承载无界二进制正文。

### 4.4 出站连接优先

Device Agent 默认主动连接控制端并维持租约或长连接，不要求控制端直接访问设备端口。这有利于
穿越 NAT、防火墙和移动网络，也减少设备暴露面。

### 4.5 中央策略与本地策略同时生效

控制端可以拒绝派发，设备端也必须独立拒绝超出本地权限、温度、资源或用户授权范围的任务。
任一侧拒绝都不能被另一侧覆盖。

### 4.6 契约先行、纵向验证

先固定最小领域契约并完成一个真实端到端用例，再扩展更多传输、设备和项目 Profile。

## 5. 总体架构

```text
用户 / IDE / 自动化入口
          │
          ▼
┌──────────────────────────────────────────────────────────────┐
│ Agent Plane: testcode                                        │
│                                                              │
│ Interaction   Planner/Agent Loop   Context & Checkpoint       │
│ Safety        Capability Warehouse Observability              │
└───────────────┬───────────────────────────────┬───────────────┘
                │ model protocol                │ capability/task
                ▼                               ▼
┌──────────────────────────────┐   ┌────────────────────────────┐
│ Model Plane                  │   │ Device Control Plane       │
│ API Control Board            │   │ Registry / Scheduler       │
│                              │   │ Policy / Task Coordinator  │
│ Protocol conversion          │   │ Artifact Coordinator       │
│ Provider routing             │   └──────────────┬─────────────┘
│ Keys / quota / health        │                  │ outbound session
└──────────────┬───────────────┘          ┌───────┼────────┐
               │                          ▼       ▼        ▼
        Cloud / Local Models          Linux    Mobile   Gateway
                                      Agent     Agent     Agent
                                         │        │        │
                                      capability plugins/adapters

统一 Dashboard（可选聚合入口）
  ├── Model Plane Admin API
  ├── Agent Plane Runtime API
  └── Device Plane Registry/Task API
```

## 6. 平面与组件职责

### 6.1 Agent Plane

由 `testcode` 演进而来，负责：

- 接收用户意图并建立 run/session
- 组织项目规则、上下文、记忆和 checkpoint
- 选择模型别名或模型等级，但不管理真实 Provider 密钥
- 驱动模型—工具循环
- 管理能力目录、manifest 和 activation set
- 对所有本地、MCP 和设备能力执行风险判断与审批
- 将远程任务结果转换为统一 ToolResult
- 输出可恢复、可验证的执行总结

Agent Plane 不负责设备心跳、GPU 资源锁或大文件传输实现。

### 6.2 Model Plane

由 API Control Board 承担，负责：

- 暴露稳定的 OpenAI/Anthropic 兼容入口
- 将不同客户端协议转换成内部桥接格式
- 将模型别名或模型 slug 路由到 Provider
- 管理 Provider endpoint、认证方式和 key pool
- 强制执行客户端子密钥、Provider/模型/endpoint 权限和额度
- 记录模型用量、延迟、错误和健康状态
- 在不改变客户端契约的前提下切换云模型或本地模型

Agent Plane 决定“需要哪一类模型”；Model Plane 决定“由哪个已配置 Provider 提供它”。

### 6.3 Device Control Plane

未来新增，负责：

- 设备注册、配对、身份验证和撤销
- 能力 manifest、版本和状态维护
- 心跳、租约和在线状态判断
- 任务排队、调度、取消、超时和重试
- 数据位置规划和 artifact transfer 协调
- 设备级策略、隐私约束和审计
- 向 Agent Plane 暴露稳定的 capability source 或 task API

第一版可以作为 `testcode` 旁的轻量服务或可替换组件实现；在生命周期和并发需求明确前，不必
提前独立成复杂集群。

### 6.4 Device Agent Runtime

设备端通用职责为：

- 建立出站安全连接并续租
- 上报设备描述、能力和动态状态
- 拉取或接收已授权任务
- 通过本地 adapter/plugin 执行能力
- 上报进度、结构化结果和 artifact 引用
- 在断线、取消和进程重启后恢复或终止任务
- 执行设备本地权限与资源保护

通用 runtime 可以复用协议、任务状态和插件生命周期；平台 API、后台限制和硬件调用由适配器
负责。资源受限设备可以实现裁剪版 Agent 或通过 Gateway Agent 接入。

### 6.5 Project Composer

Project Composer 第一阶段只是 Profile 解析与校验器，负责决定：

- 启用哪些能力来源和模块
- 允许哪些设备、角色或信任域
- 使用哪个模型别名和回退策略
- 数据是否允许离开本机、局域网或私有域
- 任务默认超时、成本和交互要求
- 需要启动或连接哪些已有服务

第一阶段不生成重复项目代码，不复制 runtime，也不自动安装永久能力。只有部署组合稳定后，
才考虑生成 compose/systemd/desktop 启动描述。

### 6.6 统一 Dashboard

API Control Board 的 Web/Electron 界面可以演进为统一入口，但只聚合各平面的 API：

| 页面 | 数据所有者 |
| --- | --- |
| 模型、Provider、密钥和额度 | Model Plane |
| Agent run、checkpoint 和能力激活 | Agent Plane |
| 设备、任务、状态和制品传输 | Device Plane |
| 项目 Profile | Project Composer/Profile Store |

Dashboard 不直接修改其他服务的数据库或内部文件。

## 7. 统一领域模型

### 7.1 Capability Descriptor

能力是调度与授权的最小语义单位，建议包含：

```yaml
id: model.inference
version: 1.0.0
display_name: Local model inference
input_schema: {}
output_schema: {}
traits: [compute, gpu, network]
risk: execute
side_effect: none
streaming: false
resource_requirements:
  gpu_memory_mb: 12000
limits:
  max_input_bytes: 104857600
```

关键约束：

- `id + major version` 形成兼容边界。
- schema 负责结构，traits 负责发现和策略匹配。
- risk 进入 `testcode` 的统一 policy，不由设备来源自行降级。
- side effect 决定是否允许自动重试和并行执行。
- 动态负载不写入 descriptor，而写入设备状态。

### 7.2 Device Descriptor

```yaml
device_id: gpu-server-01
device_type: server
platform: linux
roles: [compute, storage]
agent_version: 0.1.0
protocol_versions: [device-control.v1]
trust_domain: home-lan
capabilities:
  - id: model.inference
    version: 1.0.0
labels:
  location: study
  owner: local-user
```

`device_id` 必须稳定且不可从易变 IP 或显示名称推导。密钥、token 和精确敏感硬件信息不得进入
普通 descriptor 或 prompt。

### 7.3 Device Status

动态状态独立上报，并带时间戳与租约：

```yaml
observed_at: 2026-08-03T12:00:00Z
lease_expires_at: 2026-08-03T12:00:30Z
online: true
busy_slots: 0
total_slots: 1
cpu_load: 0.32
gpu_free_memory_mb: 18000
network_class: ethernet
battery_percent: null
temperature_c: 54
```

调度器不得把过期状态当作实时事实。对隐私敏感或不稳定字段应限制采集频率和可见范围。

### 7.4 Task

```yaml
task_id: task-001
idempotency_key: run-123-step-4-attempt-family
capability:
  id: model.inference
  major_version: 1
input:
  prompt_object: artifact://sha256/abc123
constraints:
  privacy: private
  network_scope: lan
  deadline: 2026-08-03T12:05:00Z
execution:
  timeout_sec: 180
  retry_policy: safe_only
trace:
  trace_id: trace-123
  parent_run_id: run-123
```

任务生命周期为：

```text
created → queued → leased → running → succeeded
                    │          ├── failed
                    │          ├── cancelled
                    │          └── expired
                    └──────────→ expired
```

状态转换必须有单调序号或等价并发控制。迟到的旧事件不能覆盖更新状态。

### 7.5 Artifact Reference

大型输入输出只通过引用进入任务：

```yaml
artifact_id: sha256:abc123
media_type: video/mp4
size_bytes: 734003200
classification: private
locations:
  - scheme: local
    uri: local://videos/input.mp4
integrity:
  algorithm: sha256
  digest: abc123
expires_at: 2026-08-04T00:00:00Z
```

位置是可变属性，内容标识不变。控制端先规划可达位置和传输，再把可消费的引用交给执行节点。

### 7.6 Project Profile

```yaml
profile_version: 1
project:
  id: home-jarvis

model:
  preferred_alias: reasoning-local-first
  fallback_aliases: [reasoning-cloud]

capability_sources:
  builtin: true
  skills: true
  mcp: true
  devices: true

devices:
  allow_roles: [audio, display, compute]
  allow_trust_domains: [home-lan]

policies:
  private_data:
    allow_cloud_model: false
    allow_public_network: false
  physical_side_effects:
    require_confirmation: true

routing:
  model.inference:
    prefer_labels:
      location: local
  audio.play:
    prefer_roles: [active-room-audio]
```

Profile 只表达需求和策略，不记录在线状态，也不把某个瞬时设备选择固化为项目结构。

## 8. 协议边界

### 8.1 Agent Plane ↔ Model Plane

第一阶段继续使用现有 OpenAI Chat 兼容协议。必须补充并固定：

- Bearer 子密钥配置和强制认证模式
- 工具定义、并行 tool calls 和 tool result 的转换一致性
- request/run trace header
- 错误分类：认证、额度、模型不存在、上游超时、可重试服务错误
- 非流式主链路契约测试，之后再启用 streaming
- 模型别名或 slug 的稳定解析规则

协议转换不得改变工具调用 ID、参数 JSON、消息顺序或停止原因。

### 8.2 Agent Plane ↔ Device Plane

Device Plane 作为能力仓库的新 source 暴露外层目录、manifest 和激活入口。设备能力激活后，
通过 adapter 转换成普通 `Tool`，继续复用：

```text
ToolRegistry → schema validation → policy → approval → execution → logging
```

Agent Plane 不直接读取 Device Registry 数据库，也不绕过统一 ToolResult。

### 8.3 Device Control Protocol

最小控制协议需要覆盖：

- enroll / revoke
- register / update manifest
- heartbeat / lease renewal
- task offer / accept / reject
- progress / result / failure
- cancel / cancellation acknowledged
- artifact prepare / artifact available

初始实现建议使用 HTTPS 完成注册与查询，使用设备主动建立的 WebSocket 或等价长连接传递任务和
事件。MQTT 可以作为路由器、嵌入式或弱网适配器，但不作为第一版的并行主协议。

每条控制消息至少包含：

- protocol version
- message id
- device/task identity
- timestamp 与可接受时钟偏差
- trace id
- monotonic sequence 或 revision
- 可验证的发送方身份

### 8.4 MCP 的位置

MCP 继续作为通用外部工具和资源协议。若某设备已经提供 MCP server，可以通过现有
`MCPToolboxSource` 接入；但设备身份、心跳、任务租约、调度和 artifact transfer 不塞入 MCP
专用旁路，也不要求 MCP 承担完整 Device Control Protocol。

### 8.5 数据面

数据面根据规模和拓扑选择实现：

| 场景 | 首选方式 |
| --- | --- |
| 小型结构化输入输出 | 控制消息内有界 JSON |
| 局域网文件 | HTTPS object endpoint、SSH/rsync 或共享存储 |
| 大型持久制品 | S3-compatible object store |
| 实时音频或画面 | WebRTC 或专用有界流 |
| 同机进程 | 文件引用或本地 IPC |

传输实现必须验证大小、hash、媒体类型、权限和过期时间。任务完成不等于制品已经安全持久化。

## 9. 任务拆解与调度

### 9.1 Planner 与 Orchestrator

Planner 由模型驱动，负责把用户目标拆成能力步骤；Orchestrator 负责维护可执行状态和依赖，不能
只依赖模型自然语言记忆完整流程。

```text
用户目标
  → Planner 产生能力步骤和依赖
  → Orchestrator 校验输入、策略和状态
  → Scheduler 为设备步骤选择节点
  → Executor 执行本地/MCP/设备 Tool
  → checkpoint 记录结果和下一步
```

### 9.2 调度流程

设备选择采用“硬过滤后评分”：

1. 过滤离线或租约过期设备。
2. 匹配 capability id、major version 和 schema。
3. 应用信任域、隐私、网络范围和用户授权等硬策略。
4. 检查资源、温度、电量、并发槽位和 deadline。
5. 按本地性、延迟、负载、成本、能耗和用户偏好评分。
6. 创建有期限的任务 lease。
7. 设备接受后进入运行；拒绝或 lease 超时则重新调度。

评分规则只能在满足硬约束的候选集内生效。高分不能覆盖隐私或权限限制。

### 9.3 重试与副作用

- 纯读取或内容寻址计算可以在幂等键保护下自动重试。
- 文件写入需要版本/hash 前置条件。
- 播放、通知、震动等可重复但用户可感知的动作需要去重窗口。
- 门锁、购买、删除、设备控制等不可逆动作不得自动转移重试。
- 控制端失去确认时必须查询任务状态，不能直接假设失败并重复执行。

### 9.4 回退

回退是策略决策，不是固定设备列表：

```text
本地设备满足隐私与资源约束
  → 本地执行
否则私有服务器满足约束
  → 私有服务器执行
否则策略允许云端
  → 云端能力或模型 API
否则
  → 明确 blocked，并给出缺失条件
```

## 10. 安全与信任模型

### 10.1 身份分层

必须区分：

- 用户身份
- Agent Plane 客户端身份
- Model Plane 子密钥身份
- Device Agent 设备身份
- Provider 上游凭据
- artifact 访问凭据

这些身份不得复用同一长期 token。

### 10.2 配对和设备身份

- 首次加入需要一次性配对码、用户确认或管理员签发。
- 配对后使用设备专属凭据和短期会话令牌。
- 支持轮换、撤销和丢失设备清理。
- 控制端记录设备公钥/证书指纹，而不是信任显示名称或 IP。
- 资源受限设备可以由受信 Gateway 代理，但必须保留原始设备身份和来源。

### 10.3 最小权限

权限绑定到 capability 与约束，而不是笼统的“信任设备”：

```text
允许 speaker-01 执行 audio.play
不等于允许 audio.capture
不等于允许读取其他设备 artifact
```

物理动作、录音、相机、定位、健康数据和公网传输默认需要更高风险等级及明确授权。

### 10.4 双重策略执行

Agent Plane 在派发前执行用户和项目策略；Device Agent 在执行前再次检查本地权限、资源和用户
存在条件。两侧都记录拒绝原因，但不得把密钥、原始隐私数据或完整敏感参数写入普通日志。

### 10.5 Model Plane 加固

在从回环地址扩展到局域网前，API Control Board 必须支持并启用：

- 缺少或错误子密钥时默认拒绝
- endpoint、Provider、模型和每日额度限制
- 默认回环监听；非回环监听需要显式配置
- TLS 或受信反向代理
- 上游密钥与客户端子密钥分离
- 管理 API 与推理 API 权限分离

`testcode` 需要增加受保护的网关凭据配置并发送 Authorization header。

### 10.6 执行隔离

远程 Device Agent 不能把“通用 shell”作为默认能力。优先暴露结构化、最小权限的能力插件。
确需执行任意代码时，应使用容器、受限用户、资源配额和独立工作目录；进程生命周期管理不能替代
操作系统级隔离。

## 11. 状态、存储与恢复

### 11.1 状态所有权

| 状态 | 唯一所有者 |
| --- | --- |
| 对话、run、checkpoint、activation set | Agent Plane |
| Provider、key pool、子密钥、模型用量 | Model Plane |
| 设备、租约、任务、设备事件 | Device Plane |
| 大型输入输出与中间结果 | Artifact Store |
| 项目声明和策略 | Profile Store |

服务之间通过 API 和事件共享投影，不直接读写对方数据库。

### 11.2 Agent checkpoint

远程执行加入后，checkpoint 至少需要保存：

- 当前计划步骤和依赖
- 远程 task id、device id 和最近已知 revision
- 幂等键和重试类别
- 输入输出 artifact 引用
- 最近一次验证结果
- active capabilities 与协议版本

恢复时先向 Device Plane 查询权威任务状态，再决定继续等待、取回结果或重新调度。

### 11.3 离线与重连

- 心跳丢失只表示租约过期，不立即证明任务未执行。
- Device Agent 重连后使用设备身份和最后确认的 sequence 恢复事件流。
- 控制端接受重复事件但按 message id/revision 去重。
- 设备不得在任务 lease 过期后继续启动新副作用；已运行任务按 capability 策略停止或完成。
- 冲突状态进入人工可见的 reconciliation，而不是静默覆盖。

## 12. 可观察性

三个平面使用统一关联字段：

- trace id：一次端到端用户目标
- run id：一次 Agent 执行
- step id：计划中的一步
- model request id：一次模型调用
- task id：一次设备任务
- artifact id：一个内容或制品
- device id：执行节点

最低指标包括：

- 模型请求成功率、延迟、token、Provider 回退
- Agent 轮数、工具调用、审批、恢复和验证状态
- 设备在线率、租约过期、任务排队/运行时长和失败分类
- artifact 传输量、校验失败、缓存命中和过期清理
- 调度候选数、拒绝原因和最终选择依据

面向用户的摘要只展示概念、结果和可行动原因；完整事件留在可查询日志中。

## 13. 配置与部署

### 13.1 配置边界

- `testcode` 配置：Agent 策略、模型网关地址、模型别名、能力来源和运行限制。
- API Control Board 配置：Provider、模型列表、监听地址、上游凭据、子密钥和额度。
- Device Plane 配置：注册服务、信任根、调度策略、artifact backend 和租约。
- Project Profile：引用以上能力与策略，不复制真实密钥。

两个项目不得共享或互相修改同一个 `.env`。Profile 只引用命名配置和环境变量。

### 13.2 本机开发拓扑

```text
testcode CLI
  → 127.0.0.1:3000 API Control Board
  → 127.0.0.1:<device-control-port> Device Control Plane
  → local Linux Device Agent
```

访问 `127.0.0.1` 的本地模型和服务时，测试与运行环境必须绕过代理变量，并覆盖用户终端与 Agent
环境不同的情况。

### 13.3 家庭局域网拓扑

```text
Laptop: Dashboard + Agent Plane + Model Plane + Device Control Plane
GPU Server: Linux Device Agent + inference capability
Router/Gateway: discovery and embedded-device bridge
Phone/Speaker/Projector: constrained Device Agents or vendor adapters
```

非回环连接必须认证和加密。设备发现只产生候选，不自动建立信任。

### 13.4 仓库策略

当前保持两个仓库和独立进程：

- `testcode`：Python Agent Runtime
- `~/test`：Node/Electron Model Gateway

Device Plane 在最小纵向实现时可以先放在 `testcode` 的清晰扩展边界中；当它需要独立常驻、多人
开发、独立发布或多 Agent 共享时再拆分服务/仓库。

只有出现统一发布、桌面应用内嵌进程管理、共享协议频繁同步等实际压力时，才考虑 monorepo。
即使进入 monorepo，也保留进程和状态边界。

## 14. 分阶段演进计划

### 阶段 0：稳定当前 Agent Runtime

对应 `testcode` 路线图 P0：

- ContextPackager 和 prompt 总量限制
- Session checkpoint 与 cold archive
- 轻量 task plan、恢复和验证状态
- 中断、模型错误、工具错误和审批拒绝后的恢复

退出条件：长任务能稳定恢复，状态不依赖模型猜测完整历史。

### 阶段 1：模型平面正式集成

- 将 API Control Board 作为 `testcode` 推荐的本地模型网关之一。
- 为 `testcode` 增加网关 Bearer 凭据配置。
- 为 API Control Board 增加强制认证和监听地址策略。
- 建立 Chat 工具循环跨项目契约测试。
- 验证模型不存在、401/403、429、超时、5xx 和协议错误映射。
- 验证 `NO_PROXY`/`no_proxy` 下的回环访问。

退出条件：任一受支持 Provider 都能完成至少两轮工具调用，协议转换不破坏工具语义。

### 阶段 2：设备领域契约与模拟器

- 设计并版本化 Capability、Device、Task、Artifact 字段参考。
- 在能力仓库增加 `device` source 和有界 manifest。
- 实现内存 Device Registry、租约和任务状态机。
- 使用设备模拟器测试离线、迟到、重复事件、取消和版本不兼容。
- 不接入真实手机、手表或物理副作用。

退出条件：模拟设备可以安全执行只读能力，重复派发不会重复产生结果或副作用。

### 阶段 3：Linux/GPU 纵向闭环

- 实现一个 Linux Device Agent。
- 实现一个结构化 `model.inference` 或等价 GPU 能力。
- 在本地模型和 GPU Agent 之间按在线状态、显存、隐私和负载选择。
- 通过 artifact reference 传递大型输入输出。
- 验证 Agent/控制端重启、任务超时和结果回收。

退出条件：同一能力有至少两个候选节点，调度选择可解释，失败回退符合策略。

### 阶段 4：Project Profile 与统一 Dashboard

- 实现 Profile schema、校验和启停解析。
- Dashboard 聚合模型、Agent 和设备 API。
- 页面明确标识数据来源、更新时间和离线状态。
- Dashboard 不直连其他服务数据库。

退出条件：家庭助手和 GPU 计算两个 Profile 可以复用同一 runtime，不复制核心实现。

### 阶段 5：更多设备和数据流

按真实需求逐个增加：

- Android/mobile edge
- 音响/audio
- 投影仪/display
- 路由器/gateway
- 手表和受限传感器

每次只增加平台 adapter 与能力插件，并先定义权限、后台限制、数据分类和降级行为。

### 阶段 6：远程 A2A 与多控制端

只有在 checkpoint、并发状态、设备任务和 artifact 协议稳定后，才扩展：

- 独立 subagent/team
- 多 Agent 共享 Device Plane
- 远程 A2A
- 多控制端协调与高可用

远程修改仍以 patch/artifact 回到本地审批和验证流程。

## 15. 验收策略

### 15.1 契约测试

- Agent ↔ Model：消息、tools、tool calls、tool results、错误和 usage。
- Agent ↔ Device：manifest、激活、风险、Task 与 ToolResult 映射。
- Control ↔ Agent：注册、租约、事件 revision、取消和重连。
- Task ↔ Artifact：大小、hash、权限、过期和位置迁移。

### 15.2 故障注入

必须覆盖：

- 模型 Provider 在工具调用中途失败
- 控制端派发后确认丢失
- Device Agent 执行中断线或重启
- 心跳延迟和乱序
- artifact 下载一半失败或 hash 不匹配
- 同一幂等任务被重复发送
- 能力 major version 不兼容
- 用户在排队、运行和传输阶段取消
- 本地代理变量导致回环连接异常

### 15.3 安全测试

- 无凭据、错误凭据、过期凭据和已撤销设备均被拒绝。
- 一个 capability 的授权不能横向扩展到另一个 capability。
- 私有 artifact 不能被云端或不可信设备获取。
- 未确认的物理副作用不会执行。
- 日志和错误中不出现密钥或敏感正文。
- 设备来源能力仍经过 `testcode` 的 risk policy 和 approval。

### 15.4 首个产品级验收场景

```text
用户提交一个本地视频分析请求
  → testcode 形成计划和 checkpoint
  → Device Plane 选择 GPU Server
  → 数据面传输或复用内容寻址 artifact
  → GPU Agent 返回结构化结果
  → testcode 通过 Model Plane 请求语言总结
  → 最终结果带设备、模型、验证和恢复信息
```

验收时主动模拟 GPU Server 离线、模型 Provider 429、重复派发和用户取消。

## 16. 主要风险与控制措施

| 风险 | 控制措施 |
| --- | --- |
| 过早拆成大量 Core 和服务 | 先做单进程边界与纵向用例，按生命周期再拆服务 |
| Model Plane 与 Device Plane 路由混淆 | 保持不同领域模型、API 和状态所有者 |
| 协议数量失控 | 首版固定一种控制长连接和一种 artifact 路径 |
| 远程副作用重复执行 | 幂等键、lease、revision、side-effect 分类和人工 reconciliation |
| 设备状态过期导致误调度 | 短租约、时间戳、硬过滤和设备接受阶段复检 |
| Dashboard 变成巨型后端 | 只聚合 API，不直接持有领域状态 |
| 移动平台能力被高估 | 按平台后台限制设计裁剪 runtime 或 Gateway 模式 |
| 密钥和隐私扩散 | 身份分层、最小权限、数据分类、短期凭据和日志脱敏 |
| 大数据拖垮控制协议 | 内容寻址 artifact、独立数据面和明确大小上限 |
| 长任务恢复仍依赖模型记忆 | 先完成 P0 checkpoint，再进入远程编排 |

## 17. 架构决策摘要

1. 保留 `testcode` 和 API Control Board 两个现有项目及独立进程边界。
2. 将 API Control Board 定位为 Model Plane，不扩展为设备控制端。
3. 将未来设备能力作为 `CapabilityWarehouse` 的新 source 接入。
4. 设备能力激活后适配为普通 Tool，复用统一安全与日志路径。
5. Device Control Protocol 与 MCP 并存，各自承担明确语义。
6. Project Composer 首版只解析 Profile，不生成重复项目。
7. Dashboard 可以统一体验，但不能统一状态所有权。
8. 先完成 Agent checkpoint 和模型网关契约，再做设备模拟器和 Linux/GPU 纵向闭环。
9. 不按设备类型复制完整 runtime；通用 runtime + 平台 adapter + capability plugin。
10. 不以最终目录结构驱动设计，以稳定契约、真实部署和验收结果驱动拆分。
