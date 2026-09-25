<script setup>
import { computed, defineAsyncComponent, nextTick, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { Api, apiError } from "../api";
import ModelOrderPicker from "../components/ModelOrderPicker.vue";
import { settingDisplayValue, settingStorageValue, settingRangeLabel } from "./settingsDisplay.js";

const MdEditor = defineAsyncComponent(() => import("../components/AdaptiveMdEditor.vue"));

function builtinPrompt(spec) {
  return spec?.defaultValue || "";
}

const loading = ref(false);
const saving = reactive({});
const domains = ref([]);
const specs = ref({});
const values = ref({});
const masked = ref({});
const usingBuiltin = ref({});
const draft = reactive({});
const activeDomain = ref("agent");
const query = ref("");
const revision = ref(0);
const editingPath = ref("");
const editingPromptPath = ref("");
const promptPreviewOpen = ref(false);
const promptPreviewText = ref("");
const promptPreviewTitle = ref("");
const previewingPrompt = ref("");
const testingNotification = ref(false);
const testingBrowser = ref(false);
const browserProbe = ref(null);
const browserProbeState = computed(() => browserProbe.value?.endpoint === String(draft['browser.mainEndpoint'] || '').trim() ? browserProbe.value : null);
const compressionModels = ref([]);


const selectOptions = {
  "memory.provider": [
    { value: "builtin", label: "内置模式" },
    { value: "external", label: "外部服务" },
  ],
};

const vAutofocus = {
  mounted(el) {
    const input = el.querySelector?.("input, textarea") || el;
    requestAnimationFrame(() => {
      input?.focus?.();
      input?.select?.();
    });
  },
};

const domainIcons = {
  agent: "✦",
  tools: "⌘",
  browser: "◈",
  memory: "◉",
  media: "▣",
  web: "◎",
  interface: "◐",
};

function okOrThrow(data) { if (data?.ok === false) throw new Error(data.error || "操作失败"); return data; }
function matchesQuery(spec) {
  const q = query.value.trim().toLowerCase();
  if (!q) return true;
  return [spec?.path, spec?.title, spec?.desc, spec?.effect, spec?.unit]
    .some((x) => String(x || "").toLowerCase().includes(q));
}
function specsForSection(section) {
  return (section?.paths || []).map((path) => specs.value[path]).filter(Boolean).filter(matchesQuery);
}
function domainSettingCount(domain, filtered = false) {
  return (domain?.sections || []).reduce((total, section) => {
    if (filtered) return total + specsForSection(section).length;
    return total + (section.paths || []).filter((path) => specs.value[path]).length;
  }, 0);
}
const activeDomainInfo = computed(() => domains.value.find((domain) => domain.key === activeDomain.value) || domains.value[0] || {});
const visibleSections = computed(() => {
  const sourceDomains = query.value.trim() ? domains.value : [activeDomainInfo.value];
  return sourceDomains.flatMap((domain) => (domain?.sections || []).map((section) => ({
    ...section,
    domainKey: domain.key,
    domainTitle: domain.title,
    specs: specsForSection(section),
  }))).filter((section) => section.specs.length > 0);
});
const totalSettings = computed(() => Object.keys(specs.value || {}).length);
const restartCount = computed(() => Object.values(specs.value || {}).filter((s) => s.effect === "需要重启").length);
const dirtyCount = computed(() => Object.values(specs.value || {}).filter((spec) => isDirty(spec)).length);
const resultCount = computed(() => visibleSections.value.reduce((total, section) => total + section.specs.length, 0));
function optionsFor(spec) {
  if (Array.isArray(spec?.choices) && spec.choices.length) return spec.choices;
  return selectOptions[spec?.path] || [];
}
function hasOptions(spec) { return spec?.kind !== "multi" && optionsFor(spec).length > 0; }
function isMulti(spec) { return spec?.kind === "multi"; }
function isPromptEditorSpec(spec) { return spec?.editor === "prompt"; }
function isPromptEditing(spec) { return editingPromptPath.value === spec?.path; }
function promptVariableLabel(name) { return `{${name}}`; }
function isLongText(spec) { return spec?.kind === "str" && /prompt|提示词/i.test(`${spec.path} ${spec.title}`); }
function optionLabel(spec, value) {
  return optionsFor(spec).find((item) => item.value === value)?.label || value;
}
function namingCallPlan() {
  const configured = Array.isArray(draft["models.namingModels"]) ? draft["models.namingModels"] : [];
  const attempts = 1 + Math.max(0, Number(draft["agent.namingMaxRetries"] ?? values.value["agent.namingMaxRetries"] ?? 3) || 0);
  if (!configured.length) return `未配置候选时自动选择预计费用最低的可用计费模型；无计费模型时回退会话主模型。最多调用 ${attempts} 次。`;
  const names = new Map(compressionModels.value.map(model => [model.key, model.label || model.model || model.key]));
  const selected = configured.filter(key => names.has(key));
  if (!selected.length) return `当前候选均不可用；请重新选择命名模型。不可用候选不会占用调用次数。`;
  const order = Array.from({length: attempts}, (_, index) => names.get(selected[index % selected.length]));
  const unused = Math.max(0, selected.length - attempts);
  const unavailable = configured.length - selected.length;
  return `调用顺序：${order.join(" → ")}。${unused ? `当前有 ${selected.length} 个可用候选，但最多调用 ${attempts} 次，后 ${unused} 个不会被使用。` : ""}${unavailable ? `${unavailable} 个不可用候选会被跳过。` : ""}`;
}

function normalizeDraftValue(spec, value) {
  if (spec?.kind === "bool") return Boolean(value);
  if (spec?.kind === "multi") return Array.isArray(value) ? [...value] : [];
  if (isPromptEditorSpec(spec) && (value === undefined || value === null || value === "")) return builtinPrompt(spec);
  if (value === undefined || value === null) return "";
  return settingDisplayValue(spec, value);
}
function hydrateDraft() {
  for (const [path, spec] of Object.entries(specs.value || {})) {
    draft[path] = normalizeDraftValue(spec, values.value[path]);
  }
}
function displayedValue(spec) {
  if (spec.sensitive && masked.value[spec.path]) return values.value[spec.path] || "已设置";
  const value = values.value[spec.path];
  if (spec.kind === "bool") return value ? "开" : "关";
  if (spec.kind === "multi") {
    const selected = Array.isArray(value) ? value : [];
    if (!selected.length) return "未选择事件";
    return selected.map((item) => optionLabel(spec, item)).join("、");
  }
  if (isPromptEditorSpec(spec) && (value === undefined || value === null || value === "")) return builtinPrompt(spec);
  if (value === undefined || value === null || value === "") return "未设置";
  if (hasOptions(spec)) return optionLabel(spec, value);
  return `${settingDisplayValue(spec, value)}${spec.unit || ""}`;
}
function isDirty(spec) {
  if (!spec) return false;
  if (isPromptEditorSpec(spec)) {
    return String(draft[spec.path] || "") !== String(values.value[spec.path] || builtinPrompt(spec));
  }
  const original = spec.displayScale > 1 ? normalizeDraftValue(spec, values.value[spec.path]) : values.value[spec.path];
  return JSON.stringify(draft[spec.path]) !== JSON.stringify(original);
}
function inputPlaceholder(spec) {
  if (spec.sensitive) return "留空不修改，输入新值后保存";
  if (spec.displayScale > 1) return `输入数字（${spec.unit}）`;
  if (spec.kind === "int") return "整数";
  if (spec.kind === "float") return "数字";
  return "输入文本";
}
function effectTagType(effect) {
  if (effect === "立即生效") return "success";
  if (effect === "需要重启") return "warning";
  return "info";
}
async function load() {
  loading.value = true;
  try {
    const [specData, settingsData, modelData] = await Promise.all([Api.settingsSpecs(), Api.settings(), Api.rathOptions().catch(() => ({models: []}))]);
    compressionModels.value = modelData.models || [];
    okOrThrow(specData); okOrThrow(settingsData);
    const apiDomains = (specData.domains || []).filter((domain) => (domain.sections || []).length > 0);
    domains.value = apiDomains.length
      ? apiDomains
      : (specData.groups || []).map((group) => ({ ...group, sections: [group] }));
    const visiblePaths = new Set(domains.value.flatMap((domain) =>
      (domain.sections || []).flatMap((section) => section.paths || [])));
    specs.value = Object.fromEntries(Object.entries(specData.specs || {}).filter(([path]) => visiblePaths.has(path)));
    // MCP 管理页的深链只定位真实可编辑的设置项，不猜测默认分区。
    const settingPath = typeof window === "undefined" ? "" : new URLSearchParams(window.location.search).get("setting") || "";
    if (settingPath && specs.value[settingPath]) {
      const domain = domains.value.find((item) => (item.sections || []).some((section) => (section.paths || []).includes(settingPath)));
      if (domain) activeDomain.value = domain.key;
      query.value = settingPath;
    }
    values.value = settingsData.values || {};
    masked.value = settingsData.masked || {};
    usingBuiltin.value = settingsData.usingBuiltin || {};
    revision.value = settingsData.revision || 0;
    if (!domains.value.some((domain) => domain.key === activeDomain.value)) activeDomain.value = domains.value[0]?.key || "";
    hydrateDraft();
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    loading.value = false;
  }
}
function isEditing(spec) {
  return Boolean(spec) && (editingPath.value === spec.path || isDirty(spec));
}
async function beginEdit(spec) {
  if (!spec || spec.kind === "bool" || spec.kind === "multi") return;
  editingPath.value = spec.path;
  await nextTick();
}
function reset(spec, options = {}) {
  if (!spec) return;
  draft[spec.path] = normalizeDraftValue(spec, values.value[spec.path]);
  if (!options.keepOpen && editingPath.value === spec.path) editingPath.value = "";
}
async function save(spec, overrideValue = undefined) {
  if (!spec || saving[spec.path]) return false;
  if (spec.sensitive && !String(draft[spec.path] || "").trim()) {
    ElMessage.info("敏感字段留空表示不修改");
    reset(spec, { keepOpen: true });
    return false;
  }
  saving[spec.path] = true;
  try {
    const input = overrideValue === undefined ? draft[spec.path] : overrideValue;
    const data = await Api.updateSetting(spec.path, settingStorageValue(spec, input));
    okOrThrow(data);
    const fresh = await Api.settings();
    okOrThrow(fresh);
    values.value = fresh.values || {};
    masked.value = fresh.masked || {};
    usingBuiltin.value = fresh.usingBuiltin || {};
    revision.value = fresh.revision || data.revision || revision.value;
    hydrateDraft();
    if (editingPath.value === spec.path) editingPath.value = "";
    if (isPromptEditorSpec(spec)) editingPromptPath.value = "";
    ElMessage.success(`${spec.title} 已保存`);
    return true;
  } catch (error) {
    draft[spec.path] = normalizeDraftValue(spec, values.value[spec.path]);
    ElMessage.error(apiError(error));
    return false;
  } finally {
    saving[spec.path] = false;
  }
}
function handleEditorBlur(spec) {
  window.setTimeout(() => {
    if (editingPath.value !== spec.path || saving[spec.path]) return;
    reset(spec);
  }, 80);
}
async function selectOption(spec, value) {
  draft[spec.path] = value;
  await save(spec);
}
async function toggleBool(spec) {
  if (!spec || saving[spec.path]) return;
  draft[spec.path] = !Boolean(draft[spec.path]);
  await save(spec);
}
async function toggleMulti(spec, value) {
  if (!spec || saving[spec.path]) return;
  const selected = new Set(Array.isArray(draft[spec.path]) ? draft[spec.path] : []);
  if (selected.has(value)) selected.delete(value);
  else selected.add(value);
  draft[spec.path] = optionsFor(spec).map((item) => item.value).filter((item) => selected.has(item));
  await save(spec);
}
async function useBuiltinPrompt(spec) {
  if (!spec || saving[spec.path]) return;
  draft[spec.path] = builtinPrompt(spec);
  await save(spec, "");
}
async function previewPrompt(spec) {
  if (!spec || previewingPrompt.value) return;
  previewingPrompt.value = spec.path;
  try {
    const data = okOrThrow(await Api.previewSettingPrompt(spec.path, draft[spec.path]));
    promptPreviewTitle.value = `${spec.title} · 渲染预览`;
    promptPreviewText.value = data.rendered || "";
    promptPreviewOpen.value = true;
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    previewingPrompt.value = "";
  }
}
async function testBrowserConnection() {
  const endpoint = String(draft['browser.mainEndpoint'] || '').trim();
  if (testingBrowser.value || !endpoint) return;
  testingBrowser.value = true;
  browserProbe.value = null;
  try {
    const result = await Api.testBrowserConnection(endpoint);
    browserProbe.value = {endpoint, ok: Boolean(result?.ok), text: result?.ok
      ? `连接成功 · ${result.product || 'CDP'} · ${result.elapsedMs ?? 0} ms（仅验证，不保存或启用）`
      : result?.error || '连接验证失败'};
  } catch (error) {
    browserProbe.value = {endpoint, ok: false, text: apiError(error)};
  } finally {
    testingBrowser.value = false;
  }
}

async function testTaskNotification() {
  if (testingNotification.value) return;
  testingNotification.value = true;
  try {
    okOrThrow(await Api.testWebTaskNotification());
    ElMessage.success("测试通知已发送到当前 Telegram 账号");
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    testingNotification.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="settings-shell h-full" v-loading="loading">
    <header class="settings-toolbar">
      <div class="min-w-0">
        <div class="flex items-center gap-2.5">
          <h1>系统设置</h1>
          <span class="settings-health-dot" title="配置白名单与原子写入已启用"></span>
        </div>
        <p>按使用场景管理 OpenBear，不必在配置文件里翻找参数。</p>
      </div>
      <div class="settings-toolbar__actions">
        <div class="settings-search">
          <span aria-hidden="true">⌕</span>
          <input v-model="query" type="search" placeholder="搜索全部设置" aria-label="搜索全部设置" />
          <button v-if="query" type="button" aria-label="清除搜索" @click="query = ''">×</button>
        </div>
        <button class="settings-refresh" type="button" :disabled="loading" title="刷新设置" @click="load">↻</button>
      </div>
    </header>

    <div class="settings-layout">
      <aside class="settings-sidebar">
        <label class="settings-domain-picker admin-mobile-only">
          <span>设置领域</span>
          <select :value="activeDomain" aria-label="切换设置领域" @change="activeDomain = $event.target.value; query = ''">
            <option v-for="domain in domains" :key="domain.key" :value="domain.key">{{ domain.title }} · {{ domainSettingCount(domain) }} 项</option>
          </select>
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4" /></svg>
        </label>
        <div class="settings-sidebar__eyebrow">设置领域</div>
        <nav class="settings-domain-list" aria-label="设置领域">
          <button
            v-for="domain in domains"
            :key="domain.key"
            type="button"
            class="settings-domain"
            :class="activeDomain === domain.key && !query ? 'is-active' : ''"
            @click="activeDomain = domain.key; query = ''"
          >
            <span class="settings-domain__icon">{{ domainIcons[domain.key] || '•' }}</span>
            <span class="settings-domain__copy">
              <strong>{{ domain.title }}</strong>
              <small>{{ domain.desc }}</small>
            </span>
            <span class="settings-domain__count">{{ domainSettingCount(domain) }}</span>
          </button>
        </nav>
        <div class="settings-sidebar__footer">
          <span>{{ totalSettings }} 项设置</span>
          <span>Revision {{ revision }}</span>
        </div>
      </aside>

      <main class="settings-content">
        <section class="settings-intro">
          <div class="settings-intro__mark">{{ query ? '⌕' : (domainIcons[activeDomain] || '•') }}</div>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2.5">
              <h2>{{ query ? `搜索“${query}”` : activeDomainInfo.title }}</h2>
              <span class="settings-count-chip">{{ resultCount }} 项</span>
            </div>
            <p>{{ query ? '正在全部设置中匹配标题、说明、配置路径和生效方式。' : activeDomainInfo.desc }}</p>
          </div>
          <div class="settings-summary">
            <span v-if="restartCount"><b>{{ restartCount }}</b> 项需重启</span>
            <span :class="dirtyCount ? 'has-dirty' : ''"><b>{{ dirtyCount }}</b> 项未保存</span>
          </div>
        </section>

        <div v-if="!visibleSections.length" class="settings-empty">
          <div>⌕</div>
          <strong>没有找到匹配设置</strong>
          <p>换个关键词，或者搜索配置路径。</p>
        </div>

        <section v-for="section in visibleSections" :key="`${section.domainKey}:${section.key}`" class="settings-section">
          <header class="settings-section__header">
            <div>
              <div class="flex flex-wrap items-center gap-2">
                <h3>{{ section.title }}</h3>
                <span v-if="query" class="settings-domain-label">{{ section.domainTitle }}</span>
              </div>
              <p>{{ section.specs.length }} 项设置</p>
            </div>
            <button
              v-if="section.key === 'web_notifications'"
              type="button"
              class="mac-text-button"
              :class="testingNotification ? 'is-loading' : ''"
              :disabled="testingNotification"
              @click="testTaskNotification"
            >{{ testingNotification ? '发送中…' : '发送测试通知' }}</button>
          </header>

          <div class="settings-list">
            <article
              v-for="spec in section.specs"
              :key="spec.path"
              class="settings-row"
              :class="[
                isDirty(spec) ? 'is-dirty' : '',
                spec.group === 'compaction' ? 'is-context' : '',
                isPromptEditorSpec(spec) ? 'is-prompt' : '',
              ]"
            >
              <template v-if="isPromptEditorSpec(spec)">
                <div class="settings-prompt-card">
                  <div class="flex flex-wrap items-start justify-between gap-3">
                    <div class="min-w-0">
                      <div class="flex flex-wrap items-center gap-2">
                        <h4>{{ spec.title }}</h4>
                        <span class="settings-effect" :class="`is-${effectTagType(spec.effect)}`">{{ spec.effect }}</span>
                      </div>
                      <p>{{ spec.desc }}</p>
                    </div>
                    <div class="flex shrink-0 items-center gap-2">
                      <template v-if="isPromptEditing(spec)">
                        <button class="mac-text-button" :class="previewingPrompt === spec.path ? 'is-loading' : ''" :disabled="Boolean(previewingPrompt)" @click="previewPrompt(spec)">{{ previewingPrompt === spec.path ? '渲染中…' : '预览' }}</button>
                        <button class="mac-text-button" :disabled="saving[spec.path]" @click="useBuiltinPrompt(spec)">恢复内置默认</button>
                        <button class="mac-text-button" :disabled="saving[spec.path]" @click="reset(spec, { keepOpen: true }); editingPromptPath = ''">关闭编辑器</button>
                        <button class="mac-text-button is-primary" :class="saving[spec.path] ? 'is-loading' : ''" :disabled="saving[spec.path] || !isDirty(spec)" @click="save(spec)">{{ saving[spec.path] ? '保存中…' : '保存' }}</button>
                      </template>
                      <button v-else class="mac-text-button is-primary" @click="editingPromptPath = spec.path">编辑提示词</button>
                    </div>
                  </div>
                  <div v-if="isPromptEditing(spec)" class="settings-prompt-editor mt-4">
                    <MdEditor mobile-flow v-model="draft[spec.path]" completion-mode="none" square />
                  </div>
                  <div class="settings-technical is-open">
                    <code>{{ spec.path }}</code>
                    <span v-if="usingBuiltin[spec.path]" class="settings-builtin">跟随内置默认</span>
                    <span v-if="spec.variables?.length">变量：<code v-for="name in spec.variables" :key="name">{{ promptVariableLabel(name) }} </code></span>
                    <span v-else>无可用占位符</span>
                  </div>
                </div>
              </template>

              <template v-else>
                <div class="settings-row__main">
                  <div class="settings-row__copy">
                    <div class="flex flex-wrap items-center gap-2">
                      <h4>{{ spec.title }}</h4>
                      <span v-if="spec.sensitive" class="settings-sensitive">敏感</span>
                    </div>
                    <p>{{ spec.desc }}</p>
                    <div v-if="spec.path === 'browser.mainEndpoint'" class="browser-connection-test">
                      <button type="button" class="mac-text-button" :disabled="testingBrowser || saving[spec.path] || !String(draft[spec.path] || '').trim()" @mousedown.prevent @click="testBrowserConnection">{{ testingBrowser ? '验证中…' : '测试连接' }}</button>
                      <span v-if="browserProbeState" role="status" :class="{'is-error': !browserProbeState.ok}">{{ browserProbeState.text }}</span>
                    </div>
                  </div>

                  <div class="setting-control min-w-0">
                    <template v-if="spec.kind === 'bool'">
                      <div class="flex items-center justify-end gap-3">
                        <span class="setting-state-text">{{ draft[spec.path] ? '开启' : '关闭' }}</span>
                        <button
                          type="button"
                          class="mac-toggle"
                          :class="draft[spec.path] ? 'is-on' : ''"
                          :disabled="saving[spec.path]"
                          :aria-pressed="Boolean(draft[spec.path])"
                          @click="toggleBool(spec)"
                        >
                          <span class="mac-toggle__knob">{{ saving[spec.path] ? '…' : '' }}</span>
                          <span class="sr-only">切换 {{ spec.title }}</span>
                        </button>
                      </div>
                    </template>
                    <template v-else-if="['models.compressionModels', 'models.namingModels'].includes(spec.path)">
                      <div class="summary-model-setting">
                        <ModelOrderPicker v-model="draft[spec.path]" :models="compressionModels" :disabled="saving[spec.path]"
                          :label="spec.path === 'models.namingModels' ? '命名模型' : '摘要模型'"
                          :empty-label="spec.path === 'models.namingModels' ? '自动选择最便宜模型' : '使用当前执行模型'"
                          :footer-text="spec.path === 'models.namingModels' ? '按调用次数循环候选；配置候选后不追加隐藏模型。' : ''" />
                        <p v-if="spec.path === 'models.namingModels'" class="naming-model-plan">{{ namingCallPlan() }}</p>
                        <div v-if="isDirty(spec)" class="summary-model-save">
                          <button type="button" class="mac-text-button" :disabled="saving[spec.path]" @click="reset(spec)">取消更改</button>
                          <button type="button" class="mac-text-button" :disabled="saving[spec.path]" @click="save(spec)">{{ saving[spec.path] ? '保存中…' : '保存模型及顺序' }}</button>
                        </div>
                      </div>
                    </template>
                    <template v-else-if="isMulti(spec)">
                      <div class="event-multi" :class="saving[spec.path] ? 'is-saving' : ''">
                        <button
                          v-for="option in optionsFor(spec)"
                          :key="option.value"
                          type="button"
                          :class="(draft[spec.path] || []).includes(option.value) ? 'is-active' : ''"
                          :disabled="saving[spec.path]"
                          :aria-pressed="(draft[spec.path] || []).includes(option.value)"
                          @click="toggleMulti(spec, option.value)"
                        ><span>{{ (draft[spec.path] || []).includes(option.value) ? '✓' : '+' }}</span>{{ option.label }}</button>
                      </div>
                    </template>
                    <template v-else-if="hasOptions(spec)">
                      <div class="mac-segmented" :class="saving[spec.path] ? 'is-saving' : ''">
                        <button
                          v-for="option in optionsFor(spec)"
                          :key="option.value"
                          type="button"
                          :class="values[spec.path] === option.value ? 'is-active' : ''"
                          :disabled="saving[spec.path] || values[spec.path] === option.value"
                          @click="selectOption(spec, option.value)"
                        >{{ option.label }}</button>
                      </div>
                    </template>
                    <template v-else>
                      <button v-if="!isEditing(spec)" type="button" class="value-pill" @click="beginEdit(spec)">
                        <span class="value-pill__label">{{ displayedValue(spec) }}</span>
                        <span class="value-pill__edit">✎</span>
                      </button>
                      <div v-else class="space-y-1.5">
                        <div class="editor-bar">
                          <el-input
                            v-if="isLongText(spec)"
                            v-autofocus
                            v-model="draft[spec.path]"
                            class="setting-input setting-input--textarea"
                            size="small"
                            type="textarea"
                            :autosize="{ minRows: 4, maxRows: 12 }"
                            :placeholder="inputPlaceholder(spec)"
                            :disabled="saving[spec.path]"
                            @blur="handleEditorBlur(spec)"
                            @keydown.esc.prevent="reset(spec)"
                          />
                          <el-input
                            v-else
                            v-autofocus
                            v-model="draft[spec.path]"
                            class="setting-input"
                            size="small"
                            type="text"
                            :inputmode="spec.kind === 'int' || spec.kind === 'float' ? 'decimal' : undefined"
                            :placeholder="inputPlaceholder(spec)"
                            :disabled="saving[spec.path]"
                            @blur="handleEditorBlur(spec)"
                            @keydown.enter.prevent="save(spec)"
                            @keydown.esc.prevent="reset(spec)"
                          ><template v-if="spec.displayScale > 1" #append>{{ spec.unit }}</template></el-input>
                          <button type="button" class="mac-icon-action mac-icon-action--primary" :class="saving[spec.path] ? 'is-loading' : ''" :disabled="!isDirty(spec) || saving[spec.path]" title="保存" @mousedown.prevent @click="save(spec)">{{ saving[spec.path] ? '…' : '✓' }}</button>
                          <button type="button" class="mac-icon-action" :disabled="saving[spec.path]" title="撤销" @mousedown.prevent @click="reset(spec)">↩</button>
                        </div>
                        <div class="editor-hint">
                          <template v-if="spec.sensitive">留空保存不修改 · 失焦或 Esc 还原</template>
                          <template v-else>Enter 保存 · 失焦或 Esc 还原</template>
                        </div>
                      </div>
                    </template>
                  </div>
                </div>

                <div class="settings-technical is-open">
                  <code>{{ spec.path }}</code>
                  <span class="settings-effect" :class="`is-${effectTagType(spec.effect)}`">{{ spec.effect }}</span>
                  <span v-if="spec.unit">单位 {{ spec.unit }}</span>
                  <span v-if="spec.min !== null || spec.max !== null">范围 {{ settingRangeLabel(spec) }}</span>
                </div>
              </template>
            </article>
          </div>
        </section>
      </main>
    </div>

    <el-dialog v-model="promptPreviewOpen" class="admin-dialog mac-dialog" :title="promptPreviewTitle" width="860px" top="7vh" append-to-body>
      <div class="prompt-preview-note">使用示例变量渲染；保存时仍会再次执行相同的占位符校验。</div>
      <pre class="prompt-preview-output">{{ promptPreviewText }}</pre>
      <template #footer>
        <button type="button" class="mac-text-button is-primary" @click="promptPreviewOpen = false">关闭</button>
      </template>
    </el-dialog>
  </div>
</template>


<style scoped>
.browser-connection-test { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
.browser-connection-test span { color: var(--ob-text-subtle); font-size: 12px; overflow-wrap: anywhere; }
.browser-connection-test span.is-error { color: var(--ob-danger); }
.settings-row.is-context p, .settings-row.is-context .settings-technical, .settings-row.is-context .settings-builtin, .settings-row.is-context .mac-text-button { font-size: 13px; }
.settings-row.is-context .value-pill__label, .settings-row.is-context .mac-segmented button { font-size: 14px; }
.summary-model-setting { width: 100%; min-width: 0; }
.naming-model-plan { margin: 7px 2px 0; color: var(--settings-muted); font-size: 12px; line-height: 1.55; }
.summary-model-save { display:flex; justify-content:flex-end; gap:16px; margin-top:10px; font-size:14px; }
.settings-shell {
  --settings-ink: var(--ob-text-strong);
  --settings-muted: var(--ob-text-subtle);
  --settings-line: var(--ob-border);
  --settings-blue: var(--ob-blue);
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  color: var(--settings-ink);
  background: var(--ob-bg);
}
.settings-toolbar {
  display: flex;
  min-height: 74px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 28px;
  border-bottom: 1px solid var(--ob-border);
  background: var(--ob-surface);
  box-shadow: 0 1px 0 var(--ob-border);
}
.settings-toolbar h1 {
  font-size: 17px;
  font-weight: 680;
  letter-spacing: -0.025em;
}
.settings-toolbar p {
  margin-top: 3px;
  color: var(--settings-muted);
  font-size: 12px;
}
.settings-health-dot {
  width: 7px;
  height: 7px;
  border-radius: 99px;
  background: var(--ob-success);
  box-shadow: 0 0 0 4px rgb(var(--ob-success-rgb) / 0.11);
}
.settings-toolbar__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.settings-search {
  display: flex;
  width: min(320px, 34vw);
  height: 36px;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  border: 1px solid var(--ob-border);
  border-radius: 12px;
  background: rgb(var(--ob-surface-rgb) / 0.78);
  color: var(--ob-text-muted);
  box-shadow: inset 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.04), 0 1px 0 var(--ob-border);
  transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
}
.settings-search:focus-within {
  border-color: rgb(var(--ob-blue-rgb) / 0.48);
  background: rgb(var(--ob-surface-rgb) / 0.95);
  box-shadow: 0 0 0 3px rgb(var(--ob-blue-rgb) / 0.08);
}
.settings-search input {
  min-width: 0;
  flex: 1;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--ob-text);
  font-size: 12px;
}
.settings-search input::-webkit-search-cancel-button { display: none; }
.settings-search button {
  display: grid;
  width: 18px;
  height: 18px;
  place-items: center;
  border-radius: 99px;
  background: var(--ob-surface-soft);
  color: var(--ob-text-subtle);
  font-size: 13px;
  line-height: 1;
}
.settings-refresh {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border: 1px solid var(--ob-border);
  border-radius: 12px;
  background: rgb(var(--ob-surface-rgb) / 0.78);
  color: var(--ob-text);
  font-size: 18px;
  transition: transform .18s ease, box-shadow .18s ease;
}
.settings-refresh:hover:not(:disabled) {
  transform: rotate(18deg);
  box-shadow: 0 5px 14px rgb(var(--ob-shadow-rgb) / 0.08);
}
.settings-layout {
  display: grid;
  min-height: 0;
  flex: 1;
  grid-template-columns: 268px minmax(0, 1fr);
  gap: 18px;
  padding: 20px;
}
.settings-sidebar {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  padding: 12px;
  border: 1px solid var(--settings-line);
  border-radius: 20px;
  background: var(--ob-surface-soft);
  box-shadow: 0 10px 28px rgb(var(--ob-shadow-rgb) / 0.055), inset 0 1px 0 var(--ob-surface);
}
.settings-sidebar__eyebrow {
  padding: 6px 10px 10px;
  color: var(--ob-text-muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
}
.settings-domain-list {
  min-height: 0;
  flex: 1;
  overflow-x: hidden;
  overflow-y: auto;
}
.settings-domain {
  display: grid;
  width: 100%;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
  padding: 10px;
  border: 1px solid transparent;
  border-radius: 14px;
  text-align: left;
  transition: border-color .12s ease, background-color .12s ease;
}
.settings-domain:hover {
  border-color: rgb(var(--ob-blue-rgb) / 0.18);
  background: var(--ob-surface);
}
.settings-domain.is-active {
  border-color: rgb(var(--ob-blue-rgb) / 0.18);
  background: linear-gradient(135deg, rgb(var(--ob-surface-rgb) / 0.98), rgb(var(--ob-surface-rgb) / 0.93));
  box-shadow: 0 8px 20px rgb(var(--ob-blue-rgb) / 0.09), inset 3px 0 0 var(--settings-blue);
}
.settings-domain__icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid var(--ob-border);
  border-radius: 11px;
  background: rgb(var(--ob-surface-rgb) / 0.86);
  color: var(--ob-text);
  font-size: 16px;
  box-shadow: 0 2px 7px rgb(var(--ob-shadow-rgb) / 0.06);
}
.settings-domain.is-active .settings-domain__icon {
  border-color: rgb(var(--ob-blue-rgb) / 0.2);
  color: var(--settings-blue);
}
.settings-domain__copy { min-width: 0; }
.settings-domain__copy strong {
  display: block;
  color: var(--ob-text);
  font-size: 14px;
  font-weight: 650;
}
.settings-domain__copy small {
  display: block;
  overflow: hidden;
  margin-top: 2px;
  color: var(--ob-text-subtle);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.settings-domain__count {
  min-width: 24px;
  padding: 2px 6px;
  border-radius: 99px;
  background: rgb(var(--ob-surface-rgb) / 0.72);
  color: var(--ob-text-subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  text-align: center;
}
.settings-sidebar__footer {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
  padding: 12px 8px 3px;
  border-top: 1px solid var(--ob-border);
  color: var(--ob-text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
}
.settings-content {
  min-height: 0;
  overflow-y: auto;
  padding: 0 5px 36px 0;
  scroll-behavior: smooth;
}
.settings-intro {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 16px;
  padding: 16px 18px;
  border: 1px solid var(--settings-line);
  border-radius: 18px;
  background: var(--ob-surface);
  box-shadow: 0 6px 18px rgb(var(--ob-shadow-rgb) / 0.045), inset 0 1px 0 var(--ob-surface);
}
.settings-intro__mark {
  display: grid;
  width: 42px;
  height: 42px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 14px;
  background: linear-gradient(145deg, var(--ob-blue), var(--ob-blue));
  color: var(--ob-text-inverse);
  font-size: 19px;
  box-shadow: 0 9px 22px rgb(var(--ob-blue-rgb) / 0.25), inset 0 1px 0 var(--ob-border-strong);
}
.settings-intro h2 {
  font-size: 18px;
  font-weight: 680;
  letter-spacing: -.02em;
}
.settings-intro p {
  margin-top: 3px;
  color: var(--settings-muted);
  font-size: 12px;
}
.settings-count-chip,
.settings-domain-label {
  padding: 3px 8px;
  border: 1px solid var(--ob-border);
  border-radius: 99px;
  background: rgb(var(--ob-surface-rgb) / 0.8);
  color: var(--ob-text-subtle);
  font-size: 11px;
}
.settings-summary {
  display: flex;
  flex: 0 0 auto;
  gap: 7px;
}
.settings-summary span {
  padding: 6px 9px;
  border-radius: 9px;
  background: rgb(var(--ob-surface-rgb) / 0.85);
  color: var(--ob-text-subtle);
  font-size: 11px;
}
.settings-summary b { color: var(--ob-text); }
.settings-summary .has-dirty {
  background: rgb(var(--ob-blue-rgb) / 0.09);
  color: var(--ob-blue);
}
.settings-summary .has-dirty b { color: var(--ob-blue); }
.settings-section {
  overflow: hidden;
  margin-bottom: 14px;
  border: 1px solid var(--settings-line);
  border-radius: 18px;
  background: var(--ob-surface);
  box-shadow: 0 6px 18px rgb(var(--ob-shadow-rgb) / 0.04), inset 0 1px 0 var(--ob-surface);
}
.settings-section__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 17px 11px;
  border-bottom: 1px solid var(--ob-border);
  background: linear-gradient(180deg, rgb(var(--ob-surface-rgb) / 0.88), rgb(var(--ob-surface-rgb) / 0.58));
}
.settings-section__header h3 {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: -.01em;
}
.settings-section__header p {
  margin-top: 2px;
  color: var(--ob-text-muted);
  font-size: 11px;
}
.settings-list { background: rgb(var(--ob-surface-rgb) / 0.5); }
.settings-row {
  position: relative;
  padding: 16px 17px 13px;
  border-bottom: 1px solid var(--ob-border);
  transition: background-color .12s ease;
}
.settings-row:last-child { border-bottom: 0; }
.settings-row:hover { background: rgb(var(--ob-surface-rgb) / 0.76); }
.settings-row.is-dirty {
  background: linear-gradient(90deg, rgb(var(--ob-blue-rgb) / 0.055), transparent 72%);
  box-shadow: inset 3px 0 0 rgb(var(--ob-blue-rgb) / 0.72);
}
.settings-row.is-prompt { padding: 17px; }
.settings-row__main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(220px, 340px);
  align-items: center;
  gap: 24px;
}
.settings-row__copy { min-width: 0; }
.settings-row h4 {
  color: var(--ob-text);
  font-size: 14px;
  font-weight: 650;
}
.settings-row__copy > p,
.settings-prompt-card > div:first-child p {
  max-width: 720px;
  margin-top: 4px;
  color: var(--settings-muted);
  font-size: 12px;
  line-height: 1.65;
}
.settings-sensitive {
  padding: 2px 6px;
  border-radius: 99px;
  background: rgb(var(--ob-danger-rgb) / 0.09);
  color: var(--ob-danger);
  font-size: 10px;
  font-weight: 700;
}
.settings-details-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 7px;
  color: var(--ob-text-muted);
  font-size: 9px;
  transition: color .14s ease;
}
.settings-details-button:hover { color: var(--ob-text); }
.settings-details-button span:last-child { transition: transform .16s ease; }
.settings-details-button span.is-open { transform: rotate(180deg); }
.settings-technical {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid var(--ob-border);
  border-radius: 10px;
  background: rgb(var(--ob-surface-rgb) / 0.72);
  color: var(--ob-text-subtle);
  font-size: 11px;
}
.settings-technical > code {
  color: var(--ob-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.settings-effect {
  padding: 2px 6px;
  border-radius: 99px;
  background: rgb(var(--ob-surface-soft-rgb) / 0.09);
  color: var(--ob-text-subtle);
  font-size: 10px;
  font-weight: 650;
}
.settings-effect.is-success { background: rgb(var(--ob-success-rgb) / 0.1); color: var(--ob-success); }
.settings-effect.is-warning { background: rgb(var(--ob-orange-rgb) / 0.12); color: var(--ob-warning); }
.settings-empty {
  display: grid;
  min-height: 300px;
  place-items: center;
  align-content: center;
  gap: 7px;
  border: 1px dashed var(--ob-border-strong);
  border-radius: 18px;
  background: rgb(var(--ob-surface-rgb) / 0.5);
  color: var(--ob-text-muted);
  text-align: center;
}
.settings-empty > div { font-size: 28px; }
.settings-empty strong { color: var(--ob-text); font-size: 12px; }
.settings-empty p { font-size: 10px; }
.setting-control {
  display: flex;
  justify-content: flex-end;
}
.setting-control {
  display: flex;
  justify-content: flex-end;
}
.settings-prompt-card {
  display: block;
}
.settings-prompt-editor {
  height: min(58vh, 520px);
  min-height: 360px;
  overflow: hidden;
  border-radius: 0;
  background: var(--ob-surface);
  box-shadow: 0 12px 36px rgb(var(--ob-shadow-rgb) / 0.08), inset 0 0 0 1px rgb(var(--ob-shadow-rgb) / 0.22);
}
.mac-text-button {
  height: 30px;
  padding: 0 11px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: rgb(var(--ob-surface-rgb) / 0.88);
  color: var(--ob-text);
  font-size: 12px;
  box-shadow: inset 0 1px 0 var(--ob-border), 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.04);
  transition: transform 0.14s ease, box-shadow 0.14s ease, border-color 0.14s ease, opacity 0.14s ease;
}
.mac-text-button:hover:not(:disabled) {
  transform: translateY(-1px);
  border-color: rgb(var(--ob-blue-rgb) / 0.32);
  box-shadow: inset 0 1px 0 var(--ob-border), 0 6px 16px rgb(var(--ob-shadow-rgb) / 0.08);
}
.mac-text-button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.mac-text-button.is-primary {
  border-color: rgb(var(--ob-blue-rgb) / 0.68);
  background: linear-gradient(180deg, var(--ob-blue), var(--ob-blue));
  color: var(--ob-text-inverse);
  box-shadow: inset 0 1px 0 var(--ob-border-strong), 0 5px 16px rgb(var(--ob-blue-rgb) / 0.22);
}
.mac-text-button.is-primary:disabled {
  border-color: var(--ob-border);
  background: var(--ob-surface-soft);
  box-shadow: none;
}
.setting-state-text {
  min-width: 2.25rem;
  color: var(--ob-text-subtle);
  font-size: 13px;
  letter-spacing: 0.01em;
}
.value-pill {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  min-width: 188px;
  max-width: 100%;
  height: 34px;
  gap: 12px;
  padding: 0 7px 0 13px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background:
    linear-gradient(180deg, rgb(var(--ob-surface-rgb) / 0.94), rgb(var(--ob-surface-rgb) / 0.86));
  color: var(--ob-text);
  box-shadow:
    inset 0 1px 0 var(--ob-border),
    0 1px 2px rgb(var(--ob-shadow-rgb) / 0.04);
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.value-pill:hover {
  border-color: rgb(var(--ob-blue-rgb) / 0.36);
  box-shadow:
    inset 0 1px 0 var(--ob-border),
    0 4px 14px rgb(var(--ob-blue-rgb) / 0.09);
  transform: translateY(-1px);
}
.value-pill__label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
}
.value-pill__edit {
  display: grid;
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 999px;
  background: rgb(var(--ob-surface-rgb) / 0.82);
  color: var(--ob-text-subtle);
  font-size: 12px;
  box-shadow: inset 0 0 0 1px rgb(var(--ob-shadow-rgb) / 0.22);
}
.editor-bar {
  display: flex;
  align-items: center;
  width: min(360px, 100%);
  gap: 5px;
  padding: 4px;
  border: 1px solid rgb(var(--ob-blue-rgb) / 0.28);
  border-radius: 16px;
  background:
    linear-gradient(180deg, rgb(var(--ob-surface-rgb) / 0.98), rgb(var(--ob-surface-rgb) / 0.94));
  box-shadow:
    0 0 0 3px rgb(var(--ob-blue-rgb) / 0.06),
    inset 0 1px 0 var(--ob-border),
    0 6px 22px rgb(var(--ob-blue-rgb) / 0.08);
}
.editor-hint {
  padding-right: 12px;
  text-align: right;
  font-size: 10px;
  line-height: 1;
  color: var(--ob-text-muted);
}
.setting-input {
  flex: 1;
  min-width: 0;
}
:deep(.setting-input .el-input__wrapper) {
  min-height: 28px;
  padding: 0 8px;
  border-radius: 11px;
  background: transparent;
  box-shadow: none !important;
}
:deep(.setting-input .el-input__inner) {
  height: 28px;
  color: var(--ob-text-strong);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
}
:deep(.setting-input .el-input__inner::placeholder) {
  color: var(--ob-text-muted);
}
.mac-icon-action {
  display: grid;
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: rgb(var(--ob-surface-rgb) / 0.9);
  color: var(--ob-text);
  font-size: 13px;
  line-height: 1;
  box-shadow: inset 0 1px 0 var(--ob-border), 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.05);
  transition: transform 0.14s ease, box-shadow 0.14s ease, opacity 0.14s ease;
}
.mac-icon-action:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: inset 0 1px 0 var(--ob-border), 0 5px 14px rgb(var(--ob-shadow-rgb) / 0.1);
}
.mac-icon-action:disabled {
  cursor: not-allowed;
  opacity: 0.42;
}
.mac-icon-action--primary {
  border-color: rgb(var(--ob-blue-rgb) / 0.62);
  background: linear-gradient(180deg, var(--ob-blue), var(--ob-blue));
  color: var(--ob-text-inverse);
  box-shadow: inset 0 1px 0 var(--ob-border-strong), 0 4px 12px rgb(var(--ob-blue-rgb) / 0.25);
}
.mac-icon-action--primary:disabled {
  background: var(--ob-surface-soft);
  border-color: var(--ob-border);
  color: var(--ob-text-disabled);
  box-shadow: none;
}
.mac-icon-action.is-loading {
  font-weight: 700;
}
.mac-toggle {
  position: relative;
  width: 46px;
  height: 26px;
  padding: 2px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: linear-gradient(180deg, var(--ob-text), var(--ob-border));
  box-shadow: inset 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 1px 0 var(--ob-border);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}
.mac-toggle.is-on {
  border-color: rgb(var(--ob-success-rgb) / 0.72);
  background: linear-gradient(180deg, var(--ob-success), var(--ob-success));
  box-shadow: inset 0 1px 0 var(--ob-border-strong), 0 3px 10px rgb(var(--ob-success-rgb) / 0.18);
}
.mac-toggle:disabled {
  cursor: wait;
  opacity: 0.7;
}
.mac-toggle__knob {
  display: grid;
  width: 20px;
  height: 20px;
  place-items: center;
  border-radius: 999px;
  background: var(--ob-surface);
  color: var(--ob-text-subtle);
  font-size: 11px;
  box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / 0.22), inset 0 1px 0 var(--ob-border);
  transform: translateX(0);
  transition: transform 0.18s cubic-bezier(.2,.8,.2,1);
}
.mac-toggle.is-on .mac-toggle__knob {
  transform: translateX(20px);
}

.mac-segmented {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  min-height: 34px;
  padding: 3px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: linear-gradient(180deg, rgb(var(--ob-surface-rgb) / 0.96), rgb(var(--ob-surface-rgb) / 0.82));
  box-shadow: inset 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.1), inset 0 1px 0 var(--ob-border);
}
.mac-segmented button {
  min-width: 76px;
  height: 26px;
  padding: 0 11px;
  border-radius: 999px;
  color: var(--ob-text-subtle);
  font-size: 12px;
  transition: color 0.16s ease, background 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.mac-segmented button:not(:disabled):hover {
  color: var(--ob-text);
}
.mac-segmented button.is-active {
  background: rgb(var(--ob-surface-rgb) / 0.96);
  color: var(--ob-text-strong);
  box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / 0.12), inset 0 1px 0 var(--ob-border);
}
.mac-segmented.is-saving {
  opacity: 0.72;
}
.event-multi {
  display: flex;
  max-width: 420px;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}
.event-multi button {
  display: inline-flex;
  min-height: 30px;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: rgb(var(--ob-surface-rgb) / 0.92);
  color: var(--ob-text-subtle);
  font-size: 11px;
  transition: border-color .16s ease, background .16s ease, color .16s ease;
}
.event-multi button span { width: 10px; color: var(--ob-text-muted); font-weight: 700; }
.event-multi button:hover:not(:disabled) { border-color: rgb(var(--ob-blue-rgb) / .36); color: var(--ob-text); }
.event-multi button.is-active {
  border-color: rgb(var(--ob-blue-rgb) / .3);
  background: rgb(var(--ob-blue-rgb) / .08);
  color: var(--ob-blue);
}
.event-multi button.is-active span { color: var(--ob-blue); }
.event-multi.is-saving { opacity: .65; }

.settings-builtin {
  border-radius: 999px;
  background: rgb(var(--ob-success-rgb) / .1);
  padding: 2px 7px;
  color: var(--ob-success);
}
.prompt-preview-note {
  margin-bottom: 10px;
  color: var(--ob-text-subtle);
  font-size: 12px;
}
.prompt-preview-output {
  max-height: 68vh;
  overflow: auto;
  margin: 0;
  border: 1px solid var(--ob-border);
  border-radius: 12px;
  background: var(--ob-surface);
  padding: 14px;
  white-space: pre-wrap;
  color: var(--ob-text);
  font: 12px/1.65 ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (max-width: 980px) {
  .settings-toolbar { padding-inline: 18px; }
  .settings-layout {
    grid-template-columns: 220px minmax(0, 1fr);
    gap: 12px;
    padding: 12px;
  }
  .settings-domain__copy small { display: none; }
  .settings-row__main { grid-template-columns: minmax(0, 1fr) minmax(190px, 280px); }
  .settings-summary { display: none; }
}

@media (max-width: 760px) {
  .settings-shell { display: flex; min-width: 0; overflow: hidden; overflow-wrap: anywhere; }
  .settings-toolbar > div:first-child { display: none; }
  .settings-domain-picker { display: flex; align-items: center; gap: 10px; position: relative; }
  .settings-domain-picker > span { flex: none; color: var(--settings-muted); }
  .settings-domain-picker select { appearance: none; min-width: 0; width: 100%; min-height: 32px; padding: 0 34px 0 10px; border: 1px solid var(--settings-line); border-radius: 9px; background: transparent; color: inherit; }
  .settings-domain-picker svg { position: absolute; right: 12px; width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.5; pointer-events: none; }
  .settings-sidebar .settings-domain-list { display: none; }
  .settings-prompt-card > div:first-child > div:last-child { flex-wrap: wrap; gap: 8px; }
  .settings-technical { min-width: 0; overflow-wrap: anywhere; }
  .settings-technical code { white-space: normal; }
  .settings-intro h2 { overflow-wrap: anywhere; }
  .settings-intro__mark { flex-shrink: 0; }
  .settings-section__header { flex-wrap: wrap; gap: 8px; }
  .mac-segmented { flex-wrap: wrap; }
  .mac-segmented button { min-height: 40px; white-space: normal; }
  .mac-text-button { min-height: 40px; }
  .settings-prompt-editor { max-width: 100%; }
  .settings-refresh { min-width: 32px; min-height: 32px; width: 32px; height: 32px; }
  .settings-search { min-height: 32px; height: 32px; }
  .settings-toolbar {
    position: relative;
    z-index: 1;
    min-height: auto;
    flex-direction: column;
    align-items: stretch;
    gap: 6px;
    padding: 6px 12px;
  }
  .settings-toolbar__actions { width: 100%; }
  .settings-search { width: auto; flex: 1; }
  .settings-layout {
    display: flex;
    flex: 1 1 0%;
    min-height: 0;
    flex-direction: column;
    overflow: hidden;
    gap: 0;
    padding: 0 10px;
  }
  .settings-sidebar {
    flex: none;
    margin-bottom: 6px;
    padding: 6px 0;
    border-radius: 0;
    box-shadow: none;
  }
  .settings-sidebar__eyebrow,
  .settings-sidebar__footer { display: none; }
  .settings-domain-list {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 2px;
  }
  .settings-domain {
    width: auto;
    min-width: max-content;
    grid-template-columns: 26px auto;
    margin: 0;
    padding: 7px 10px 7px 7px;
    border-radius: 12px;
  }
  .settings-domain:hover { transform: none; }
  .settings-domain.is-active { box-shadow: inset 0 -2px 0 var(--settings-blue); }
  .settings-domain__icon { width: 26px; height: 26px; border-radius: 8px; font-size: 12px; }
  .settings-domain__copy small,
  .settings-domain__count { display: none; }
  .settings-content { flex: 1 1 0%; min-height: 0; overflow-y: auto; padding: 0 0 12px; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  .settings-content::-webkit-scrollbar { display: none; }
  .settings-intro { padding: 13px; border-radius: 16px; }
  .settings-intro__mark { width: 36px; height: 36px; border-radius: 11px; }
  .settings-section { border-radius: 16px; }
  .settings-row { padding: 13px; }
  .settings-row__main { display: block; }
  .setting-control { margin-top: 11px; justify-content: flex-start; }
  .setting-control > div { width: 100%; justify-content: space-between; }
  .value-pill,
  .editor-bar { width: 100%; }
  .mac-segmented { width: 100%; }
  .mac-segmented button { flex: 1; }
  .settings-prompt-card > div:first-child { display: block; }
  .settings-prompt-card > div:first-child > div:last-child { margin-top: 12px; }
  .settings-prompt-editor { min-height: 0; height: auto; }
}
</style>

<style>
/* OpenBear system dark theme */
html.dark .settings-toolbar {
		border-bottom: 1px solid var(--ob-border);
		background: var(--ob-surface);
		box-shadow: 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .settings-search {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.78);
		color: var(--ob-text-subtle);
		box-shadow: inset 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.16), 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .settings-search:focus-within {
		border-color: rgb(var(--ob-blue-rgb) / 0.48);
		background: rgb(var(--ob-surface-soft-rgb) / 0.95);
	}
html.dark .settings-search input {
		color: var(--ob-text-strong);
	}
html.dark .settings-search button {
		background: var(--ob-surface-raised);
	}
html.dark .settings-refresh {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.78);
		color: var(--ob-text);
	}
html.dark .settings-refresh:hover:not(:disabled) {
		box-shadow: 0 5px 14px rgb(var(--ob-shadow-rgb) / 0.16);
	}
html.dark .settings-sidebar {
		background: var(--ob-surface);
		box-shadow: 0 10px 28px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .settings-sidebar__eyebrow {
		color: var(--ob-text-subtle);
	}
html.dark .settings-domain:hover {
		border-color: rgb(var(--ob-blue-rgb) / 0.18);
		background: var(--ob-surface);
	}
html.dark .settings-domain.is-active {
		border-color: rgb(var(--ob-blue-rgb) / 0.18);
		background: linear-gradient(135deg, rgb(var(--ob-surface-soft-rgb) / 0.98), rgb(var(--ob-surface-soft-rgb) / 0.93));
	}
html.dark .settings-domain__icon {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.86);
		color: var(--ob-text);
		box-shadow: 0 2px 7px rgb(var(--ob-shadow-rgb) / 0.16);
	}
html.dark .settings-domain.is-active .settings-domain__icon {
		border-color: rgb(var(--ob-blue-rgb) / 0.2);
	}
html.dark .settings-domain__copy strong {
		color: var(--ob-text-strong);
	}
html.dark .settings-domain__copy small {
		color: var(--ob-text-subtle);
	}
html.dark .settings-domain__count {
		background: rgb(var(--ob-surface-soft-rgb) / 0.72);
		color: var(--ob-text);
	}
html.dark .settings-sidebar__footer {
		border-top: 1px solid var(--ob-border);
		color: var(--ob-text-subtle);
	}
html.dark .settings-intro {
		background: var(--ob-surface);
		box-shadow: 0 6px 18px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .settings-intro__mark {
		box-shadow: 0 9px 22px rgb(var(--ob-blue-rgb) / 0.25), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .settings-count-chip,
html.dark .settings-domain-label {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.8);
		color: var(--ob-text);
	}
html.dark .settings-summary span {
		background: rgb(var(--ob-surface-soft-rgb) / 0.85);
		color: var(--ob-text);
	}
html.dark .settings-summary b {
		color: var(--ob-text);
	}
html.dark .settings-summary .has-dirty {
		color: var(--ob-blue);
	}
html.dark .settings-summary .has-dirty b {
		color: var(--ob-blue);
	}
html.dark .settings-section {
		background: var(--ob-surface);
		box-shadow: 0 6px 18px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .settings-section__header {
		border-bottom: 1px solid var(--ob-border);
		background: linear-gradient(180deg, rgb(var(--ob-surface-soft-rgb) / 0.88), rgb(var(--ob-surface-soft-rgb) / 0.58));
	}
html.dark .settings-section__header p {
		color: var(--ob-text-subtle);
	}
html.dark .settings-list {
		background: rgb(var(--ob-surface-soft-rgb) / 0.5);
	}
html.dark .settings-row {
		border-bottom: 1px solid var(--ob-border);
	}
html.dark .settings-row:hover {
		background: rgb(var(--ob-surface-soft-rgb) / 0.76);
	}
html.dark .settings-row.is-dirty {
		box-shadow: inset 3px 0 0 rgb(var(--ob-blue-rgb) / 0.45);
	}
html.dark .settings-row h4 {
		color: var(--ob-text-strong);
	}
html.dark .settings-sensitive {
		color: var(--ob-danger);
	}
html.dark .settings-details-button {
		color: var(--ob-text-subtle);
	}
html.dark .settings-details-button:hover {
		color: var(--ob-text);
	}
html.dark .settings-technical {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.72);
		color: var(--ob-text-subtle);
	}
html.dark .settings-technical > code {
		color: var(--ob-text);
	}
html.dark .settings-effect {
		background: rgb(var(--ob-surface-soft-rgb) / 0.09);
		color: var(--ob-text);
	}
html.dark .settings-effect.is-success {
		color: var(--ob-success);
	}
html.dark .settings-effect.is-warning {
		color: var(--ob-warning);
	}
html.dark .settings-empty {
		border: 1px dashed var(--ob-border-strong);
		background: rgb(var(--ob-surface-soft-rgb) / 0.5);
		color: var(--ob-text-subtle);
	}
html.dark .settings-empty strong {
		color: var(--ob-text);
	}
html.dark .settings-prompt-editor {
		background: var(--ob-surface);
		box-shadow: 0 12px 36px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 0 0 1px rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .mac-text-button {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.88);
		color: var(--ob-text);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11), 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.16);
	}
html.dark .mac-text-button:hover:not(:disabled) {
		border-color: rgb(var(--ob-blue-rgb) / 0.32);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11), 0 6px 16px rgb(var(--ob-shadow-rgb) / 0.16);
	}
html.dark .mac-text-button.is-primary {
		border-color: rgb(var(--ob-blue-rgb) / 0.52);
		color: var(--ob-text-inverse);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11), 0 5px 16px rgb(var(--ob-blue-rgb) / 0.22);
	}
html.dark .mac-text-button.is-primary:disabled {
		border-color: var(--ob-border);
		background: var(--ob-surface-raised);
	}
html.dark .setting-state-text {
		color: var(--ob-text);
	}
html.dark .value-pill {
		border: 1px solid var(--ob-border);
		background: linear-gradient(180deg, rgb(var(--ob-surface-soft-rgb) / 0.94), rgb(var(--ob-surface-soft-rgb) / 0.86));
		color: var(--ob-text-strong);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11),
	    0 1px 2px rgb(var(--ob-shadow-rgb) / 0.16);
	}
html.dark .value-pill:hover {
		border-color: rgb(var(--ob-blue-rgb) / 0.36);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11),
	    0 4px 14px rgb(var(--ob-blue-rgb) / 0.09);
	}
html.dark .value-pill__edit {
		background: rgb(var(--ob-surface-soft-rgb) / 0.82);
		color: var(--ob-text);
		box-shadow: inset 0 0 0 1px rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .editor-bar {
		border: 1px solid rgb(var(--ob-blue-rgb) / 0.28);
		background: linear-gradient(180deg, rgb(var(--ob-surface-soft-rgb) / 0.98), rgb(var(--ob-surface-soft-rgb) / 0.94));
		box-shadow: 0 0 0 3px rgb(var(--ob-blue-rgb) / 0.06),
	    inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11),
	    0 6px 22px rgb(var(--ob-blue-rgb) / 0.08);
	}
html.dark .editor-hint {
		color: var(--ob-text-subtle);
	}
html.dark .setting-input .el-input__inner {
		color: var(--ob-text-strong);
	}
html.dark .setting-input .el-input__inner::placeholder {
		color: var(--ob-text-subtle);
	}
html.dark .mac-icon-action {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.9);
		color: var(--ob-text);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11), 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.16);
	}
html.dark .mac-icon-action:hover:not(:disabled) {
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11), 0 5px 14px rgb(var(--ob-shadow-rgb) / 0.16);
	}
html.dark .mac-icon-action--primary {
		border-color: rgb(var(--ob-blue-rgb) / 0.52);
		color: var(--ob-text-inverse);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11), 0 4px 12px rgb(var(--ob-blue-rgb) / 0.25);
	}
html.dark .mac-icon-action--primary:disabled {
		background: linear-gradient(180deg, var(--ob-surface-raised), var(--ob-surface-raised));
		border-color: var(--ob-border);
	}
html.dark .mac-toggle {
		border: 1px solid var(--ob-border);
		background: linear-gradient(180deg, var(--ob-text-strong), var(--ob-surface-raised));
		box-shadow: inset 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .mac-toggle.is-on {
		border-color: rgb(var(--ob-success-rgb) / 0.52);
		box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11), 0 3px 10px rgb(var(--ob-success-rgb) / 0.18);
	}
html.dark .mac-toggle__knob {
		background: var(--ob-surface);
		color: var(--ob-text);
		box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / 0.22), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .mac-segmented {
		border: 1px solid var(--ob-border);
		background: linear-gradient(180deg, rgb(var(--ob-surface-soft-rgb) / 0.96), rgb(var(--ob-surface-soft-rgb) / 0.82));
		box-shadow: inset 0 1px 2px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .mac-segmented button {
		color: var(--ob-text);
	}
html.dark .mac-segmented button:not(:disabled):hover {
		color: var(--ob-text-strong);
	}
html.dark .mac-segmented button.is-active {
		background: rgb(var(--ob-surface-soft-rgb) / 0.96);
		color: var(--ob-text-strong);
		box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / 0.16), inset 0 1px 0 rgb(var(--ob-border-rgb) / 0.11);
	}
html.dark .event-multi button {
		border: 1px solid var(--ob-border);
		background: rgb(var(--ob-surface-soft-rgb) / 0.92);
		color: var(--ob-text);
	}
html.dark .event-multi button span {
		color: var(--ob-text-subtle);
	}
html.dark .event-multi button:hover:not(:disabled) {
		border-color: rgb(var(--ob-blue-rgb) / 0.36);
		color: var(--ob-text-strong);
	}
html.dark .event-multi button.is-active {
		border-color: rgb(var(--ob-blue-rgb) / 0.3);
		background: rgb(var(--ob-blue-rgb) / 0.08);
		color: var(--ob-blue);
	}
html.dark .event-multi button.is-active span {
		color: var(--ob-blue);
	}
html.dark .settings-builtin {
		background: rgb(var(--ob-success-rgb) / 0.1);
		color: var(--ob-success);
	}
html.dark .prompt-preview-note {
		color: var(--ob-text);
	}
html.dark .prompt-preview-output {
		border: 1px solid var(--ob-border);
		background: var(--ob-surface);
		color: var(--ob-text-strong);
	}
</style>
