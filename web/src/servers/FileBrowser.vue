<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { ArrowUp, Document, DocumentAdd, Folder, FolderAdd, Link, MoreFilled, Refresh, Upload } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import FloatingPanel from '../references/FloatingPanel.vue';
import FileViewer from './FileViewer.vue';
import { archiveBase, kind, modeBits, modeFrom } from './fileKinds.js';
import { pathsFor } from './filePaths.js';
import { bytes, dateTime, errorText, noAutofill, safeError } from './serverHelpers.js';

const props = defineProps({serverId: {type: String, required: true}, start: {type: String, default: ''}, platform: {type: String, default: 'linux'},
  phone: Boolean});
const P = computed(() => pathsFor(props.platform));
const home = () => props.start || P.value.start;
const path = ref(home()), draft = ref(P.value.show(home())), entries = ref([]), truncated = ref(false);
const loading = ref(false), error = ref(''), hidden = ref(false), dragging = ref(false), uploads = ref([]);
const picker = ref(null), selected = ref([]), busy = ref('');
const menu = reactive({open: false, anchor: null, targets: [], from: null});
const floating = ref(null), menuBox = ref(null);
const viewing = ref(null);
const chmod = reactive({open: false, targets: [], bits: [], octal: '', special: 0, recursive: false});
const pack = reactive({open: false, targets: [], name: '', format: 'tar.gz'});
let alive = true;

const shown = computed(() => entries.value.filter(e => hidden.value || !e.name.startsWith('.')));
const crumbs = computed(() => P.value.crumbs(path.value));
// Windows has ACLs, not mode bits; and the drive list itself holds no files.
const modes = computed(() => !P.value.windows);
const drives = computed(() => P.value.windows && path.value === '/');
const picked = computed(() => shown.value.filter(e => selected.value.includes(e.name)));
const allPicked = computed(() => shown.value.length > 0 && picked.value.length === shown.value.length);
const join = (dir, name) => P.value.join(dir, name);
const full = entry => join(path.value, entry.name);
const where = () => P.value.show(path.value) || '此电脑';

async function go(target = draft.value) {
  const next = P.value.normalize(target);
  loading.value = true; error.value = '';
  try {
    const data = ok(await Api.serverFiles(props.serverId, next));
    if (data.path !== path.value) selected.value = [];
    path.value = data.path;
    draft.value = P.value.show(data.path);
    entries.value = data.entries;
    truncated.value = data.truncated;
    selected.value = selected.value.filter(n => data.entries.some(e => e.name === n));
  } catch (e) {
    error.value = props.platform === 'darwin' && e?.response?.data?.error === 'permission_denied'
      ? '没有权限。macOS 的隐私保护不让读取这个目录：在“系统设置 → 隐私与安全性 → 完全磁盘访问权限”里加入 /usr/local/bin/openbear-node 后重试。'
      : safeError(e);
    draft.value = P.value.show(path.value);
  } finally { loading.value = false; }
}
function up() { if (path.value !== '/') go(P.value.parent(path.value)); }
// Folders open, text and images open in the viewer; anything else shows its menu.
function choose(entry, event) {
  const k = kind(entry);
  if (k === 'dir') go(full(entry));
  else if (k === 'text' || k === 'image') view(entry);
  else openMenu(event, entry);
}
function view(entry, asText = false) { viewing.value = {entry, path: full(entry), asText}; }
function toggle(entry) {
  selected.value = selected.value.includes(entry.name) ? selected.value.filter(n => n !== entry.name) : [...selected.value, entry.name];
}
function toggleAll() { selected.value = allPicked.value ? [] : shown.value.map(e => e.name); }
function download(entry) {
  const a = document.createElement('a');
  a.href = Api.serverFileDownloadUrl(props.serverId, full(entry));
  a.download = entry.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// One menu for right-click, the row's ⋯ button and the empty area. A row
// inside a multiple selection acts on the whole selection.
// Inputs keep the browser's own menu for copy and paste.
function background(event) { if (!event.target.closest('input, textarea')) openMenu(event, null); }
function openMenu(event, entry) {
  event?.preventDefault?.();
  event?.stopPropagation?.();
  if (busy.value) return;
  menu.targets = entry ? (picked.value.length > 1 && selected.value.includes(entry.name) ? picked.value : [entry]) : [];
  menu.anchor = event?.type === 'contextmenu' ? new DOMRect(event.clientX, event.clientY, 0, 0) : event?.currentTarget || null;
  menu.from = event?.currentTarget instanceof HTMLElement ? event.currentTarget : document.activeElement;
  menu.open = Boolean(menu.anchor);
  if (menu.open) void focusMenu();
}
// Focus moves into the menu, so Escape closes only the menu, not a sheet around it.
async function focusMenu() {
  await nextTick();
  await floating.value?.position();
  await nextTick();
  menuBox.value?.querySelector('.fb-menu-item')?.focus({preventScroll: true});
}
function closeMenu(refocus = false) { menu.open = false; if (refocus) menu.from?.focus?.({preventScroll: true}); }
function menuKey(event) {
  const items = [...(menuBox.value?.querySelectorAll('.fb-menu-item') || [])], at = items.indexOf(document.activeElement);
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeMenu(true); }
  else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    items[(at + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
  }
}
const actions = computed(() => {
  const t = menu.targets;
  if (drives.value) return t.length === 1 ? [['open', '打开'], ['copy', '复制路径']] : [['refresh', '刷新']];
  if (!t.length) return [['mkdir', '新建文件夹'], ['touch', '新建文件'], ['upload', '上传文件'], ['refresh', '刷新']];
  if (t.length > 1) return [['compress', `压缩 ${t.length} 项`], modes.value && ['chmod', '修改权限'], ['delete', `删除 ${t.length} 项`, 'danger']].filter(Boolean);
  const k = kind(t[0]);
  return [
    k === 'dir' && ['open', '打开'], k === 'link' && ['open', '打开'],
    k === 'text' && ['edit', '编辑'], k === 'image' && ['view', '查看'], k === 'other' && t[0].size <= 2 * 1024 * 1024 && ['text', '以文本打开'],
    k === 'archive' && ['extract', '解压…'],
    k !== 'dir' && ['download', '下载'],
    ['rename', '重命名…'], modes.value && ['chmod', '修改权限…'], ['compress', '压缩…'], ['copy', '复制路径'],
    ['delete', '删除', 'danger'],
  ].filter(Boolean);
});
async function act(key) {
  const targets = menu.targets, one = targets[0];
  closeMenu();
  if (key === 'open') go(full(one));
  else if (key === 'edit' || key === 'view') view(one);
  else if (key === 'text') view(one, true);
  else if (key === 'download') download(one);
  else if (key === 'copy') copy(P.value.show(full(one)));
  else if (key === 'upload') picker.value.click();
  else if (key === 'refresh') go(path.value);
  else if (key === 'mkdir' || key === 'touch') create(key);
  else if (key === 'rename') rename(one);
  else if (key === 'delete') remove(targets);
  else if (key === 'chmod') openChmod(targets);
  else if (key === 'compress') openPack(targets);
  else if (key === 'extract') extract(one);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); ElMessage.success('已复制路径'); }
  catch { ElMessage.warning('浏览器不允许复制'); }
}

// Every change runs on the server as a background job; long ones are polled.
async function run(op, data, label) {
  busy.value = label;
  try {
    let job = ok(await Api.serverFileOp(props.serverId, {op, ...data})).job, wait = 250;
    while (job.state === 'running' && alive) {
      await new Promise(resolve => setTimeout(resolve, wait));
      wait = Math.min(1500, wait * 1.6);
      job = ok(await Api.serverFileOpJob(props.serverId, job.id)).job;
    }
    if (job.state === 'failed') { ElMessage.error(errorText(job.error) + (job.detail ? `（${job.detail}）` : '')); return false; }
    return true;
  } catch (e) { ElMessage.error(safeError(e)); return false; }
  finally { busy.value = ''; if (alive) void go(path.value); }
}
const valid = value => P.value.validName(value);
async function ask(title, label, value = '') {
  try {
    const {value: answer} = await ElMessageBox.prompt(label, title, {inputValue: value, inputValidator: valid, confirmButtonText: '确定', cancelButtonText: '取消',
      customClass: 'fb-prompt'});
    return answer.trim();
  } catch { return null; }
}
async function create(kindOf) {
  const name = await ask(kindOf === 'mkdir' ? '新建文件夹' : '新建文件', `在 ${where()} 中创建`);
  if (!name || !await run(kindOf, {path: path.value, name}, '正在创建…') || kindOf !== 'touch') return;
  await go(path.value);
  const made = entries.value.find(e => e.name === name);
  if (made) view(made);
}
async function rename(entry) {
  const name = await ask('重命名', `「${entry.name}」的新名称`, entry.name);
  if (name && name !== entry.name) await run('rename', {path: full(entry), name}, '正在重命名…');
}
async function remove(targets) {
  const list = targets.slice(0, 5).map(e => `「${e.name}」`).join('、') + (targets.length > 5 ? ` 等 ${targets.length} 项` : '');
  const folders = targets.some(e => e.dir);
  try {
    await ElMessageBox.confirm(`删除 ${list}？${folders ? '文件夹里的内容会一起删除。' : ''}删除后无法恢复。`, '删除', {confirmButtonText: '删除', cancelButtonText: '取消',
      type: 'warning', confirmButtonClass: 'el-button--danger'});
  } catch { return; }
  if (await run('delete', {paths: targets.map(full)}, '正在删除…')) selected.value = [];
}
function openChmod(targets) {
  const mode = targets.length === 1 ? targets[0].mode : '0644';
  // setuid, setgid and sticky bits have no checkbox; keep them as they were.
  const special = (parseInt(mode || '0', 8) >> 9) & 7;
  Object.assign(chmod, {open: true, targets, bits: modeBits(mode), octal: modeFrom(modeBits(mode), special), special, recursive: false});
}
function syncOctal() { chmod.octal = modeFrom(chmod.bits, chmod.special); }
function syncBits() {
  if (!/^[0-7]{3,4}$/.test(chmod.octal)) return;
  chmod.bits = modeBits(chmod.octal.slice(-3));
  chmod.special = chmod.octal.length === 4 ? Number(chmod.octal[0]) : 0;
}
async function applyChmod() {
  if (!/^[0-7]{3,4}$/.test(chmod.octal)) { ElMessage.error(errorText('invalid_file_mode')); return; }
  chmod.open = false;
  await run('chmod', {paths: chmod.targets.map(full), mode: chmod.octal, recursive: chmod.recursive}, '正在修改权限…');
}
function openPack(targets) {
  const base = targets.length === 1 ? targets[0].name : (P.value.leaf(path.value) || 'archive');
  Object.assign(pack, {open: true, targets, name: base, format: P.value.windows ? 'zip' : 'tar.gz'});
}
async function applyPack() {
  const name = pack.name.trim();
  if (valid(name) !== true) { ElMessage.error(errorText('invalid_file_name')); return; }
  pack.open = false;
  if (await run('compress', {paths: pack.targets.map(full), name, format: pack.format}, '正在压缩…')) selected.value = [];
}
async function extract(entry) {
  try {
    const {value} = await ElMessageBox.prompt(`「${entry.name}」解压到（不存在会自动创建，同名文件会被覆盖）`, '解压', {
      inputValue: P.value.show(join(path.value, archiveBase(entry.name))), confirmButtonText: '解压', cancelButtonText: '取消', customClass: 'fb-prompt',
      inputValidator: v => (v && P.value.absolute(v)) || (P.value.windows ? '请填写带盘符的完整路径，例如 D:\\backup' : '请填写以 / 开头的完整路径')});
    await run('extract', {path: full(entry), dest: P.value.normalize(value)}, '正在解压…');
  } catch { /* cancelled */ }
}

const UPLOAD_LIMIT = 4 * 1024 ** 3;
async function send(file, overwrite = false) {
  const target = join(path.value, file.name);
  const task = reactive({name: file.name, size: file.size, done: 0, state: 'running', error: ''});
  uploads.value = [task, ...uploads.value.filter(u => u.state === 'running')].slice(0, 6);
  // The controller refuses larger uploads from Content-Length before any data is sent.
  if (file.size > UPLOAD_LIMIT) { task.state = 'failed'; task.error = errorText('body_too_large'); return; }
  try {
    await Api.uploadServerFile(props.serverId, target, file, {overwrite, onProgress: e => { task.done = e.loaded; }});
    task.state = 'done';
  } catch (e) {
    if (e?.response?.data?.error === 'file_exists' && !overwrite) {
      uploads.value = uploads.value.filter(u => u !== task);
      try { await ElMessageBox.confirm(`「${file.name}」已存在，要覆盖吗？`, '覆盖文件', {confirmButtonText: '覆盖', cancelButtonText: '取消', type: 'warning'}); }
      catch { return; }
      return send(file, true);
    }
    task.state = 'failed'; task.error = safeError(e);
  }
  void go(path.value);
}
async function upload(files) { for (const file of files) await send(file); }
function chosen(event) { upload([...event.target.files]); event.target.value = ''; }
function dropped(event) { dragging.value = false; if (event.dataTransfer?.files?.length) upload([...event.dataTransfer.files]); }
const icon = entry => entry.dir ? Folder : entry.type === 'link' ? Link : Document;
watch(() => props.serverId, () => { selected.value = []; go(home()); });
onMounted(() => go(home()));
onBeforeUnmount(() => { alive = false; });
</script>
<template>
  <div class="fb" :class="{'is-dragging': dragging}" @dragover.prevent="dragging = true" @dragleave.self="dragging = false" @drop.prevent="dropped"
    @contextmenu="background">
    <div class="fb-bar">
      <button type="button" class="ix-icon-button" title="上一级" aria-label="上一级" :disabled="path === '/'" @click="up"><el-icon><ArrowUp /></el-icon></button>
      <input v-bind="noAutofill" v-model="draft" class="mono fb-path" aria-label="路径" spellcheck="false" :placeholder="P.windows ? '此电脑' : '/'" @keydown.enter="go()" />
      <button type="button" class="ix-icon-button" title="刷新" aria-label="刷新" @click="go(path)"><el-icon><Refresh /></el-icon></button>
      <button type="button" class="ix-icon-button" title="新建文件夹" aria-label="新建文件夹" :disabled="Boolean(busy) || drives" @click="create('mkdir')"><el-icon><FolderAdd /></el-icon></button>
      <button type="button" class="ix-icon-button" title="新建文件" aria-label="新建文件" :disabled="Boolean(busy) || drives" @click="create('touch')"><el-icon><DocumentAdd /></el-icon></button>
      <button type="button" class="ix-icon-button" title="上传文件" aria-label="上传文件" :disabled="drives" @click="picker.click()"><el-icon><Upload /></el-icon></button>
      <input ref="picker" type="file" multiple hidden @change="chosen" />
    </div>
    <nav class="fb-crumbs" aria-label="路径">
      <button v-for="c in crumbs" :key="c.path" type="button" @click="go(c.path)">{{ c.name }}</button>
      <span class="ob-grow" />
      <label class="fb-hidden"><input v-model="hidden" type="checkbox" />显示隐藏文件</label>
    </nav>
    <div v-if="picked.length" class="fb-selection" role="toolbar" aria-label="已选文件">
      <label class="fb-hidden"><input type="checkbox" :checked="allPicked" :indeterminate="!allPicked" aria-label="全选" @change="toggleAll" />已选 {{ picked.length }} 项</label>
      <span class="ob-grow" />
      <button type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="openPack(picked)">压缩</button>
      <button v-if="modes" type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="openChmod(picked)">权限</button>
      <button type="button" class="ix-text-button ob-danger" :disabled="Boolean(busy)" @click="remove(picked)">删除</button>
      <button type="button" class="ix-text-button" @click="selected = []">取消</button>
    </div>
    <p v-if="busy" class="fb-busy" role="status"><span class="fb-spinner" aria-hidden="true"></span>{{ busy }}</p>
    <ul v-if="uploads.length" class="fb-uploads" aria-label="上传">
      <li v-for="u in uploads" :key="u.name + u.size" :data-state="u.state">
        <span class="fb-upload-name">{{ u.name }}</span>
        <span>{{ u.state === 'failed' ? u.error : u.state === 'done' ? '已上传' : `${Math.round(u.done / Math.max(u.size, 1) * 100)}%` }}</span>
        <i class="sv-bar" :style="{'--v': (u.state === 'done' ? 100 : u.done / Math.max(u.size, 1) * 100) + '%'}" aria-hidden="true"></i>
      </li>
    </ul>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <p v-if="loading && !entries.length" class="instance-muted fb-note" role="status">正在读取…</p>
    <p v-else-if="!shown.length && !error" class="instance-muted fb-note">空目录{{ entries.length ? '（只有隐藏文件）' : '' }}</p>
    <ul class="fb-list" :aria-busy="loading">
      <li v-for="e in shown" :key="e.name" class="fb-item" :class="{'is-picked': selected.includes(e.name)}" @contextmenu.stop="openMenu($event, e)">
        <input type="checkbox" class="fb-check" :checked="selected.includes(e.name)" :aria-label="`选择 ${e.name}`" @change="toggle(e)" />
        <button type="button" class="fb-row" :class="{'is-dot': e.name.startsWith('.')}" :disabled="e.type === 'other'"
          :title="e.target ? `${e.name} → ${e.target}` : e.name" @click="choose(e, $event)">
          <el-icon class="fb-icon" :data-kind="e.dir ? 'dir' : e.type"><component :is="icon(e)" /></el-icon>
          <span class="fb-name">{{ e.name }}</span>
          <span class="fb-size">{{ e.dir ? '' : bytes(e.size) }}</span>
          <span class="fb-mode mono">{{ modes ? e.mode : '' }}</span>
          <time class="fb-time">{{ dateTime(e.mtime * 1000) }}</time>
        </button>
        <button type="button" class="fb-more" :aria-label="`${e.name} 的操作`" title="更多操作" @click="openMenu($event, e)"><el-icon><MoreFilled /></el-icon></button>
      </li>
    </ul>
    <p v-if="truncated" class="instance-muted fb-note">目录内容太多，只显示了前 5000 项。</p>
    <p class="instance-muted fb-note fb-tip">点击文件夹进入，点击文本或图片直接打开；{{ phone ? '点 ⋯' : '右键或点 ⋯' }} 查看更多操作；把文件拖到这里上传。</p>

    <FloatingPanel ref="floating" :open="menu.open" :anchor="menu.anchor" placement="bottom-start" :width="200" label="文件操作" @close="closeMenu()">
      <div ref="menuBox" class="fb-menu" role="menu" @keydown="menuKey" :aria-label="menu.targets.length === 1 ? `${menu.targets[0].name} 的操作` : '文件操作'">
        <p v-if="menu.targets.length === 1" class="fb-menu-title" :title="menu.targets[0].name">{{ menu.targets[0].name }}</p>
        <button v-for="[key, label, tone] in actions" :key="key" type="button" role="menuitem" class="fb-menu-item" :data-tone="tone" @click="act(key)">{{ label }}</button>
      </div>
    </FloatingPanel>

    <el-dialog v-model="chmod.open" title="修改权限" width="380px" append-to-body class="admin-dialog instance-dialog instance-ui servers-ui fb-dialog">
      <p class="instance-muted fb-dialog-note">{{ chmod.targets.length === 1 ? chmod.targets[0].name : `${chmod.targets.length} 项` }}</p>
      <table class="fb-perm">
        <thead><tr><th></th><th>读</th><th>写</th><th>执行</th></tr></thead>
        <tbody>
          <tr v-for="(who, row) in ['所有者', '用户组', '其他人']" :key="who">
            <th scope="row">{{ who }}</th>
            <td v-for="col in 3" :key="col"><input v-model="chmod.bits[row * 3 + col - 1]" type="checkbox" :aria-label="`${who}${['读', '写', '执行'][col - 1]}`" @change="syncOctal" /></td>
          </tr>
        </tbody>
      </table>
      <label class="ob-field"><span>数字</span><input v-model="chmod.octal" v-bind="noAutofill" class="mono sv-day" maxlength="4" inputmode="numeric" aria-label="权限数字" @input="syncBits" /></label>
      <label v-if="chmod.targets.some(e => e.dir)" class="ob-check"><input v-model="chmod.recursive" type="checkbox" />同时应用到文件夹里的所有内容</label>
      <template #footer>
        <div class="instance-form-footer"><el-button @click="chmod.open = false">取消</el-button><el-button type="primary" @click="applyChmod">修改</el-button></div>
      </template>
    </el-dialog>
    <el-dialog v-model="pack.open" title="压缩" width="420px" append-to-body class="admin-dialog instance-dialog instance-ui servers-ui fb-dialog">
      <p class="instance-muted fb-dialog-note">{{ pack.targets.length === 1 ? pack.targets[0].name : `${pack.targets.length} 项` }} → {{ where() }}</p>
      <div class="ob-fields">
        <label class="ob-field"><span>文件名</span><input v-model="pack.name" v-bind="noAutofill" aria-label="压缩包名称" @keydown.enter="applyPack" /></label>
        <label class="ob-field"><span>格式</span>
          <select v-if="P.windows" v-model="pack.format" aria-label="压缩格式"><option value="zip">zip</option><option value="tar.gz">tar.gz</option></select>
          <select v-else v-model="pack.format" aria-label="压缩格式"><option value="tar.gz">tar.gz（通用）</option><option value="zip">zip（Windows 友好）</option><option value="tar.xz">tar.xz（更小，更慢）</option></select>
        </label>
      </div>
      <template #footer>
        <div class="instance-form-footer"><el-button @click="pack.open = false">取消</el-button><el-button type="primary" @click="applyPack">压缩</el-button></div>
      </template>
    </el-dialog>
    <FileViewer v-if="viewing" :server-id="serverId" :path="viewing.path" :where="P.show(P.parent(viewing.path)) || '此电脑'" :entry="viewing.entry" :as-text="viewing.asText" :phone="phone"
      @close="viewing = null" @saved="go(path)" @download="download(viewing.entry)" />
  </div>
</template>
