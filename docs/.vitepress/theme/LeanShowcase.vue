<script setup>
import { ref } from 'vue'
import { withBase } from 'vitepress'
import example from '../../../agent_monitor/examples/erdos-straus.json'
const selected = ref(example.formal_graph.nodes[0])
const nodes = example.formal_graph.nodes
const pick = node => { selected.value = node }
</script>

<template>
  <section class="ansatz-lean-showcase" aria-labelledby="lean-showcase-title">
    <p class="ansatz-section-index">RESEARCH IN VIEW / LEAN DAGs</p>
    <h2 id="lean-showcase-title">See what a proof depends on.</h2>
    <p class="lean-intro">Follow a Lean proof from its definitions through supporting lemmas to the final theorem. Select a declaration below to inspect it—and see exactly where the evidence stops.</p>
    <div class="lean-example-caption"><strong>Erdős–Straus · a worked research example</strong><span>499 exact arithmetic checks · 5 kernel-checked witnesses · general conjecture open</span></div>
    <div class="lean-mini-graph" aria-label="Lean dependency graph for five finite witnesses">
      <div class="lean-root"><button :aria-pressed="selected.id === nodes[0].id" @click="pick(nodes[0])">UnitFractionWitness <small>definition</small></button></div>
      <svg class="lean-branches" viewBox="0 0 1000 48" preserveAspectRatio="none" aria-hidden="true"><path d="M500 0V20H100V48M300 20V48M500 20V48M500 20H900V48M700 20V48" /></svg>
      <div class="lean-leaves"><button v-for="node in nodes.slice(1, 6)" :key="node.id" :aria-pressed="selected.id === node.id" @click="pick(node)">{{ node.label }}<small>kernel checked</small></button></div>
      <svg class="lean-branches" viewBox="0 0 1000 48" preserveAspectRatio="none" aria-hidden="true"><path d="M100 0V24H900V0M300 0V24M700 0V24M500 0V48" /></svg>
      <div class="lean-root"><button :aria-pressed="selected.id === nodes[6].id" @click="pick(nodes[6])">checked_cases <small>five finite witnesses—not the full conjecture</small></button></div>
      <button class="lean-open" :aria-pressed="selected.id === nodes[7].id" @click="pick(nodes[7])">ErdosStraus <small>Open goal · no proof dependency connects these finite checks to the universal claim</small></button>
    </div>
    <div class="lean-node-note" aria-live="polite"><strong>{{ selected.label }}</strong><p>{{ selected.statement }}</p><small>{{ selected.status }}</small></div>
    <p class="lean-honesty">This curated example includes real exact-arithmetic and Lean checks. The research roles illustrate a workflow; they are not a recorded multi-agent execution. A DAG shows dependencies, while Lean’s kernel checks the formal statements.</p>
    <div class="lean-example-links"><a :href="withBase('/example-run')">Explore the advanced example →</a><a :href="withBase('/example-run-viewer.html?view=formal')">Open the run’s Lean DAG ↗</a></div>
  </section>
</template>

<style scoped>
.ansatz-lean-showcase{padding:64px 0;border-bottom:1px solid var(--vp-c-divider)}.ansatz-lean-showcase h2{font:500 clamp(34px,4vw,52px)/1.1 'Newsreader Variable',Georgia,serif;border:0;margin:0;padding:0;letter-spacing:-.03em}.lean-intro{max-width:760px;color:var(--vp-c-text-2);font-size:16px;line-height:1.7}.lean-example-caption{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;margin:28px 0 16px}.lean-example-caption span{font-size:12px;color:var(--vp-c-text-2)}.lean-mini-graph{padding:24px;background:var(--vp-c-bg-alt);border:1px solid var(--vp-c-divider);overflow:auto}.lean-root{display:flex;justify-content:center;min-width:610px}.lean-mini-graph button{border:1px solid var(--vp-c-border);border-top:3px solid var(--vp-c-brand-1);padding:12px 8px;background:var(--vp-c-bg);color:var(--vp-c-text-1);font:11px/1.4 var(--vp-font-family-mono);cursor:pointer;border-radius:3px;overflow-wrap:anywhere}.lean-mini-graph button:focus-visible{outline:3px solid var(--vp-c-brand-1);outline-offset:3px}.lean-mini-graph button[aria-pressed=true]{background:var(--vp-c-brand-soft)}.lean-mini-graph small{display:block;font:10px/1.5 var(--vp-font-family-base);color:var(--vp-c-text-2);margin-top:5px}.lean-root button{min-width:240px}.lean-leaves{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;min-width:610px}.lean-branches{display:block;width:100%;min-width:610px;height:36px;fill:none;stroke:var(--vp-c-border);stroke-width:1.5}.lean-open{display:block;margin:24px auto 0;border-style:dashed!important;border-top-color:#a58154!important;max-width:420px}.lean-node-note{border-left:3px solid var(--vp-c-brand-1);background:var(--vp-c-bg-soft);padding:16px 20px;margin-top:16px;overflow-wrap:anywhere}.lean-node-note p{margin:8px 0;font-size:14px}.lean-node-note small,.lean-honesty{font-size:12px;color:var(--vp-c-text-2)}.lean-example-links{display:flex;flex-wrap:wrap;gap:24px;font-size:14px;margin-top:20px}@media(max-width:600px){.ansatz-lean-showcase{padding:40px 0}.lean-mini-graph{padding:16px}}
</style>
