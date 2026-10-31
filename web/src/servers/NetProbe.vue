<script setup>
import { computed, onBeforeUnmount, onDeactivated, onMounted, reactive, ref, watch } from 'vue';
import { NetApi } from './netApi.js';
import { countLabel, countRange, DEFAULTS, failureKind, FAMILIES, fieldError, GEO_SCOPES, geoPending, geoText, hopsWaitingForGeo, meshPathView, pctText, PROBE_MODES, PROBE_STATES,
  hopPending, probeErrorText, probeRequest, resultText, summaryParts, targetOptions } from './netHelpers.js';
import { flag, noAutofill, safeError } from './serverHelpers.js';
import { mesh } from './mesh/meshStore.js';
import { msText, nameOf } from './mesh/meshHelpers.js';
import SuggestInput from './SuggestInput.vue';
import { MOBILE_VIEWPORT_QUERY } from '../mobileViewport.js';
import './net.css';

// Ping, tcping or trace from one server. Results show as they come (polled every second);
// a test still running is stopped when the page, the panel or the server is left.
// card: the detail page's card; otherwise the mesh member panel's test tab, where a mesh
// path can be shown on the board (board) and mesh addresses come first in the suggestions.
const props = defineProps({serverId: {type: String, required: true}, servers: {type: Array, default: () => []}, online: {type: Boolean, default: true},
  active: {type: Boolean, default: true}, card: Boolean, board: Boolean, shownPath: {type: String, default: ''}});
const emit = defineEmits(['show-path']);

const form = reactive({mode: 'ping', target: '', port: '', family: 0, count: '', intervalMs: '', timeoutMs: ''});
const errors = ref({}), startError = ref(''), note = ref(''), advanced = ref(null);
// idle → starting → running → (stopping) → ended
const phase = ref('idle'), status = ref(null), run = ref(null), showAll = ref(false), geoDone = ref(false);
const ROWS = 10, GEO_TRIES = 3;
let round = 0, timer, fails = 0, geoTries = 0;

const busy = computed(() => ['starting', 'running', 'stopping'].includes(phase.value));
const members = computed(() => mesh.work?.members || []);
const options = computed(() => targetOptions(props.servers, props.serverId, members.value, {meshFirst: props.board}));
const name = id => members.value.some(m => m.serverId === id) ? nameOf(members.value, id) : props.servers.find(s => s.id === id)?.name || id;

const st = computed(() => status.value || {});
// A test the node dropped (not polled for 30 seconds) still says running in its last reply.
const shownState = computed(() => phase.value === 'ended' && st.value.state === 'running' ? 'stopped' : st.value.state || (busy.value ? 'running' : ''));
const kind = computed(() => st.value.requestedMode || st.value.mode || run.value?.mode || form.mode);
const meshPath = computed(() => st.value.meshPath ? meshPathView(st.value.meshPath, {work: mesh.work, data: mesh.data, name, target: st.value.ip || st.value.target || run.value?.target || ''}) : null);
const meshTrace = computed(() => kind.value === 'trace' && Boolean(meshPath.value));
const results = computed(() => Array.isArray(st.value.results) ? st.value.results : []);
const shownResults = computed(() => showAll.value ? results.value : results.value.slice(-ROWS));
const summary = computed(() => summaryParts(st.value.summary));
const hops = computed(() => Array.isArray(st.value.hops) ? st.value.hops : []);
const failed = computed(() => st.value.state === 'error' && st.value.error ? probeErrorText(st.value.error) : null);
const heading = computed(() => {
  const s = st.value, target = s.target || run.value?.target || '';
  return [s.ip && s.ip !== target ? `${target}（${s.ip}）` : target, s.family ? `IPv${s.family}` : '', s.port ? `端口 ${s.port}` : ''].filter(Boolean).join(' · ');
});
// The latency of the last results as bars, the slowest one full height; a lost one a full red bar.
const bars = computed(() => {
  const list = results.value.slice(-60), top = Math.max(1, ...list.filter(r => r.ok).map(r => Number(r.rttMs) || 0));
  return list.map(r => ({seq: r.seq, fail: !r.ok, h: r.ok ? Math.max(6, Math.round((Number(r.rttMs) || 0) * 100 / top)) : 100, text: `${r.seq} · ${resultText(r)}`}));
});
const pathKey = computed(() => meshPath.value ? `${props.serverId}>${meshPath.value.dst}${meshPath.value.subnet ? `>${meshPath.value.subnet}` : ''}` : '');

function setMode(mode) {
  if (busy.value) return;
  form.mode = mode;
  const e = {...errors.value};
  delete e.port; delete e.count; delete e.target;
  errors.value = e;
}
function clearError(field) { if (errors.value[field]) { const e = {...errors.value}; delete e[field]; errors.value = e; } }
function setFamily(value) { form.family = value; clearError('family'); }
// A field inside the advanced options that is wrong: open them so the message shows.
function showErrors(found) {
  errors.value = found;
  if (['count', 'intervalMs', 'timeoutMs', 'family'].some(k => found[k]) && advanced.value) advanced.value.open = true;
}

async function start() {
  const {body, errors: found} = probeRequest(form);
  showErrors(found); startError.value = '';
  if (Object.keys(found).length || !props.online) return;
  const mine = ++round, sid = props.serverId;
  clearTimeout(timer);
  phase.value = 'starting'; status.value = null; note.value = ''; showAll.value = false; geoDone.value = false; fails = 0; geoTries = 0;
  run.value = {id: '', serverId: sid, mode: body.mode, target: body.target};
  try {
    const reply = await NetApi.start(sid, body);
    // Left (or stopped) while it was starting: it is not wanted any more.
    if (mine !== round) { if (reply?.id) void NetApi.stop(sid, reply.id).catch(() => {}); return; }
    run.value = {...run.value, id: reply.id};
    phase.value = 'running';
    void poll(mine);
  } catch (e) {
    if (mine !== round) return;
    phase.value = 'idle'; run.value = null;
    const data = e?.response?.data || {};
    if (data.error === 'invalid_probe' && fieldError(data.field, body.mode)) showErrors({[data.field === 'mode' || data.field === 'maxHops' ? 'form' : data.field]: fieldError(data.field, body.mode)});
    else startError.value = safeError(e);
  }
}
async function poll(mine) {
  clearTimeout(timer);
  const r = run.value;
  if (mine !== round || !r?.id) return;
  try {
    const reply = await NetApi.status(r.serverId, r.id);
    if (mine !== round) return;
    status.value = reply; fails = 0; note.value = '';
    if (reply?.state !== 'running') {
      phase.value = 'ended';
      // Places are looked up as the addresses show up: a few more reads fill in the last ones.
      if (hopsWaitingForGeo(reply?.hops) && geoTries < GEO_TRIES) geoTries++;
      else { geoDone.value = true; return; }
    }
  } catch (e) {
    if (mine !== round) return;
    if (e?.response?.data?.error === 'not_found') {
      phase.value = 'ended'; geoDone.value = true;
      note.value = '节点上已经没有这次测试：超过 30 秒没有查询时，节点会自动停止并清理它。';
      return;
    }
    if (++fails >= 5) { phase.value = phase.value === 'running' ? 'ended' : phase.value; geoDone.value = true; note.value = `读不到结果：${safeError(e)}`; return; }
    note.value = `暂时读不到结果（${safeError(e)}），正在重试…`;
  }
  timer = setTimeout(() => poll(mine), 1000);
}
// Stops the running test. leaving: the view is going away, so the last state is not read back.
async function stop(leaving = false) {
  const r = run.value, was = phase.value;
  round++; clearTimeout(timer);
  if (!r?.id || !['starting', 'running'].includes(was)) { if (was === 'starting') phase.value = 'idle'; return; }
  if (leaving) { phase.value = 'ended'; void NetApi.stop(r.serverId, r.id).catch(() => {}); return; }
  phase.value = 'stopping';
  const mine = round;
  try { await NetApi.stop(r.serverId, r.id); } catch { /* already gone: the read below says so */ }
  try { const reply = await NetApi.status(r.serverId, r.id); if (mine === round) status.value = reply; } catch { /* keep what was shown */ }
  if (mine !== round) return;
  phase.value = 'ended'; geoDone.value = !hopsWaitingForGeo(status.value?.hops);
  // A stopped trace may still be waiting for places: read them in a moment.
  if (!geoDone.value) { geoTries = 0; phase.value = 'ended'; timer = setTimeout(() => { geoTries++; void poll(mine); }, 1000); }
}
function toggle() { if (phase.value === 'running' || phase.value === 'starting') void stop(); else if (phase.value !== 'stopping') void start(); }
function showOnBoard() {
  const p = meshPath.value;
  if (!p) return;
  emit('show-path', {src: props.serverId, dst: p.dst, subnet: p.subnet, hops: p.hops, rttMs: p.total, stale: p.stale, key: pathKey.value});
}

// Leaving: another page or tab (active), the page kept alive in the background, another server, the panel closed, the window closed.
watch(() => props.active, value => { if (!value) void stop(true); });
watch(() => props.serverId, () => { void stop(true); status.value = null; run.value = null; phase.value = 'idle'; note.value = ''; startError.value = ''; });
watch(() => props.online, value => { if (!value && busy.value) void stop(true); });
const leave = () => { const r = run.value; if (r?.id && ['starting', 'running'].includes(phase.value)) { round++; void NetApi.stop(r.serverId, r.id, true).catch(() => {}); } };
// On a phone the trace is a list of hops two lines each instead of the table (no sideways scrolling).
const phone = ref(false);
const inline = computed(() => props.card && !phone.value);
let media;
const syncPhone = () => { phone.value = Boolean(media?.matches); };
const geoTitle = geo => geo ? [...new Set([geo.countryName, geoText(geo)].filter(Boolean))].join(' · ') : '';
const lossShown = h => Number(h?.lossPct) > 0;
onMounted(() => {
  window.addEventListener('pagehide', leave);
  media = window.matchMedia?.(MOBILE_VIEWPORT_QUERY);
  syncPhone();
  media?.addEventListener?.('change', syncPhone);
});
onDeactivated(() => { void stop(true); });
onBeforeUnmount(() => { window.removeEventListener('pagehide', leave); media?.removeEventListener?.('change', syncPhone); void stop(true); });
</script>
<template>
  <section :class="card ? 'ix-card net-probe' : 'net-probe'" aria-label="网络测试">
    <header v-if="card" class="ix-card-head"><h3>网络测试</h3><span class="instance-muted">从这台服务器发起，测到目标的延迟和路线</span></header>
    <form class="net-form" autocomplete="off" novalidate @submit.prevent="toggle">
      <div class="net-line">
        <div class="sv-ranges net-modes" role="radiogroup" aria-label="测试方式">
          <button v-for="[value, label] in PROBE_MODES" :key="value" type="button" role="radio" :aria-checked="form.mode === value" :disabled="busy" @click="setMode(value)">{{ label }}</button>
        </div>
        <label class="net-field net-target"><span class="net-label">目标</span>
          <SuggestInput v-model="form.target" :options="options" class="mono" :class="{invalid: errors.target}" :disabled="busy" maxlength="300" placeholder="域名或 IP" aria-label="目标"
            autocapitalize="off" spellcheck="false" enterkeyhint="go" @update:model-value="clearError('target')" />
          <small v-if="errors.target" class="ob-bad">{{ errors.target }}</small>
        </label>
        <label v-if="form.mode === 'tcping'" class="net-field net-port"><span class="net-label">端口</span>
          <input v-model.trim="form.port" v-bind="noAutofill" class="mono" :class="{invalid: errors.port}" :disabled="busy" inputmode="numeric" maxlength="5" placeholder="必填" aria-label="端口" enterkeyhint="go" @input="clearError('port')" />
          <small v-if="errors.port" class="ob-bad">{{ errors.port }}</small>
        </label>
        <el-button class="net-go" :type="busy ? 'default' : 'primary'" native-type="submit" :loading="phase === 'starting' || phase === 'stopping'" :disabled="!online && !busy">{{ busy ? '停止' : '开始' }}</el-button>
      </div>
      <!-- The detail page on a wide screen has the room: the options stay out, nothing to unfold. -->
      <details ref="advanced" class="net-advanced" :class="{'is-inline': inline}" :open="inline || undefined">
        <summary>高级选项</summary>
        <div class="net-adv-grid">
          <label class="net-field"><span>{{ countLabel(form.mode) }}</span>
            <input v-model.trim="form.count" v-bind="noAutofill" :class="{invalid: errors.count}" :disabled="busy" inputmode="numeric" maxlength="3" :placeholder="String(countRange(form.mode)[2])" :aria-label="countLabel(form.mode)" @input="clearError('count')" />
            <small v-if="errors.count" class="ob-bad">{{ errors.count }}</small>
          </label>
          <label class="net-field"><span>间隔（毫秒）</span>
            <input v-model.trim="form.intervalMs" v-bind="noAutofill" :class="{invalid: errors.intervalMs}" :disabled="busy" inputmode="numeric" maxlength="5" :placeholder="String(DEFAULTS.intervalMs)" aria-label="间隔（毫秒）" @input="clearError('intervalMs')" />
            <small v-if="errors.intervalMs" class="ob-bad">{{ errors.intervalMs }}</small>
          </label>
          <label class="net-field"><span>超时（毫秒）</span>
            <input v-model.trim="form.timeoutMs" v-bind="noAutofill" :class="{invalid: errors.timeoutMs}" :disabled="busy" inputmode="numeric" maxlength="5" :placeholder="String(DEFAULTS.timeoutMs)" aria-label="超时（毫秒）" @input="clearError('timeoutMs')" />
            <small v-if="errors.timeoutMs" class="ob-bad">{{ errors.timeoutMs }}</small>
          </label>
          <div class="net-field net-family-field"><span>协议</span>
            <div class="sv-ranges net-families" role="radiogroup" aria-label="协议" :aria-invalid="Boolean(errors.family) || undefined">
              <button v-for="[value, label] in FAMILIES" :key="value" type="button" role="radio" :aria-checked="Number(form.family) === value" :disabled="busy" @click="setFamily(value)">{{ label }}</button>
            </div>
            <small v-if="errors.family" class="ob-bad">{{ errors.family }}</small>
          </div>
        </div>
      </details>
      <p v-if="!online" class="instance-muted net-state">节点离线，上线后才能测试。</p>
      <p v-if="errors.form" class="ob-bad net-state">{{ errors.form }}</p>
      <p v-if="startError" class="ob-bad net-state" role="alert">{{ startError }}</p>
    </form>

    <div v-if="run || status" class="net-result" aria-label="测试结果">
      <div class="net-result-head">
        <span class="mono net-result-target">{{ heading }}</span>
        <span v-if="shownState" class="pb-tag" :data-tone="PROBE_STATES[shownState]?.[0]">{{ PROBE_STATES[shownState]?.[1] || shownState }}</span>
      </div>
      <p v-if="failed" class="ob-bad net-state net-failed">{{ failed.title }}<small v-if="failed.detail" class="instance-muted mono">{{ failed.detail }}</small></p>
      <p v-if="note" class="instance-muted net-state" role="status">{{ note }}</p>

      <!-- A target in the mesh: the machines the traffic passes, and the end-to-end ping under it. -->
      <section v-if="meshTrace" class="net-mesh-path" aria-label="组网路径">
        <header class="net-sub-head"><h4>组网路径</h4><span class="instance-muted">按各台上报的转发表，中转不回应路由追踪</span></header>
        <p v-if="meshPath.brokenText" class="ob-bad net-path-line">{{ meshPath.hops.length ? `${meshPath.text}，` : '' }}{{ meshPath.brokenText }}</p>
        <p v-else class="net-path-line">{{ meshPath.text }}</p>
        <p class="net-path-total">
          <strong v-if="meshPath.totalText">{{ meshPath.totalText }}</strong>
          <span v-if="meshPath.stale" class="ob-warn">数据不完整：有的线路没有上报，路径和延迟可能不准</span>
          <button v-if="board && meshPath.hops.length" type="button" class="ix-text-button net-on-board" :aria-pressed="shownPath === pathKey" @click="showOnBoard">在画板上查看</button>
        </p>
      </section>

      <template v-if="kind !== 'trace' || meshTrace">
        <h4 v-if="meshTrace" class="net-sub-title">端到端 Ping</h4>
        <dl v-if="summary" class="net-summary" :class="{'is-tiles': card}">
          <div v-for="[label, value] in summary" :key="label" :data-tone="label === '丢包' && st.summary?.lossPct > 0 ? (st.summary.lossPct >= 20 ? 'bad' : 'warn') : undefined"><dt>{{ label }}</dt><dd>{{ value }}</dd></div>
        </dl>
        <div v-if="card && !meshTrace && bars.length > 1" class="net-bars" aria-hidden="true">
          <i v-for="b in bars" :key="b.seq" :style="{height: b.h + '%'}" :data-fail="b.fail || undefined" :title="b.text" />
        </div>
        <p v-else-if="busy" class="instance-muted net-state" role="status">等待第一次结果…</p>
        <p v-if="meshPath && !meshTrace" class="instance-muted net-through">经过：{{ meshPath.broken ? meshPath.brokenText : meshPath.through }}</p>
        <button v-if="!meshTrace && results.length > ROWS" type="button" class="ix-text-button mesh-fold-toggle net-more" :aria-expanded="showAll" @click="showAll = !showAll">{{ showAll ? `只看最近 ${ROWS} 次` : `显示全部 ${results.length} 次` }}</button>
        <ol v-if="!meshTrace && results.length" class="net-results">
          <li v-for="r in shownResults" :key="r.seq" :data-fail="failureKind(r) || undefined">
            <span class="net-seq">{{ r.seq }}</span>
            <span class="net-rtt">{{ resultText(r) }}</span>
            <span class="net-ttl instance-muted">{{ r.ok && r.ttl ? `TTL ${r.ttl}` : '' }}</span>
          </li>
        </ol>
      </template>

      <template v-else>
        <p v-if="!hops.length && busy" class="instance-muted net-state" role="status">等待第一轮结果…</p>
        <!-- Phone: per address a line with the hop, address and name, the average on the right of the hop's first line;
             under it in small print the place, and the loss when there is any. -->
        <ol v-if="hops.length && phone" class="net-hops" aria-label="路由追踪">
          <li v-for="h in hops" :key="h.ttl" :data-reached="h.reached || undefined" :data-silent="!h.addrs?.length || undefined" :data-pending="hopPending(h) || undefined">
            <div v-if="hopPending(h)" class="net-hop-line">
              <span class="net-hop-ttl">{{ h.ttl }}</span>
              <span v-if="busy" class="net-hop-addr"><span class="net-geo-wait" aria-label="等待结果">等待中</span></span>
              <span v-else class="net-hop-addr instance-muted">未测</span>
            </div>
            <div v-else-if="!h.addrs?.length" class="net-hop-line">
              <span class="net-hop-ttl">{{ h.ttl }}</span><span class="net-hop-addr">*</span><span class="net-hop-rtt">*</span>
            </div>
            <template v-else><template v-for="(a, i) in h.addrs" :key="a.ip">
              <div class="net-hop-line" :title="a.name ? `${a.ip}  ${a.name}` : a.ip">
                <span class="net-hop-ttl">{{ i ? '' : h.ttl }}</span>
                <span class="net-hop-addr"><span class="mono">{{ a.ip }}</span><small v-if="a.name" class="instance-muted">{{ a.name }}</small></span>
                <span v-if="!i" class="net-hop-rtt">{{ msText(h.avgMs) || '*' }}</span>
              </div>
              <div class="net-hop-line net-hop-sub" :title="geoTitle(a.geo)">
                <span class="net-hop-ttl" />
                <span class="net-hop-addr">
                  <template v-if="a.geo"><span v-if="flag(a.geo.country)" class="sv-flag" aria-hidden="true">{{ flag(a.geo.country) }}</span><span class="net-hop-place">{{ geoText(a.geo) || a.geo.countryName }}</span></template>
                  <span v-else-if="geoPending(a) && !geoDone" class="net-geo-wait" aria-label="正在查询归属地">查询中</span>
                  <span v-else class="net-hop-place">{{ a.geoSkipped ? '未查询' : geoPending(a) ? '未查到' : GEO_SCOPES[a.scope] || '' }}</span>
                </span>
                <span v-if="!i && lossShown(h)" class="net-hop-loss">丢包 {{ pctText(h.lossPct) }}</span>
              </div>
            </template></template>
          </li>
        </ol>
        <div v-else-if="hops.length" class="net-trace-wrap">
          <table class="net-trace">
            <thead><tr><th class="net-num">跳</th><th>地址</th><th>归属地</th><th class="net-num">丢包</th><th class="net-num">平均</th><th class="net-num">最好</th><th class="net-num">最差</th></tr></thead>
            <tbody>
              <tr v-for="h in hops" :key="h.ttl" :data-reached="h.reached || undefined" :data-silent="!h.addrs?.length || undefined" :data-pending="hopPending(h) || undefined">
                <td class="net-num">{{ h.ttl }}</td>
                <!-- Not tried yet: a placeholder while it runs (not "0% loss"), "未测" once it has ended. -->
                <td v-if="hopPending(h)" class="net-trace-addr" colspan="6">
                  <span v-if="busy" class="net-line-cell"><span class="net-geo-wait" aria-label="等待结果">等待中</span></span>
                  <span v-else class="net-line-cell instance-muted">未测</span>
                </td>
                <template v-else>
                <td class="net-trace-addr">
                  <template v-if="h.addrs?.length">
                    <span v-for="a in h.addrs" :key="a.ip" class="net-line-cell" :title="a.name ? `${a.ip}  ${a.name}` : a.ip"><span class="mono">{{ a.ip }}</span><small v-if="a.name" class="instance-muted">{{ a.name }}</small></span>
                  </template>
                  <span v-else class="net-line-cell instance-muted">*</span>
                </td>
                <td class="net-trace-geo">
                  <template v-if="h.addrs?.length">
                    <span v-for="a in h.addrs" :key="a.ip" class="net-line-cell" :title="geoTitle(a.geo)">
                      <template v-if="a.geo"><span v-if="flag(a.geo.country)" class="sv-flag" aria-hidden="true">{{ flag(a.geo.country) }}</span><span>{{ geoText(a.geo) || a.geo.countryName }}</span></template>
                      <span v-else-if="geoPending(a) && !geoDone" class="net-geo-wait" aria-label="正在查询归属地">查询中</span>
                      <span v-else class="instance-muted">{{ a.geoSkipped ? '未查询' : geoPending(a) ? '未查到' : GEO_SCOPES[a.scope] || '—' }}</span>
                    </span>
                  </template>
                  <span v-else class="net-line-cell instance-muted">*</span>
                </td>
                <td class="net-num">{{ pctText(h.lossPct) }}</td>
                <td class="net-num">{{ h.addrs?.length ? msText(h.avgMs) || '*' : '*' }}</td>
                <td class="net-num">{{ h.addrs?.length ? msText(h.minMs) || '*' : '*' }}</td>
                <td class="net-num">{{ h.addrs?.length ? msText(h.maxMs) || '*' : '*' }}</td>
                </template>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="hops.length && phase === 'ended' && st.state !== 'error' && !st.reached" class="instance-muted net-state">目标没有回应，表里是途经的路由器；目标可能不回应 ping。</p>
      </template>
    </div>
  </section>
</template>
