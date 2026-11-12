<script setup>
// One AS of the phone's tree (bgpTree): its line, the Tier 1s it reaches as a row, the Tier 1s met only
// behind one of those, then the upstreams it goes through, each the same way one step in.
defineOptions({name: 'BgpTreeItem'});
defineProps({item: {type: Object, required: true}, focus: {type: [Number, String], default: null}});
const emit = defineEmits(['pick']);
</script>
<template>
  <div class="bg-tree-node" :data-kind="item.origin ? 'origin' : 'transit'">
    <button type="button" :aria-pressed="focus === item.asn" @click="emit('pick', item.asn)"><b>AS{{ item.asn }}</b><span>{{ item.name }}</span></button>
    <small v-if="item.origin">源</small>
  </div>
  <div v-if="item.tier1.length" class="bg-tree-t1">
    <span>Tier 1</span>
    <button v-for="t in item.tier1" :key="t.asn" type="button" :aria-pressed="focus === t.asn" @click="emit('pick', t.asn)">{{ t.name || `AS${t.asn}` }}</button>
  </div>
  <div v-for="b in item.behind" :key="b.via.asn" class="bg-tree-t1 is-behind">
    <span>经 {{ b.via.name || `AS${b.via.asn}` }}</span>
    <button v-for="t in b.tier1" :key="t.asn" type="button" :aria-pressed="focus === t.asn" @click="emit('pick', t.asn)">{{ t.name || `AS${t.asn}` }}</button>
  </div>
  <ul v-if="item.transit.length" class="bg-tree">
    <li v-for="up in item.transit" :key="up.asn"><BgpTreeItem :item="up" :focus="focus" @pick="emit('pick', $event)" /></li>
  </ul>
</template>
