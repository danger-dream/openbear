<script setup>
import MarkdownIt from 'markdown-it';
import guide from './guide.md?raw';

const html = new MarkdownIt({html: false, linkify: true, typographer: false}).render(guide);
</script>
<template>
  <article class="sv-guide" v-html="html" />
</template>
