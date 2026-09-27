<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const host = ref(null)
const mounted = ref(false)
const onScreen = ref(false)
const pageVisible = ref(true)
const reducedMotion = ref(true)
const userPaused = ref(false)
const userOptedIn = ref(false)
const motionEnabled = computed(() => !userPaused.value && (!reducedMotion.value || userOptedIn.value))
const running = computed(() => mounted.value && motionEnabled.value && onScreen.value && pageVisible.value)

// A deterministic parametric surface, used as an illustration rather than a research graph.
// Project a torus into the page; every curve is rendered once, with no animation-time Vue updates.
function project(u, v) {
  const radius = 132 + 46 * Math.cos(v)
  const x = radius * Math.cos(u)
  const y = radius * Math.sin(u)
  const z = 46 * Math.sin(v)
  const tilt = -.88, turn = -.34
  const yy = y * Math.cos(tilt) - z * Math.sin(tilt)
  const zz = y * Math.sin(tilt) + z * Math.cos(tilt)
  const xx = x * Math.cos(.28) + zz * Math.sin(.28)
  return [320 + xx * Math.cos(turn) - yy * Math.sin(turn), 278 + xx * Math.sin(turn) + yy * Math.cos(turn)]
}
function curve(varyU, fixed) {
  return Array.from({ length: 65 }, (_, index) => {
    const angle = index / 64 * Math.PI * 2
    const [x, y] = project(varyU ? angle : fixed, varyU ? fixed : angle)
    return `${index ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`
  }).join(' ')
}
const latitude = Array.from({ length: 17 }, (_, i) => ({
  path: curve(true, i / 17 * Math.PI * 2),
  opacity: (.23 + .31 * ((Math.sin(i / 17 * Math.PI * 2) + 1) / 2)).toFixed(2)
}))
const longitude = Array.from({ length: 23 }, (_, i) => ({
  path: curve(false, i / 23 * Math.PI * 2),
  opacity: (.22 + .2 * ((Math.cos(i / 23 * Math.PI * 2) + 1) / 2)).toFixed(2)
}))
const stars = [
  { x: 109, y: 144, r: 4.5 }, { x: 188, y: 95, r: 3 }, { x: 373, y: 83, r: 3.3 },
  { x: 509, y: 151, r: 5 }, { x: 566, y: 299, r: 3 }, { x: 465, y: 438, r: 4.4 },
  { x: 255, y: 470, r: 3 }, { x: 85, y: 356, r: 3.6 }, { x: 493, y: 344, r: 2.2 },
  { x: 174, y: 414, r: 2.2 }, { x: 87, y: 235, r: 1.8 }, { x: 430, y: 113, r: 1.8 }
]
const connections = [
  'M109 144 Q242 45 373 83', 'M373 83 Q515 50 509 151',
  'M509 151 Q595 211 566 299', 'M566 299 Q537 383 465 438',
  'M465 438 Q358 511 255 470', 'M255 470 Q109 465 85 356',
  'M85 356 Q54 237 109 144', 'M188 95 Q334 220 465 438',
  'M109 144 Q289 278 493 344', 'M85 356 Q291 231 509 151'
]
const tracePaths = [connections[0], connections[3], connections[5], curve(true, .9), curve(true, 3.8)]
let observer
let media
let frame = 0
let cleanup = []

function resetPointer() {
  if (frame) cancelAnimationFrame(frame)
  frame = 0
  host.value?.style.setProperty('--hero-pointer-x', '0px')
  host.value?.style.setProperty('--hero-pointer-y', '0px')
}
function toggleMotion() {
  if (motionEnabled.value) {
    userPaused.value = true
    resetPointer()
  } else {
    userPaused.value = false
    userOptedIn.value = true
  }
}
function onPointerMove(event) {
  if (!running.value || event.pointerType === 'touch' || !host.value) return
  const bounds = host.value.getBoundingClientRect()
  const x = ((event.clientX - bounds.left) / bounds.width - .5) * 14
  const y = ((event.clientY - bounds.top) / bounds.height - .5) * 10
  if (frame) cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    frame = 0
    if (!running.value || !host.value) return
    host.value.style.setProperty('--hero-pointer-x', `${x.toFixed(2)}px`)
    host.value.style.setProperty('--hero-pointer-y', `${y.toFixed(2)}px`)
  })
}

onMounted(() => {
  mounted.value = true
  media = window.matchMedia('(prefers-reduced-motion: reduce)')
  const syncPreference = () => {
    reducedMotion.value = media.matches
    if (media.matches) {
      userOptedIn.value = false
      resetPointer()
    }
  }
  const syncVisibility = () => {
    pageVisible.value = document.visibilityState !== 'hidden'
    if (!pageVisible.value) resetPointer()
  }
  syncPreference()
  syncVisibility()
  media.addEventListener('change', syncPreference)
  document.addEventListener('visibilitychange', syncVisibility)
  cleanup.push(() => media.removeEventListener('change', syncPreference))
  cleanup.push(() => document.removeEventListener('visibilitychange', syncVisibility))
  if ('IntersectionObserver' in window) {
    observer = new IntersectionObserver(entries => {
      onScreen.value = entries.some(entry => entry.isIntersecting)
      if (!onScreen.value) resetPointer()
    }, { threshold: 0 })
    observer.observe(host.value)
  } else {
    onScreen.value = true
  }
})
onBeforeUnmount(() => {
  observer?.disconnect()
  cleanup.forEach(remove => remove())
  cleanup = []
  resetPointer()
})
</script>

<template>
  <figure
    ref="host"
    class="hero-motion"
    :data-motion="running ? 'running' : 'paused'"
    :data-motion-enabled="String(motionEnabled)"
    @pointermove="onPointerMove"
    @pointerleave="resetPointer"
  >
    <svg class="hero-motion-art" viewBox="0 0 640 560" fill="none" aria-hidden="true" focusable="false">
      <g class="motion-registration">
        <path d="M38 70V50H58M582 50H602V70M38 468V488H58M582 488H602V468" />
        <path d="M38 269H47M597 269H606M320 45V54M320 484V493" />
        <text x="51" y="32">THE SPACE OF POSSIBILITIES</text>
        <text x="590" y="32" text-anchor="end">∴</text>
      </g>
      <g class="motion-parallax">
        <g class="motion-orbit-lines">
          <ellipse cx="320" cy="278" rx="249" ry="181" transform="rotate(-24 320 278)" />
          <ellipse cx="320" cy="278" rx="245" ry="153" transform="rotate(40 320 278)" />
          <circle cx="320" cy="278" r="213" class="motion-guide-circle" />
        </g>
        <g class="motion-constellation">
          <path v-for="(path, i) in connections" :key="i" :d="path" />
        </g>
        <g class="motion-surface">
          <path v-for="(line, i) in latitude" :key="`latitude-${i}`" :d="line.path" :opacity="line.opacity" />
          <path v-for="(line, i) in longitude" :key="`longitude-${i}`" :d="line.path" :opacity="line.opacity" />
          <path class="motion-surface-edge" :d="curve(true, 0)" />
          <path class="motion-surface-edge motion-inner-edge" :d="curve(true, Math.PI)" />
          <g class="motion-traces">
            <path v-for="(path, i) in tracePaths.slice(3)" :key="i" :d="path" pathLength="1" :style="{ animationDelay: `${i * -4}s` }" />
          </g>
        </g>
        <g class="motion-traces motion-outer-traces">
          <path v-for="(path, i) in tracePaths.slice(0, 3)" :key="i" :d="path" pathLength="1" :style="{ animationDelay: `${i * -3.4}s` }" />
        </g>
        <g class="motion-stars">
          <g v-for="(star, i) in stars" :key="i" :transform="`translate(${star.x} ${star.y})`">
            <circle v-if="i < 8" class="motion-star-halo" :r="star.r + 7" :style="{ animationDelay: `${i * -.9}s` }" />
            <circle class="motion-star-core" :r="star.r" />
            <circle v-if="i < 8" class="motion-star-center" r="1" />
          </g>
        </g>
        <g class="motion-formulae">
          <text x="84" y="120">f : X → Y</text>
          <text x="465" y="128">∂² = 0</text>
          <text x="77" y="394">∀ ε &gt; 0</text>
          <text x="474" y="474">∞</text>
        </g>
        <g class="motion-center">
          <text x="320" y="274" text-anchor="middle">What if?</text>
          <path d="M304 291H336" />
          <circle cx="320" cy="291" r="2" />
        </g>
      </g>
      <g class="motion-bottom-label">
        <path d="M51 521H106" />
        <text x="120" y="525">AN IDEA, TAKING SHAPE.</text>
      </g>
    </svg>
    <figcaption class="hero-motion-caption">
      <span class="hero-motion-mobile-caption" aria-hidden="true">IDEAS, IN MOTION.</span>
      <span class="hero-motion-sr-only">Decorative mathematical illustration of a curved surface and connected ideas.</span>
      <button
        class="hero-motion-toggle"
        type="button"
        :aria-label="motionEnabled ? 'Pause decorative animation' : 'Play decorative animation'"
        :aria-pressed="motionEnabled"
        @click="toggleMotion"
      >
        <svg v-if="motionEnabled" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><path d="M3 2H5V10H3zM7 2H9V10H7z" /></svg>
        <svg v-else viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><path d="M3 1.7 10 6 3 10.3z" /></svg>
        {{ motionEnabled ? 'Pause motion' : 'Play motion' }}
      </button>
    </figcaption>
  </figure>
</template>

<style scoped>
.hero-motion {
  --motion-ink: #2563eb;
  --motion-soft: #6291cf;
  --motion-line: #8196b1;
  --motion-paper: #ffffff;
  --motion-label: #4c5c70;
  --hero-pointer-x: 0px;
  --hero-pointer-y: 0px;
  position: relative;
  isolation: isolate;
  width: 100%;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  contain: layout paint;
}
:global(.dark .hero-motion) {
  --motion-ink: #a7c8ff;
  --motion-soft: #82b1ff;
  --motion-line: #778ba8;
  --motion-paper: #111318;
  --motion-label: #b9c2d0;
}
.hero-motion-art { display: block; width: 100%; height: auto; overflow: hidden; }
.motion-registration path { stroke: var(--motion-line); stroke-width: .8; opacity: .45; }
.motion-registration text, .motion-bottom-label text {
  fill: var(--motion-label);
  font: 500 8px 'Space Grotesk Variable', Arial, sans-serif;
  letter-spacing: 2px;
}
.motion-registration text:last-child { font: 400 23px 'Newsreader Variable', Georgia, serif; }
.motion-parallax { transform: translate(var(--hero-pointer-x), var(--hero-pointer-y)); transition: transform 700ms cubic-bezier(.2,.7,.3,1); }
.hero-motion[data-motion='paused'] .motion-parallax { transition: none; }
.motion-orbit-lines { stroke: var(--motion-line); stroke-width: .7; opacity: .4; }
.motion-guide-circle { opacity: .4; stroke-dasharray: 2 7; }
.motion-constellation { stroke: var(--motion-soft); stroke-width: .7; opacity: .32; }
.motion-surface { transform-origin: 320px 278px; animation: surface-drift 18s ease-in-out infinite; stroke: var(--motion-ink); stroke-width: .8; }
.motion-surface-edge { stroke-width: 1.25; opacity: .7; }
.motion-inner-edge { opacity: .5; }
.motion-traces path { stroke: var(--motion-ink); stroke-width: 1.8; stroke-linecap: round; stroke-dasharray: .075 .925; animation: trace-thought 12s linear infinite; }
.motion-outer-traces path { stroke-width: 1.3; opacity: .68; }
.motion-star-halo { stroke: var(--motion-soft); stroke-width: .8; fill: var(--motion-paper); animation: star-breathe 7s ease-in-out infinite; }
.motion-star-core { fill: var(--motion-ink); }
.motion-star-center { fill: var(--motion-paper); }
.motion-formulae text { fill: var(--motion-label); font: italic 18px 'Newsreader Variable', Georgia, serif; }
.motion-formulae text:last-child { font-size: 30px; }
.motion-center text { fill: var(--motion-ink); font: italic 27px 'Newsreader Variable', Georgia, serif; }
.motion-center path { stroke: var(--motion-line); stroke-width: .75; }
.motion-center circle { fill: var(--motion-ink); }
.motion-bottom-label path { stroke: var(--motion-soft); stroke-width: .8; }
.hero-motion-mobile-caption { display: none; }
.hero-motion-caption { position: absolute; right: 6%; bottom: 4%; display: flex; align-items: center; justify-content: flex-end; }
.hero-motion-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 6px 9px;
  border: 1px solid transparent;
  border-radius: 999px;
  color: var(--motion-label);
  background: var(--motion-paper);
  font: 500 9px 'Space Grotesk Variable', Arial, sans-serif;
  cursor: pointer;
}
.hero-motion-toggle svg { width: 10px; height: 10px; flex: none; }
.hero-motion-toggle:hover { border-color: var(--motion-line); color: var(--motion-ink); }
.hero-motion-toggle:focus-visible { outline: 2px solid var(--motion-ink); outline-offset: 3px; }
.hero-motion-sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.hero-motion .motion-surface, .hero-motion .motion-traces path, .hero-motion .motion-star-halo { animation-play-state: paused; }
.hero-motion[data-motion='running'] .motion-surface, .hero-motion[data-motion='running'] .motion-traces path, .hero-motion[data-motion='running'] .motion-star-halo { animation-play-state: running; }
@keyframes surface-drift { 0%, 100% { transform: rotate(-2deg) translateY(0); } 50% { transform: rotate(2deg) translateY(-5px); } }
@keyframes trace-thought { from { stroke-dashoffset: 0; } to { stroke-dashoffset: -1; } }
@keyframes star-breathe { 0%, 100% { opacity: .3; } 50% { opacity: .85; } }
@media (max-width: 639px) {
  .hero-motion-caption { position: static; justify-content: space-between; gap: 8px; padding: 0 5% 4px; }
  .hero-motion-mobile-caption { display: block; color: var(--motion-label); font: 500 8px 'Space Grotesk Variable', Arial, sans-serif; letter-spacing: .12em; }
  .hero-motion-toggle { min-height: 44px; font-size: 10px; flex: none; }
  .motion-bottom-label { display: none; }
  .motion-registration text, .motion-bottom-label text { font-size: 9px; }
}

/* A deliberate Play action may opt into this illustration only; the site's
   reduced-motion policy continues to apply everywhere else. */
@media (prefers-reduced-motion: reduce) {
  .hero-motion[data-motion-enabled='true'] .motion-surface { animation-duration: 18s !important; animation-iteration-count: infinite !important; }
  .hero-motion[data-motion-enabled='true'] .motion-traces path { animation-duration: 12s !important; animation-iteration-count: infinite !important; }
  .hero-motion[data-motion-enabled='true'] .motion-star-halo { animation-duration: 7s !important; animation-iteration-count: infinite !important; }
  .hero-motion[data-motion='running'] .motion-parallax { transition-duration: 700ms !important; }
}
</style>
