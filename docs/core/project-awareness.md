# 核心运行时：项目感知与测试解析

## 文档职责

本文档说明运行时如何判断一个请求是否需要项目上下文、识别项目类型、加载规则和解析
默认测试命令。它不定义通用扩展接口或 prompt 字段格式；这些内容分别见
[运行时扩展](../extensions/runtime-interfaces.md)和[总体架构](../architecture.md)。

## 上下文来源

当前项目感知由三个上下文加载器和一个共享探测器组成：

- `ProjectRulesLoader`：从工作目录向上查找 `AGENTS.md`，在项目边界内按从远到近的
  顺序加载，近目录规则具有更高优先级。
- `WorkspaceSummaryLoader`：只在请求与当前项目相关时收集项目标记、Git 摘要、建议
  测试命令和有界目录树。
- `ExplicitContextLoader`：展开用户通过 `--context` 指定的工作区内文件、目录和
  glob，拒绝越界路径与二进制文件。
- `ProjectDetector`：为 workspace summary 和 `run_tests` 提供同一份项目类型与测试
  命令事实。

仅出现 `code`、`test`、`project` 等歧义词不会单独触发完整 workspace 摘要。明确的
代码动作、代码路径、仓库语义、显式 context 或运行时 metadata 才构成充分信号。

## 项目识别

共享探测器当前支持：

| 标记 | 项目类型 | 可靠时提供的默认测试命令 |
| --- | --- | --- |
| `pyproject.toml` | Python | `python -m pytest` |
| `package.json` | Node.js | `npm test` |
| `Cargo.toml` | Rust | `cargo test` |
| `go.mod` | Go | `go test ./...` |

Python 只有在存在 `tests/`、测试文件、pytest 配置或依赖证据时才提供默认命令。
Node.js 只有在 `package.json` 中存在有效的 `scripts.test` 时才提供默认命令。

探测器先从指定目录向项目边界查找最近标记；没有结果时，再有界扫描嵌套目录。缓存、
虚拟环境、构建产物和依赖目录不会进入嵌套扫描。

## `run_tests` 命令解析

用户显式传入的 `command` 始终优先。省略命令时：

1. 在 workspace 边界内探测项目。
2. 没有可靠候选时返回 `test_command_not_detected`。
3. 存在多个可运行候选时返回 `test_command_ambiguous`，要求显式选择。
4. 只有一个可靠候选时才自动执行。

Python 环境按以下顺序选择：

1. 项目中的 `.venv` 或 `venv` 可执行解释器。
2. 当前 testcode 进程的 Python 解释器。

检测到明确的 Python `src` layout 时，解析器为测试命令设置受控的相对
`PYTHONPATH=src`。结果 metadata 会记录命令来源、环境来源、项目根目录、耗时和是否
通过。

## 边界与失败语义

- 不自动创建、安装或激活虚拟环境。
- 不在多个候选之间猜测。
- 不把任意目标 workspace 的 `.env` 当作 testcode 模型配置加载。
- 自动命令仍属于 `test` 风险，必须遵守当前安全模式和审批规则。
- 项目规则与显式上下文有独立大小和数量限制；当前尚未经过统一 token 预算打包。
- workspace summary 是模型上下文，不是构建系统或项目配置的替代品。
