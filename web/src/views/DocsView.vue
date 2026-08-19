<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import MdEditor from "../components/MdEditor.vue";
import draggable from "vuedraggable";
import { assetTimeLine } from "../utils/assetTime";
import { dragAutoScrollOptions } from "../utils/dragScroll";

const docs = ref([]);
const loading = ref(false);
const editing = ref(null);
const original = ref("");
const dialogOpen = ref(false);
const showArchived = ref(false);
const selectedIds = ref([]);
const groups = ref([]);
const scrollContainer = ref(null);
const dragging = ref(false);

function splitList(s) {
  if (Array.isArray(s)) return s.filter(Boolean);
  return String(s || "").split(/[,，\n]/).map((x) => x.trim()).filter(Boolean);
}
function joinList(arr) { return (arr || []).map((x) => String(x).trim()).filter(Boolean).join(", "); }
async function load() {
  loading.value = true;
  try {
    const data = await Api.docs(showArchived.value);
    docs.value = data.items || [];
    rebuildGroups();
  } finally { loading.value = false; }
}
async function refresh() { await load(); clearSelection(); ElMessage.success("已刷新"); }
onMounted(load);
const shownDocs = computed(() => docs.value.filter((d) => showArchived.value || !d.archived));
const selectedDocs = computed(() => shownDocs.value.filter((d) => selectedIds.value.includes(d.id)));
const allShownSelected = computed(() => shownDocs.value.length > 0 && shownDocs.value.every((d) => selectedIds.value.includes(d.id)));
const projectOptions = computed(() => [...new Set(docs.value.flatMap((d) => splitList(d.project)))].sort());
const groupNames = computed(() => [...new Set(docs.value.map((d) => d.grp).filter(Boolean))]);
function rebuildGroups() {
  const map = new Map();
  const order = [];
  for (const doc of shownDocs.value) {
    const name = doc.grp || "";
    if (!map.has(name)) { map.set(name, []); order.push(name); }
    map.get(name).push(doc);
  }
  groups.value = order.map((name) => ({ name, items: map.get(name) }));
}
function boolInt(v) { return v === true || v === 1 || v === "1" ? 1 : 0; }
function editSnapshot(d) {
  if (!d) return "";
  return JSON.stringify({
    id: d.id ?? null,
    name: d.name || "",
    title: d.title || "",
    summary: d.summary || "",
    projectList: Array.isArray(d.projectList) ? d.projectList : splitList(d.project),
    tagList: Array.isArray(d.tagList) ? d.tagList : splitList(d.tags),
    importance: Number(d.importance || 3),
    grp: d.grp || "",
    sort: Number(d.sort || 0),
    enabled: boolInt(d.enabled),
    archived: boolInt(d.archived),
    content: d.content || "",
  });
}
const dirty = () => editing.value && editSnapshot(editing.value) !== original.value;

function isSelected(id) { return selectedIds.value.includes(id); }
function setSelected(id, checked) { selectedIds.value = checked ? [...new Set([...selectedIds.value, id])] : selectedIds.value.filter((x) => x !== id); }
function clearSelection() { selectedIds.value = []; }
function toggleSelectAllShown(v) { selectedIds.value = v ? shownDocs.value.map((d) => d.id) : []; }
async function openEdit(d) {
  let row;
  if (d) {
    try {
      const got = await Api.doc(d.id);
      if (got?.ok === false) throw new Error(got.error || "文档不存在");
      row = got.item || d;
      const idx = docs.value.findIndex((x) => x.id === row.id);
      if (idx >= 0) docs.value.splice(idx, 1, row);
    } catch (error) {
      ElMessage.error("加载最新文档失败: " + apiError(error));
      return;
    }
  } else {
    row = { id: null, name: "", title: "", summary: "", project: "", importance: 3, tags: "", grp: "", sort: (docs.value.length + 1) * 10, enabled: 1, archived: 0, content: "" };
  }
  editing.value = { ...row, grp: row.grp || "", sort: Number(row.sort || 0), enabled: boolInt(row.enabled ?? 1), archived: boolInt(row.archived || 0), importance: Number(row.importance || 3), projectList: splitList(row.project), tagList: splitList(row.tags) };
  original.value = editSnapshot(editing.value);
  dialogOpen.value = true;
}
function normalizeDocForSave() {
  const d = { ...editing.value };
  d.project = joinList(d.projectList);
  d.tags = joinList(d.tagList);
  delete d.projectList;
  delete d.tagList;
  return d;
}
function docPayload(d, patch = {}) { return { ...d, grp: d.grp || "", sort: Number(d.sort || 0), enabled: d.enabled ?? 1, archived: d.archived || 0, ...patch }; }
async function save() {
  const d = normalizeDocForSave();
  if (!d.name?.trim()) { ElMessage.warning("名称不能为空"); return; }
  try {
    const r = d.id ? await Api.updateDoc(d.id, d) : await Api.createDoc(d);
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
async function remove(d) {
  await ElMessageBox.confirm(`确定删除文档「${d.name}」？不可恢复。`, "删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  const r = await Api.deleteDoc(d.id);
  if (r?.ok === false) throw new Error(r.error || "删除失败");
  ElMessage.success("已删除");
  await load();
}
async function toggleArchive(d) {
  const next = d.archived ? 0 : 1;
  const act = next ? "归档" : "取消归档";
  await ElMessageBox.confirm(`确定${act}文档「${d.name}」？`, `${act}确认`, { type: "warning" });
  const got = await Api.doc(d.id);
  if (got?.ok === false) throw new Error(got.error || "文档不存在");
  const r = await Api.updateDoc(d.id, docPayload(got.item, { archived: next, enabled: next ? 0 : (got.item.enabled ?? 1) }));
  if (r?.ok === false) throw new Error(r.error || "更新失败");
  ElMessage.success(`已${act}`);
  await load();
}
async function toggleEnabled(d) {
  const got = await Api.doc(d.id);
  if (got?.ok === false) throw new Error(got.error || "文档不存在");
  const r = await Api.updateDoc(d.id, docPayload(got.item, { enabled: d.enabled ? 0 : 1 }));
  if (r?.ok === false) throw new Error(r.error || "更新失败");
  await load();
}
async function batchUpdateDocs(patch, message) {
  const rows = [...selectedDocs.value];
  for (const row of rows) {
    const got = await Api.doc(row.id);
    if (got?.ok === false) throw new Error(got.error || "文档不存在");
    const r = await Api.updateDoc(row.id, docPayload(got.item, patch));
    if (r?.ok === false) throw new Error(r.error || "批量更新失败");
  }
  ElMessage.success(message || `已处理 ${rows.length} 篇文档`);
  clearSelection();
  await load();
}
async function batchDeleteDocs() {
  const rows = [...selectedDocs.value];
  await ElMessageBox.confirm(`确定删除选中的 ${rows.length} 篇文档？此操作不可恢复。`, "批量删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  for (const row of rows) {
    const r = await Api.deleteDoc(row.id);
    if (r?.ok === false) throw new Error(r.error || "批量删除失败");
  }
  ElMessage.success(`已删除 ${rows.length} 篇文档`);
  clearSelection();
  await load();
}
async function finishDrag() {
  dragging.value = false;
  await persistGroups();
}
async function persistGroups() {
  const flat = groups.value.flatMap((group) => group.items.map((doc) => ({ ...doc, grp: group.name })));
  flat.forEach((doc, idx) => (doc.sort = (idx + 1) * 10));
  docs.value = flat;
  try {
    const result = await Api.reorder("docs", flat.map((doc) => ({ id: doc.id, grp: doc.grp, sort: doc.sort })));
    if (result?.ok === false) throw new Error(result.error || "排序保存失败");
    ElMessage.success("顺序已保存");
  } catch (error) {
    ElMessage.error("排序保存失败: " + (error.message || error));
    await load();
  }
}
const impColor = (n) => ["", "info", "info", "", "warning", "danger"][n] || "";
</script>

<template>
  <div class="h-full flex flex-col">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="flex items-center gap-2">
        <h1 class="text-base font-semibold">文档库</h1>
        <span class="text-xs text-macsub">点卡片编辑 · Memory(resource=doc) 按需取全文</span>
      </div>
      <div class="flex items-center gap-3">
        <el-checkbox v-model="showArchived" size="small" @change="load(); clearSelection()">显示归档</el-checkbox>
        <el-button :icon="'Refresh'" circle @click="refresh" title="刷新" />
        <el-button type="primary" :icon="'Plus'" @click="openEdit(null)" round>新建文档</el-button>
      </div>
    </header>

    <div v-if="selectedIds.length" class="mx-6 mt-3 px-3 py-2 rounded-2xl border border-macblue/20 bg-macblue/5 flex items-center gap-2 shrink-0">
      <el-checkbox :model-value="allShownSelected" @change="toggleSelectAllShown">全选当前列表</el-checkbox>
      <span class="text-xs text-macsub mr-2">已选 {{ selectedIds.length }} 篇</span>
      <el-button size="small" :icon="'Box'" @click="batchUpdateDocs({ archived: 1, enabled: 0 }, '已批量归档')">批量归档</el-button>
      <el-button size="small" :icon="'RefreshLeft'" @click="batchUpdateDocs({ archived: 0 }, '已批量恢复')">恢复</el-button>
      <el-button size="small" :icon="'Unlock'" @click="batchUpdateDocs({ enabled: 1, archived: 0 }, '已批量启用注入')">启用注入</el-button>
      <el-button size="small" :icon="'Lock'" @click="batchUpdateDocs({ enabled: 0 }, '已批量禁用注入')">禁用注入</el-button>
      <el-button size="small" type="danger" :icon="'Delete'" @click="batchDeleteDocs">删除</el-button>
      <el-button size="small" text @click="clearSelection">取消选择</el-button>
    </div>

    <div ref="scrollContainer" class="flex-1 min-h-0 overflow-y-auto p-6" :class="{ 'select-none': dragging }" v-loading="loading">
      <div v-if="!shownDocs.length" class="text-center text-macsub py-16 text-sm">暂无文档</div>
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
              :group="{ name: 'doc-groups' }" @choose="dragging = true" @unchoose="dragging = false" @end="finishDrag" class="space-y-2 min-h-6">
              <template #item="{ element: d }">
                <div @click="openEdit(d)"
                  class="mac-panel mac-shadow p-4 flex items-start gap-3 cursor-pointer hover:border-macblue/50 transition-colors"
          :class="{ 'opacity-50 border-dashed': d.archived || !d.enabled, 'ring-1 ring-macblue/30 bg-macblue/5': isSelected(d.id) }">
                  <div class="pt-0.5" @click.stop><el-checkbox :model-value="isSelected(d.id)" @change="(v) => setSelected(d.id, v)" /></div>
                  <el-icon class="drag-handle cursor-move select-none text-gray-300 hover:text-macsub mt-0.5" :size="18" @click.stop><Rank /></el-icon>
                  <el-icon :size="18" class="text-macsub mt-0.5"><Document /></el-icon>
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 min-w-0">
              <span class="font-medium text-sm truncate">{{ d.title || d.name }}</span>
              <el-tag size="small" :type="impColor(d.importance)" effect="light">P{{ d.importance }}</el-tag>
              <span v-for="p in splitList(d.project)" :key="p" class="text-[11px] text-macblue shrink-0">#{{ p }}</span>
              <span v-if="d.archived" class="text-[11px] text-macsub shrink-0">已归档</span>
              <el-tag v-if="!d.enabled && !d.archived" size="small" type="warning" effect="plain">未注入</el-tag>
            </div>
            <div class="text-xs text-macsub mt-0.5 line-clamp-1">{{ d.summary || '（无备注）' }}</div>
            <div class="text-[11px] text-macsub mt-1 flex flex-wrap gap-1 items-center">
              <span>名称: <code>{{ d.name }}</code></span>
              <el-tag v-for="t in splitList(d.tags)" :key="t" size="small" effect="plain">{{ t }}</el-tag>
            </div>
            <div class="text-[10px] text-macsub/75 mt-1 tabular-nums">{{ assetTimeLine(d) }}</div>
          </div>
          <div class="flex gap-2 shrink-0" @click.stop>
            <el-switch :model-value="!!d.enabled" size="small" :disabled="!!d.archived" @change="toggleEnabled(d)" />
            <el-button size="small" plain :icon="'Edit'" @click="openEdit(d)">编辑</el-button>
            <el-button size="small" plain :icon="d.archived ? 'RefreshLeft' : 'Box'" @click="toggleArchive(d)">{{ d.archived ? '取消归档' : '归档' }}</el-button>
                    <el-button size="small" plain type="danger" :icon="'Delete'" @click="remove(d)">删除</el-button>
                  </div>
                </div>
              </template>
            </draggable>
          </section>
        </template>
      </draggable>
    </div>

    <el-dialog v-model="dialogOpen" :title="editing?.id ? '编辑文档' : '新建文档'" width="980px" top="4vh" :close-on-click-modal="true" :before-close="tryClose">
      <div v-if="editing" class="flex flex-col gap-3" style="height: 72vh;">
        <div class="grid grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)_200px_110px] gap-3 items-end">
          <div><label class="text-xs text-macsub mb-1 block">名称(引用用)</label><el-input v-model="editing.name" placeholder="如 poker-deploy" /></div>
          <div><label class="text-xs text-macsub mb-1 block">标题</label><el-input v-model="editing.title" /></div>
          <div>
            <label class="text-xs text-macsub mb-1 block">分组（可选）</label>
            <el-select v-model="editing.grp" filterable allow-create clearable default-first-option placeholder="不分组" class="w-full">
              <el-option v-for="g in groupNames" :key="g" :label="g" :value="g" />
            </el-select>
          </div>
          <div><label class="text-xs text-macsub mb-1 block">序号</label><el-input-number v-model="editing.sort" :min="0" :step="10" class="!w-full" /></div>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-xs text-macsub mb-1 block">关联项目（可多选）</label>
            <el-select v-model="editing.projectList" multiple filterable allow-create default-first-option class="w-full" placeholder="选择或输入项目名">
              <el-option v-for="p in projectOptions" :key="p" :label="p" :value="p" />
            </el-select>
          </div>
          <div>
            <label class="text-xs text-macsub mb-1 block">标签</label>
            <el-input-tag v-model="editing.tagList" placeholder="输入标签后回车" />
          </div>
        </div>
        <div><label class="text-xs text-macsub mb-1 block">备注</label><el-input v-model="editing.summary" placeholder="简短说明这篇文档何时该取用" /></div>
        <div class="flex items-center gap-4">
          <el-switch v-model="editing.enabled" :active-value="1" :inactive-value="0" :disabled="!!editing.archived" active-text="注入提示词" inactive-text="不注入" />
          <el-switch v-model="editing.archived" :active-value="1" :inactive-value="0" @change="(v) => { if (v) editing.enabled = 0 }" active-text="归档" inactive-text="未归档" />
        </div>
        <div class="flex-1 min-h-0 flex flex-col">
          <label class="text-xs text-macsub mb-1 block">正文（Markdown）</label>
          <div class="flex-1 min-h-0"><MdEditor v-model="editing.content" /></div>
        </div>
      </div>
      <template #footer>
        <div class="flex items-center justify-between w-full">
          <div class="flex items-center gap-4">
            <div class="flex items-center gap-2">
              <span class="text-xs text-macsub">重要度</span>
              <el-rate v-model="editing.importance" :max="5" size="small" />
              <span class="text-xs text-macsub">P{{ editing.importance }}</span>
            </div>
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
