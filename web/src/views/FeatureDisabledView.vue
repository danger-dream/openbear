<script setup>
import { computed, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Api, apiError } from '../api.js';
import AdminPageHeader from '../components/AdminPageHeader.vue';
import { FEATURE_LABELS, FEATURE_SETTINGS, applyFeatureSettings } from '../featureToggles.js';

// Stands in for 实例/主机 when its switch is off (or not yet known) so a direct
// link never lands on a blank page.
const props = defineProps({
  feature: { type: String, required: true },
  pending: { type: Boolean, default: false },
});
const emit = defineEmits(['mobile-header-ready', 'open-settings']);
const label = computed(() => FEATURE_LABELS[props.feature] || '');
const tool = computed(() => (props.feature === 'servers' ? 'Servers' : 'InstanceControl'));
const enabling = ref(false);

async function enable() {
  if (enabling.value) return;
  enabling.value = true;
  try {
    const saved = await Api.updateSetting(FEATURE_SETTINGS[props.feature], true);
    if (saved?.ok === false) throw new Error(saved.error || '开启失败');
    const fresh = await Api.settings();
    applyFeatureSettings(fresh?.values || { [FEATURE_SETTINGS[props.feature]]: true });
    ElMessage.success(`已开启${label.value}`);
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    enabling.value = false;
  }
}
</script>

<template>
  <section class="feature-off h-full min-h-0" :data-feature="feature">
    <AdminPageHeader :title="label" inline-actions @mobile-header-ready="emit('mobile-header-ready', $event)">
      <template #mobile-navigation><slot name="mobile-navigation" /></template>
    </AdminPageHeader>
    <div v-if="!pending" class="feature-off__body" role="status">
      <h2>{{ label }}功能未开启</h2>
      <p>开启后显示“{{ label }}”菜单，并向模型开放 {{ tool }} 工具（每轮会多占用一些 token）。</p>
      <div class="feature-off__actions">
        <el-button type="primary" :loading="enabling" @click="enable">开启{{ label }}</el-button>
        <button type="button" class="feature-off__link" @click="emit('open-settings', FEATURE_SETTINGS[feature])">在系统设置中管理</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.feature-off { display: flex; flex-direction: column; overflow: hidden; background: var(--ob-chat-bg); }
.feature-off > .admin-page-header { flex: none; }
.feature-off__body { max-width: 460px; margin: 64px auto; padding: 0 20px; text-align: center; }
.feature-off__body h2 { margin-bottom: 8px; color: var(--ob-chat-text); font-size: 16px; font-weight: 600; }
.feature-off__body p { margin-bottom: 18px; color: var(--ob-chat-subtle); font-size: 13px; line-height: 1.7; }
.feature-off__actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 8px 16px; }
.feature-off__link { min-height: 32px; padding: 0 4px; border: 0; background: none; color: var(--ob-chat-subtle); font-size: 13px; cursor: pointer; }
.feature-off__link:hover { color: var(--ob-chat-text); }
.feature-off__link:focus-visible { outline: 2px solid var(--ob-focus); outline-offset: 2px; border-radius: 4px; }
@media (max-width: 760px) {
  .feature-off__body { margin-top: 48px; }
  .feature-off__actions .el-button, .feature-off__link { min-height: 44px; }
}
</style>
