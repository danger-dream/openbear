<script setup>
import { computed, onMounted, ref } from "vue";
import { encode } from "gpt-tokenizer";
import { Api, apiError } from "../api";
import { ElMessage } from "element-plus";

const logs = ref([]);
const todayLogs = ref([]);
const auditLogs = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const loading = ref(false);
const detail = ref(null);
const drawerOpen = ref(false);
const detailTab = ref("output");
const activeTab = ref("render");

function tokenCount(text) {
  if (!text) return 0;
  try { return encode(text).length; } catch { return Math.ceil(String(text).length / 2); }
}
function formatNum(n) { return Number(n || 0).toLocaleString(); }
function fmtTime(ts) { return ts ? new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false }) : "—"; }
function isToday(ts) {
  if (!ts) return false;
  const d = new Date(Number(ts) * 1000);
  const now = new Date();
  return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
}
const todayStats = computed(() => {
  const rows = todayLogs.value.filter((r) => isToday(r.ts));
  const count = rows.length;
  const chars = rows.reduce((s, r) => s + Number(r.output_len || 0), 0);
  const avgMs = count ? Math.round(rows.reduce((s, r) => s + Number(r.ms || 0), 0) / count) : 0;
  return { count, chars, avgMs };
});

async function load() {
  loading.value = true;
  try {
    const [list, stats, audit] = await Promise.all([
      Api.renderLogs({ page: page.value, pageSize: pageSize.value }),
      Api.renderLogs({ page: 1, pageSize: 200 }),
      Api.auditLogs({ page: 1, pageSize: 100 }),
    ]);
    logs.value = list.items || [];
    total.value = list.total || 0;
    todayLogs.value = stats.items || [];
    auditLogs.value = audit.items || [];
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally { loading.value = false; }
}
onMounted(load);

async function viewDetail(row) {
  try {
    const data = await Api.renderLog(row.id);
    if (data?.ok === false) throw new Error(data.error || "日志不存在");
    detail.value = data.item || data;
    detailTab.value = "output";
    drawerOpen.value = true;
  } catch (error) { ElMessage.error(apiError(error)); }
}
function onPageChange(p) { page.value = p; load(); }
function onSizeChange(s) { pageSize.value = s; page.value = 1; load(); }
function prettyParams(s) { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; } }
function prettyJson(value) { try { return JSON.stringify(value, null, 2); } catch { return String(value || ""); } }
</script>

<template>
  <div class="h-full flex flex-col">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="flex items-center gap-2">
        <h1 class="text-base font-semibold">系统日志</h1>
        <span class="text-xs text-macsub">提示词渲染记录 + Web 审计日志</span>
      </div>
      <el-button :icon="'Refresh'" @click="load" round :loading="loading">刷新</el-button>
    </header>

    <div class="grid grid-cols-4 gap-3 px-6 pt-5 shrink-0">
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">今日组装</div><div class="text-lg font-semibold">{{ formatNum(todayStats.count) }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">今日字符</div><div class="text-lg font-semibold">{{ formatNum(todayStats.chars) }}</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">平均耗时</div><div class="text-lg font-semibold">{{ todayStats.avgMs }}ms</div></div>
      <div class="mac-panel px-4 py-3"><div class="text-[11px] text-macsub">日志总数</div><div class="text-lg font-semibold">{{ formatNum(total) }}</div></div>
    </div>

    <div class="px-6 pt-4 shrink-0">
      <el-tabs v-model="activeTab" class="mac-logs-tabs">
        <el-tab-pane label="Render logs" name="render" />
        <el-tab-pane label="Audit logs" name="audit" />
      </el-tabs>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto px-6 pb-6">
      <template v-if="activeTab === 'render'">
        <el-table :data="logs" size="small" stripe class="mac-shadow rounded-xl overflow-hidden" @row-click="viewDetail" v-loading="loading">
          <el-table-column label="时间" width="170"><template #default="{ row }">{{ fmtTime(row.ts) }}</template></el-table-column>
          <el-table-column prop="source" label="来源" width="120" show-overflow-tooltip />
          <el-table-column prop="client_ip" label="调用IP" width="130" show-overflow-tooltip />
          <el-table-column prop="template_name" label="模板" width="180" show-overflow-tooltip />
          <el-table-column prop="output_len" label="输出字符" width="100"><template #default="{ row }">{{ formatNum(row.output_len) }}</template></el-table-column>
          <el-table-column prop="ms" label="耗时ms" width="90" />
          <el-table-column prop="params_json" label="参数摘要" show-overflow-tooltip><template #default="{ row }"><span class="text-xs text-macsub font-mono">{{ row.params_json?.slice(0, 140) }}</span></template></el-table-column>
          <el-table-column label="" width="70"><template #default="{ row }"><el-button size="small" text type="primary" @click.stop="viewDetail(row)">详情</el-button></template></el-table-column>
        </el-table>
        <div class="mt-4 flex justify-end">
          <el-pagination background layout="total, sizes, prev, pager, next, jumper" :total="total" :current-page="page" :page-size="pageSize" :page-sizes="[10, 20, 50, 100]" @current-change="onPageChange" @size-change="onSizeChange" />
        </div>
      </template>

      <template v-else>
        <el-table :data="auditLogs" size="small" stripe class="mac-shadow rounded-xl overflow-hidden" v-loading="loading">
          <el-table-column label="时间" width="170"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
          <el-table-column prop="kind" label="动作" min-width="220" show-overflow-tooltip />
          <el-table-column prop="actor" label="来源" width="90" />
          <el-table-column prop="chat_id" label="Chat" width="130" />
          <el-table-column prop="ip" label="IP" width="130" show-overflow-tooltip />
          <el-table-column label="详情" min-width="360" show-overflow-tooltip><template #default="{ row }"><code>{{ prettyJson(row.detail || {}) }}</code></template></el-table-column>
        </el-table>
      </template>
    </div>

    <el-drawer v-model="drawerOpen" title="系统日志详情" size="60%">
      <div v-if="detail" class="h-full flex flex-col">
        <div class="flex gap-4 text-xs text-macsub mb-3 flex-wrap">
          <span>#{{ detail.id }}</span>
          <span>{{ fmtTime(detail.ts) }}</span>
          <span>来源: {{ detail.source || '—' }}</span>
          <span>IP: {{ detail.client_ip || '—' }}</span>
          <span>模板: {{ detail.template_name || '—' }}</span>
          <span>输出: {{ formatNum(detail.output_len) }} 字符</span>
          <span>{{ formatNum(tokenCount(detail.output || '')) }} tokens</span>
          <span>耗时: {{ detail.ms }}ms</span>
        </div>
        <el-tabs v-model="detailTab" class="flex-1 min-h-0 flex flex-col mac-detail-tabs">
          <el-tab-pane label="组装输出 (完整提示词)" name="output" class="h-full">
            <pre class="h-full overflow-auto m-0 p-3 text-xs leading-relaxed whitespace-pre-wrap break-words font-mono bg-black/[0.02] rounded-lg">{{ detail.output || '(此条日志无输出)' }}</pre>
          </el-tab-pane>
          <el-tab-pane label="输入参数" name="params" class="h-full">
            <pre class="h-full overflow-auto m-0 p-3 text-xs leading-relaxed whitespace-pre-wrap break-words font-mono bg-black/[0.02] rounded-lg">{{ prettyParams(detail.params_json) }}</pre>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-drawer>
  </div>
</template>

<style>
.mac-detail-tabs .el-tabs__content { flex: 1; min-height: 0; }
.mac-detail-tabs .el-tab-pane { height: 100%; }
.mac-logs-tabs .el-tabs__header { margin-bottom: 0; }
</style>
