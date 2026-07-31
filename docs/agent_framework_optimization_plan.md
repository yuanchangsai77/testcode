# Agent 框架优化方案

## 1. 背景与结论

前次运行暴露了四类问题：

1. 生成代码包含硬编码凭据，且后端专用密钥进入了前端代码。
2. 模型重复执行相同的只读工具，浪费 Turn，并延迟实际修改。
3. Python 项目验证时出现工作目录、虚拟环境或导入路径不正确的问题。
4. 部分能力已经存在，但方案倾向于新增平行实现，容易造成重复建设。

本次优化采用“Prompt 降低发生概率、执行前策略强制阻断、编排层纠正行为、诊断层复用现有能力”的分层方案。

核心结论：

- Prompt 是指导层，不能充当安全边界。
- 内容安全检查应作为独立的执行前拦截器，不应耦合进通用风险策略。
- `patch`、兼容写入工具及未来写入工具应通过统一的内容变更接口接入检查。
- Progress Guard 应由独立策略产生信号，Orchestration Engine 只负责协调。
- 不新增与现有 `run_tests` 重叠的工具；优先抽取项目识别能力并增强现有接口。

---

## 2. 当前实现基线

优化必须建立在当前代码事实上，避免重复建设：

- [`ModelPromptBuilder`](../src/testcode/model/prompt.py) 已集中构造系统 Prompt。
- [`DefaultPolicy`](../src/testcode/safety/policy.py) 已负责运行模式和工具风险决策。
- [`Guardrails`](../src/testcode/safety/guardrails.py) 已提供策略检查入口。
- [`redaction.py`](../src/testcode/safety/redaction.py) 已负责日志和输出脱敏。
- [`ToolRegistry`](../src/testcode/tools/registry.py) 已提供工具注册、参数校验和统一执行入口。
- `patch` 与兼容工具 `apply_change` 都能够修改文件。
- `run_tests` 已支持显式命令、工作目录和超时。
- Workspace Summary 已能识别常见项目标记并给出建议测试命令。
- Engine 已保存动作指纹、重复次数和 `duplicate` 元数据，并在写入后清理旧的重复状态。

因此，本方案以扩展现有边界为主，不建立第二套工具注册、安全策略或测试执行系统。

---

## 3. 设计原则

### 3.1 单一职责

- Prompt Rules 只描述模型行为。
- Risk Policy 只判断工具是否允许执行、是否需要确认。
- Content Safety 只判断拟写入内容是否违反安全规则。
- Redaction 只保证敏感内容不进入日志和用户可见诊断。
- Progress Policy 只判断是否需要纠正执行进度。
- Tool Handler 只处理自身业务动作。

### 3.2 依赖倒置

Engine 和 Tool Registry 依赖稳定接口，不依赖具体的密钥正则、项目类型或单个工具实现。

### 3.3 组合优于条件堆叠

通过拦截器、内容提取器和扫描器组合能力，避免在 Engine、Registry 或 Policy 中持续增加工具名分支。

### 3.4 默认安全、允许演进

- 高置信度凭据默认阻断。
- 检测结果不得携带命中的秘密原文。
- 新增写入工具时必须显式声明其内容提取方式，否则不能自动获得“已通过内容安全检查”的状态。
- 安全规则支持独立扩展、测试和配置，但不得由项目代码任意关闭。

### 3.5 显式优先于魔法

- 用户显式提供的测试命令优先。
- 自动诊断只在结果唯一且可靠时执行。
- 无法可靠选择命令时返回候选项，不猜测执行。

---

## 4. 目标架构

```text
Model response
     │
     ▼
ToolRegistry：参数与工具存在性校验
     │
     ▼
Execution Interceptor Chain
     ├── Risk / approval（现有 Guardrails，由 Engine 调用）
     ├── Content mutation extraction
     └── Secret write guard
              │
              ├── allow ──► Tool handler ──► ToolResult
              └── block ──► 标准安全错误（不包含秘密原文）

Engine
  ├── RequestIntentClassifier
  ├── DuplicateTracker（现有状态）
  └── ProgressPolicy ──► progress_required

ProjectDetector
  ├── Workspace Summary
  └── run_tests 默认命令解析
```

架构中的共享组件均面向接口，具体规则通过注册或构造注入。

---

## 5. 模块设计

### 5.1 Prompt 安全与架构规则

#### 目标

在模型生成代码前明确安全约束，减少被底层拦截和重新生成的概率。

#### 接入方式

第一阶段仍由 `ModelPromptBuilder` 统一组装规则，避免为三条规则过早引入复杂框架。规则增长到多个领域后，再抽取轻量的 `PromptRuleProvider` 接口。

建议规则语义：

1. 不得把真实凭据、私钥、访问令牌或密码硬编码到源码、测试、示例和配置模板中；运行时从环境变量或受保护的秘密存储读取。
2. `.env` 只能用于本地开发，并必须被版本控制忽略；仓库只允许提交不含真实值的 `.env.example`。
3. 后端专用凭据不得进入浏览器可下载的 HTML、CSS、JavaScript、Source Map 或静态资源；前端通过本地后端代理访问相关服务。
4. Python 的可导入包名和模块目录不得包含连字符；发行项目名可以保留连字符，但导入名应规范化为下划线。
5. 安全检查返回阻断结果后，必须改用安全配置方式，不得通过编码、拆分字符串或改名规避检测。

#### 维护要求

- Prompt 测试验证规则存在及语义，不依赖完整字符串快照。
- 安全规则使用稳定标识，例如 `SEC-CREDENTIAL-001`，方便测试和诊断关联。
- 项目级 `AGENTS.md` 可以增加更严格的规则，但不能降低全局安全基线。

---

### 5.2 统一内容安全层

#### 5.2.1 为什么不放入 `DefaultPolicy`

`DefaultPolicy` 当前根据工具风险和运行模式做授权决策。密钥扫描需要理解“将写入什么内容”，两者的输入、生命周期和测试方式不同。

将内容解析塞入 `DefaultPolicy` 会导致：

- 风险授权与内容合规互相耦合。
- 每增加一个写入工具都要修改 Policy。
- 扫描器难以独立测试或复用。
- 容易把“用户已确认写入”误解为“允许写入秘密”。

用户确认不能绕过高置信度的凭据泄露阻断。

#### 5.2.2 核心接口

建议新增以下稳定抽象，名称可在实现阶段按项目风格调整：

```python
@dataclass(frozen=True)
class ContentMutation:
    path: str
    added_text: str
    source: str


class MutationExtractor(Protocol):
    def supports(self, action: ToolAction, definition: ToolDefinition) -> bool: ...
    def extract(self, action: ToolAction) -> list[ContentMutation]: ...


class ContentScanner(Protocol):
    def scan(self, mutation: ContentMutation) -> list[SafetyFinding]: ...


class ExecutionInterceptor(Protocol):
    def before_execute(
        self,
        action: ToolAction,
        definition: ToolDefinition,
        context: ToolContext,
    ) -> ToolResult | None: ...
```

职责划分：

- `MutationExtractor` 将工具参数转换为统一的“新增内容”表示。
- `ContentScanner` 与工具无关，只扫描标准化内容。
- `SecretWriteGuard` 组合多个 extractor 和 scanner，并实现执行前拦截接口。
- `ToolRegistry` 仅按顺序运行拦截器；收到阻断结果后不再调用工具 Handler。

这一结构允许未来复用到许可证头检查、危险配置检查和禁止文件类型检查，而无需修改每个工具。

#### 5.2.3 首批内容提取器

`PatchMutationExtractor`

- 解析 unified diff。
- 只收集新增行。
- 排除 `+++` 文件头。
- 不扫描删除行，确保系统能够删除仓库中已有的泄露凭据。
- 解析失败交由 `patch` 原有语法校验处理，不在安全层重复实现完整 patch 校验器。

`FullContentMutationExtractor`

- 服务于兼容工具 `apply_change`。
- 将完整目标内容视为拟写入内容。
- 保留该提取器直至 `apply_change` 完全移除。

Shell 命令可以通过编码、重定向或子进程间接写文件，静态内容提取无法提供完整保证。因此：

- `shell_exec` 继续受执行确认、工作区边界和沙箱保护。
- 可对明显的字面量凭据做补充检查，但不能宣称覆盖任意 Shell 写入。
- 长期应优先让模型使用结构化 `patch`，而不是用 Shell 生成文件。

#### 5.2.4 规则目录与复用

新增共享的 `SecretPatternCatalog`，由安全扫描与日志脱敏共同引用“令牌格式定义”，但保留不同的决策逻辑：

- Redaction：尽可能脱敏，允许更宽松的匹配。
- Blocking Scanner：只阻断高置信度结果，优先降低误报。

首批规则：

- 常见供应商令牌前缀。
- PEM 私钥头。
- Bearer Token。
- 敏感字段赋值且值具备足够长度或复杂度。
- 高置信度 URL 凭据。

不应仅凭“32 位十六进制字符串”直接阻断，因为文件哈希、内容摘要和测试数据很常见。

明确允许的占位形式：

- `${API_KEY}`、`$API_KEY`
- `YOUR_API_KEY`
- `<api-key>`
- 空字符串或显式示例值

占位判断必须发生在阻断决策前，并具有独立测试。

#### 5.2.5 标准错误协议

阻断结果：

```json
{
  "success": false,
  "error_code": "blocked_by_security_policy",
  "output": "Security policy blocked this write because it appears to contain a hardcoded credential. Read the value from a protected runtime source instead.",
  "metadata": {
    "policy_id": "SEC-CREDENTIAL-001",
    "finding_count": 1,
    "locations": [
      {
        "path": "src/example.py",
        "line": 12,
        "category": "credential_assignment"
      }
    ]
  }
}
```

约束：

- `output`、日志及 metadata 均不得包含命中原文。
- 行号是变更内容中的逻辑行号；不能可靠计算时允许省略。
- `blocked_by_security_policy` 属于不可原样重试错误。
- Engine 应引导模型改变方案，而不是重复同一动作。

---

### 5.3 Progress Guard 解耦

#### 目标

在第二次完全相同的只读动作出现时，及时提醒模型使用已有结果并进入修改或结束阶段，同时避免把合理的重新读取误判为循环。

#### 独立策略

从 Engine 中抽取 `ProgressPolicy`：

```python
class ProgressPolicy(Protocol):
    def evaluate(self, context: ProgressContext) -> ProgressSignal | None: ...
```

`ProgressContext` 至少包含：

- 请求意图。
- 当前 Turn 的结果。
- 动作指纹和重复次数。
- 自上次动作以来是否发生成功写入。
- 是否已经发送过恢复提示。

Engine 负责收集上下文、调用策略并把信号转换为 `ToolResult`，不直接包含具体判断规则。

#### 请求意图复用

新增 `RequestIntentClassifier`，统一判断：

- 是否属于项目请求。
- 是否明确要求文件变更。
- 是否只是审查、解释或诊断。

Workspace Summary 与 Progress Policy 复用该分类器，避免两套中英文关键词逐渐分叉。

英文关键词必须使用词边界；中文使用动作词与目标词组合。显式请求元数据可以覆盖自动判断。

#### 触发条件

同时满足以下条件时触发一次 `progress_required`：

1. 用户请求明确包含文件变更意图。
2. 动作属于只读上下文工具。
3. 相同工具和规范化参数在同一写入世代中第二次出现，即 `duplicate_count >= 1`。
4. 中间没有成功写入使旧上下文失效。
5. 本次运行尚未发送过相同恢复提示。

成功写入后递增“写入世代”或清空对应动作指纹，使后续重新读取合法。

#### 修正现有实现的一致性

如果触发条件从 `error_code == "duplicate_tool_call"` 改为 `metadata.duplicate == true`，生成恢复结果的逻辑也必须使用同一判断，否则 `repeated_actions` 会为空。

重复结果本身可以保持成功并引用历史结果；Progress Guard 通过单独的合成结果表达纠正信号，不混淆“工具失败”和“流程没有推进”。

---

### 5.4 项目识别与测试诊断复用

#### 决策

当前阶段不新增 `run_project_diagnostics`。先增强已有 `run_tests`，并抽取可复用的项目识别服务。

#### `ProjectDetector` 接口

从 Workspace Summary 中抽取项目标记识别：

```python
class ProjectDetector(Protocol):
    def detect(self, root: Path) -> list[ProjectProfile]: ...
```

`ProjectProfile` 包含：

- 项目类型和根目录。
- 标记文件。
- 候选测试命令。
- 可选的环境信息，例如 `.venv`。
- 检测依据和置信度。

复用方：

- Workspace Summary：展示项目概况。
- `run_tests`：解析默认测试命令。
- 未来诊断功能：选择构建、检查或健康检查流程。

#### `run_tests` 接口演进

- 保留显式 `command`，确保向后兼容。
- 允许省略 `command`，此时使用 `ProjectDetector`。
- 保留可选 `cwd` 和 `timeout`。
- 显式命令始终优先于自动检测。
- 仅有一个高置信度候选时自动执行。
- 多项目或多候选时返回结构化的 `test_command_ambiguous`，列出安全的候选描述。
- 找不到项目时返回 `test_command_not_detected`。

Python 环境选择顺序：

1. 用户显式命令中的解释器。
2. 当前进程所属解释器适用于目标项目时继续使用。
3. 项目根目录下已存在且结构有效的 `.venv`。
4. 系统 Python。

不得自动执行 `source`。应直接调用目标解释器，减少对持久 Shell 状态的依赖。

Python 导入路径应通过正确的项目根目录、可编辑安装或项目自身配置解决。只有检测到明确的源码布局且缺少安装步骤时，才设置受控的 `PYTHONPATH`，并在结果 metadata 中说明原因。

#### 何时再新增诊断工具

只有出现以下稳定需求后，才新增独立诊断工具：

- 启动并停止本地服务。
- 端口和健康检查。
- 多阶段构建、迁移与测试。
- 跨语言项目的统一诊断报告。

此时诊断工具应编排已有能力，而不是复制测试执行器。

---

## 6. 建议目录结构

```text
src/testcode/
├── intent/
│   ├── classifier.py
│   └── types.py
├── project/
│   ├── detector.py
│   └── types.py
├── safety/
│   ├── guardrails.py
│   ├── policy.py
│   ├── redaction.py
│   ├── secret_patterns.py
│   └── content/
│       ├── models.py
│       ├── scanner.py
│       ├── extractors.py
│       └── interceptor.py
├── orchestration/
│   ├── engine.py
│   └── progress.py
└── tools/
    ├── registry.py
    └── builtins/
        ├── patch.py
        ├── apply_change.py
        └── run_tests.py
```

目录不是一次性迁移要求。实现时优先形成稳定接口，再根据模块规模拆文件，避免只有一个实现却创建大量空抽象。

---

## 7. 测试策略

### 7.1 Prompt

- 安全规则被注入。
- `.env.example` 与 `.env` 的边界表达正确。
- Python 发行名与导入名不被混淆。
- 项目规则不能取消基础安全规则。

### 7.2 Secret Scanner 单元测试

必须覆盖：

- 常见真实令牌格式被阻断。
- 私钥头被阻断。
- 敏感字段赋值被阻断。
- 环境变量引用和标准占位值被允许。
- 哈希、UUID、普通 Base64、测试摘要不被误报。
- patch 只扫描新增行。
- 删除仓库中已有凭据能够成功。
- 多文件 patch 能返回所有安全位置。
- 扫描结果、日志和错误中不存在秘密原文。

### 7.3 执行拦截集成测试

- `patch` 在 Handler 执行前被阻断，文件保持不变。
- `apply_change` 使用相同安全入口。
- 无内容提取器的普通只读工具不受影响。
- 拦截器顺序稳定，参数校验失败时不运行扫描。
- 安全阻断不触发批准询问，也不能通过批准绕过。
- `blocked_by_security_policy` 不被原样重试。

### 7.4 Progress Guard

- 修改请求中第二次相同只读动作立即触发恢复提示。
- 纯审查或读取请求不触发。
- 参数不同的读取不视为重复。
- 成功写入后的重新读取合法。
- 只发送一次恢复提示。
- `repeated_actions` 包含正确且已脱敏的动作摘要。
- 中英文意图判断均覆盖词边界和动作/目标组合。

### 7.5 测试诊断

- 显式命令保持原行为。
- 单一项目能够选择默认命令。
- 多项目返回歧义错误，不擅自执行。
- `.venv` 解释器选择正确。
- 超时后进程被回收。
- 工作目录和导入路径正确。
- 结果说明实际使用的项目根目录、命令来源和环境来源。

---

## 8. 可观测性与安全指标

新增结构化事件时只记录安全摘要：

- `safety.content_scan.completed`
- `safety.content_scan.blocked`
- `orchestration.progress_guard.triggered`
- `project.detected`
- `tests.command_resolved`

建议指标：

- 凭据写入阻断次数及规则类别。
- 扫描耗时的 P50/P95。
- 重复读取在 Guard 前后的平均次数。
- Guard 触发后下一步进入写入或结束的比例。
- 自动测试命令解析成功率和歧义率。
- Scanner 误报回归用例数量。

严禁在事件中记录命中的凭据、完整 diff、完整环境变量或未经脱敏的工具参数。

---

## 9. 分阶段实施

### 阶段一：低风险行为约束

交付：

- 增加并测试 Prompt 安全规则。
- 修正 Python 包命名规则的语义。
- 明确 `.env`、`.env.example` 和前后端凭据边界。

验收：

- Prompt 测试通过。
- 不改变工具执行协议。

### 阶段二：Progress Guard

交付：

- 抽取 `RequestIntentClassifier`。
- 抽取 `ProgressPolicy`。
- 第二次相同只读动作触发恢复提示。
- 修正恢复结果与触发条件不一致的问题。

验收：

- 现有重复动作测试保持通过。
- 新增“写入后可重新读取”和“只读请求不误伤”测试。
- 典型循环从多次重复读取缩短为一次读取、一次提示、随后修改或结束。

### 阶段三：内容安全防线

交付：

- 定义 mutation、finding、extractor、scanner 和 interceptor 接口。
- 实现 patch 与完整内容提取器。
- 实现高置信度 Secret Scanner。
- 复用令牌规则目录并保证全链路脱敏。

验收：

- 高置信度凭据在任何文件写入前被阻断。
- 删除已有凭据不被阻断。
- 占位符和常见非秘密字符串不误报。
- 安全错误和运行日志不含秘密原文。

### 阶段四：诊断能力复用

交付：

- 抽取 `ProjectDetector`。
- Workspace Summary 改为复用该服务。
- `run_tests` 支持可靠的默认命令解析。
- 完善虚拟环境、工作目录、超时和进程清理。

验收：

- 原有显式命令完全兼容。
- 单项目可零配置运行测试。
- 多项目不误执行。
- 不残留后台进程。

---

## 10. 明确不做的事项

本轮不做：

- 不把 Secret Scanner 直接写入 `DefaultPolicy`。
- 不以单个简单正则作为安全边界。
- 不扫描 patch 删除行。
- 不在安全错误中返回秘密原文。
- 不允许用户确认绕过高置信度凭据阻断。
- 不新增与 `run_tests` 重叠的测试工具。
- 不自动创建、激活或修改虚拟环境。
- 不在无法确定项目类型时猜测并执行命令。
- 不用新的抽象替换所有现有代码；采用兼容、渐进式迁移。

---

## 11. 完成标准

方案完成不以“新增了多少规则或类”为准，而以以下结果为准：

1. 模型更少生成不安全代码。
2. 即使模型生成了真实凭据，写入也会在执行前被可靠阻断。
3. 安全检查、错误结果和日志均不会造成二次泄露。
4. 重复读取能够在第二次出现时得到纠正，且不妨碍合理的重新读取。
5. 项目识别只有一个事实来源，Workspace Summary 与测试执行保持一致。
6. 新写入工具可以通过实现统一接口接入安全能力，无需修改 Engine。
7. 每一层都能独立测试、替换和扩展，现有行为保持向后兼容。
