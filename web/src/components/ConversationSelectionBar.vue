<script setup>
import { Box, Check, Delete, FolderOpened, MoreFilled, RefreshLeft } from "@element-plus/icons-vue";

defineProps({
  count: { type: Number, default: 0 },
  unreadCount: { type: Number, default: 0 },
  scopeLabel: { type: String, default: "" },
  unarchive: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(["select-scope", "select-automatic", "clear", "done", "read", "move", "archive", "delete"]);
</script>

<template>
  <div class="selection-bar" role="toolbar" aria-label="批量操作">
    <div class="selection-head">
      <span class="selection-count" role="status" aria-live="polite">已选 {{ count }} 个</span>
      <button class="selection-link" type="button" :disabled="busy || !scopeLabel" :title="scopeLabel ? `全选「${scopeLabel}」中的会话` : '先在目录中选中一个目录'" @click="emit('select-scope')">全选本目录</button>
      <el-dropdown trigger="click" placement="top-end" :disabled="busy" @command="emit($event)">
        <button class="selection-icon" type="button" aria-label="更多选择方式" :disabled="busy"><el-icon><MoreFilled /></el-icon></button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="select-automatic" :disabled="!scopeLabel">只选本目录中自动创建的</el-dropdown-item>
            <el-dropdown-item command="clear" :disabled="!count">清空选择</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <button class="selection-done" type="button" @click="emit('done')">完成</button>
    </div>
    <div class="selection-actions">
      <button type="button" :disabled="busy || !unreadCount" @click="emit('read')"><el-icon><Check /></el-icon><span>已读</span></button>
      <button type="button" :disabled="busy || !count" @click="emit('move')"><el-icon><FolderOpened /></el-icon><span>移动</span></button>
      <button type="button" :disabled="busy || !count" @click="emit('archive')"><el-icon><component :is="unarchive ? RefreshLeft : Box" /></el-icon><span>{{ unarchive ? '取消归档' : '归档' }}</span></button>
      <button class="danger" type="button" :disabled="busy || !count" @click="emit('delete')"><el-icon><Delete /></el-icon><span>删除</span></button>
    </div>
  </div>
</template>

<style scoped>
.selection-bar { display:flex; flex:none; flex-direction:column; gap:4px; margin:0 7px; padding:6px 4px 2px; border-top:1px solid var(--ob-border-soft); }
.selection-head { display:flex; min-width:0; align-items:center; gap:4px; }
.selection-count { min-width:0; flex:1; overflow:hidden; color:var(--ob-text); font-size:12px; font-variant-numeric:tabular-nums; text-overflow:ellipsis; white-space:nowrap; }
.selection-link,.selection-done,.selection-icon { display:inline-flex; min-height:26px; align-items:center; justify-content:center; padding:0 7px; border:0; border-radius:7px; background:transparent; color:var(--ob-text-subtle); font:inherit; font-size:12px; white-space:nowrap; cursor:pointer; }
.selection-icon { width:26px; padding:0; }
.selection-done { color:var(--ob-blue); font-weight:500; }
.selection-actions { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:2px; }
.selection-actions button { display:flex; min-width:0; min-height:30px; align-items:center; justify-content:center; gap:4px; padding:0 4px; border:0; border-radius:7px; background:transparent; color:var(--ob-text-subtle); font:inherit; font-size:12px; cursor:pointer; }
.selection-actions button span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.selection-actions button.danger { color:var(--ob-danger); }
.selection-bar button:hover:not(:disabled) { background:var(--ob-hover); color:var(--ob-text); }
.selection-actions button.danger:hover:not(:disabled) { background:rgb(var(--ob-danger-rgb) / .1); color:var(--ob-danger); }
.selection-bar button:focus-visible { outline:2px solid var(--ob-blue); outline-offset:-2px; }
.selection-bar button:disabled { opacity:.4; cursor:default; }
@media (max-width:760px), (hover:none) and (pointer:coarse) {
  .selection-link,.selection-done { min-height:44px; padding:0 10px; }
  .selection-icon { width:44px; min-height:44px; }
  .selection-actions button { min-height:44px; }
}
</style>
