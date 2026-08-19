<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";

const loading = ref(false);
const reloading = ref(false);
const items = ref([]);
const stats = ref({ total: 0, enabled: 0, disabled: 0, dependencyMissing: 0, directory: "" });
const skillsDir = ref("");
const query = ref("");
const statusFilter = ref("all");
const showDisabled = ref(true);
const detailOpen = ref(false);
const detailLoading = ref(false);
const detail = ref(null);
const installOpen = ref(false);
const uninstallingNames = ref(new Set());

const statusOptions = [
  { value: "all", label: "全部状态" },
  { value: "enabled", label: "已注入" },
  { value: "disabled", label: "已停用" },
  { value: "dependency_missing", label: "依赖缺失" },
  { value: "unavailable", label: "不可用" },
];

const filteredItems = computed(() => {
  const kw = query.value.trim().toLowerCase();
  return items.value.filter((row) => {
    const status = String(row.status || "");
    if (!showDisabled.value && status === "disabled") return false;
    if (statusFilter.value !== "all" && status !== statusFilter.value) return false;
    if (!kw) return true;
    const haystack = [row.name, row.description, row.location, row.baseDir, row.reason, row.skillKey, row.homepage]
      .map((x) => String(x || "").toLowerCase())
      .join("\n");
    return haystack.includes(kw);
  });
});

function okOrThrow(data) {
  if (data?.ok === false) throw new Error(data.error || "操作失败");
  return data;
}
function reqList(row, key) {
  const value = row?.requires?.[key];
  return Array.isArray(value) ? value.filter(Boolean) : [];
}
function isUninstalling(row) {
  return uninstallingNames.value.has(String(row?.name || ""));
}
function setUninstalling(name, value) {
  const next = new Set(uninstallingNames.value);
  if (value) next.add(name);
  else next.delete(name);
  uninstallingNames.value = next;
}
function configuredEnabled(row) {
  if (row?.configuredEnabled !== undefined) return Boolean(row.configuredEnabled);
  if (row?.userEnabled !== undefined) return Boolean(row.userEnabled);
  return row?.status !== "disabled";
}
function statusType(row) {
  const status = String(row?.status || "");
  if (status === "enabled") return "success";
  if (status === "dependency_missing") return "warning";
  if (status === "disabled") return "info";
  return "danger";
}
function statusText(row) {
  return row?.statusLabel || ({ enabled: "已注入", disabled: "已停用", dependency_missing: "依赖缺失", unavailable: "不可用" }[row?.status] || row?.status || "未知");
}
function primaryEnvText(row) {
  const env = row?.primaryEnv || "";
  return env || (reqList(row, "env")[0] || "");
}
function shortPath(value) {
  const text = String(value || "");
  if (!text) return "—";
  return text.replace(/^\/home\/([^/]+)/, "~");
}
function displayEmoji(row) {
  return row?.emoji || "🧩";
}

async function load(options = {}) {
  const silent = Boolean(options.silent);
  if (!silent) loading.value = true;
  try {
    const data = okOrThrow(await Api.skills());
    items.value = Array.isArray(data.items) ? data.items : [];
    stats.value = data.stats || {};
    skillsDir.value = data.skillsDir || data.stats?.directory || "";
  } catch (error) {
    if (!silent) ElMessage.error(apiError(error));
  } finally {
    if (!silent) loading.value = false;
  }
}

async function reloadSkills() {
  reloading.value = true;
  try {
    const data = okOrThrow(await Api.skillsReload());
    items.value = Array.isArray(data.items) ? data.items : [];
    stats.value = data.stats || {};
    skillsDir.value = data.skillsDir || data.stats?.directory || "";
    ElMessage.success("Skills 已重新加载；后续对话会使用新的注入结果");
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    reloading.value = false;
  }
}

async function openDetail(row) {
  detailOpen.value = true;
  detail.value = row;
  detailLoading.value = true;
  try {
    const data = okOrThrow(await Api.skillDetail(row.name));
    detail.value = data.item || row;
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    detailLoading.value = false;
  }
}

async function uninstallSkill(row) {
  const name = String(row?.name || "").trim();
  if (!name) return;
  try {
    await ElMessageBox.prompt(
      `Skill 会从运行目录移到隐藏归档，不会删除它额外安装的依赖。当前有运行中任务时后端会拒绝操作。请输入完整名称「${name}」确认。`,
      `卸载 Skill · ${name}`,
      {
        type: "error",
        confirmButtonText: "确认卸载",
        cancelButtonText: "取消",
        inputPlaceholder: name,
        inputValidator: (value) => value === name || "名称不一致",
        closeOnClickModal: false,
      },
    );
  } catch {
    return;
  }
  setUninstalling(name, true);
  try {
    const data = okOrThrow(await Api.uninstallSkill(name));
    items.value = Array.isArray(data.items) ? data.items : items.value.filter((item) => item.name !== name);
    stats.value = data.stats || stats.value;
    skillsDir.value = data.skillsDir || skillsDir.value;
    if (detail.value?.name === name) {
      detailOpen.value = false;
      detail.value = null;
    }
    ElMessage.success(`Skill「${name}」已卸载并归档为 ${data.archiveName || "隐藏备份"}`);
  } catch (error) {
    ElMessage.error(apiError(error));
    await load({ silent: true });
  } finally {
    setUninstalling(name, false);
  }
}

async function toggleSkill(row) {
  const next = !configuredEnabled(row);
  const verb = next ? "启用" : "停用";
  if (!next) {
    try {
      await ElMessageBox.confirm(
        `确认停用 Skill「${row.name}」？它会从下一轮对话的 skills 注入中移除。`,
        `停用 ${row.name}`,
        { type: "warning", confirmButtonText: "停用", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
  }
  try {
    okOrThrow(await Api.skillToggle(row.name, next));
    ElMessage.success(`${verb}配置已保存；下一轮对话生效`);
    await load({ silent: true });
    if (detailOpen.value && detail.value?.name === row.name) {
      const updated = items.value.find((item) => item.name === row.name);
      if (updated) await openDetail(updated);
    }
  } catch (error) {
    ElMessage.error(apiError(error));
  }
}

onMounted(load);
</script>

<template>
  <div class="h-full flex flex-col" v-loading="loading">
    <header class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
      <div class="min-w-0 flex items-center gap-2">
        <h1 class="text-base font-semibold">Skills</h1>
        <span class="truncate text-xs text-macsub">任务技能管理 · 启用后下一轮对话生效</span>
      </div>
      <div class="flex items-center gap-2">
        <el-button plain round :icon="'InfoFilled'" @click="installOpen = true">安装说明</el-button>
        <el-button round :icon="'Refresh'" :loading="reloading" @click="reloadSkills">重新加载</el-button>
      </div>
    </header>

    <div class="grid grid-cols-1 gap-3 px-6 pt-5 shrink-0 md:grid-cols-4">
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">Skill 总数</div>
        <div class="text-lg font-semibold">{{ stats.total || 0 }}</div>
      </div>
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">已注入 / 启用</div>
        <div class="text-lg font-semibold text-emerald-700">{{ stats.enabled || 0 }}</div>
      </div>
      <div class="mac-panel px-4 py-3">
        <div class="text-[11px] text-macsub">依赖缺失</div>
        <div class="text-lg font-semibold" :class="stats.dependencyMissing ? 'text-amber-700' : ''">{{ stats.dependencyMissing || 0 }}</div>
      </div>
      <div class="mac-panel min-w-0 px-4 py-3">
        <div class="text-[11px] text-macsub">Skills 目录</div>
        <div class="truncate font-mono text-sm font-semibold" :title="skillsDir || stats.directory">{{ shortPath(skillsDir || stats.directory) }}</div>
      </div>
    </div>

    <section class="mx-6 mt-4 shrink-0 rounded-2xl border border-macborder bg-white/70 p-3 backdrop-blur">
      <div class="flex flex-col gap-3 lg:flex-row lg:items-center">
        <el-input v-model="query" clearable :prefix-icon="'Search'" placeholder="搜索 name / description / path / reason" class="lg:max-w-md" />
        <el-select v-model="statusFilter" class="w-full lg:w-44">
          <el-option v-for="option in statusOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-checkbox v-model="showDisabled">显示停用</el-checkbox>
        <div class="ml-auto text-xs text-macsub">当前显示 {{ filteredItems.length }} / {{ items.length }}</div>
      </div>
    </section>

    <main class="min-h-0 flex-1 overflow-y-auto p-6">
      <el-empty v-if="!filteredItems.length" description="暂无匹配的 Skills" />
      <div v-else class="grid grid-cols-1 gap-3 xl:grid-cols-2">
        <article v-for="row in filteredItems" :key="row.name" class="mac-panel mac-shadow p-4">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex min-w-0 items-center gap-2">
                <el-tag :type="statusType(row)" round>{{ statusText(row) }}</el-tag>
                <span class="text-lg leading-none">{{ displayEmoji(row) }}</span>
                <h2 class="truncate text-[15px] font-semibold" :title="row.name">{{ row.name }}</h2>
              </div>
              <p class="mt-2 line-clamp-2 text-sm leading-6 text-mactext/80">{{ row.description || '暂无描述' }}</p>

              <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-macsub">
                <span v-if="reqList(row, 'bins').length" class="rounded-full bg-black/[0.04] px-2 py-0.5">bin: {{ reqList(row, 'bins').join(', ') }}</span>
                <span v-if="reqList(row, 'env').length" class="rounded-full bg-black/[0.04] px-2 py-0.5">env: {{ reqList(row, 'env').join(', ') }}</span>
                <span v-if="primaryEnvText(row)" class="rounded-full bg-black/[0.04] px-2 py-0.5">primaryEnv: {{ primaryEnvText(row) }}</span>
                <span v-if="row.always" class="rounded-full bg-emerald-50 px-2 py-0.5 text-emerald-700">always</span>
                <span class="max-w-full truncate rounded-full bg-black/[0.04] px-2 py-0.5 font-mono" :title="row.location">{{ shortPath(row.location) }}</span>
              </div>

              <div v-if="row.reason" class="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">
                {{ row.reason }}
              </div>
            </div>
            <div class="flex shrink-0 gap-1">
              <el-button size="small" text type="primary" @click="openDetail(row)">详情</el-button>
              <el-button size="small" text :type="configuredEnabled(row) ? 'warning' : 'success'" :disabled="isUninstalling(row)" @click="toggleSkill(row)">
                {{ configuredEnabled(row) ? '停用' : '启用' }}
              </el-button>
              <el-button size="small" text type="danger" :loading="isUninstalling(row)" @click="uninstallSkill(row)">卸载</el-button>
            </div>
          </div>
        </article>
      </div>
    </main>

    <el-drawer v-model="detailOpen" size="70%" :title="detail?.name ? `Skill 详情 · ${detail.name}` : 'Skill 详情'">
      <div v-if="detail" class="h-full min-h-0 overflow-y-auto" v-loading="detailLoading">
        <section class="mac-panel p-4">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0">
              <div class="flex items-center gap-2">
                <el-tag :type="statusType(detail)" round>{{ statusText(detail) }}</el-tag>
                <span class="text-2xl">{{ displayEmoji(detail) }}</span>
                <h2 class="truncate text-lg font-semibold">{{ detail.name }}</h2>
              </div>
              <p class="mt-2 text-sm leading-6 text-mactext/80">{{ detail.description || '暂无描述' }}</p>
            </div>
            <div class="flex shrink-0 gap-2">
              <el-button :type="configuredEnabled(detail) ? 'warning' : 'success'" plain round :disabled="isUninstalling(detail)" @click="toggleSkill(detail)">
                {{ configuredEnabled(detail) ? '停用' : '启用' }}
              </el-button>
              <el-button type="danger" plain round :loading="isUninstalling(detail)" @click="uninstallSkill(detail)">卸载</el-button>
            </div>
          </div>
        </section>

        <section class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div class="mac-panel p-4 text-sm leading-7">
            <h3 class="mb-2 text-sm font-semibold">Metadata</h3>
            <div>status：<code>{{ detail.status }}</code></div>
            <div>skillKey：<code>{{ detail.skillKey || '—' }}</code></div>
            <div>primaryEnv：<code>{{ detail.primaryEnv || '—' }}</code></div>
            <div>homepage：<code>{{ detail.homepage || '—' }}</code></div>
            <div>always：<code>{{ detail.always ? 'true' : 'false' }}</code></div>
          </div>
          <div class="mac-panel p-4 text-sm leading-7">
            <h3 class="mb-2 text-sm font-semibold">路径 / 依赖</h3>
            <div class="break-all">location：<code>{{ detail.location }}</code></div>
            <div class="break-all">baseDir：<code>{{ detail.baseDir }}</code></div>
            <div>requires.bins：<code>{{ reqList(detail, 'bins').join(', ') || '—' }}</code></div>
            <div>requires.env：<code>{{ reqList(detail, 'env').join(', ') || '—' }}</code></div>
          </div>
        </section>

        <section v-if="detail.reason" class="mac-panel mt-4 border-amber-200 bg-amber-50/70 p-4 text-sm leading-6 text-amber-900">
          <h3 class="mb-1 text-sm font-semibold">过滤原因</h3>
          {{ detail.reason }}
        </section>

        <section class="mac-panel mt-4 p-4">
          <div class="mb-2 flex items-center justify-between gap-3">
            <h3 class="text-sm font-semibold">SKILL.md（只读）</h3>
            <span class="text-xs text-macsub">{{ detail.content?.length || 0 }} chars</span>
          </div>
          <pre class="skill-content">{{ detail.content || '未读取到内容' }}</pre>
        </section>
      </div>
    </el-drawer>

    <el-dialog v-model="installOpen" title="安装 Skill 的推荐方式" width="620px">
      <div class="space-y-3 text-sm leading-6 text-zinc-700">
        <p>Web 管理界面负责浏览、启停、可恢复卸载和重新加载，不提供上传 ZIP、Git clone、在线编辑或新增安装功能。</p>
        <p>推荐在对话里让 OpenBear 人工处理安装：说明 Skill 来源、用途和安全边界，由 OpenBear 检查目录结构、依赖和 <code>SKILL.md</code> 后放入 Skills 目录。</p>
        <div class="rounded-2xl border border-macborder bg-zinc-50 p-3">
          <div class="text-xs text-macsub">当前 Skills 目录</div>
          <code class="break-all text-xs">{{ skillsDir || stats.directory || '—' }}</code>
        </div>
        <p>外部完成文件变更后，点击「重新加载」。新的启停配置和可用性会在下一轮对话生效。</p>
      </div>
      <template #footer>
        <el-button type="primary" @click="installOpen = false">知道了</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.skill-content {
  max-height: 56vh;
  overflow: auto;
  border-radius: 14px;
  border: 1px solid rgba(24, 24, 27, 0.08);
  background: rgba(250, 250, 250, 0.9);
  padding: 14px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 12px;
  line-height: 1.65;
  color: #27272a;
}
</style>
