<script setup>
import {onBeforeUnmount, onMounted, ref, watch} from "vue";
import {ArrowUp, Grid} from "@element-plus/icons-vue";
import "./sidebarResources.css";

const props = defineProps({
  items: {type: Array, default: () => []},
  active: {type: String, default: ""},
  sidebarOpen: {type: Boolean, default: false},
});
const emit = defineEmits(["select"]);
const phone = ref(false);
const menuOpen = ref(false);
let media;
function updateMedia() {
  phone.value = Boolean(media?.matches);
  if (!phone.value) menuOpen.value = false;
}
function selectPage(key) {
  menuOpen.value = false;
  emit("select", key);
}
watch(() => [props.sidebarOpen, props.active], () => { menuOpen.value = false; });
onMounted(() => {
  media = window.matchMedia("(max-width: 760px)");
  updateMedia();
  media.addEventListener("change", updateMedia);
});
onBeforeUnmount(() => media?.removeEventListener("change", updateMedia));
</script>

<template>
  <footer v-if="phone" class="mobile-sidebar-resources">
    <el-popover v-model:visible="menuOpen" trigger="click" placement="top-start" :width="264" :show-arrow="false" popper-class="sidebar-resources-popper">
      <template #reference>
        <button type="button" class="sidebar-resources-entry" aria-label="资源与设置" aria-haspopup="dialog" :aria-expanded="menuOpen">
          <Grid/><span>资源与设置</span><ArrowUp class="resources-caret" :class="{'is-open': menuOpen}"/>
        </button>
      </template>
      <nav class="sidebar-resources-grid sidebar-resource-grid" aria-label="资源与设置">
        <button v-for="item in props.items" :key="item.key" type="button" class="sidebar-resource-tile" :aria-current="props.active === item.key ? 'page' : undefined" @click="selectPage(item.key)">
          <el-icon class="sidebar-resource-icon" :size="18" aria-hidden="true"><component :is="item.icon"/></el-icon><span class="sidebar-resource-label">{{ item.label }}</span>
        </button>
      </nav>
    </el-popover>
  </footer>
</template>

<style scoped>
.mobile-sidebar-resources { flex: 0 0 auto; margin-top: 6px; padding-top: 4px; border-top: 1px solid var(--el-border-color-lighter); }
.sidebar-resources-entry { display: flex; align-items: center; gap: 10px; width: 100%; height: 44px; padding: 0 8px; border: 0; border-radius: 9px; background: transparent; color: var(--el-text-color-regular); font-size: 13px; cursor: pointer; }
.sidebar-resources-entry svg { flex: none; width: 17px; height: 17px; color: var(--el-text-color-secondary); }
.sidebar-resources-entry .resources-caret { width: 12px; height: 12px; margin-left: auto; transition: transform .15s ease; }
.resources-caret.is-open { transform: rotate(180deg); }
.sidebar-resources-grid { max-height: calc(var(--mobile-viewport-height, 100dvh) - 100px); overflow-y: auto; overscroll-behavior: contain; font-size: 13px; line-height: 20px; }
.sidebar-resources-entry:hover { background: var(--el-fill-color-light); color: var(--el-text-color-primary); }
button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
@media (prefers-reduced-motion: reduce) { .resources-caret { transition: none; } }
</style>

<style>
.sidebar-resources-popper.el-popper { padding: 6px; border-radius: 14px; max-width: calc(100vw - 24px); }
</style>
