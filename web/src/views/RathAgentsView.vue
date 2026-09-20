<script setup>
import MobileAdminSummary from "../components/MobileAdminSummary.vue";
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";

const loading = ref(false);
const agents = ref([]);
const options = ref({ models: [], tools: [], thinkLevels: ["", "off", "low", "medium", "high"], currentModel: "", primaryModel: "" });
const drawerOpen = ref(false);
const editing = ref(null);
const showDisabled = ref(true);
const saving = ref(false);
const editingBaseline = ref(null);
const optionsLoading = ref(false);
let optionsRequest = 0;
const toolGroups = computed(() => {
  const groups = new Map();
  for (const tool of options.value.tools || []) {
    const label = tool.kind === "mcp" ? `MCP · ${tool.serverKey}` : "内置工具";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(tool);
  }
  return [...groups].map(([label, tools]) => ({ label, tools }));
});
const unavailableSelected = computed(() => normalizeTools(editing.value?.toolAllowlist).filter(name => !(options.value.tools || []).some(tool => tool.name === name)));
async function loadOptions() {
  const request = ++optionsRequest;
  optionsLoading.value = true;
  try {
    const data = okOrThrow(await Api.rathOptions());
    if (request === optionsRequest) options.value = { ...options.value, ...data };
  } catch {
    if (request === optionsRequest) ElMessage.warning("工具目录刷新失败，已保留当前选择。");
  } finally { if (request === optionsRequest) optionsLoading.value = false; }
}

const AGENT_PROMPT_PRESETS = [
  {
    key: "general",
    label: "通用专业 Agent",
    prompt: `你是 OpenBear 主控注册的 Agent 预设。预设只补充这个角色的专业能力与领域内的注意事项；工作方式、范围边界、授权、工作记忆和交接方式由你的基础 system prompt 统一规定，这里不再重复。

真实任务目标始终来自每次指派的任务消息，不来自本预设。按任务要求的深度完成指定的结果并交回 OpenBear，不套用固定的工作流程或报告栏目。本预设未定义额外专长时，按通用能力处理任务。`,
  },
  {
    key: "developer",
    label: "项目开发 Agent",
    prompt: `你是 OpenBear 的项目开发预设：擅长理解现有代码结构、定位最小可行改动、实现并自测。

专业注意事项：
- 先读清相关源码的实际实现和调用路径，再改；不凭文件名或注释猜测行为。
- 只做任务要求的最小改动，不顺手重构、不改无关文件；保留原有行为、默认值和接口约定。
- 实现完成后用任务授权范围内的方式验证（运行相关测试、复现场景、静态核对），并如实区分“已验证”与“未验证”。
- 修改前先理解已有测试对该行为的约定；需要补测试时紧贴改动点。
- 交接时写清改了哪些文件、为什么这样改、影响面和验证结果。`,
  },
  {
    key: "tester",
    label: "测试验证 Agent",
    prompt: `你是 OpenBear 的测试验证预设：擅长判断一项变更是否被足够验证，运行测试与检查，并对失败做准确归因。

专业注意事项：
- 验证围绕任务指定的变更或行为，不把范围扩大成全仓审计；需要全量回归时以任务明确要求为准。
- 先验证主路径，再按变更影响补边界与回归；记录实际执行的命令、目录和结果要点。
- 失败先归因：实现缺陷、测试假设错误、环境或依赖问题、外部服务不可用，不要混为一谈。
- “通过”只对应实际执行且成功的检查；同时写明本次没有覆盖到什么。
- 不贴无关长日志，保留能定位问题的关键错误。`,
  },
  {
    key: "reviewer",
    label: "代码审查 Agent",
    prompt: `你是 OpenBear 的代码审查预设：从正确性、边界条件、并发、数据安全、兼容性和回归风险角度审查实现或变更。

专业注意事项：
- 先理解变更目标和任务给出的具体审查问题，围绕它们审；不做无关风格挑刺。
- 优先找会导致错误行为、数据损坏、安全风险或回归的缺陷，并给出准确位置和触发条件。
- 证据不足时明确标注，不臆断；能最小复现时给出复现方式。
- 修复建议具体可执行，不只说“这里可能有问题”。
- 邻近但不影响本次审查结论的问题最多简要提及，不展开成新的审查。`,
  },
  {
    key: "researcher",
    label: "资料调研 Agent",
    prompt: `你是 OpenBear 的资料调研预设：擅长搜索、阅读、筛选和交叉验证公开资料。

专业注意事项：
- 先明确调研问题的关键词、范围、时间敏感性和判断标准，再检索。
- 优先权威来源、官方文档和原始材料；对二手转述保持谨慎并注明。
- 来源之间有冲突时如实指出，不强行合并成单一结论。
- 重要结论附来源名称或 URL；无法追溯的断言不写成事实。
- 证据足以回答任务问题时收口，不为凑来源继续追加。`,
  },
  {
    key: "longform",
    label: "长文/小说分析 Agent",
    prompt: `你是 OpenBear 的长文与小说分析预设：擅长处理小说、长篇文章、故事设定、人物关系、情节线和文本风格。

专业注意事项：
- 先确认分析目标：剧情梳理、人物关系、设定整理、主题分析、续写建议、风格模仿或具体问题。
- 基于文本证据回答，引用相关情节或原文依据；没有出现的信息不当作事实。
- 长文本分层处理：主线、人物、设定、冲突、伏笔、时间线。
- 同一作品的多轮任务保持称谓、设定和结论一致。
- 续写或改写先保持原作设定与风格，再做创作。`,
  },
];


const visibleAgents = computed(() => agents.value);
const enabledAgents = computed(() => visibleAgents.value.filter((a) => !!a.enabled));

function items(data) { return Array.isArray(data?.items) ? data.items : []; }
function okOrThrow(data) { if (data?.ok === false) throw new Error(data.error || "操作失败"); return data; }
function fmtTime(ts) { return ts ? new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false }) : "—"; }
function toolText(row) {
  const tools = Array.isArray(row?.tool_allowlist) ? row.tool_allowlist : [];
  return tools.length ? tools.join(", ") : "不附加预设工具限制";
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
    const [, ag] = await Promise.all([
      loadOptions(),
      Api.rathAgents({ disabled: showDisabled.value ? 1 : 0 }),
    ]);
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
  editingBaseline.value = row ? fullPayload() : null;
  drawerOpen.value = true;
  void loadOptions();
}

function fullPayload() {
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

function payload() {
  const current = fullPayload();
  if (!editingBaseline.value) return current;
  const changed = Object.fromEntries(Object.entries(current).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(editingBaseline.value[key])));
  if ("toolAllowlist" in changed) changed.expectedToolAllowlist = [...editingBaseline.value.toolAllowlist];
  return changed;
}

async function save() {
  if (saving.value) return;
  if (!editing.value?.name?.trim()) {
    ElMessage.warning("Agent 名称不能为空");
    return;
  }
  saving.value = true;
  try {
    const data = payload();
    if (editing.value.id) okOrThrow(await Api.updateRathAgent(editing.value.id, data));
    else okOrThrow(await Api.createRathAgent(data));
    drawerOpen.value = false;
    ElMessage.success("Agent 已保存");
    await load();
  } catch (error) {
    const code = error?.response?.data?.error;
    ElMessage.error(code === "agent_tool_allowlist_conflict" ? "工具上限已被其他操作修改，草稿未覆盖，请重新打开预设后确认。" : code === "agent_tool_not_available" ? "有新增工具当前不可委派，选择已保留，请刷新目录后确认。" : apiError(error));
    await loadOptions();
  } finally { saving.value = false; }
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
  <div class="admin-page agents-page h-full flex flex-col" v-loading="loading">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="admin-heading flex items-center gap-2">
        <h1 class="text-base font-semibold">Agent Presets</h1>
        <span class="text-xs text-macsub">system prompt 与适用场景</span>
      </div>
      <div class="flex items-center gap-2">
        <el-checkbox v-model="showDisabled" size="small" @change="load">显示停用</el-checkbox>
        <el-button :icon="'Refresh'" circle @click="load" title="刷新" />
        <el-button type="primary" :icon="'Plus'" round @click="openEdit(null)">新建 Preset</el-button>
      </div>
    </header>

    <div class="admin-desktop-only admin-stats grid grid-cols-1 md:grid-cols-4 gap-3 px-6 pt-5 shrink-0">
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">Preset 总数</div><div class="text-lg font-semibold">{{ visibleAgents.length }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">当前显示</div><div class="text-lg font-semibold">{{ visibleAgents.length }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">启用中</div><div class="text-lg font-semibold">{{ enabledAgents.length }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">默认 Worker</div><div class="text-lg font-semibold">general-purpose</div></div>
    </div>

    <MobileAdminSummary :items="[{ label: 'Preset 总数', value: visibleAgents.length }, { label: '当前显示', value: visibleAgents.length }, { label: '启用中', value: enabledAgents.length }, { label: '默认 Worker', value: 'general-purpose' }]">当前 {{ visibleAgents.length }} · 启用 {{ enabledAgents.length }}</MobileAdminSummary>

    <div class="admin-list flex-1 min-h-0 overflow-y-auto p-6">
      <div v-if="!visibleAgents.length" class="text-center text-macsub py-16 text-sm">暂无 Preset</div>
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <article v-for="row in visibleAgents" :key="row.id" class="agent-card mac-panel mac-shadow p-4">
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
            <div class="admin-card-actions flex gap-1 shrink-0">
              <el-button size="small" text type="success" :disabled="!row.enabled" @click="trial(row)">试运行</el-button>
              <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
              <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
            </div>
          </div>
        </article>
      </div>
    </div>

    <el-drawer append-to-body class="admin-drawer agent-drawer" v-model="drawerOpen" size="72%" :title="editing?.id ? '编辑 Preset' : '新建 Preset'">
      <template v-if="editing">
        <div class="agent-edit-body h-full flex flex-col min-h-0 gap-4">
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
              <label class="text-xs text-macsub mb-1 block">工具上限</label>
              <el-select v-model="editing.toolAllowlist" multiple filterable class="w-full" :loading="optionsLoading" :disabled="saving" placeholder="选择工具上限；留空不附加预设限制" @visible-change="(visible) => { if (visible) loadOptions(); }">
                <el-option-group v-for="group in toolGroups" :key="group.label" :label="group.label">
                  <el-option v-for="t in group.tools" :key="t.name" :label="t.kind === 'mcp' ? `${t.serverKey} / ${t.originalToolName}` : t.name" :value="t.name" :title="`${t.name} · ${t.description || ''}`" />
                </el-option-group>
                <el-option-group v-if="unavailableSelected.length" label="已选但当前不可用">
                  <el-option v-for="name in unavailableSelected" :key="name" :label="`${name}（当前不可用 · 已保留）`" :value="name" disabled />
                </el-option-group>
              </el-select>
              <div v-if="unavailableSelected.length" class="mt-2 space-y-1 text-xs text-amber-700">
                <div v-for="name in unavailableSelected" :key="name" class="flex items-start gap-2">
                  <span class="break-all">{{ name }} · 当前不可用，恢复后按已选设置处理</span>
                  <el-button text size="small" :disabled="saving" @click="editing.toolAllowlist = editing.toolAllowlist.filter(item => item !== name)">移除</el-button>
                </div>
              </div>
              <div class="text-[11px] leading-5 text-macsub mt-1">留空不附加预设限制，本轮仍须显式授权；Agent 的 tools=[] 表示本轮无业务工具。MCP 需先在 <a href="/mcp" class="text-macblue">MCP 管理</a> 开放 Agent 访问；实际执行仍遵守调用审批。</div>
            </div>
            <div class="md:col-span-4 flex items-center gap-5"><el-switch v-model="editing.enabled" active-text="启用" inactive-text="停用" /></div>
          </section>

          <section class="agent-prompt-layout grid grid-cols-1 lg:grid-cols-[1fr_240px] gap-4 flex-1 min-h-0">
            <div class="mac-panel p-4 flex flex-col min-h-0">
              <div class="flex items-center justify-between gap-3 mb-2">
                <label class="text-xs text-macsub block">System Prompt</label>
                <el-select size="small" clearable placeholder="套用模板" class="w-44" @change="applyPromptPreset">
                  <el-option v-for="preset in AGENT_PROMPT_PRESETS" :key="preset.key" :label="preset.label" :value="preset.key" />
                </el-select>
              </div>
              <el-input v-model="editing.systemPrompt" type="textarea" resize="none" class="flex-1 agent-textarea" placeholder="只补充这个 Agent 的专业能力与领域注意事项；工作方式、边界和交接由基础提示词统一规定" />
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

          <footer class="agent-desktop-footer shrink-0 flex justify-end gap-2 border-t border-macborder pt-3">
            <el-button @click="drawerOpen = false">取消</el-button>
            <el-button type="primary" :loading="saving" :disabled="saving" @click="save">保存</el-button>
          </footer>
        </div>
      </template>
      <template #footer>
        <div v-if="editing" class="admin-mobile-only agent-mobile-footer">
          <el-button @click="drawerOpen = false">取消</el-button>
          <el-button type="primary" :loading="saving" :disabled="saving" @click="save">保存</el-button>
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
