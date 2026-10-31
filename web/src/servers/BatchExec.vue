<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import { EXEC_ERRORS, errorText, flag, platformOf, safeError } from './serverHelpers.js';

const props = defineProps({items: {type: Array, required: true}, preset: {type: Array, default: () => []}});
const emit = defineEmits(['close', 'done']);
const MAX_TARGETS = 100;
const TIMEOUTS = [[30, '30 秒'], [120, '2 分钟'], [600, '10 分钟'], [1800, '30 分钟']];
const usable = item => item.live && item.node?.caps?.exec !== false;
const selected = ref(new Set(props.preset.filter(id => props.items.some(i => i.id === id && usable(i)))));
const command = ref(''), timeout = ref(120), job = ref(null), busy = ref(false), error = ref(''), saved = ref([]), open = ref(new Set());
let timer, alive = true;

const servers = computed(() => props.items.filter(i => i.node));
const groups = computed(() => [...new Set(servers.value.map(i => i.profile.group).filter(Boolean))]);
const nameOf = id => props.items.find(i => i.id === id)?.name || id;
// Windows nodes run commands with PowerShell, the others with sh.
const windows = item => platformOf(item) === 'windows';
const chosenKinds = computed(() => new Set(props.items.filter(i => selected.value.has(i.id)).map(windows)));
const mixed = computed(() => chosenKinds.value.size > 1);
const onlyWindows = computed(() => chosenKinds.value.size === 1 && chosenKinds.value.has(true));
const results = computed(() => job.value?.items || []);
const counts = computed(() => ({ok: results.value.filter(r => r.state === 'done' && r.ok).length, bad: results.value.filter(r => r.state === 'done' && !r.ok).length,
  running: results.value.filter(r => r.state === 'running').length}));

function toggle(item) {
  if (!usable(item)) return;
  const next = new Set(selected.value);
  next.has(item.id) ? next.delete(item.id) : next.add(item.id);
  selected.value = next;
}
function pick(group) {
  const pool = servers.value.filter(i => usable(i) && (group === null || i.profile.group === group));
  const all = pool.every(i => selected.value.has(i.id));
  const next = new Set(selected.value);
  pool.forEach(i => all ? next.delete(i.id) : next.add(i.id));
  selected.value = next;
}
function insert(event) {
  const item = saved.value.find(c => String(c.id) === event.target.value);
  if (item) command.value = item.command;
  event.target.value = '';
}
async function run(ids = [...selected.value]) {
  if (busy.value || !ids.length || !command.value.trim()) return;
  if (ids.length > MAX_TARGETS) { error.value = errorText('invalid_exec_targets'); return; }
  busy.value = true; error.value = ''; open.value = new Set();
  try {
    job.value = ok(await Api.execServers({serverIds: ids, command: command.value, timeout: timeout.value})).job;
    poll();
  } catch (e) { error.value = safeError(e); busy.value = false; }
}
function poll() {
  clearTimeout(timer);
  if (!alive) return;
  timer = setTimeout(async () => {
    try { job.value = ok(await Api.serverExecJob(job.value.id)).job; } catch (e) {
      // A dropped request is retried; only an expired job ends the wait.
      if (e?.response?.status !== 404) { poll(); return; }
      error.value = safeError(e); busy.value = false; return;
    }
    if (job.value.done) { busy.value = false; emit('done'); } else poll();
  }, 800);
}
function retryFailed() { run(results.value.filter(r => !r.ok).map(r => r.serverId)); }
function expand(id) { const next = new Set(open.value); next.has(id) ? next.delete(id) : next.add(id); open.value = next; }
function status(r) {
  if (r.state === 'running') return '执行中…';
  if (r.error) return EXEC_ERRORS[r.error] || errorText(r.error).replace(/。$/, '');
  if (r.timedOut) return '超时，已结束';
  return `退出码 ${r.exitCode}`;
}
async function copy(r) {
  try { await navigator.clipboard.writeText([r.stdout, r.stderr].filter(Boolean).join('\n')); ElMessage.success('已复制输出'); }
  catch { ElMessage.warning('浏览器不允许复制'); }
}
onMounted(async () => { try { saved.value = ok(await Api.serverCommands()).items; } catch { /* optional */ } });
onBeforeUnmount(() => { alive = false; clearTimeout(timer); });
</script>
<template>
  <div class="be">
    <section class="be-targets" aria-label="选择服务器">
      <header>
        <strong>服务器</strong><span class="instance-muted">已选 {{ selected.size }} 台</span><span class="ob-grow" />
        <button type="button" class="ix-text-button" @click="pick(null)">全部在线</button>
        <button v-for="g in groups" :key="g" type="button" class="ix-text-button" @click="pick(g)">{{ g }}</button>
      </header>
      <div class="be-chips">
        <button v-for="item in servers" :key="item.id" type="button" class="be-chip" :aria-pressed="selected.has(item.id)" :disabled="!usable(item)"
          :title="!item.live ? '离线' : item.node.caps?.exec === false ? '节点关闭了命令执行' : windows(item) ? `${item.name}（Windows，用 PowerShell 执行）` : item.name" @click="toggle(item)">
          <span class="sv-flag" aria-hidden="true">{{ flag(item.region) }}</span>{{ item.name }}<small v-if="windows(item)" class="be-os">PowerShell</small>
        </button>
      </div>
      <p v-if="mixed" class="ob-notice be-mixed">选中的服务器里既有 Windows 也有 Linux / macOS。Windows 用 PowerShell 执行，同一条命令在两边的写法通常不一样。</p>
    </section>
    <label class="be-command">
      <span class="be-label"><strong>命令</strong><span class="ob-grow" />
        <select v-if="saved.length" class="be-insert" aria-label="插入常用命令" @change="insert"><option value="">插入常用命令…</option><option v-for="c in saved" :key="c.id" :value="c.id">{{ c.name }}</option></select>
      </span>
      <textarea v-model="command" class="mono" rows="4" maxlength="8000" :placeholder="onlyWindows ? '例如：Get-PSDrive C; Get-Service sshd' : '例如：df -h /; free -m'" aria-label="要执行的命令" @keydown.ctrl.enter="run()" @keydown.meta.enter="run()" />
    </label>
    <div class="be-actions">
      <label class="be-timeout"><span>超时</span><select v-model.number="timeout" aria-label="超时"><option v-for="[v, l] in TIMEOUTS" :key="v" :value="v">{{ l }}</option></select></label>
      <span class="instance-muted be-hint">以 root（Windows 为 SYSTEM）身份在每台服务器上执行，最多同时 8 台；命令会记录在各服务器的命令记录里。</span>
      <span class="ob-grow" />
      <el-button @click="emit('close')">关闭</el-button>
      <el-button type="primary" :loading="busy" :disabled="!selected.size || !command.trim()" @click="run()">执行{{ selected.size ? `（${selected.size} 台）` : '' }}</el-button>
    </div>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>

    <section v-if="job" class="be-results" aria-label="执行结果">
      <header>
        <strong>结果</strong>
        <span class="instance-muted">{{ counts.ok }} 成功 · {{ counts.bad }} 失败{{ counts.running ? ` · ${counts.running} 执行中` : '' }}</span>
        <span class="ob-grow" />
        <button v-if="!busy && counts.bad" type="button" class="ix-text-button" @click="retryFailed">重试失败的</button>
      </header>
      <ul>
        <li v-for="r in results" :key="r.serverId" :data-state="r.state === 'running' ? 'running' : r.ok ? 'ok' : 'bad'">
          <button type="button" class="be-row" :aria-expanded="open.has(r.serverId)" :disabled="r.state === 'running'" @click="expand(r.serverId)">
            <span class="be-state" aria-hidden="true"></span>
            <strong>{{ nameOf(r.serverId) }}</strong>
            <span class="be-status">{{ status(r) }}</span>
            <span class="instance-muted">{{ r.durationMs != null ? `${(r.durationMs / 1000).toFixed(1)} 秒` : '' }}</span>
          </button>
          <div v-if="open.has(r.serverId)" class="be-output">
            <pre v-if="r.stdout">{{ r.stdout }}</pre>
            <pre v-if="r.stderr" class="is-err">{{ r.stderr }}</pre>
            <p v-if="!r.stdout && !r.stderr" class="instance-muted">没有输出</p>
            <div class="be-output-foot"><span v-if="r.truncated" class="instance-muted">输出过长，只保留了末尾部分</span><span class="ob-grow" /><button v-if="r.stdout || r.stderr" type="button" class="ix-text-button" @click="copy(r)">复制输出</button></div>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>
