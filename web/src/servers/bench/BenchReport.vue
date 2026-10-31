<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ExternalLink } from '@lucide/vue';
import { Api } from '../../api.js';
import { errorText, flag } from '../serverHelpers.js';
import { ansiHtml, isActive, runTime, stateText, stateTone, took } from './benchHelpers.js';

// One run: what was run where, its report sections (IPv4 / IPv6 when both were tested, one at a time
// by tabs), the online report link if one was made. While it runs, the job's latest output; when it
// ended without a report, its log (a report already holds what the log ends with).
// compact: a column of the side-by-side comparison, without the actions, its sections one under another.
// active: the report is on screen; the latest output is fetched only then.
const props = defineProps({run: {type: Object, required: true}, server: {type: Object, default: null}, now: {type: Number, default: () => Date.now()},
  compact: Boolean, busy: Boolean, active: {type: Boolean, default: true}});
const emit = defineEmits(['stop', 'delete']);
const live = ref(''), liveError = ref(''), tab = ref(0);
let timer, alive = true;

const sections = computed(() => props.run.sections || []);
const tabbed = computed(() => !props.compact && sections.value.length > 1);
const running = computed(() => isActive(props.run));
const meta = computed(() => [runTime(props.run.createdMs), props.run.label, props.run.package, took(props.run, props.now)].filter(Boolean).join(' · '));
const failure = computed(() => props.run.state === 'failed' || props.run.state === 'interrupted' ? errorText(props.run.error || 'bench_internal') : '');
const links = computed(() => sections.value.filter(s => s.link).map(s => ({label: s.label ? `${s.label} 在线报告` : '在线报告', href: s.link})));

// The job's last lines as a terminal would show them: each line from its last carriage return.
function tail(text) {
  return text.split('\n').map(line => line.slice(line.lastIndexOf('\r') + 1)).filter((line, i, all) => line || i < all.length - 1).slice(-60).join('\n');
}
async function follow() {
  clearTimeout(timer);
  if (!alive || !props.active) return;
  if (props.run.state !== 'running' || !props.run.jobId) { live.value = ''; return; }
  if (document.hidden) { timer = setTimeout(follow, 3000); return; }
  try {
    const data = await Api.serverJob(props.run.serverId, props.run.jobId, {limit: 32768});
    live.value = tail(data?.log?.data || '');
    liveError.value = '';
  } catch (e) { liveError.value = errorText(e?.response?.data?.error || 'node_connection_lost'); }
  if (alive) timer = setTimeout(follow, 3000);
}
watch(() => [props.run.id, props.run.state, props.run.jobId, props.active], follow, {immediate: true});
watch(() => [props.run.id, sections.value.length], () => { if (tab.value >= sections.value.length) tab.value = 0; });
onBeforeUnmount(() => { alive = false; clearTimeout(timer); });
</script>
<template>
  <article class="bn-report" :class="{'is-compact': compact}" :data-run="run.id">
    <header class="bn-rep-head">
      <div class="bn-rep-title">
        <h3><span v-if="server" class="sv-flag" aria-hidden="true">{{ flag(server.region) }}</span>{{ server?.name || '已删除的服务器' }}<span class="bn-rep-test">{{ run.name }}</span></h3>
        <p class="bn-rep-meta">{{ meta }}</p>
      </div>
      <span class="ix-pill" :data-tone="stateTone(run.state)">{{ stateText(run) }}</span>
      <div v-if="!compact" class="bn-rep-actions">
        <a v-for="l in links" :key="l.href" class="ix-text-button bn-link" :href="l.href" target="_blank" rel="noopener noreferrer">{{ l.label }}<ExternalLink :size="13" aria-hidden="true" /></a>
        <el-button v-if="running" size="small" :loading="busy" :disabled="run.stopping" @click="emit('stop', run)">{{ run.stopping ? '正在停止' : run.state === 'queued' ? '取消' : '停止' }}</el-button>
        <el-button v-else size="small" :loading="busy" class="dd-danger" @click="emit('delete', run)">删除</el-button>
      </div>
    </header>
    <p v-if="failure" class="dd-note bn-note" data-tone="bad">{{ failure }}<template v-if="run.detail">（{{ run.detail }}）</template></p>
    <template v-if="running">
      <p class="instance-muted bn-wait" role="status">{{ run.state === 'queued' ? '排队中：这台服务器上前面还有评测，结束后自动开始。' : run.state === 'running' ? '正在运行，下面是节点上的最新输出。' : '正在准备，稍等。' }}</p>
      <pre v-if="live" class="bn-term bn-live" v-html="ansiHtml(live)"></pre>
      <p v-else-if="liveError" class="instance-muted">{{ liveError }}</p>
    </template>
    <div v-if="tabbed" class="sv-ranges bn-section-tabs" role="tablist" aria-label="报告">
      <button v-for="(section, i) in sections" :key="i" type="button" role="tab" :aria-selected="tab === i" @click="tab = i">{{ section.label || `报告 ${i + 1}` }}</button>
    </div>
    <section v-for="(section, i) in sections" v-show="!tabbed || tab === i" :key="i" class="bn-section">
      <h4 v-if="section.label && !tabbed">{{ section.label }}</h4>
      <pre class="bn-term" tabindex="0" v-html="ansiHtml(section.ansi)"></pre>
    </section>
    <p v-if="!running && !sections.length && !failure && !run.log" class="instance-muted">这次评测没有报告。</p>
    <section v-if="!compact && !running && !sections.length && run.log" class="bn-section">
      <h4>运行日志</h4>
      <pre class="bn-term" tabindex="0" v-html="ansiHtml(run.log)"></pre>
    </section>
  </article>
</template>
