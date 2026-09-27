---
layout: home
title: Ansätze
titleTemplate: An AI workspace for mathematical research

hero:
  name: Ansätze
  text: Every proof begins with an idea.
  tagline: An AI workspace for mathematical research. Explore an approach, work with proving agents, and examine the evidence behind every result.
  actions:
    - theme: brand
      text: Use as guest
      link: /guest
      target: _self
    - theme: alt
      text: Sign in
      link: /sign-in
      target: _self
    - theme: alt
      text: Quickstart
      link: /quickstart
---

<script setup>
import AtlasPreview from './.vitepress/theme/AtlasPreview.vue'
import HomeProblemCollections from './.vitepress/theme/HomeProblemCollections.vue'
</script>

<aside class="ansatz-definition" aria-label="Meaning of Ansätze"><span>THE NAME</span><p><strong>Ansätze</strong> <em>plural noun · mathematics</em> Proposed forms or starting approaches to a problem. The work begins by asking where they lead.</p></aside>

<AtlasPreview />

<HomeProblemCollections />

<section class="ansatz-system" aria-labelledby="system-title">
  <header class="ansatz-section-heading">
    <p class="ansatz-section-index">01 / THE SYSTEM</p>
    <h2 id="system-title">Built to show its work.</h2>
    <p>Ansätze keeps the question, the search process, and the resulting evidence together—so a promising answer is the start of inspection, not the end of it.</p>
  </header>

  <div class="ansatz-capability-list">
    <article><span>01</span><div><h3>One proving workspace</h3><p>Launch different reasoning engines from one console without losing the original problem or run context.</p></div></article>
    <article><span>02</span><div><h3>Inspect every trace</h3><p>Move between pipeline and graph views to see where a proof developed, branched, or left a gap.</p></div></article>
    <article><span>03</span><div><h3>Preserve useful work</h3><p>Keep proofs, artifacts, memory, skills, and tools attached to the work that produced them.</p></div></article>
    <article><span>04</span><div><h3>Continue, don’t restart</h3><p>Add feedback to an existing run and continue from its workspace instead of rebuilding context.</p></div></article>
    <article><span>05</span><div><h3>Compare approaches</h3><p>Try multiple engines and models while retaining a common view of progress, output, token use, and cost.</p></div></article>
    <article><span>06</span><div><h3>Configure your routes</h3><p>Manage provider access in account settings and choose the model route that fits each run.</p></div></article>
  </div>
</section>

<section class="ansatz-workflow" aria-labelledby="workflow-title">
  <header class="ansatz-section-heading ansatz-section-heading-compact">
    <p class="ansatz-section-index">02 / WORKFLOW</p>
    <h2 id="workflow-title">From question to inspectable result.</h2>
  </header>

  <ol class="ansatz-steps">
    <li class="ansatz-step"><span>01</span><div><strong>Describe the problem</strong><p>Paste a statement or choose an existing problem, then select a model and proving engine.</p></div></li>
    <li class="ansatz-step"><span>02</span><div><strong>Watch the work</strong><p>Follow live agent output in a structured pipeline or a free-form dependency map.</p></div></li>
    <li class="ansatz-step"><span>03</span><div><strong>Review the artifact</strong><p>Inspect the resulting proof, supporting files, verification output, and unresolved gaps.</p></div></li>
  </ol>
</section>

<section class="ansatz-next" aria-labelledby="next-title">
  <p class="ansatz-section-index">03 / START HERE</p>
  <h2 id="next-title">Take a closer look.</h2>
  <p>Try the console in a browser-scoped guest session, sign in for durable account history, or read the main concepts first.</p>
  <nav aria-label="Getting started links">
    <a href="/guest">Use as guest <span aria-hidden="true">→</span></a>
    <a href="/sign-in">Sign in <span aria-hidden="true">→</span></a>
    <a href="/news">News <span aria-hidden="true">→</span></a>
    <a href="/engineering">Engineering & reliability <span aria-hidden="true">→</span></a>
    <a href="/quickstart">Quickstart <span aria-hidden="true">→</span></a>
  </nav>
</section>
