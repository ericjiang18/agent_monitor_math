<script setup>
import { defineAsyncComponent, onMounted, onBeforeUnmount, ref } from 'vue'
import BipartiteAtlas from './BipartiteAtlas.vue'
const SourceAtlas = defineAsyncComponent(() => import('./SourceAtlas.vue'))
const view = ref('bipartite')
function readLocation() {
  view.value = new URLSearchParams(location.search).get('view') === 'source' || /^#(?:stacks|memory)(?::|%3A)/i.test(location.hash) ? 'source' : 'bipartite'
}
function changeView(value) {
  view.value = value
  const url = new URL(location.href)
  if (value === 'source') url.searchParams.set('view', 'source')
  else url.searchParams.delete('view')
  url.hash = ''
  history.replaceState(null, '', url)
}
onMounted(() => { readLocation(); window.addEventListener('popstate', readLocation); window.addEventListener('hashchange', readLocation) })
onBeforeUnmount(() => { window.removeEventListener('popstate', readLocation); window.removeEventListener('hashchange', readLocation) })
</script>
<template>
  <nav class="atlas-view-switch" aria-label="Research atlas view"><span>EXPLORE THE ATLAS</span><div><button :aria-pressed="view === 'bipartite'" @click="changeView('bipartite')">Informal ↔ Formal</button><button :aria-pressed="view === 'source'" @click="changeView('source')">Original source DAG</button></div></nav>
  <BipartiteAtlas v-if="view === 'bipartite'" /><SourceAtlas v-else />
</template>
<style scoped>
.atlas-view-switch{max-width:1560px;margin:0 auto;padding:28px 48px 0;display:flex;align-items:center;gap:20px}.atlas-view-switch>span{font-size:9px;letter-spacing:.13em;color:var(--vp-c-text-3)}.atlas-view-switch>div{display:flex;padding:4px;border:1px solid var(--vp-c-divider);border-radius:7px;background:var(--vp-c-bg-soft)}.atlas-view-switch button{font:500 11px 'Space Grotesk Variable',sans-serif;padding:8px 14px;border-radius:4px;color:var(--vp-c-text-2);cursor:pointer}.atlas-view-switch button[aria-pressed=true]{background:var(--vp-c-bg);color:var(--vp-c-brand-1);box-shadow:0 1px 5px #17212f12}.atlas-view-switch button:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:2px}@media(max-width:639px){.atlas-view-switch{padding:22px 20px 0;gap:10px;flex-wrap:wrap}.atlas-view-switch>span{width:100%}}
</style>
