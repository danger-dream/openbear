<script setup>
import { ref } from 'vue';
import ProcessTop from '../ProcessTop.vue';
import DdnsSheet from '../../ddns/DdnsSheet.vue';

// The processes behind a figure, ranked by it: beside the card on a desktop, in a bottom sheet on a phone.
// The slot is the card; it gets `open` to show the card pressed. Without a sort it is only the card.
const props = defineProps({server: {type: Object, required: true}, sort: {type: String, default: ''}, phone: Boolean});
const open = ref(false);
const tap = () => { if (props.sort && props.phone) open.value = true; };
</script>
<template>
  <el-popover :visible="!phone && open" :disabled="phone || !sort" trigger="click" placement="bottom-start" :width="580"
    popper-class="instance-ui servers-ui sv-top-popper" @update:visible="v => open = v">
    <template #reference><slot :open="open" :tap="tap" /></template>
    <ProcessTop v-if="!phone && open" :server="server" :sort="sort" />
  </el-popover>
  <DdnsSheet v-if="phone && sort" :model-value="open" phone title="进程占用" @update:model-value="v => open = v">
    <ProcessTop v-if="open" :server="server" :sort="sort" />
  </DdnsSheet>
</template>
