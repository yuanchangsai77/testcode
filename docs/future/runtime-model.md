# 未来平台运行时领域

## 文档职责

本文定义跨实现必须理解的运行时概念、生命周期和所有权，不固定最终类名、序列化字段或数据库
结构。网络边界与最小消息语义见[协议边界](protocol-boundaries.md)。

## 1. 任务概念分层

“任务”不能同时代表用户目标、设备租约和远程 Agent 状态。建议使用以下语义层次；最终类型名可
调整，但所有权必须保留：

```text
Conversation
└── Workflow                 用户目标的可恢复生命周期
    ├── Step                 计划依赖节点
    └── ExecutionAttempt     一次实际执行尝试
        ├── LocalToolExecution
        ├── ModelRequest
        ├── DeviceExecution
        └── RemoteAgentTaskRef
```

- `Conversation`：持续用户交互和话题关系，由 Agent Plane 拥有。
- `Workflow`：一个可暂停、恢复、取消和验证的目标，由 Agent Plane 拥有。
- `Step`：能力需求与依赖，不绑定具体设备或 Provider。
- `ExecutionAttempt`：一次带超时、幂等和结果的执行选择。
- `DeviceExecution`：由 Device Plane 调度和维护租约。
- `RemoteAgentTaskRef`：远程 A2A Task 的本地引用；权威状态仍在远程 Agent。
- `ModelRequest`：一次推理请求，不升级成通用 Workflow 状态所有者。

状态名不是蓝图不变量。实现必须保证的是：终态不可被迟到旧事件覆盖，等待授权与等待资源可以
区分，取消和超时有明确语义，重试产生新的 attempt 而不是篡改历史。

## 2. Capability 与执行

Capability 描述“系统能做什么”，设备或 Provider 描述“谁能做”。最小稳定语义包括：

- 稳定能力标识和兼容版本。
- 发行者身份、内容摘要、准入状态和撤销信息；稳定标识必须位于可追溯的命名域。
- 输入与输出的结构约束。
- 操作风险、数据要求、副作用和是否支持流式。
- 必要资源和实现限制。
- 可观测的成功、失败和取消语义。

Capability 进入模型可见范围前仍经过能力仓库 manifest 和 activation；激活不等于授权。设备能力
激活后适配成普通 Tool，继续经过统一预检、policy、approval、execution 和 logging。

动态资源、健康和在线指标由实现声明，属于运行状态，不写进静态 Capability 描述，也不要求所有
平台提供同一组指标。

### 动态实现绑定

框架区分“能力语义”和“能力实现”：

```text
Capability Manifest
  说明需要完成什么、输入输出约束和效果语义

Implementation Manifest
  说明某个 Plugin、Agent、模型或设备如何实现、适用条件和动态状态

Binding Decision
  在当前 Workflow、Policy、成本、性能和可用性下选择一个兼容实现
```

AI 可以提出 Binding Decision、选择已激活实现或在失败后建议替代实现；运行时负责验证兼容版本、
数据边界、权限、预算和状态。新 Plugin 加入后通过 manifest 自动成为候选，不需要在 Planner 中新增
场景分支。

实现切换发生在明确的执行边界。无状态或幂等步骤可以重新绑定；有状态流、外部副作用和长事务
必须先保存 checkpoint、确认旧执行状态并满足迁移/补偿规则，不能为了性能直接热切换。

`[不变量]` 绑定和节点调度是两级决策：Agent Plane 选择兼容的实现类别和约束；若该实现由设备
提供，Device Plane 再在满足约束的节点中选择具体执行者。Agent Plane 不能绕过 Device Plane 的
lease 和本地复检指定节点，Device Plane 也不能把任务改绑到语义、数据域或授权不兼容的实现。

## 3. Conversation 与持续模型切换

Conversation 持有：

- 用户与活跃终端引用。
- 当前话题和话题分支。
- 与 Workflow 的可见关系。
- 有界 hot context、warm summary 和 cold archive 引用。
- pending event 摘要。

具体模型只看到当前回合需要的视图。在本地、私有或外部模型实现之间切换时，
Conversation ID、Workflow 状态和授权引用保持稳定。模型不能直接成为会话数据库。

## 4. Workflow 与并行执行

每个用户目标、后台作业、主动信息处理或远程委托都可以形成独立 Workflow。Workflow Supervisor
负责：

- 计划依赖、状态和 checkpoint。
- 每个 Workflow 独立的上下文预算。
- 远程执行引用和最近验证结果。
- 暂停、恢复、取消和失败处理。
- 向 Conversation 投递最小用户摘要。

主对话不加载无关后台 Workflow 的完整工具历史，不同 Workflow 也不能互相读取无关原始内容。
`trace_id` 可以关联审计，但不授予跨 Workflow 读取权限。

## 5. Event、Subscription 与 Attention

Event 是后台事实或状态变化，不是直接 UI 指令。来源可以是设备、订阅源、远程 Agent、业务系统
或安全系统，框架不预定义固定事件类型。

最小语义包括来源、类型、产生时间、优先级、有效期、去重标识和有界摘要/正文引用。字段名称和
编码可以调整。

`[当前推荐]` Event delivery 采用至少一次交付：

- Event Inbox 负责幂等、去重和重放。
- 同一来源需要可判断顺序，跨来源不承诺全局排序。
- 过期事件不再打扰用户，但保留必要审计。
- 事件来源不能直接抢占语音或调用设备。

Subscription 表达允许的主题、频率、时段和模态。Attention Manager 根据当前交互状态、环境
约束、免打扰策略、优先级和终端能力，决定立即提示、自然间隙提示、合并、静默或丢弃。

## 6. Project Profile、Policy Source 与 Policy View

Profile 声明能力组合、偏好和期望信任域，不保存实时设备状态、真实密钥，也不拥有组织安全规则或
设备硬限制。系统、组织、项目和设备本地 Policy 分别由各自来源维护；Trust Runtime 只组合经过验证
的只读投影。避免使用“一切有副作用的动作都确认”之类全局布尔值，推荐按能力元数据、触发来源和
参数范围表达意图：

```text
read-only capability       在允许的数据域内可自动执行
bounded reversible action  显式请求且参数在预设内时执行后通知
suggested side effect       AI 主动建议时要求确认并应用硬上限
sensitive remote action    需要强认证并绑定具体任务与动作
```

这只是语义示例，不固定配置格式。Trust Runtime 根据 Profile 与各 Policy Source 的版本化投影形成
只读 Policy View；Policy Decision 必须记录所有输入版本和关键依据。Profile 可以表达用户期望，
但不能覆盖组织规则、系统硬限制或设备本地拒绝。

Profile 不枚举平台支持的全部能力。Plugin 或远端 source 动态提供 Capability Manifest，AI 根据
Manifest、当前目标和 Policy View 组合 Workflow；未知能力默认不可见或需要显式激活。

偏好与 Experience 的持久化、候选确认、冲突和检索生命周期由
[经验与偏好](experience-and-preference.md)定义。Conversation 摘要和 Workflow archive 不因可被检索
而自动升级为长期偏好或经验。

## 7. 设备发现与调度

设备选择采用“硬过滤后评分”：

1. 过滤离线、租约过期或版本不兼容实现。
2. 匹配能力和必要资源。
3. 应用数据边界、信任域、用户授权和设备本地策略。
4. 检查实现动态声明的资源、健康、并发和 deadline 等现实限制。
5. 在剩余候选中按本地性、延迟、负载、成本和能耗评分。
6. 创建设备执行 lease，由设备再次复检后接受或拒绝。

评分不能覆盖硬策略。模型可以提出偏好，但不直接选定违反策略的节点。

## 8. 重试、幂等与物理效果

- 纯读取和内容寻址计算可以在幂等保护下自动重试。
- 写入需要版本/hash 或其他并发前置条件。
- 通知、播放和震动需要去重窗口。
- 不可逆、物理、财务或外部副作用动作默认不自动转移重试。
- 控制端失去确认时先查询权威状态，不能直接重发。

物理动作的结果至少区分以下语义；最终状态名可调整：

```text
命令被接收
设备开始执行
物理效果被观察
期望状态被验证
```

Capability Plugin 应声明可用的验证信号或补偿方式。只确认命令下发时，用户摘要必须明确“尚未
验证现实效果”。不可逆动作失败后进入 reconciliation，不伪装成自动恢复成功。

### 8.1 竞态、时间与恢复语义

`[不变量]` 每个 Execution 的状态更新带权威 revision；同一 revision 只能对应一个不可变事实。
终态不会被较旧的进度、取消确认或断线重放覆盖。实现必须显式处理以下竞态：

- 取消与完成并发：权威执行方按 revision 确定先后；已经产生的现实效果不能因取消回执被抹除。
- deadline 与迟到结果：deadline 阻止新的执行承诺，但迟到的已发生结果仍进入审计和 reconciliation。
- lease 过期与旧节点恢复：旧节点只能报告历史结果，不能继续取得新副作用权限。
- 控制端超时与确认丢失：先按 execution id 查询权威状态；无法证明未执行时不得盲目重发。
- Profile、Policy、身份或授权变化：排队、恢复、重新绑定和副作用执行前重新判定；旧 Decision
  只作为审计记录。Receipt 的复检与失效条件由
  [安全与授权](security-and-authorization.md#6-委托授权流程)统一定义。

幂等标识必须声明作用域、发行方和最短保留期。保留期至少覆盖最大执行时长、消息重放窗口与断线
恢复窗口；超过窗口后不能假设重复请求仍会被识别。不可逆副作用要求由执行方持久化消费记录。

协议不假设节点时钟严格同步。相对超时由接收方权威时钟计量；跨节点产生时间只用于展示和审计，
若要参与顺序或授权判断，必须同时使用权威 revision、签发方时间约束或可验证的时钟误差上限。

## 9. World State 与 Perception

Device Registry 回答有哪些执行节点；World State 使用可扩展 schema 表达当前项目需要的实体、
关系和环境状态。实体类型与关系由领域 Plugin/Profile 注册，不由框架写死。

World State 中的信息分三类：

- 事实：设备遥测或经过验证的物理结果。
- 观察：带来源、时间、有效期、证据和置信度的感知输出。
- 推断：模型基于事实和观察产生的可撤销解释。

任何 Observation 都只提供证据，不提供执行权限；置信度只影响是否建议、是否继续观察。执行权
来自用户意图和 Policy。

Perception Session 是有界感知工作：选择数据源、应用领域声明的控制范围和时长、选择允许的模型、
产生 Observation，并在结束时释放资源。实现时应记录观察模型/版本、处理链、证据权限与保留策略。

## 10. 数据与 Artifact

大型视频、音频、模型文件和中间结果通过 Artifact 引用流转。稳定语义包括内容身份、媒体类型、
大小与完整性、数据分类、可用位置/访问授权和过期策略。路径、URL 和凭据不是长期稳定标识。

实时数据流与持久 Artifact 是不同生命周期：实时流优先使用短期会话密钥和有界传输；需要审计或
后续处理的内容再显式落为 Artifact。受限原始数据默认不离开其授权域，跨域处理必须形成独立可
审计的数据授权。

## 11. 降级模式

| 故障 | 默认安全降级 |
| --- | --- |
| 某信任域网络中断 | 本地域策略继续；跨域观察和控制不可用 |
| Model Plane 不可用 | 保存 Conversation/Workflow，等待或切换策略允许的模型 |
| 首选模型不可用且替代域被禁止 | 明确不可执行，不扩大数据边界 |
| 满足要求的用户认证器不可用 | 强授权保持等待，不降级为低强度确认 |
| World State 过期 | 重新观察；不能用陈旧状态触发受限动作 |
| 非关键事件源不可用 | 静默降级，不影响主会话 |
| 远程 Agent 失联 | 保留远程引用和 checkpoint，禁止盲目重复副作用 |
| Relay/Rendezvous 不可用 | 本地服务继续，跨域消息等待或过期 |
