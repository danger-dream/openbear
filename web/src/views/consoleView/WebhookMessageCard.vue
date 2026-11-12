<script setup>
import {computed, onBeforeUnmount, ref, watch} from 'vue';
import {Message, ArrowRight, CopyDocument, Check} from '@element-plus/icons-vue';
import {ElMessage} from 'element-plus';
import {Api} from '../../api.js';
import {copyTextToClipboard} from '../../utils/clipboard.js';
import {eventMessageView, formatEventData, highlightEventData} from './webhookMessage.js';
const props = defineProps({message: {type: Object, required: true}});
const view = computed(() => eventMessageView(props.message));
const open = ref(false), tab = ref('event'), selected = ref(''), detail = ref(null), loading = ref(false), error = ref(''), copied = ref(false);
const time = value => value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '时间未知';
const arrival = computed(() => time(view.value.receivedAtMs));
let generation = 0, abort;
async function load() {
  const run = ++generation; abort?.abort(); detail.value = null; error.value = ''; loading.value = false;
  if (!open.value || tab.value !== 'event' || !selected.value) return;
  abort = new AbortController(); loading.value = true;
  try {
    const data = await Api.webhookEvent(selected.value, {signal: abort.signal});
    if (run === generation) detail.value = data.event;
  } catch (e) { if (run === generation && !abort.signal.aborted) error.value = e?.response?.data?.message || e.message; }
  finally { if (run === generation) loading.value = false; }
}
function show() {
  selected.value = view.value.events[0]?.eventId || ''; tab.value = selected.value ? 'event' : 'input'; open.value = true;
}
watch([open, tab, selected], load);
watch(() => props.message.opId, () => { open.value = false; });
onBeforeUnmount(() => { generation++; abort?.abort(); });
const body = computed(() => detail.value?.raw?.body ?? detail.value?.raw?.content ?? '');
async function copy() {
  const value = tab.value === 'input' ? props.message.content : formatEventData(body.value);
  try { await copyTextToClipboard(value); copied.value = true; }
  catch { ElMessage.warning('复制失败，请选中文本手动复制'); }
}
watch([tab, selected, open], () => { copied.value = false; });
</script>

<template>
  <button type="button" class="event-envelope" aria-haspopup="dialog" :aria-label="`查看事件：${view.name}`" @click="show">
    <span class="event-envelope-icon" aria-hidden="true"><el-icon><Message/></el-icon></span>
    <span class="event-envelope-main">
      <span class="event-envelope-label">事件来信 <span v-if="view.count > 1">· 合并 {{ view.count }} 条</span></span>
      <strong>{{ view.name }}</strong>
      <span v-if="view.summary" class="event-envelope-summary">{{ view.summary }}</span>
      <span class="event-envelope-meta"><time>{{ arrival }}</time><span v-if="view.shortId" class="event-envelope-id">#{{ view.shortId }}</span></span>
    </span>
    <el-icon class="event-envelope-arrow"><ArrowRight/></el-icon>
  </button>
  <el-dialog v-model="open" title="事件来信" width="min(850px, calc(100vw - 24px))" append-to-body class="mobile-viewport-dialog event-letter-dialog" destroy-on-close>
    <div class="event-letter-head"><strong>{{ view.name }}</strong><span>{{ arrival }} · #{{ view.shortId }}</span></div>
    <div class="event-letter-toolbar">
      <div class="event-letter-tabs" role="tablist" aria-label="事件内容">
        <button type="button" role="tab" :aria-selected="tab === 'event'" :disabled="!view.events.length" @click="tab = 'event'">事件材料</button>
        <button type="button" role="tab" :aria-selected="tab === 'input'" @click="tab = 'input'">模型输入</button>
      </div>
      <el-button text :disabled="loading || (tab === 'event' && !detail?.raw)" @click="copy"><el-icon><Check v-if="copied"/><CopyDocument v-else/></el-icon><span>{{ copied ? '已复制' : '复制内容' }}</span></el-button>
    </div>
    <div class="event-letter-content" role="tabpanel" tabindex="0">
      <template v-if="tab === 'event'">
        <label v-if="view.events.length > 1" class="event-letter-select">批次中的事件
          <select v-model="selected"><option v-for="(item, index) in view.events" :key="item.eventId" :value="item.eventId">{{ index + 1 }} · {{ time(item.receivedAtMs) }} · {{ item.summary?.slice(0, 48) || '#' + item.eventId.slice(0, 8) }}</option></select>
        </label>
        <p v-if="loading" role="status">正在读取事件材料…</p>
        <p v-else-if="error" role="alert">{{ error }} <el-button link @click="load">重试</el-button></p>
        <template v-else-if="detail?.raw">
          <h3>消息正文 <small>{{ detail.raw.contentType }}</small></h3>
          <pre class="event-json"><code v-html="highlightEventData(body)"></code></pre>
          <template v-if="detail.raw.query?.length"><h3>Query 参数</h3><pre class="event-json"><code v-html="highlightEventData(detail.raw.query)"></code></pre></template>
          <template v-if="detail.derived != null"><h3>前置处理材料</h3><pre class="event-json"><code v-html="highlightEventData(detail.derived)"></code></pre></template>
        </template>
        <p v-else class="event-letter-hint">原始事件材料已清理或不可用；仍可查看当时保存的模型输入。</p>
      </template>
      <template v-else><p class="event-letter-hint">当时发送的处理提示词与事件内容，原文保留。</p><pre class="event-json event-model-input"><code>{{ message.content }}</code></pre></template>
    </div>
  </el-dialog>
</template>

<style scoped src="./eventMessageCard.css"></style>
<style scoped>
.event-letter-head{display:flex;flex-direction:column;gap:6px;padding:0 2px 16px;overflow-wrap:anywhere;}
.event-letter-head strong{font-size:15px;}.event-letter-head span{font-size:11px;color:var(--ob-text-muted);}
.event-letter-toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid var(--ob-border);padding-bottom:8px;}
.event-letter-tabs{display:flex;gap:4px;}.event-letter-tabs button{border:0;border-radius:6px;background:transparent;color:var(--ob-text-subtle);padding:7px 12px;cursor:pointer;font-size:12px;}.event-letter-tabs button[aria-selected=true]{background:var(--el-color-primary-light-9);color:var(--el-color-primary);}.event-letter-tabs button:disabled{opacity:.45;cursor:default;}
.event-letter-toolbar .el-button span{margin-left:5px;font-size:12px;}
.event-letter-content{max-height:min(60vh,600px);min-height:130px;overflow:auto;padding:16px 2px 4px;overscroll-behavior:contain;}
.event-letter-content h3{font-size:12px;font-weight:600;margin:4px 0 10px;}.event-letter-content h3 small{font-weight:400;margin-left:8px;color:var(--ob-text-muted);}
.event-letter-select{display:flex;flex-direction:column;gap:6px;margin-bottom:16px;font-size:11px;color:var(--ob-text-subtle);}.event-letter-select select{max-width:100%;padding:8px;border:1px solid var(--ob-border);border-radius:6px;background:var(--el-bg-color);color:var(--ob-text);}
.event-json{margin:0 0 18px;padding:14px;border:1px solid var(--ob-border);border-radius:8px;background:var(--ob-bg-sunken,var(--el-fill-color-light));font-size:12px;line-height:1.75;tab-size:2;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ob-text);}
.event-json :deep(.hljs-attr){color:var(--el-color-primary);}.event-json :deep(.hljs-string){color:var(--el-color-success);}.event-json :deep(.hljs-number),.event-json :deep(.hljs-literal){color:var(--el-color-warning);}.event-json :deep(.hljs-punctuation){color:var(--ob-text-muted);}
.event-letter-hint{font-size:12px;line-height:1.7;color:var(--ob-text-subtle);margin:0 0 12px;}
@media(max-width:640px){.event-letter-content{max-height:56dvh;}.event-letter-tabs button{padding:10px;}.event-json{padding:10px;font-size:11px;}}
</style>
