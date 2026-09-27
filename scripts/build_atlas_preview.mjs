// Deterministic, explicitly sampled homepage illustration of the real catalogue.
// Run after importing source data; every displayed point retains its source ID.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const source = JSON.parse(fs.readFileSync(path.join(root, 'docs/public/research/informal-atlas.json'), 'utf8'))
const target = path.join(root, 'docs/.vitepress/theme/atlasPreviewData.json')
const palette = ['#9bc8a1', '#b7acdc', '#88cbd0', '#d8b987', '#8daee0', '#d89f96', '#bacf88']
let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
for (const node of source.nodes) {
  minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x)
  minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y)
}
const scale = Math.min(504 / Math.max(1, maxX - minX), 310 / Math.max(1, maxY - minY))
const x = value => Number((274 + (value - (minX + maxX) / 2) * scale).toFixed(2))
const y = value => Number((172 + (value - (minY + maxY) / 2) * scale).toFixed(2))
const clusters = source.clusters.map((cluster, index) => ({
  id: cluster.id, title: cluster.title, x: x(cluster.x), y: y(cluster.y),
  color: palette[index % palette.length], count: cluster.count,
}))
const nodes = []
for (const cluster of clusters) {
  const members = source.nodes.filter(node => node.cluster === cluster.id)
  const sampleSize = Math.min(18, members.length)
  for (let index = 0; index < sampleSize; index++) {
    const node = members[Math.floor(index * members.length / sampleSize)]
    nodes.push({ id: node.id, x: x(node.x), y: y(node.y), color: cluster.color })
  }
}
const byId = new Map(source.nodes.map(node => [node.id, node]))
const crossReferences = new Map()
for (const edge of source.edges) {
  const a = byId.get(edge.source).cluster, b = byId.get(edge.target).cluster
  if (a === b) continue
  const key = a + '\0' + b
  if (!crossReferences.has(key)) crossReferences.set(key, { source: a, target: b, count: 0 })
  crossReferences.get(key).count++
}
const edges = [...crossReferences.values()].sort((a, b) => b.count - a.count || a.source.localeCompare(b.source) || a.target.localeCompare(b.target)).slice(0, 32)
const preview = { count: source.nodes.length, fields: clusters.length, edgeCount: source.edges.length,
  sourceRevision: source.source.revision, sampleCount: nodes.length, nodes, clusters, edges }
fs.writeFileSync(target, JSON.stringify(preview) + '\n')
console.log(JSON.stringify({ sourceNodes: source.nodes.length, previewNodes: nodes.length, fields: clusters.length, target }))
