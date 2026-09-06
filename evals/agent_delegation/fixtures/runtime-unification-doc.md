# OpenBear 统一 Agent Runtime 重构方案

> **文档性质**：设计、迁移与验收总纲；不代表任何代码、数据库迁移、部署或服务重启已经实施。\
> **适用范围**：OpenBear 主会话、direct 子 Agent、managed（Plan 管理）子 Agent。\
> **依据**：2026-08-26 源码架构审计及用户确认、修正后的目标边界。\
> **变更原则**：可以改变内部模块、数据库 schema、API payload 与事件 wire；不得改变用户实际看到的内容和用户可见行为，且历史会话必须可正确续聊。

## 1. 摘要与核心决策

OpenBear 当前有两套实质性的模型/工具执行循环：主会话使用 `app/agent/loop.py::Agent.run`，Web 侧由 `app/web_console/chat_runtime.py::WebAdminChatRunMixin._run_web_turn` 组装上下文、持久化、账单和投影；子 Agent 使用 `app/rath/single_agent.py::SingleAgentWorkflowRunner` 自行完成模型调用、工具循环、重试、压缩、原生上下文、账单和控制检查。Rath 同时还承载任务、会话、事件、artifact、Plan、控制请求、并发租约与恢复等有价值的控制面能力。

本方案的核心决策：

1. 从主 Agent 的成熟执行语义中抽取**唯一完整执行内核 `AgentRuntime`**，但不把 Web 壳复制给子 Agent。
2. 主会话与子 Agent 都通过明确的 Adapter、Policy、Store、Event Sink 驱动同一 Runtime；差异以组合实现，不在内核中堆积 `is_rath`、`is_main` 一类 mode flag。
3. Rath 不再拥有独立模型/工具循环；其任务、Plan、控制、并发、artifact、通知等能力迁入独立 `Supervisor` 控制面并继续服务统一 Runtime。
4. 引入规范化的持久运行模型和内部事件模型；旧表、旧 API、旧事件可以迁移或替换。对用户可见界面统一通过 `Compatibility Projection` 保持现状。
5. 历史连续性以“历史会话真实可续聊且上下文语义正确”为硬契约，而不是“旧表永远不改”。采用版本化迁移器、备份、双读/受控双写、canary、语义校验和可逆切换。
6. 分阶段迁移：先冻结契约并统一物理模型请求，再从主路径抽出完整 Runtime，随后建立新持久化/投影、迁移 direct 与 managed，最后切换主会话新真源和历史数据；任何阶段失败均可按 conversation/task 粒度切回旧路径，避免大爆炸替换。

## 2. 目标、非目标与契约分层

### 2.1 目标

- 只有一个负责“模型请求—流式响应—工具选择与执行—上下文推进—重试/压缩—终止”的生产执行内核。
- 主会话、direct 子 Agent、managed 子 Agent 使用同一状态机和相同的 provider 原生上下文、usage、取消等基础语义。
- Web、Plan、任务控制、工具授权、持久化、事件投影彼此解耦，可独立测试和替换。
- Rath 的有效控制面能力不退化，同时删除其独立 executor。
- 已有会话和 Agent session 在数据转换后仍能正确续聊。
- 页面展示、文案、消息顺序、工具/Plan/任务卡片、统计、错误/停止状态和操作结果不变。

### 2.2 非目标

- 不建设插件平台、多租户、分布式调度或通用工作流产品。
- 不做无关 UI 改版、文案优化或交互重设计。
- 不借重构更换模型供应商协议或改变 Agent 产品能力。
- 不要求保留当前 Rath 模块名、旧表名、旧 payload 字段或旧 wire contract。
- 本文档不授权代码修改、数据迁移、部署、重启或生产开关切换。

### 2.3 三类契约

#### A. 用户可见契约（必须保持）

用户从页面和操作中观察到的内容与结果必须保持：可见消息文本与顺序、思考展示规则、工具行及状态、Plan 与步骤卡片、任务/Agent 卡片、artifact、进度与通知、token/费用/耗时统计、错误文案、停止/取消/暂停/恢复结果、刷新后的状态，以及相同操作的结果。

#### B. 内部可变结构（允许改变）

Python 模块与类名、数据库表与字段、API/WS/SSE payload、内部事件、前端 store、DAO、模型上下文 sidecar、Rath 命名和兼容代码均可改变。变化必须由版本化迁移和 Compatibility Projection 吸收。

#### C. 历史连续性契约（必须保持）

历史可见内容不丢失；续聊时角色、顺序、工具调用与结果配对、摘要边界、系统快照、附件、Task Memory、原生 provider continuation 和 Agent session 归属语义正确；不会重放已执行工具、重复计费或因旧格式无法恢复。若某个 provider 原生 opaque item 无法安全转换，必须明确降级到经过校验的中性上下文并记录原因，不能猜测重建。

## 3. 当前源码事实与要解决的问题

### 3.1 已确认的源码事实

- `app/agent/loop.py` 已定义 `Renderer`、`Persister`、`EmergencyCompactor` Protocol；`Agent.run` 负责模型流、工具循环、重试、无进展检测、usage、原生 continuation、上下文压缩、steer 与取消检查。它目前固定读取 `ToolRegistry.schemas(scope="main")`，并含 Web 页脚、Agent 工具 usage 合并等边界语义。
- `app/web_console/chat_runtime.py::_run_web_turn` 是大型 Web 编排层，负责模型/think/Fast 快照、系统提示和历史、私有模型上下文、Task Memory 注入、流式渲染、账单 ledger、压缩、通知及收尾。这些职责不应整体进入 Runtime。
- `app/rath/single_agent.py::SingleAgentWorkflowRunner` 再实现了 provider 调用、工具 schema 冻结与授权、循环/重试、原生上下文、provider usage 驱动的压缩、账单、Plan 协议和 durable control 检查；它同时承担了执行面与控制面。
- `app/rath/manager.py::RathTaskManager` 的 durable 控制真源是 `rath_task_controls`，内存 registry 用于进程内即时取消；它还管理并发 `ExecutionLease`、pause/resume/stop/steer 与任务路由。
- `app/rath/plan.py` 和 `app/rath/dao.py` 覆盖 Plan version/state/step/evidence/decision、task/event/artifact/control、Agent session/model context 等持久控制语义。
- `app/models/agent_runtime.py` 当前名称虽含 runtime，但职责是解析并冻结 Agent 的 model/think/Fast 配置，不是统一执行内核。
- 相关测试入口包括 `tests/test_agent_loop.py`、`tests/test_agent_runtime.py`、`tests/test_live_agent.py`、`tests/test_rath_single_agent.py`、`tests/test_rath_plan*.py`、`tests/test_rath_dao.py`、`tests/test_rath_web_api.py`、`tests/test_tools_agent_orchestration.py` 等。本文档不声称这些测试当前已全部通过。

### 3.2 痛点

1. **行为漂移风险**：主/子循环各自修复 provider、retry、native context、compaction、usage 时容易只修一侧。
2. **边界倒置**：执行状态机知道 Web renderer/footer、Rath Plan/Agent tool 等上层概念；Rath runner 同时知道 DAO、Plan、模型和工具细节。
3. **账单与统计多路径**：物理 model call、aggregate usage、子 Agent usage 合并散布于 loop、chat runtime、Agent 工具和 Rath runner，存在重复记账风险。
4. **控制语义耦合**：cancel、stop、pause、resume、steer 的即时信号与 durable 状态分散，难以证明“确认后必生效且不污染下一轮”。
5. **事件即实现细节**：Rath events、主流式 renderer 和 Web payload 缺少共同规范，前端兼容依赖手工同步。
6. **历史上下文多形态**：公开 transcript、私有模型 checkpoint、summary、Rath model context、Agent session 历史存在不同锚点和校验方式，迁移需做语义级而非字段级验证。

## 4. 术语

- **Runtime**：唯一的模型/工具执行状态机。输入为规范化 RunSpec 与可恢复状态，输出为规范事件和 RunOutcome；不拥有 Web、Plan 或具体数据库。
- **Adapter**：将主会话或子任务环境转换为 Runtime ports 的边界实现，如 Model Adapter、Tool Adapter、Context Adapter。
- **Policy**：纯决策或小状态决策接口，如重试、压缩、工具授权、预算、停止条件；不直接渲染 UI。
- **Store**：持久化 port 及其实现，保存运行、消息、原生上下文、usage、task/plan/control 等 durable state。
- **Event Sink**：消费 Runtime 规范事件；可持久化、流式推送、记录指标或投影 UI，不能反向改变内核事实。
- **Supervisor**：控制面，管理任务生命周期、并发租约、pause/resume/cancel/steer、Plan gate、Agent session、通知与恢复；不调用模型流或自行执行工具循环。
- **Compatibility Projection**：将新内部状态/事件投影为当前用户看到的页面模型、文案、顺序和操作结果；不是永久冻结旧 wire。
- **Run**：一次可暂停/恢复的执行实例；主 turn 和子 task 都映射为 Run。
- **Attempt**：Run 内一次物理模型请求；账单幂等键以 Attempt 为基础。

## 5. 目标架构

```text
Web/API/Agent Tools
        |
  Entry Adapters
  - MainConversationAdapter
  - DirectAgentAdapter
  - ManagedAgentAdapter
        |
  Supervisor ---------------- Plan / Control / Lease / Notification
        |                                  |
        +---------- RunSpec / Control ------+
                         |
                   AgentRuntime
       +-----------------+------------------+
       | Model Port      | Tool Port        |
       | Context Policy  | Retry Policy     |
       | Compaction Port | Cancellation Port|
       | Checkpoint Store| Accounting Sink  |
       +-----------------+------------------+
                         |
               Canonical Runtime Events
                  /          |          \
             Run Store   Event Store   Compatibility Projection
                                          |
                                     current visible UI
```

### 5.1 Runtime 只负责

- 按状态机顺序组装并发出物理模型请求。
- 消费流式 model event，维护正文、reasoning、tool call、native item 与 usage。
- 依据注入 Policy 做 retry、empty/reasoning-only、overflow compaction、无进展与预算终止。
- 对获准工具执行、持久化工具结果并推进上下文。
- 在安全点处理 cancel/steer/checkpoint，产出确定的终态。
- 每个事实只发一个规范事件，不生成 UI 文案。

### 5.2 Runtime 明确不负责

- 读取 Web conversation 设置、构造页面 payload 或 HTML/footer。
- 决定 Plan 是否批准、task 是否占并发槽、通知发给谁。
- 直接 SQL 操作 Rath/Web 表。
- 依据 `main/direct/managed` mode 分支实现第二套算法。

### 5.3 Adapter 与 Policy 组合

| 组合项 | 主会话 | direct 子 Agent | managed 子 Agent |
|---|---|---|---|
| Context Adapter | Web transcript + snapshot + private checkpoint + Task Memory | Agent session/task context | direct 基础 + approved Plan runtime/evidence |
| Tool Policy | main scope + 现有授权 | Agent allowlist + agent scope | allowlist + Plan phase frozen schema/gate |
| Supervisor | turn control | task/lease/control | task/lease/control + Plan coordinator |
| Event Projection | 可见聊天流 | Agent/task 卡片与通知 | task + Plan/步骤/evidence 卡片 |
| Store | conversation/run store | run + agent session/task store | run + task/plan/control store |

差异只存在于 ports 的实现和输入数据；模型调用、retry、native continuation、工具执行推进、usage 采集属于同一 Runtime。

## 6. 关键接口（概念契约）

接口名可在实施中调整，但边界语义必须保持。

```python
@dataclass(frozen=True)
class RunSpec:
    run_uuid: str
    owner_kind: Literal["conversation_turn", "agent_task"]
    owner_uuid: str
    root_turn_uuid: str
    model_snapshot: ModelSnapshot
    initial_context: ContextRef
    policy_snapshot: PolicySnapshot
    idempotency_key: str

class AgentRuntime:
    async def execute(
        self,
        spec: RunSpec,
        ports: RuntimePorts,
        resume: ResumeToken | None = None,
    ) -> RunOutcome: ...

class ModelPort(Protocol):
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...

class ToolPort(Protocol):
    async def schemas(self, auth: ToolAuthorizationSnapshot) -> list[ToolSchema]: ...
    async def execute(
        self,
        call: ToolCall,
        ctx: ToolContext,
        idempotency_key: str,
    ) -> ToolResult: ...

class ContextStore(Protocol):
    async def load(self, ref: ContextRef) -> CanonicalContext: ...
    async def checkpoint(
        self,
        checkpoint: RuntimeCheckpoint,
        expected_revision: int,
    ) -> int: ...

class RuntimeEventSink(Protocol):
    async def append(self, event: RuntimeEvent) -> None: ...

class ControlPort(Protocol):
    async def poll(
        self,
        run_uuid: str,
        after_revision: int,
    ) -> list[ControlCommand]: ...

class AccountingSink(Protocol):
    async def record_attempt(
        self,
        usage: AttemptUsage,
        idempotency_key: str,
    ) -> None: ...
```

### 6.1 不变量

- `run_uuid + event_seq` 唯一且递增；重放不得产生第二次副作用。
- 每个工具调用有稳定 `tool_call_uuid`；执行幂等键不因恢复改变。
- 每个物理模型请求有稳定 `attempt_uuid`；账单只能写入一次。
- checkpoint 采用 revision/CAS；并发恢复不能双跑。
- 原生上下文必须绑定 provider/protocol/model/session/transcript anchor/schema version；不匹配时拒绝 opaque replay。
- pause 只在声明的安全点进入 `paused`；cancel 可抢占 retry wait/model stream/tool 边界，最终只产生一个终态。
- Event Sink 失败策略明确：durable sink 未确认则不得对外宣称相应状态已提交。

## 7. 状态机与控制语义

建议 Run 状态：`created -> queued -> running -> {waiting_tool, waiting_control, paused} -> running -> {succeeded, failed, cancelled}`。`pausing`、`cancelling` 可作为操作中的 durable 状态，不能映射成错误的用户终态。

- **取消/停止**：写 durable command 后触发进程内 wake-up；Runtime 在 model stream、retry sleep、工具前后检查。已提交到外部且不可取消的工具必须记录“结果未知/已提交”而非重做。
- **暂停**：只在 checkpoint 已落盘、没有未决不可幂等副作用的安全点确认 `paused`；释放 `ExecutionLease`。
- **恢复**：Supervisor 获取 lease 和 run CAS，加载 checkpoint、已消费 controls、工具/attempt 幂等记录后继续。
- **steer/message**：作为有序 control 输入，在下一模型请求前并入上下文；不可悄悄改写已冻结的 Plan scope。managed 场景若改变 Plan 合同，先经 replan gate。
- **进程重启**：Supervisor 扫描非终态 run，以 lease owner/heartbeat/revision 判断接管；不依赖内存 registry 作为事实真源。

## 8. 规范事件与 Compatibility Projection

### 8.1 内部事件最小集合

`run.created/started/status_changed`、`model.attempt_started/delta/reasoning/native_item/usage/completed/failed`、`tool.requested/started/progress/completed/failed`、`context.checkpointed/compacted/native_reset`、`control.accepted/applied/rejected`、`plan.*`、`artifact.created`、`accounting.recorded`、`run.paused/resumed/cancelled/failed/succeeded`。

每个事件至少含 `event_uuid`、`run_uuid`、`seq`、`occurred_at`、`owner_kind/uuid`、`root_turn_uuid`、`schema_version`、`payload`；包含敏感/opaque provider 数据的事件标为 private，不进入前端。

### 8.2 投影规则

1. Runtime 事件先归一化，前端不直接理解 provider 或 Runtime 内部状态。
2. Compatibility Projection 生成当前页面需要的 message/tool/Plan/task/statistics/error/stop view model；可继续临时输出旧 API，也可同步改前端消费新 API。
3. 用户可见排序使用显式 projection sequence，不依赖异步事件到达时间。
4. 文案由现有 UI 文案目录/投影规则提供，不从内部异常文本临时拼接。
5. ledger 是统计真源；流式 usage 仅作进行中展示，终态以 ledger projection 校正，避免 aggregate 重写导致双计费。

### 8.3 用户可见兼容验证

- 建立代表性 fixture 的**规范事件 golden**与**UI view-model golden**。
- 浏览器测试比较关键 DOM 文本、元素顺序、状态 class/ARIA、按钮可用性和操作结果；动态 UUID/时间做字段级归一化，不忽略业务内容。
- 对主会话、direct、managed 的关键路径做固定 viewport 截图回归；截图只作辅助，DOM/行为断言为主。
- 捕获迁移前后的页面可见消息、工具行、Plan/task 卡片、统计和错误，做结构化 diff。
- 验证刷新/重连后投影与流式结束态一致。
- wire 变化时只要求新旧前后端在发布窗口内按部署策略兼容，不将旧 wire 永久写入架构约束。

## 9. 数据模型与版本策略

以下是目标逻辑模型，物理表名可按项目数据库规范确定。

| 逻辑实体 | 关键字段 | 用途 |
|---|---|---|
| `runtime_runs` | run_uuid, owner_kind/uuid, root_turn_uuid, status, state_version, model/policy snapshot, checkpoint_revision, legacy_ref | 统一主/子运行实例 |
| `runtime_events` | event_uuid, run_uuid, seq, type, visibility, payload, schema_version | 可重放规范事件 |
| `runtime_checkpoints` | run_uuid, revision, phase, canonical_context, native_context_ref, pending calls, control cursor, digest | 恢复状态 |
| `runtime_attempts` | attempt_uuid, run_uuid, request_no, provider identity, status, usage, cost, accounting key | 物理请求与账单幂等 |
| `runtime_tool_calls` | tool_call_uuid, run_uuid, provider id, name, args, auth snapshot, status, result, idempotency key | 工具副作用与恢复 |
| `runtime_contexts` | context_uuid, owner, canonical messages, anchor, schema_version | 可迁移中性上下文 |
| `runtime_native_contexts` | context_uuid, protocol/model/session/anchor, opaque payload, replayable | provider 原生 continuation |
| `agent_tasks/sessions` | 现 Rath task/session 的业务字段 | Supervisor 控制面 |
| `agent_controls/artifacts` | command cursor/result；artifact 元数据 | durable 控制与产物 |
| `agent_plan_*` | version/state/step/evidence/decision | managed 控制面，保持业务语义 |

### 9.1 Schema/version

- 数据库使用项目现有 `app/db/schema.sql` 与 `app/db/schema_migrations.py` 机制增加明确 migration id；不要只靠运行时“字段不存在就加”。
- 每个 JSON payload 自带 `schema_version`；数据库 migration version 与 payload version 分开。
- Reader 支持的版本集合必须显式；writer 只写当前版本。旧版本转换为 canonical DTO 后再进入 Runtime。
- 模型/think/Fast 的冻结优先级与 `app/models/agent_runtime.py` 当前语义保持；可重命名该模块以免与执行 Runtime 混淆。

## 10. 历史数据迁移与恢复

### 10.1 迁移对象

公开 conversation/message/system snapshot/summary、主会话 private model context、Rath tasks/events/artifacts/controls、Rath Agent sessions/model contexts、Plan versions/state/steps/evidence/decisions，以及 usage/accounting 关联。实施前先建立逐表/逐字段映射清单和不可变业务键。

### 10.2 迁移器要求

1. **增量、幂等**：以 `migration_name + source_type + source_pk + source_revision` 建 migration ledger；重复运行只校验或补齐，不重复生成 event、tool call、usage。
2. **可断点**：按 conversation/task 批处理；批次有 started/completed/failed、计数、digest 和错误样本。
3. **可验证**：迁移前后做行数/归属/终态/usage 总和校验，更重要的是 canonical transcript digest、工具配对、summary anchor、native context identity、Plan 当前版本语义校验。
4. **不盲转 opaque 数据**：只有 provider/protocol/model/session/anchor 全匹配才复制为 replayable；否则保留原始备份，生成 `native_reset_reason`，使用中性 transcript 续聊并做真实 provider 样本验证。
5. **备份先行**：迁移前生成一致性数据库备份和附件/artifact 引用清单，记录 checksum、创建时间、恢复命令及演练结果；备份不写入公共 artifact。
6. **dry-run**：先只输出映射、冲突、预计变更和样本 diff，不写生产数据。

### 10.3 发布期间读写策略

推荐“**新表回填 + 双读比较 + 单真源写入**”，尽量避免长期双写：

- 旧 executor 阶段仍写旧真源；迁移器持续回填新模型，shadow reader 比较 canonical DTO。
- 切换某 cohort 后，新 Runtime 写新真源；Compatibility Projection 从新真源输出。必要时短期写 legacy projection，但必须由事件投影产生并有幂等键，不允许两个 executor 同时写业务事实。
- reader 按 conversation/task 上的 `runtime_generation` 或 routing manifest 选单一真源，禁止“新表缺了就无声拼旧表”造成混合历史。

### 10.4 Canary 与真实旧会话

- 从只读副本选取脱敏规则不改变语义的真实样本；测试环境按授权使用真实数据，不凭空合成替代。
- 样本至少包含：长历史+summary、工具多轮、失败/取消、中断恢复、Responses native items、Anthropic thinking/signature（如现网存在）、有附件、Task Memory、direct、managed/多 Plan 版本、Agent session 连续任务、账单记录。
- canary 顺序：内部测试 conversation → 新建低风险 conversation → 已迁移的真实旧主会话 → direct → managed；可按阶段迁移顺序调整，但每类必须单独开关。
- 每个 canary 比较迁移前只读基线、迁移后首次打开、首次续聊、再次刷新和账单变化。

### 10.5 失败恢复

- 切换前：丢弃未完成新批次并从 migration ledger 重跑。
- 切换后未产生新写：routing manifest 切回旧 reader/executor。
- 切换后已产生新写：停止该 cohort 新请求，导出自切换水位后的新事件，用经演练的 reverse projector 回填旧兼容结构，校验后再切回；不可直接恢复整库覆盖其他会话。
- 工具副作用和账单依据 idempotency ledger 对账，绝不通过重跑模型/工具“补历史”。

### 10.6 删除 legacy reader/旧表的门槛

仅当全部 cohort 已迁移；至少一个稳定发布观察窗口无 legacy read；所有真实样本可续聊；新备份可独立恢复；reverse/forward 校验完成；无未处理迁移错误；用户可见回归与账单对账通过；旧 executor 已无生产调用且有静态架构测试；数据库负责人批准后，才可在独立 migration 中删除旧表。删除前必须做最终归档备份。不要预设旧结构永久保留。

## 11. 分阶段实施计划

> 相对量级仅用于排序：S（局部）、M（跨模块）、L（跨执行路径/数据）。不承诺精确工期。

### 阶段 0：冻结基线与可见契约（M）

- **目标**：把“不能变什么”变成可执行基线。
- **范围**：现有主/direct/managed 的事件、DOM、截图、账单、控制和历史样本；schema 映射清单。
- **具体工作**：建立 fixture 录制/归一化工具；定义 canonical event 和 projection golden；建立真实旧会话样本集与恢复副本；标注物理 model call/工具/账单幂等键；记录现有文案与排序。
- **明确不做**：不改 executor，不迁表，不调整 UI。
- **交付物**：兼容基线、样本 manifest、验收 harness、数据映射草案、已知差异清单。
- **进入条件**：获准读取测试/生产副本样本；确定数据保管边界。
- **完成条件**：全局矩阵每个场景都有可重复 fixture 或明确阻塞说明。
- **验收条件**：旧实现连续两次运行经动态字段归一化后 golden 稳定；账单与 DB ledger 对得上。
- **测试证据**：测试报告、DOM/行为断言、截图、canonical digest；不以“肉眼看起来一致”代替。
- **回滚点**：仅新增测试资产，删除/关闭 harness 即可；不触及生产状态。
- **依赖**：Web 测试能力、数据库只读副本、审计结论。

### 阶段 1：统一物理模型请求执行器（M，高风险）

- **目标**：先消除主/Rath 在 provider 流式调用、retry、usage 和 native item 聚合上的第一层双实现，为完整 Runtime 提取建立稳定底座。
- **范围**：`app/agent/loop.py` 的物理模型请求段、`app/rath/single_agent.py::_call_model`；统一 `ModelRequestExecutor`、`ModelExecutionResult` 与 `ModelAttemptRecord`。
- **具体工作**：统一 `backend.stream` 事件聚合、正文/reasoning/tool call/native item、finish reason、retry 分类与等待、partial stream 失败、retry cancel check、每次物理请求 usage/cost attempt hook；主/Rath 外层循环暂时只负责消费同一结果。
- **明确不做**：不统一工具循环，不迁 Plan，不改数据库真源，不改前端 wire，不改变主/子生命周期。
- **交付物**：共享模型请求执行器、两条旧循环的薄适配、attempt contract tests、旧/新结果 trace 对比报告。
- **进入条件**：阶段 0 的 provider/retry/native/accounting 基线可重复；已定义 partial stream 后 tool call 完整性的处理规则。
- **完成条件**：主会话和 Rath 生产路径均通过共享执行器发起物理模型请求；两边不再各自维护等价的流式聚合与 retry 算法。
- **验收条件**：普通回复、tool-call、retryable/non-retryable error、partial stream、empty/reasoning-only、native continuation、overflow signal、取消与 attempt 账单结果和基线一致；每个物理请求只生成一条 attempt 记录。
- **测试证据**：现有主/Rath provider 相关测试、新共享 executor contract test、失败注入与动态字段归一化 trace；只有实际 CI 运行后才能标记通过。
- **回滚点**：main/direct/managed 可分别通过临时开关回到原物理请求实现；不涉及数据迁移。
- **依赖**：阶段 0；provider adapter、retry 和 accounting 规则评审。

### 阶段 2：抽取 Runtime ports 与纯内核，主路径同实现适配（L）

- **目标**：从 `app/agent/loop.py` 提炼唯一可复用内核，保持主会话行为。
- **范围**：model/tool/context/retry/compaction/control/accounting/event ports；现 `Agent.run` 作为旧 facade 适配新内核。
- **具体工作**：在阶段 1 共享模型请求执行器之上拆出 Runtime 状态机和规范事件；把 `Renderer/Persister/footer` 转为 adapter/sink；工具 scope 改由 Tool Policy 注入；把 usage/attempt idempotency 明确化；保留 facade 供现有调用方。
- **明确不做**：不接 Rath runner，不改数据库真源，不改前端 wire。
- **交付物**：Runtime 核心、主会话 adapters、contract tests、架构依赖测试。
- **进入条件**：阶段 1 通过；状态机/事件 ADR 批准；共享 `ModelRequestExecutor` 的结果契约稳定。
- **完成条件**：主会话可通过 facade 使用新内核；核心不导入 `web_console`、`rath` 或具体 DAO。
- **验收条件**：主会话全矩阵 golden 相同；retry/native/compaction/tool/取消结果一致；每个 attempt 只记账一次。
- **测试证据**：`test_agent_loop` 等现有相关测试加新 contract test；失败注入测试；静态 import/调用图检查。只有实际 CI 运行后才能标记通过。
- **回滚点**：feature flag 切回原 `Agent.run` 实现；旧 facade/API 保留。
- **依赖**：阶段 1；模型 adapter 与 accounting 所有者。

### 阶段 3：统一运行持久化与事件投影（L）

- **目标**：建立可恢复 Run/Attempt/Tool/Event/Checkpoint 真源和 Compatibility Projection。
- **范围**：新 schema、migration scaffold、shadow write/read comparison、前端 projection adapter。
- **具体工作**：增加版本化表；实现幂等 stores；主会话在不切真源时 shadow 生成新事件；将新事件投影成当前 UI view model；进行账单双算但仅旧路径入账。
- **明确不做**：不删除旧表，不让两个路径真实记账，不迁子 Agent executor。
- **交付物**：schema migration（待授权执行）、stores、projection、shadow diff 报告、恢复演练脚本。
- **进入条件**：阶段 2 通过；数据库设计和备份/恢复方案评审通过。
- **完成条件**：代表性 run 可从 checkpoint 重建；shadow projection 与基线无未解释差异。
- **验收条件**：event seq/CAS/幂等/崩溃点测试通过；账单双算差为零或每项有批准解释。
- **测试证据**：migration up/down 或 forward/reverse 测试、故障注入、projection golden、只读对账报告。
- **回滚点**：关闭 shadow；新表不作为真源，可保留待诊断或按 migration 回退。
- **依赖**：数据库迁移授权在实际阶段另行取得；阶段 2。

### 阶段 4：迁移 direct 子 Agent（L）

- **目标**：direct 子 Agent 改用统一 Runtime，Rath 保留 Supervisor 控制面。
- **范围**：`SingleAgentWorkflowRunner` 中模型/工具循环替换为 DirectAgentAdapter；Agent session、工具 allowlist、task event/artifact、usage、通知。
- **具体工作**：将 prompt/context 和 frozen model/think/Fast 转为 adapters；工具授权做 Policy；manager lease/control 接 ControlPort；按 task cohort feature flag 路由；旧 runner 与新 Runtime 做非副作用场景 shadow 比较。
- **明确不做**：不迁 managed Plan gate；不删除旧 runner。
- **交付物**：DirectAgentAdapter、Supervisor 接口、cohort 开关、direct 兼容报告。
- **进入条件**：阶段 3 stores/projection 稳定；direct 样本和工具幂等策略齐备。
- **完成条件**：新建与续跑 direct task 均由统一 Runtime 完成，旧 UI/通知/统计不变。
- **验收条件**：工具授权无扩大；取消/停止/AgentMessage、Agent session 连续性、native context、账单和 artifact 全通过矩阵。
- **测试证据**：扩展 `test_rath_single_agent.py`、`test_tools_agent_orchestration.py`、`test_rath_agent_tool_compat.py`；真实 canary 报告。
- **回滚点**：按 task/cohort 切回旧 runner；已由新真源写入的 task 使用 reverse projection 后切回，禁止同一 task 双跑。
- **依赖**：阶段 3；工具副作用幂等覆盖。

### 阶段 5：迁移 managed Agent 与 Plan（L，高风险）

- **目标**：Plan 作为 Supervisor Policy/Gate 驱动统一 Runtime，不再嵌在模型循环。
- **范围**：Plan submit/progress/replan/decision/evidence、执行工具冻结、pause/resume、并发 lease、控制确认。
- **具体工作**：把 `app/rath/plan.py` 语义映射为 Plan Supervisor；在模型请求前注入 Plan runtime snapshot；在工具调用前做 phase/step/allowlist gate；Plan event 进入规范事件后兼容投影；验证重启接管和等待审批释放 lease。
- **明确不做**：不改变 Plan 产品流程、卡片、文案、批准规则；不顺带重做 Plan schema 产品设计。
- **交付物**：ManagedAgentAdapter、Plan Policy/Supervisor、恢复测试、managed canary 报告。
- **进入条件**：direct 稳定；Plan 状态/版本/控制不变量评审完成；真实多版本样本可用。
- **完成条件**：managed task 无需 Rath 独立 model/tool loop；Plan 生命周期和任务终态由 Supervisor+Runtime 完成。
- **验收条件**：未批准不执行；replan 后旧版本不能继续；pause/resume 不占错 lease；重启无双跑；所有卡片和操作结果与基线一致。
- **测试证据**：`test_rath_plan.py`、`test_rath_plan_runtime.py`、manager stale/restart/并发故障测试、DOM/行为/截图 golden。
- **回滚点**：managed cohort 路由回旧 runner；以 Plan version 和 event waterline 反投影，活动 task 先安全暂停再切换。
- **依赖**：阶段 4；Plan owner、DB owner、Web projection owner 共同验收。

### 阶段 6：主会话切换新真源与历史迁移（L，高风险）

- **目标**：主会话和全部子 Agent 使用统一 Run Store；历史数据满足连续性契约。
- **范围**：公开 transcript、summary、private/native context、Task Memory anchor、usage；前后端可同步改 wire。
- **具体工作**：dry-run/备份/回填；dual-read compare；按 conversation canary 切 reader/writer；真实旧会话续聊；新 API 与前端 store 如需同步发布则使用版本协商或部署顺序兼容。
- **明确不做**：不删除 legacy reader/表，不改变 UI 展示。
- **交付物**：幂等迁移器、校验/对账报告、routing manifest、恢复 runbook、canary 结论。
- **进入条件**：阶段 5 稳定；备份恢复演练成功；迁移和生产操作另获授权。
- **完成条件**：所有目标 cohort 新写入新真源；旧会话样本连续续聊；没有 silent fallback/mixed history。
- **验收条件**：内容/顺序/工具配对/summary anchor/native identity/usage 对账通过；首次续聊不重复工具或账单；UI 全矩阵通过。
- **测试证据**：migration ledger、前后 digest、真实样本 transcript diff、provider continuation 测试、UI 回归、账单对账。
- **回滚点**：routing manifest 按 conversation 回切；有新写时依 10.5 reverse projection；整库恢复只用于灾难恢复且须单独批准。
- **依赖**：阶段 0 样本、阶段 3 stores、数据库与发布授权。

### 阶段 7：删除双实现与 legacy（M/L）

- **目标**：完成唯一内核约束，清理已证明无调用的旧路径。
- **范围**：`SingleAgentWorkflowRunner` 独立 model/tool loop、旧 facade 分支、legacy readers/tables/events。
- **具体工作**：生产调用计数归零；删除 executor；保留/重命名 Rath 控制面；增加禁止第二循环的架构测试；按独立 migration 删除旧表。
- **明确不做**：不借机通用化框架或改 UI。
- **交付物**：删除清单、依赖图、最终迁移、归档与恢复说明。
- **进入条件**：第 10.6 节全部门槛满足；最终备份完成；删除获批准。
- **完成条件**：生产只有一个 Runtime；旧 executor/reader 无代码路径；新备份可恢复。
- **验收条件**：全局矩阵、静态边界测试、灾难恢复演练通过；无 legacy read 指标。
- **测试证据**：全套相关 CI、调用图/rg 辅助证据、迁移验证、恢复报告。文本搜索只能辅助，不能单独证明无调用。
- **回滚点**：代码版本回滚 + 数据 forward-compatible 恢复；旧表删除后从最终归档或 reverse migration 恢复，故删除必须独立发布。
- **依赖**：阶段 6 稳定观察窗口及所有 owner 批准。

## 12. 全局验收矩阵

每个阶段按适用列执行；最终发布必须全部通过。`基线一致` 指用户可见契约一致，不指内部 wire 字节一致。

| 场景 | 必测行为 | 数据/技术断言 | 用户可见断言 |
|---|---|---|---|
| 主会话 | 普通/流式/多轮/附件/思考/Fast | 单 Run、上下文锚点正确、attempt 唯一 | 文本、顺序、思考规则、页脚/统计一致 |
| direct 子 Agent | 新建、持续 Agent session、消息/停止 | 同一 Runtime；allowlist；session 归属正确 | task 卡、通知、结果、artifact 一致 |
| managed 子 Agent | Plan 提交/批准/执行/replan/阻塞 | 版本 gate、step/evidence、冻结工具正确 | Plan/步骤/状态/按钮结果一致 |
| 旧会话续聊 | 主/direct/managed 真实样本 | transcript digest、summary/native anchor 正确，无重放 | 历史完整，继续回复语义正确 |
| 工具调用 | 单/并行表达、多轮、失败、超时、不可取消副作用 | call id/结果配对，授权不扩大，恢复不重复 | 工具行、参数摘要、进度、结果顺序一致 |
| 原生模型上下文 | Responses/Anthropic/普通 chat 可用样本 | identity 校验；opaque 不错配；降级有原因 | 回复连续，无内部 opaque 泄漏 |
| 账单 | 成功、retry、失败、取消、压缩、子 Agent 合并 | 每 attempt 一次；ledger 与 provider usage/费用规则一致 | token/费用/耗时展示不重复不倒退 |
| 取消/停止 | model stream、retry wait、工具前后、已终态后点击 | durable command 一次应用，不污染下一轮 | 停止状态、文案、按钮结果一致 |
| 暂停/恢复 | 安全点暂停、重启后恢复、多次点击 | checkpoint/CAS/lease 正确，无双跑 | 暂停/恢复状态和进度连续 |
| 通知 | 后台完成、失败、取消、父会话交互 | 去重键、root turn/task 归属正确 | 通知内容、时机、点击结果一致 |
| 前端可见内容 | 流式、刷新、重连、错误 | projection replay 与在线终态一致 | DOM/截图/行为 golden 无未批准差异 |

额外横切测试：provider 限流与指数退避、上下文超限压缩、空响应/仅 reasoning、进程在每个 checkpoint 前后崩溃、Event Sink 暂时失败、数据库 CAS 冲突、并发上限、旧/新前后端发布窗口。

## 13. 风险与治理决策

| 风险 | 严重度 | 强制控制 |
|---|---|---|
| 工具在恢复时重复执行 | 极高 | 稳定 call id、工具幂等 ledger、安全点、未知结果不自动重跑 |
| 账单重复或漏记 | 极高 | attempt 级唯一键；单 accounting sink；aggregate 只投影不再入账 |
| opaque native context 错配 | 高 | 完整 identity+anchor 校验；失败显式降级，不猜测转换 |
| Plan gate 被通用 Runtime 绕过 | 高 | 工具前强制 Policy；approved version snapshot；架构/故障测试 |
| pause/cancel 与并发 lease 竞态 | 高 | durable command cursor、CAS、明确安全点和 lease ownership |
| UI 异步顺序变化 | 高 | projection sequence、golden、刷新重放测试 |
| 双写分叉 | 高 | cohort 单真源；shadow 无副作用；migration ledger 与对账 |
| mode flag 巨型类重现 | 高 | ports 组合；依赖规则；评审拒绝 owner-kind 分叉核心算法 |
| 旧会话字段迁了但语义错 | 高 | canonical digest + 真实续聊，不只做行数校验 |

### 13.1 必须形成 ADR 的治理决策

1. Runtime 状态机与安全点。
2. canonical message/native context 的版本和降级规则。
3. attempt/tool/accounting 幂等键。
4. Plan gate 在 Runtime 调用链中的不可绕过位置。
5. migration routing manifest 与切回协议。
6. 用户可见 projection 的 owner 和 golden 更新审批规则。

Golden 变化必须说明是 bug 修复还是产品变化；产品变化不属于本重构，需用户另行批准。

## 14. 代码所有权与防止重新双实现

建议所有权边界：

- `agent/runtime/*`：Runtime owner；不得依赖 Web、Rath DAO、Plan 实现。
- `agent/adapters/model|tool|context/*`：对应 provider/tool/context owner。
- `agent/supervisor/*`：任务、control、lease、Plan owner；不得自行调用 `LLMBackend.stream` 或执行工具。
- `agent/store/*` 与 migration：DB owner。
- `web_console/*projection*` 与前端 view store：Web owner；不得成为执行真源。
- accounting sink：计费 owner；其他模块只能提交 attempt usage。

强制架构约束：

- 生产代码只允许 Runtime 的 Model Adapter 调用底层模型 stream；静态测试维护 allowlist。
- 生产代码只允许 Runtime ToolPort 调用通用工具执行入口；Supervisor 只发控制/授权。
- 禁止以复制循环、继承后 override 大段循环、`if owner_kind == ...` 分叉核心算法实现新模式。
- Runtime public contract 必须有三种 entry adapter 的 contract test。
- 每次 provider/retry/native/usage 修复必须在共享 Runtime 测试中覆盖，不接受仅 Rath 或仅 Web 的同类补丁。
- `app/models/agent_runtime.py` 应重命名为配置解析语义名称（如 `agent_run_config.py`），避免“Runtime”概念歧义；具体时间在调用迁移时决定。

## 15. 推荐第一个实施包

**推荐包：阶段 0 的最小必要基线 + 阶段 1“共享物理模型请求执行器”，不碰工具循环、数据库和前端。**

边界：

1. 固化主/Rath 对等的普通回复、tool-call、retry、partial stream、取消、native continuation 与 attempt usage fixtures。
2. 定义 `ModelExecutionResult`、`ModelAttemptRecord` 和共享 `ModelRequestExecutor`，只统一一次物理模型请求的执行与结果聚合。
3. 让 `app/agent/loop.py` 与 `app/rath/single_agent.py::_call_model` 都消费该执行器；两边外层工具循环和生命周期暂时保持。
4. 不改 Tool Policy、Plan、数据库 schema、API payload、前端 store 或用户可见投影。
5. 在无副作用 fixture 中对比新旧 trace；涉及真实模型/工具时只走单一路径，避免重复计费或重复副作用。
6. main/direct/managed 的临时回退开关彼此独立，保证共享 executor 出现 provider 特异问题时可局部回切。

选择它的原因：这是当前两套实现最明确、最底层的重复 seam，能最早停止 provider、retry、stream/native、usage 修复继续双改；同时不提前锁死完整 Runtime、数据模型和 Plan 边界，回滚成本最低。

该包的验收不是“新增了公共函数”，而是：主/Rath 的物理模型请求实际经过同一执行器，代表性 provider/retry/native/accounting trace 与基线一致，每个 attempt 只记账一次，且外层主/子行为没有用户可见变化。完整 Runtime 与主会话 Adapter 在阶段 2 继续完成。

## 16. 发布与回滚策略

- 开关至少按 `main/direct/managed`、conversation/task cohort、reader/writer generation 分开；开关状态持久化并可审计。
- 顺序采用：离线 contract → shadow（无副作用）→ 内部 canary → 小 cohort → 分批扩大 → 稳定观察 → 下一阶段。
- 不在同一发布同时做“executor 切换、全量数据迁移、前端 wire 切换、旧表删除”。
- 每次扩大前检查：失败率、取消延迟、恢复成功率、事件投影差异、usage/cost 对账、legacy fallback、工具幂等冲突、用户可见 golden。
- 触发回滚的硬条件：工具重复/授权扩大、账单重复、历史错序/丢失、Plan 未批准执行、双跑、无法停止/恢复、可见终态错误。此类问题不得以继续扩大 cohort 来收集数据。
- 回滚优先按 cohort 路由，不做全库覆盖；涉及新写按第 10.5 节执行。
- legacy 删除是最后一个独立发布，且其回滚依赖最终归档与已演练 restore。

## 17. 文档维护与完成定义

本方案是实施总纲。每个阶段开始前应引用本方案建立阶段 ADR、数据映射和执行 runbook；阶段结束后把实际 commit、migration id、测试报告、canary cohort、差异批准和回滚演练链接补入长期文档，不得把“计划测试”改写成“已经通过”。

统一 Runtime 重构的最终完成定义：

1. 主、direct、managed 的生产模型/工具执行均调用同一 Runtime。
2. Rath 独立 executor 已删除，但 task/Plan/control/lease/session/artifact/notification 能力在 Supervisor 中保持。
3. 历史真实样本可正确续聊，迁移/恢复/账单校验通过。
4. 全局验收矩阵的用户可见结果与基线一致。
5. legacy reader/旧表按门槛退役，且新备份可独立恢复。
6. 架构测试能阻止第二套模型/工具循环重新进入生产代码。

在这些证据实际产生前，只能表述为“方案已完成”，不能表述为“重构已完成”。

## 附录 A：源码依据索引

- `app/agent/loop.py`：主执行循环；现有 Renderer/Persister/Compactor ports、工具循环、native context、usage、retry/cancel。
- `app/web_console/chat_runtime.py`：主会话上下文、模型配置、Task Memory、账单、压缩、Web 投影与收尾编排。
- `app/rath/single_agent.py`：当前独立子 Agent 模型/工具循环及 Plan/control/context/accounting 混合职责。
- `app/tools/agents.py`：Agent 工具编排、子任务 usage 合并与 ledger 交互。
- `app/rath/manager.py`：durable control、内存取消、并发 ExecutionLease、pause/resume/stop/steer。
- `app/rath/plan.py`：Plan version/state/step/evidence/decision 与控制协调。
- `app/rath/dao.py`：Rath task/session/event/artifact/control/model context 持久化。
- `app/models/agent_runtime.py`：Agent model/think/Fast 解析与冻结快照（不是执行 Runtime）。
- `app/db/schema.sql`、`app/db/schema_migrations.py`：当前数据库 schema 与 migration 权威入口。

## 附录 B：实施检查清单

- [ ] 本阶段没有扩大用户可见产品范围。
- [ ] Runtime 内无 Web/Rath/DAO 反向依赖。
- [ ] 物理 model attempt 和 tool call 均有稳定幂等键。
- [ ] durable control 与内存 wake-up 的职责清楚。
- [ ] native context identity/anchor 校验覆盖迁移和恢复。
- [ ] 账单只有一个写入真源，shadow 不入账。
- [ ] UI 比较包含 DOM、行为、顺序、截图和刷新/重连。
- [ ] 真实旧会话做了首次续聊，不只验证能打开。
- [ ] migration 可重跑、可断点、可对账、可恢复。
- [ ] feature flag 能按 cohort 切回，且没有同一 run 双跑。
- [ ] 实际测试证据已链接；未执行项仍明确标为未执行。

## 附录 C：用户要求追踪矩阵

本矩阵用于防止方案在实施过程中偏离 2026-08-26 的实际沟通；如后续要求更新，以用户最新明确决定修订本表和相关章节。

| 用户要求/修正 | 正式解释 | 文档落点 | 最终验收证据 |
|---|---|---|---|
| 主 Agent 与 Rath 是两套执行实现，修改能力要改两套 | 删除重复模型/工具执行内核，生产只保留一个 `AgentRuntime` | §1、§3、§5、阶段 1/2/4/5/7、§14 | 主/direct/managed 均调用同一 Runtime；静态边界测试阻止第二循环 |
| 从主 Agent 抽出完整执行内核并兼容主/子 Agent | 抽取的是协议无关内核，不是复制 Web 主会话壳 | §5、§6、阶段 2、§17 | 三类 Adapter contract test；Runtime 不依赖 Web/Rath DAO/Plan 实现 |
| 尽量解耦，避免高耦合导致改动困难 | Runtime、Adapter、Policy、Store、Event Sink、Supervisor、Projection 分责 | §4～§8、§14 | 依赖图、import 规则和职责测试通过；核心无 owner-kind 算法分叉 |
| 历史会话必须继续续聊 | 硬约束是迁移后的语义连续性，不是旧数据结构不可改 | §2.3C、§9～§10、阶段 6、全局矩阵 | 真实旧会话迁移后可打开、首次续聊、刷新和再次续聊；无历史工具/账单重放 |
| 允许转换、调整甚至必要的破坏性数据操作 | 可迁表、改 schema、替换 legacy；执行时须另获授权并具备备份、校验和回滚 | §9～§10、阶段 3/6/7、§16 | dry-run、备份恢复演练、migration ledger、前后 digest、reverse/forward 校验 |
| 前后端内部结构可以改变 | API、payload、事件、前端 store 和后端 DTO 均属内部可变结构 | §2.3B、§8、阶段 3/6 | 新结构的 contract/集成测试通过；不要求旧 wire 永久兼容 |
| 用户实际看到的展现内容不能改变 | 页面消息、文案、顺序、工具/Plan/任务卡片、统计、错误/停止和操作结果保持 | §2.3A、§8.3、阶段 0、全局矩阵 | UI view-model/DOM/行为 golden 为主，截图辅助；刷新和重连结果一致 |
| 要有完整方案、阶段目标及逐项目验收条件 | 每阶段都包含目标、范围、工作、不做项、交付物、进入/完成/验收、证据、回滚和依赖 | §11、§12、§15～§17 | 阶段未具备证据不得标记完成；总完成定义六项全部满足 |
| 当前只要求写方案文档 | 文档完成不等于重构完成，也不授权代码、迁移、部署或重启 | 文首、§2.2、§17 | `@doc` 已写入并复读核对；实施动作另行决定和授权 |
