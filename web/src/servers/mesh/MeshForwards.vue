<script setup>
import { computed, ref, watch } from 'vue';
import { mesh } from './meshStore.js';
import { errorsUnder, fastProtoText, forwardAllowText, forwardLineText, forwardState, forwardTargetText, nameOf, nodeState, rowButton } from './meshHelpers.js';
import MeshForwardEdit from './MeshForwardEdit.vue';
import MeshNodeName from './MeshNodeName.vue';

// Port forwards: a port on an entry node whose clients go to an address its target reaches, over the mesh or the fast relay.
const props = defineProps({phone: Boolean, items: {type: Array, default: () => []}, forward: {type: String, default: ''}, create: {type: Object, default: null}});
const work = computed(() => mesh.work);
const members = computed(() => (work.value?.members || []).filter(m => !m.removing));
const regionOf = id => String(members.value.find(m => m.serverId === id)?.region || props.items.find(i => i.id === id)?.region || '').toUpperCase();
const forwards = computed(() => work.value?.forwards || []);
const editing = ref(null);
watch(() => props.forward, id => { if (id) editing.value = {id}; }, {immediate: true});
watch(() => props.create, c => { if (c?.kind === 'forward') editing.value = {create: true}; }, {immediate: true});
const errorsOf = i => errorsUnder(mesh.errors, `forwards[${i}]`).length;
const stateOf = f => forwardState(mesh.data, f);
</script>
<template>
  <section class="m3-pane" aria-label="端口转发">
    <header class="m3-pane-head"><h2>端口转发</h2><span class="ob-grow" />
      <el-button size="small" type="primary" :disabled="members.length < 1" @click="editing = {create: true}">＋ 新建端口转发</el-button></header>
    <p class="m3-lead">在<b>入口节点</b>上开一个端口，连进来的客户端被转到<b>目标节点</b>能访问的地址，默认是目标自己的 127.0.0.1。
      可以选走<b>组网</b>（加密，更安全）或<b>快速通道</b>（更快），线路由组网自动选、自动换。专线入口机上不用再跑 realm。</p>
    <ul class="m3-rows m3-rows-forwards" :class="{'is-phone': phone}">
      <li v-if="!phone && forwards.length" aria-hidden="true"><div class="m3-row m3-row-head"><span>入口</span><span>端口</span><span>转到</span><span>线路</span><span>来源</span><span>状态</span></div></li>
      <li v-for="(f, i) in forwards" :key="f.id" :data-off="f.enabled ? undefined : ''">
        <div class="m3-row m3-row-click" :aria-label="`打开 ${nameOf(members, f.entry)} :${f.listen} 的端口转发`" v-bind="rowButton(() => { editing = {id: f.id}; })">
          <span class="m3-row-name"><span class="ob-dot" :data-tone="nodeState(mesh.data, f.entry)[0]" aria-hidden="true"></span><strong><MeshNodeName :id="f.entry" plain /></strong>
            <span v-if="errorsOf(i)" class="m3-tag" data-kind="bad">需修改</span><span v-if="!f.enabled" class="m3-tag">草稿停用</span></span>
          <span class="m3-faint"><span class="mono">:{{ f.listen }}</span> · {{ fastProtoText(f.proto) }}</span>
          <span class="m3-row-conn">{{ forwardTargetText(members, f) }}<template v-if="f.note"><span class="m3-faint"> · {{ f.note }}</span></template></span>
          <span class="m3-faint">{{ forwardLineText(f.line) }}<template v-if="f.proxyProtocol"> · PROXY v{{ f.proxyProtocol }}</template></span>
          <span class="m3-faint m3-fwd-allow">{{ forwardAllowText(f.allow) }}</span>
          <span class="m3-faint m3-fwd-run"><span class="ob-dot" :data-tone="stateOf(f)[0]" aria-hidden="true"></span> {{ stateOf(f)[1] }}</span>
        </div>
      </li>
      <li v-if="!forwards.length" class="instance-muted">还没有端口转发。比如入口选 HyperPods-广东、端口 443，目标选香港那台、目标端口 443：之后连广东的 443 就到了香港本机的 443。</li>
    </ul>
    <MeshForwardEdit :at="editing" :phone="phone" :region-of="regionOf" @close="editing = null" />
  </section>
</template>
