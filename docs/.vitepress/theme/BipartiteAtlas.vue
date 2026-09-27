<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useData, withBase } from 'vitepress'
import { boundsFor, hitNode } from './informalGraph.js'
import { prepareBipartite, drawBipartite, attachDependencies } from './bipartiteGraph.js'
import summary from './bipartiteSummary.json'
const { isDark } = useData()
const data = shallowRef(null), graph = shallowRef(null), loading = ref(true), error = ref('')
const project = ref(''), query = ref(''), includeUnresolved = ref(false), listView = ref(false), layer = ref('')
const showDependencies = ref(false), dependencyData = shallowRef(null), dependencyState = ref('idle'), dependencyLimit = ref(8)
const selectedId = ref(''), selectedDetail = shallowRef(null), detailState = ref(''), connectionLimit = ref(8)
const viewport = ref(null), canvas = ref(null), hovered = shallowRef(null), zoom = ref(10), page = ref(1)
const camera = { x: 0, y: 0, scale: .1 }, pointers = new Map(), detailCache = new Map()
let dimensions = { width: 800, height: 640, ratio: 1 }, observer, frame = 0, disposed = false, controller, drag, pinch
const count = value => Number(value || 0).toLocaleString('en-US')
const stats = computed(() => data.value?.stats || summary.stats)
const projects = computed(() => data.value?.projects || [])
const selected = computed(() => graph.value?.byId.get(selectedId.value))
const selectedProject = computed(() => projects.value.find(item => item.id === selected.value?.project))
const connections = computed(() => graph.value?.adjacency.get(selectedId.value) || [])
const dependencies = computed(() => showDependencies.value ? graph.value?.dependencyAdjacency.get(selectedId.value) || [] : [])
const pendingCount = computed(() => data.value?.edges.filter(edge => (edge.source === selectedId.value || edge.target === selectedId.value) && !['resolved', 'resolved_mathlib'].includes(edge.status)).length || 0)
const matches = computed(() => {
  const term = query.value.trim().toLowerCase()
  return graph.value?.nodes.filter(node => (!layer.value || node.layer === layer.value) && (!term || `${node.title} ${node.name || ''} ${node.summary} ${node.module || ''} ${(node.labels || []).join(' ')} ${node.kind}`.toLowerCase().includes(term))) || []
})
const matchIds = computed(() => query.value.trim() || layer.value ? new Set(matches.value.map(node => node.id)) : null)
const pages = computed(() => Math.max(1, Math.ceil(matches.value.length / 30)))
const pageNodes = computed(() => matches.value.slice((page.value - 1) * 30, page.value * 30))
const layerName = node => node?.layer === 'formal' ? 'Lean declaration' : node?.layer === 'unresolved' ? 'Unresolved Lean reference' : 'Informal mathematics'
const statusName = status => ({ resolved: 'Resolved in project', resolved_mathlib: 'Resolved in Mathlib', unresolved: 'Unresolved reference', ambiguous: 'Ambiguous reference' }[status] || status)
const sourceText = computed(() => selectedDetail.value?.content_full || selectedDetail.value?.content || selectedDetail.value?.text || '')
const readableStatement = computed(() => sourceText.value.replace(/\\(?:begin|end)\{[^}]*\}(?:\[[^\]]*\])?/g, '').replace(/\\(?:label|lean|uses|proves)\{[^}]*\}/g, '').replace(/\\(?:leanok|mathlibok|notready)\b/g, '').trim())
const sourceLink = computed(() => {
  const value = selected.value?.layer === 'informal' ? selectedProject.value?.blueprint_url : selectedProject.value?.repository_url
  return typeof value === 'string' && value.startsWith('https://github.com/') ? value : ''
})
function scheduleDraw() {
  if (frame || !canvas.value || !graph.value) return
  frame = requestAnimationFrame(() => { frame = 0; if (canvas.value && graph.value) drawBipartite(canvas.value, graph.value, camera, { ...dimensions, dark: isDark.value, selected: selectedId.value, matches: matchIds.value, showDependencies: showDependencies.value && dependencyState.value === 'ready' }) })
}
function resize() {
  if (!viewport.value || !canvas.value) return
  const width = viewport.value.clientWidth, height = viewport.value.clientHeight
  if (!width || !height) return
  camera.x += (width - dimensions.width) / 2; camera.y += (height - dimensions.height) / 2
  dimensions = { width, height, ratio: Math.min(window.devicePixelRatio || 1, 2) }
  canvas.value.width = Math.round(width * dimensions.ratio); canvas.value.height = Math.round(height * dimensions.ratio); scheduleDraw()
}
async function connectCanvas(fit = false) {
  await nextTick(); observer?.disconnect()
  if (viewport.value) { observer = new ResizeObserver(resize); observer.observe(viewport.value) }
  resize(); if (fit) fitGraph(); else scheduleDraw()
}
function fitNodes(nodes) {
  if (!nodes?.length) return
  const bounds = boundsFor(nodes)
  camera.scale = Math.max(.015, Math.min(2, (dimensions.width - 90) / Math.max(150, bounds.maxX - bounds.minX), (dimensions.height - 140) / Math.max(150, bounds.maxY - bounds.minY)))
  camera.x = dimensions.width / 2 - (bounds.minX + bounds.maxX) / 2 * camera.scale
  camera.y = dimensions.height / 2 - (bounds.minY + bounds.maxY) / 2 * camera.scale
  zoom.value = Math.round(camera.scale * 100); hovered.value = null; scheduleDraw()
}
function fitGraph() { fitNodes(matchIds.value ? matches.value : graph.value?.nodes) }
function fitConnections() { if (selected.value) fitNodes([selected.value, ...connections.value.map(item => item.node), ...dependencies.value.map(item => item.node)]) }
function fitDependencies() { if (selected.value) fitNodes([selected.value, ...dependencies.value.map(item => item.node)]) }
function setZoom(scale, x = dimensions.width / 2, y = dimensions.height / 2) {
  const next = Math.max(.015, Math.min(5, scale))
  camera.x = x - (x - camera.x) * next / camera.scale; camera.y = y - (y - camera.y) * next / camera.scale
  camera.scale = next; zoom.value = Math.round(next * 100); hovered.value = null; scheduleDraw()
}
async function loadDetail(node) {
  selectedDetail.value = null; detailState.value = 'loading'
  try {
    if (!detailCache.has(node.detail_key)) {
      const response = await fetch(withBase(`/research/bipartite-details/${encodeURIComponent(node.detail_key)}.json`), { signal: controller.signal })
      if (!response.ok) throw new Error('Source details could not be loaded.')
      detailCache.set(node.detail_key, await response.json())
    }
    if (disposed || selectedId.value !== node.id) return
    const detail = detailCache.get(node.detail_key)[node.id]
    if (!detail) throw new Error('Source details are missing.')
    selectedDetail.value = detail; detailState.value = 'ready'
  } catch { if (!disposed && selectedId.value === node.id) detailState.value = 'error' }
}
async function loadDependencies() {
  if (dependencyState.value === 'loading' || dependencyState.value === 'ready') return
  dependencyState.value = 'loading'
  try {
    const response = await fetch(withBase('/research/bipartite-dependencies.json'), { signal: controller.signal })
    if (!response.ok) throw new Error('Dependencies could not be loaded.')
    const result = await response.json()
    if (!Array.isArray(result.edges)) throw new Error('Dependency data is incomplete.')
    if (disposed) return
    dependencyData.value = result
    if (graph.value) graph.value = attachDependencies(graph.value, result.edges)
    dependencyState.value = 'ready'; scheduleDraw()
  } catch { if (!disposed) dependencyState.value = 'error' }
}
function choose(id, fit = false) {
  const node = graph.value?.byId.get(id)
  if (!node) return
  if (matchIds.value && !matchIds.value.has(id)) { query.value = ''; layer.value = '' }
  selectedId.value = id; connectionLimit.value = 8; dependencyLimit.value = 8; hovered.value = null
  history.replaceState(null, '', location.pathname + location.search + '#bridge:' + encodeURIComponent(id))
  loadDetail(node); if (fit) fitConnections(); else scheduleDraw()
}
function searchFirst() { if (matches.value.length) choose(matches.value[0].id, true) }
async function followLocation() {
  if (!data.value || !location.hash.startsWith('#bridge:')) return
  let id = ''; try { id = decodeURIComponent(location.hash.slice(8)) } catch { return }
  const node = data.value.nodes.find(item => item.id === id)
  if (!node || selectedId.value === id) return
  if (node.layer === 'unresolved') includeUnresolved.value = true
  if (!graph.value?.byId.has(id)) project.value = ''
  await nextTick()
  if (!disposed) choose(id, true)
}
function rebuild() {
  if (!data.value) return
  graph.value = prepareBipartite(data.value, { project: project.value, includeUnresolved: includeUnresolved.value })
  if (dependencyData.value) graph.value = attachDependencies(graph.value, dependencyData.value.edges)
  if (!graph.value.byId.has(selectedId.value)) {
    const node = graph.value.nodes.find(item => item.layer === 'informal' && graph.value.adjacency.get(item.id)?.length)
    if (node) choose(node.id); else { selectedId.value = ''; selectedDetail.value = null }
  }
  if (!includeUnresolved.value && layer.value === 'unresolved') layer.value = ''
  page.value = 1; hovered.value = null; nextTick(() => connectCanvas(true))
}
async function loadAtlas() {
  loading.value = true; error.value = ''
  try {
    const response = await fetch(withBase('/research/bipartite-atlas.json'), { signal: controller.signal })
    if (!response.ok) throw new Error('The mathematical atlas could not be loaded. Please try again.')
    const result = await response.json()
    if (!Array.isArray(result.nodes) || !result.nodes.length || !Array.isArray(result.edges)) throw new Error('The atlas data is incomplete.')
    if (disposed) return
    data.value = result
    let hash = ''; try { hash = decodeURIComponent(location.hash.replace(/^#bridge:/, '')) } catch {}
    if (data.value.nodes.some(node => node.id === hash && node.layer === 'unresolved')) includeUnresolved.value = true
    selectedId.value = hash; rebuild(); loading.value = false
    await connectCanvas(true)
    if (graph.value.byId.has(hash)) choose(hash, true)
  } catch (cause) { if (!disposed) { error.value = cause.message; loading.value = false } }
}
function point(event) { const box = canvas.value.getBoundingClientRect(); return { x: event.clientX - box.left, y: event.clientY - box.top } }
function hit(at) { return hitNode(matchIds.value ? { nodes: matches.value } : graph.value, camera, at.x, at.y) }
function startPan(event) {
  if (event.button !== 0 && event.pointerType !== 'touch') return
  const at = point(event); pointers.set(event.pointerId, at); canvas.value.setPointerCapture(event.pointerId)
  drag = { pointer: event.pointerId, start: at, last: at, moved: false }
  if (pointers.size === 2) { const [a,b] = [...pointers.values()]; pinch = { distance: Math.hypot(a.x-b.x,a.y-b.y), x:(a.x+b.x)/2, y:(a.y+b.y)/2 }; drag.moved = true }
  hovered.value = null
}
function pan(event) {
  if (!graph.value) return
  const at = point(event)
  if (!pointers.has(event.pointerId)) {
    const node = hit(at); hovered.value = node ? { node, x: Math.max(8, Math.min(at.x + 12, dimensions.width - 235)), y: Math.max(60, Math.min(at.y - 20, dimensions.height - 85)) } : null; return
  }
  pointers.set(event.pointerId, at)
  if (pinch && pointers.size === 2) {
    const [a,b] = [...pointers.values()], distance = Math.hypot(a.x-b.x,a.y-b.y), x=(a.x+b.x)/2, y=(a.y+b.y)/2
    setZoom(camera.scale * distance / Math.max(1,pinch.distance),x,y)
    camera.x += x-pinch.x; camera.y += y-pinch.y; pinch = {distance,x,y}; if(drag) drag.moved=true
  } else if (drag?.pointer === event.pointerId) {
    camera.x += at.x-drag.last.x; camera.y += at.y-drag.last.y
    if (Math.hypot(at.x-drag.start.x,at.y-drag.start.y)>5) drag.moved=true
    drag.last=at
  }
  scheduleDraw()
}
function stopPan(event) {
  if (event && drag && !drag.moved && !pinch && event.type !== 'pointercancel') { const node = hit(point(event)); if (node) choose(node.id) }
  if (event) pointers.delete(event.pointerId); else pointers.clear(); drag = null; pinch = null
}
function wheel(event) { event.preventDefault(); const at = point(event); setZoom(camera.scale*Math.exp(-event.deltaY*.0017),at.x,at.y) }
function keyboard(event) {
  const moves = { ArrowLeft:[60,0],ArrowRight:[-60,0],ArrowUp:[0,60],ArrowDown:[0,-60] }
  if (moves[event.key]) { event.preventDefault(); camera.x+=moves[event.key][0];camera.y+=moves[event.key][1];scheduleDraw() }
  if (['+','=','-','0'].includes(event.key)) { event.preventDefault(); if(event.key==='0') fitGraph();else setZoom(camera.scale*(event.key==='-'?1/1.3:1.3)) }
}
watch([project, includeUnresolved], rebuild)
watch(showDependencies, enabled => { if (enabled) loadDependencies(); scheduleDraw() })
watch([query,layer], () => { page.value=1;hovered.value=null;scheduleDraw() })
watch(isDark, scheduleDraw)
watch(listView, () => { if (!listView.value) connectCanvas() })
onMounted(() => { controller = new AbortController(); loadAtlas(); window.addEventListener('hashchange', followLocation); window.addEventListener('popstate', followLocation) })
onBeforeUnmount(() => { disposed=true;controller?.abort();observer?.disconnect();cancelAnimationFrame(frame);stopPan(); window.removeEventListener('hashchange', followLocation); window.removeEventListener('popstate', followLocation) })
</script>

<template>
  <main class="bipartite-atlas">
    <header class="bridge-header">
      <div><p class="bridge-eyebrow"><i></i> THE RESEARCH ATLAS <span>VOL. 04 / TWO LANGUAGES</span></p><h1>Mathematical ideas.<br><em>Formal connections.</em></h1><p class="bridge-description">From a statement on the page to a declaration in Lean. Explore two sides of the same mathematical landscape, connected by the references in each project’s blueprint.</p></div>
      <div class="bridge-stats" aria-label="Bipartite atlas statistics"><div><strong>{{ count(stats.informal) }}</strong><span>informal entries</span></div><div><strong>{{ count(stats.formal) }}</strong><span>Lean declarations</span></div><div><strong>{{ count(stats.resolved_links) }}</strong><span>resolved connections</span></div></div>
    </header>
    <div class="bridge-intro"><span><b>{{ count(stats.projects) }} PROJECTS</b> · One view of informal and formal mathematics.</span><a :href="withBase('/dag-sources#informal-formal-bridge')">About these connections ↗</a></div>
    <section class="bridge-workbench" :aria-label="showDependencies ? 'Mathematical graph with dependencies' : 'Bipartite mathematical graph'">
      <div class="bridge-toolbar"><div class="bridge-map-title"><i></i><strong>Informal ↔ Formal</strong></div><div class="bridge-controls"><label class="bridge-search"><span aria-hidden="true">⌕</span><input v-model="query" type="search" aria-label="Search mathematical statements and Lean declarations" placeholder="Search statements, Lean names…" @keydown.enter="searchFirst"></label><select v-model="project" aria-label="Filter project"><option value="">All {{ stats.projects }} projects</option><option v-for="item in projects" :key="item.id" :value="item.id">{{ item.title }}</option></select><button class="bridge-view-button" :aria-pressed="listView" @click="listView=!listView">{{ listView ? '◇ Map' : '☷ List' }}</button></div></div>
      <div class="bridge-options"><span><i class="informal-symbol"></i> Informal <span class="bridge-rule"></span><i class="formal-symbol"></i> Formal</span><div class="bridge-option-toggles"><label><input v-model="showDependencies" type="checkbox" :disabled="loading || !!error"> Show dependencies <span class="bridge-pending-count">{{ count(stats.dependencies) }}</span></label><label><input v-model="includeUnresolved" type="checkbox"> Show unresolved references <span class="bridge-pending-count">{{ count(stats.unresolved_links) }}</span></label></div></div>
      <div v-if="showDependencies" class="bridge-dependency-guide" role="status"><span v-if="dependencyState==='loading'">Loading informal references and proof links…</span><span v-else-if="dependencyState==='error'">Dependencies could not be loaded. <button @click="loadDependencies">Retry dependencies</button></span><template v-else-if="dependencyState==='ready'"><span><i class="dependency-line"></i> Reference <i class="dependency-line proof"></i> Proof link</span><span>Arrows: citing entry → reference; proof → statement. Select an informal point to highlight its links.</span></template></div>
      <div v-if="loading || error" class="bridge-loading" role="status"><span>✧</span><h2>{{ loading ? 'Connecting the mathematical landscape…' : 'The atlas needs another moment.' }}</h2><p>{{ error || 'Loading statements, declarations, and their blueprint connections.' }}</p><button v-if="error" @click="loadAtlas">Reload atlas</button></div>
      <div v-else-if="graph" class="bridge-explorer">
        <div class="bridge-map-column">
          <div v-if="query || layer" class="bridge-search-summary" role="status"><span>{{ count(matches.length) }} matching entries</span><button v-if="matches.length" @click="searchFirst">Go to first →</button><button @click="query='';layer=''">Clear filters</button></div>
          <template v-if="listView"><div class="bridge-list-controls"><label>Show <select v-model="layer" aria-label="Filter entry type"><option value="">Both sides</option><option value="informal">Informal entries</option><option value="formal">Lean declarations</option><option v-if="includeUnresolved" value="unresolved">Unresolved references</option></select></label></div><div class="bridge-node-list" aria-label="Mathematical entries"><button v-for="node in pageNodes" :key="node.id" :aria-pressed="selectedId===node.id" @click="choose(node.id, true)"><i :class="node.layer==='informal'?'informal-symbol':'formal-symbol'" :style="{background:node.color}"></i><span><strong>{{ node.title }}</strong><small>{{ layerName(node) }} · {{ node.kind || node.module }}</small></span><span>↗</span></button><p v-if="!matches.length" class="bridge-empty">No matching entries. Try another term or project.</p></div><nav class="bridge-pagination" aria-label="Mathematical entry pages"><button :disabled="page===1" @click="page--">← Previous</button><span>{{ page }} / {{ count(pages) }} · {{ count(matches.length) }} entries</span><button :disabled="page===pages" @click="page++">Next →</button></nav></template>
          <div v-else ref="viewport" class="bridge-viewport"><canvas ref="canvas" tabindex="0" :aria-label="`${showDependencies ? 'Two-layer graph with informal dependencies' : 'Bipartite graph'}: informal mathematics on the left, formal Lean declarations on the right. Drag to pan; scroll or pinch to zoom. Arrow keys pan, plus and minus zoom, zero fits. Use List to select entries with a keyboard.`" @pointerdown="startPan" @pointermove="pan" @pointerup="stopPan" @pointercancel="stopPan" @pointerleave="hovered=null" @wheel="wheel" @keydown="keyboard"></canvas><div class="bridge-partitions" aria-hidden="true"><span><i class="informal-symbol"></i> INFORMAL MATHEMATICS<small>Statements & proofs</small></span><span class="bridge-crossing">↔</span><span><i class="formal-symbol"></i> FORMAL MATHEMATICS<small>Lean declarations</small></span></div><div v-if="hovered" class="bridge-hover" :style="{left:hovered.x+'px',top:hovered.y+'px'}"><small>{{ layerName(hovered.node) }}</small><strong>{{ hovered.node.title }}</strong></div><div v-if="!matches.length" class="bridge-no-results" role="status">No matching entries. Try another search.</div><div class="bridge-map-hint">{{ showDependencies ? 'Select an informal point to trace its references and formal connections.' : 'Select a point to trace its connection across the two sides.' }}</div></div>
          <div class="bridge-map-footer"><span>{{ count(graph.nodes.length) }} entries <span class="footer-divider">/</span> {{ count(graph.edges.length) }} cross-links <template v-if="showDependencies && dependencyState==='ready'"><span class="footer-divider">/</span> {{ count(graph.dependencies.length) }} dependencies</template></span><div v-if="!listView" class="bridge-zoom"><button aria-label="Zoom out" @click="setZoom(camera.scale/1.3)">−</button><output aria-label="Zoom level">{{ zoom }}%</output><button aria-label="Zoom in" @click="setZoom(camera.scale*1.3)">+</button><button @click="fitGraph">Fit</button></div></div>
        </div>
        <aside v-if="selected" class="bridge-inspector" aria-label="Selected mathematical entry" aria-live="polite"><div class="bridge-inspector-top"><span>{{ selectedProject?.title || (selected.layer==='unresolved'?'Reference awaiting resolution':'Mathlib') }}</span><span>{{ selected.kind }}</span></div><span class="bridge-layer-badge" :class="selected.layer"><i :class="selected.layer==='informal'?'informal-symbol':'formal-symbol'"></i>{{ layerName(selected) }}</span><h2>{{ selected.layer === 'informal' ? selected.title : (selectedDetail?.fqname || selectedDetail?.name || selected.name || selected.title) }}</h2><p v-if="selected.module" class="bridge-module">{{ selected.module }}</p>
          <div v-if="detailState==='loading'" class="bridge-detail-status" role="status">Loading original source…</div><div v-else-if="detailState==='error'" class="bridge-detail-status" role="status">Source details could not be loaded. <button @click="loadDetail(selected)">Retry</button></div>
          <template v-else-if="selectedDetail"><template v-if="selected.layer==='informal'"><p v-if="selectedDetail.labels_tex?.length" class="bridge-source-label">{{ selectedDetail.labels_tex.join(' · ') }}</p><h3 class="bridge-code-label">Blueprint statement · LaTeX</h3><pre class="bridge-statement">{{ readableStatement }}</pre><details class="bridge-source-detail"><summary>Original blueprint LaTeX</summary><pre>{{ sourceText }}</pre></details></template><template v-else-if="selected.layer==='formal'"><h3 class="bridge-code-label">Lean type signature</h3><pre v-if="selectedDetail.type_full" class="bridge-lean-code">{{ selectedDetail.type_full }}</pre><p v-else class="bridge-note">The supplied export identifies this declaration but does not include its type signature.</p><p v-if="selectedDetail.doc_string" class="bridge-note">{{ selectedDetail.doc_string }}</p></template><p v-else class="bridge-note">{{ statusName(selectedDetail.resolution_status) }}. The supplied data does not identify a unique Lean declaration for this reference.</p><a v-if="sourceLink" :href="sourceLink" target="_blank" rel="noopener noreferrer" class="bridge-source-link">{{ selected.layer==='informal'?'Open pinned blueprint source':'Open pinned project repository' }} ↗</a></template>
          <section v-if="showDependencies && dependencyState==='ready'" class="bridge-dependencies" aria-label="Dependencies of selected entry">
            <div class="bridge-connections-heading"><h3>{{ selected.layer==='informal'?'Informal dependencies':'Formal dependencies' }} <span v-if="selected.layer==='informal'">{{ count(dependencies.length) }}</span></h3><button v-if="dependencies.length && !listView" @click="fitDependencies">Fit dependencies ↗</button></div>
            <template v-if="selected.layer==='informal'"><button v-for="(item,index) in dependencies.slice(0,dependencyLimit)" :key="item.node.id+item.edge.type+index" class="bridge-dependency" :class="{proof:item.edge.type==='PROVES'}" @click="choose(item.node.id,true)"><span><small>{{ item.direction }} · {{ item.edge.type==='PROVES'?'Proof link':'Reference' }}</small><strong>{{ item.node.title }}</strong><code v-if="item.edge.label">{{ item.edge.label }}</code></span><span>→</span></button><button v-if="dependencies.length>dependencyLimit" class="bridge-more" @click="dependencyLimit+=12">Show {{ Math.min(12,dependencies.length-dependencyLimit) }} more dependencies ↓</button><p v-if="!dependencies.length" class="bridge-note">No informal reference or proof links are recorded for this entry.</p></template>
            <p v-else class="bridge-note">Formal-to-formal dependencies are not included in the supplied data.</p>
          </section>
          <div class="bridge-connections"><div class="bridge-connections-heading"><h3>{{ selected.layer==='informal'?'Connected Lean declarations':'Connected informal entries' }} <span>{{ connections.length }}</span></h3><button v-if="connections.length && !listView" @click="fitConnections">Fit connections ↗</button></div><button v-for="(item,index) in connections.slice(0,connectionLimit)" :key="item.node.id+index" class="bridge-connection" @click="choose(item.node.id,true)"><span><small :class="{pending:!['resolved','resolved_mathlib'].includes(item.edge.status)}">{{ statusName(item.edge.status) }}</small><strong>{{ item.node.title }}</strong><code v-if="item.edge.lean_ref">{{ item.edge.lean_ref }}</code></span><span>→</span></button><button v-if="connections.length>connectionLimit" class="bridge-more" @click="connectionLimit+=12">Show {{ Math.min(12,connections.length-connectionLimit) }} more connections ↓</button><p v-if="!connections.length" class="bridge-note">No {{ includeUnresolved?'':'resolved ' }}cross-link is recorded for this entry in the supplied data.</p><button v-if="pendingCount && !includeUnresolved" class="bridge-more" @click="includeUnresolved=true">Show {{ pendingCount }} unresolved {{ pendingCount===1?'reference':'references' }} →</button></div>
          <p class="bridge-evidence">Connections come from explicit blueprint references. “Resolved” means a declaration was found; it does not certify that the informal statement and Lean declaration are equivalent.</p>
        </aside>
      </div>
    </section>
    <div class="bridge-caption"><span>READING THIS MAP</span><p>Circles are informal statements and proofs; squares are Lean declarations. Cross-links connect the two sides. Colors group projects. Enable “Show dependencies” to explore informal references and proof links within the left side; arrows follow the source’s direction. Formal-to-formal dependencies are not included in this dataset. Entries without a resolved counterpart remain visible. Unresolved references appear as hollow squares and dashed lines when enabled. <a :href="withBase('/dag-sources#informal-formal-bridge')">Data, coverage & provenance ↗</a></p></div>
    <footer class="bridge-bottom"><p>One idea, two ways to explore it.</p><a :href="withBase('/open-problems')">Find the next open problem ↗</a></footer>
  </main>
</template>
<style scoped src="./bipartiteAtlas.css"></style>
