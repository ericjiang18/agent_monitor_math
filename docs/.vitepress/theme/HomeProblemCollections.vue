<script setup>
import { withBase } from 'vitepress'
const collections = [
  { id: 'erdos', name: 'Erdős Problems', note: 'Number theory, combinatorics, and questions that connect them.', mark: 'EP' },
  { id: 'kourovka', name: 'The Kourovka Notebook', note: 'A continuing collection of problems in group theory.', mark: 'KN' },
  { id: 'clay', name: 'Millennium Prize Problems', note: 'Foundational questions, recorded progress, and recent announcements.', mark: 'MP' },
  { id: 'ramanujan', name: 'Ramanujan’s legacy', note: 'Questions surrounding factorials, tau coefficients, and modular forms.', mark: 'R' },
  { id: 'hilbert', name: 'Hilbert’s problems', note: 'Remaining questions and open extensions of the original list.', mark: 'H' },
  { id: 'smale', name: 'Smale’s problems', note: 'Questions across dynamics, computation, and mathematical physics.', mark: 'S' },
]
</script>

<template>
  <section class="home-problems" aria-labelledby="home-problems-title">
    <header><div><p class="home-problems-eyebrow">OPEN PROBLEMS / LASTING QUESTIONS</p><h2 id="home-problems-title">Choose a question<br><em>worth staying with.</em></h2></div><p>Read the mathematical claim, explore known results, and follow sourced proof announcements before starting your own approach.</p></header>
    <nav class="home-collection-grid" aria-label="Open problem collections">
      <a v-for="collection in collections" :key="collection.id" :href="withBase('/open-problems?source=' + collection.id)"><span class="collection-mark">{{ collection.mark }}</span><h3>{{ collection.name }}</h3><p>{{ collection.note }}</p><span class="collection-explore">Explore the collection <span aria-hidden="true">↗</span></span></a>
    </nav>
    <a class="home-problems-all" :href="withBase('/open-problems')">Browse all research problems →</a>
  </section>
</template>

<style scoped>
.home-problems{max-width:1280px;margin:0 auto;padding:62px 0;border-bottom:1px solid var(--vp-c-divider)}.home-problems header{display:grid;grid-template-columns:1.1fr 1fr;gap:60px;align-items:end;margin-bottom:32px}.home-problems-eyebrow{font-size:10px;letter-spacing:.13em;font-weight:650;color:var(--vp-c-brand-1);margin:0 0 20px}.home-problems h2{font:400 clamp(30px,3.2vw,45px)/1.1 'Newsreader Variable',Georgia,serif;letter-spacing:-.03em;margin:0;border:0;padding:0}.home-problems h2 em{color:var(--vp-c-brand-1);font-weight:400}.home-problems header>p{font-size:14px;line-height:1.85;color:var(--vp-c-text-2);margin:0;max-width:480px}.home-collection-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.home-collection-grid>a{display:flex;flex-direction:column;align-items:flex-start;min-width:0;padding:23px;border:1px solid var(--vp-c-divider);border-radius:6px;text-decoration:none;background:var(--vp-c-bg);transition:border-color .15s}.home-collection-grid>a:hover{border-color:var(--vp-c-brand-1)}.home-collection-grid>a:focus-visible,.home-problems-all:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:4px}.collection-mark{display:grid;place-items:center;min-width:32px;height:30px;border:1px solid var(--vp-c-divider);border-radius:4px;font:600 10px var(--vp-font-family-mono);color:var(--vp-c-brand-1)}.home-collection-grid h3{font:500 24px/1.2 'Newsreader Variable',Georgia,serif;color:var(--vp-c-text-1);margin:17px 0 10px}.home-collection-grid p{font-size:13px;line-height:1.75;color:var(--vp-c-text-2);margin:0 0 22px}.collection-explore{display:flex;justify-content:space-between;width:100%;gap:10px;margin-top:auto;font-size:12px;color:var(--vp-c-brand-1)}.home-problems-all{display:inline-block;margin-top:24px;font-size:13px;color:var(--vp-c-brand-1);text-decoration:none}@media(max-width:1350px){.home-problems{margin:0 48px}}@media(max-width:800px){.home-problems header{grid-template-columns:1fr;gap:20px}.home-collection-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:639px){.home-problems{margin:0 24px;padding:44px 0}.home-collection-grid{grid-template-columns:1fr}.home-collection-grid>a{padding:21px}.home-problems h2{font-size:33px}}@media(prefers-reduced-motion:reduce){.home-collection-grid>a{transition:none}}
</style>
