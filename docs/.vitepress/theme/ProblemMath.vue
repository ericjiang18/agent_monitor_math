<script setup>
import { computed } from 'vue'
import katex from './vendor/katex/katex.mjs'
import './vendor/katex/katex.min.css'
const props = defineProps({ text: { type: String, default: '' } })
const escape = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
const rendered = computed(() => {
  const input = props.text
  const pattern = /(?<!\\)\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$])\$(?!\$)((?:\\.|[^$\n])+?)\$/g
  let result = '', position = 0
  for (const match of input.matchAll(pattern)) {
    result += escape(input.slice(position, match.index))
    result += katex.renderToString(match[1] || match[2] || match[3] || match[4], { displayMode: Boolean(match[1] || match[2]), throwOnError: false, trust: false, strict: false })
    position = match.index + match[0].length
  }
  return result + escape(input.slice(position))
})
</script>

<template><div class="problem-math" v-html="rendered"></div></template>

<style scoped>
.problem-math { line-height: 1.85; white-space: pre-line; overflow-wrap: anywhere; }
.problem-math :deep(.katex) { white-space: normal; font-size: 1.08em; }
.problem-math :deep(.katex-display) { overflow-x: auto; overflow-y: hidden; padding: 6px 0; max-width: 100%; }
</style>
