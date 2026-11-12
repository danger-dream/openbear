<script setup>
import { computed } from 'vue';
import { mesh } from './meshStore.js';
import { proxyPorts } from './meshHelpers.js';
import MeshNodeName from './MeshNodeName.vue';

// The exit proxy across the mesh, at a glance: the exits and the proxy ports, each opening its member's 代理 tab, where
// they are set; groups and links are on the 出口和代理 page.
const emit = defineEmits(['member']);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const problems = m => {
  const i = (mesh.work?.members || []).indexOf(m);
  return (mesh.errors || []).filter(e => e.path?.startsWith(`members[${i}].proxy`)).length;
};
const exits = computed(() => members.value.filter(m => m.proxy?.exit));
const ports = computed(() => members.value.filter(m => proxyPorts(m).length));
</script>
<template>
  <div class="mesh-proxies">
    <p v-if="!exits.length && !ports.length" class="instance-muted">还没有出口。一台节点当出口后，组网里的程序填它的 SOCKS5 链接就从那台出去；在节点的「代理」里打开。</p>
    <dl v-else class="mesh-proxy-summary">
      <div>
        <dt>出口</dt>
        <dd>
          <button v-for="m in exits" :key="m.serverId" type="button" class="ix-text-button mesh-proxy-on" :aria-label="`打开 ${m.name} 的代理设置`" @click="emit('member', m.serverId)">
            <MeshNodeName :id="m.serverId" plain /><small class="mono">{{ m.proxy.exit.port }}</small><span v-if="problems(m)" class="pb-tag" data-tone="bad">需修改</span>
          </button>
          <span v-if="!exits.length" class="instance-muted">还没有</span>
        </dd>
      </div>
      <div>
        <dt>端口</dt>
        <dd>
          <button v-for="m in ports" :key="m.serverId" type="button" class="ix-text-button mesh-proxy-on" :aria-label="`打开 ${m.name} 的代理设置`" @click="emit('member', m.serverId)">
            <MeshNodeName :id="m.serverId" plain /><small class="mono">{{ proxyPorts(m).map(p => p.port).join(' ') }}</small><span v-if="!m.proxy.exit && problems(m)" class="pb-tag" data-tone="bad">需修改</span>
          </button>
          <span v-if="!ports.length" class="instance-muted">还没有</span>
        </dd>
      </div>
    </dl>
  </div>
</template>
