<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import draggable from "vuedraggable";
import { assetTimeLine } from "../utils/assetTime";
import { dragAutoScrollOptions } from "../utils/dragScroll";

const secrets = ref([]);
const loading = ref(false);
const editing = ref(null);
const original = ref("");
const dialogOpen = ref(false);
const showSecretValues = ref(false);
const showArchived = ref(false);
const selectedIds = ref([]);
const groups = ref([]);
const scrollContainer = ref(null);
const dragging = ref(false);

async function load() {
  loading.value = true;
  try {
    const data = await Api.secrets(true, showArchived.value);
    secrets.value = data.items || [];
    rebuildGroups();
  } finally { loading.value = false; }
}
async function refresh() { await load(); clearSelection(); ElMessage.success("已刷新"); }
onMounted(load);
const shownSecrets = computed(() => secrets.value.filter((s) => showArchived.value || !s.archived));
const selectedSecrets = computed(() => shownSecrets.value.filter((s) => selectedIds.value.includes(s.id)));
const allShownSelected = computed(() => shownSecrets.value.length > 0 && shownSecrets.value.every((s) => selectedIds.value.includes(s.id)));
const groupNames = computed(() => [...new Set(secrets.value.map((s) => s.grp).filter(Boolean))]);
function rebuildGroups() {
  const map = new Map();
  const order = [];
  for (const secret of shownSecrets.value) {
    const name = secret.grp || "";
    if (!map.has(name)) { map.set(name, []); order.push(name); }
    map.get(name).push(secret);
  }
  groups.value = order.map((name) => ({ name, items: map.get(name) }));
}

function isSelected(id) { return selectedIds.value.includes(id); }
function setSelected(id, checked) { selectedIds.value = checked ? [...new Set([...selectedIds.value, id])] : selectedIds.value.filter((x) => x !== id); }
function clearSelection() { selectedIds.value = []; }
function toggleSelectAllShown(v) { selectedIds.value = v ? shownSecrets.value.map((s) => s.id) : []; }
function secretPayload(s, patch = {}) { return { ...s, grp: s.grp || "", kvJson: JSON.stringify(s.kv || [], null, 2), enabled: s.enabled ?? 1, archived: s.archived || 0, ...patch }; }
function maskValue(value) {
  const s = String(value ?? "");
  if (!s) return "";
  if (s.length <= 6) return "••••••";
  return `${s.slice(0, 2)}••••••${s.slice(-2)}`;
}
function boolInt(v) { return v === true || v === 1 || v === "1" ? 1 : 0; }
function editSnapshot(s) {
  if (!s) return "";
  return JSON.stringify({
    id: s.id ?? null,
    name: s.name || "",
    note: s.note || "",
    grp: s.grp || "",
    kv: Array.isArray(s.kv) ? s.kv.map((x) => ({ key: x.key || "", value: x.value || "" })) : [],
    enabled: boolInt(s.enabled),
    archived: boolInt(s.archived),
    sort: Number(s.sort || 0),
  });
}
async function openEdit(s) {
  let row = s ? JSON.parse(JSON.stringify(s)) : { id: null, name: "", note: "", grp: "", kv: [{ key: "", value: "" }], enabled: 1, archived: 0, sort: (secrets.value.length + 1) * 10 };
  if (s?.id) {
    try {
      const data = await Api.secret(s.id);
      if (data?.ok === false) throw new Error(data.error || "凭证不存在");
      row = data.item || row;
      const idx = secrets.value.findIndex((x) => x.id === row.id);
      if (idx >= 0) secrets.value.splice(idx, 1, row);
    } catch (error) {
      ElMessage.error("加载最新凭证失败: " + apiError(error));
      return;
    }
  }
  editing.value = row;
  if (!editing.value.kv?.length) editing.value.kv = [{ key: "", value: "" }];
  editing.value.grp = editing.value.grp || "";
  editing.value.enabled = boolInt(editing.value.enabled ?? 1);
  editing.value.archived = boolInt(editing.value.archived ?? 0);
  editing.value.sort = Number(editing.value.sort || 0);
  original.value = editSnapshot(editing.value);
  dialogOpen.value = true;
}
const dirty = () => editing.value && editSnapshot(editing.value) !== original.value;
function addKv() { editing.value.kv.push({ key: "", value: "" }); }
function delKv(i) { editing.value.kv.splice(i, 1); }
async function save() {
  const s = editing.value;
  if (!s.name?.trim()) { ElMessage.warning("名称不能为空"); return; }
  s.kv = s.kv.filter((x) => String(x.key || "").trim());
  try {
    const payload = { ...s, kvJson: JSON.stringify(s.kv || [], null, 2) };
    const r = s.id ? await Api.updateSecret(s.id, payload) : await Api.createSecret(payload);
    if (r?.ok === false) throw new Error(r.error || "保存失败");
    ElMessage.success("已保存");
    dialogOpen.value = false;
    await load();
  } catch (e) { ElMessage.error("保存失败: " + (e.message || e)); }
}
async function tryClose(done) {
  if (dirty()) {
    try { await ElMessageBox.confirm("有未保存的修改，确定关闭？", "提示", { type: "warning", confirmButtonText: "放弃", cancelButtonText: "继续编辑" }); }
    catch { return; }
  }
  done ? done() : (dialogOpen.value = false);
}
async function remove(s) {
  await ElMessageBox.confirm(`确定删除凭证「${s.name}」？不可恢复。`, "删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  const r = await Api.deleteSecret(s.id);
  if (r?.ok === false) throw new Error(r.error || "删除失败");
  ElMessage.success("已删除");
  await load();
}
async function toggleEnabled(s) {
  const r = await Api.updateSecret(s.id, secretPayload(s, { enabled: s.enabled ? 0 : 1 }));
  if (r?.ok === false) throw new Error(r.error || "更新失败");
  await load();
}
async function toggleArchived(s) {
  const next = s.archived ? 0 : 1;
  const r = await Api.updateSecret(s.id, secretPayload(s, { archived: next, enabled: next ? 0 : s.enabled }));
  if (r?.ok === false) throw new Error(r.error || "更新失败");
  ElMessage.success(next ? "已归档" : "已恢复");
  await load();
}
async function batchUpdateSecrets(patch, message) {
  const rows = [...selectedSecrets.value];
  for (const row of rows) {
    const r = await Api.updateSecret(row.id, secretPayload(row, patch));
    if (r?.ok === false) throw new Error(r.error || "批量更新失败");
  }
  ElMessage.success(message || `已处理 ${rows.length} 条`);
  clearSelection();
  await load();
}
async function batchDeleteSecrets() {
  const rows = [...selectedSecrets.value];
  await ElMessageBox.confirm(`确定删除选中的 ${rows.length} 条凭证？此操作不可恢复。`, "批量删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  for (const row of rows) {
    const r = await Api.deleteSecret(row.id);
    if (r?.ok === false) throw new Error(r.error || "批量删除失败");
  }
  ElMessage.success(`已删除 ${rows.length} 条凭证`);
  clearSelection();
  await load();
}
async function finishDrag() {
  dragging.value = false;
  await persistGroups();
}
async function persistGroups() {
  const flat = groups.value.flatMap((group) => group.items.map((secret) => ({ ...secret, grp: group.name })));
  flat.forEach((secret, idx) => (secret.sort = (idx + 1) * 10));
  secrets.value = flat;
  try {
    const r = await Api.reorder("secrets", flat.map((secret) => ({ id: secret.id, grp: secret.grp, sort: secret.sort })));
    if (r?.ok === false) throw new Error(r.error || "排序保存失败");
    ElMessage.success("顺序已保存");
  } catch (e) {
    ElMessage.error("排序保存失败: " + (e.message || e));
    await load();
  }
}
</script>

<template>
  <div class="h-full flex flex-col">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="flex items-center gap-2">
        <h1 class="text-base font-semibold">凭证库</h1>
        <span class="text-xs text-macsub">拖手柄排序 · 点卡片编辑 · 审计日志不记录 value 明文</span>
      </div>
      <div class="flex items-center gap-3">
        <el-checkbox v-model="showArchived" size="small" @change="load(); clearSelection()">显示归档</el-checkbox>
        <el-switch v-model="showSecretValues" size="small" active-text="显示明文" inactive-text="隐藏明文" />
        <el-button :icon="'Refresh'" circle @click="refresh" title="刷新" />
        <el-button type="primary" :icon="'Plus'" @click="openEdit(null)" round>新建凭证</el-button>
      </div>
    </header>

    <div v-if="selectedIds.length" class="mx-6 mt-3 px-3 py-2 rounded-2xl border border-macblue/20 bg-macblue/5 flex items-center gap-2 shrink-0">
      <el-checkbox :model-value="allShownSelected" @change="toggleSelectAllShown">全选当前列表</el-checkbox>
      <span class="text-xs text-macsub mr-2">已选 {{ selectedIds.length }} 条</span>
      <el-button size="small" :icon="'Box'" @click="batchUpdateSecrets({ archived: 1, enabled: 0 }, '已批量归档')">批量归档</el-button>
      <el-button size="small" :icon="'RefreshLeft'" @click="batchUpdateSecrets({ archived: 0 }, '已批量恢复')">恢复</el-button>
      <el-button size="small" :icon="'Unlock'" @click="batchUpdateSecrets({ enabled: 1, archived: 0 }, '已批量启用注入')">启用注入</el-button>
      <el-button size="small" :icon="'Lock'" @click="batchUpdateSecrets({ enabled: 0 }, '已批量禁用注入')">禁用注入</el-button>
      <el-button size="small" type="danger" :icon="'Delete'" @click="batchDeleteSecrets">删除</el-button>
      <el-button size="small" text @click="clearSelection">取消选择</el-button>
    </div>

    <div ref="scrollContainer" class="flex-1 min-h-0 overflow-y-auto p-6" :class="{ 'select-none': dragging }" v-loading="loading">
      <div v-if="!shownSecrets.length" class="text-center text-macsub py-16 text-sm">暂无凭证</div>
      <draggable v-model="groups" item-key="name" handle=".group-handle" :animation="180"
        v-bind="dragAutoScrollOptions" :scroll="scrollContainer"
        @choose="dragging = true" @unchoose="dragging = false" @end="finishDrag">
        <template #item="{ element: g }">
          <section class="mb-5">
            <div class="text-xs font-semibold text-macsub mb-2 flex items-center gap-2">
              <el-icon class="group-handle cursor-move select-none text-gray-300 hover:text-macsub" :size="14"><Rank /></el-icon>
              <span class="w-1 h-3.5 bg-macblue rounded"></span>{{ g.name || '未分组' }}
              <span class="opacity-50">({{ g.items.length }})</span>
            </div>
            <draggable v-model="g.items" item-key="id" handle=".drag-handle" :animation="180"
              v-bind="dragAutoScrollOptions" :scroll="scrollContainer"
              :group="{ name: 'secret-groups' }" @choose="dragging = true" @unchoose="dragging = false" @end="finishDrag"
              class="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3 gap-3 min-h-6">
              <template #item="{ element: s }">
                <div v-if="showArchived || !s.archived" @click="openEdit(s)"
            class="mac-panel mac-shadow p-4 flex flex-col gap-2 cursor-pointer hover:border-macblue/50 transition-colors"
            :class="{ 'opacity-50 border-dashed': s.archived || !s.enabled, 'ring-1 ring-macblue/30 bg-macblue/5': isSelected(s.id) }">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2 min-w-0">
                <div @click.stop><el-checkbox :model-value="isSelected(s.id)" @change="(v) => setSelected(s.id, v)" /></div>
                <el-icon class="drag-handle cursor-move select-none text-gray-300 hover:text-macsub shrink-0" :size="16" @click.stop><Rank /></el-icon>
                <div class="min-w-0">
                  <div class="font-medium text-sm truncate flex items-center gap-1">
                    <span>{{ s.name }}</span>
                    <el-tag v-if="s.archived" size="small" type="info" effect="plain">已归档</el-tag>
                    <el-tag v-if="!s.enabled && !s.archived" size="small" type="warning" effect="plain">未注入</el-tag>
                  </div>
                  <div v-if="s.note" class="text-xs text-macsub mt-0.5 truncate">{{ s.note }}</div>
                </div>
              </div>
              <div class="flex items-center gap-1" @click.stop>
                <el-switch :model-value="!!s.enabled" size="small" :disabled="!!s.archived" @change="toggleEnabled(s)" />
                <el-button size="small" text :type="s.archived ? 'primary' : 'info'" :icon="s.archived ? 'RefreshLeft' : 'Box'" @click="toggleArchived(s)">{{ s.archived ? '恢复' : '归档' }}</el-button>
                <el-button size="small" text type="danger" :icon="'Delete'" @click="remove(s)" />
              </div>
            </div>
                  <div class="border-t border-macborder pt-2 space-y-1">
                    <div v-for="(kv, j) in s.kv" :key="j" class="grid grid-cols-[120px_minmax(0,1fr)] gap-2 text-xs">
                      <span class="text-macsub truncate" :title="kv.key">{{ kv.key }}</span>
                      <span class="font-mono break-all whitespace-pre-wrap text-mactext">{{ showSecretValues ? kv.value : maskValue(kv.value) }}</span>
                    </div>
                  </div>
                  <div class="text-[10px] text-macsub/75 tabular-nums">{{ assetTimeLine(s) }}</div>
                </div>
              </template>
            </draggable>
          </section>
        </template>
      </draggable>
    </div>

    <el-dialog v-model="dialogOpen" :title="editing?.id ? '编辑凭证' : '新建凭证'" width="860px" top="6vh" :close-on-click-modal="true" :before-close="tryClose">
      <div v-if="editing" class="flex flex-col gap-4">
        <div class="grid grid-cols-[minmax(0,1fr)_220px_120px] gap-3 items-end">
          <div><label class="text-xs text-macsub mb-1 block">名称（引用用，如 github）</label><el-input v-model="editing.name" /></div>
          <div>
            <label class="text-xs text-macsub mb-1 block">分组（可选）</label>
            <el-select v-model="editing.grp" filterable allow-create clearable default-first-option placeholder="不分组" class="w-full">
              <el-option v-for="g in groupNames" :key="g" :label="g" :value="g" />
            </el-select>
          </div>
          <div><label class="text-xs text-macsub mb-1 block">序号</label><el-input-number v-model="editing.sort" :min="0" :step="10" class="!w-full" /></div>
        </div>
        <div><label class="text-xs text-macsub mb-1 block">备注</label><el-input v-model="editing.note" /></div>
        <div class="flex items-center gap-4">
          <el-switch v-model="editing.enabled" :active-value="1" :inactive-value="0" :disabled="!!editing.archived" active-text="注入提示词" inactive-text="不注入" />
          <el-switch v-model="editing.archived" :active-value="1" :inactive-value="0" @change="(v) => { if (v) editing.enabled = 0 }" active-text="归档" inactive-text="未归档" />
        </div>
        <div>
          <div class="flex items-center justify-between mb-1">
            <label class="text-xs text-macsub">字段（key-value）</label>
            <el-button size="small" text :icon="'Plus'" @click="addKv">加一行</el-button>
          </div>
          <div class="space-y-2 max-h-[46vh] overflow-y-auto pr-1">
            <div v-for="(kv, i) in editing.kv" :key="i" class="grid grid-cols-[220px_minmax(0,1fr)_40px] gap-2">
              <el-input v-model="kv.key" placeholder="字段名 / key" />
              <el-input v-if="showSecretValues" v-model="kv.value" placeholder="值（支持多行）" type="textarea" :autosize="{ minRows: 2, maxRows: 10 }" />
              <el-input v-else v-model="kv.value" placeholder="值" type="password" />
              <el-button text type="danger" :icon="'Remove'" @click="delKv(i)" />
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <div class="flex items-center justify-between w-full">
          <div class="flex items-center gap-4">
            <el-switch v-model="showSecretValues" active-text="显示密码内容" inactive-text="隐藏密码内容" />
            <span v-if="editing?.id" class="text-[11px] text-macsub">{{ assetTimeLine(editing) }}</span>
          </div>
          <div>
            <el-button @click="tryClose()">取消</el-button>
            <el-button type="primary" @click="save">保存</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>
