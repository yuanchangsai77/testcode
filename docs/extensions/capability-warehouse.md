# 扩展：能力仓库与渐进式披露

## 文档职责

本文档定义 `testcode` 的统一能力仓库，以及工具、MCP、Skill、插件等能力如何按需进入模型工作台。

它重点回答：

- 哪些能力应常驻工作台
- 单体工具与工具箱如何统一建模
- MCP 和 Skill 为什么都是工具箱
- 仓库目录、工具箱目录、完整 schema/指令如何逐层暴露
- 激活、使用、回收和失败状态如何管理
- “provider 直接注册全部工具”的旧实现如何迁移

相关文档：

- 总体 runtime 分层见 `docs/architecture.md`
- 通用扩展边界见 `docs/extensions/runtime-interfaces.md`
- MCP 协议与生命周期见 `docs/extensions/mcp-integration.md`
- Skill 内容结构见 `docs/extensions/skill-system.md`
- tool 字段契约见 `docs/reference/tool-contract.md`

本文档是能力可见性和激活策略的主设计。其他专项文档若仍描述“启动或每次请求时注册全部外部工具”，应以本文档为准逐步修订。

## 当前实现状态

截至 2026-07-31，第一版运行链路已经落地：

- 核心工具与仓库操作常驻工作台，MCP、Skill 和本地 Subagent 工具箱叶子能力默认不注册。
- MCP 配置只形成带用途描述的外层工具箱条目；模型自行判断并打开所需工具箱，打开时才按需 discovery。
- 打开只返回有界 manifest；选中的叶子工具在激活后的下一模型回合才进入完整 tool schema。
- Skill metadata 只进入外层目录，正文只在激活 `instructions` 叶子后进入 prompt。
- 与 Skill 工作流强关联的本地工具作为同箱叶子按需激活，不再常驻核心工作台。
- activation set 已支持数量和字符预算、名称冲突预检、turn/run/session 范围、显式释放和 session 隔离。
- 状态查询区分生命周期、健康度、目录来源和连接状态，并记录使用与释放结果。
- 交互补全可列出已存在的外层工具箱和已激活能力；读取候选不触发远程 discovery、
  改变 toolbox 生命周期或激活。
- 用户通过 `/capabilities activate` 明确选择 toolbox 后，命令层可在激活前打开它，
  并将 manifest 中的叶子能力作为一组激活；
  模型工具仍遵守先 open、后 activate 的渐进披露契约。

仍属于后续增强而非本版已实现能力：

- Skill references、assets、scripts 的逐项 manifest 与独立激活。
- MCP resources 和 prompts 的统一叶子激活路径。
- Plugin 与非核心单体工具的仓库 source。
- 基于 TTL/LRU 的自动回收、用户固定和健康度排序。
- 大规模目录的持久索引或语义检索。

## 1. 背景与问题

能力仓库引入前，启用的 MCP server 曾被当成普通 `ToolProvider`：每次执行前发现 server 内的全部工具，将它们注册到 `ToolRegistry`，再把所有工具名称、描述和参数 schema 发送给模型。当前主链路已经迁移为本文件开头所述的工具箱目录、manifest 和显式激活模型。

这种旧方式在只有少量工具时可工作，但无法支撑真实规模；以下均描述迁移前的问题：

- 配置 MCP 等于立即激活其全部工具。
- MCP server 越多，模型请求中的 function schema 越大。
- 不相关工具会干扰模型选择。
- 闲置 server 的连接、发现和错误会影响当前请求。
- Skill、MCP、插件分别发展出不同的加载机制。
- 模型容易把“仓库里存在能力”误解为“当前工作台已经可以直接使用”。

我们需要把“存在于系统中”和“当前对模型可见”分开。

## 2. 核心比喻

整个能力系统分成三个空间：

```text
能力仓库
├── 单体工具
│   └── weather_lookup
├── MCP 工具箱
│   └── amap
│       ├── 地点搜索
│       ├── 地理编码
│       ├── 驾车路线
│       └── 公交路线
├── Skill 工具箱
│   └── git-helper
│       ├── instructions
│       ├── references
│       ├── scripts
│       └── optional tools
└── Plugin 工具箱
    └── plugin-owned capabilities

当前工作台
├── 少量核心工具
├── 仓库列举/打开/激活入口
└── 本次任务已激活的能力
```

含义：

- 仓库保存系统知道的全部能力，但不等于模型可以立即调用。
- 工具箱是有目录、有状态、有生命周期的一组能力。
- 工作台只放常用基础工具和当前任务真正需要的能力。
- 模型通过阅读描述、打开、激活逐层取得能力，而不是由关键词匹配替它决定。

## 3. 设计原则

### 3.1 存在不等于激活

配置、安装或发现某个能力，只说明它在仓库中存在。只有进入当前 activation set 后，完整 schema 或指令才可进入模型请求。

### 3.2 渐进式披露

每一层只暴露下一步决策所需的信息：

1. 仓库层：名称、类型、短描述、能力标签。
2. 工具箱层：箱内条目名称和短描述。
3. 激活层：被选中工具的完整 schema，或被选中 Skill 内容。
4. 执行层：结果、错误和必要的后续状态。

### 3.3 默认不连接闲置外部服务

配置中的 MCP server 默认只形成仓库条目。未被打开或激活时：

- 不要求建立连接
- 不执行远端 discovery
- 不把失败写进当前任务提示
- 不影响其他任务

允许使用有 TTL 的离线目录缓存，但缓存存在不意味着远端当前在线。

### 3.4 激活范围必须有界

每次只激活完成当前步骤所需的最小能力集合。激活 toolbox/单体能力组数、叶子 schema 字符数、
Skill 指令字符数都必须受预算约束。同一 toolbox 内的多个叶子共享一个数量单位，
但它们的完整 schema/指令字符仍全部进入字符预算。

批量激活是原子操作：预算、manifest 归属、注册表冲突和批次内部重名必须在提交前统一校验。任一叶子能力失败时，不得注册部分新工具，也不得提前修改已有能力的 scope、reason 或持久化状态。

### 3.5 激活不等于授权执行

激活只决定模型是否能看到和请求某项能力。真正执行仍必须经过参数校验、policy、approval、logger 和结果裁剪。

## 4. 统一领域模型

### 4.1 仓库条目

仓库条目是最外层可检索对象，最小字段包括：

- 稳定 id
- 显示名称
- 类型：单体工具或工具箱
- 来源：builtin、MCP、Skill、plugin、user
- 一句话描述
- 能力标签
- 大致风险、成本和延迟提示
- 配置状态
- 目录缓存状态

仓库条目不包含完整 tool schema、Skill 正文、远端资源正文或敏感配置。

### 4.2 单体工具

单体工具没有必须展开的内层目录，例如一个独立天气查询工具。它仍先作为仓库条目存在，选中后才激活完整 schema。

少数高频、低风险且低上下文成本的单体工具可标记为核心工具，常驻工作台。当前
`git_status`、`git_diff` 属于核心观察能力；更低频的历史查询、测试执行以及会改变仓库状态的
Git 操作适合按需激活。隐藏 schema 只控制上下文成本，不构成安全边界。

### 4.3 工具箱

工具箱是能力容器。不同来源共享同一外层语义，但箱内资产可以不同：

| 工具箱类型 | 箱内资产 |
| --- | --- |
| MCP | tools、resources、prompts 及 server 状态 |
| 本地工具箱 | 零到多个 workflow instructions、tools，以及未来的 references/assets/scripts |
| Plugin | plugin 提供的 tools、skills、resources 或 commands |
| 自定义工具包 | 一组有关联的本地或远端工具 |

工具箱外层只提供用途概述和能力标签；打开后才返回受限的 manifest。

### 4.4 激活集

激活集是当前 run/session 对模型可见的非核心能力集合，应记录：

- 激活条目 id
- 来源工具箱
- 激活原因或匹配依据
- 激活范围：turn、run、session
- 激活时间和最后使用时间
- schema/指令预算占用
- 健康状态
- 是否被用户固定

`ToolRegistry` 应只承载核心工具与激活集，而不是整个仓库。

## 5. 可见性层级

### L0：核心工作台

模型一开始只看到少量通用能力：

- shell/bash
- read
- list/find/search
- edit/write/patch
- ask/confirm
- warehouse list
- toolbox open/inspect
- capability activate/deactivate/status

实际名称可以沿用现有 builtin tool，但应控制数量和 schema 总量。

### L1：仓库目录

当现有知识和工具不足，或任务明显需要专业能力时，模型先调用仓库列举入口，再阅读每个工具箱的用途描述，自行判断相关候选：

```text
- amap (MCP toolbox): 地点搜索、地理编码、路线规划
- local-map-data (toolbox): 离线地图数据读取
```

此时不暴露 `amap` 内 15 个工具的完整 schema。

### L2：工具箱 manifest

打开 `amap` 后返回箱内的简短目录：

```text
- maps_text_search: 根据关键词查询地点
- maps_geo: 地址转坐标
- maps_direction_driving: 驾车路线规划
- maps_direction_transit_integrated: 公交路线规划
```

manifest 应包含选择所需的参数概要、风险和健康状态，但仍不必发送完整 JSON Schema。

### L3：完整激活内容

确定需要“地点搜索 + 驾车路线”后，仅激活这两个工具。下一轮模型请求才携带它们的完整 function schema。

对于 Skill，此层对应被选中的指令正文、引用片段或可执行脚本入口，而不是整个 Skill 目录。

### L4：执行结果与后续状态

执行结果进入 session history 时继续遵循结果裁剪和 artifact 规则。工具箱其余内容不会因为一次调用而自动展开。

## 6. 生命周期与状态机

统一生命周期：

```text
stored
  → opened
  → activated
  → used
  → released
```

异常分支：

```text
stored/opened/activated
  → unavailable
  → retryable 或 blocked
```

状态含义：

- `stored`：仓库中存在，未加载箱内目录。
- `opened`：manifest 已取得，可能来自缓存或实时 discovery。
- `activated`：完整 schema/指令已进入当前激活集。
- `used`：本轮确实调用或引用过。
- `released`：已从模型可见集合移除。
- `unavailable`：打开、激活或执行阶段失败。

不能用一个 `ready` 同时表示“配置存在”“目录缓存存在”“远端在线”“工具可执行”。这些是不同层次的状态。

运行时状态因此拆成四条独立轴：

| 状态轴 | 典型值 | 回答的问题 |
| --- | --- | --- |
| 生命周期 | stored、opened、activated、used、released | 能力走到了哪一步 |
| 健康度 | unknown、ready、degraded、unavailable、disabled | 当前已知是否可用 |
| 目录来源 | configuration、local、cache、live、stale | manifest 信息来自哪里 |
| 连接状态 | not_connected、connected、disconnected、failed、not_applicable | 是否实际建立过外部连接 |

未打开的 MCP 健康度必须是 `unknown`、连接状态必须是 `not_connected`；不能仅凭配置存在推断在线，也不能仅凭缓存存在推断已连接。

## 7. 仓库操作面

工作台应常驻一组稳定且很小的仓库操作：

### 7.1 目录列举

返回带非空用途描述的有界目录。目录较大时使用分页继续列举；运行时不通过关键词匹配替模型选择，也不连接所有 MCP。

### 7.2 打开工具箱

读取指定工具箱 manifest。对于 MCP：

1. 优先读取未过期缓存。
2. 需要实时信息时再连接 server。
3. discovery 失败只影响该工具箱。
4. 返回脱敏、分层的状态。

### 7.3 激活

将选中的叶子能力放进当前 activation set。激活必须检查：

- 是否超过工具数量预算
- 是否超过 schema/指令字符预算
- 名称是否与核心工具、已激活工具或同批次其他叶子能力冲突
- 能力是否可用
- 风险和审批提示是否完整

上述检查通过后才能一次性提交激活记录和工具注册。失败批次保持原 activation set 不变，避免出现“记录显示已激活，但工具并未注册”或失败请求意外把 run scope 提升为 session scope。

### 7.4 释放

任务阶段结束、TTL 到期、切换 session 或预算不足时释放闲置能力。用户显式固定的能力可以保留到 session 结束。

### 7.5 状态查询

状态通过按需查询获得，不应把整个仓库所有 server 的详细状态永久写入系统提示。

## 8. MCP 工具箱语义

一个 MCP server 对应一个工具箱，而不是一批启动即注册的工具。

### 配置阶段

只创建外层条目：

- server name
- transport
- 脱敏 target
- 简短描述和能力标签
- enabled/configured 状态

不要求 initialize 或 `tools/list`。

### 打开阶段

按需执行 initialize、`tools/list` 和可选的 `resources/list`，形成 manifest。目录缓存记录：

- 来源是 live 还是 cache
- 刷新时间和年龄
- tool/resource 数量
- discovery 错误及底层原因

### 激活阶段

从 manifest 中选择最小工具集合，完成名称适配、schema 校验、risk mapping 和冲突检测，再注册到当前 activation set。

### 执行阶段

通过现有 manager/client/transport 调用远端工具，继续复用 policy、approval、logger、结果裁剪和连接失效策略。

### 资源与 prompts

MCP resources 和 prompts 仍是箱内资产，但不应自动转化为 function tools。它们通过各自的选择与预算路径进入上下文。

## 9. Skill 工具箱语义

Skill 不再是特殊运行时容器。它负责从磁盘发现、版本化和覆盖工作流内容，再被适配为普通
`LocalToolboxSpec`。Subagent 等内置能力使用同一种本地工具箱结构，只是可以不带说明书。

本地工具箱统一允许：

- 零到多个 workflow instruction 叶子；
- 零到多个本地工具叶子；
- 独立的目录描述、标签和可用性策略。

外层条目只包含：

- name、description、version
- triggers/capability tags
- 来源和信任级别
- 箱内资产类型概览

打开 Skill 后才展示：

- instructions 摘要
- references 索引
- assets 索引
- scripts/工具入口及风险

激活时只加载当前步骤需要的内容：

- 必要指令正文
- 被选中的 reference 片段
- 被选中的脚本或工具 schema

Skill script 的激活不代表允许执行；执行仍需走 policy 和 approval。

## 10. Prompt 与模型请求契约

模型请求应保持稳定前缀：

1. 核心行为规则
2. 当前任务所需的项目/用户上下文
3. 少量核心工具 schema
4. 当前 activation set 的 schema/指令

不应默认包含：

- 全部 MCP 工具 schema
- 全部 Skill 正文
- 全部工具箱详细状态
- 与任务无关的仓库目录
- 为外部查询准备的项目目录树

分页列举结果和工具箱 manifest 应作为短期、可裁剪的工具结果进入会话，而不是永久追加到系统提示。

## 11. 选择与激活策略

规则：

- 用户明确指定某个工具箱时优先打开该箱。
- 用户只描述目标且现有能力不足时，由模型主动进入仓库、阅读目录描述并选择候选。
- 不因为仓库中存在大量工具就全部激活。
- 一次默认激活 1～3 个叶子能力；确有依赖时再增量激活。
- 新增激活必须能解释“为什么当前步骤需要它”。

## 12. 激活范围与回收

支持三种范围：

- turn：只服务下一次模型决策。
- run：服务当前用户请求，结束后释放。
- session：跨多轮保留，适合连续使用同一工具箱。

CLI 进入或切换会话时必须先以 session id 切换运行时能力状态：清理上一会话的激活集，再恢复
目标会话持久化的 session 能力。交互命令预激活的 turn/run 能力属于目标会话的待执行状态，
下一次执行不得因重复初始化而提前回收；切换到其他会话时则必须清理，避免跨会话泄漏。

默认建议：

- 独立查询使用 run scope。
- 连续地图规划可使用 session scope。
- 高风险或大 schema 工具优先 turn/run scope。
- 长时间未使用的能力按 TTL 或 LRU 释放。

回收只影响模型可见性，不删除仓库索引和目录缓存。

## 13. 安全与可观察性

仓库相关事件建议包括：

- warehouse list
- toolbox opened/open failed
- capability activated/released
- activation rejected by budget/conflict/policy
- capability used

日志应能回答：

- 为什么选择了这个工具箱
- manifest 来自缓存还是实时 discovery
- 为什么激活这些工具
- 激活占用了多少预算
- 何时、为什么释放
- 失败发生在配置、打开、激活还是执行阶段

敏感 URL userinfo/query、headers、env 和 Skill secrets 在仓库、manifest、状态和日志各层都必须脱敏。用于目录或状态展示的 URL target 只保留 scheme、host、port 和 path，不得保留用户名、密码、query 或 fragment。

## 14. 失败语义

### 显式请求

用户明确要求使用某个 MCP/Skill，而对应工具箱无法打开或没有可用叶子能力时，应直接返回该工具箱状态，不得搜索项目源码、配置或环境变量来猜原因，除非用户要求排障。

### 模型自主选择

用户只描述目标时，模型根据用途描述自行选择；如果首选工具箱不可用，可以继续阅读目录中的其他候选，但不得无限重试同一个箱子。

### 缓存

缓存 manifest 可用于选择，但必须标明年龄和来源。真正执行失败时应更新工具箱健康状态，不能继续把缓存描述符等同于远端在线。

## 15. 运行时边界调整

目标边界：

- `CapabilityWarehouse`：保存带用途描述的外层目录和来源信息。
- `ToolboxCatalog`：按需打开工具箱并返回 manifest。
- `ActivationManager`：管理当前 activation set、预算、TTL 和回收。
- `ToolRegistry`：只保存核心工具与已激活工具。
- MCP manager/client/transport：只负责打开或执行阶段的协议与连接。
- Skill registry/loader：改为仓库目录与按需资产读取，不再直接决定完整 prompt 注入。

`ToolProvider` 的职责需要调整：它不再表示“把来源中的所有工具注册进 runtime”，而应服务于叶子能力被选中后的适配和激活。

## 16. 典型流程

用户请求：

```text
帮我看留仙洞到梅塘的驾车路线
```

期望流程：

1. runtime 判断这是外部地图能力请求，不加载项目目录树。
2. 模型判断任务需要专业地图能力，调用仓库列举入口。
3. 仓库返回外层用途描述；模型据此判断 `amap` 工具箱相关。
4. 打开 `amap`，获取受限 manifest。
5. 选择并激活地点搜索/地理编码与驾车路线工具。
6. 下一轮模型只看到核心工具和这 2～3 个高德工具。
7. 调用工具并返回路线。
8. run 结束后释放高德工具；保留仓库条目和目录缓存。

禁止流程：

- 启动时连接所有 MCP。
- 将高德全部工具永久放入模型 schema。
- 因高德失败而搜索当前项目源码。
- 为路线查询注入 Git 状态、测试命令和目录树。

## 17. 当前实现边界

当前主链路已经完成仓库与注册表分离，并支持 MCP 工具箱、Skill instructions、本地
Subagent 工具箱、目录列举、manifest 打开、显式激活、释放、状态查询和
turn/run/session 范围回收。`ToolRegistry` 只承载核心工具与当前激活集。Subagent 工具箱只在
前台父会话目录中出现，后台子会话不提供该目录，从源头避免递归委派。

尚未完成的设计边界包括 Skill `references/`、`assets/`、`scripts/` 的独立索引和生命周期，以及 TTL/LRU 和基于健康度、风险、成本的候选排序。这些能力仍应遵守本文定义的渐进披露、预算、安全与原子激活契约。

具体优先级和完成状态统一由 [演进路线图](../roadmap.md) 维护，本文不再保留阶段式实施清单。

## 18. 验收标准

### 上下文与工具可见性

- 配置 10 个 MCP server 时，初始模型请求既不包含工具箱目录，也不包含它们的工具 schema。
- 未打开的 MCP 不产生连接。
- 打开高德只返回 manifest，不立即激活全部工具。
- 路线请求最多激活完成任务所需的少量地图工具。
- Skill 未激活时，其正文和 references 不进入 prompt。

### 生命周期

- 激活工具能在下一轮模型请求中出现。
- 释放后不再出现在模型工具列表。
- 不同 session 的 activation set 相互隔离。
- 缓存 manifest 与远端在线状态明确区分。

### 安全

- 激活不绕过执行审批。
- 未激活工具不能被模型直接调用。
- 名称冲突（包括同批次重名）、预算超限和非法 schema 会原子拒绝整个激活批次并提供诊断。
- 仓库、manifest、状态和日志均不泄露凭证。

### 失败与降级

- 显式请求不可用工具箱时快速失败，不进入项目排查。
- 一个工具箱故障不影响其他工具箱和核心工作台。
- 非代码外部查询不注入 workspace tree、Git 和测试信号；`code`、`test`、`project` 等歧义词单独出现时不得作为项目请求的充分条件。

## 19. 非目标

本设计暂不规定：

- 大规模目录未来是否需要额外的索引或语义检索
- UI 中工具箱的最终视觉样式
- MCP 协议本身的 transport 细节
- Skill 文件格式的全部字段
- 是否允许模型永久安装新能力

这些可以在仓库、manifest、activation 三层边界稳定后分别演进。

## 20. 结论

`testcode` 不应把全部能力直接当成当前工具。正确模型是：

```text
能力存在于仓库
→ 相关工具箱被找到
→ 箱内目录按需展开
→ 少量叶子能力进入激活集
→ 模型使用
→ 任务结束后释放
```

MCP、Skill、插件和单体工具都应服从这套渐进式披露模型。核心工作台保持小而稳定，仓库可以持续扩张，而不会让模型上下文、连接生命周期和工具选择复杂度随能力总量线性增长。
