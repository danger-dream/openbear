<script setup>
import { onBeforeUnmount, ref } from 'vue';
import draggable from 'vuedraggable';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import { dragEdgeScroll } from './dragEdgeScroll.js';
import { flag, safeError } from './serverHelpers.js';

// Installed and uninstalled servers are ordered separately; the board always
// shows the uninstalled ones last.
const props = defineProps({items: {type: Array, required: true}});
const emit = defineEmits(['saved', 'cancel']);
const installed = ref(props.items.filter(i => i.node)), pending = ref(props.items.filter(i => !i.node));
const busy = ref(false), error = ref('');
defineExpose({busy});

function move(list, index, direction) {
  const items = [...list.value], target = index + direction;
  if (target < 0 || target >= items.length) return;
  [items[index], items[target]] = [items[target], items[index]];
  list.value = items;
}
async function save() {
  if (busy.value) return;
  error.value = ''; busy.value = true;
  try { emit('saved', ok(await Api.orderServers([...installed.value, ...pending.value].map(i => i.id))).items); }
  catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
const sections = [['已安装节点', installed], ['未安装节点', pending]];
let stopEdgeScroll = () => {};
function startScroll(event) { stopEdgeScroll(); stopEdgeScroll = dragEdgeScroll(event.from); }
function stopScroll() { stopEdgeScroll(); stopEdgeScroll = () => {}; }
onBeforeUnmount(stopScroll);
</script>
<template>
  <div class="so">
    <p class="instance-muted so-hint">拖动左侧把手或用上下箭头调整。探针页的默认排序按这个顺序，未安装节点的服务器始终排在最后。</p>
    <template v-for="[title, list] in sections" :key="title">
      <section v-if="list.value.length" class="so-section" :aria-label="title">
        <h4>{{ title }}<span class="instance-muted">{{ list.value.length }}</span></h4>
        <draggable v-model="list.value" item-key="id" handle=".so-grip" :animation="160" ghost-class="so-ghost" class="so-list" :disabled="busy"
          :scroll="false" @start="startScroll" @end="stopScroll">
          <template #item="{element, index}">
            <div class="so-item">
              <span class="so-grip" aria-hidden="true" title="拖动调整顺序"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg></span>
              <span class="so-number">{{ index + 1 }}</span>
              <span class="sv-flag" aria-hidden="true">{{ flag(element.region) }}</span>
              <span class="so-name" :title="element.name">{{ element.name }}</span>
              <span v-if="element.profile.group" class="so-group instance-muted">{{ element.profile.group }}</span>
              <div class="so-actions">
                <button type="button" :disabled="busy || index === 0" :aria-label="`上移 ${element.name}`" title="上移" @click="move(list, index, -1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 14 6-6 6 6"/></svg></button>
                <button type="button" :disabled="busy || index === list.value.length - 1" :aria-label="`下移 ${element.name}`" title="下移" @click="move(list, index, 1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 10 6 6 6-6"/></svg></button>
              </div>
            </div>
          </template>
        </draggable>
      </section>
    </template>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div class="instance-form-footer">
      <el-button :disabled="busy" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="busy" @click="save">保存</el-button>
    </div>
  </div>
</template>
