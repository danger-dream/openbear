<script setup>
// A section's mark before its title: its icon on a soft tint of its colour (blue, success, orange, violet,
// warning, danger).
defineProps({icon: {type: [Object, Function], required: true}, tone: {type: String, default: 'blue'}});
</script>
<template>
  <span class="dt-ico" :data-tone="tone" aria-hidden="true"><el-icon><component :is="icon" /></el-icon></span>
</template>
