<script setup>
import { computed, ref } from 'vue';
import { NodeViewContent, NodeViewWrapper } from '@tiptap/vue-3';
import { Check, ChevronRight, Copy } from '@lucide/vue';

const props = defineProps({
  node: { type: Object, required: true },
  updateAttributes: { type: Function, required: true },
  editor: { type: Object, required: true },
});

const CODE_LANGUAGES = [
  ['', 'plain'],
  ['bash', 'bash'],
  ['sh', 'sh'],
  ['python', 'python'],
  ['javascript', 'javascript'],
  ['typescript', 'typescript'],
  ['go', 'go'],
  ['json', 'json'],
  ['yaml', 'yaml'],
  ['toml', 'toml'],
  ['dockerfile', 'dockerfile'],
  ['sql', 'sql'],
  ['html', 'html'],
  ['css', 'css'],
  ['vue', 'vue'],
  ['java', 'java'],
  ['rust', 'rust'],
  ['c', 'c'],
  ['cpp', 'cpp'],
  ['markdown', 'markdown'],
  ['xml', 'xml'],
  ['ini', 'ini'],
];

const copied = ref(false);
let copyTimer = 0;

const currentLang = computed({
  get: () => String(props.node.attrs?.language || '').toLowerCase(),
  set: (val) => props.updateAttributes({ language: val }),
});

const isEditable = computed(() => Boolean(props.editor?.isEditable));

const languageOptions = computed(() => {
  const cur = currentLang.value;
  if (!cur || CODE_LANGUAGES.some(([k]) => k === cur)) return CODE_LANGUAGES;
  return [[cur, cur], ...CODE_LANGUAGES];
});

const lineCount = computed(() => {
  const raw = String(props.node.textContent || '');
  return Math.max(1, raw.split('\n').length);
});

async function copyCode() {
  const text = props.node.textContent || '';
  try {
    await navigator.clipboard.writeText(text);
    copied.value = true;
    window.clearTimeout(copyTimer);
    copyTimer = window.setTimeout(() => { copied.value = false; }, 1600);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    copied.value = true;
    window.clearTimeout(copyTimer);
    copyTimer = window.setTimeout(() => { copied.value = false; }, 1600);
  }
}
</script>

<template>
  <NodeViewWrapper class="sy-code-block">
    <div class="sy-code-header" contenteditable="false">
      <span class="sy-code-dots" aria-hidden="true">
        <i class="is-red" />
        <i class="is-amber" />
        <i class="is-green" />
      </span>

      <div class="sy-code-actions">
        <label v-if="isEditable" class="sy-code-lang-wrap">
          <select
            v-model="currentLang"
            class="sy-code-lang-select"
            aria-label="代码块语言"
          >
            <option v-for="[val, label] in languageOptions" :key="val" :value="val">{{ label }}</option>
          </select>
          <ChevronRight :size="11" class="sy-code-lang-caret" />
        </label>
        <span v-else class="sy-code-lang-readonly">{{ currentLang || 'plain' }}</span>

        <button
          type="button"
          class="sy-code-copy-btn"
          :class="{ 'is-copied': copied }"
          :title="copied ? '已复制' : '复制代码'"
          :aria-label="copied ? '已复制' : '复制代码'"
          @mousedown.prevent
          @click="copyCode"
        >
          <Check v-if="copied" :size="13.5" />
          <Copy v-else :size="13.5" />
        </button>
      </div>
    </div>

    <div class="sy-code-body">
      <div class="sy-code-gutter" contenteditable="false" aria-hidden="true">
        <span v-for="n in lineCount" :key="n">{{ n }}</span>
      </div>
      <pre class="sy-code-pre hljs"><NodeViewContent as="code" /></pre>
    </div>
  </NodeViewWrapper>
</template>
