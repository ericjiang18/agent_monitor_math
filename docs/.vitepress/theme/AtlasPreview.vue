<script setup>
import { useData, withBase } from 'vitepress'
import { graphColor } from './informalGraph.js'
import preview from './bipartitePreviewData.json'
import bipartiteSummary from './bipartiteSummary.json'
const { isDark } = useData()
const stats = bipartiteSummary.stats
const count = value => value.toLocaleString('en-US')
const nodes = new Map(preview.nodes.map(node => [node.id, node]))
const path = edge => {
  const a = nodes.get(edge.source), b = nodes.get(edge.target), bend = (b.x - a.x) * .42
  return `M${a.x},${a.y} C${a.x+bend},${a.y} ${b.x-bend},${b.y} ${b.x},${b.y}`
}
</script>

<template>
  <section class="atlas-home-preview" aria-labelledby="atlas-preview-title">
    <div class="atlas-preview-copy">
      <p>THE RESEARCH ATLAS / INFORMAL ↔ FORMAL</p>
      <h2 id="atlas-preview-title">Two languages.<br><em>One mathematical landscape.</em></h2>
      <div>Explore mathematical statements alongside their connected Lean declarations, across {{ count(stats.projects) }} projects. Select an idea, read its source, and follow the formal reference.</div>
      <nav aria-label="Explore research"><a :href="withBase('/dag')">Explore the bipartite graph <span>↗</span></a><a :href="withBase('/dag?view=source')">Original source DAG <span>↗</span></a></nav>
      <small>{{ count(stats.informal) }} informal entries · {{ count(stats.formal) }} formal declarations<br>{{ count(stats.resolved_links) }} supplied cross-links</small>
    </div>
    <a class="atlas-preview-map" :href="withBase('/dag')" :aria-label="`Open the bipartite research atlas with ${count(stats.informal)} informal entries and ${count(stats.formal)} formal declarations`">
      <div class="preview-map-heading"><span><i></i> INFORMAL ↔ FORMAL</span><span>{{ count(stats.resolved_links) }} LINKS ↗</span></div>
      <svg viewBox="0 0 548 382" role="img" :aria-label="`Sample of ${preview.nodes.length} nodes and ${preview.edges.length} supplied cross-links. Informal statements are circles on the left; Lean declarations are squares on the right.`">
        <defs><pattern id="preview-grid" width="16" height="16" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".5" fill="#94a3b8" opacity=".2"/></pattern></defs>
        <rect width="548" height="382" fill="url(#preview-grid)"/>
        <g class="preview-partition-labels" aria-hidden="true"><circle cx="74" cy="25" r="2.5"/><text x="86" y="28">INFORMAL MATH</text><rect x="359" y="22.5" width="5" height="5"/><text x="373" y="28">FORMAL LEAN</text></g>
        <g class="preview-lines"><path v-for="edge in preview.edges" :key="edge.source + edge.target" :data-source="edge.source" :data-target="edge.target" :d="path(edge)"/></g>
        <g class="preview-informal-nodes"><circle v-for="node in preview.nodes.filter(item => item.layer === 'informal')" :key="node.id" :data-source-id="node.id" :cx="node.x" :cy="node.y" r="1.85" :fill="graphColor(node.color, isDark)" opacity=".88"/></g>
        <g class="preview-formal-nodes"><rect v-for="node in preview.nodes.filter(item => item.layer === 'formal')" :key="node.id" :data-source-id="node.id" :x="node.x - 1.85" :y="node.y - 1.85" width="3.7" height="3.7" :fill="graphColor(node.color, isDark)" opacity=".88"/></g>
        <text x="274" y="371" text-anchor="middle" class="preview-sample-label">{{ preview.nodes.length }} sampled nodes · {{ preview.edges.length }} real cross-links</text>
      </svg>
      <span>A sample of the connections. Open the map to explore every node. <b>↗</b></span>
    </a>
  </section>
</template>

<style scoped>
.atlas-home-preview{max-width:1280px;margin:0 auto;display:grid;grid-template-columns:1fr 1.1fr;align-items:center;gap:66px;padding:62px 0;border-top:1px solid var(--vp-c-divider);border-bottom:1px solid var(--vp-c-divider)}.atlas-preview-copy>p{margin:0 0 20px;font-size:10px;font-weight:650;letter-spacing:.13em;color:var(--vp-c-brand-1)}.atlas-preview-copy h2{font:400 clamp(30px,3.2vw,45px)/1.1 'Newsreader Variable',Georgia,serif;letter-spacing:-.03em;margin:0 0 20px;border:0;padding:0}.atlas-preview-copy h2 em{color:var(--vp-c-brand-1)}.atlas-preview-copy>div{font-size:13px;color:var(--vp-c-text-2);line-height:1.85;max-width:470px}.atlas-preview-copy nav{display:flex;flex-wrap:wrap;gap:12px 24px;margin:22px 0}.atlas-preview-copy nav a{color:var(--vp-c-brand-1);font-size:12px;font-weight:600;text-decoration:none;border-bottom:1px solid var(--vp-c-border);padding-bottom:5px}.atlas-preview-copy nav a span{margin-left:12px}.atlas-preview-copy small{font-size:9px;color:var(--vp-c-text-3);line-height:1.8;display:block}.atlas-preview-map{border:1px solid var(--atlas-map-border);border-radius:8px;padding:17px;background:var(--atlas-map-bg);text-decoration:none;box-shadow:0 14px 34px #15192212;overflow:hidden}.atlas-preview-map svg{width:100%;height:auto;overflow:visible;margin-top:12px}.preview-map-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--atlas-map-text);font-size:8px;letter-spacing:.1em;padding-bottom:12px;border-bottom:1px solid var(--atlas-map-border)}.preview-map-heading i{display:inline-block;width:4px;height:4px;background:var(--vp-c-brand-1);border-radius:50%;margin-right:7px}.preview-map-heading>span:last-child{font-size:7px;color:var(--atlas-map-muted)}.atlas-preview-map>span{display:flex;justify-content:space-between;gap:12px;font-size:9px;padding:12px 0 0;color:var(--atlas-map-muted);border-top:1px solid var(--atlas-map-border);line-height:1.7}.preview-lines path{stroke:var(--atlas-map-link);fill:none;opacity:.18;stroke-width:.7}.preview-cluster-label{font-size:7px;fill:var(--atlas-map-label);paint-order:stroke;stroke:var(--atlas-map-bg);stroke-width:3px;stroke-linejoin:round}.atlas-preview-map:hover{border-color:var(--vp-c-brand-1)}.atlas-preview-map:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:4px}
@media(max-width:1350px){.atlas-home-preview{margin:0 48px;gap:36px}}
@media(max-width:800px){.atlas-home-preview{grid-template-columns:1fr;gap:30px;padding:44px 0}.atlas-preview-copy>div{max-width:none}}
@media(max-width:639px){.atlas-home-preview{margin:0 24px}.atlas-preview-copy h2{font-size:33px}.atlas-preview-map{padding:12px}.preview-map-heading{font-size:7px}.preview-map-heading>span:last-child{font-size:6px}}
.preview-partition-labels{fill:var(--atlas-map-label)}.preview-partition-labels text{font-size:8px;font-weight:600;letter-spacing:.08em}.preview-sample-label{fill:var(--atlas-map-muted);font-size:8px}.preview-lines path{opacity:.2;stroke-width:.8}.atlas-preview-copy h2 em{font-weight:400}
</style>
