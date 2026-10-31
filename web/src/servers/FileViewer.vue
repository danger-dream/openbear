<script setup>
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Download } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import { IMAGE_LIMIT, TEXT_LIMIT, decodeText, imageType, imageUrl, language, readLimited } from './fileKinds.js';
import { bytes, dateTime, safeError } from './serverHelpers.js';

const CodeEditor = defineAsyncComponent(() => import('../views/consoleView/contextEditor/CodeEditor.vue'));
// `asText` opens a file of unknown type as text; its content decides.
const props = defineProps({serverId: {type: String, required: true}, path: {type: String, required: true}, where: {type: String, default: ''},
  entry: {type: Object, required: true}, asText: Boolean, phone: Boolean});
const emit = defineEmits(['close', 'saved', 'download']);
const image = computed(() => !props.asText && Boolean(imageType(props.entry.name)));
const text = ref(''), original = ref(''), url = ref(''), loading = ref(true), error = ref(''), saving = ref(false);
const opened = {mtime: props.entry.mtime, size: props.entry.size};
const dirty = computed(() => !image.value && !error.value && text.value !== original.value);
const lang = computed(() => language(props.entry.name));
const dir = computed(() => props.where || props.path.replace(/\/[^/]+$/, '') || '/');

async function load() {
  const limit = image.value ? IMAGE_LIMIT : TEXT_LIMIT;
  const tooLarge = `文件超过 ${bytes(limit)}，请下载后查看。`;
  if (props.entry.size > limit) { error.value = tooLarge; loading.value = false; return; }
  try {
    const response = await fetch(Api.serverFileDownloadUrl(props.serverId, props.path), {credentials: 'same-origin'});
    if (!response.ok) { error.value = safeError({response: {status: response.status, data: await response.json().catch(() => ({}))}}); return; }
    const buffer = await readLimited(response, limit);
    if (buffer === null) { error.value = tooLarge; return; }
    if (image.value) { url.value = imageUrl(buffer, imageType(props.entry.name)); return; }
    const decoded = decodeText(buffer);
    if (decoded === null) error.value = '这不是 UTF-8 文本文件，可以下载后用本地工具查看。';
    else text.value = original.value = decoded;
  } catch (e) { error.value = safeError(e); }
  finally { loading.value = false; }
}
async function current() {
  try { return ok(await Api.serverFiles(props.serverId, dir.value)).entries.find(e => e.name === props.entry.name) || null; }
  catch { return null; }
}
async function save() {
  if (!dirty.value || saving.value) return;
  saving.value = true;
  try {
    const now = await current();
    if (now && (now.mtime !== opened.mtime || now.size !== opened.size)) {
      try {
        await ElMessageBox.confirm(`「${props.entry.name}」在你打开后被修改过（${dateTime(now.mtime * 1000)}）。继续保存会覆盖服务器上的版本。`, '文件已变化',
          {confirmButtonText: '覆盖保存', cancelButtonText: '取消', type: 'warning'});
      } catch { return; }
    }
    const submitted = text.value;
    await Api.uploadServerFile(props.serverId, props.path, new Blob([submitted], {type: 'text/plain'}), {overwrite: true});
    original.value = submitted;
    const fresh = await current();
    if (fresh) Object.assign(opened, {mtime: fresh.mtime, size: fresh.size});
    ElMessage.success('已保存');
    emit('saved');
  } catch (e) { ElMessage.error(safeError(e)); }
  finally { saving.value = false; }
}
async function close(done) {
  if (dirty.value) {
    try { await ElMessageBox.confirm('有未保存的修改，关闭后会丢失。', '放弃修改？', {confirmButtonText: '放弃', cancelButtonText: '继续编辑', type: 'warning'}); }
    catch { return; }
  }
  if (typeof done === 'function') done();
  emit('close');
}
function key(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); void save(); }
}
onMounted(load);
onBeforeUnmount(() => { if (url.value.startsWith('blob:')) URL.revokeObjectURL(url.value); });
</script>
<template>
  <el-dialog :model-value="true" :fullscreen="phone" width="min(1100px, 94vw)" append-to-body :close-on-click-modal="false" :before-close="close"
    :class="['admin-dialog', 'instance-dialog', 'instance-ui', 'servers-ui', 'fv-dialog', phone ? 'is-phone' : '']" :aria-label="`文件 ${path}`">
    <template #header>
      <div class="fv-head">
        <strong class="fv-name" :title="path">{{ entry.name }}</strong>
        <span class="fv-meta mono">{{ dir }} · {{ bytes(entry.size) }}{{ dirty ? ' · 未保存' : '' }}</span>
      </div>
    </template>
    <div class="fv" @keydown="key">
      <p v-if="loading" class="instance-muted fv-note" role="status">正在读取…</p>
      <div v-else-if="error" class="fv-note">
        <p class="instance-error">{{ error }}</p>
        <el-button size="small" @click="emit('download')"><el-icon><Download /></el-icon>下载</el-button>
      </div>
      <div v-else-if="image" class="fv-image"><img :src="url" :alt="entry.name" /></div>
      <div v-else class="fv-editor"><CodeEditor v-model="text" :language="lang" :label="`编辑 ${entry.name}`" /></div>
    </div>
    <template #footer>
      <div class="instance-form-footer">
        <span class="ob-hint">{{ image || error ? '' : '保存时保留原来的属主和权限；Ctrl / ⌘ + S 保存。' }}</span>
        <el-button @click="emit('download')">下载</el-button>
        <el-button @click="close()">关闭</el-button>
        <el-button v-if="!image && !error && !loading" type="primary" :loading="saving" :disabled="!dirty" @click="save">保存</el-button>
      </div>
    </template>
  </el-dialog>
</template>
