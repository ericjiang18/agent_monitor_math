---
title: "Overview has moved"
description: "The platform overview is now part of the Ansätze Quickstart."
sidebar: false
aside: false
head:
  - - link
    - rel: canonical
      href: /quickstart
---

<script setup>
import { onMounted } from 'vue'
import { withBase } from 'vitepress'
onMounted(() => {
  const aliases = { '#proving-engines': '#proving-engines', '#real-time-trace-views': '#trace-views-and-proof-graphs', '#persistent-library-memory-skills-tools': '#reusable-memory-skills-and-tools', '#persistent-library-memory-·-skills-·-tools': '#reusable-memory-skills-and-tools', '#project-layout': '#project-layout', '#account-boundaries': '#1-open-the-console', '#where-to-go-next': '#keep-exploring' }
  let hash = window.location.hash
  try { hash = decodeURIComponent(hash) } catch {}
  window.location.replace(withBase('/quickstart') + window.location.search + (aliases[hash] || window.location.hash))
})
</script>

# The overview is now in Quickstart

Read the **[complete Quickstart](/quickstart)** for the first-run guide, proving engines, trace views, and reusable library.
