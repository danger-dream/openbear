<script setup>
import { onMounted, reactive, ref } from 'vue';
import { ElMessageBox } from 'element-plus';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import { safeError, noAutofill } from './serverHelpers.js';

const props = defineProps({ready: Boolean});
const emit = defineEmits(['run', 'close']);
const items = ref([]), editing = ref(false), error = ref(''), busy = ref(false), dragging = ref(null);
const dialog = reactive({open: false, id: null, name: '', command: ''});

async function load() {
  try { items.value = ok(await Api.serverCommands()).items; error.value = ''; } catch (e) { error.value = safeError(e); }
}
function pick(item) {
  if (editing.value) Object.assign(dialog, {open: true, id: item.id, name: item.name, command: item.command});
  else if (props.ready) emit('run', item.command);
}
function add() { Object.assign(dialog, {open: true, id: null, name: '', command: ''}); }
async function save() {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try {
    const body = {name: dialog.name.trim(), command: dialog.command};
    items.value = ok(await (dialog.id ? Api.updateServerCommand(dialog.id, body) : Api.addServerCommand(body))).items;
    dialog.open = false;
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
async function remove() {
  try { await ElMessageBox.confirm(`删除命令「${dialog.name}」？`, '删除命令', {confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning'}); } catch { return; }
  try { items.value = ok(await Api.deleteServerCommand(dialog.id)).items; dialog.open = false; } catch (e) { error.value = safeError(e); }
}
function drop(target) {
  const from = items.value.findIndex(i => i.id === dragging.value), to = items.value.findIndex(i => i.id === target.id);
  dragging.value = null;
  if (from < 0 || to < 0 || from === to) return;
  const next = [...items.value];
  next.splice(to, 0, next.splice(from, 1)[0]);
  items.value = next;
  Api.orderServerCommands(next.map(i => i.id)).then(r => { items.value = ok(r).items; }).catch(e => { error.value = safeError(e); void load(); });
}
onMounted(load);
</script>
<template>
  <section class="tw-commands" aria-label="常用命令">
    <header>
      <strong>常用命令</strong>
      <span class="instance-muted">{{ editing ? '点击命令修改，拖动调整顺序' : ready ? '点击发送到当前终端' : '先打开一个终端' }}</span>
      <span class="ob-grow" />
      <button type="button" class="ix-text-button" @click="add">添加</button>
      <button v-if="items.length" type="button" class="ix-text-button" :aria-pressed="editing" @click="editing = !editing">{{ editing ? '完成' : '编辑' }}</button>
      <button type="button" class="ix-text-button" aria-label="收起常用命令" @click="emit('close')">收起</button>
    </header>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <p v-if="!items.length" class="instance-muted tw-commands-empty">还没有常用命令。添加后一键发送到终端，多行命令按顺序执行。</p>
    <div v-else class="tw-chips">
      <button v-for="item in items" :key="item.id" type="button" class="tw-chip" :class="{'is-editing': editing}" :title="item.command" :disabled="!editing && !ready"
        :draggable="editing" @click="pick(item)" @dragstart="dragging = item.id" @dragover.prevent @drop.prevent="drop(item)">{{ item.name }}</button>
    </div>
    <el-dialog v-model="dialog.open" :title="dialog.id ? '修改命令' : '添加命令'" width="480px" class="admin-dialog instance-dialog instance-ui servers-ui" append-to-body destroy-on-close>
      <form class="instance-form" @submit.prevent="save">
        <div class="ob-fields">
          <label class="ob-field"><span>名称</span><input v-bind="noAutofill" v-model="dialog.name" maxlength="60" required aria-label="命令名称" placeholder="例如：查看磁盘" /></label>
          <label class="ob-field"><span>命令</span><textarea v-model="dialog.command" class="mono" rows="5" maxlength="8000" required aria-label="命令内容" placeholder="df -h" /></label>
        </div>
        <div class="instance-form-footer">
          <el-button v-if="dialog.id" class="ob-danger" :disabled="busy" @click="remove">删除</el-button>
          <span class="ob-grow" />
          <el-button :disabled="busy" @click="dialog.open = false">取消</el-button>
          <el-button type="primary" native-type="submit" :loading="busy">保存</el-button>
        </div>
      </form>
    </el-dialog>
  </section>
</template>
