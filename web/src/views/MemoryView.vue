<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import MdEditor from "../components/MdEditor.vue";
import draggable from "vuedraggable";
import { encode } from "gpt-tokenizer";
import { pinyin } from "pinyin-pro";
import { assetTimeLine } from "../utils/assetTime";
import { dragAutoScrollOptions } from "../utils/dragScroll";

const props = defineProps({ activeType: { type: String, default: "" } });
const emit = defineEmits(["type-changed"]);

function toKey(name) {
  if (!name) return "";
  let out = "";
  let i = 0;
  const isZh = (ch) => /[\u4e00-\u9fa5]/.test(ch);
  while (i < name.length) {
    if (isZh(name[i])) {
      let j = i;
      while (j < name.length && isZh(name[j])) j += 1;
      out += pinyin(name.slice(i, j), { toneType: "none", v: true, type: "array" }).join("");
      i = j;
    } else {
      out += name[i];
      i += 1;
    }
  }
  return out.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/-+/g, "-").replace(/\.+/g, ".").replace(/^[-.]+|[-.]+$/g, "");
}

const categories = ref([
  { key: "memory", name: "长期记忆", icon: "🧠" },
  { key: "tools", name: "工具说明", icon: "🛠" },
]);
function normalizeCategory(value) {
  return categories.value.some((item) => item.key === value) ? value : "memory";
}
const activeCat = ref(normalizeCategory(props.activeType));
const entries = ref([]);
const loading = ref(false);
const scrollContainer = ref(null);
const dragging = ref(false);
const editing = ref(null);
const original = ref("");
const dialogOpen = ref(false);
const showArchived = ref(false);
const selectedIds = ref([]);
const refData = ref({ mem: [], secret: [], doc: [] });

async function loadCategories() {
  // 身份、人格和行为准则属于提示词模板，不再作为高频记忆资产管理。
  // network/service/project 等长期事实继续聚合到“长期记忆”入口。
  activeCat.value = normalizeCategory(activeCat.value || props.activeType);
}
async function loadEntries() {
  if (!activeCat.value) return;
  loading.value = true;
  try {
    const category = activeCat.value === "tools" ? "tools" : "";
    const scope = activeCat.value === "memory" ? "memory" : "";
    const data = await Api.entries(category, showArchived.value, scope);
    entries.value = data.items || [];
    rebuildGroups();
  } finally {
    loading.value = false;
  }
}
async function loadRefData() {
  try {
    const [allEntries, secrets, docs] = await Promise.all([
      Api.entries("", true),
      Api.secrets(false, true),
      Api.docs(true),
    ]);
    refData.value = {
      mem: (allEntries.items || []).filter((e) => e.ref).map((e) => ({ key: e.ref, name: e.title, note: e.note || "" })),
      secret: (secrets.items || []).filter((s) => !s.archived && (s.enabled ?? 1)).map((s) => ({ key: s.name, note: s.note || "" })),
      doc: (docs.items || []).filter((d) => !d.archived && (d.enabled ?? 1)).map((d) => ({ key: d.name, title: d.title || "" })),
    };
  } catch {
    // 补全数据失败不阻塞编辑。
  }
}
async function refresh() {
  await Promise.all([loadCategories(), loadRefData()]);
  await loadEntries();
  clearSelection();
  ElMessage.success("已刷新");
}
onMounted(async () => { await loadCategories(); await loadEntries(); await loadRefData(); });
watch(() => props.activeType, (next) => {
  const normalized = normalizeCategory(next);
  if (activeCat.value !== normalized) activeCat.value = normalized;
});
watch(activeCat, (value) => {
  const normalized = normalizeCategory(value);
  if (normalized !== value) {
    activeCat.value = normalized;
    return;
  }
  emit("type-changed", activeCat.value);
  clearSelection();
  loadEntries();
});
watch(showArchived, () => { clearSelection(); loadEntries(); });

const curCat = computed(() => categories.value.find((c) => c.key === activeCat.value));
const groups = ref([]);
function rebuildGroups() {
  const map = new Map();
  const order = [];
  const expanded = [];
  for (const entry of entries.value) {
    if (entry.expanded) {
      expanded.push(entry);
      continue;
    }
    const name = entry.grp || "";
    if (!map.has(name)) { map.set(name, []); order.push(name); }
    map.get(name).push(entry);
  }
  groups.value = [
    { key: "expanded", name: "", expanded: true, items: expanded },
    ...order.map((name) => ({ key: `grp:${name}`, name, expanded: false, items: map.get(name) })),
  ];
}
const groupNames = computed(() => [...new Set(entries.value.map((e) => e.grp).filter(Boolean))]);
const hasGroups = computed(() => groups.value.some((g) => !g.expanded && g.name));
const selectedEntries = computed(() => entries.value.filter((e) => selectedIds.value.includes(e.id)));
const allShownSelected = computed(() => entries.value.length > 0 && entries.value.every((e) => selectedIds.value.includes(e.id)));
const visibleActiveEntries = computed(() => entries.value.filter((e) => !e.archived && e.enabled));
const totalTokens = computed(() => visibleActiveEntries.value.reduce((s, e) => s + tokenCount(e.body), 0));
const expandedEntries = computed(() => visibleActiveEntries.value.filter((e) => e.expanded));
const expandedTokens = computed(() => expandedEntries.value.reduce((sum, entry) => sum + tokenCount(entry.body), 0));
const enabledCount = computed(() => visibleActiveEntries.value.length);
const archivedCount = computed(() => entries.value.filter((e) => e.archived).length);
function boolInt(v) { return v === true || v === 1 || v === "1" ? 1 : 0; }
function editSnapshot(e) {
  if (!e) return "";
  return JSON.stringify({
    id: e.id ?? null,
    category: e.category || activeCat.value || "memory",
    title: e.title || e.name || "",
    ref: e.ref || "",
    note: e.note || "",
    grp: e.grp || "",
    fieldsJson: e.fieldsJson || JSON.stringify(e.fields || {}, null, 2),
    body: e.body || "",
    expanded: boolInt(e.expanded),
    enabled: boolInt(e.enabled),
    archived: boolInt(e.archived),
    sort: Number(e.sort || 0),
  });
}
const dirty = computed(() => editing.value && editSnapshot(editing.value) !== original.value);
const keyPreview = computed(() => {
  if (!editing.value) return "";
  const uk = (editing.value.ref || "").trim();
  return uk ? toKey(uk) : toKey(editing.value.title || "");
});

function isSelected(id) { return selectedIds.value.includes(id); }
function setSelected(id, checked) { selectedIds.value = checked ? [...new Set([...selectedIds.value, id])] : selectedIds.value.filter((x) => x !== id); }
function clearSelection() { selectedIds.value = []; }
function toggleSelectAllShown(v) { selectedIds.value = v ? entries.value.map((e) => e.id) : []; }
function entryPayload(e, patch = {}) {
  return {
    category: e.category || activeCat.value || "memory",
    name: e.title || e.name || "",
    ref: e.ref || "",
    grp: e.grp || "",
    note: e.note || "",
    fieldsJson: e.fieldsJson || JSON.stringify(e.fields || {}, null, 2),
    body: e.body || "",
    expanded: !!e.expanded,
    enabled: e.enabled !== false && e.enabled !== 0,
    archived: !!e.archived,
    sort: Number(e.sort || 0),
    ...patch,
  };
}

const tokenCache = new Map();
function tokenCount(text) {
  if (!text) return 0;
  if (tokenCache.has(text)) return tokenCache.get(text);
  let n;
  try { n = encode(text).length; } catch { n = Math.ceil(text.length / 2); }
  tokenCache.set(text, n);
  return n;
}
function snippet(body) {
  if (!body) return "";
  return body
    .replace(/```[\s\S]*?```/g, " 〔代码块〕 ")
    .replace(/^#+\s*/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`>]/g, "")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 180);
}
async function openEdit(e) {
  let row = e
    ? JSON.parse(JSON.stringify(e))
    : { id: null, category: curCat.value?.key || "memory", title: "", ref: "", note: "", grp: "", fieldsJson: "{}", body: "", expanded: 0, enabled: 1, archived: 0, sort: (entries.value.length + 1) * 10 };
  if (e?.id) {
    try {
      const data = await Api.entry(e.id);
      if (data?.ok === false) throw new Error(data.error || "记忆不存在");
      row = data.item || row;
      const idx = entries.value.findIndex((x) => x.id === row.id);
      if (idx >= 0) entries.value.splice(idx, 1, row);
    } catch (error) {
      ElMessage.error("加载最新记忆失败: " + apiError(error));
      return;
    }
  }
  editing.value = row;
  if (editing.value.category == null) editing.value.category = curCat.value?.key || "memory";
  if (editing.value.ref == null) editing.value.ref = "";
  if (editing.value.note == null) editing.value.note = "";
  if (editing.value.fieldsJson == null) editing.value.fieldsJson = JSON.stringify(editing.value.fields || {}, null, 2);
  editing.value.expanded = boolInt(editing.value.expanded ?? 0);
  editing.value.enabled = boolInt(editing.value.enabled ?? 1);
  editing.value.archived = boolInt(editing.value.archived ?? 0);
  editing.value.sort = Number(editing.value.sort || 0);
  original.value = editSnapshot(editing.value);
  dialogOpen.value = true;
}
async function save() {
  const e = editing.value;
  if (!e.title?.trim()) { ElMessage.warning("名称不能为空"); return; }
  if (!e.ref?.trim()) e.ref = toKey(e.title);
  else e.ref = toKey(e.ref);
  try {
    JSON.parse(e.fieldsJson || "{}");
    const payload = entryPayload(e);
    const r = e.id ? await Api.updateEntry(e.id, payload) : await Api.createEntry(payload);
    if (r?.ok === false) throw new Error(r.error || "保存失败");
    const cells = r?.refCascade?.totalCells || 0;
    ElMessage.success(cells ? `已保存，并自动更新 ${cells} 处 @mem 引用` : "已保存");
    original.value = editSnapshot(editing.value);
    dialogOpen.value = false;
    await loadEntries();
    await loadRefData();
  } catch (err) {
    ElMessage.error("保存失败: " + (err.message || err));
  }
}
async function tryClose(done) {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm("有未保存的修改，确定关闭？", "提示", { type: "warning", confirmButtonText: "放弃修改", cancelButtonText: "继续编辑" });
    } catch { return; }
  }
  if (done) done(); else dialogOpen.value = false;
}
async function removeEntry(e) {
  await ElMessageBox.confirm(`确定删除「${e.title}」？此操作不可恢复。`, "删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  const r = await Api.deleteEntry(e.id);
  if (r?.ok === false) throw new Error(r.error || "删除失败");
  ElMessage.success("已删除");
  await loadEntries();
  await loadRefData();
}
async function toggleEnabled(e) {
  const r = await Api.updateEntry(e.id, entryPayload(e, { enabled: e.enabled ? 0 : 1 }));
  if (r?.ok === false) throw new Error(r.error || "更新失败");
  await loadEntries();
  await loadRefData();
}
async function toggleArchived(e) {
  const next = e.archived ? 0 : 1;
  const r = await Api.updateEntry(e.id, entryPayload(e, { archived: next, enabled: next ? 0 : e.enabled }));
  if (r?.ok === false) throw new Error(r.error || "更新失败");
  ElMessage.success(next ? "已归档，默认不再注入记忆" : "已恢复");
  await loadEntries();
  await loadRefData();
}
async function batchUpdateEntries(patch, message) {
  const rows = [...selectedEntries.value];
  for (const row of rows) {
    const r = await Api.updateEntry(row.id, entryPayload(row, patch));
    if (r?.ok === false) throw new Error(r.error || "批量更新失败");
  }
  ElMessage.success(message || `已处理 ${rows.length} 条`);
  clearSelection();
  await loadEntries();
  await loadRefData();
}
async function batchDeleteEntries() {
  const rows = [...selectedEntries.value];
  await ElMessageBox.confirm(`确定删除选中的 ${rows.length} 条记忆？此操作不可恢复。`, "批量删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  for (const row of rows) {
    const r = await Api.deleteEntry(row.id);
    if (r?.ok === false) throw new Error(r.error || "批量删除失败");
  }
  ElMessage.success(`已删除 ${rows.length} 条记忆`);
  clearSelection();
  await loadEntries();
  await loadRefData();
}
async function persistFromGroups(nextGroups) {
  const pinned = nextGroups.find((group) => group.expanded) || { key: "expanded", name: "", expanded: true, items: [] };
  const orderedGroups = [pinned, ...nextGroups.filter((group) => !group.expanded)];
  const flat = [];
  for (const group of orderedGroups) {
    for (const item of group.items) {
      flat.push({
        ...item,
        grp: group.expanded ? (item.grp || "") : group.name,
        expanded: group.expanded ? 1 : 0,
      });
    }
  }
  flat.forEach((entry, index) => (entry.sort = (index + 1) * 10));
  groups.value = orderedGroups;
  entries.value = flat;
  try {
    const r = await Api.reorder("entries", flat.map((entry) => ({ id: entry.id, sort: entry.sort, grp: entry.grp, expanded: entry.expanded })));
    if (r?.ok === false) throw new Error(r.error || "排序保存失败");
    ElMessage.success("顺序已保存");
  } catch (err) { ElMessage.error("排序保存失败: " + (err.message || err)); }
}
function finishDrag() {
  dragging.value = false;
  persistFromGroups(groups.value);
}
</script>

<template>
  <div class="h-full flex flex-col">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="flex items-center gap-2">
        <h1 class="text-base font-semibold">记忆管理</h1>
        <span class="text-xs text-macsub">正文为本体 · 启用 {{ enabledCount }} 条 · 展开 {{ expandedEntries.length }} 条 / ~{{ expandedTokens.toLocaleString() }} tk · 总计 ~{{ totalTokens.toLocaleString() }} tk</span>
      </div>
      <div class="flex items-center gap-3">
        <el-checkbox v-model="showArchived" size="small">显示归档 <span v-if="archivedCount">({{ archivedCount }})</span></el-checkbox>
        <el-button :icon="'Refresh'" circle @click="refresh" title="刷新" />
        <el-button type="primary" :icon="'Plus'" @click="openEdit(null)" round>新建条目</el-button>
      </div>
    </header>

    <div class="px-6 pt-4 pb-3 flex gap-2 flex-wrap shrink-0">
      <button v-for="c in categories" :key="c.key" @click="activeCat = c.key"
        class="px-3.5 py-1.5 rounded-full text-sm transition-all border flex items-center gap-1"
        :class="activeCat === c.key ? 'bg-mactext text-white border-mactext' : 'bg-white text-mactext border-macborder hover:border-gray-400'">
        <span>{{ c.icon || '🧠' }}</span>{{ c.name }}
      </button>
    </div>

    <div v-if="selectedIds.length" class="mx-6 mb-3 px-3 py-2 rounded-2xl border border-macblue/20 bg-macblue/5 flex items-center gap-2 shrink-0">
      <el-checkbox :model-value="allShownSelected" @change="toggleSelectAllShown">全选当前列表</el-checkbox>
      <span class="text-xs text-macsub mr-2">已选 {{ selectedIds.length }} 条</span>
      <el-button size="small" :icon="'Box'" @click="batchUpdateEntries({ archived: 1, enabled: 0 }, '已批量归档')">批量归档</el-button>
      <el-button size="small" :icon="'RefreshLeft'" @click="batchUpdateEntries({ archived: 0 }, '已批量恢复')">恢复</el-button>
      <el-button size="small" :icon="'Unlock'" @click="batchUpdateEntries({ enabled: 1, archived: 0 }, '已批量启用注入')">启用注入</el-button>
      <el-button size="small" :icon="'Lock'" @click="batchUpdateEntries({ enabled: 0 }, '已批量禁用注入')">禁用注入</el-button>
      <el-button size="small" type="danger" :icon="'Delete'" @click="batchDeleteEntries">删除</el-button>
      <el-button size="small" text @click="clearSelection">取消选择</el-button>
    </div>

    <div ref="scrollContainer" class="flex-1 min-h-0 overflow-y-auto px-6 pb-6" :class="{ 'select-none': dragging }" v-loading="loading">
      <div v-if="!entries.length" class="text-center text-macsub py-16 text-sm">
        {{ showArchived ? '该分类暂无条目' : '该分类暂无未归档条目' }}
      </div>

      <draggable v-model="groups" item-key="key" handle=".group-handle" :animation="180"
        v-bind="dragAutoScrollOptions" :scroll="scrollContainer"
        @choose="dragging = true" @unchoose="dragging = false" @end="finishDrag" :disabled="!hasGroups">
        <template #item="{ element: g }">
          <div class="mb-4" :class="g.expanded ? 'rounded-2xl border border-amber-300/70 bg-amber-50/45 p-3' : ''">
            <div class="text-xs font-semibold text-macsub mb-1.5 flex items-center gap-2 group/grp">
              <el-icon v-if="!g.expanded" class="group-handle cursor-move select-none text-gray-300 hover:text-macsub" :size="14"><Rank /></el-icon>
              <span v-else class="w-[14px] text-center select-none">📌</span>
              <span class="w-1 h-3.5 rounded" :class="g.expanded ? 'bg-amber-500' : 'bg-macblue'"></span>{{ g.expanded ? '提示词展开' : (g.name || '未分组') }}
              <span class="opacity-50">({{ g.items.length }}<template v-if="g.expanded"> 条 · 每轮约 {{ expandedTokens.toLocaleString() }} tk</template>)</span>
              <span v-if="g.expanded" class="font-normal opacity-60">拖入开启，拖出关闭</span>
            </div>
            <draggable v-model="g.items" item-key="id" handle=".drag-handle" :animation="180"
              v-bind="dragAutoScrollOptions" :scroll="scrollContainer"
              :group="{ name: 'memory-entry-groups' }" @choose="dragging = true" @unchoose="dragging = false" @end="finishDrag"
              class="space-y-2 min-h-8" :class="g.expanded && !g.items.length ? 'rounded-xl border border-dashed border-amber-300/70' : ''">
              <template #item="{ element: e }">
                <div @click="openEdit(e)"
                  class="mac-panel px-3.5 py-3 grid grid-cols-[28px_20px_40px_minmax(0,1fr)_auto] items-stretch gap-3 cursor-pointer hover:border-macblue/50 transition-colors"
                  :class="{ 'opacity-45': !e.enabled || e.archived, 'border-dashed': e.archived, 'ring-1 ring-macblue/30 bg-macblue/5': isSelected(e.id) }">
                  <div class="self-stretch flex items-center justify-center" @click.stop>
                    <el-checkbox :model-value="isSelected(e.id)" @change="(v) => setSelected(e.id, v)" />
                  </div>
                  <div class="drag-handle cursor-move select-none self-stretch flex items-center justify-center text-gray-300 hover:text-macsub" @click.stop>
                    <el-icon :size="16"><Rank /></el-icon>
                  </div>
                  <span class="text-[11px] text-macsub text-right tabular-nums self-stretch flex items-center justify-end">{{ e.sort }}</span>
                  <div class="min-w-0 self-start">
                    <div class="flex items-center gap-2 min-w-0">
                      <span class="text-sm font-semibold truncate">{{ e.title }}</span>
                      <el-tag v-if="g.expanded" size="small" type="warning" effect="plain">已展开</el-tag>
                      <el-tag v-if="e.archived" size="small" type="info" effect="plain">已归档</el-tag>
                      <span v-if="e.note" class="text-[11px] text-macsub truncate max-w-[32%]" :title="e.note">{{ e.note }}</span>
                      <code v-if="e.ref" class="text-[10px] text-macblue/70 bg-macblue/5 px-1.5 py-0.5 rounded shrink-0 truncate max-w-[42%]" :title="'@mem/' + e.ref">@mem/{{ e.ref }}</code>
                    </div>
                    <div v-if="snippet(e.body)" class="pm-clamp2 text-xs text-macsub mt-1.5 leading-relaxed">{{ snippet(e.body) }}</div>
                    <div class="text-[10px] text-macsub/75 mt-1.5 tabular-nums">{{ assetTimeLine(e) }}</div>
                  </div>
                  <div class="flex items-center gap-2 shrink-0 self-center" @click.stop>
                    <span class="text-[10px] text-macsub tabular-nums shrink-0">{{ tokenCount(e.body) }} tk</span>
                    <el-switch :model-value="!!e.enabled" @change="toggleEnabled(e)" size="small" :disabled="!!e.archived" />
                    <el-button size="small" text :type="e.archived ? 'primary' : 'info'" :icon="e.archived ? 'RefreshLeft' : 'Box'" @click="toggleArchived(e)">{{ e.archived ? '恢复' : '归档' }}</el-button>
                    <el-button size="small" text type="danger" :icon="'Delete'" @click="removeEntry(e)" />
                  </div>
                </div>
              </template>
            </draggable>
          </div>
        </template>
      </draggable>
    </div>

    <el-dialog v-model="dialogOpen" :title="editing?.id ? '编辑条目' : '新建条目'" width="900px" top="4vh"
      :close-on-click-modal="true" :before-close="tryClose" class="mac-edit-dialog">
      <div v-if="editing" class="flex flex-col gap-3" style="height: 70vh;">
        <div class="flex items-end gap-3">
          <div class="flex-1">
            <label class="text-xs text-macsub mb-1 block">名称</label>
            <el-input v-model="editing.title" placeholder="条目名称" />
          </div>
          <div class="w-44">
            <label class="text-xs text-macsub mb-1 block">分组（可选）</label>
            <el-select v-model="editing.grp" filterable allow-create clearable default-first-option placeholder="不分组" class="w-full">
              <el-option v-for="g in groupNames" :key="g" :label="g" :value="g" />
            </el-select>
          </div>
          <div>
            <label class="text-xs text-macsub mb-1 block">序号</label>
            <el-input-number v-model="editing.sort" :min="0" :step="10" controls-position="right" class="!w-24" />
          </div>
          <div class="flex items-center gap-1.5 pb-2">
            <el-switch v-model="editing.expanded" :active-value="1" :inactive-value="0" :disabled="!!editing.archived || !editing.enabled" />
            <span class="text-xs whitespace-nowrap" title="开启后，正文可通过 memory.expandedEntries 每轮展开到提示词">展开</span>
          </div>
          <div class="flex items-center gap-1.5 pb-2">
            <el-switch v-model="editing.enabled" :active-value="1" :inactive-value="0" :disabled="!!editing.archived" @change="(v) => { if (!v) editing.expanded = 0 }" />
            <span class="text-xs whitespace-nowrap">{{ editing.archived ? '归档不注入' : (editing.enabled ? '注入' : '停用') }}</span>
          </div>
          <div class="flex items-center gap-1.5 pb-2">
            <el-switch v-model="editing.archived" :active-value="1" :inactive-value="0" @change="(v) => { if (v) { editing.enabled = 0; editing.expanded = 0 } }" />
            <span class="text-xs whitespace-nowrap">归档</span>
          </div>
        </div>
        <div class="flex items-end gap-3">
          <div class="w-80">
            <label class="text-xs text-macsub mb-1 block">引用 key（留空自动按名称生成）</label>
            <el-input v-model="editing.ref" placeholder="自动生成">
              <template #prepend>@mem/</template>
            </el-input>
          </div>
          <div class="flex-1">
            <label class="text-xs text-macsub mb-1 block">备注（可选）</label>
            <el-input v-model="editing.note" placeholder="简短说明，便于检索" />
          </div>
        </div>
        <div class="-mt-1 text-[11px] text-macsub">
          实际引用：<code class="text-macblue">@mem/{{ keyPreview }}</code>
          <span class="opacity-60"> · 改名称不影响已生成的 key，引用始终稳定</span>
        </div>
        <div class="flex-1 min-h-0 flex flex-col">
          <label class="text-xs text-macsub mb-1 block">正文（Markdown · 记忆本体）</label>
          <div class="flex-1 min-h-0"><MdEditor v-model="editing.body" :ref-data="refData" /></div>
        </div>
      </div>
      <template #footer>
        <span v-if="editing?.id" class="text-[11px] text-macsub mr-auto">{{ assetTimeLine(editing) }}</span>
        <span v-if="dirty" class="text-xs text-orange-500 mr-3">● 有未保存修改</span>
        <el-button @click="tryClose()">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style>
.mac-edit-dialog .el-dialog__body { padding-top: 8px; }
.pm-clamp2 { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
</style>
