<script setup>
import CardIcon from './CardIcon.vue';
import { computed, ref, watch } from 'vue';
import { Close, Compass } from '@element-plus/icons-vue';
import { Api } from '../../api.js';
import { ok, relativeTime } from '../../instances/instanceHelpers.js';
import { safeError } from '../serverHelpers.js';
import { useVisible } from './useVisible.js';
import BgpGraph from './BgpGraph.vue';

// BGP 上游: how the world reaches the network the server's address is in (RIPE RIS, via the controller),
// read once the card is scrolled to; IPv4 or IPv6, and 刷新 to ask RIPE again. The same body serves the
// card's popover (bare: no card around it).
const props = defineProps({server: {type: Object, required: true}, active: {type: Boolean, default: true}, phone: Boolean, now: {type: Number, default: () => Date.now()},
  bare: Boolean, heading: {type: String, default: 'BGP 上游'}, closable: Boolean});
const emit = defineEmits(['close']);
const el = ref(null), family = ref(4), data = ref(null), error = ref(''), refreshing = ref(false);
const {seen} = props.bare ? {seen: ref(true)} : useVisible(el);
let mine = 0;

async function load(fresh = false) {
  const n = ++mine;
  if (fresh) refreshing.value = true;
  try {
    const result = ok(fresh ? await Api.refreshServerBgp(props.server.id, family.value) : await Api.serverBgp(props.server.id, family.value));
    if (n === mine) {
      data.value = result; error.value = '';
      // IPv6-only nodes still have an upstream even though the first request asks for IPv4.
      const available = (result.families || []).filter(f => f === 4 || f === 6);
      if (available.length && !available.includes(family.value)) family.value = available[0];
    }
  } catch (e) { if (n === mine) error.value = safeError(e); }
  finally { if (fresh) refreshing.value = false; }
}
watch(() => seen.value && props.active && `${props.server.id}/${family.value}`, on => { if (on) { data.value = null; void load(); } }, {immediate: true});

const families = computed(() => data.value?.families || []);
const origin = computed(() => {
  const d = data.value;
  if (!d?.origins?.length) return '';
  return d.origins.map(a => `AS${a}${d.originNames?.[a] ? ` ${d.originNames[a]}` : ''}`).join('、');
});
const said = computed(() => {
  const d = data.value;
  if (!d) return '';
  return {no_address: `这台服务器没有公网 IPv${d.family} 地址。`,
    not_routed: `${d.ip} 所在的网段没有在 BGP 里宣告，可能是商家内网或 NAT 后面的地址。`,
    no_transit: `RIPE 观测点看到的 ${d.prefix} 的路径都没有经过 Tier 1，没有可画的上游；这不代表它只有直连对等，只是这些观测点没看到。`,
    unavailable: `暂时取不到 RIPE 的数据：${d.error}`}[d.state] || '';
});
</script>
<template>
  <component :is="bare ? 'div' : 'section'" ref="el" :class="bare ? 'bg-bare' : ['ix-card', 'dt-section', {'is-waiting': !data && !error}]" :data-jump="bare ? null : 'bgp'" aria-label="BGP 上游">
    <header class="ix-card-head">
      <h3 v-if="heading"><CardIcon v-if="!bare" :icon="Compass" tone="violet" />{{ heading }}</h3>
      <span v-if="data?.prefix" class="instance-muted bg-head-note">{{ data.prefix }}<template v-if="origin"> · {{ origin }}</template></span>
      <span class="ob-grow" />
      <div v-if="families.length" class="sv-ranges" role="tablist" aria-label="地址">
        <button v-for="f in families" :key="f" type="button" role="tab" :aria-selected="family === f" @click="family = f">IPv{{ f }}</button>
      </div>
      <button v-if="data && data.state !== 'no_address'" type="button" class="ix-text-button" :disabled="refreshing" @click="load(true)">{{ refreshing ? '刷新中…' : '刷新' }}</button>
      <button v-if="closable" type="button" class="bg-close" aria-label="关闭" title="关闭" @click="emit('close')"><el-icon><Close /></el-icon></button>
    </header>
    <p v-if="error" class="instance-error">{{ error }}</p>
    <p v-else-if="!data" class="instance-muted dt-wait" role="status">正在读取…</p>
    <p v-else-if="said" class="dt-empty">{{ said }}</p>
    <template v-else>
      <BgpGraph :data="data" :phone="phone">
        <span v-if="data.error">这是 {{ relativeTime(data.atMs, now) }}取到的结果，刚才没能更新：{{ data.error }}</span>
        <span v-else>RIPE RIS · {{ relativeTime(data.atMs, now) }}取得 · 只画经过 Tier 1 的路径</span>
        <span>{{ phone ? '点一个 AS 看它从哪里来' : '悬停或点一个 AS 看经过它的路径，点 AS 号打开 bgp.tools' }}</span>
      </BgpGraph>
    </template>
  </component>
</template>
