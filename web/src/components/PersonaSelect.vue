<script setup>
import {computed, onMounted} from 'vue';
import {defaultPromptTemplateName, loadPromptTemplateChoices, normalizePromptTemplate, promptTemplateChoices, promptTemplateName} from '../views/consoleView/promptTemplate.js';

// 人设 in folder and conversation properties. value is a template id ('' for the
// active main template) or null when the folder inherits; change reports the same.
const INHERIT = '__inherit';
const props = defineProps({
  value: {type: String, default: null},
  inherited: {type: String, default: ''},
  inheritable: Boolean,
  inheritLabel: {type: String, default: '继承上级目录'},
  disabled: Boolean,
  overridden: Boolean,
  hint: {type: String, default: ''},
});
const emit = defineEmits(['change']);
const selection = computed(() => props.value === null ? INHERIT : normalizePromptTemplate(props.value));
const options = computed(() => {
  const ids = ['', ...promptTemplateChoices.items.map(item => item.id)];
  // A stored choice that is no longer offered stays visible so it is not silently replaced.
  for (const id of [props.value, props.inherited].map(normalizePromptTemplate)) if (id && !ids.includes(id)) ids.push(id);
  return ids.map(id => ({value: id, label: id ? promptTemplateName(id) : defaultPromptTemplateName()}));
});
const inheritText = computed(() => `${props.inheritLabel} · ${promptTemplateName(props.inherited)}`);
function changed(value) { emit('change', value === INHERIT ? null : normalizePromptTemplate(value)); }
onMounted(() => { loadPromptTemplateChoices({force: true}).catch(() => {}); });
</script>

<template>
  <div class="persona-select">
    <div class="persona-select-head">
      <h3>人设</h3>
      <p>选择这里的会话使用哪份系统提示词。人设是在「提示词模板」里打开了「作为人设」的模板。</p>
    </div>
    <el-select :model-value="selection" :disabled="disabled || overridden" aria-label="人设" popper-class="folder-properties-popover" :show-arrow="false" @change="changed">
      <el-option v-if="inheritable" :value="INHERIT" :label="inheritText" />
      <el-option v-for="item in options" :key="item.value" :value="item.value" :label="item.label" />
    </el-select>
    <p v-if="overridden" class="persona-select-note is-warning">已勾选「覆盖系统提示词」，以覆盖内容为准，人设不生效。</p>
    <p v-else-if="hint" class="persona-select-note">{{ hint }}</p>
    <p v-if="promptTemplateChoices.error" class="persona-select-note is-error" role="alert">人设列表读取失败，已选的仍会保留。</p>
  </div>
</template>

<style scoped>
.persona-select { display:flex; flex-direction:column; gap:8px; min-width:0; }
.persona-select-head h3 { margin:0; font-size:14px; font-weight:600; line-height:1.5; color:var(--fp-text, var(--ob-text)); }
.persona-select-head p { margin:2px 0 0; font-size:13px; line-height:1.5; color:var(--fp-secondary, var(--ob-text-subtle)); }
.persona-select-note { margin:0; font-size:12.5px; line-height:1.55; color:var(--fp-secondary, var(--ob-text-subtle)); }
.persona-select-note.is-warning { color:var(--ob-warning); }
.persona-select-note.is-error { color:var(--ob-danger); }
</style>
