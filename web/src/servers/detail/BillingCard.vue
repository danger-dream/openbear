<script setup>
import { Wallet } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed } from 'vue';
import { billingResetDay, bytes, cycleLabel, cycleProgress, daysLeft, expiryText, expiryTone, monthly, priceText, trafficMode, trafficTypeText } from '../serverHelpers.js';

// 账单: when it runs out and how far into the cycle it is, the price and what it comes to a month, what is
// left of what was paid and a day's cost, and the traffic plan when the page has no live traffic of its own
// to show.
const props = defineProps({server: {type: Object, required: true}, now: {type: Number, required: true}, busy: Boolean});
const emit = defineEmits(['edit', 'renew']);
const billing = computed(() => props.server.profile.billing || {});
const cur = computed(() => billing.value.currency || '');
const progress = computed(() => cycleProgress(billing.value, new Date(props.now)));
const perMonth = computed(() => billing.value.cycle !== 30 ? monthly(billing.value) : 0);
const facts = computed(() => {
  const p = props.server.profile || {}, b = billing.value, t = p.traffic || {};
  const paid = b.price > 0 && b.cycle > 0, left = paid ? daysLeft(b.expiresAt, new Date(props.now)) : null;
  const worth = left === null ? '' : `${cur.value}${(b.price * Math.min(Math.max(left, 0), b.cycle) / b.cycle).toFixed(2)}`;
  return [['开始', b.cycle !== -1 ? b.startedAt : ''], ['剩余价值', worth], ['每天', paid ? `约 ${cur.value}${(b.price / b.cycle).toFixed(2)}` : ''],
    ['流量', props.server.traffic ? '' : trafficText(t, trafficTypeText(t.type))], ['商家', p.provider], ['备注', p.remark]].filter(([, v]) => v);
});
function trafficText(t, type) {
  const day = props.server.traffic?.resetDay || t.resetDay || billingResetDay(billing.value);
  const mode = trafficMode(t);
  if (mode === 'unlimited') return '不限流量';
  if (mode === 'metered') return `按流量计费 · ${cur.value}${t.price || 0}/GB · ${type} · 每月 ${day} 号结算`;
  return `${t.limit ? `${bytes(t.limit)} · ${type}` : '未设额度'} · 每月 ${day} 号重置`;
}
</script>
<template>
  <section class="ix-card sv-billing" data-part="billing">
    <header class="ix-card-head"><h3><CardIcon :icon="Wallet" tone="orange" />账单</h3><span class="ob-grow" />
      <a v-if="server.profile.providerUrl" class="ix-text-button" :href="server.profile.providerUrl" target="_blank" rel="noopener noreferrer" title="打开商家的续费页面">去续费 ↗</a>
      <button v-if="billing.cycle > 0" type="button" class="ix-text-button" :disabled="busy" title="续费后点这里，把到期日顺延一个周期" @click="emit('renew')">续费</button>
      <button type="button" class="ix-text-button" @click="emit('edit')">编辑</button>
    </header>
    <div v-if="billing.expiresAt && billing.cycle !== -1" class="sv-expiry" :data-tone="expiryTone(billing, new Date(now))">
      <div><strong>{{ expiryText(billing, new Date(now)) }}</strong><span class="instance-muted">{{ billing.expiresAt }} 到期</span></div>
      <i v-if="progress !== null" class="sv-bar" :style="{'--v': progress + '%'}" aria-hidden="true"></i>
    </div>
    <div class="sv-price">
      <strong>{{ priceText(billing) || '未填价格' }}</strong>
      <span v-if="perMonth" class="instance-muted">约 {{ cur }}{{ perMonth.toFixed(2) }}/月</span>
      <span class="ob-grow" />
      <span class="pb-tag is-plain">{{ cycleLabel(billing.cycle ?? 0) }}</span>
      <span class="pb-tag" :class="{'is-plain': !billing.autoRenew}" :data-tone="billing.autoRenew ? 'ok' : null">{{ billing.autoRenew ? '自动续费' : '手动续费' }}</span>
    </div>
    <dl class="ix-facts">
      <div v-for="[label, value] in facts" :key="label"><dt>{{ label }}</dt><dd :class="{'sv-remark': label === '备注'}">{{ value }}</dd></div>
    </dl>
  </section>
</template>
