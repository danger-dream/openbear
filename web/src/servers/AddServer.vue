<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { CopyDocument, QuestionFilled } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { HOST_PATTERN, HOST_TITLE, USER_PATTERN, USER_TITLE, ok, relativeTime } from '../instances/instanceHelpers.js';
import SshAuth from '../instances/SshAuth.vue';
import AccessPicker from './AccessPicker.vue';
import InstallCommand from './InstallCommand.vue';
import InstallConflict from './InstallConflict.vue';
import InstallProgress from './InstallProgress.vue';
import SuggestInput from './SuggestInput.vue';
import { dateTime, noAutofill, safeError } from './serverHelpers.js';

const props = defineProps({settings: {type: Object, required: true}, groups: {type: Array, default: () => []}});
const emit = defineEmits(['open', 'changed', 'settings', 'guide', 'close']);
const MODES = [['ssh', 'SSH 安装'], ['batch', '批量 SSH'], ['command', '安装命令'], ['code', '注册码']];
const mode = ref('ssh'), busy = ref(false), error = ref('');
const form = reactive({name: '', host: '', port: 22, username: 'root', auth: 'key', group: '', access: {primary: '', backup: ''},
  lines: '', defaultPort: 22, codeName: '', expires: 0, uses: 0, browser: false});
const login = ref(null), batchLogin = ref(null);
const created = ref(null), command = ref(null), job = ref(null), codes = ref([]), fresh = ref('');
let timer, alive = true;
const BATCH_LIMIT = 50;
// What the controller accepts as an SSH host.
const HOST = /^[A-Za-z0-9][A-Za-z0-9.:-]*$/;

const points = computed(() => props.settings.accessPoints || []);
const ready = computed(() => points.value.length > 0);
const profile = () => ({...(form.group.trim() ? {group: form.group.trim()} : {}), ...(form.access.primary || form.access.backup ? {access: {...form.access}} : {})});
const parsed = computed(() => form.lines.split('\n').map(l => l.trim()).filter(Boolean).map(line => {
  const words = line.split(/[\s,，]+/).filter(Boolean);
  const numeric = v => /^\d{1,5}$/.test(v || '');
  let name = '', host = '', port = Number(form.defaultPort) || 22;
  if (words.length >= 3) [name, host, port] = [words[0], words[1], numeric(words[2]) ? Number(words[2]) : NaN];
  else if (words.length === 2 && numeric(words[1])) [host, port] = [words[0], Number(words[1])];
  else if (words.length === 2) [name, host] = words;
  else host = words[0];
  const valid = HOST.test(host) && !host.includes('..') && host.length <= 253 && port >= 1 && port <= 65535;
  return {line, name: name || host, host, port, valid};
}));
const lineErrors = computed(() => parsed.value.filter(l => !l.valid));
// A batch item as it is now: a server installed again on its own (forced over another node) is followed by its install.
const itemState = item => item.install?.state === 'running' ? 'installing' : item.install?.state === 'done' ? 'done' : item.state;
const itemError = item => (item.install?.state === 'failed' && item.install.error) || item.error;
const batchCounts = computed(() => {
  const items = job.value?.items || [];
  return {done: items.filter(i => itemState(i) === 'done').length, failed: items.filter(i => itemState(i) === 'failed').length, total: items.length};
});

function stop() { clearTimeout(timer); }
function poll(fn) { stop(); if (alive) timer = setTimeout(async () => { if (await fn() !== false) poll(fn); }, 1500); }
function switchMode(value) { if (busy.value) return; mode.value = value; error.value = ''; if (value === 'code') void loadCodes(); }

async function installViaSsh() {
  if (busy.value) return;
  error.value = ''; busy.value = true;
  try {
    const item = ok(await Api.createServer({name: form.name.trim() || form.host.trim(), profile: profile(),
      ssh: {host: form.host.trim(), port: Number(form.port), username: form.username.trim(), auth: form.auth, ...login.value.credentials()}})).item;
    login.value.clear();
    emit('changed');
    // The server exists from here on: a failed start is retried, not saved again.
    created.value = item;
    const started = ok(await Api.installServerNode(item.id, {browser: form.browser}));
    created.value = {...item, install: started.install};
    poll(async () => {
      try { created.value = ok(await Api.server(item.id)).item; } catch { return true; }
      if (created.value.install?.state !== 'running') { emit('changed'); return false; }
    });
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
// force: replace a node this controller does not know (machine_has_other_node), already confirmed.
async function retry(force = false) {
  if (!created.value || busy.value) return;
  busy.value = true; error.value = '';
  try {
    created.value = {...created.value, install: ok(await Api.installServerNode(created.value.id, {browser: form.browser, ...(force === true ? {force: true} : {})})).install};
    poll(async () => {
      try { created.value = ok(await Api.server(created.value.id)).item; } catch { return true; }
      if (created.value.install?.state !== 'running') { emit('changed'); return false; }
    });
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
async function installBatch() {
  if (busy.value || !parsed.value.length || lineErrors.value.length || parsed.value.length > BATCH_LIMIT) return;
  error.value = ''; busy.value = true;
  try {
    job.value = ok(await Api.batchInstallServers({servers: parsed.value.map(({name, host, port}) => ({name, host, port})),
      ssh: {username: form.username.trim(), auth: form.auth, ...batchLogin.value.credentials()}, profile: profile(), browser: form.browser})).job;
    poll(async () => {
      try { job.value = ok(await Api.batchInstallStatus(job.value.id)).job; } catch { return true; }
      emit('changed');
      if (job.value.done) return false;
    });
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
// One batch item over another node, already confirmed: that server alone, followed in the batch's view.
async function forceItem(item) {
  if (busy.value || !item.serverId) return;
  busy.value = true; error.value = '';
  try {
    item.install = ok(await Api.installServerNode(item.serverId, {browser: form.browser, force: true})).install;
    poll(async () => {
      try { job.value = ok(await Api.batchInstallStatus(job.value.id)).job; } catch { return true; }
      emit('changed');
      if (job.value.done && !job.value.items.some(i => i.install?.state === 'running')) return false;
    });
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
// The server this machine is already connected as.
function openExisting(id) { if (id) emit('open', {id}); }
async function makeCommand() {
  if (busy.value || !form.name.trim()) return;
  error.value = ''; busy.value = true;
  try {
    const item = ok(await Api.createServer({name: form.name.trim(), profile: profile()})).item;
    emit('changed');
    const enrollment = ok(await Api.serverEnrollment(item.id, {browser: form.browser}));
    command.value = {item, ...enrollment, registered: false, online: false};
    poll(async () => {
      let current;
      try { current = ok(await Api.server(item.id)).item; } catch { return true; }
      command.value = {...command.value, item: current, registered: Boolean(current.node && current.node.enrolledAtMs >= enrollment.issuedAtMs - 1000), online: Boolean(current.live)};
      if (command.value.online) { emit('changed'); return false; }
    });
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
async function loadCodes() {
  try { codes.value = ok(await Api.registrationCodes()).items; } catch (e) { error.value = safeError(e); }
}
async function createCode() {
  if (busy.value || !form.codeName.trim()) return;
  error.value = ''; busy.value = true;
  try {
    const item = ok(await Api.createRegistrationCode({name: form.codeName.trim(), expiresInDays: Number(form.expires), maxUses: Number(form.uses), ...profile()})).item;
    fresh.value = item.id; form.codeName = '';
    await loadCodes();
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
async function revokeCode(code) {
  try { await ElMessageBox.confirm(`作废注册码「${code.name}」？已经接入的服务器不受影响，之后不能再用它添加新服务器。`, '作废注册码', {confirmButtonText: '作废', cancelButtonText: '取消', type: 'warning'}); } catch { return; }
  try { codes.value = ok(await Api.revokeRegistrationCode(code.id)).items; } catch (e) { error.value = safeError(e); }
}
async function copy(text, label = '命令') {
  try { await navigator.clipboard.writeText(text); ElMessage.success(`已复制${label}`); }
  catch { ElMessage.warning('浏览器不允许复制，请手动选择文字'); }
}
function again() { stop(); created.value = null; command.value = null; job.value = null; form.name = ''; form.host = ''; form.lines = ''; }
const codeState = c => c.revokedAtMs ? '已作废' : c.active ? '可用' : c.maxUses && c.uses >= c.maxUses ? '次数已用完' : '已过期';
// A machine that already runs a node says so in its own line under the item.
const CONFLICTS = {machine_has_node: '已作为另一台接入', machine_has_other_node: '已装有其他节点'};
const batchState = item => ({pending: '等待', connecting: '连接 SSH…', installing: (item.install?.steps || []).find(s => s.state === 'running')?.label || '安装中', done: '已上线',
  failed: CONFLICTS[itemError(item)] || safeError({response: {data: {error: itemError(item)}}}).replace(/。$/, '')})[itemState(item)] || itemState(item);
onMounted(() => { if (!ready.value) mode.value = 'ssh'; });
onBeforeUnmount(() => { alive = false; stop(); });
</script>
<template>
  <div class="as">
    <div class="as-top">
      <div class="sv-ranges as-modes" role="tablist" aria-label="添加方式">
        <button v-for="[key, label] in MODES" :key="key" type="button" role="tab" :aria-selected="mode === key" @click="switchMode(key)">{{ label }}</button>
      </div>
      <button type="button" class="ix-text-button" aria-label="接入说明" title="接入说明" @click="emit('guide')"><el-icon class="sv-only-narrow"><QuestionFilled /></el-icon><span class="sv-hide-narrow">接入说明</span></button>
    </div>
    <div v-if="!ready" class="ix-notice" data-tone="warn">
      <div><strong>还没有接入点</strong><span>节点需要一个能连回 OpenBear 的地址，先在设置里添加接入点。不知道怎么准备，可以看接入说明。</span></div>
      <div class="instance-actions"><el-button size="small" @click="emit('settings')">去设置</el-button></div>
    </div>

    <!-- SSH: the controller logs in and installs, step by step. -->
    <template v-if="mode === 'ssh'">
      <div v-if="created" class="as-result">
        <p class="as-title"><strong>{{ created.name }}</strong><span class="instance-muted">{{ created.ssh?.host }}:{{ created.ssh?.port }}</span></p>
        <InstallProgress :install="created.install" :busy="busy" @open="openExisting" @force="retry(true)" />
        <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
        <div class="instance-form-footer">
          <el-button @click="again">继续添加</el-button>
          <el-button v-if="!created.install || created.install.state === 'failed'" :loading="busy" @click="retry">重试安装</el-button>
          <el-button type="primary" @click="emit('open', created)">打开服务器</el-button>
        </div>
      </div>
      <form v-else class="instance-form" autocomplete="off" @submit.prevent="installViaSsh">
        <div class="ob-fields" :class="{busy}">
          <div class="ob-field"><span>地址</span>
            <div class="ob-ctl">
              <div class="ob-inline"><input v-model="form.host" v-bind="noAutofill" required maxlength="253" :pattern="HOST_PATTERN" :title="HOST_TITLE" class="mono" placeholder="IP 或域名" aria-label="服务器地址" /><input v-bind="noAutofill" v-model.number="form.port" class="mono ob-port" type="number" inputmode="numeric" min="1" max="65535" required aria-label="SSH 端口" /></div>
              <small>OpenBear 用这个地址登录服务器安装节点，内网地址也可以。</small>
            </div>
          </div>
          <label class="ob-field"><span>用户</span><input v-model="form.username" v-bind="noAutofill" required maxlength="64" :pattern="USER_PATTERN" :title="USER_TITLE" class="mono" aria-label="SSH 用户" /></label>
          <SshAuth ref="login" v-model="form.auth" @error="error = $event" />
          <label class="ob-field"><span>名称</span><input v-model="form.name" v-bind="noAutofill" maxlength="60" placeholder="留空用地址" aria-label="服务器名称" /></label>
          <label class="ob-field"><span>分组</span><SuggestInput v-model="form.group" :options="groups" maxlength="40" placeholder="可选" aria-label="分组" /></label>
          <div class="ob-field"><span>接入点</span><AccessPicker v-model="form.access" :points="points" :default-id="settings.defaultAccess" /></div>
          <div class="ob-field"><span>浏览器</span><label class="ob-check"><input v-model="form.browser" type="checkbox" /><span>同时开启浏览器<small class="instance-muted">主控可以用这台机器的浏览器和网络访问网页；没装 Chrome 时会自动下载</small></span></label></div>
        </div>
        <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
        <div class="instance-form-footer">
          <span class="ob-hint">保存时先测试 SSH，再安装节点，整个过程会逐步显示。</span>
          <el-button :disabled="busy" @click="emit('close')">取消</el-button>
          <el-button type="primary" native-type="submit" :loading="busy" :disabled="!ready">保存并安装</el-button>
        </div>
      </form>
    </template>

    <!-- Batch: one line per server, one shared login. -->
    <template v-else-if="mode === 'batch'">
      <div v-if="job" class="as-result">
        <p class="as-title"><strong>批量安装</strong><span class="instance-muted">{{ batchCounts.done }} 台已上线 · {{ batchCounts.failed }} 台失败 · 共 {{ batchCounts.total }} 台</span></p>
        <ul class="as-batch">
          <li v-for="(item, i) in job.items" :key="i" :data-state="itemState(item)">
            <span class="be-state" aria-hidden="true"></span>
            <strong>{{ item.name }}</strong>
            <span class="instance-muted mono">{{ item.host }}:{{ item.port }}</span>
            <span class="as-batch-state">{{ batchState(item) }}</span>
            <InstallConflict v-if="itemState(item) === 'failed'" :install="item.install" :busy="busy" @open="openExisting" @force="forceItem(item)" />
          </li>
        </ul>
        <div class="instance-form-footer">
          <el-button @click="again">继续添加</el-button>
          <el-button type="primary" @click="emit('close')">完成</el-button>
        </div>
      </div>
      <form v-else class="instance-form" autocomplete="off" @submit.prevent="installBatch">
        <div class="ob-fields" :class="{busy}">
          <div class="ob-field"><span>服务器</span>
            <div class="ob-ctl">
              <textarea v-model="form.lines" class="mono" rows="6" placeholder="每行一台：名称 地址 端口&#10;东京 203.0.113.10 27920&#10;198.51.100.7" aria-label="服务器列表" />
              <small v-if="lineErrors.length" class="ob-bad">第 {{ parsed.indexOf(lineErrors[0]) + 1 }} 行格式不对：{{ lineErrors[0].line }}</small>
              <small v-else-if="parsed.length > BATCH_LIMIT" class="ob-bad">一次最多安装 {{ BATCH_LIMIT }} 台，现在有 {{ parsed.length }} 台，请分批添加。</small>
              <small v-else-if="parsed.length">将安装 {{ parsed.length }} 台：{{ parsed.slice(0, 3).map(l => l.name).join('、') }}{{ parsed.length > 3 ? ' 等' : '' }}</small>
              <small v-else>名称和端口可以省略，省略的端口用下面的默认端口。</small>
            </div>
          </div>
          <label class="ob-field"><span>默认端口</span><input v-bind="noAutofill" v-model.number="form.defaultPort" class="mono sv-day" type="number" inputmode="numeric" min="1" max="65535" aria-label="默认 SSH 端口" /></label>
          <label class="ob-field"><span>用户</span><input v-model="form.username" v-bind="noAutofill" required maxlength="64" :pattern="USER_PATTERN" :title="USER_TITLE" class="mono" aria-label="SSH 用户" /></label>
          <SshAuth ref="batchLogin" v-model="form.auth" @error="error = $event" />
          <label class="ob-field"><span>分组</span><SuggestInput v-model="form.group" :options="groups" maxlength="40" placeholder="可选" aria-label="分组" /></label>
          <div class="ob-field"><span>接入点</span><AccessPicker v-model="form.access" :points="points" :default-id="settings.defaultAccess" /></div>
          <div class="ob-field"><span>浏览器</span><label class="ob-check"><input v-model="form.browser" type="checkbox" /><span>同时开启浏览器<small class="instance-muted">每台都开启；没装 Chrome 的会自动下载</small></span></label></div>
        </div>
        <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
        <div class="instance-form-footer">
          <span class="ob-hint">同时安装 4 台，每台的进度都会显示。</span>
          <el-button :disabled="busy" @click="emit('close')">取消</el-button>
          <el-button type="primary" native-type="submit" :loading="busy" :disabled="!ready || !parsed.length || lineErrors.length > 0 || parsed.length > BATCH_LIMIT">开始安装{{ parsed.length ? `（${parsed.length} 台）` : '' }}</el-button>
        </div>
      </form>
    </template>

    <!-- One-time command: for machines without SSH or behind NAT. -->
    <template v-else-if="mode === 'command'">
      <div v-if="command" class="as-result">
        <p class="as-title"><strong>{{ command.item.name }}</strong><span class="instance-muted">30 分钟内有效，只能用一次</span></p>
        <InstallCommand :command="command.command" :windows-command="command.windowsCommand" note />
        <InstallProgress :manual="command" />
        <div class="instance-form-footer">
          <el-button @click="again">继续添加</el-button>
          <el-button type="primary" @click="emit('open', command.item)">打开服务器</el-button>
        </div>
      </div>
      <form v-else class="instance-form" autocomplete="off" @submit.prevent="makeCommand">
        <div class="ob-fields" :class="{busy}">
          <label class="ob-field"><span>名称</span><input v-model="form.name" v-bind="noAutofill" required maxlength="60" placeholder="例如：家里的 NAS" aria-label="服务器名称" /></label>
          <label class="ob-field"><span>分组</span><SuggestInput v-model="form.group" :options="groups" maxlength="40" placeholder="可选" aria-label="分组" /></label>
          <div class="ob-field"><span>接入点</span><AccessPicker v-model="form.access" :points="points" :default-id="settings.defaultAccess" /></div>
          <div class="ob-field"><span>浏览器</span><label class="ob-check"><input v-model="form.browser" type="checkbox" /><span>同时开启浏览器<small class="instance-muted">生成的命令会带上这个选项</small></span></label></div>
        </div>
        <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
        <div class="instance-form-footer">
          <span class="ob-hint">适合不能 SSH 登录、或在 NAT 后面的机器。</span>
          <el-button :disabled="busy" @click="emit('close')">取消</el-button>
          <el-button type="primary" native-type="submit" :loading="busy" :disabled="!ready || !form.name.trim()">生成安装命令</el-button>
        </div>
      </form>
    </template>

    <!-- Registration codes: one command for any number of new machines. -->
    <template v-else>
      <form class="instance-form as-code-form" autocomplete="off" @submit.prevent="createCode">
        <div class="ob-fields" :class="{busy}">
          <label class="ob-field"><span>名称</span><input v-model="form.codeName" v-bind="noAutofill" maxlength="40" placeholder="例如：新开的 VPS" aria-label="注册码名称" /></label>
          <label class="ob-field"><span>分组</span><SuggestInput v-model="form.group" :options="groups" maxlength="40" placeholder="新服务器自动归入这个分组" aria-label="分组" /></label>
          <div class="ob-field"><span>接入点</span><AccessPicker v-model="form.access" :points="points" :default-id="settings.defaultAccess" /></div>
          <div class="ob-field"><span>限制</span>
            <div class="ob-inline">
              <select v-model.number="form.expires" aria-label="有效期"><option :value="0">长期有效</option><option :value="1">1 天</option><option :value="7">7 天</option><option :value="30">30 天</option></select>
              <select v-model.number="form.uses" aria-label="可用次数"><option :value="0">不限次数</option><option :value="1">1 次</option><option :value="5">5 次</option><option :value="20">20 次</option></select>
            </div>
          </div>
        </div>
        <div class="instance-form-footer">
          <span class="ob-hint">一条命令装任意多台，按主机名自动建档；像是已有服务器重装后接入的，先作为新服务器待你确认。</span>
          <el-button type="primary" native-type="submit" :loading="busy" :disabled="!ready || !form.codeName.trim()">新建注册码</el-button>
        </div>
      </form>
      <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
      <ul v-if="codes.length" class="as-codes">
        <li v-for="c in codes" :key="c.id" :data-active="c.active" :class="{'is-fresh': fresh === c.id}">
          <div class="as-code-head">
            <strong>{{ c.name }}</strong>
            <span class="ob-dot" :data-tone="c.active ? 'ok' : 'off'">{{ codeState(c) }}</span>
            <span class="instance-muted">{{ [c.group, `已用 ${c.uses}${c.maxUses ? '/' + c.maxUses : ''} 次`, c.expiresAtMs ? `${dateTime(c.expiresAtMs)} 到期` : '长期有效', c.lastUsedAtMs ? `最近使用 ${relativeTime(c.lastUsedAtMs, Date.now())}` : ''].filter(Boolean).join(' · ') }}</span>
            <span class="ob-grow" />
            <button v-if="c.command" type="button" class="ix-text-button" @click="copy(c.command)">复制命令</button>
            <button v-if="c.windowsCommand" type="button" class="ix-text-button" @click="copy(c.windowsCommand)">复制 Windows 命令</button>
            <button v-if="!c.revokedAtMs" type="button" class="ix-text-button ob-danger" @click="revokeCode(c)">作废</button>
          </div>
          <code v-if="c.command && fresh === c.id" class="as-code-command mono">{{ c.command }}</code>
        </li>
      </ul>
      <p v-else class="instance-muted">还没有注册码。</p>
    </template>
  </div>
</template>
