<script setup>
import { Unlock } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { Api } from '../../api.js';
import { ok, relativeTime } from '../../instances/instanceHelpers.js';
import { regionName, safeError } from '../serverHelpers.js';
import { SPRITE } from '../serviceLogos.js';
import { useVisible } from './useVisible.js';
import { unlockList } from './unlockList.js';

// 服务解锁: every service the full check measured (Service Unlock, run on the node each time the cards'
// own checks run), with the cards' nine verdicts in their place; read once the card is scrolled to, and
// again every few seconds while a run is under way. Where no full result can be had, the cards' nine.
const props = defineProps({server: {type: Object, required: true}, unlock: {type: Object, default: null}, active: Boolean, online: Boolean,
  busy: Boolean, unlockBusy: Boolean, phone: Boolean, now: {type: Number, required: true}});
const emit = defineEmits(['unlock']);
const el = ref(null), data = ref(null), error = ref(''), opened = ref({});
const {seen} = useVisible(el);
// A phone shows this many of a group until it is opened, when that hides at least five.
const FOLD = 8;
const ERRORS = {unlock_full_failed: '程序运行出错', unlock_full_timeout: '运行超时', unlock_full_bad_report: '结果无法解析',
  download_failed: '下载检测程序失败', checksum_mismatch: '检测程序校验失败', node_connection_lost: '节点连接中断'};
let mine = 0, timer = 0;
// Also drops the answer of a read under way, which would otherwise schedule the next poll.
function stop() { ++mine; clearTimeout(timer); }

async function load() {
  const n = ++mine;
  clearTimeout(timer);
  try {
    const result = ok(await Api.serverUnlockFull(props.server.id));
    if (n !== mine) return;
    data.value = result;
    error.value = '';
    // Put off behind a benchmark: read now and then until it has run.
    if ((result.running || result.deferred) && props.active) timer = setTimeout(load, result.running ? 4000 : 15000);
  } catch (e) { if (n === mine) error.value = safeError(e); }
}
watch(() => props.server.id, () => { data.value = null; opened.value = {}; });
watch(() => seen.value && props.active && props.server.id, on => { if (on) void load(); else stop(); }, {immediate: true});
// 重新检测 ran the cards' checks; the full one has started behind them.
watch(() => props.unlockBusy, (busy, was) => { if (was && !busy && seen.value && props.active) void load(); });
onBeforeUnmount(stop);

const full = computed(() => data.value?.full || null);
const running = computed(() => Boolean(data.value?.running));
// Until the full result is read, nothing rather than the nine growing into the whole list.
const list = computed(() => unlockList(full.value, data.value || error.value ? props.unlock : null));
const groups = computed(() => list.value.groups.map(g => ({...g,
  shown: props.phone && !opened.value[g.key] && g.items.length >= FOLD + 5 ? g.items.slice(0, FOLD) : g.items})));
const counts = computed(() => [['ok', '可用'], ['limited', '受限'], ['blocked', '不可用'], ['error', '未知']]
  .map(([key, name]) => ({key, name, n: list.value.counts[key]})).filter(c => c.key === 'ok' || c.n));
const meta = computed(() => {
  if (full.value) return [`检测于 ${relativeTime(full.value.at, props.now)}`, full.value.country && `出口 ${regionName(full.value.country)}`].filter(Boolean).join(' · ');
  if (props.unlock) return `检测于 ${relativeTime(props.unlock.at, props.now)}${props.unlock.family === 'v6' ? ' · IPv6' : ''}`;
  return '还没有检测结果';
});
const notice = computed(() => {
  const d = data.value;
  if (!d || d.off) return '';
  const why = d.error && (ERRORS[d.error] || d.error);
  if (d.unable === 'platform') return '这台服务器不是 Linux，跑不了完整检测，只显示常用的 9 项。';
  if (d.unable === 'exec') return '这台服务器的节点关闭了命令执行，跑不了完整检测，只显示常用的 9 项。';
  if (d.deferred && !running.value) return `这台服务器上有评测在进行，完整检测等它结束后再运行；${full.value ? '现在显示的是上一次的结果。' : '先显示常用的 9 项。'}`;
  if (full.value) return why ? `${relativeTime(d.failedAt, props.now)}的完整检测没有成功（${why}），显示的是上一次的结果。` : '';
  if (running.value) return '完整检测进行中，一般一两分钟出结果。';
  return why ? `完整检测没有成功（${why}），只显示常用的 9 项。` : '完整检测还没有结果，先显示常用的 9 项。';
});

function cellStyle(cell) {
  return {backgroundImage: `url(${SPRITE.src})`, backgroundSize: `${SPRITE.cols * 100}% ${SPRITE.rows * 100}%`,
    backgroundPosition: `${cell.x / (SPRITE.cols - 1) * 100}% ${cell.y / (SPRITE.rows - 1) * 100}%`};
}
</script>
<template>
  <section ref="el" :class="['ix-card', 'dt-section', 'su', {'is-waiting': !data && !error}]" data-jump="unlock" data-unlock-detail aria-label="服务解锁">
    <header class="ix-card-head">
      <h3><CardIcon :icon="Unlock" tone="success" />服务解锁</h3>
      <span class="instance-muted">{{ meta }}</span>
      <span v-if="running" class="su-running" role="status">完整检测中…</span>
      <span class="ob-grow" />
      <button v-if="online" type="button" class="ix-text-button" :disabled="busy || running" @click="emit('unlock')">
        {{ unlockBusy ? '检测中…' : unlock || full ? '重新检测' : '检测' }}</button>
    </header>
    <p v-if="error" class="instance-error">{{ error }}</p>
    <p v-if="list.items.length" class="su-counts" aria-label="汇总">
      <span v-for="c in counts" :key="c.key" :data-state="c.key">{{ c.name }}<b>{{ c.n }}</b></span>
    </p>
    <p v-if="notice" class="dt-note su-notice">{{ notice }}</p>
    <p v-if="!list.items.length && !error" class="instance-muted dt-wait" role="status">{{ data ? '还没有检测结果' : '正在读取…' }}</p>
    <div v-for="g in groups" :key="g.key" class="su-group" :data-group="g.key">
      <h4>{{ g.name }}<small>{{ g.items.length }}</small></h4>
      <ul class="su-grid">
        <li v-for="svc in g.shown" :key="svc.id" :data-service="svc.id" :data-state="svc.state" :title="svc.label">
          <span class="su-logo" :style="svc.logo?.svg ? {'--svc': svc.logo.svg.color, '--svc-dark': svc.logo.svg.dark || svc.logo.svg.color} : null" aria-hidden="true">
            <svg v-if="svc.logo?.svg" viewBox="0 0 24 24"><path :d="svc.logo.svg.path" :fill-rule="svc.logo.svg.evenodd ? 'evenodd' : null" /></svg>
            <i v-else-if="svc.logo?.cell" :style="cellStyle(svc.logo.cell)" :data-tone="svc.logo.cell.tone || null" />
            <b v-else>{{ svc.name.slice(0, 1).toUpperCase() }}</b>
          </span>
          <span class="su-name">{{ svc.name }}<small v-if="svc.notes.length">{{ svc.notes.join('，') }}</small></span>
          <strong :data-state="svc.state" :data-tone="svc.mark?.tone || null">{{ svc.text }}</strong>
        </li>
      </ul>
      <button v-if="g.shown.length < g.items.length" type="button" class="ix-text-button su-more" @click="opened = {...opened, [g.key]: true}">
        展开其余 {{ g.items.length - g.shown.length }} 项</button>
    </div>
  </section>
</template>
