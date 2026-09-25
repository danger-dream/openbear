<script setup>
import { computed } from "vue";
import { Box, Check, CollectionTag, CopyDocument, Delete, Edit, More, View, Hide } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { formatAssetTime } from "../utils/assetTime";
import { copyTextToClipboard } from "../utils/clipboard";

const props = defineProps({
  modelValue: Boolean, item: { type: Object, required: true }, detail: { type: Object, default: null },
  kind: { type: String, required: true }, view: { type: String, default: "detail" },
  loading: Boolean, busy: Boolean, error: { type: String, default: "" },
  tokens: { type: Number, default: 0 }, secretValues: Boolean,
});
const emit = defineEmits(["update:modelValue", "action", "retry", "update:secretValues"]);
const title = computed(() => props.item.title || props.item.name || "未命名");
const reference = computed(() => {
  const key = props.kind === "memory" ? props.item.ref : props.item.name;
  return key ? `@${{ memory: "mem", secrets: "secret", docs: "doc" }[props.kind]}/${key}` : "";
});
const fields = computed(() => {
  const row = props.detail || props.item;
  const result = [{ label: "引用", value: reference.value || "未设置" }];
  if (props.kind === "memory") result.push({ label: "Tokens", value: `${props.tokens.toLocaleString()} tk` }, { label: "备注", value: row.note || "—" });
  if (props.kind === "docs") result.push({ label: "优先级", value: `P${row.importance ?? "—"}` }, { label: "项目", value: row.project || "—" }, { label: "标签", value: row.tags || "—" });
  if (props.kind === "secrets") result.push({ label: "字段数", value: (row.kv || []).length }, { label: "备注", value: row.note || "—" });
  result.push(
    { label: "注入状态", value: props.item.archived ? "已归档" : props.item.enabled ? "已启用" : "未注入" },
    { label: "分组", value: row.grp || "未分组" },
    { label: "排序", value: row.sort ?? "—" },
    { label: "创建时间", value: formatAssetTime(row.createdAt ?? row.created_at) },
    { label: "修改时间", value: formatAssetTime(row.updatedAt ?? row.updated_at) },
  );
  return result;
});
function maskValue(value) {
  const text = String(value ?? "");
  if (!text) return "";
  return text.length <= 6 ? "••••••" : `${text.slice(0, 2)}••••••${text.slice(-2)}`;
}
async function copyReference() {
  try { await copyTextToClipboard(reference.value); ElMessage.success("已复制引用"); }
  catch { ElMessage.error("复制失败，请手动复制引用"); }
}
</script>

<template>
  <el-drawer :model-value="props.modelValue" direction="btt" size="auto" :title="title" class="mobile-asset-sheet" append-to-body destroy-on-close @update:model-value="emit('update:modelValue', $event)">
    <div class="mobile-asset-grip" aria-hidden="true"></div>
    <template v-if="props.view === 'detail'">
      <p v-if="props.loading" class="asset-sheet-notice" role="status">正在读取内容…</p>
      <div v-else-if="props.error" class="asset-sheet-notice" role="alert">{{ props.error }}<button type="button" @click="emit('retry')">重试</button></div>
      <template v-else-if="props.detail">
        <p v-if="props.kind === 'docs' && props.detail.summary" class="asset-sheet-subtitle">{{ props.detail.summary }}</p>
        <dl class="asset-sheet-fields"><div v-for="field in fields" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ Array.isArray(field.value) ? field.value.join(' · ') : field.value }}</dd></div></dl>
        <section v-if="props.kind === 'secrets'" class="asset-sheet-content">
          <dl class="asset-sheet-fields asset-secret-fields"><div v-for="(field, index) in props.detail.kv || []" :key="index"><dt>{{ field.key }}</dt><dd>{{ props.secretValues ? field.value : maskValue(field.value) }}</dd></div></dl>
          <button type="button" class="asset-sheet-action" @click="emit('update:secretValues', !props.secretValues)"><Hide v-if="props.secretValues"/><View v-else/><span>{{ props.secretValues ? '隐藏明文' : '显示明文' }}</span></button>
        </section>
        <section v-else class="asset-sheet-content"><h3>{{ props.kind === 'memory' ? '正文' : '文档内容' }}</h3><pre>{{ (props.kind === 'memory' ? props.detail.body : props.detail.content) || '（空）' }}</pre></section>
      </template>
      <div class="asset-sheet-actions"><button type="button" class="asset-sheet-action" :disabled="props.busy || props.loading" @click="emit('action', 'edit')"><Edit/><span>编辑内容</span></button><button type="button" class="asset-sheet-action" :disabled="props.busy" @click="emit('action', 'more')"><More/><span>更多操作</span></button></div>
    </template>
    <template v-else>
      <p class="asset-sheet-subtitle">{{ reference }}</p>
      <div class="asset-sheet-actions">
        <button type="button" class="asset-sheet-action" :disabled="props.busy" @click="emit('action', 'edit')"><Edit/><span>编辑内容</span></button>
        <button type="button" class="asset-sheet-action" role="switch" :aria-checked="!!props.item.enabled" :disabled="props.busy || !!props.item.archived" @click="emit('action', 'enabled')"><Check/><span>启用注入</span><i class="asset-sheet-switch"></i></button>
        <button v-if="props.kind === 'memory'" type="button" class="asset-sheet-action" role="switch" :aria-checked="!!props.item.expanded" :disabled="props.busy || !!props.item.archived" @click="emit('action', 'expanded')"><CollectionTag/><span>提示词展开</span><i class="asset-sheet-switch"></i></button>
        <button type="button" class="asset-sheet-action" :disabled="props.busy" @click="emit('action', 'archive')"><Box/><span>{{ props.item.archived ? '恢复归档' : '归档' }}</span></button>
      </div>
      <div class="asset-sheet-actions">
        <button type="button" class="asset-sheet-action" :disabled="!reference" @click="copyReference"><CopyDocument/><span>复制引用</span></button>
        <button type="button" class="asset-sheet-action is-danger" :disabled="props.busy" @click="emit('action', 'remove')"><Delete/><span>删除</span></button>
      </div>
    </template>
  </el-drawer>
</template>

<style>
.mobile-asset-sheet.el-drawer { left: 0; right: 0; bottom: max(0px, calc(100% - var(--mobile-viewport-top, 0px) - var(--mobile-viewport-height, 100dvh))); width: 100%; max-height: calc(var(--mobile-viewport-height, 100dvh) * .87); border-radius: 20px 20px 0 0; background: var(--ob-chat-bg); color: var(--ob-chat-text); }
.mobile-asset-sheet .el-drawer__header { flex: none; margin: 0; padding: 20px 17px 8px; color: var(--ob-chat-text); }
.mobile-asset-sheet .el-drawer__title { min-width: 0; overflow-wrap: anywhere; font-size: 15px; font-weight: 500; line-height: 22px; }
.mobile-asset-sheet .el-drawer__close-btn { flex: none; width: 36px; height: 36px; border-radius: 50%; padding: 8px; color: var(--ob-chat-subtle); background: var(--ob-chat-hover); }
.mobile-asset-sheet .el-drawer__body { min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 0 17px max(21px, env(safe-area-inset-bottom, 0px)); scrollbar-width: none; }
.mobile-asset-grip { position: absolute; top: 8px; left: calc(50% - 15px); width: 30px; height: 4px; border-radius: 5px; background: var(--ob-chat-border); }
.mobile-asset-sheet .asset-sheet-subtitle { margin: 0 0 13px; overflow-wrap: anywhere; font-size: 11px; line-height: 1.7; color: var(--ob-chat-subtle); }
.mobile-asset-sheet .asset-sheet-fields, .mobile-asset-sheet .asset-sheet-content, .mobile-asset-sheet .asset-sheet-actions { margin: 0 0 12px; overflow: hidden; border-radius: 11px; background: var(--ob-chat-panel); }
.mobile-asset-sheet .asset-sheet-fields > div { display: flex; align-items: baseline; gap: 12px; min-height: 43px; padding: 10px 13px; font-size: 12px; line-height: 1.6; }
.mobile-asset-sheet .asset-sheet-fields > div + div { border-top: 1px solid var(--ob-chat-line); }
.mobile-asset-sheet dt { flex: none; min-width: 64px; color: var(--ob-chat-subtle); }
.mobile-asset-sheet dd { min-width: 0; margin: 0 0 0 auto; overflow-wrap: anywhere; text-align: right; font-size: 11px; }
.mobile-asset-sheet .asset-secret-fields { margin-bottom: 0; }
.mobile-asset-sheet .asset-secret-fields dt { flex: 0 1 35%; min-width: 0; overflow-wrap: anywhere; }
.mobile-asset-sheet .asset-secret-fields dd { white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.mobile-asset-sheet .asset-sheet-content h3 { margin: 0; padding: 12px 13px 0; color: var(--ob-chat-subtle); font-size: 11px; font-weight: 400; }
.mobile-asset-sheet .asset-sheet-content pre { margin: 0; padding: 8px 13px 13px; white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; font-size: 12px; line-height: 1.85; }
.mobile-asset-sheet .asset-sheet-action { display: flex; align-items: center; gap: 11px; width: 100%; min-height: 46px; padding: 0 13px; border: 0; color: var(--ob-chat-text); background: transparent; text-align: left; font-size: 12px; }
.mobile-asset-sheet .asset-sheet-action + .asset-sheet-action { border-top: 1px solid var(--ob-chat-line); }
.mobile-asset-sheet .asset-sheet-action > span { flex: 1; }
.mobile-asset-sheet .asset-sheet-action > svg { flex: none; width: 16px; height: 16px; color: var(--ob-chat-subtle); }
.mobile-asset-sheet .asset-sheet-action:disabled { opacity: .4; cursor: default; }
.mobile-asset-sheet .asset-sheet-action.is-danger, .mobile-asset-sheet .asset-sheet-action.is-danger > svg { color: var(--ob-danger); }
.mobile-asset-sheet button:focus-visible { outline: 2px solid var(--ob-focus); outline-offset: -2px; }
.mobile-asset-sheet .asset-sheet-switch { position: relative; flex: none; width: 30px; height: 18px; border-radius: 20px; background: var(--ob-chat-selected); }
.mobile-asset-sheet .asset-sheet-switch::after { position: absolute; top: 3px; left: 3px; width: 12px; height: 12px; border-radius: 50%; background: var(--ob-chat-muted); content: ''; }
.mobile-asset-sheet [aria-checked='true'] .asset-sheet-switch { background: var(--ob-chat-button); }
.mobile-asset-sheet [aria-checked='true'] .asset-sheet-switch::after { left: 15px; background: var(--ob-chat-button-text); }
.mobile-asset-sheet .asset-sheet-notice { padding: 16px 4px; font-size: 12px; color: var(--ob-chat-subtle); }
.mobile-asset-sheet .asset-sheet-notice button { margin-left: 12px; text-decoration: underline; }
</style>
