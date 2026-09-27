<!-- Display the linked UCLA Computer Science and Mathematics partner marks. -->
<script setup>
import { withBase } from 'vitepress'
</script>

<template>
  <div class="ansatz-institution-wrap">
    <section class="ansatz-institution-rail" aria-label="Participating UCLA departments">
      <div class="ansatz-institution-marks">
        <a
          class="ansatz-institution-mark ansatz-institution-mark-cs"
          href="https://www.cs.ucla.edu/"
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Visit the UCLA Samueli Computer Science department (opens in a new tab)"
        >
          <img
            class="ansatz-mark-light"
            :src="withBase('/partners/ucla-samueli-computer-science-light.png')"
            alt="UCLA Samueli Computer Science"
            width="859"
            height="180"
          >
          <img
            class="ansatz-mark-dark"
            :src="withBase('/partners/ucla-samueli-computer-science-white.png')"
            alt="UCLA Samueli Computer Science"
            width="859"
            height="180"
          >
        </a>

        <span class="ansatz-institution-cross" aria-hidden="true">×</span>

        <a
          class="ansatz-institution-mark ansatz-institution-mark-math"
          href="https://www.math.ucla.edu/"
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Visit the UCLA Mathematics department (opens in a new tab)"
        >
          <img
            class="ansatz-mark-light"
            :src="withBase('/partners/ucla-mathematics-light.png')"
            alt="UCLA College Physical Sciences Mathematics"
            width="1246"
            height="322"
          >
          <img
            class="ansatz-mark-dark"
            :src="withBase('/partners/ucla-mathematics-white.png')"
            alt="UCLA College Physical Sciences Mathematics"
            width="1246"
            height="322"
          >
        </a>
      </div>
    </section>
  </div>
</template>
