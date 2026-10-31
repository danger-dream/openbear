<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Api } from '../api.js';
import { ok, relativeTime, timeLabel } from '../instances/instanceHelpers.js';
import { MOBILE_VIEWPORT_QUERY } from '../mobileViewport.js';
import { bytes, safeError } from './serverHelpers.js';
import { FOLLOW_GAP, LOG_LIMIT, followDelay, keepRecent } from './jobLog.js';

const props = defineProps({serverId: {type: String, required: true}, active: Boolean, now: {type: Number, required: true}});
// Output read per request; earlier output comes in pieces of the same size.
const CHUNK = 64 * 1024;
const jobs = ref(null), error = ref(''), stopping = ref(''), phone = ref(false);
const viewing = ref(''), log = ref(null), logError = ref(''), loadingEarlier = ref(false), logBox = ref(null);
let alive = true, timer, logTimer, media;

const STATES = {running: '运行中', exited: '已结束', stopped: '已停止', interrupted: '已中断'};
const job = computed(() => jobs.value?.find(j => j.id === viewing.value) || log.value?.job || null);
const running = computed(() => (jobs.value || []).some(j => j.state === 'running'));
const stateTone = j => j.state === 'running' ? 'ok' : j.state === 'exited' && j.exitCode === 0 ? 'off' : 'bad';
const stateText = j => j.state === 'exited' ? (j.exitCode === 0 ? '成功' : `退出码 ${j.exitCode}`) : STATES[j.state] || j.state;
function took(j) {
  const ms = j.state === 'running' ? props.now - j.startedAtMs : j.durationMs ?? ((j.endedAtMs || props.now) - j.startedAtMs);
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`;
}
const facts = computed(() => {
  const j = job.value;
  if (!j) return [];
  return [['状态', stateText(j)], ['开始', timeLabel(j.startedAtMs)], ['耗时', took(j)], ['目录', j.cwd], ['Shell', j.shell],
    ['输出', log.value ? `${bytes(log.value.size)}${log.value.first ? `，最早的 ${bytes(log.value.first)} 已丢弃` : ''}` : '']].filter(([, v]) => v);
});

async function load() {
  clearTimeout(timer);
  try { jobs.value = ok(await Api.serverJobs(props.serverId)).jobs; error.value = ''; }
  catch (e) { error.value = safeError(e); }
  if (alive && props.active) timer = setTimeout(load, running.value ? 5000 : 30000);
}
// The newest output, then whatever the job writes after it while the panel is open.
async function follow() {
  clearTimeout(logTimer);
  const id = viewing.value;
  if (!id) return;
  try {
    const current = log.value;
    const params = current ? {offset: current.next, limit: CHUNK * 4} : {limit: CHUNK};
    const data = ok(await Api.serverJob(props.serverId, id, params));
    if (viewing.value !== id) return;
    const part = data.log, box = logBox.value;
    const atEnd = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    if (!current || part.offset > current.next) {
      // A tail usually starts inside a line; that line comes back whole with the earlier output.
      const cut = part.offset > part.first ? part.data.indexOf('\n') + 1 : 0;
      log.value = keepRecent({...part, data: part.data.slice(cut), start: part.offset + new TextEncoder().encode(part.data.slice(0, cut)).length, job: data.job});
    } else log.value = keepRecent({...current, data: current.data + part.data, next: part.next, size: part.size, first: part.first, job: data.job});
    if (atEnd) void nextTick(() => { if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight; });
    logError.value = '';
    const delay = followDelay(part, current?.next);
    if (data.job.state === 'running' || delay === FOLLOW_GAP) logTimer = setTimeout(follow, delay);
  } catch (e) { if (viewing.value === id) logError.value = safeError(e); }
}
async function earlier() {
  const current = log.value;
  if (!current || current.start <= current.first || loadingEarlier.value) return;
  loadingEarlier.value = true;
  try {
    const offset = Math.max(current.first, current.start - CHUNK);
    const part = ok(await Api.serverJob(props.serverId, viewing.value, {offset, limit: current.start - offset})).log;
    if (log.value === current) log.value = {...current, data: part.data + current.data, start: part.offset};
  } catch (e) { ElMessage.error(safeError(e)); }
  finally { loadingEarlier.value = false; }
}
function open(j) { viewing.value = j.id; log.value = null; logError.value = ''; void follow(); }
function close() { viewing.value = ''; clearTimeout(logTimer); log.value = null; }
async function stop(j) {
  try {
    await ElMessageBox.confirm(`结束这个任务和它启动的所有进程？\n${j.command.length > 200 ? j.command.slice(0, 200) + '…' : j.command}`, '停止后台任务',
      {confirmButtonText: '停止', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  stopping.value = j.id;
  try {
    const result = ok(await Api.stopServerJob(props.serverId, j.id));
    ElMessage[result.stopped ? 'success' : 'info'](result.stopped ? '任务已停止' : '任务已经结束了');
  } catch (e) { ElMessage.error(safeError(e)); }
  finally { stopping.value = ''; void load(); if (viewing.value === j.id) void follow(); }
}
function syncPhone() { phone.value = media.matches; }

watch(() => [props.serverId, props.active], () => { if (props.active) void load(); else clearTimeout(timer); });
onMounted(() => {
  media = window.matchMedia(MOBILE_VIEWPORT_QUERY);
  syncPhone();
  media.addEventListener('change', syncPhone);
  if (props.active) void load();
});
onBeforeUnmount(() => { alive = false; clearTimeout(timer); clearTimeout(logTimer); media?.removeEventListener('change', syncPhone); });
</script>
<template>
  <section class="ix-card sv-jobs">
    <header class="ix-card-head"><h3>后台任务</h3><span class="instance-muted">{{ jobs?.length ? `${jobs.length} 个` : '' }}</span><span class="ob-grow" />
      <button type="button" class="ix-text-button" @click="load">刷新</button>
    </header>
    <p v-if="error" class="instance-error">{{ error }}</p>
    <p v-else-if="!jobs" class="instance-muted" role="status">正在读取…</p>
    <p v-else-if="!jobs.length" class="instance-muted">还没有后台任务。AI 用 Servers 的 job_start 启动的长时间命令会显示在这里，节点重启后也不会丢失。</p>
    <ol v-else class="sv-job-list">
      <li v-for="j in jobs" :key="j.id" :data-tone="stateTone(j)">
        <button type="button" class="sv-job-open" :aria-label="`查看任务 ${j.id} 的日志`" @click="open(j)">
          <span class="sv-job-state">{{ stateText(j) }}</span>
          <code class="sv-job-command">{{ j.command }}</code>
          <span class="sv-job-meta"><span>{{ took(j) }}</span><time :title="timeLabel(j.startedAtMs)">{{ relativeTime(j.startedAtMs, now) }}</time></span>
        </button>
        <button v-if="j.state === 'running'" type="button" class="ix-text-button sv-job-stop" :disabled="stopping === j.id" @click="stop(j)">停止</button>
      </li>
    </ol>
    <el-drawer :model-value="Boolean(viewing)" @update:model-value="v => { if (!v) close() }" :direction="phone ? 'btt' : 'rtl'" :size="phone ? '86%' : '560px'"
      :title="`后台任务 · ${viewing}`" :class="['instance-ui', 'servers-ui', 'sv-job-drawer', phone ? 'mobile-asset-sheet mobile-viewport-sheet' : '']" append-to-body>
      <div v-if="phone" class="mobile-asset-grip" aria-hidden="true"></div>
      <div v-if="job" class="sv-job-view">
        <pre class="sv-job-full mono">{{ job.command }}</pre>
        <dl class="ix-facts">
          <div v-for="[label, value] in facts" :key="label"><dt>{{ label }}</dt><dd class="ix-ellipsis" :title="value">{{ value }}</dd></div>
        </dl>
        <div class="sv-job-actions">
          <button v-if="log && log.start > log.first && !log.trimmed && log.data.length < LOG_LIMIT" type="button" class="ix-text-button" :disabled="loadingEarlier" @click="earlier">加载更早的输出</button>
          <span class="ob-grow" />
          <el-button v-if="job.state === 'running'" size="small" :loading="stopping === job.id" @click="stop(job)">停止任务</el-button>
        </div>
        <p v-if="log?.trimmed" class="instance-muted" role="status">输出较多，这里只保留最近约 {{ bytes(LOG_LIMIT) }}，更早的部分已截断。</p>
        <p v-if="logError" class="instance-error">{{ logError }}</p>
        <pre v-else-if="log" ref="logBox" class="sv-job-log" tabindex="0" aria-label="任务输出">{{ log.data || (job.state === 'running' ? '还没有输出' : '没有输出') }}</pre>
        <p v-else class="instance-muted" role="status">正在读取…</p>
      </div>
    </el-drawer>
  </section>
</template>
