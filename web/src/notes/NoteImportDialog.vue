<script setup>
import { computed, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { FolderUp } from '@lucide/vue';
import { Api, apiError } from '../api.js';

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  targetParentUuid: { type: String, default: '' },
  targetParentTitle: { type: String, default: '' },
});

const emit = defineEmits(['update:modelValue', 'imported']);

const importBusy = ref(false);
const fileInputRef = ref(null);
const selectedFiles = ref([]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
});

function onPickFiles(event) {
  const list = [...(event.target?.files || [])];
  selectedFiles.value = list;
}

async function runFilesImport() {
  if (!selectedFiles.value.length) {
    ElMessage.warning('请先选择 .md 或 .zip 文件');
    return;
  }
  importBusy.value = true;
  try {
    const res = await Api.importKbNoteFiles(selectedFiles.value, props.targetParentUuid || '');
    ElMessage.success(
      `导入完成：共创建 ${res.createdCount} 篇笔记${res.importedAssets ? `，导入图片 ${res.importedAssets} 张` : ''}`,
    );
    selectedFiles.value = [];
    visible.value = false;
    emit('imported', res);
  } catch (err) {
    ElMessage.error(apiError(err));
  } finally {
    importBusy.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="导入笔记"
    width="min(560px, calc(100vw - 24px))"
    append-to-body
    class="kb-import-dialog mobile-viewport-dialog"
    :close-on-click-modal="!importBusy"
  >
    <p v-if="targetParentTitle" class="kb-import-target">
      将导入到上级笔记：<strong>{{ targetParentTitle }}</strong>
    </p>

    <div class="kb-import-body">
      <div class="kb-dropzone" @click="fileInputRef?.click()">
        <FolderUp :size="26" />
        <strong>点击选择 .md 或 .zip 压缩包</strong>
        <small>支持多选 Markdown 文件，或包含目录结构与 assets 图片的 ZIP 导出包</small>
        <input
          ref="fileInputRef"
          type="file"
          accept=".md,.markdown,.txt,.zip"
          multiple
          hidden
          @change="onPickFiles"
        />
      </div>
      <ul v-if="selectedFiles.length" class="kb-picked-files">
        <li v-for="f in selectedFiles" :key="f.name">{{ f.name }} <small>({{ (f.size / 1024).toFixed(1) }} KiB)</small></li>
      </ul>
    </div>

    <template #footer>
      <el-button :disabled="importBusy" @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="importBusy"
        :disabled="!selectedFiles.length"
        @click="runFilesImport"
      >
        导入所选文件
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.kb-import-target {
  margin: 0 0 10px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--ob-surface-soft, rgba(127, 127, 127, 0.06));
  font-size: 12px;
}
.kb-import-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.kb-dropzone {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 28px 16px;
  border: 1.5px dashed var(--ob-border-strong, rgba(127, 127, 127, 0.32));
  border-radius: 10px;
  cursor: pointer;
  color: var(--ob-text-subtle, #6e7781);
  text-align: center;
}
.kb-dropzone:hover {
  border-color: var(--ob-blue, #2563eb);
  color: var(--ob-text, #1f2328);
}
.kb-dropzone small {
  font-size: 11.5px;
  color: var(--ob-text-muted, #8c959f);
}
.kb-picked-files {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  max-height: 130px;
  overflow-y: auto;
}
</style>
