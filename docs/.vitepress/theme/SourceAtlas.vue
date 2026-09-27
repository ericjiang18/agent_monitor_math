<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useData, withBase } from 'vitepress'
import { researchFetch } from './researchApi.js'
import { prepareGraph, boundsFor, drawGraph, hitNode } from './informalGraph.js'
import preview from './atlasPreviewData.json'

const { isDark } = useData()

const atlas = shallowRef(null), graph = shallowRef(null), loading = ref(true), loadError = ref('')
const area = ref(''), query = ref(''), selectedId = ref(''), viewport = ref(null), canvas = ref(null)
const listView = ref(false), page = ref(1), pageSize = 30, camera = { x: 0, y: 0, scale: .1 }
const zoom = ref(10), hovered = shallowRef(null), connectionLimit = ref(8)
const memories = ref([]), memoryState = ref('loading'), user = ref(null)
const contributionOpen = ref(false), sourceRun = ref(''), publish = ref(false), submitting = ref(false)
const contributionMessage = ref(''), contributionError = ref(false)
const allowedModels = ref(['gpt-6', 'fable', 'gpt-5.6-sol'])
const fields = computed(() => graph.value?.clusters.map(cluster => cluster.title).sort() || [])
const selected = computed(() => graph.value?.byId.get(selectedId.value))
const matches = computed(() => {
  const term = query.value.trim().toLowerCase()
  return graph.value?.nodes.filter(node => (!area.value || node.area === area.value) && (!term || `${node.title} ${node.tag || ''} ${node.area} ${node.summary} ${node.record?.model || ''}`.toLowerCase().includes(term))) || []
})
const matchIds = computed(() => query.value.trim() ? new Set(matches.value.map(node => node.id)) : null)
const pages = computed(() => Math.max(1, Math.ceil(matches.value.length / pageSize)))
const pageNodes = computed(() => matches.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const connections = computed(() => graph.value?.adjacency.get(selectedId.value) || [])
const selectedMemories = computed(() => memories.value.filter(item => item.layer !== 'formal' && item.node_id === selectedId.value))
const informalMemories = computed(() => memories.value.filter(item => item.layer !== 'formal'))
const count = value => Number(value || 0).toLocaleString('en-US')
const sourceLink = computed(() => {
  const url = selected.value?.source_url || selected.value?.reference_source?.url
  return typeof url === 'string' && /^https:\/\//.test(url) ? url : ''
})
const pinnedSourceLink = computed(() => {
  const url = selected.value?.source_git_url
  return typeof url === 'string' && /^https:\/\/github\.com\/stacks\/stacks-project\/blob\/[a-f0-9]{40}\//.test(url) ? url : ''
})
const featuredFields = computed(() => ['Commutative Algebra', 'Schemes', 'Sheaf Cohomology', 'Categories & Foundations'].filter(field => fields.value.includes(field)))
let resizeObserver, frame = 0, disposed = false, abortController
let dimensions = { width: 800, height: 610, ratio: 1 }
const pointers = new Map()
let drag = null, pinch = null

function scheduleDraw() {
  if (frame || !graph.value || !canvas.value) return
  frame = requestAnimationFrame(() => {
    frame = 0
    if (canvas.value && graph.value) drawGraph(canvas.value, graph.value, camera, { ...dimensions, dark: isDark.value, area: area.value, selected: selectedId.value, matches: matchIds.value })
  })
}
function resize() {
  if (!viewport.value || !canvas.value) return
  const width = viewport.value.clientWidth, height = viewport.value.clientHeight
  if (!width || !height) return
  const oldWidth = dimensions.width, oldHeight = dimensions.height
  dimensions = { width, height, ratio: Math.min(window.devicePixelRatio || 1, 2) }
  canvas.value.width = Math.round(width * dimensions.ratio)
  canvas.value.height = Math.round(height * dimensions.ratio)
  camera.x += (width - oldWidth) / 2; camera.y += (height - oldHeight) / 2
  scheduleDraw()
}
async function connectCanvas(fit = false) {
  await nextTick()
  resizeObserver?.disconnect()
  if (viewport.value) { resizeObserver = new ResizeObserver(resize); resizeObserver.observe(viewport.value) }
  resize()
  if (fit) fitGraph()
  else scheduleDraw()
}
function setCamera(scale, x = dimensions.width / 2, y = dimensions.height / 2) {
  const next = Math.max(.015, Math.min(4, scale))
  camera.x = x - (x - camera.x) * next / camera.scale
  camera.y = y - (y - camera.y) * next / camera.scale
  camera.scale = next; zoom.value = Math.round(next * 100)
  hovered.value = null; scheduleDraw()
}
function fitGraph() {
  if (!graph.value) return
  const nodes = area.value || query.value.trim() ? matches.value : graph.value.nodes
  if (!nodes.length) return
  const bounds = boundsFor(nodes), padding = 90
  camera.scale = Math.min(1.4, (dimensions.width - padding) / Math.max(120, bounds.maxX - bounds.minX), (dimensions.height - padding - 35) / Math.max(120, bounds.maxY - bounds.minY))
  camera.scale = Math.max(.015, camera.scale)
  camera.x = dimensions.width / 2 - (bounds.minX + bounds.maxX) / 2 * camera.scale
  camera.y = (dimensions.height - 25) / 2 - (bounds.minY + bounds.maxY) / 2 * camera.scale
  zoom.value = Math.round(camera.scale * 100); scheduleDraw()
}
async function choose(id, center = true) {
  const node = graph.value?.byId.get(id)
  if (!node) return
  selectedId.value = id; connectionLimit.value = 8; contributionOpen.value = false; contributionMessage.value = ''
  hovered.value = null
  if (area.value && node.area !== area.value) area.value = ''
  if (center) {
    camera.scale = Math.max(camera.scale, .9)
    camera.x = dimensions.width / 2 - node.x * camera.scale
    camera.y = dimensions.height / 2 - node.y * camera.scale
    zoom.value = Math.round(camera.scale * 100)
  }
  if (typeof window !== 'undefined') history.replaceState(null, '', '#' + encodeURIComponent(id))
  scheduleDraw()
  if (node.memory && !node.record._full) {
    try {
      const data = await researchFetch('/api/research/dag/' + encodeURIComponent(node.record.id))
      const record = data.memory || data
      if (record.id === node.record.id && typeof record.content === 'string') {
        memories.value = memories.value.map(item => item.id === record.id ? { ...item, ...record, _full: true } : item)
        refreshGraph()
      }
    } catch { /* The catalogue preview remains readable if details are unavailable. */ }
  }
}
function searchFirst() {
  const term = query.value.trim().toLowerCase()
  const node = matches.value.find(node => node.id.toLowerCase() === term || node.tag?.toLowerCase() === term) || matches.value[0]
  if (node) choose(node.id)
}
function fitField() {
  if (!matches.value.some(node => node.id === selectedId.value)) {
    const node = matches.value.find(node => node.kind === 'theorem') || matches.value[0]
    if (node) choose(node.id, false)
  }
  nextTick(fitGraph)
}
function changeField(field) { area.value = field; query.value = ''; fitField() }
function eventPoint(event) { const rect = canvas.value.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top } }
function startPan(event) {
  if (event.button !== 0 && event.pointerType !== 'touch') return
  const point = eventPoint(event)
  pointers.set(event.pointerId, point); canvas.value.setPointerCapture(event.pointerId)
  drag = { pointer: event.pointerId, start: point, last: point, moved: false }
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()]
    pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y), center: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } }
  }
  hovered.value = null
}
function pan(event) {
  if (!graph.value) return
  const point = eventPoint(event)
  if (!pointers.has(event.pointerId)) {
    if (event.pointerType !== 'touch') {
      const node = hitNode(graph.value, camera, point.x, point.y, area.value)
      hovered.value = node ? { node, x: Math.max(8, Math.min(point.x + 13, dimensions.width - 220)), y: Math.max(55, Math.min(point.y - 12, dimensions.height - 68)) } : null
    }
    return
  }
  pointers.set(event.pointerId, point)
  if (pinch && pointers.size === 2) {
    const [a, b] = [...pointers.values()], distance = Math.hypot(a.x - b.x, a.y - b.y)
    const center = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
    setCamera(camera.scale * distance / Math.max(1, pinch.distance), center.x, center.y)
    camera.x += center.x - pinch.center.x; camera.y += center.y - pinch.center.y
    pinch = { distance, center }; if (drag) drag.moved = true
  } else if (drag && drag.pointer === event.pointerId) {
    camera.x += point.x - drag.last.x; camera.y += point.y - drag.last.y
    if (Math.hypot(point.x - drag.start.x, point.y - drag.start.y) > 5) drag.moved = true
    drag.last = point
  }
  scheduleDraw()
}
function stopPan(event) {
  const wasPinch = Boolean(pinch)
  if (event && drag && !drag.moved && !wasPinch && event.type !== 'pointercancel') {
    const point = eventPoint(event), node = hitNode(graph.value, camera, point.x, point.y, area.value)
    if (node) choose(node.id, false)
  }
  if (event) pointers.delete(event.pointerId); else pointers.clear()
  drag = null; pinch = null
}
function wheel(event) { event.preventDefault(); const point = eventPoint(event); setCamera(camera.scale * Math.exp(-event.deltaY * .0017), point.x, point.y) }
function keyboard(event) {
  const moves = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] }
  if (moves[event.key]) { event.preventDefault(); camera.x += moves[event.key][0]; camera.y += moves[event.key][1]; scheduleDraw() }
  if (event.key === '+' || event.key === '=') { event.preventDefault(); setCamera(camera.scale * 1.3) }
  if (event.key === '-') { event.preventDefault(); setCamera(camera.scale / 1.3) }
  if (event.key === '0') { event.preventDefault(); fitGraph() }
}
function refreshGraph() {
  if (!atlas.value) return
  graph.value = prepareGraph(atlas.value, informalMemories.value)
  if (!graph.value.byId.has(selectedId.value)) {
    const start = graph.value.nodes.find(node => node.kind?.toLowerCase() === 'theorem' && graph.value.adjacency.get(node.id)?.length >= 3)
    selectedId.value = start?.id || graph.value.nodes[0]?.id || ''
  }
  scheduleDraw()
}
async function loadAtlas() {
  loading.value = true; loadError.value = ''
  try {
    const response = await fetch(withBase('/research/informal-atlas.json'), { signal: abortController.signal })
    if (!response.ok) throw new Error('The research catalogue could not be loaded. Please try again.')
    const data = await response.json()
    if (!Array.isArray(data.nodes) || !data.nodes.length || !Array.isArray(data.edges)) throw new Error('The research catalogue is not available yet.')
    if (disposed) return
    atlas.value = data; refreshGraph()
    let hash = ''
    try { hash = decodeURIComponent(window.location.hash.slice(1)) } catch { /* Ignore malformed URL fragments. */ }
    loading.value = false
    await connectCanvas(true)
    if (graph.value.byId.has(hash)) choose(hash)
  } catch (error) { if (!disposed) { loadError.value = error.message; loading.value = false } }
}
async function loadMemory() {
  memoryState.value = 'loading'
  try {
    const all = [], seen = new Set()
    let offset = 0
    do {
      const data = await researchFetch('/api/research/dag?layer=informal&limit=1000&offset=' + offset)
      if (disposed) return
      user.value = data.user || null
      if (data.policy?.allowed_models?.length) allowedModels.value = data.policy.allowed_models
      for (const item of data.memories || []) if (!seen.has(item.id)) { seen.add(item.id); all.push(item) }
      if (!data.has_more || !Number.isFinite(data.next_offset) || data.next_offset <= offset) break
      offset = data.next_offset
    } while (!disposed)
    memories.value = all; memoryState.value = 'ready'; refreshGraph()
  } catch { if (!disposed) memoryState.value = 'unavailable' }
}
async function saveContribution() {
  if (!sourceRun.value.trim() || !publish.value || selected.value?.memory) return
  submitting.value = true; contributionMessage.value = ''
  try {
    await researchFetch('/api/research/dag', { method: 'POST', body: JSON.stringify({ source_run_id: sourceRun.value.trim(), node_id: selectedId.value, layer: 'informal', publish: true }) })
    contributionError.value = false; contributionMessage.value = 'Research memory published and connected to this idea.'
    sourceRun.value = ''; publish.value = false; await loadMemory()
  } catch (error) { contributionError.value = true; contributionMessage.value = error.message }
  finally { submitting.value = false }
}
async function revokeMemory(memory) {
  if (!window.confirm('Remove this memory from the public atlas?')) return
  try { await researchFetch('/api/research/dag/' + encodeURIComponent(memory.id), { method: 'DELETE' }); await loadMemory() }
  catch (error) { contributionError.value = true; contributionMessage.value = error.message; contributionOpen.value = true }
}
watch([query, area], () => { page.value = 1; scheduleDraw() })
watch(isDark, scheduleDraw)
watch(listView, () => { if (!listView.value) connectCanvas() })
onMounted(() => { abortController = new AbortController(); loadAtlas(); loadMemory() })
onBeforeUnmount(() => { disposed = true; abortController?.abort(); resizeObserver?.disconnect(); cancelAnimationFrame(frame); stopPan() })
</script>

<template>
  <main class="research-atlas">
    <header class="atlas-header">
      <div class="atlas-intro"><p class="atlas-eyebrow"><span></span> THE RESEARCH ATLAS <span class="atlas-edition">VOL. 03 / INFORMAL</span></p><h1>{{ count(atlas?.nodes.length || preview.count) }} ideas.<br><em>Every one has a source.</em></h1><p class="atlas-description">Travel through algebra, geometry, and the ideas between them. A source-linked mathematical DAG, with room for the next useful discovery.</p></div>
      <div class="atlas-numberplate" aria-label="Atlas statistics"><div><strong>{{ atlas ? count(atlas.nodes.length) : '—' }}</strong><span>source-linked nodes</span></div><div><strong>{{ atlas ? count(atlas.edges.length) : '—' }}</strong><span>directed references</span></div><div><strong>{{ count(informalMemories.length) }}</strong><span>research memories</span></div></div>
    </header>
    <div class="atlas-routebar"><span class="atlas-route-label">CHOOSE A STARTING POINT</span><button v-for="field in featuredFields" :key="field" @click="changeField(field)">{{ field }} <span>↗</span></button><button @click="changeField(''); listView = false">The whole landscape <span>↗</span></button></div>
    <section class="atlas-workbench" aria-label="Interactive mathematical research graph">
      <div class="atlas-toolbar"><div class="atlas-map-title"><i class="layer-dot"></i><strong>The informal DAG</strong><span v-if="graph">{{ count(graph.clusters.length) }} fields</span></div><div class="atlas-filters"><label class="atlas-search"><span aria-hidden="true">⌕</span><input v-model="query" aria-label="Find a mathematical idea" type="search" placeholder="Search ideas, tags, statements…" @keydown.enter="searchFirst"></label><select v-model="area" aria-label="Filter mathematical field" @change="fitField"><option value="">All fields</option><option v-for="field in fields" :key="field">{{ field }}</option><option v-if="informalMemories.length">Research memory</option></select><button class="atlas-list-toggle" :aria-pressed="listView" @click="listView = !listView">{{ listView ? '◇ Map' : '☷ List' }}</button></div></div>
      <div v-if="loading || loadError" class="atlas-load-state" role="status"><span class="atlas-load-orbit">✧</span><h2>{{ loading ? 'Unfolding the mathematical landscape…' : 'The atlas needs another moment.' }}</h2><p>{{ loadError || 'Loading the complete source-linked catalogue.' }}</p><button v-if="loadError" @click="loadAtlas">Reload the atlas</button></div>
      <div v-else-if="graph" class="atlas-explorer">
        <div class="atlas-map-column">
          <div v-if="query || area" class="atlas-search-summary" role="status">{{ count(matches.length) }} matching {{ matches.length === 1 ? 'node' : 'nodes' }}<button v-if="matches.length" @click="searchFirst">Go to first →</button><button @click="query = ''; changeField('')">Clear filters</button></div>
          <div v-if="listView" class="atlas-list-wrapper"><div class="atlas-node-list" aria-label="Research nodes"><button v-for="node in pageNodes" :key="node.id" :class="{ selected: selectedId === node.id }" :aria-pressed="selectedId === node.id" @click="choose(node.id)"><span :class="['layer-dot', { 'memory-dot': node.memory }]" :style="{ background: node.color }"></span><span><strong>{{ node.title }}</strong><small>{{ node.memory ? node.record.model : node.area }} · {{ node.tag ? 'Tag ' + node.tag : node.kind }}</small></span><span>↗</span></button><p v-if="!matches.length" class="atlas-empty">No matching ideas. Try another term or field.</p></div><nav class="atlas-pagination" aria-label="Research node pages"><button :disabled="page === 1" @click="page--">← Previous</button><span>Page {{ count(page) }} / {{ count(pages) }} · {{ count(matches.length) }} nodes</span><button :disabled="page === pages" @click="page++">Next →</button></nav></div>
          <div v-else ref="viewport" class="atlas-viewport"><canvas ref="canvas" class="atlas-canvas" tabindex="0" aria-label="Interactive informal research graph. Drag to pan, scroll or pinch to zoom. Arrow keys pan, plus and minus zoom, zero fits. Use List for accessible node selection." @pointerdown="startPan" @pointermove="pan" @pointerup="stopPan" @pointercancel="stopPan" @pointerleave="hovered = null" @wheel="wheel" @keydown="keyboard"></canvas><div class="atlas-map-badge"><span class="map-live-dot"></span>{{ area || (zoom < 50 ? 'Field overview' : 'Inside the landscape') }}<small>{{ count(graph.catalogueCount) }} source nodes + {{ count(informalMemories.length) }} memories</small></div><div v-if="hovered" class="atlas-hover" :style="{ left: hovered.x + 'px', top: hovered.y + 'px' }"><small>{{ hovered.node.memory ? 'RESEARCH MEMORY' : hovered.node.area }}</small><strong>{{ hovered.node.title }}</strong></div><div class="atlas-map-hint">{{ zoom < 50 && !area ? 'Every point is an idea. Zoom in to follow its references.' : 'Select a point to read its statement and trace its references.' }}</div><div class="atlas-compass" aria-hidden="true"><span>↑</span>EXPLORE</div></div>
          <div class="atlas-map-footer"><span><i class="layer-dot"></i>Source node <i class="memory-diamond"></i>Model memory</span><span v-if="!listView" class="atlas-pan-hint">{{ zoom < 50 && !area ? 'Field links aggregated at this zoom' : 'Arrows: referenced result → citing result' }}</span><div v-if="!listView" class="atlas-zoom"><button @click="setCamera(camera.scale / 1.3)" aria-label="Zoom out">−</button><output aria-label="Zoom level">{{ zoom }}%</output><button @click="setCamera(camera.scale * 1.3)" aria-label="Zoom in">+</button><button @click="fitGraph" class="atlas-fit">Fit</button></div></div>
        </div>
        <aside v-if="selected" class="atlas-inspector" aria-label="Selected research node" aria-live="polite"><div class="inspector-topline"><span>{{ selected.area }}</span><span>{{ selected.tag ? 'TAG ' + selected.tag : 'MEMORY' }}</span></div><span :class="['inspector-layer', { 'memory-layer': selected.memory }]"><i></i>{{ selected.memory ? 'Model-generated · informal' : 'Informal · ' + (selected.kind || 'Source statement') }}</span><h2>{{ selected.title }}</h2><p class="inspector-description">{{ selected.memory ? selected.record.content?.slice(0, 500) : selected.summary }}</p><a v-if="sourceLink" class="inspector-source" :href="sourceLink" target="_blank" rel="noopener noreferrer">Read the Stacks Project reference ↗</a><details v-if="selected.statement_latex" class="inspector-statement"><summary>Original mathematical statement</summary><pre>{{ selected.statement_latex }}</pre><small>Original source LaTeX; follow the reference for rendered mathematics.</small><a v-if="pinnedSourceLink" class="inspector-pinned-source" :href="pinnedSourceLink" target="_blank" rel="noopener noreferrer">View pinned source ↗</a></details><div v-if="selected.memory" class="inspector-memory-provenance"><strong>{{ selected.record.model }}</strong><span>Source run: {{ selected.record.source_run_id || selected.record.run_id }}</span><details><summary>Read memory</summary><pre>{{ selected.record.content }}</pre><small v-if="!selected.record._full">Preview shown. Select this memory again to retry loading the complete result.</small></details><button v-if="selected.record.is_owner" class="memory-remove" @click="revokeMemory(selected.record)">Remove my contribution</button></div><div v-else class="inspector-state"><span>◇ Published mathematical reference</span><p>Part of the source catalogue. Model research appears as separate gold diamonds, with its recorded model and source run.</p></div>
          <div v-if="connections.length" class="inspector-connections"><h3>Follow the references <span>{{ count(connections.length) }}</span></h3><button v-for="(connection, index) in connections.slice(0, connectionLimit)" :key="connection.node.id + index" @click="choose(connection.node.id)"><span><small>{{ connection.direction }}</small>{{ connection.node.title }}</span><span>→</span></button><button v-if="connections.length > connectionLimit" class="more-connections" @click="connectionLimit += 12">Show {{ Math.min(12, connections.length - connectionLimit) }} more references ↓</button></div><p v-else class="inspector-no-connections">No direct references in the displayed source-reference DAG.</p>
          <div v-if="!selected.memory" class="inspector-memories"><h3>Research memory <span>{{ selectedMemories.length }}</span></h3><button v-for="memory in selectedMemories" :key="memory.id" class="inspector-memory-link" @click="choose('memory:' + memory.id)"><strong>{{ memory.title }}</strong><small>{{ memory.model }} ↗</small></button><p v-if="!selectedMemories.length">No published memory for this node yet.</p></div><button v-if="!selected.memory" class="inspector-contribute" @click="contributionOpen = !contributionOpen">{{ contributionOpen ? 'Close contribution' : '+ Contribute a completed run' }}</button>
          <div v-if="contributionOpen" class="atlas-contribution"><template v-if="user && !user.guest"><p>Publish a result from your completed run. Its response must identify an eligible model. Runs without that record, including current native Codex runs, cannot be published.</p><form @submit.prevent="saveContribution"><label>Source run ID<input v-model="sourceRun" required placeholder="Your completed run ID" maxlength="160"></label><label class="publish-check"><input v-model="publish" type="checkbox" required> Make this result public as shared research memory.</label><button type="submit" :disabled="submitting || !publish || !sourceRun.trim()">{{ submitting ? 'Checking provenance…' : 'Verify & publish' }}</button></form></template><template v-else><p>Sign in to contribute a completed proving run to this node.</p><a href="/sign-in">Sign in to contribute →</a></template><p v-if="contributionMessage" :class="{ 'contribution-error': contributionError }" role="status">{{ contributionMessage }}</p></div>
        </aside>
      </div>
    </section>
    <p class="atlas-caption"><span>READING THIS MAP</span><span>The catalogue contains {{ count(atlas?.nodes.length || preview.count) }} mathematical statements from the <a href="https://stacks.math.columbia.edu/" target="_blank" rel="noopener noreferrer">Stacks Project</a>. Arrows follow earlier source references; forward references are omitted to create a DAG. Isolated statements and separate components remain in the catalogue. Each statement retains its source text and stable reference tag. <a href="/dag-sources">Coverage, attribution & source license ↗</a></span></p>
    <section class="atlas-memory-section" aria-labelledby="memory-title"><div class="atlas-memory-intro"><p class="atlas-eyebrow">FROM ONE RUN TO THE NEXT</p><h2 id="memory-title">Good work should<br><em>have somewhere to go.</em></h2><p>Connect a completed run to an idea. Its research becomes a visible node and reusable context for the next proving harness.</p><a href="/guest">Open the proving workspace →</a></div><div class="atlas-memory-details"><div class="memory-rule"><span>01</span><div><h3>Only eligible model results</h3><p>Shared research memory accepts recorded runs from <strong>{{ allowedModels.join(', ') }}</strong>. Eligibility comes from the actual run provenance.</p></div></div><div class="memory-rule"><span>02</span><div><h3>A source, a result, a connection</h3><p>Publish an eligible completed run to a source node. Gold diamonds distinguish model reasoning from the mathematical literature and retain the source run.</p></div></div><div class="memory-rule"><span>03</span><div><h3>A live record of useful work</h3><p v-if="memoryState === 'loading'" role="status">Checking the shared memory collection…</p><p v-else-if="memoryState === 'unavailable'" role="status">Shared memory is temporarily unavailable. The source catalogue is ready to explore. <button @click="loadMemory">Retry</button></p><p v-else>{{ count(informalMemories.length) }} published informal {{ informalMemories.length === 1 ? 'memory' : 'memories' }}, displayed in addition to the {{ count(atlas?.nodes.length || preview.count) }} source nodes. <button @click="loadMemory">Refresh memories</button></p></div></div></div></section><footer class="atlas-bottom-link"><span>The next question might already be waiting.</span><a href="/open-problems">Explore Open Problems & Forum <span>↗</span></a></footer>
  </main>
</template>

<style scoped>
.research-atlas { --atlas-green:#2563eb; --atlas-rust:#b45309; max-width:1560px; margin:0 auto; padding:52px 48px 44px; color:var(--vp-c-text-1); }
.research-atlas *, .research-atlas *::before, .research-atlas *::after { box-sizing:border-box; }
.research-atlas button, .research-atlas input, .research-atlas select { font:inherit; }
.research-atlas button { cursor:pointer; }
.research-atlas button:focus-visible, .research-atlas a:focus-visible, .research-atlas input:focus-visible, .research-atlas select:focus-visible { outline:2px solid var(--vp-c-brand-1); outline-offset:3px; }
.atlas-header { display:flex; justify-content:space-between; align-items:flex-end; gap:36px; padding:0 0 32px; }
.atlas-eyebrow { display:flex; align-items:center; gap:9px; font-size:10px; line-height:1.5; font-weight:650; letter-spacing:.14em; color:var(--vp-c-brand-1); margin:0 0 22px; }
.atlas-eyebrow > span:first-child { width:6px; height:6px; background:var(--vp-c-brand-1); border-radius:50%; }
.atlas-edition { padding-left:14px; border-left:1px solid var(--vp-c-divider); color:var(--vp-c-text-3); font-size:9px; }
.atlas-intro h1 { font:400 clamp(34px,3.8vw,59px)/1.04 'Newsreader Variable',Georgia,serif; letter-spacing:-.035em; margin:0; }
.atlas-intro h1 em, .atlas-memory-intro h2 em { color:var(--vp-c-brand-1); font-weight:400; }
.atlas-description { max-width:610px; margin:19px 0 0; color:var(--vp-c-text-2); font-size:13px; line-height:1.8; }
.atlas-numberplate { display:flex; border-top:1px solid var(--vp-c-divider); border-bottom:1px solid var(--vp-c-divider); padding:18px 0; gap:22px; min-width:280px; }
.atlas-numberplate > div { display:flex; flex-direction:column; gap:5px; }
.atlas-numberplate strong { font:400 35px/1 'Newsreader Variable',Georgia,serif; }
.atlas-numberplate span { font-size:9px; color:var(--vp-c-text-3); white-space:nowrap; }
.atlas-routebar { display:flex; align-items:center; gap:12px; border-top:1px solid var(--vp-c-divider); padding:15px 0 23px; }
.atlas-route-label { font-size:9px; font-weight:600; letter-spacing:.1em; color:var(--vp-c-text-3); padding-right:8px; }
.atlas-routebar button { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:7px 11px; background:transparent; border:1px solid var(--vp-c-divider); border-radius:4px; font-size:10px; }
.atlas-routebar button:hover { background:var(--vp-c-bg-soft); border-color:var(--vp-c-brand-1); }
.atlas-routebar button span { color:var(--vp-c-brand-1); }
.atlas-workbench { border:1px solid var(--vp-c-divider); border-radius:10px; overflow:hidden; background:var(--vp-c-bg-elv); box-shadow:0 8px 32px #17212f05; }
.atlas-toolbar { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:12px; border-bottom:1px solid var(--vp-c-divider); }
.layer-dot { display:inline-block; flex-shrink:0; width:6px; height:6px; border-radius:50%; background:var(--atlas-green); }
.atlas-filters { display:flex; gap:8px; align-items:center; }
.atlas-search { display:flex; align-items:center; gap:6px; width:175px; padding:6px 9px; border:1px solid var(--vp-c-divider); border-radius:5px; }
.atlas-search > span { font-size:18px; color:var(--vp-c-text-3); }
.atlas-search input { min-width:0; width:100%; background:transparent; font-size:11px; outline:none; }
.atlas-filters select { max-width:155px; padding:8px; border:1px solid var(--vp-c-divider); border-radius:5px; font-size:10px; background:var(--vp-c-bg-elv); color:var(--vp-c-text-2); }
.atlas-list-toggle { border:1px solid var(--vp-c-divider); padding:7px 10px; border-radius:5px; font-size:11px!important; white-space:nowrap; }
.atlas-explorer { display:grid; grid-template-columns:minmax(0,1fr) 288px; }
.atlas-map-column { min-width:0; position:relative; display:flex; flex-direction:column; }
.atlas-viewport { position:relative; height:606px; overflow:auto; background-color:var(--vp-c-bg); background-image:radial-gradient(var(--vp-c-divider) .7px,transparent .7px); background-size:17px 17px; cursor:grab; overscroll-behavior-x:contain; }
.atlas-viewport:active { cursor:grabbing; }
.atlas-node:hover { z-index:2; border-color:var(--atlas-green); box-shadow:0 3px 10px #17212f20; }
.atlas-map-footer { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; padding:10px 12px; border-top:1px solid var(--vp-c-divider); font-size:9px; color:var(--vp-c-text-3); background:var(--vp-c-bg-elv); margin-top:auto; }
.atlas-map-footer > span { display:flex; align-items:center; gap:5px; }
.atlas-pan-hint { margin-left:auto; font-size:8px; }
.atlas-zoom { display:flex; align-items:center; gap:4px; }
.atlas-zoom button { padding:2px 7px; border:1px solid var(--vp-c-divider); border-radius:3px; font-size:13px; }
.atlas-zoom output { width:35px; text-align:center; }
.atlas-zoom .atlas-fit { font-size:10px; padding:3px 8px; margin-left:5px; }
.atlas-inspector { border-left:1px solid var(--vp-c-divider); padding:20px; max-height:650px; overflow-y:auto; background:var(--vp-c-bg-elv); }
.inspector-topline { display:flex; justify-content:space-between; gap:8px; margin-bottom:20px; font-size:8px; color:var(--vp-c-text-3); letter-spacing:.03em; }
.inspector-layer { display:inline-flex; align-items:center; gap:6px; padding:4px 7px; background:#2563eb0c; border:1px solid #2563eb22; border-radius:3px; color:var(--vp-c-brand-1); font-size:9px; }
.inspector-layer i { width:5px; height:5px; background:currentColor; border-radius:50%; }
.atlas-inspector h2 { margin:14px 0 10px; border:0; font:400 30px/1.06 'Newsreader Variable',Georgia,serif; letter-spacing:-.02em; }
.inspector-formula { padding:13px 0; font:italic 19px/1.4 Georgia,serif; color:var(--vp-c-brand-1); }
.inspector-description { font-size:11px; line-height:1.85; color:var(--vp-c-text-2); margin:0 0 12px; }
.inspector-pinned-source { display:block; margin-top:8px; color:var(--vp-c-brand-1); font-size:10px; text-decoration:underline; text-underline-offset:3px; }
.inspector-source { color:var(--vp-c-brand-1); font-size:10px; text-decoration:underline; text-underline-offset:3px; }
.inspector-state { border:1px solid var(--vp-c-divider); border-radius:5px; background:var(--vp-c-bg-soft); padding:11px; margin-top:18px; }
.inspector-state > span { font-size:10px; font-weight:600; }
.inspector-state p { font-size:10px; color:var(--vp-c-text-2); line-height:1.7; margin:6px 0 0; }
.inspector-connections { margin-top:18px; }
.atlas-inspector h3 { font-family:var(--vp-font-family-base); font-size:10px; font-weight:600; margin:0 0 7px; }
.inspector-connections button { display:flex; width:100%; align-items:center; justify-content:space-between; gap:10px; font-size:11px; text-align:left; padding:8px 0; border-bottom:1px solid var(--vp-c-divider); }
.inspector-connections button small { display:block; color:var(--vp-c-text-3); font-size:8px; margin-bottom:2px; }
.inspector-connections button:hover { color:var(--vp-c-brand-1); }
.inspector-memories { margin-top:18px; }
.inspector-memories h3 { display:flex; justify-content:space-between; }
.inspector-memories > p { color:var(--vp-c-text-3); font-size:10px; line-height:1.7; }
.inspector-memories article { padding:10px 0; border-top:1px solid var(--vp-c-divider); }
.inspector-memories article strong { display:block; font-size:11px; }
.inspector-memories article small { display:block; color:var(--vp-c-text-3); font-size:9px; }
.memory-remove { margin-top:8px; color:var(--vp-c-text-3); text-decoration:underline; font-size:10px!important; }
.inspector-memories details { font-size:10px; margin-top:7px; }
.inspector-memories pre { white-space:pre-wrap; overflow-wrap:anywhere; font-size:10px; max-height:260px; overflow-y:auto; }
.inspector-contribute { width:100%; text-align:center; border:1px solid var(--vp-c-border); border-radius:5px; padding:10px 8px; font-size:10px!important; margin-top:12px; }
.atlas-contribution { margin-top:12px; font-size:10px; line-height:1.7; color:var(--vp-c-text-2); }
.atlas-contribution a { color:var(--vp-c-brand-1); text-decoration:underline; }
.atlas-contribution form { display:flex; flex-direction:column; gap:10px; margin-top:10px; }
.atlas-contribution input:not([type=checkbox]) { width:100%; display:block; padding:7px 8px; border:1px solid var(--vp-c-border); border-radius:4px; margin-top:4px; }
.atlas-contribution .publish-check { display:flex; gap:6px; align-items:flex-start; }
.publish-check input { margin-top:4px; }
.atlas-contribution form button { padding:9px; background:var(--vp-c-brand-1); color:var(--vp-c-bg); border-radius:4px; }
.atlas-contribution form button:disabled { opacity:.45; }
.contribution-error { color:#b85f41; }
.atlas-search-summary { padding:9px 14px; font-size:10px; border-bottom:1px solid var(--vp-c-divider); }
.atlas-search-summary button { color:var(--vp-c-brand-1); margin-left:12px; }
.atlas-node-list { max-height:606px; min-height:606px; overflow-y:auto; padding:8px 14px; }
.atlas-node-list > button { display:flex; width:100%; align-items:center; gap:12px; padding:13px 8px; border-bottom:1px solid var(--vp-c-divider); text-align:left; }
.atlas-node-list > button > span:nth-child(2) { flex:1; }
.atlas-node-list strong { display:block; font-size:12px; }
.atlas-node-list small { display:block; color:var(--vp-c-text-3); font-size:10px; margin-top:3px; }
.atlas-node-list > button.selected { background:var(--vp-c-brand-soft); }
.atlas-empty { color:var(--vp-c-text-3); font-size:12px; padding:30px 12px; }
.atlas-caption { display:flex; gap:18px; padding:15px 1px 0; margin:0; font-size:10px; color:var(--vp-c-text-3); line-height:1.7; }
.atlas-caption > span { flex-shrink:0; font-size:8px; letter-spacing:.1em; margin-top:2px; }
.atlas-memory-section { display:grid; grid-template-columns:1fr 1.1fr; gap:80px; padding:65px 0 52px; }
.atlas-memory-intro .atlas-eyebrow { margin:0 0 15px; font-size:9px; }
.atlas-memory-intro h2 { font:400 37px/1.1 'Newsreader Variable',Georgia,serif; letter-spacing:-.03em; margin:0 0 17px; }
.atlas-memory-intro > p:not(.atlas-eyebrow) { max-width:380px; font-size:12px; line-height:1.85; color:var(--vp-c-text-2); }
.atlas-memory-intro a { display:inline-block; color:var(--vp-c-brand-1); font-size:11px; font-weight:600; margin-top:18px; border-bottom:1px solid var(--vp-c-border); padding-bottom:5px; }
.atlas-memory-details { display:flex; flex-direction:column; }
.memory-rule { display:flex; gap:19px; padding:16px 0; border-top:1px solid var(--vp-c-divider); }
.memory-rule > span { font:400 22px 'Newsreader Variable',Georgia,serif; color:var(--vp-c-text-3); }
.memory-rule h3 { font-family:var(--vp-font-family-base); font-size:12px; font-weight:600; margin:0 0 6px; }
.memory-rule p { font-size:11px; line-height:1.85; color:var(--vp-c-text-2); margin:0; }
.memory-rule p strong { font-weight:500; }
.memory-rule button { text-decoration:underline; color:var(--vp-c-brand-1); }
.atlas-bottom-link { display:flex; justify-content:space-between; gap:20px; padding:22px 0; border-top:1px solid var(--vp-c-divider); color:var(--vp-c-text-3); font-size:12px; }
.atlas-bottom-link a { color:var(--vp-c-brand-1); font-weight:600; }
.atlas-bottom-link a span { margin-left:18px; }
:global(.dark) .research-atlas { --atlas-green:#82b1ff; --atlas-rust:#fbbf24; }
@media (min-width:1600px) { .atlas-explorer { grid-template-columns:minmax(0,1fr) 310px; } .atlas-viewport { height:660px; } .atlas-inspector { max-height:704px; padding:24px; } }
@media (max-width:1100px) { .research-atlas { padding:40px 28px; } .atlas-numberplate { min-width:240px; gap:15px; } .atlas-numberplate span { font-size:8px; } .atlas-toolbar { flex-wrap:wrap; } .atlas-explorer { grid-template-columns:minmax(0,1fr) 262px; } .atlas-inspector { padding:17px; } .atlas-filters { flex:1; justify-content:flex-end; } .atlas-search { width:140px; } .atlas-memory-section { gap:40px; } }
@media (max-width:800px) { .atlas-header { align-items:flex-start; flex-direction:column; gap:25px; } .atlas-intro h1 { font-size:45px; } .atlas-numberplate { width:100%; gap:0; justify-content:space-between; padding:15px 0; } .atlas-numberplate > div { min-width:90px; } .atlas-numberplate span { font-size:10px; } .atlas-numberplate strong { font-size:31px; } .atlas-routebar { gap:8px; flex-wrap:wrap; border-top:0; padding-top:0; } .atlas-route-label { flex-basis:100%; margin-bottom:3px; } .atlas-explorer { grid-template-columns:minmax(0,1fr); } .atlas-viewport { height:480px; } .atlas-inspector { border-left:0; border-top:1px solid var(--vp-c-divider); max-height:none; padding:24px; } .inspector-topline { margin-bottom:15px; } .inspector-description { font-size:13px; } .inspector-state p, .inspector-source, .inspector-memories > p { font-size:12px; } .inspector-connections { display:grid; grid-template-columns:1fr 1fr; gap:0 24px; } .inspector-connections h3 { grid-column:1/-1; } .inspector-pair, .inspector-contribute { font-size:12px!important; } .atlas-inspector h3 { font-size:12px; } .atlas-node-list { min-height:400px; max-height:480px; } .atlas-memory-section { gap:30px; padding:44px 0; } .atlas-memory-intro h2 { font-size:31px; } .atlas-contribution { font-size:12px; } .atlas-contribution input:not([type=checkbox]) { font-size:16px; } }
@media (max-width:540px) { .research-atlas { padding:30px 16px; } .atlas-eyebrow { font-size:9px; gap:7px; } .atlas-edition { font-size:8px; padding-left:9px; } .atlas-intro h1 { font-size:37px; } .atlas-description { font-size:12px; margin-top:15px; } .atlas-header { padding-bottom:22px; } .atlas-routebar button { font-size:9px; padding:7px 9px; gap:10px; } .atlas-toolbar { padding:10px; gap:10px; } .atlas-layer-picker { width:100%; } .atlas-layer-picker button { flex:1; justify-content:center; font-size:11px; padding:7px; } .atlas-filters { display:grid; grid-template-columns:minmax(0,1fr) auto; width:100%; gap:8px; } .atlas-search { width:100%; grid-column:1/-1; padding:7px 9px; } .atlas-search input { font-size:16px; } .atlas-filters select { max-width:none; width:100%; font-size:12px; min-height:35px; } .atlas-list-toggle { min-height:35px; } .atlas-viewport { height:405px; } .atlas-map-footer { padding:9px; gap:10px; } .atlas-map-footer > span { font-size:8px; } .atlas-zoom { margin-left:auto; } .atlas-zoom button { min-width:28px; min-height:28px; } .atlas-caption { flex-direction:column; gap:5px; font-size:10px; } .atlas-inspector { padding:20px; } .atlas-memory-section { grid-template-columns:1fr; gap:27px; } .atlas-memory-intro h2 { font-size:34px; } .atlas-memory-details { padding-top:2px; } .memory-rule p { font-size:12px; } .atlas-bottom-link { flex-direction:column; gap:10px; font-size:12px; } .inspector-connections { grid-template-columns:1fr; } }
@media (prefers-reduced-motion:reduce) { .atlas-node { transition:none; } }
/* The source catalogue uses a canvas landscape; list and inspector remain semantic HTML. */
.research-atlas{max-width:1660px;padding:46px 44px 35px}.atlas-header{gap:32px}.atlas-intro h1{font-size:clamp(36px,3.7vw,58px)}.atlas-numberplate{gap:25px;min-width:0}.atlas-numberplate strong{font-size:36px;font-variant-numeric:tabular-nums}.atlas-numberplate>div{gap:8px}.atlas-routebar{flex-wrap:wrap}.atlas-toolbar{padding:13px 16px}.atlas-map-title{display:flex;gap:8px;align-items:center;white-space:nowrap;font-size:11px}.atlas-map-title strong{font-weight:600}.atlas-map-title>span{font-size:10px;color:var(--vp-c-text-3);padding-left:4px}.atlas-search{width:250px}.atlas-filters select{max-width:180px}.atlas-explorer{grid-template-columns:minmax(0,1fr) 290px}.atlas-viewport{height:620px;overflow:hidden;background-color:var(--atlas-map-bg);background-image:radial-gradient(ellipse at 15% 10%,var(--atlas-map-wash),transparent 55%),radial-gradient(var(--atlas-map-grid) .8px,transparent .8px);background-size:auto,22px 22px}.atlas-canvas{position:absolute;inset:0;width:100%;height:100%;touch-action:none;cursor:grab}.atlas-canvas:active{cursor:grabbing}.atlas-canvas:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:-4px}.atlas-map-badge{position:absolute;left:21px;top:20px;pointer-events:none;max-width:calc(100% - 36px);padding:10px 13px;border:1px solid var(--atlas-map-border);border-radius:5px;background:var(--atlas-map-overlay);font-size:11px;color:var(--atlas-map-text);line-height:1.5}.atlas-map-badge small{display:block;font-size:9px;color:var(--atlas-map-muted);padding-left:14px;margin-top:3px}.map-live-dot{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--vp-c-brand-1);margin-right:9px;box-shadow:0 0 7px #82b1ff60}.atlas-map-hint{position:absolute;bottom:18px;left:21px;max-width:calc(100% - 115px);font-size:9px;color:var(--atlas-map-muted);pointer-events:none;background:var(--atlas-map-overlay);padding:5px 7px;border-radius:4px;line-height:1.6}.atlas-compass{position:absolute;right:20px;bottom:20px;display:flex;align-items:center;gap:7px;letter-spacing:.12em;font-size:7px;color:var(--atlas-map-muted);pointer-events:none}.atlas-compass span{display:grid;place-items:center;width:25px;height:25px;border:1px solid var(--atlas-map-border);border-radius:50%;font-size:16px}.atlas-hover{position:absolute;width:210px;background:#ffffff;color:#17212f;border:1px solid #dfe5ee;border-radius:5px;padding:10px 12px;pointer-events:none;box-shadow:0 5px 18px #11131826}.atlas-hover small{display:block;font-size:8px;letter-spacing:.06em;color:#4c5c70;margin-bottom:5px}.atlas-hover strong{display:block;font-size:11px;line-height:1.5;font-weight:600}.memory-diamond{display:inline-block;width:5px;height:5px;background:#c29052;transform:rotate(45deg);margin-left:8px}.atlas-zoom output{width:37px}.atlas-load-state{padding:95px 24px;text-align:center;min-height:550px}.atlas-load-orbit{font:60px Georgia,serif;color:var(--vp-c-brand-1)}.atlas-load-state h2{font:30px 'Newsreader Variable',Georgia,serif;margin:22px 0 12px}.atlas-load-state p{font-size:12px;color:var(--vp-c-text-2)}.atlas-load-state button{margin-top:20px;border:1px solid var(--vp-c-border);border-radius:5px;padding:9px 15px;font-size:12px}.atlas-inspector{max-height:710px;overflow-wrap:anywhere}.inspector-topline>span:last-child{white-space:nowrap}.atlas-inspector h2{line-height:1.09;margin-bottom:16px}.inspector-layer.memory-layer{color:#a15c00;background:#c290520d;border-color:#c2905240}.memory-layer i{border-radius:0;transform:rotate(45deg)}.inspector-description{white-space:pre-wrap}.inspector-statement{margin-top:14px;font-size:10px;color:var(--vp-c-text-2)}.inspector-statement summary{cursor:pointer;color:var(--vp-c-brand-1)}.inspector-statement pre,.inspector-memory-provenance pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:10px;line-height:1.7;max-height:300px;overflow-y:auto;padding:10px;background:var(--vp-c-bg-soft);border-radius:4px}.inspector-statement small{font-size:9px;line-height:1.7;display:block}.inspector-connections h3{display:flex;justify-content:space-between;gap:8px}.inspector-connections button.more-connections{font-size:10px;color:var(--vp-c-brand-1);justify-content:center;padding:12px 0}.inspector-no-connections{color:var(--vp-c-text-3);font-size:10px;line-height:1.7;margin-top:18px}.inspector-memory-link{display:block;text-align:left;width:100%;padding:10px 0;border-top:1px solid var(--vp-c-divider)}.inspector-memory-link strong{display:block;font-size:11px}.inspector-memory-link small{display:block;color:var(--vp-c-text-3);font-size:9px}.inspector-memory-provenance{border:1px solid #c2905240;border-radius:5px;padding:12px;margin:18px 0;font-size:10px}.inspector-memory-provenance>span{display:block;font-size:9px;color:var(--vp-c-text-3);margin:5px 0 10px}.atlas-list-wrapper{height:620px;display:flex;flex-direction:column}.atlas-node-list{min-height:0;max-height:none;flex:1}.atlas-node-list>button>span:nth-child(2){min-width:0}.atlas-node-list strong{overflow-wrap:anywhere}.atlas-node-list .memory-dot{border-radius:0;transform:rotate(45deg)}.atlas-pagination{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:11px 15px;border-top:1px solid var(--vp-c-divider);font-size:10px}.atlas-pagination>span{font-size:9px;color:var(--vp-c-text-3)}.atlas-pagination button{padding:5px 7px;border:1px solid var(--vp-c-divider);border-radius:4px}.atlas-pagination button:disabled{cursor:default;opacity:.4}.atlas-caption>span:last-child{font-size:10px;letter-spacing:0;flex-shrink:1}.atlas-caption a{color:var(--vp-c-brand-1);text-decoration:underline;text-underline-offset:2px}
@media(min-width:1600px){.atlas-explorer{grid-template-columns:minmax(0,1fr) 320px}}
@media(max-width:1150px){.research-atlas{padding:36px 26px}.atlas-header{gap:25px}.atlas-numberplate{gap:16px}.atlas-numberplate strong{font-size:32px}.atlas-toolbar{flex-wrap:wrap}.atlas-explorer{grid-template-columns:minmax(0,1fr) 265px}.atlas-search{width:220px}.atlas-pan-hint{display:none}}
@media(max-width:800px){.atlas-intro h1{font-size:46px}.atlas-numberplate{width:100%;justify-content:space-between}.atlas-numberplate strong{font-size:33px}.atlas-toolbar{padding:12px}.atlas-map-title{width:100%}.atlas-filters{width:100%;justify-content:flex-start}.atlas-search{flex:1}.atlas-explorer{grid-template-columns:minmax(0,1fr)}.atlas-viewport,.atlas-list-wrapper{height:500px}.atlas-inspector{max-height:none}.inspector-description{max-height:300px;overflow-y:auto}}
@media(max-width:540px){.research-atlas{padding:28px 16px}.atlas-intro h1{font-size:38px}.atlas-numberplate strong{font-size:30px}.atlas-numberplate span{font-size:8px}.atlas-search{width:100%}.atlas-filters select{max-width:none;min-height:37px}.atlas-list-toggle{min-height:37px}.atlas-viewport,.atlas-list-wrapper{height:425px}.atlas-map-badge{top:12px;left:12px;padding:8px 10px;font-size:10px}.atlas-map-badge small{font-size:8px}.atlas-map-hint{left:10px;bottom:13px;max-width:calc(100% - 45px);font-size:8px}.atlas-compass{display:none}.atlas-zoom button{min-height:30px}.atlas-pagination{padding:10px 8px;font-size:10px}.atlas-pagination>span{font-size:8px}}

.atlas-viewport{flex:1 0 620px;height:auto}.atlas-map-footer{margin-top:0}
@media(max-width:800px){.atlas-viewport{flex-basis:500px;min-height:500px}}
@media(max-width:540px){.atlas-viewport{flex-basis:425px;min-height:425px}}
</style>
