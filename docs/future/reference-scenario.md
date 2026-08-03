# 参考场景：异地持续家庭助手

## 文档职责

本文用一个贯穿案例验证未来平台的 Plane、Layer、运行域和安全边界。它不定义最终 API 字段；涉及
的稳定语义分别由[运行时领域](runtime-model.md)、[协议边界](protocol-boundaries.md)和
[安全与授权](security-and-authorization.md)维护。

本文是**非规范性示例**。猫、食盆、摄像头、投喂机、空调、新闻、手机和 Coding Agent 都不是
框架内建类型，也不要求实现同名 Core、枚举、表或 API。换成工业检测、实验室仪器、车辆、办公
自动化或其他未来能力时，框架仍使用相同的发现、组合、策略、执行和恢复机制。

## 示例的动态装配边界

场景成立的前提不是 Planner 写有一条“找猫”分支，而是运行时动态取得：

```text
Capability Manifest
  观察、转动、执行、读取状态、模型推理、事件订阅、远程委托

Domain Plugin / Schema
  当前项目关心的实体、关系、Observation 和动作约束

Project Profile / Policy
  数据边界、允许主体、参数范围、通知偏好和认证强度

Runtime State
  当前在线实现、资源、位置、有效观察、会话和后台工作流
```

AI 根据这些动态信息生成 Workflow，并可以在实现离线、性能不足或策略变化时选择另一个兼容实现。
运行时负责校验生成结果是否引用已发现的能力、是否满足 schema/Policy、是否可恢复；AI 不能创造
一个不存在的设备能力，也不能因为自然语言相似就绕过 Adapter。

在本示例中出现的名称只用于方便阅读：

- “看猫”是观察能力、可控视角能力、感知模型和 World State 的一次组合。
- “投喂”是一个带现实副作用、参数硬上限和验证信号的动作能力。
- “空调”是一个可配置、可回读状态的执行能力。
- “新闻”是一个不可信外部内容源产生的主动 Event。
- “Coding Agent”是一个通过 A2A 暴露动态技能的远程 Agent。

不同项目可以替换其中任意能力、实体 schema、模型、设备、通知源或授权方式，不修改通用
Orchestration Runtime。

## 1. 场景

用户在回家路上通过手机与主 Assistant 语音交流：

1. 用户说“看看猫猫”，AI 在家庭域选择并转动摄像头寻找猫，返回观察结果。
2. AI 发现食盆看起来接近空，询问是否投喂；用户同意后投喂一次并验证设备反馈。
3. 用户要求开空调，系统按已配置的卧室舒适预设执行并报告实际状态。
4. 新闻 Agent 发现订阅热点，系统在自然间隙询问是否听简要版，用户继续讨论。
5. 同时，异地 Coding Agent 等待高风险权限；授权请求被路由到手机，用户理解并完成本地强认证，
   Coding Workflow 继续，而主新闻对话不中断或丢失上下文。

模型可以来自家庭电脑、异地私有服务器或允许的付费云端，但持续状态不属于任何具体模型。

## 2. 参与运行域

| 运行域 | 在场景中的职责 |
| --- | --- |
| Interaction Runtime | 手机语音、当前话题、通知与用户反馈 |
| Orchestration Runtime | Planner、Workflow、Event Inbox 和后台并发 |
| Trust Runtime | Policy、强认证路由和授权回执 |
| Environment Runtime（家庭域实例） | World State、摄像头感知、投喂机和空调 |
| Model Plane | 为对话、视觉、摘要和 Coding 路由允许的模型 |
| Device Plane | 设备身份、在线状态、执行、租约和结果 |
| Foundation | Capability、Artifact、A2A、状态恢复和 trace |

这些是逻辑职责，不要求每项都是独立服务。

## 3. 主会话建立

```text
L6 手机体验
  → L5 加载个人/家庭 Profile
  → L3 恢复 Conversation、当前话题和活跃终端
  → L2 形成有界 Conversation Input
  → L1/L0 选择当前允许的语音与对话模型
```

模型切换只替换推理执行，不改变 Conversation、Workflow 或授权状态。

## 4. “看看猫猫”

```text
L6  接收语音并显示进行中
  → L5 读取摄像头、媒体出域和 PTZ 预授权
  → L4 Planner 拆成定位、选择摄像头、观察、搜索和总结
  → L3 创建 cat-observation Workflow
  → L3 World State + Device 调度选择摄像头
  → L2 创建有界 Perception Session/Execution
  → L1 Camera/Vision Adapter
  → L0 摄像头和家庭/私有视觉模型
  → L2 返回带来源、时间、证据和置信度的 Observation
  → L3 更新短期 World State
  → L6 返回口语总结
```

媒体是否离开家庭域在模型路由之前决定。摄像头找不到猫、视频中断或状态过期时返回不确定结果，
不能伪造“已看到”。

## 5. 投喂建议与执行

食盆接近空只是 Observation，不是“猫需要投喂”的事实：

```text
Planner 根据 Observation 提出建议
  → Policy 判定为普通确认
  → 用户说“喂一点”
  → 校验剂量、日累计、最近投喂、并发和设备状态
  → 创建有幂等和期限的 Device Execution
  → Feeder Adapter 执行
  → 读取重量/马达/料仓反馈
  → 报告已验证结果或说明尚未验证
```

本次口头同意只授权本次有界动作，不能复用于下一次投喂。

## 6. 空调执行

```text
用户明确要求开空调
  → Profile 将目标解析为无歧义的卧室舒适预设
  → Policy 确认参数在预授权安全范围
  → 创建 climate execution
  → Adapter 转为厂商命令
  → 回读房间、模式、温度和在线状态
  → “卧室空调已开启，25℃，制冷模式”
```

若没有默认房间或存在歧义，系统询问一次；得到授权后是否保存为长期偏好由用户决定。

若用户选择保存，“默认卧室舒适预设”作为有作用范围和版本的明确 Preference 写入 Profile；它不保存
空调实时状态，也不扩大可执行参数或数据域。以后调用时仍使用当前 Profile/Policy 和设备状态复检。

## 7. 新闻主动事件

```text
邮箱/Feed/News Agent
  → 不可信内容提取与来源记录
  → 相关性和摘要模型（无设备权限）
  → news Event
  → Event Inbox 去重、限流、校验 Subscription
  → Attention 等待当前话语自然结束
  → Conversation 只接收有界提示
  → 用户选择是否进入新闻话题分支
```

新闻内容不能通过提示注入调用家庭设备或 Coding 工具。

## 8. 异地 Coding 授权

```text
Remote Coding Agent 的 A2A Task 进入 TASK_STATE_AUTH_REQUIRED
  → 本地 Workflow 保存远程状态投影和 checkpoint
  → Authorization 验证请求方，形成有界 Challenge
  → Event/Presence 路由到满足 assurance 的手机
  → Attention 在安全交互时机解释请求
  → 用户理解后触发手机本地认证器
  → 认证器签发一次性、受众绑定 Receipt
  → Receipt 经独立安全通道交给原始 Coding Agent
  → Coding Agent 验证和消费后继续
```

人脸或指纹只解锁手机本地密钥，不进入模型或 A2A。主交互模型负责解释，不持有认证器，也不签发
回执。用户正在驾驶或终端不满足认证强度时，Coding Task 保持等待。

## 9. 代表性工作与经验固化

若 Coding Workflow 经历多轮重复失败后找到了关键根因，并通过测试稳定复现修复：

```text
远程 Agent 返回结果、失败路径和验证 Artifact
  → 本地 Workflow 完成并记录验证结论
  → Knowledge Runtime 提取 Experience Candidate
  → 绑定项目、版本、症状、根因、有效方法、失败尝试和证据
  → 去重、冲突检查和本地 Policy 检查
  → 按 Profile 自动接受低风险且证据充分的经验，或请求用户确认
  → 发布为可检索 Experience
```

远程 Agent 不能直接写入本地经验库。以后相似任务只取得符合项目、版本、信任域和上下文预算的
经验投影；主模型可以参考，但仍需验证当前环境。若该方法需要成为可执行流程，则另行经过测试、
版本和能力仓库准入后晋升为 Skill。

## 10. 并发车道

```text
Conversation  语音 ─ 猫结果 ─ 新闻讨论 ─ 授权提示 ─ 继续新闻
Home          猫观察 ─ 投喂 ─ 空调 ─ completed
News          collect ─ summarize ─ inbox ─ delivered ─ topic branch
Coding        running ─ auth-required ─ waiting ─ verified ─ running
Authorization challenge ─ phone ─ user verification ─ receipt ─ consumed
```

- Conversation 只持有用户可见摘要。
- 每个 Workflow 有独立 checkpoint 和 context budget。
- Event 支持去重、确认和过期。
- Approval 单独关联挑战、回执、消费和审计。
- trace 可以串联全程，但不授予跨车道读取权限。

## 11. 验收不变量

- L6 不直接操作设备；动作经过 L2 契约与 L3 policy/orchestration。
- 模型可以建议和规划，不能扩大授权或覆盖数据边界。
- 家庭原始媒体默认不离开家庭域，出域必须可见、受限和可审计。
- Observation 无论置信度多高都不自动获得物理执行权。
- 投喂、空调和 PTZ 区分命令接受、设备执行和效果验证。
- 新闻事件不抢占用户发言，也不能调用其他 Workflow 的能力。
- Coding 授权只对原始动作短期有效，拒绝和过期不影响主会话。
- 模型、设备、Relay 或网络切换后，上层 ID、checkpoint 和授权语义保持稳定。
- 远程结果只能形成带来源的经验候选，不能直接改写 Experience 或 Profile。
- 被检索经验不授予能力或权限，环境或版本不匹配时必须降级为排查线索。
