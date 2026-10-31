<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { ChevronRight, Columns2, Plus } from '@lucide/vue';
import { Api } from '../../api.js';
import { MOBILE_VIEWPORT_QUERY } from '../../mobileViewport.js';
import { ensureFlagFont, flag, safeError } from '../serverHelpers.js';
import { usePoll } from '../mesh/usePoll.js';
import DdnsSheet from '../../ddns/DdnsSheet.vue';
import BenchCompare from './BenchCompare.vue';
import BenchReport from './BenchReport.vue';
import BenchRunForm from './BenchRunForm.vue';
import { buildTree, eligible, isActive, runTime, stateText, stateTone } from './benchHelpers.js';
import '../../instances/instances.css';
import '../servers.css';
import '../../ddns/ddns.css';
import './bench.css';

// 评测, a tab of the servers page: on the left every server's runs as a tree (server → category →
// test → run), on the right the chosen run's report, or the checked runs side by side. A phone shows
// the tree alone; reports, the comparison and the form open as the bottom sheet.
// servers: the servers page's list; active: the tab is shown (it polls only then).
const props = defineProps({servers: {type: Array, default: () => []}, active: {type: Boolean, default: true}});
const catalog = ref(null), runs = ref([]), loading = ref(true), loadError = ref(''), now = ref(Date.now()), phone = ref(false);
const details = reactive({}), expanded = reactive({});
const focused = ref(''), picked = ref([]), comparing = ref(false), sheet = ref(''), preset = ref(null), busy = ref(''), formRef = ref(null);
let first = true, media;
const detailRequests = new Map(), detailFailures = new Set();

const tree = computed(() => buildTree(props.servers, runs.value, catalog.value));
const runnable = computed(() => props.servers.filter(eligible));
const maxPick = computed(() => (phone.value ? 2 : 4));
const serverOf = run => props.servers.find(s => s.id === run?.serverId) || null;
// The run as the page shows it: its details once read, the latest summary meanwhile.
function shown(id) {
  const summary = runs.value.find(r => r.id === id);
  const detail = details[id];
  if (!summary) return detail || null;
  return detail ? {...detail, ...summary, sections: detail.sections, log: detail.log} : {...summary, sections: [], log: ''};
}
const focusedRun = computed(() => (focused.value ? shown(focused.value) : null));
const pickedRuns = computed(() => picked.value.map(shown).filter(Boolean));
const open = (key, fallback = true) => expanded[key] ?? fallback;
const toggle = (key, fallback = true) => { expanded[key] = !open(key, fallback); };

function needsDetail(id) {
  return !details[id] || detailFailures.has(id) || details[id].state !== runs.value.find(r => r.id === id)?.state;
}
async function fetchDetail(id) {
  if (detailRequests.has(id)) return detailRequests.get(id);
  const request = (async () => {
    try { details[id] = (await Api.benchRun(id)).run; detailFailures.delete(id); }
    catch { detailFailures.add(id); }
    finally { detailRequests.delete(id); }
  })();
  detailRequests.set(id, request);
  return request;
}
async function load() {
  try {
    if (!catalog.value) catalog.value = await Api.benchCatalog();
    const list = (await Api.benchRuns()).runs || [];
    runs.value = list; now.value = Date.now(); loadError.value = '';
    if (first) {
      first = false;
      for (const id of new Set(list.map(r => r.serverId))) if (expanded[`s:${id}`] === undefined) expanded[`s:${id}`] = true;
    }
    const updates = list.filter(run => (details[run.id] || focused.value === run.id || picked.value.includes(run.id)) && needsDetail(run.id));
    await Promise.all(updates.map(run => fetchDetail(run.id)));
    const ids = new Set(list.map(r => r.id));
    for (const id of detailFailures) if (!ids.has(id)) detailFailures.delete(id);
    picked.value = picked.value.filter(id => ids.has(id));
    if (focused.value && !ids.has(focused.value)) { focused.value = ''; if (sheet.value === 'report') sheet.value = ''; }
  } catch (e) { loadError.value = safeError(e); }
  finally { loading.value = false; }
}
const poll = usePoll(load, {interval: () => (runs.value.some(isActive) || detailFailures.size ? 4000 : 30000), active: () => props.active});
const refresh = () => poll.run();

function openRun(run) {
  focused.value = run.id; comparing.value = false;
  if (needsDetail(run.id)) void fetchDetail(run.id);
  if (phone.value) sheet.value = 'report';
}
function togglePick(run) {
  const index = picked.value.indexOf(run.id);
  if (index >= 0) picked.value.splice(index, 1);
  else if (picked.value.length >= maxPick.value) ElMessage.info(`最多同时对比 ${maxPick.value} 条`);
  else picked.value.push(run.id);
}
function compare() {
  for (const id of picked.value) if (needsDetail(id)) void fetchDetail(id);
  comparing.value = true;
  if (phone.value) sheet.value = 'compare';
}
function clearPicks() { picked.value = []; comparing.value = false; if (sheet.value === 'compare') sheet.value = ''; }
function openForm(value = null) { preset.value = value; sheet.value = 'form'; }
function closeSheet(name) { if (sheet.value === name) sheet.value = ''; }
function started(created) {
  sheet.value = '';
  ElMessage.success(created.length > 1 ? `已排入 ${created.length} 项评测，会依次运行` : '评测已开始');
  const run = created[0];
  if (run) {
    expanded[`s:${run.serverId}`] = true;
    runs.value = [...created, ...runs.value];
    if (!phone.value) openRun(run);
  }
  void refresh();
}
async function stopRun(run) {
  busy.value = run.id;
  try { await Api.stopBench(run.id); }
  catch (e) { ElMessage.error(safeError(e)); }
  finally { busy.value = ''; void refresh(); }
}
async function deleteRun(run) {
  try {
    await ElMessageBox.confirm(`删除这条评测记录？\n${serverOf(run)?.name || ''} · ${run.name} · ${runTime(run.createdMs)}`, '删除评测记录',
      {confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  busy.value = run.id;
  try {
    await Api.deleteBench(run.id);
    delete details[run.id];
    if (focused.value === run.id) { focused.value = ''; closeSheet('report'); }
    picked.value = picked.value.filter(id => id !== run.id);
    ElMessage.success('已删除');
  } catch (e) { ElMessage.error(safeError(e)); }
  finally { busy.value = ''; void refresh(); }
}
function syncPhone() { phone.value = media.matches; if (phone.value && picked.value.length > 2) picked.value = picked.value.slice(0, 2); }
onMounted(() => {
  ensureFlagFont();
  media = window.matchMedia(MOBILE_VIEWPORT_QUERY);
  syncPhone();
  media.addEventListener?.('change', syncPhone);
  void refresh();
});
onBeforeUnmount(() => media?.removeEventListener?.('change', syncPhone));
</script>
<template>
  <section class="dd-ui bn-page" aria-label="评测">
    <p v-if="loadError" role="alert" class="instance-error instance-banner">{{ loadError }} <button type="button" class="instance-link" @click="refresh">重试</button></p>
    <div class="bn-body">
      <nav class="bn-tree" aria-label="评测记录">
        <header class="bn-tree-head">
          <h2>评测记录</h2>
          <span class="ob-grow" />
          <span class="dd-actions"><el-button type="primary" :disabled="!catalog" data-testid="bn-new" @click="openForm()"><Plus :size="15" aria-hidden="true" class="dd-btn-icon" />运行评测</el-button></span>
        </header>
        <div v-if="picked.length" class="bn-pickbar" role="status">
          <span>已选 {{ picked.length }} 条</span>
          <span class="ob-grow" />
          <button type="button" class="ix-text-button" @click="clearPicks">清除</button>
          <el-button size="small" :disabled="picked.length < 2" data-testid="bn-compare" @click="compare"><Columns2 :size="14" aria-hidden="true" class="dd-btn-icon" />并排对比</el-button>
        </div>
        <ul v-if="tree.length" class="bn-nodes" role="tree" aria-label="服务器">
          <li v-for="node in tree" :key="node.server.id" role="treeitem" :aria-expanded="open(`s:${node.server.id}`, false)" :data-server="node.server.id">
            <div class="bn-row bn-row-server">
              <button type="button" class="bn-row-main" @click="toggle(`s:${node.server.id}`, false)">
                <ChevronRight :size="14" class="bn-chev" :class="{'is-open': open(`s:${node.server.id}`, false)}" aria-hidden="true" />
                <span class="sv-flag" aria-hidden="true">{{ flag(node.server.region) }}</span>
                <span class="bn-name">{{ node.server.name }}</span>
                <span class="bn-count" :data-tone="node.active ? 'busy' : undefined">{{ node.active ? `${node.active} 项进行中` : node.runs || '' }}</span>
              </button>
              <button v-if="node.eligible" type="button" class="ix-icon-button bn-add" :aria-label="`在 ${node.server.name} 上运行评测`" title="运行评测" @click="openForm({serverId: node.server.id})"><Plus :size="15" /></button>
            </div>
            <ul v-if="open(`s:${node.server.id}`, false)" role="group">
              <li v-for="cat in node.categories" :key="cat.id" role="treeitem" :aria-expanded="open(`c:${node.server.id}:${cat.id}`)">
                <div class="bn-row bn-row-cat">
                  <button type="button" class="bn-row-main" @click="toggle(`c:${node.server.id}:${cat.id}`)">
                    <ChevronRight :size="13" class="bn-chev" :class="{'is-open': open(`c:${node.server.id}:${cat.id}`)}" aria-hidden="true" /><span class="bn-name">{{ cat.name }}</span>
                  </button>
                </div>
                <ul v-if="open(`c:${node.server.id}:${cat.id}`)" role="group">
                  <li v-for="t in cat.tests" :key="t.test.id" role="treeitem" :aria-expanded="open(`t:${node.server.id}:${t.test.id}`)">
                    <div class="bn-row bn-row-test">
                      <button type="button" class="bn-row-main" @click="toggle(`t:${node.server.id}:${t.test.id}`)">
                        <ChevronRight :size="13" class="bn-chev" :class="{'is-open': open(`t:${node.server.id}:${t.test.id}`)}" aria-hidden="true" />
                        <span class="bn-name">{{ t.test.name }}</span><span class="bn-count">{{ t.runs.length }}</span>
                      </button>
                      <button v-if="node.eligible" type="button" class="ix-icon-button bn-add" :aria-label="`在 ${node.server.name} 上运行 ${t.test.name}`" title="再运行一次"
                        @click="openForm({serverId: node.server.id, test: t.test.id})"><Plus :size="15" /></button>
                    </div>
                    <ul v-if="open(`t:${node.server.id}:${t.test.id}`)" role="group">
                      <li v-for="r in t.runs" :key="r.id" role="treeitem" :data-run="r.id">
                        <div class="bn-row bn-row-run" :aria-current="focused === r.id && !comparing">
                          <label class="bn-pick" :title="'选中后可以并排对比'">
                            <input type="checkbox" :checked="picked.includes(r.id)" :aria-label="`选中 ${runTime(r.createdMs)} 用于对比`" @change="togglePick(r)" />
                          </label>
                          <button type="button" class="bn-row-main" @click="openRun(r)">
                            <span class="bn-time">{{ runTime(r.createdMs) }}</span>
                            <span class="bn-label">{{ r.label }}</span>
                            <span v-if="r.state !== 'done'" class="ix-pill" :data-tone="stateTone(r.state)">{{ stateText(r) }}</span>
                          </button>
                        </div>
                      </li>
                    </ul>
                  </li>
                </ul>
              </li>
            </ul>
          </li>
        </ul>
        <p v-else class="instance-empty bn-empty">{{ loading ? '正在读取…' : runnable.length ? '还没有评测记录，点“运行评测”开始。'
          : '没有能运行评测的服务器：需要已接入节点的 Linux 服务器（amd64 或 arm64）。' }}</p>
      </nav>

      <section v-if="!phone" class="bn-main">
        <template v-if="comparing && pickedRuns.length >= 2">
          <div class="bn-main-bar">
            <b>并排对比 {{ pickedRuns.length }} 条</b><span class="instance-muted">滚动任意一列，其余列跟着走</span>
            <span class="ob-grow" />
            <button type="button" class="ix-text-button" @click="comparing = false">退出对比</button>
          </div>
          <BenchCompare :runs="pickedRuns" :server-of="serverOf" :now="now" :active="active" />
        </template>
        <div v-else-if="focusedRun" class="bn-main-scroll">
          <BenchReport :key="focusedRun.id" :run="focusedRun" :server="serverOf(focusedRun)" :now="now" :active="active" :busy="busy === focusedRun.id" @stop="stopRun" @delete="deleteRun" />
        </div>
        <div v-else class="bn-intro">
          <h3>服务器评测</h3>
          <p>在服务器上运行 IP 质量、网络质量、硬件、流媒体与 AI 解锁等检测，每次的报告都存下来。在左侧点一条记录查看报告；勾选两到四条可以并排对比，同一台服务器前后的变化或不同服务器之间的差别一眼就能看出来。</p>
          <p class="instance-muted">检测脚本由 OpenBear 固定版本托管，运行时推送到节点执行，不从上游仓库现拉；默认不上传结果。</p>
          <el-button :disabled="!catalog || !runnable.length" @click="openForm()"><Plus :size="15" aria-hidden="true" class="dd-btn-icon" />运行评测</el-button>
        </div>
      </section>
    </div>

    <DdnsSheet v-if="phone" :model-value="sheet === 'report' && Boolean(focusedRun)" kind="drawer" :phone="phone" title="评测报告"
      @update:model-value="v => { if (!v) closeSheet('report'); }">
      <BenchReport v-if="focusedRun" :key="focusedRun.id" :run="focusedRun" :server="serverOf(focusedRun)" :now="now" :active="active && sheet === 'report'" :busy="busy === focusedRun.id" @stop="stopRun" @delete="deleteRun" />
    </DdnsSheet>
    <DdnsSheet v-if="phone" :model-value="sheet === 'compare' && pickedRuns.length >= 2" kind="drawer" :phone="phone" title="对比"
      @update:model-value="v => { if (!v) closeSheet('compare'); }">
      <BenchCompare :runs="pickedRuns" :server-of="serverOf" :now="now" :active="active && sheet === 'compare'" phone />
    </DdnsSheet>
    <DdnsSheet :model-value="sheet === 'form'" :phone="phone" title="运行评测" sub="脚本由 OpenBear 托管并推送到节点运行；同一台服务器上的评测依次进行" width="560px" :busy="Boolean(formRef?.busy)"
      @update:model-value="v => { if (!v) closeSheet('form'); }">
      <BenchRunForm v-if="sheet === 'form' && catalog" ref="formRef" :catalog="catalog" :servers="runnable" :preset="preset" @started="started" @cancel="closeSheet('form')" />
    </DdnsSheet>
  </section>
</template>
