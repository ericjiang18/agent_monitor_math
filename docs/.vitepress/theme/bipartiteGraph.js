import { boundsFor, graphColor, palette } from './informalGraph.js'

// The two node sets share project slots. Formalization links cross the gutter;
// optional reference links stay within the informal side.
// Layout is deterministic and computed once; interaction only redraws the canvas.
const goldenAngle = 2.399963229728653
const unresolvedColor = '#f2bd72'
const validLayer = layer => layer === 'informal' || layer === 'formal' || layer === 'unresolved'

function hash(value) {
  let result = 2166136261
  for (const character of String(value)) result = Math.imul(result ^ character.charCodeAt(0), 16777619)
  return result >>> 0
}

function packProjects(groups) {
  // Variable-size shelves keep dozens of projects in a compact landscape. Using
  // the larger side for each slot keeps corresponding projects level with one another.
  for (const group of groups) {
    group.radius = Math.max(34, Math.sqrt(Math.max(group.informal.length, group.formal.length)) * 9.5)
    group.size = group.radius * 2 + 76
  }
  const totalArea = groups.reduce((sum, group) => sum + group.size ** 2, 0)
  const width = Math.max(...groups.map(group => group.size), Math.sqrt(totalArea) * .94, 200)
  const rows = []
  let row = { groups: [], width: 0, height: 0 }
  for (const group of groups) {
    if (row.groups.length && row.width + group.size > width) { rows.push(row); row = { groups: [], width: 0, height: 0 } }
    group.slotX = row.width + group.size / 2
    row.groups.push(group); row.width += group.size; row.height = Math.max(row.height, group.size)
  }
  if (row.groups.length) rows.push(row)
  let y = 0
  const actualWidth = Math.max(200, ...rows.map(item => item.width))
  for (const item of rows) {
    for (const group of item.groups) {
      group.slotX += (actualWidth - item.width) / 2
      group.slotY = y + item.height / 2
    }
    y += item.height
  }
  for (const group of groups) group.slotY -= y / 2
  return { width: actualWidth, height: y, gutter: Math.max(210, actualWidth * .14) }
}

export function prepareBipartite(data, { project = '', includeUnresolved = false } = {}) {
  const projects = new Map((data.projects || []).map(item => [item.id, item]))
  const inputNodes = (data.nodes || []).filter(node => validLayer(node.layer) && (includeUnresolved || node.layer !== 'unresolved'))
  const inputById = new Map(inputNodes.map(node => [node.id, node]))
  const inputEdges = (data.edges || []).filter(edge => inputById.get(edge.source)?.layer === 'informal' && ['formal', 'unresolved'].includes(inputById.get(edge.target)?.layer))
  const included = project ? new Set(inputNodes.filter(node => node.project === project).map(node => node.id)) : null
  if (included) for (const edge of inputEdges) if (inputById.get(edge.source).project === project) included.add(edge.target)
  const nodes = inputNodes.filter(node => !included || included.has(node.id)).map((node, index) => ({ ...node, index, x: 0, y: 0, color: '', layoutProject: '', area: '' }))
  const byId = new Map(nodes.map(node => [node.id, node]))
  const edges = inputEdges.filter(edge => byId.has(edge.source) && byId.has(edge.target)).map(edge => ({ ...edge, a: byId.get(edge.source), b: byId.get(edge.target) }))

  // Shared Mathlib declarations have no project. Place each with the project
  // supplying most of its links, while retaining its original ownership metadata.
  const incomingProjects = new Map()
  for (const edge of edges) {
    if (edge.b.project && edge.b.project !== 'shared' && (!project || edge.b.project === project)) continue
    if (!incomingProjects.has(edge.target)) incomingProjects.set(edge.target, new Map())
    const votes = incomingProjects.get(edge.target), owner = edge.a.project || 'shared'
    votes.set(owner, (votes.get(owner) || 0) + 1)
  }
  const groupsById = new Map()
  for (const node of nodes) {
    const votes = incomingProjects.get(node.id)
    const inferredProject = votes ? [...votes].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0] : ''
    const owner = project || (node.project !== 'shared' && node.project) || inferredProject || 'shared'
    if (!groupsById.has(owner)) groupsById.set(owner, { id: owner, project: owner, title: projects.get(owner)?.title || (owner === 'shared' ? 'Shared declarations' : owner), informal: [], formal: [], count: 0 })
    const group = groupsById.get(owner)
    node.layoutProject = owner; node.area = group.title
    group[node.layer === 'informal' ? 'informal' : 'formal'].push(node); group.count++
  }
  const groups = [...groupsById.values()].sort((a, b) => Math.max(b.informal.length, b.formal.length) - Math.max(a.informal.length, a.formal.length) || a.id.localeCompare(b.id))
  const layout = packProjects(groups)
  const clusters = []
  for (const group of groups) {
    const color = palette[hash(group.id) % palette.length]
    const cluster = { id: group.id, project: group.project, title: group.title, color, count: group.count, informalCount: group.informal.length, formalCount: group.formal.length }
    for (const layer of ['informal', 'formal']) {
      const items = group[layer], direction = layer === 'informal' ? -1 : 1
      const x = direction * (layout.gutter / 2 + group.slotX), y = group.slotY
      const radius = Math.sqrt(items.length) * 9.5
      const rotation = hash(group.id + layer) / 4294967296 * Math.PI * 2
      for (let index = 0; index < items.length; index++) {
        const node = items[index], angle = index * goldenAngle + rotation
        const distance = Math.sqrt(index + .5) * 9.5
        node.x = x + Math.cos(angle) * distance; node.y = y + Math.sin(angle) * distance
        node.color = node.layer === 'unresolved' ? unresolvedColor : color
      }
      cluster[layer] = { x, y, radius, count: items.length, bounds: boundsFor(items) }
    }
    cluster.x = 0; cluster.y = group.slotY
    cluster.bounds = boundsFor([...group.informal, ...group.formal])
    clusters.push(cluster)
  }

  const adjacency = new Map(nodes.map(node => [node.id, []]))
  const bundlesByKey = new Map(), drawGroupsByKey = new Map()
  for (const edge of edges) {
    const unresolved = edge.b.layer === 'unresolved'
    adjacency.get(edge.source).push({ node: edge.b, direction: unresolved ? 'Unresolved Lean reference' : 'Formal declaration', edge })
    adjacency.get(edge.target).push({ node: edge.a, direction: 'Informal statement', edge })
    const key = `${edge.a.layoutProject}\u0000${edge.b.layoutProject}\u0000${unresolved}`
    if (!bundlesByKey.has(key)) bundlesByKey.set(key, { ax: 0, ay: 0, bx: 0, by: 0, count: 0, unresolved })
    const bundle = bundlesByKey.get(key)
    bundle.ax += edge.a.x; bundle.ay += edge.a.y; bundle.bx += edge.b.x; bundle.by += edge.b.y; bundle.count++
  }
  for (const bundle of bundlesByKey.values()) for (const axis of ['ax', 'ay', 'bx', 'by']) bundle[axis] /= bundle.count
  for (const node of nodes) {
    const key = `${node.layer}\u0000${node.color}`
    if (!drawGroupsByKey.has(key)) drawGroupsByKey.set(key, { layer: node.layer, color: node.color, nodes: [] })
    drawGroupsByKey.get(key).nodes.push(node)
  }
  const informalCount = nodes.filter(node => node.layer === 'informal').length
  const formalCount = nodes.filter(node => node.layer === 'formal').length
  const unresolvedCount = nodes.length - informalCount - formalCount
  return {
    nodes, byId, edges, adjacency, dependencies: [], dependencyAdjacency: new Map(nodes.map(node => [node.id, []])),
    clusters, bounds: boundsFor(nodes), catalogueCount: nodes.length,
    informalCount, formalCount, unresolvedCount, linkCount: edges.length,
    unlinkedCount: nodes.filter(node => !adjacency.get(node.id).length).length,
    project, includeUnresolved, bundles: [...bundlesByKey.values()], drawGroups: [...drawGroupsByKey.values()],
    partitions: { informal: { minX: -layout.gutter / 2 - layout.width, maxX: -layout.gutter / 2 }, formal: { minX: layout.gutter / 2, maxX: layout.gutter / 2 + layout.width }, gutter: layout.gutter },
    screenX: new Float32Array(nodes.length), screenY: new Float32Array(nodes.length), screenFlags: new Uint8Array(nodes.length)
  }
}

// Dependency data is loaded separately. Attaching it never changes node geometry,
// the existing formalization links, or their adjacency and counts.
export function attachDependencies(graph, edges) {
  const dependencyAdjacency = new Map(graph.nodes.map(node => [node.id, []]))
  const dependencies = []
  for (const edge of edges || []) {
    const a = graph.byId.get(edge.source), b = graph.byId.get(edge.target)
    if (a?.layer !== 'informal' || b?.layer !== 'informal' || !['USES', 'PROVES'].includes(edge.type)) continue
    const dependency = { ...edge, a, b }
    dependencies.push(dependency)
    dependencyAdjacency.get(a.id).push({ node: b, direction: edge.type === 'PROVES' ? 'Proves' : 'References', edge: dependency })
    dependencyAdjacency.get(b.id).push({ node: a, direction: edge.type === 'PROVES' ? 'Proof entry' : 'Referenced by', edge: dependency })
  }
  return { ...graph, dependencies, dependencyAdjacency }
}

function curve(ctx, ax, ay, bx, by) {
  const bend = (bx - ax) * .42
  ctx.moveTo(ax, ay); ctx.bezierCurveTo(ax + bend, ay, bx - bend, by, bx, by)
}

function dependencyGeometry(edge, screenX, screenY, scale) {
  const ax = screenX[edge.a.index], ay = screenY[edge.a.index], bx = screenX[edge.b.index], by = screenY[edge.b.index]
  const distance = Math.hypot(bx - ax, by - ay)
  const loop = edge.a === edge.b || distance < .5
  const bend = loop ? Math.max(13, Math.min(30, scale * 20)) : Math.max(9, Math.min(100, distance * .22))
  // Both controls lie left of the endpoints. The complete Bezier curve therefore
  // stays within the informal half, even for cross-project links and self links.
  const controlX = Math.min(ax, bx) - bend
  const lift = loop ? bend : Math.abs(by - ay) < 3 ? bend * .6 : 0
  return { ax, ay, bx, by, c1x: controlX, c1y: ay - lift, c2x: controlX, c2y: by + (loop ? lift : -lift) }
}

function dependencyCurve(ctx, curve) {
  ctx.moveTo(curve.ax, curve.ay)
  ctx.bezierCurveTo(curve.c1x, curve.c1y, curve.c2x, curve.c2y, curve.bx, curve.by)
}

function dependencyArrow(ctx, curve, radius) {
  const dx = curve.bx - curve.c2x, dy = curve.by - curve.c2y, length = Math.hypot(dx, dy) || 1
  const ux = dx / length, uy = dy / length
  const x = curve.bx - ux * (radius + 2), y = curve.by - uy * (radius + 2)
  ctx.beginPath(); ctx.moveTo(x, y)
  ctx.lineTo(x - ux * 6 - uy * 2.7, y - uy * 6 + ux * 2.7)
  ctx.lineTo(x - ux * 6 + uy * 2.7, y - uy * 6 - ux * 2.7)
  ctx.closePath(); ctx.fill()
}

function shape(ctx, node, x, y, radius) {
  if (node.layer === 'informal') { ctx.moveTo(x + radius, y); ctx.arc(x, y, radius, 0, Math.PI * 2) }
  else ctx.rect(x - radius, y - radius, radius * 2, radius * 2)
}

export function drawBipartite(canvas, graph, camera, options) {
  const start = performance.now(), ctx = canvas.getContext('2d')
  if (!ctx) return
  const { width, height, ratio } = options, dark = options.dark !== false
  const { x: cameraX, y: cameraY, scale } = camera
  const colors = dark
    ? { edge: '#91a8c126', bundle: '150, 172, 202', highlight: '#a7c8ff', selected: '#eef5ff', label: '#dce7f7', labelBg: '#151922e8', unresolved: '#f2bd72', reference: '#5dd6c5', proof: '#c7a0ff' }
    : { edge: '#506e9321', bundle: '70, 102, 145', highlight: '#2563eb', selected: '#1d4ed8', label: '#334155', labelBg: '#fffffff0', unresolved: '#a16207', reference: '#0f8074', proof: '#8246bd' }
  const px = x => x * scale + cameraX, py = y => y * scale + cameraY
  const { screenX, screenY, screenFlags } = graph
  let visible = 0, renderedNodes = 0, renderedEdges = 0, renderedBundles = 0, renderedDependencies = 0
  for (const node of graph.nodes) {
    const i = node.index, x = px(node.x), y = py(node.y)
    screenX[i] = x; screenY[i] = y
    const onScreen = x >= -12 && x <= width + 12 && y >= -12 && y <= height + 12
    const active = !options.matches || options.matches.has(node.id)
    screenFlags[i] = (onScreen ? 1 : 0) | (active ? 2 : 0)
    if (onScreen) { renderedNodes++; if (active) visible++ }
  }
  const edgeOnScreen = (ax, ay, bx, by) => Math.max(ax, bx) >= -12 && Math.min(ax, bx) <= width + 12 && Math.max(ay, by) >= -12 && Math.min(ay, by) <= height + 12
  const dependencyOnScreen = curve => edgeOnScreen(
    Math.min(curve.ax, curve.bx, curve.c1x, curve.c2x), Math.min(curve.ay, curve.by, curve.c1y, curve.c2y),
    Math.max(curve.ax, curve.bx, curve.c1x, curve.c2x), Math.max(curve.ay, curve.by, curve.c1y, curve.c2y)
  )
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height)
  ctx.globalAlpha = 1; ctx.setLineDash([])
  const detailed = scale >= .45 || graph.nodes.length < 1800 || Boolean(options.matches)
  const selected = graph.byId.get(options.selected)
  const selectedActive = selected && (screenFlags[selected.index] & 2)
  const showDependencies = Boolean(options.showDependencies)

  if (!detailed) {
    for (const bundle of graph.bundles) {
      const ax = px(bundle.ax), ay = py(bundle.ay), bx = px(bundle.bx), by = py(bundle.by)
      if (!edgeOnScreen(ax, ay, bx, by)) continue
      ctx.beginPath(); curve(ctx, ax, ay, bx, by)
      ctx.setLineDash(bundle.unresolved ? [4, 4] : [])
      ctx.strokeStyle = bundle.unresolved ? colors.unresolved : `rgba(${colors.bundle},${Math.min(.23, .05 + Math.log1p(bundle.count) * .021)})`
      ctx.globalAlpha = bundle.unresolved ? .16 : 1
      ctx.lineWidth = Math.min(4, .55 + Math.log1p(bundle.count) * .38); ctx.stroke(); renderedBundles++
    }
    ctx.globalAlpha = 1; ctx.setLineDash([])
  } else {
    for (const unresolved of [false, true]) {
      ctx.beginPath()
      for (const edge of graph.edges) {
        if ((edge.b.layer === 'unresolved') !== unresolved || !(screenFlags[edge.a.index] & 2) || !(screenFlags[edge.b.index] & 2)) continue
        const ax = screenX[edge.a.index], ay = screenY[edge.a.index], bx = screenX[edge.b.index], by = screenY[edge.b.index]
        if (!edgeOnScreen(ax, ay, bx, by)) continue
        curve(ctx, ax, ay, bx, by); renderedEdges++
      }
      ctx.setLineDash(unresolved ? [4, 4] : [])
      ctx.strokeStyle = unresolved ? colors.unresolved : colors.edge; ctx.globalAlpha = unresolved ? .17 : 1; ctx.lineWidth = .65; ctx.stroke()
    }
    ctx.globalAlpha = 1; ctx.setLineDash([])
  }

  // The full atlas overview keeps its existing silhouette. Close zooms and
  // individual projects add faint context; a selection is readable at any zoom.
  if (showDependencies && (scale >= .75 || graph.project)) {
    for (const type of ['USES', 'PROVES']) {
      ctx.beginPath()
      for (const edge of graph.dependencies || []) {
        if (edge.type !== type || !(screenFlags[edge.a.index] & 2) || !(screenFlags[edge.b.index] & 2)) continue
        if (selectedActive && (edge.a === selected || edge.b === selected)) continue
        const geometry = dependencyGeometry(edge, screenX, screenY, scale)
        if (!dependencyOnScreen(geometry)) continue
        dependencyCurve(ctx, geometry); renderedDependencies++
      }
      ctx.strokeStyle = type === 'PROVES' ? colors.proof : colors.reference
      ctx.lineWidth = type === 'PROVES' ? .9 : .65
      ctx.globalAlpha = selectedActive ? .055 : type === 'PROVES' ? .21 : .12
      ctx.stroke()
    }
    ctx.globalAlpha = 1
  }

  const radius = Math.max(.68, Math.min(3.2, .7 + scale * 1.55))
  for (const group of graph.drawGroups) {
    for (const active of [false, true]) {
      ctx.beginPath()
      for (const node of group.nodes) {
        const flag = screenFlags[node.index]
        if (!(flag & 1) || Boolean(flag & 2) !== active) continue
        shape(ctx, node, screenX[node.index], screenY[node.index], group.layer === 'unresolved' ? Math.max(1.2, radius) : radius)
      }
      ctx.globalAlpha = active ? .86 : .09
      if (group.layer === 'unresolved') { ctx.strokeStyle = colors.unresolved; ctx.lineWidth = .85; ctx.stroke() }
      else { ctx.fillStyle = graphColor(group.color, dark); ctx.fill() }
    }
  }
  ctx.globalAlpha = 1

  if (selectedActive) {
    const neighbours = new Set([selected])
    const dependencyColours = new Map()
    for (const item of graph.adjacency.get(selected.id) || []) {
      const { a, b } = item.edge
      const ax = screenX[a.index], ay = screenY[a.index], bx = screenX[b.index], by = screenY[b.index]
      if (edgeOnScreen(ax, ay, bx, by)) {
        ctx.beginPath(); curve(ctx, ax, ay, bx, by)
        ctx.setLineDash(b.layer === 'unresolved' ? [4, 4] : [])
        ctx.strokeStyle = b.layer === 'unresolved' ? colors.unresolved : colors.highlight
        ctx.lineWidth = 1.25; ctx.globalAlpha = .66; ctx.stroke(); renderedEdges++
      }
      neighbours.add(item.node)
    }
    ctx.globalAlpha = 1; ctx.setLineDash([])
    if (showDependencies) {
      // A self link has incoming and outgoing inspector entries but one curve.
      const incidentEdges = new Set((graph.dependencyAdjacency?.get(selected.id) || []).map(item => item.edge))
      for (const edge of incidentEdges) {
        const color = edge.type === 'PROVES' ? colors.proof : colors.reference
        const geometry = dependencyGeometry(edge, screenX, screenY, scale)
        if (dependencyOnScreen(geometry)) {
          ctx.beginPath(); dependencyCurve(ctx, geometry)
          ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.5; ctx.globalAlpha = .86; ctx.stroke()
          dependencyArrow(ctx, geometry, edge.b === selected ? 4.2 : Math.max(2.25, radius + .6))
          renderedDependencies++
        }
        const neighbour = edge.a === selected ? edge.b : edge.a
        neighbours.add(neighbour)
        if (!dependencyColours.has(neighbour.id) || edge.type === 'PROVES') dependencyColours.set(neighbour.id, color)
      }
      ctx.globalAlpha = 1
    }
    for (const node of neighbours) {
      if (!(screenFlags[node.index] & 1)) continue
      const isSelected = node.id === selected.id, x = screenX[node.index], y = screenY[node.index]
      ctx.beginPath(); shape(ctx, node, x, y, isSelected ? 4.2 : Math.max(2.25, radius + .6))
      ctx.fillStyle = isSelected ? colors.selected : graphColor(node.color, dark)
      if (node.layer === 'unresolved') { ctx.strokeStyle = colors.unresolved; ctx.lineWidth = 1.3; ctx.stroke() } else ctx.fill()
      if (isSelected) {
        ctx.beginPath(); shape(ctx, node, x, y, 8)
        ctx.strokeStyle = colors.highlight; ctx.lineWidth = 1; ctx.globalAlpha = .55; ctx.stroke(); ctx.globalAlpha = 1
      } else if (dependencyColours.has(node.id)) {
        ctx.beginPath(); shape(ctx, node, x, y, Math.max(3.5, radius + 1.6))
        ctx.strokeStyle = dependencyColours.get(node.id); ctx.lineWidth = 1; ctx.globalAlpha = .9; ctx.stroke(); ctx.globalAlpha = 1
      }
    }
  }

  const labelBoxes = []
  if (!options.matches && scale < .7) {
    ctx.font = '500 10px "Space Grotesk Variable", sans-serif'; ctx.textAlign = 'center'
    for (const cluster of graph.clusters.slice(0, graph.project ? 1 : 12)) {
      const title = cluster.title.length > 24 ? cluster.title.slice(0, 22) + '…' : cluster.title
      const measure = ctx.measureText(title).width
      for (const layer of ['informal', 'formal']) {
        const region = cluster[layer]
        if (!region.count) continue
        const x = px(region.x), y = py(region.y + region.radius) + 15
        const box = { left: x - measure / 2 - 6, right: x + measure / 2 + 6, top: y - 11, bottom: y + 5 }
        if (box.left < 5 || box.right > width - 5 || box.top < 52 || box.bottom > height - 35 || labelBoxes.some(other => box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top)) continue
        labelBoxes.push(box); ctx.fillStyle = colors.labelBg; ctx.fillRect(box.left, box.top, box.right - box.left, box.bottom - box.top)
        ctx.fillStyle = colors.label; ctx.fillText(title, x, y)
      }
    }
  }
  if (selected && detailed && (screenFlags[selected.index] & 3) === 3) {
    ctx.font = '500 11px "Space Grotesk Variable", sans-serif'; ctx.textAlign = 'left'
    const title = selected.title.length > 48 ? selected.title.slice(0, 46) + '…' : selected.title
    const labelWidth = Math.min(width - 24, ctx.measureText(title).width + 20)
    const x = Math.max(8, Math.min(width - labelWidth - 8, screenX[selected.index] + 14)), y = Math.max(60, Math.min(height - 45, screenY[selected.index] - 13))
    ctx.fillStyle = colors.labelBg; ctx.beginPath(); ctx.roundRect(x, y - 17, labelWidth, 27, 4); ctx.fill()
    ctx.fillStyle = colors.label; ctx.fillText(title, x + 10, y, labelWidth - 20)
  }
  ctx.globalAlpha = 1; ctx.setLineDash([])
  Object.assign(canvas.dataset, {
    graphMode: showDependencies ? 'layered' : 'bipartite', nodeCount: String(graph.nodes.length), catalogueCount: String(graph.catalogueCount),
    informalCount: String(graph.informalCount), formalCount: String(graph.formalCount), unresolvedCount: String(graph.unresolvedCount),
    edgeCount: String(graph.edges.length), visibleCount: String(visible), renderedNodes: String(renderedNodes),
    renderedEdges: String(renderedEdges), renderedBundles: String(renderedBundles),
    dependencyCount: String(graph.dependencies?.length || 0), renderedDependencies: String(renderedDependencies), showDependencies: String(showDependencies), selectedId: selected?.id || '',
    drawMs: String(Math.round((performance.now() - start) * 10) / 10)
  })
}
