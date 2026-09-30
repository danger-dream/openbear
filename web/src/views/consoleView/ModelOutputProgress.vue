<script setup>
import {computed, onBeforeUnmount, onMounted, ref} from 'vue';
import {SquareTerminal, Search, FileText, FilePenLine, PencilLine, Globe, History, BookOpen, Wrench, ListChecks} from '@lucide/vue';
import {modelOutputView} from './modelOutputPresentation.js';

const props = defineProps({progress: {type: Object, required: true}});
const now = ref(Date.now());
const view = computed(() => modelOutputView(props.progress, now.value));
const processIcon = computed(() => (props.progress.toolNames || []).length > 1 ? ListChecks : ({
  Bash: SquareTerminal, Read: FileText, Write: FilePenLine, Edit: PencilLine, EditBatch: PencilLine,
  Browser: Globe, History, Memory: BookOpen, TaskMemory: BookOpen,
  mcp__parrot__web_search: Search, mcp__parrot__web_fetch: Globe,
})[(props.progress.toolNames || [])[0]] || Wrench);
let timer;
onMounted(() => {timer = setInterval(() => {now.value = Date.now();}, 1000);});
onBeforeUnmount(() => {clearInterval(timer);});
</script>

<template>
  <div class="conversation-process model-output-progress" :title="view.hint"
       :aria-label="`${view.label}，已接收 ${view.bytes}，耗时 ${view.elapsed}；尚未执行工具`">
    <component :is="processIcon" class="process-icon" :stroke-width="1.75" aria-hidden="true"/>
    <span class="process-copy">
      <span class="process-kind" :class="{'work-status-sweep': !view.quiet}">{{ view.label }}</span>
      <span class="process-preview" :class="{'work-status-sweep': !view.quiet}">{{ view.tools ? `${view.tools} · ` : '' }}已接收 {{ view.bytes }} · 耗时 {{ view.elapsed }}</span>
      <span v-if="view.idle" class="process-meta">· {{ view.idle }}</span>
    </span>
  </div>
</template>

<style scoped>
/* Match ConversationProcessEvent's existing tool summary: no card, badge,
   separate activity track or background. The shared text sweep is the cue. */
.model-output-progress { display: flex; align-items: center; gap: 8px; max-width: 100%; min-width: 0; min-height: 24px; color: var(--work-muted, var(--ob-text-muted)); font-size: 14px; line-height: 1.65; }
.process-icon { width: 16px; height: 16px; flex: none; align-self: center; color: var(--work-faint, var(--ob-text-muted)); }
.process-copy { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0 8px; min-width: 0; }
.process-kind { flex: none; color: var(--work-faint, var(--ob-text-muted)); font-weight: 400; white-space: nowrap; }
.process-preview { color: var(--work-muted, var(--ob-text-muted)); font-variant-numeric: tabular-nums; }
.process-meta { color: var(--work-faint, var(--ob-text-muted)); font-size: 13px; }
</style>
