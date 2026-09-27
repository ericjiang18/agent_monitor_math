// Canvas keeps the complete source catalogue out of the DOM. Geometry is immutable;
// only the camera, filters, and the selected neighbourhood change on interaction.
export const palette = ['#9bc8a1', '#b7acdc', '#88cbd0', '#d8b987', '#8daee0', '#d89f96', '#bacf88']
const lightPalette = ['#4b8253', '#8066aa', '#31838c', '#98732b', '#4c75af', '#a66359', '#6f8337']
export function graphColor(color, dark) {
  return dark ? color : (lightPalette[palette.indexOf(color)] || (color === '#f2bd72' ? '#a16207' : color))
}

export function prepareGraph(atlas, memories = []) {
  const nodes = atlas.nodes.map((node, index) => ({ ...node, index, memory: false }))
  const byId = new Map(nodes.map(node => [node.id, node]))
  const grouped = new Map()
  for (const node of nodes) {
    if (!grouped.has(node.area)) grouped.set(node.area, [])
    grouped.get(node.area).push(node)
  }
  const clusters = [...grouped].map(([title, items], index) => {
    const column = index % 6, row = Math.floor(index / 6)
    items.forEach((node, position) => {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) {
        const angle = position * 2.399963, radius = Math.sqrt(position + 1) * 11
        node.x = column * 1250 + Math.cos(angle) * radius
        node.y = row * 1120 + Math.sin(angle) * radius
      }
      node.color = palette[index % palette.length]
    })
    const x = items.reduce((sum, item) => sum + item.x, 0) / items.length
    const y = items.reduce((sum, item) => sum + item.y, 0) / items.length
    const bounds = boundsFor(items)
    return { title, x, y, count: items.length, color: palette[index % palette.length], bounds }
  })
  const edges = atlas.edges.filter(edge => byId.has(edge.source) && byId.has(edge.target)).map(edge => ({ ...edge, a: byId.get(edge.source), b: byId.get(edge.target) }))
  const memoryNodes = []
  const originalBounds = boundsFor(nodes)
  for (const memory of memories.filter(item => item.layer !== 'formal')) {
    const parent = byId.get(memory.node_id)
    const index = memoryNodes.length
    const angle = index * 2.399963
    const radius = 65 + Math.sqrt(index + 1) * 17
    const node = {
      id: `memory:${memory.id}`, title: memory.title || 'Research memory',
      summary: memory.content, area: 'Research memory', layer: 'informal', kind: 'Memory',
      x: parent ? parent.x + Math.cos(angle) * radius : originalBounds.maxX + 240 + (index % 8) * 48,
      y: parent ? parent.y + Math.sin(angle) * radius : originalBounds.minY + 100 + Math.floor(index / 8) * 48,
      color: '#f2bd72', memory: true, record: memory, index: nodes.length + index
    }
    memoryNodes.push(node)
    byId.set(node.id, node)
    if (parent) edges.push({ source: parent.id, target: node.id, a: parent, b: node, kind: 'memory' })
  }
  nodes.push(...memoryNodes)
  const adjacency = new Map(nodes.map(node => [node.id, []]))
  for (const edge of edges) {
    adjacency.get(edge.source).push({ node: edge.b, direction: edge.kind === 'memory' ? 'Research memory' : 'Used by', edge })
    adjacency.get(edge.target).push({ node: edge.a, direction: edge.kind === 'memory' ? 'Attached to' : 'References', edge })
  }
  const crossLinks = new Map(), clustersByTitle = new Map(clusters.map(cluster => [cluster.title, cluster]))
  for (const edge of edges) {
    if (edge.a.area === edge.b.area || edge.kind === 'memory') continue
    const key = [edge.a.area, edge.b.area].sort().join('\u0000')
    if (!crossLinks.has(key)) crossLinks.set(key, { a: clustersByTitle.get(edge.a.area), b: clustersByTitle.get(edge.b.area), count: 0 })
    crossLinks.get(key).count++
  }
  return { nodes, byId, edges, adjacency, clusters, crossLinks: [...crossLinks.values()], bounds: boundsFor(nodes), catalogueCount: atlas.nodes.length }
}

export function boundsFor(nodes) {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
  for (const node of nodes) {
    minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x)
    minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y)
  }
  return nodes.length ? { minX, minY, maxX, maxY } : { minX: 0, minY: 0, maxX: 100, maxY: 100 }
}

export function drawGraph(canvas, graph, camera, options) {
  const start = performance.now(), ctx = canvas.getContext('2d')
  if (!ctx) return
  const { width, height, ratio } = options
  const dark = options.dark !== false
  const colors = dark
    ? { edge: '#94a3b833', cross: '153, 176, 210', link: '#a7c8ffb0', arrow: '#a7c8ff', selected: '#f1f5ff', ring: '#c6dcff80', labelBg: '#151922ee', label: '#dce7f7' }
    : { edge: '#647b9a40', cross: '80, 108, 148', link: '#2563ebaa', arrow: '#2563eb', selected: '#1d4ed8', ring: '#2563eb80', labelBg: '#fffffff0', label: '#334155' }
  const { x: cameraX, y: cameraY, scale } = camera
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
  ctx.clearRect(0, 0, width, height)
  const px = x => x * scale + cameraX, py = y => y * scale + cameraY
  const onScreen = node => px(node.x) >= -12 && px(node.x) <= width + 12 && py(node.y) >= -12 && py(node.y) <= height + 12
  const active = node => (!options.area || node.area === options.area) && (!options.matches || options.matches.has(node.id))
  const neighbourhood = new Set([options.selected, ...(graph.adjacency.get(options.selected) || []).map(item => item.node.id)])
  const selected = graph.byId.get(options.selected)
  const detailed = scale >= .5 || Boolean(options.area)
  let visible = 0, renderedEdges = 0

  ctx.lineWidth = 1
  if (!detailed) {
    for (const edge of graph.crossLinks) {
      if (!edge.a || !edge.b) continue
      ctx.strokeStyle = `rgba(${colors.cross}, ${Math.min(.14, .035 + Math.log1p(edge.count) * .012)})`
      ctx.beginPath(); ctx.moveTo(px(edge.a.x), py(edge.a.y))
      ctx.quadraticCurveTo(px((edge.a.x + edge.b.x) / 2), py(Math.min(edge.a.y, edge.b.y) - 100), px(edge.b.x), py(edge.b.y)); ctx.stroke()
    }
  } else {
    ctx.beginPath()
    for (const edge of graph.edges) {
      if ((!onScreen(edge.a) && !onScreen(edge.b)) || !active(edge.a) || !active(edge.b)) continue
      ctx.moveTo(px(edge.a.x), py(edge.a.y)); ctx.lineTo(px(edge.b.x), py(edge.b.y)); renderedEdges++
    }
    ctx.strokeStyle = colors.edge; ctx.lineWidth = .65; ctx.stroke()
  }

  // Batch same-colour circles, drawing every on-screen node, including filters'
  // dimmed context. There is no random sampling or truncated node count.
  for (const color of [...palette, '#f2bd72']) {
    for (const bright of [false, true]) {
      ctx.beginPath()
      for (const node of graph.nodes) {
        if (node.memory || node.color !== color || !onScreen(node) || active(node) !== bright) continue
        const radius = Math.max(.65, Math.min(3.4, 1.05 + scale * 1.3))
        ctx.moveTo(px(node.x) + radius, py(node.y)); ctx.arc(px(node.x), py(node.y), radius, 0, Math.PI * 2)
        if (bright) visible++
      }
      ctx.fillStyle = graphColor(color, dark); ctx.globalAlpha = bright ? .86 : .09; ctx.fill()
    }
  }
  ctx.globalAlpha = 1
  // Diamonds remain distinguishable from literature nodes, even when tiny.
  for (const node of graph.nodes) {
    if (!node.memory || !onScreen(node)) continue
    const x = px(node.x), y = py(node.y), radius = Math.max(3.5, Math.min(7, scale * 5))
    ctx.beginPath(); ctx.moveTo(x, y - radius); ctx.lineTo(x + radius, y); ctx.lineTo(x, y + radius); ctx.lineTo(x - radius, y); ctx.closePath()
    ctx.fillStyle = graphColor('#f2bd72', dark); ctx.globalAlpha = active(node) ? 1 : .14; ctx.fill()
    if (active(node)) visible++
  }
  ctx.globalAlpha = 1
  if (selected && active(selected)) {
    for (const item of graph.adjacency.get(selected.id) || []) {
      const { a, b } = item.edge
      if (!onScreen(a) && !onScreen(b)) continue
      const ax = px(a.x), ay = py(a.y), bx = px(b.x), by = py(b.y)
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by)
      ctx.strokeStyle = item.edge.kind === 'memory' ? graphColor('#f2bd72', dark) + 'b0' : colors.link; ctx.lineWidth = 1.2; ctx.stroke()
      const angle = Math.atan2(by - ay, bx - ax), arrow = 4
      if (Math.hypot(bx - ax, by - ay) > 16) {
        const tx = bx - Math.cos(angle) * 6, ty = by - Math.sin(angle) * 6
        ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(tx - Math.cos(angle - .5) * arrow, ty - Math.sin(angle - .5) * arrow); ctx.lineTo(tx - Math.cos(angle + .5) * arrow, ty - Math.sin(angle + .5) * arrow); ctx.closePath(); ctx.fillStyle = colors.arrow; ctx.fill()
      }
      renderedEdges++
    }
    for (const id of neighbourhood) {
      const node = graph.byId.get(id)
      if (!node || !onScreen(node)) continue
      ctx.beginPath(); ctx.arc(px(node.x), py(node.y), id === selected.id ? 6 : 3, 0, Math.PI * 2)
      ctx.fillStyle = id === selected.id ? colors.selected : graphColor(node.color, dark); ctx.fill()
      if (id === selected.id) { ctx.beginPath(); ctx.arc(px(node.x), py(node.y), 10, 0, Math.PI * 2); ctx.strokeStyle = colors.ring; ctx.lineWidth = 1; ctx.stroke() }
    }
  }
  if (!options.area && scale < .5) {
    ctx.textAlign = 'center'
    const labels = []
    for (const cluster of graph.clusters) {
      const x = px(cluster.x), y = py(cluster.bounds.maxY) + 16
      if (x < -100 || x > width + 100 || y < 0 || y > height + 24) continue
      const title = cluster.title.length > 28 ? cluster.title.slice(0, 26) + '…' : cluster.title
      ctx.font = '500 10px "Space Grotesk Variable", sans-serif'
      const measure = ctx.measureText(title).width
      const box = { left: x - measure / 2 - 7, right: x + measure / 2 + 7, top: y - 12, bottom: y + 6 }
      if (box.left < 3 || box.right > width - 3 || labels.some(other => box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top)) continue
      labels.push(box)
      ctx.fillStyle = colors.labelBg; ctx.fillRect(x - measure / 2 - 5, y - 10, measure + 10, 15)
      ctx.fillStyle = colors.label; ctx.fillText(title, x, y)
    }
  }
  if (selected && onScreen(selected) && active(selected) && detailed) {
    ctx.font = '500 12px "Space Grotesk Variable", sans-serif'; ctx.textAlign = 'left'
    const title = selected.title.length > 44 ? selected.title.slice(0, 42) + '…' : selected.title
    const labelWidth = Math.min(width - 24, ctx.measureText(title).width + 20)
    const x = Math.max(8, Math.min(width - labelWidth - 8, px(selected.x) + 15)), y = Math.max(25, py(selected.y) - 12)
    ctx.fillStyle = '#ffffff'; ctx.beginPath(); ctx.roundRect(x, y - 17, labelWidth, 28, 5); ctx.fill()
    ctx.fillStyle = '#17212f'; ctx.fillText(title, x + 10, y + 1, labelWidth - 20)
  }
  canvas.dataset.nodeCount = String(graph.nodes.length)
  canvas.dataset.catalogueCount = String(graph.catalogueCount)
  canvas.dataset.visibleCount = String(visible)
  canvas.dataset.renderedEdges = String(renderedEdges)
  canvas.dataset.drawMs = String(Math.round((performance.now() - start) * 10) / 10)
}

export function hitNode(graph, camera, x, y, area = '') {
  const worldX = (x - camera.x) / camera.scale, worldY = (y - camera.y) / camera.scale
  let closest = null, distance = (12 / camera.scale) ** 2
  for (const node of graph.nodes) {
    if (area && node.area !== area) continue
    const candidate = (node.x - worldX) ** 2 + (node.y - worldY) ** 2
    if (candidate < distance) { closest = node; distance = candidate }
  }
  return closest
}
