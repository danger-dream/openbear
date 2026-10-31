<script setup>
import { computed, nextTick, ref, useId } from 'vue';
import FloatingPanel from '../references/FloatingPanel.vue';
import { flag, noAutofill, regionName } from './serverHelpers.js';

// ISO 3166-1 alpha-2; browsers can name regions but cannot list them.
const CODES = ('AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ '
  + 'DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP '
  + 'KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ '
  + 'OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ '
  + 'UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW').split(' ');
const english = typeof Intl !== 'undefined' && Intl.DisplayNames ? new Intl.DisplayNames(['en'], {type: 'region'}) : null;
const collator = typeof Intl !== 'undefined' ? new Intl.Collator('zh-CN') : null;
const ALL = CODES.map(code => ({code, name: regionName(code), en: (() => { try { return english?.of(code) || ''; } catch { return ''; } })()}))
  .sort((a, b) => collator ? collator.compare(a.name, b.name) : a.name.localeCompare(b.name));
const BY_CODE = Object.fromEntries(ALL.map(r => [r.code, r]));

// modelValue '' means "detect from the server's IP".
const props = defineProps({modelValue: {type: String, default: ''}, detected: {type: String, default: ''}, used: {type: Array, default: () => []}});
const emit = defineEmits(['update:modelValue']);
const button = ref(null), search = ref(null), floating = ref(null), open = ref(false), query = ref(''), active = ref(0);
const listId = `ob-region-${useId()}`;

const current = computed(() => (props.modelValue || '').toUpperCase());
const shown = computed(() => current.value || (props.detected || '').toUpperCase());
const label = computed(() => current.value ? `地区：${regionName(current.value)}` : shown.value ? `地区：${regionName(shown.value)}（自动识别）` : '地区：自动识别');
// Rows as rendered: auto first, then regions in use, then everything; a search is one flat list.
const rows = computed(() => {
  const q = query.value.trim().toLowerCase();
  if (q) return ALL.filter(r => r.code.toLowerCase() === q || r.name.toLowerCase().includes(q) || r.en.toLowerCase().includes(q) || r.code.toLowerCase().startsWith(q))
    .sort((a, b) => (b.code.toLowerCase() === q) - (a.code.toLowerCase() === q)).map(r => ({...r, kind: 'region'}));
  const used = [...new Set(props.used.map(c => String(c || '').toUpperCase()))].filter(c => BY_CODE[c]);
  return [{kind: 'auto', code: ''},
    ...(used.length ? [{kind: 'caption', code: '_used', name: '你的服务器'}, ...used.map(c => ({...BY_CODE[c], kind: 'region', key: `u-${c}`}))] : []),
    {kind: 'caption', code: '_all', name: '全部地区'}, ...ALL.map(r => ({...r, kind: 'region'}))];
});
const choices = computed(() => rows.value.filter(r => r.kind !== 'caption'));

async function toggle() {
  if (open.value) { close(); return; }
  query.value = '';
  open.value = true;
  active.value = Math.max(0, choices.value.findIndex(r => r.code === current.value));
  // The panel stays hidden until positioned, and hidden inputs cannot take focus.
  await nextTick();
  await floating.value?.position();
  await nextTick();
  if (!open.value) return;
  search.value?.focus({preventScroll: true});
  scrollActive();
}
function close(refocus = false) { open.value = false; if (refocus) button.value?.focus(); }
function pick(code) { emit('update:modelValue', code); close(true); }
function scrollActive() { nextTick(() => document.getElementById(`${listId}-${active.value}`)?.scrollIntoView({block: 'nearest'})); }
function key(event) {
  if (event.isComposing || event.keyCode === 229) return;
  const n = choices.value.length;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (n) { active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + n) % n; scrollActive(); }
  } else if (event.key === 'Enter') {
    event.preventDefault();
    if (choices.value[active.value]) pick(choices.value[active.value].code);
  } else if (event.key === 'Escape') {
    // Only the picker closes, not the dialog around it.
    event.preventDefault();
    event.stopPropagation();
    close(true);
  }
}
function typed() { active.value = 0; scrollActive(); }
const indexOf = row => choices.value.indexOf(row);
</script>
<template>
  <button ref="button" type="button" class="rp-button" :data-auto="!current || undefined" :aria-label="label" :title="label"
    aria-haspopup="listbox" :aria-expanded="open" @click="toggle">
    <span v-if="shown" class="sv-flag" aria-hidden="true">{{ flag(shown) }}</span>
    <svg v-else viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></svg>
  </button>
  <FloatingPanel ref="floating" :open="open" :anchor="button" placement="bottom-end" :width="264" label="选择地区" @close="close()">
    <div class="rp-panel">
      <input ref="search" v-model="query" v-bind="noAutofill" type="search" class="rp-search" placeholder="搜索名称或代码，如 日本 / JP" aria-label="搜索地区"
        role="combobox" aria-autocomplete="list" :aria-controls="listId" :aria-activedescendant="choices[active] ? `${listId}-${active}` : undefined" @input="typed" @keydown="key" />
      <ul :id="listId" class="rp-list" role="listbox" aria-label="地区">
        <template v-for="row in rows" :key="row.key || row.code">
          <li v-if="row.kind === 'caption'" class="rp-caption" role="presentation">{{ row.name }}</li>
          <li v-else :id="`${listId}-${indexOf(row)}`" role="option" class="rp-option" :class="{'is-active': indexOf(row) === active}"
            :aria-selected="row.code === current" @pointerdown.prevent="pick(row.code)" @pointerenter="active = indexOf(row)">
            <template v-if="row.kind === 'auto'">
              <span class="rp-flag sv-flag" aria-hidden="true">{{ flag(detected) || '🌐' }}</span>
              <span class="rp-name">自动识别</span>
              <span class="rp-code">{{ detected ? regionName(detected) : '按 IP' }}</span>
            </template>
            <template v-else>
              <span class="rp-flag sv-flag" aria-hidden="true">{{ flag(row.code) }}</span>
              <span class="rp-name">{{ row.name }}</span>
              <span class="rp-code">{{ row.code }}</span>
            </template>
          </li>
        </template>
        <li v-if="!choices.length" class="rp-empty" role="presentation">没有匹配的地区</li>
      </ul>
    </div>
  </FloatingPanel>
</template>
