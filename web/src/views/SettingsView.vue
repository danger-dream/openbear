<script setup>
import { computed, defineAsyncComponent, nextTick, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { Api, apiError } from "../api";

const MdEditor = defineAsyncComponent(() => import("../components/MdEditor.vue"));

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

function normalizeDraftValue(spec, value) {
  if (spec?.kind === "bool") return Boolean(value);
  if (spec?.kind === "multi") return Array.isArray(value) ? [...value] : [];
  if (isPromptEditorSpec(spec) && (value === undefined || value === null || value === "")) return builtinPrompt(spec);
  if (value === undefined || value === null) return "";
  return value;
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
  return `${value}${spec.unit || ""}`;
}
function isDirty(spec) {
  if (!spec) return false;
  if (isPromptEditorSpec(spec)) {
    return String(draft[spec.path] || "") !== String(values.value[spec.path] || builtinPrompt(spec));
  }
  return JSON.stringify(draft[spec.path]) !== JSON.stringify(values.value[spec.path]);
}
function inputPlaceholder(spec) {
  if (spec.sensitive) return "留空不修改，输入新值后保存";
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
    const [specData, settingsData] = await Promise.all([Api.settingsSpecs(), Api.settings()]);
    okOrThrow(specData); okOrThrow(settingsData);
    const apiDomains = (specData.domains || []).filter((domain) => (domain.sections || []).length > 0);
    domains.value = apiDomains.length
      ? apiDomains
      : (specData.groups || []).map((group) => ({ ...group, sections: [group] }));
    const visiblePaths = new Set(domains.value.flatMap((domain) =>
      (domain.sections || []).flatMap((section) => section.paths || [])));
    specs.value = Object.fromEntries(Object.entries(specData.specs || {}).filter(([path]) => visiblePaths.has(path)));
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
    const data = await Api.updateSetting(spec.path, overrideValue === undefined ? draft[spec.path] : overrideValue);
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
                isPromptEditorSpec(spec) ? 'is-prompt' : '',
              ]"
            >
              <template v-if="isPromptEditorSpec(spec)">
                <div class="compact-prompt-card">
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
                  <div v-if="isPromptEditing(spec)" class="compact-prompt-editor mt-4">
                    <MdEditor v-model="draft[spec.path]" completion-mode="none" square />
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
                          />
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
                  <span v-if="spec.min !== null || spec.max !== null">范围 {{ spec.min ?? '—' }} ～ {{ spec.max ?? '—' }}</span>
                </div>
              </template>
            </article>
          </div>
        </section>
      </main>
    </div>

    <el-dialog v-model="promptPreviewOpen" class="mac-dialog" :title="promptPreviewTitle" width="860px" top="7vh" append-to-body>
      <div class="prompt-preview-note">使用示例变量渲染；保存时仍会再次执行相同的占位符校验。</div>
      <pre class="prompt-preview-output">{{ promptPreviewText }}</pre>
      <template #footer>
        <button type="button" class="mac-text-button is-primary" @click="promptPreviewOpen = false">关闭</button>
      </template>
    </el-dialog>
  </div>
</template>


<style scoped>
.settings-shell {
  --settings-ink: #18181b;
  --settings-muted: #71717a;
  --settings-line: rgba(212, 212, 216, 0.78);
  --settings-blue: #007aff;
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  color: var(--settings-ink);
  background:
    radial-gradient(circle at 82% 3%, rgba(190, 219, 255, 0.34), transparent 28%),
    linear-gradient(145deg, #f7f7f8 0%, #f2f4f7 48%, #edf2f8 100%);
}
.settings-toolbar {
  display: flex;
  min-height: 74px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 28px;
  border-bottom: 1px solid rgba(212, 212, 216, 0.72);
  background: #ffffff;
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.75);
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
  background: #34c759;
  box-shadow: 0 0 0 4px rgba(52, 199, 89, 0.11);
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
  border: 1px solid rgba(212, 212, 216, 0.92);
  border-radius: 12px;
  background: rgba(250, 250, 250, 0.78);
  color: #a1a1aa;
  box-shadow: inset 0 1px 2px rgba(24, 24, 27, 0.04), 0 1px 0 rgba(255, 255, 255, 0.9);
  transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
}
.settings-search:focus-within {
  border-color: rgba(0, 122, 255, 0.48);
  background: rgba(255, 255, 255, 0.95);
  box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.08);
}
.settings-search input {
  min-width: 0;
  flex: 1;
  border: 0;
  outline: 0;
  background: transparent;
  color: #27272a;
  font-size: 12px;
}
.settings-search input::-webkit-search-cancel-button { display: none; }
.settings-search button {
  display: grid;
  width: 18px;
  height: 18px;
  place-items: center;
  border-radius: 99px;
  background: #d4d4d8;
  color: white;
  font-size: 13px;
  line-height: 1;
}
.settings-refresh {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border: 1px solid rgba(212, 212, 216, 0.9);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.78);
  color: #52525b;
  font-size: 18px;
  transition: transform .18s ease, box-shadow .18s ease;
}
.settings-refresh:hover:not(:disabled) {
  transform: rotate(18deg);
  box-shadow: 0 5px 14px rgba(24, 24, 27, 0.08);
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
  background: #f8f8fa;
  box-shadow: 0 10px 28px rgba(24, 24, 27, 0.055), inset 0 1px 0 #fff;
}
.settings-sidebar__eyebrow {
  padding: 6px 10px 10px;
  color: #a1a1aa;
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
  border-color: rgba(0, 122, 255, 0.18);
  background: #ffffff;
}
.settings-domain.is-active {
  border-color: rgba(0, 122, 255, 0.18);
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.98), rgba(235, 244, 255, 0.93));
  box-shadow: 0 8px 20px rgba(0, 85, 190, 0.09), inset 3px 0 0 var(--settings-blue);
}
.settings-domain__icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid rgba(228, 228, 231, 0.86);
  border-radius: 11px;
  background: rgba(255, 255, 255, 0.86);
  color: #52525b;
  font-size: 16px;
  box-shadow: 0 2px 7px rgba(24, 24, 27, 0.06);
}
.settings-domain.is-active .settings-domain__icon {
  border-color: rgba(0, 122, 255, 0.2);
  color: var(--settings-blue);
}
.settings-domain__copy { min-width: 0; }
.settings-domain__copy strong {
  display: block;
  color: #27272a;
  font-size: 14px;
  font-weight: 650;
}
.settings-domain__copy small {
  display: block;
  overflow: hidden;
  margin-top: 2px;
  color: #8b8b92;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.settings-domain__count {
  min-width: 24px;
  padding: 2px 6px;
  border-radius: 99px;
  background: rgba(228, 228, 231, 0.72);
  color: #71717a;
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
  border-top: 1px solid rgba(228, 228, 231, 0.8);
  color: #a1a1aa;
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
  background: #ffffff;
  box-shadow: 0 6px 18px rgba(24, 24, 27, 0.045), inset 0 1px 0 #fff;
}
.settings-intro__mark {
  display: grid;
  width: 42px;
  height: 42px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 14px;
  background: linear-gradient(145deg, #198bff, #0064d8);
  color: white;
  font-size: 19px;
  box-shadow: 0 9px 22px rgba(0, 122, 255, 0.25), inset 0 1px 0 rgba(255, 255, 255, 0.34);
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
  border: 1px solid rgba(212, 212, 216, 0.8);
  border-radius: 99px;
  background: rgba(244, 244, 245, 0.8);
  color: #71717a;
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
  background: rgba(244, 244, 245, 0.85);
  color: #71717a;
  font-size: 11px;
}
.settings-summary b { color: #3f3f46; }
.settings-summary .has-dirty {
  background: rgba(0, 122, 255, 0.09);
  color: #0068d9;
}
.settings-summary .has-dirty b { color: #0068d9; }
.settings-section {
  overflow: hidden;
  margin-bottom: 14px;
  border: 1px solid var(--settings-line);
  border-radius: 18px;
  background: #ffffff;
  box-shadow: 0 6px 18px rgba(24, 24, 27, 0.04), inset 0 1px 0 #fff;
}
.settings-section__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 17px 11px;
  border-bottom: 1px solid rgba(228, 228, 231, 0.78);
  background: linear-gradient(180deg, rgba(250, 250, 250, 0.88), rgba(247, 247, 248, 0.58));
}
.settings-section__header h3 {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: -.01em;
}
.settings-section__header p {
  margin-top: 2px;
  color: #a1a1aa;
  font-size: 11px;
}
.settings-list { background: rgba(255, 255, 255, 0.5); }
.settings-row {
  position: relative;
  padding: 16px 17px 13px;
  border-bottom: 1px solid rgba(228, 228, 231, 0.72);
  transition: background-color .12s ease;
}
.settings-row:last-child { border-bottom: 0; }
.settings-row:hover { background: rgba(249, 250, 252, 0.76); }
.settings-row.is-dirty {
  background: linear-gradient(90deg, rgba(0, 122, 255, 0.055), transparent 72%);
  box-shadow: inset 3px 0 0 rgba(0, 122, 255, 0.72);
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
  color: #27272a;
  font-size: 14px;
  font-weight: 650;
}
.settings-row__copy > p,
.compact-prompt-card > div:first-child p {
  max-width: 720px;
  margin-top: 4px;
  color: var(--settings-muted);
  font-size: 12px;
  line-height: 1.65;
}
.settings-sensitive {
  padding: 2px 6px;
  border-radius: 99px;
  background: rgba(255, 59, 48, 0.09);
  color: #d92d20;
  font-size: 10px;
  font-weight: 700;
}
.settings-details-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 7px;
  color: #a1a1aa;
  font-size: 9px;
  transition: color .14s ease;
}
.settings-details-button:hover { color: #52525b; }
.settings-details-button span:last-child { transition: transform .16s ease; }
.settings-details-button span.is-open { transform: rotate(180deg); }
.settings-technical {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid rgba(228, 228, 231, 0.72);
  border-radius: 10px;
  background: rgba(244, 244, 245, 0.72);
  color: #8b8b92;
  font-size: 11px;
}
.settings-technical > code {
  color: #52525b;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.settings-effect {
  padding: 2px 6px;
  border-radius: 99px;
  background: rgba(113, 113, 122, 0.09);
  color: #71717a;
  font-size: 10px;
  font-weight: 650;
}
.settings-effect.is-success { background: rgba(52, 199, 89, 0.1); color: #23833f; }
.settings-effect.is-warning { background: rgba(255, 159, 10, 0.12); color: #a65d00; }
.settings-empty {
  display: grid;
  min-height: 300px;
  place-items: center;
  align-content: center;
  gap: 7px;
  border: 1px dashed rgba(161, 161, 170, 0.6);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.5);
  color: #a1a1aa;
  text-align: center;
}
.settings-empty > div { font-size: 28px; }
.settings-empty strong { color: #52525b; font-size: 12px; }
.settings-empty p { font-size: 10px; }
.setting-control {
  display: flex;
  justify-content: flex-end;
}
.setting-control {
  display: flex;
  justify-content: flex-end;
}
.compact-prompt-card {
  display: block;
}
.compact-prompt-editor {
  height: min(58vh, 520px);
  min-height: 360px;
  overflow: hidden;
  border-radius: 0;
  background: #fff;
  box-shadow: 0 12px 36px rgba(24, 24, 27, 0.08), inset 0 0 0 1px rgba(228, 228, 231, 0.9);
}
.mac-text-button {
  height: 30px;
  padding: 0 11px;
  border: 1px solid rgba(212, 212, 216, 0.9);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.88);
  color: #52525b;
  font-size: 12px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.96), 0 1px 2px rgba(24, 24, 27, 0.04);
  transition: transform 0.14s ease, box-shadow 0.14s ease, border-color 0.14s ease, opacity 0.14s ease;
}
.mac-text-button:hover:not(:disabled) {
  transform: translateY(-1px);
  border-color: rgba(0, 122, 255, 0.32);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.96), 0 6px 16px rgba(24, 24, 27, 0.08);
}
.mac-text-button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.mac-text-button.is-primary {
  border-color: rgba(0, 122, 255, 0.68);
  background: linear-gradient(180deg, #1c8dff, #007aff);
  color: #fff;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.34), 0 5px 16px rgba(0, 122, 255, 0.22);
}
.mac-text-button.is-primary:disabled {
  border-color: rgba(212, 212, 216, 0.9);
  background: #d4d4d8;
  box-shadow: none;
}
.setting-state-text {
  min-width: 2.25rem;
  color: #71717a;
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
  border: 1px solid rgba(212, 212, 216, 0.95);
  border-radius: 999px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(244, 244, 245, 0.86));
  color: #27272a;
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.95),
    0 1px 2px rgba(24, 24, 27, 0.04);
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.value-pill:hover {
  border-color: rgba(0, 122, 255, 0.36);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.98),
    0 4px 14px rgba(0, 122, 255, 0.09);
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
  background: rgba(255, 255, 255, 0.82);
  color: #71717a;
  font-size: 12px;
  box-shadow: inset 0 0 0 1px rgba(228, 228, 231, 0.9);
}
.editor-bar {
  display: flex;
  align-items: center;
  width: min(360px, 100%);
  gap: 5px;
  padding: 4px;
  border: 1px solid rgba(0, 122, 255, 0.28);
  border-radius: 16px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.98), rgba(246, 250, 255, 0.94));
  box-shadow:
    0 0 0 3px rgba(0, 122, 255, 0.06),
    inset 0 1px 0 rgba(255, 255, 255, 0.95),
    0 6px 22px rgba(0, 53, 128, 0.08);
}
.editor-hint {
  padding-right: 12px;
  text-align: right;
  font-size: 10px;
  line-height: 1;
  color: #a1a1aa;
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
  color: #18181b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
}
:deep(.setting-input .el-input__inner::placeholder) {
  color: #a1a1aa;
}
.mac-icon-action {
  display: grid;
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  place-items: center;
  border: 1px solid rgba(212, 212, 216, 0.92);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.9);
  color: #52525b;
  font-size: 13px;
  line-height: 1;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.95), 0 1px 2px rgba(24, 24, 27, 0.05);
  transition: transform 0.14s ease, box-shadow 0.14s ease, opacity 0.14s ease;
}
.mac-icon-action:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.95), 0 5px 14px rgba(24, 24, 27, 0.1);
}
.mac-icon-action:disabled {
  cursor: not-allowed;
  opacity: 0.42;
}
.mac-icon-action--primary {
  border-color: rgba(0, 122, 255, 0.62);
  background: linear-gradient(180deg, #1c8dff, #007aff);
  color: #fff;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.35), 0 4px 12px rgba(0, 122, 255, 0.25);
}
.mac-icon-action--primary:disabled {
  background: linear-gradient(180deg, #d4d4d8, #c4c4cc);
  border-color: rgba(212, 212, 216, 0.9);
  color: rgba(255, 255, 255, 0.88);
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
  border: 1px solid rgba(212, 212, 216, 0.9);
  border-radius: 999px;
  background: linear-gradient(180deg, #e4e4e7, #d4d4d8);
  box-shadow: inset 0 1px 2px rgba(63, 63, 70, 0.16), inset 0 1px 0 rgba(255, 255, 255, 0.72);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}
.mac-toggle.is-on {
  border-color: rgba(52, 199, 89, 0.72);
  background: linear-gradient(180deg, #44d36f, #34c759);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.38), 0 3px 10px rgba(52, 199, 89, 0.18);
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
  background: #fff;
  color: #71717a;
  font-size: 11px;
  box-shadow: 0 1px 3px rgba(24, 24, 27, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.9);
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
  border: 1px solid rgba(212, 212, 216, 0.95);
  border-radius: 999px;
  background: linear-gradient(180deg, rgba(244, 244, 245, 0.96), rgba(228, 228, 231, 0.82));
  box-shadow: inset 0 1px 2px rgba(63, 63, 70, 0.1), inset 0 1px 0 rgba(255, 255, 255, 0.8);
}
.mac-segmented button {
  min-width: 76px;
  height: 26px;
  padding: 0 11px;
  border-radius: 999px;
  color: #71717a;
  font-size: 12px;
  transition: color 0.16s ease, background 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.mac-segmented button:not(:disabled):hover {
  color: #27272a;
}
.mac-segmented button.is-active {
  background: rgba(255, 255, 255, 0.96);
  color: #18181b;
  box-shadow: 0 1px 3px rgba(24, 24, 27, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.9);
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
  border: 1px solid rgba(212, 212, 216, 0.92);
  border-radius: 999px;
  background: rgba(250, 250, 250, 0.92);
  color: #71717a;
  font-size: 11px;
  transition: border-color .16s ease, background .16s ease, color .16s ease;
}
.event-multi button span { width: 10px; color: #a1a1aa; font-weight: 700; }
.event-multi button:hover:not(:disabled) { border-color: rgba(0, 122, 255, .36); color: #27272a; }
.event-multi button.is-active {
  border-color: rgba(0, 122, 255, .3);
  background: rgba(0, 122, 255, .08);
  color: #0068d9;
}
.event-multi button.is-active span { color: #007aff; }
.event-multi.is-saving { opacity: .65; }

.settings-builtin {
  border-radius: 999px;
  background: rgba(52, 199, 89, .1);
  padding: 2px 7px;
  color: #248a3d;
}
.prompt-preview-note {
  margin-bottom: 10px;
  color: #71717a;
  font-size: 12px;
}
.prompt-preview-output {
  max-height: 68vh;
  overflow: auto;
  margin: 0;
  border: 1px solid rgba(212, 212, 216, .82);
  border-radius: 12px;
  background: #fafafa;
  padding: 14px;
  white-space: pre-wrap;
  color: #27272a;
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

@media (max-width: 720px) {
  .settings-shell { overflow-y: auto; }
  .settings-toolbar {
    position: sticky;
    z-index: 8;
    top: 0;
    min-height: auto;
    flex-direction: column;
    align-items: stretch;
    gap: 11px;
    padding: 13px 14px;
  }
  .settings-toolbar__actions { width: 100%; }
  .settings-search { width: auto; flex: 1; }
  .settings-layout {
    display: block;
    overflow: visible;
    padding: 10px;
  }
  .settings-sidebar {
    margin-bottom: 10px;
    padding: 8px;
    border-radius: 16px;
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
  .settings-content { overflow: visible; padding: 0 0 24px; }
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
  .compact-prompt-card > div:first-child { display: block; }
  .compact-prompt-card > div:first-child > div:last-child { margin-top: 12px; }
  .compact-prompt-editor { min-height: 300px; height: 52vh; }
}

</style>
