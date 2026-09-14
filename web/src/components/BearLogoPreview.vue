<script setup>
import { nextTick, ref } from "vue";
import { ElImageViewer } from "element-plus";
import BearLogo from "./BearLogo.vue";

const previewOpen = ref(false);
const trigger = ref(null);
const originalImages = ["/assets/brand/openbear-original-d32cdfb09c17.png"];

function openPreview() {
  previewOpen.value = true;
}

async function closePreview() {
  previewOpen.value = false;
  await nextTick();
  trigger.value?.focus({ preventScroll: true });
}
</script>

<template>
  <span class="bear-logo-preview">
    <button
      ref="trigger"
      type="button"
      class="bear-logo-preview-trigger"
      title="查看 OpenBear 大图"
      aria-label="查看 OpenBear 大图"
      aria-haspopup="dialog"
      :aria-expanded="previewOpen"
      @click.stop="openPreview"
    >
      <BearLogo />
    </button>
    <ElImageViewer
      v-if="previewOpen"
      :url-list="originalImages"
      :initial-index="0"
      :infinite="false"
      :close-on-press-escape="true"
      hide-on-click-modal
      teleported
      @close="closePreview"
    />
  </span>
</template>

<style scoped>
.bear-logo-preview,
.bear-logo-preview-trigger {
  display: block;
  width: 100%;
  height: 100%;
}

.bear-logo-preview-trigger {
  padding: 0;
  border: 0;
  border-radius: 22%;
  background: transparent;
  line-height: 0;
  cursor: zoom-in;
  touch-action: manipulation;
}

.bear-logo-preview-trigger:focus-visible {
  outline: 2px solid var(--bear-accent, #2563eb);
  outline-offset: 3px;
}
</style>
