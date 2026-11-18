<script setup>
import { computed, ref } from 'vue';
import { mesh } from './meshStore.js';
import { errorsUnder, fastProtoText, forwardLineText, forwardState, forwardTargetText, nameOf } from './meshHelpers.js';
import MeshForwardEdit from './MeshForwardEdit.vue';
import MeshFr from './MeshFr.vue';

// A node's 端口转发: the ports it takes clients on for other nodes' services, and the forwards that end on it.
const props = defineProps({member: {type: Object, required: true}, phone: Boolean, regionOf: {type: Function, default: () => ''}});
const id = computed(() => props.member.serverId);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const all = computed(() => (mesh.work?.forwards || []).map((f, i) => ({f, i})));
const out = computed(() => all.value.filter(x => x.f.entry === id.value));
const into = computed(() => all.value.filter(x => x.f.target === id.value && x.f.entry !== id.value));
const bad = i => errorsUnder(mesh.errors, `forwards[${i}]`).length > 0;
const target = computed(() => mesh.data?.nodes?.[id.value]?.summary?.forward?.target || null);
const editing = ref(null);
</script>
<template>
  <section id="m3-sec-forward" class="m3-dsec" aria-label="端口转发">
    <h3>端口转发</h3>
    <p class="m3-explain">这台当<b>入口</b>时，在公网端口上收客户端，转到另一台的服务；当<b>目标</b>时，替入口去连本机或它能访问的地址。都在「端口转发」页统一管理。</p>
    <MeshFr label="从这台转出" hint="入口防火墙自动放行这些端口；端口被别的程序（比如还在跑的 realm）占着时，应用会被拒绝。">
      <button v-for="{f, i} in out" :key="f.id" type="button" class="m3-rel" @click="editing = {id: f.id}">
        <span class="ob-grow"><b class="mono">:{{ f.listen }}</b> · {{ fastProtoText(f.proto) }} → {{ forwardTargetText(members, f) }} · {{ forwardLineText(f.line) }}
          <span v-if="bad(i)" class="m3-tag" data-kind="bad">需修改</span>
          <small class="m3-block m3-faint"><span class="ob-dot" :data-tone="forwardState(mesh.data, f)[0]" aria-hidden="true"></span> {{ forwardState(mesh.data, f)[1] }}<template v-if="f.note"> · {{ f.note }}</template></small></span>
        <span class="m3-chev">›</span></button>
      <span v-if="!out.length" class="m3-faint">没有从这台转出的端口</span>
      <div class="m3-ctl"><el-button size="small" @click="editing = {create: true, entry: id}">＋ 新建端口转发</el-button></div>
    </MeshFr>
    <MeshFr label="转到这台">
      <button v-for="{f, i} in into" :key="f.id" type="button" class="m3-rel" @click="editing = {id: f.id}">
        <span class="ob-grow">{{ nameOf(members, f.entry) }} <b class="mono">:{{ f.listen }}</b> → {{ f.host === '127.0.0.1' ? '本机' : f.host }} <span class="mono">:{{ f.port }}</span> · {{ forwardLineText(f.line) }}
          <span v-if="bad(i)" class="m3-tag" data-kind="bad">需修改</span><span v-if="!f.enabled" class="m3-tag">已停用</span></span>
        <span class="m3-chev">›</span></button>
      <span v-if="!into.length" class="m3-faint">没有转到这台的端口</span>
      <template v-if="target">
        <small v-for="e in target.errors" :key="e" class="ob-bad m3-block">隧道端口没有监听上：{{ e }}</small>
        <small class="m3-faint m3-block">当前 {{ target.active }} 个连接 · 累计 {{ target.opened }} 个<template v-if="target.refused"> · 拒绝 {{ target.refused }} 个</template><template v-if="target.failed"> · 失败 {{ target.failed }} 个</template></small>
      </template>
    </MeshFr>
    <MeshForwardEdit :at="editing" :phone="phone" :region-of="regionOf" @close="editing = null" />
  </section>
</template>
