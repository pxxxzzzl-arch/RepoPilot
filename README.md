# Issue2Patch

Issue2Patch 是一个面向“从 Issue 到补丁”流程的 Python CLI 项目。当前完成到第六阶段：在可审计的安全工具、Docker 测试沙箱和供应商无关编排器之上，接入真实 Responses API 模型客户端与人工批准 CLI。

## 当前范围

- 使用 `pyproject.toml` 管理构建、依赖和 pytest 配置。
- 提供最小可运行的 `issue2patch` CLI 入口。
- 提供 `search_code`、`read_file`、`run_tests` 和 `git_diff` 四个只读工具。
- 提供结构化的 `PatchOperation`、`FileChange` 和 `apply_patch`。
- 支持先全量预检、生成 unified diff、dry-run 以及多文件写入失败回滚。
- `TraceRecorder` 以 JSONL 记录运行标识、时间、耗时、文件路径、哈希和错误摘要，不记录源码正文或环境变量。
- 只读工具返回 `ExecutionResult`，修改工具返回包含 `FileChange` 和 unified diff 的 `PatchResult`。
- `run_tests` 默认使用 `DockerSandboxRunner`；`run_tests_trusted` 仅适用于已信任的本地仓库。
- Docker 沙箱仅只读挂载仓库的临时副本，并限制网络、权限、CPU、内存、PID、临时目录和执行时间。
- `AgentOrchestrator` 每次运行再创建一份临时工作副本，所有搜索、读取、补丁和测试都只接收该副本的路径。
- 第一次模型调用前自动通过 `TestRunner` 执行目标测试；基准结果会作为结构化字段加入 `AgentContext`。若测试最初已经通过，Agent 会直接成功结束而不调用模型。
- 测试结果区分断言失败、测试超时、Docker/沙箱基础设施错误和容器清理错误；后三类不会降级为普通 `TESTS_FAILED`。
- 仓库复制、快照与搜索受文件数、单文件字节数和总字节数限制；搜索还受独立超时和最大结果数限制。
- Agent 搜索默认按固定字符串匹配。正则搜索需要动作设置 `regex=True`，并由 `AgentConfig(allow_regex_search=True)` 显式授权。
- Agent 仅能选择 `SearchAction`、`ReadFileAction`、`PatchAction`、`RunTestsAction` 和 `FinishAction`，不存在 shell 命令动作。
- `ReadFileAction` 在正文之外返回规范化相对路径、原始字节数和 SHA-256；模型必须将该哈希原样用于 `expected_sha256`，无需也不允许猜测哈希。
- `ModelClient` 是供应商无关的 Protocol；`ScriptedModel` 用于确定性测试，真实运行使用 `OpenAIResponsesModelClient`。
- `OpenAIResponsesModelClient` 使用 Responses API 严格 JSON Schema，将模型输出映射为现有 Action 联合类型；自由文本、额外字段、shell 动作和未知动作都会被拒绝。
- API Key 只能从 `OPENAI_API_KEY` 环境变量读取，不接受命令行参数；请求具有超时、有限重试和指数退避。
- 最终结果统计模型请求、重试、输入/缓存/输出 token、模型耗时和估算费用。
- `issue2patch run` 在执行前要求独立人工批准，实时状态写入 stderr，stdout 最终只输出临时副本的 unified diff。
- `demo_repo` 的 `divide()` 已通过第三阶段工具从乘法安全修改为除法。
- 暂不包含网页、Docker 构建自动化或 GitHub API；也不会自动修改原仓库、提交或创建 PR。

## 环境要求

- Python 3.10 或更高版本

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 运行 CLI

```bash
issue2patch --version
```

真实模型只从环境变量读取密钥：

```bash
export OPENAI_API_KEY="your-api-key"

demo_workdir="$(mktemp -d)"
cp -R examples/broken_calculator/. "$demo_workdir/"

issue2patch run \
  --repo "$demo_workdir" \
  --issue "divide should return quotient"
```

CLI 会先显示仓库、模型、潜在 API 费用和“仅修改临时副本”的边界，并要求输入 `y` 批准。非交互运行必须显式添加 `--approve`：

```bash
issue2patch run \
  --repo "$demo_workdir" \
  --issue "divide should return quotient" \
  --approve
```

进度、终止状态和用量统计写入 stderr；stdout 仅输出临时副本相对初始状态的 unified diff，因此可以安全重定向到补丁文件。命令不会把 Diff 应用到原仓库。

默认模型为 `gpt-5.6-luna`。可通过 `--model` 指定其他模型；未知模型仍统计 token，但费用显示为不可用。正则搜索仍需额外传入 `--allow-regex-search`。

## 构建测试沙箱镜像

```bash
docker build -f docker/sandbox.Dockerfile -t issue2patch-sandbox:py311 .
```

运行时使用 `--pull never`，不会自动从网络拉取镜像。镜像必须在本地预先构建。

## 运行项目测试

```bash
pytest
```

根目录的 pytest 配置只收集 `tests/` 中的 Issue2Patch 项目单元测试，它们应当全部通过。

## 验证演示模板

```bash
demo_workdir="$(mktemp -d)"
cp -R examples/broken_calculator/. "$demo_workdir/"
pytest "$demo_workdir/tests"
```

这里预期得到失败测试。`examples/broken_calculator/` 是始终保持故障的演示模板；每次演示都先复制到新的临时目录，再把临时目录交给 Agent。仓库中的 `demo_repo` 保留为第三阶段已经修复并能通过测试的历史样例。

具备 Docker、已构建沙箱镜像并设置 `OPENAI_API_KEY` 后，真实第六阶段验收应观察到：基准测试失败、模型读取文件并获得 SHA-256、生成合法补丁、临时副本测试通过、CLI 输出 Diff，并且模板与传入的原仓库都保持不变。本仓库当前开发机器尚未满足 Docker/API Key 前置条件，因此不能把 Mock 测试记作这项真实验收。

## 运行目标仓库测试

```python
from issue2patch.tools import run_tests, run_tests_trusted

# 默认：不可信仓库在 Docker 沙箱中运行。
result = run_tests("/path/to/repository", timeout=60)

# 仅当仓库已经可信时才在主机直接运行。
trusted_result = run_tests_trusted("/path/to/trusted-repository", timeout=60)
```

`DockerSandboxRunner` 使用随机 128 位容器名，禁止网络，以 UID/GID `65532:65532` 运行，将根文件系统和仓库副本设为只读，删除全部 capabilities，并启用 `no-new-privileges`。超时后会执行 `docker rm --force`，清理结果通过 `cleanup_status` 返回。

## 确定性 Agent 编排

```python
import hashlib
from pathlib import Path

from issue2patch import (
    AgentOrchestrator,
    FinishAction,
    PatchAction,
    PatchOperation,
    ReadFileAction,
    RunTestsAction,
    ScriptedModel,
)

repository = Path("/path/to/repository")
relative_path = "package/calculator.py"
before = (repository / relative_path).read_bytes()

model = ScriptedModel(
    [
        ReadFileAction(relative_path),
        PatchAction(
            (
                PatchOperation(
                    path=relative_path,
                    old_content="return a * b",
                    new_content="return a / b",
                    expected_sha256=hashlib.sha256(before).hexdigest(),
                ),
            )
        ),
        RunTestsAction(),
        FinishAction("tests passed"),
    ]
)

result = AgentOrchestrator(model).run(
    repository,
    issue="divide should return a quotient",
)
```

`AgentConfig` 限制最大步数、工具调用数、单次工具观察字符数、重复动作次数、测试/搜索超时、搜索结果数，以及仓库文件数、单文件和总字节数。自动基准测试计入一次工具调用。`AgentRunResult.status` 使用结构化终止状态，区分成功、断言失败、测试超时、测试基础设施错误、容器清理错误、非法动作、模型/工具异常、资源限制、无进展循环和补丁冲突。

`AgentContext.baseline_test` 始终保存第一次模型调用前的 `TestRunSummary`；`latest_test` 保存最近一次测试结果。`TestOutcome` 的可能值为 `PASSED`、`ASSERTION_FAILED`、`TIMED_OUT`、`INFRASTRUCTURE_ERROR` 和 `CLEANUP_ERROR`。

Agent JSONL 审计仅保存动作类型、读取路径/字节数/SHA-256、结果字符数与哈希、修改文件哈希、耗时、模型用量、脱敏错误摘要和终止原因；不保存源码、Diff、模型隐藏推理、环境变量或 API Key。

## 模型客户端测试

Mock API 单元测试不联网、不会产生费用，并覆盖结构化输出映射、非法输出、超时参数、有限重试和用量/费用统计：

```bash
pytest tests/test_models.py tests/test_cli.py
```

真实 API smoke test 默认跳过。只有同时配置 `OPENAI_API_KEY` 并明确设置下列开关时才会发出一个付费请求：

```bash
ISSUE2PATCH_RUN_LIVE_API=1 pytest tests/test_live_api_smoke.py
```

## 安全修改示例

```python
import hashlib
from pathlib import Path

from issue2patch import PatchOperation, TraceRecorder, apply_patch

root = Path("/path/to/repository")
relative_path = "package/calculator.py"
before = (root / relative_path).read_bytes()

operation = PatchOperation(
    path=relative_path,
    old_content="return a * b",
    new_content="return a / b",
    expected_sha256=hashlib.sha256(before).hexdigest(),
)
recorder = TraceRecorder(root / ".issue2patch" / "trace.jsonl")

preview = apply_patch(root, [operation], dry_run=True, trace_recorder=recorder)
result = apply_patch(root, [operation], trace_recorder=recorder)
```

`apply_patch` 仅修改已存在的 UTF-8 普通文件。它拒绝绝对路径、`..`、符号链接、敏感文件、超限文件、哈希冲突和不唯一的旧内容匹配。默认单次最多接受 10 个操作，累计处理的目标文件不超过 2,000,000 字节。
