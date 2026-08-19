<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";

const loading = ref(false);
const agents = ref([]);
const options = ref({ models: [], tools: [], thinkLevels: ["", "off", "low", "medium", "high"], currentModel: "", primaryModel: "" });
const drawerOpen = ref(false);
const editing = ref(null);
const showDisabled = ref(true);

const AGENT_PROMPT_PRESETS = [
  {
    key: "general",
    label: "通用专业 Agent",
    prompt: `你是 OpenBear 后台 Agent preset 中的专业执行 Agent。你只负责 OpenBear 分配给你的子任务，不负责最终对用户发言。

工作方式：
1. 先理解任务目标、上下文、边界和输出要求。
2. 必要时使用你被授权的工具收集证据；没有授权工具时不要声称已读取/执行。
3. 输出中文 Markdown，结构为：结论、关键依据、风险/不确定性、建议下一步。
4. 不要把问题扩大成全量审计；聚焦当前子任务。
5. 如果信息不足，列出缺口和你建议 OpenBear 追问或补充的内容。
6. 不要执行破坏性操作；如任务需要删除、覆盖、公开发送、危险命令，先要求确认。

你的输出会被 OpenBear 汇总，请保持清晰、可引用、不过度冗长。`,
  },
  {
    key: "developer",
    label: "项目开发 Agent",
    prompt: `你是 OpenBear 后台 Agent preset 中的项目开发专家。你只负责“实现/修改/实现侧分析”这一类子任务，输出给 OpenBear 汇总，不直接对最终用户发言，也不能调用其他 Agent。

## 核心职责
1. 理解需求、现有代码结构和约束，定位最小可行改动。
2. 当 instruction 明确授权修改且工具权限允许时，完成小范围代码实现。
3. 当 instruction 是只读任务时，只做实现侧分析和修改建议。
4. 交付给 OpenBear 的内容必须能让测试验证专家接手：改了什么、为什么、影响面、建议验证什么。

## 严格边界
- 不负责测试验证、构建验收、上线结论、质量盖章。
- 不运行 pytest、lint、build、typecheck、端到端测试或“检查是否还有旧字段残留”这类验证任务；这些属于测试验证专家。
- 不检查构建产物是否同步、不把 grep 全仓残留当成验收报告；除非它是实现定位所必需的源码查找。
- 不修改测试文件，除非 instruction 明确要求你补测试；一般测试文件交给测试验证专家。
- 不重启、发布、删除、改权限、外部发送、破坏性数据库操作，除非 instruction 明确授权。
- 如果 OpenBear 分配给你的 instruction 实际是“验证/测试/验收”任务，应明确说明该任务不属于开发专家，并只给实现侧可交接信息；不要越权完成测试专家工作。

## 工作流程
1. 读清 instruction：确认允许修改的目录/文件、禁止事项和交付格式。
2. 定位相关源码：用 Read/Glob/Grep 查最小必要范围。
3. 设计最小改动：只选一条你判断最稳的实现路径。
4. 如已授权修改：用 Edit/Write 做最小实现，不做无关重构。
5. 自检代码一致性：只做静态阅读级自检，不运行测试/构建来证明通过。
6. 输出交接：列出修改文件、核心逻辑、影响面、测试专家应验证的场景。

## 工具使用规则
- 没读到代码就不要断言实现。
- 未实际修改就不要说“已实现/已修复”。
- 未实际运行测试就不要说“测试通过”；原则上你不应该运行测试。
- 使用 Bash 时仅限安全的源码定位/查看辅助命令；不要用 Bash 做测试、构建或验收。

## 输出格式
- 开发侧结论：已实现 / 可实现但未修改 / 有阻断 / 任务越界。
- 已修改文件：逐项说明；未修改则写“无”。
- 实现依据：文件路径 + 函数/类/关键逻辑。
- 影响面与风险：兼容性、边界、数据、安全。
- 交给测试验证专家：必须验证的命令/场景清单，但不要声称已验证。

## 质量标准
- 角色纯粹：实现归实现，验证归验证。
- 改动最小、可回退、可解释。
- 不把“做了很多检查”当成开发成果。`,
  },
  {
    key: "tester",
    label: "测试验证 Agent",
    prompt: `你是 OpenBear 后台 Agent preset 中的测试验证专家。你只负责“验证/测试/构建/失败归因/覆盖风险”这一类子任务，输出给 OpenBear 汇总，不直接对最终用户发言，也不能调用其他 Agent。

## 核心职责
1. 根据需求、开发专家交接、代码 diff 或指定文件，判断变更是否被足够验证。
2. 在 instruction 允许时运行非破坏性测试/lint/build/typecheck/静态检查。
3. 区分代码失败、环境失败、依赖缺失、配置缺失、外部服务不可用和测试假设错误。
4. 列出覆盖范围和未覆盖风险，避免“跑了一个小测试就说稳了”。

## 严格边界
- 不写业务代码，不修实现，不替开发专家改源码。
- 不做需求设计或实现方案主导；实现问题只给最小复现、失败证据和建议交回开发专家。
- 不重启线上服务、不发布、不删除数据、不改配置，除非 instruction 明确授权。
- 如果 instruction 实际要求你实现功能，应说明任务越界，并建议交给项目开发专家；不要越权写代码。

## 工作流程
1. 阅读任务目标、开发交接、相关源码和现有测试结构。
2. 先验证主路径，再补边界/回归；范围由 instruction 和变更影响决定。
3. 运行允许范围内的命令；记录命令、cwd、结果摘要和关键错误。
4. 失败时先归因：实现缺陷 / 测试假设 / 环境依赖 / 配置问题 / 外部服务。
5. 输出可复现证据和未覆盖项。

## 工具使用规则
- 只有实际执行命令后，才能写“已执行/已验证/通过”。
- 命令失败时保留关键错误，不贴无关长日志。
- 不修改源码；如果需要临时文件或测试夹具，必须在 instruction 授权的临时目录内操作并说明。

## 输出格式
- 验证结论：通过 / 失败 / 未执行 / 无法判断 / 任务越界。
- 已执行命令：命令 + cwd + 结果摘要。
- 覆盖范围：主路径、边界、回归、lint/build/typecheck。
- 失败或风险分析：证据和归因。
- 未覆盖项。
- 给 OpenBear 的下一步建议。

## 质量标准
- “通过”只能对应实际执行过且成功的命令/检查。
- 必须写未覆盖范围。
- 不把环境问题误判为业务失败。
- 角色纯粹：验证归验证，修代码交回开发专家。`,
  },
  {
    key: "reviewer",
    label: "代码审查 Agent",
    prompt: `你是 OpenBear 后台 Agent preset 中的代码审查专家，擅长从正确性、可维护性、安全性、边界条件和回归风险角度审查实现方案或代码变更。

职责：
1. 先理解变更目标，不做无关风格挑刺。
2. 重点找会导致错误行为、数据损坏、安全风险、并发问题、兼容性问题的缺陷。
3. 如果证据不足，不要臆断；明确标注“需要进一步验证”。
4. 给出可执行的修复建议，不只说“这里可能有问题”。
5. 不重复开发 Agent 的工作；你负责审查和风险识别。

输出格式：
- 审查结论：可接受/需修改/需要更多信息。
- 高风险问题：按严重程度列出。
- 中低风险问题：简短列出。
- 缺失测试：建议补哪些测试。
- 最小修复建议。`,
  },
  {
    key: "researcher",
    label: "资料调研 Agent",
    prompt: `你是 OpenBear 后台 Agent preset 中的资料调研专家，擅长搜索、阅读、筛选和交叉验证公开资料。

职责：
1. 先拆解调研问题，明确关键词、范围、时间敏感性和判断标准。
2. 如果有 WebSearch，先找多来源；如果有 WebExtract，深读关键来源。
3. 优先权威来源、官方文档、原始材料；对二手资料保持谨慎。
4. 对冲突信息要明确指出，不强行合并成单一结论。
5. 不输出无法追溯的断言；重要结论附来源名称/URL。

输出格式：
- 结论摘要。
- 关键证据：来源 + 要点。
- 冲突/不确定性。
- 可采信程度。
- 建议 OpenBear 如何向用户表达。`,
  },
  {
    key: "longform",
    label: "长文/小说分析 Agent",
    prompt: `你是 OpenBear 后台 Agent preset 中的长文与小说分析专家，擅长处理小说、长篇文章、故事设定、人物关系、情节线和文本风格。

职责：
1. 先确认分析目标：剧情梳理、人物关系、设定整理、主题分析、续写建议、风格模仿或问题回答。
2. 基于文本证据回答，不把没出现的信息当事实。
3. 长文本要分层处理：主线、人物、设定、冲突、伏笔、时间线。
4. 如果用户持续追问同一作品，保持前后称谓、设定和结论一致。
5. 涉及续写/改写时，先保持原作设定和风格，再做创作。

输出格式：
- 直接回答用户问题。
- 相关文本依据或情节依据。
- 人物/设定/时间线补充。
- 不确定点和可能解释。`,
  },
];


const visibleAgents = computed(() => agents.value);
const enabledAgents = computed(() => visibleAgents.value.filter((a) => !!a.enabled));

function items(data) { return Array.isArray(data?.items) ? data.items : []; }
function okOrThrow(data) { if (data?.ok === false) throw new Error(data.error || "操作失败"); return data; }
function fmtTime(ts) { return ts ? new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false }) : "—"; }
function toolText(row) {
  const tools = Array.isArray(row?.tool_allowlist) ? row.tool_allowlist : [];
  return tools.length ? tools.join(", ") : "无工具";
}
function normalizeTools(value) {
  if (Array.isArray(value)) return value.map((x) => String(x || "").trim()).filter(Boolean);
  return String(value || "").replace(/，/g, ",").split(",").map((x) => x.trim()).filter(Boolean);
}
function modelLabel(key) {
  if (!key) return "跟随当前模型";
  const item = options.value.models?.find((m) => m.key === key);
  const tags = [item?.reasoning ? "reasoning" : "", item?.supportsFast ? "Fast" : ""].filter(Boolean).join(" · ");
  return item ? `${item.key}${tags ? ` · ${tags}` : ""}` : key;
}
const editingModelInfo = computed(() => {
  const key = editing.value?.model || options.value.currentModel || options.value.primaryModel || "";
  return options.value.models?.find((m) => m.key === key) || null;
});
const editingThinkLevels = computed(() => Array.isArray(editingModelInfo.value?.thinkingLevels) ? editingModelInfo.value.thinkingLevels.filter(Boolean) : []);
const editingDefaultThinkLevel = computed(() => {
  const levels = editingThinkLevels.value;
  if (!levels.length) return "";
  const configured = editingModelInfo.value?.defaultThinkingLevel || "";
  return levels.includes(configured) ? configured : levels[levels.length - 1];
});
function syncEditingModelCapabilities({ resetThinking = false } = {}) {
  if (!editing.value) return;
  const levels = editingThinkLevels.value;
  if (!levels.length) {
    editing.value.thinkLevel = "";
  } else if (resetThinking || !levels.includes(editing.value.thinkLevel)) {
    editing.value.thinkLevel = editingDefaultThinkLevel.value;
  }
}
function onEditingModelChanged() {
  syncEditingModelCapabilities({ resetThinking: true });
}
function defaultSystemPrompt() {
  return AGENT_PROMPT_PRESETS[0].prompt;
}
function applyPromptPreset(key) {
  if (!editing.value) return;
  const preset = AGENT_PROMPT_PRESETS.find((x) => x.key === key);
  if (preset) editing.value.systemPrompt = preset.prompt;
}
async function run(action, success = "已完成") {
  loading.value = true;
  try {
    const ret = await action();
    if (success) ElMessage.success(success);
    return ret;
  } catch (error) {
    ElMessage.error(apiError(error));
    throw error;
  } finally {
    loading.value = false;
  }
}

async function load() {
  loading.value = true;
  try {
    const [opt, ag] = await Promise.all([
      Api.rathOptions(),
      Api.rathAgents({ disabled: showDisabled.value ? 1 : 0 }),
    ]);
    options.value = { ...options.value, ...(opt || {}) };
    agents.value = items(ag);
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    loading.value = false;
  }
}

function openEdit(row = null) {
  editing.value = row ? {
    ...row,
    agentKey: row.agent_key || row.agentKey || "",
    systemPrompt: row.system_prompt || defaultSystemPrompt(),
    thinkLevel: row.think_level,
    toolAllowlist: Array.isArray(row.tool_allowlist) ? [...row.tool_allowlist] : [],
  } : {
    id: 0,
    name: "",
    agentKey: "",
    description: "",
    systemPrompt: defaultSystemPrompt(),
    model: "",
    thinkLevel: "",
    toolAllowlist: [],
    enabled: true,
  };
  syncEditingModelCapabilities({ resetThinking: false });
  drawerOpen.value = true;
}

function payload() {
  return {
    name: editing.value.name,
    agentKey: editing.value.agentKey,
    description: editing.value.description,
    systemPrompt: editing.value.systemPrompt || defaultSystemPrompt(),
    model: editing.value.model,
    thinkLevel: editing.value.thinkLevel,
    toolAllowlist: normalizeTools(editing.value.toolAllowlist),
    enabled: !!editing.value.enabled,
  };
}

async function save() {
  if (!editing.value?.name?.trim()) {
    ElMessage.warning("Agent 名称不能为空");
    return;
  }
  await run(async () => {
    const data = payload();
    if (editing.value.id) okOrThrow(await Api.updateRathAgent(editing.value.id, data));
    else okOrThrow(await Api.createRathAgent(data));
    drawerOpen.value = false;
    await load();
  }, "Agent 已保存");
}

async function remove(row) {
  await ElMessageBox.confirm(`删除 Agent「${row.name}」？历史任务不会删除。`, "确认删除", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  await run(async () => {
    okOrThrow(await Api.deleteRathAgent(row.id));
    await load();
  }, "Agent 已删除");
}

async function toggle(row) {
  await run(async () => {
    okOrThrow(await Api.updateRathAgent(row.id, { enabled: !row.enabled }));
    await load();
  }, row.enabled ? "已停用" : "已启用");
}

async function trial(row) {
  const { value } = await ElMessageBox.prompt(
    `输入要交给「${row.name}」试运行的任务`,
    "试运行 Agent",
    {
      inputType: "textarea",
      inputPlaceholder: "例如：请用三句话介绍你的能力和适用场景",
      confirmButtonText: "启动试运行",
      cancelButtonText: "取消",
      inputValidator: (v) => String(v || "").trim() ? true : "任务不能为空",
    },
  );
  await run(async () => {
    const ret = okOrThrow(await Api.trialRathAgent(row.id, String(value || "").trim()));
    ElMessage.success(`试运行已启动：${ret.taskUuid}`);
    await load();
  }, "");
}

onMounted(load);
</script>

<template>
  <div class="h-full flex flex-col" v-loading="loading">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="flex items-center gap-2">
        <h1 class="text-base font-semibold">Agent Presets</h1>
        <span class="text-xs text-macsub">system prompt 与适用场景</span>
      </div>
      <div class="flex items-center gap-2">
        <el-checkbox v-model="showDisabled" size="small" @change="load">显示停用</el-checkbox>
        <el-button :icon="'Refresh'" circle @click="load" title="刷新" />
        <el-button type="primary" :icon="'Plus'" round @click="openEdit(null)">新建 Preset</el-button>
      </div>
    </header>

    <div class="grid grid-cols-1 md:grid-cols-4 gap-3 px-6 pt-5 shrink-0">
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">Preset 总数</div><div class="text-lg font-semibold">{{ visibleAgents.length }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">当前显示</div><div class="text-lg font-semibold">{{ visibleAgents.length }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">启用中</div><div class="text-lg font-semibold">{{ enabledAgents.length }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">默认 Worker</div><div class="text-lg font-semibold">general-purpose</div></div>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto p-6">
      <div v-if="!visibleAgents.length" class="text-center text-macsub py-16 text-sm">暂无 Preset</div>
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <article v-for="row in visibleAgents" :key="row.id" class="mac-panel mac-shadow p-4">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <el-tag :type="row.enabled ? 'success' : 'info'" round>{{ row.enabled ? '✅ 启用' : '停用' }}</el-tag>
                <h2 class="font-semibold text-[15px] truncate">{{ row.name }}</h2>
                <span class="text-[11px] text-macsub truncate">{{ row.agent_key }}</span>
              </div>
              <p class="mt-2 text-sm text-mactext/80 line-clamp-2">{{ row.description || '暂无描述' }}</p>
              <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-macsub">
                <span class="px-2 py-0.5 rounded-full bg-black/[0.04]">🤖 {{ modelLabel(row.model) }}</span>
                <span class="px-2 py-0.5 rounded-full bg-black/[0.04]">🧠 {{ row.think_level || '模型默认' }}</span>
                <span class="px-2 py-0.5 rounded-full bg-black/[0.04]">🛠 {{ toolText(row) }}</span>
                <span class="px-2 py-0.5 rounded-full bg-black/[0.04]">{{ fmtTime(row.updated_at) }}</span>
              </div>
            </div>
            <div class="flex gap-1 shrink-0">
              <el-button size="small" text type="success" :disabled="!row.enabled" @click="trial(row)">试运行</el-button>
              <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
              <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
            </div>
          </div>
        </article>
      </div>
    </div>

    <el-drawer v-model="drawerOpen" size="72%" :title="editing?.id ? '编辑 Preset' : '新建 Preset'">
      <template v-if="editing">
        <div class="h-full flex flex-col min-h-0 gap-4">
          <section class="mac-panel p-4 grid grid-cols-1 md:grid-cols-4 gap-3 shrink-0">
            <div><label class="text-xs text-macsub mb-1 block">名称</label><el-input v-model="editing.name" placeholder="如 深度调研员" /></div>
            <div><label class="text-xs text-macsub mb-1 block">Key</label><el-input v-model="editing.agentKey" placeholder="researcher" /></div>
            <div>
              <label class="text-xs text-macsub mb-1 block">模型</label>
              <el-select v-model="editing.model" clearable filterable allow-create default-first-option class="w-full" placeholder="留空=跟随 OpenBear 当前模型" @change="onEditingModelChanged">
                <el-option label="跟随当前模型" value="" />
                <el-option v-for="m in options.models" :key="m.key" :label="`${m.key}${m.primary ? ' · 主模型' : ''}${m.reasoning ? ' · reasoning' : ''}${m.supportsFast ? ' · Fast' : ''}`" :value="m.key" />
              </el-select>
            </div>
            <div><label class="text-xs text-macsub mb-1 block">思考模式</label><el-select v-model="editing.thinkLevel" clearable class="w-full" :disabled="!editingThinkLevels.length" :placeholder="editingThinkLevels.length ? `默认=${editingDefaultThinkLevel || '模型默认'}` : '该模型未配置思考强度'"><el-option :label="`模型默认（${editingDefaultThinkLevel || 'off'}）`" value="" /><el-option v-for="lv in editingThinkLevels" :key="lv" :label="lv" :value="lv" /></el-select></div>
            <div class="md:col-span-4"><label class="text-xs text-macsub mb-1 block">适用场景</label><el-input v-model="editing.description" type="textarea" :rows="3" /></div>
            <div class="md:col-span-4">
              <label class="text-xs text-macsub mb-1 block">授权工具</label>
              <el-select v-model="editing.toolAllowlist" multiple filterable class="w-full" placeholder="选择授权工具；留空=无工具">
                <el-option v-for="t in options.tools" :key="t.name" :label="t.name" :value="t.name" />
              </el-select>
              <div class="text-[11px] text-macsub mt-1">留空表示不授权任何工具；只能从固定 Agent 白名单中选择。</div>
            </div>
            <div class="md:col-span-4 flex items-center gap-5"><el-switch v-model="editing.enabled" active-text="启用" inactive-text="停用" /></div>
          </section>

          <section class="grid grid-cols-1 lg:grid-cols-[1fr_240px] gap-4 flex-1 min-h-0">
            <div class="mac-panel p-4 flex flex-col min-h-0">
              <div class="flex items-center justify-between gap-3 mb-2">
                <label class="text-xs text-macsub block">System Prompt</label>
                <el-select size="small" clearable placeholder="套用模板" class="w-44" @change="applyPromptPreset">
                  <el-option v-for="preset in AGENT_PROMPT_PRESETS" :key="preset.key" :label="preset.label" :value="preset.key" />
                </el-select>
              </div>
              <el-input v-model="editing.systemPrompt" type="textarea" resize="none" class="flex-1 agent-textarea" placeholder="定义这个 Agent 的角色、工作方式、输出格式和边界" />
            </div>
            <div class="mac-panel p-4 overflow-y-auto text-xs text-macsub leading-relaxed">
              <label class="text-xs text-macsub mb-3 block">调用方式</label>
              <pre class="whitespace-pre-wrap rounded border border-macborder bg-macbg/70 p-3 text-[11px] text-mactext">Agent({
  workerType: "{{ editing.agentKey || 'general-purpose' }}",
  prompt: "...",
  tools: {{ JSON.stringify(normalizeTools(editing.toolAllowlist)) }}
})</pre>
            </div>
          </section>

          <footer class="shrink-0 flex justify-end gap-2 border-t border-macborder pt-3">
            <el-button @click="drawerOpen = false">取消</el-button>
            <el-button type="primary" @click="save">保存</el-button>
          </footer>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
:deep(.agent-textarea), :deep(.agent-textarea .el-textarea__inner) {
  height: 100%;
  min-height: 260px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 12px;
}

</style>
