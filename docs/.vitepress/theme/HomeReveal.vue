<script setup>
import { onMounted, onBeforeUnmount } from 'vue'

let observer
let preference
const animations = new Set()
const stop = () => {
  observer?.disconnect()
  for (const animation of animations) animation.finish()
  animations.clear()
}
const preferenceChanged = () => { if (preference.matches) stop() }

onMounted(() => {
  preference = matchMedia('(prefers-reduced-motion: reduce)')
  preference.addEventListener('change', preferenceChanged)
  if (preference.matches || !('IntersectionObserver' in window) || !Element.prototype.animate) return
  const targets = document.querySelectorAll('.VPHome .ansatz-definition, .VPHome .atlas-home-preview, .VPHome .ansatz-system, .VPHome .ansatz-workflow, .VPHome .ansatz-next')
  observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue
      observer.unobserve(entry.target)
      // Content stays visible in the HTML. Motion is an enhancement on entry.
      const animation = entry.target.animate([
        { opacity: .15, transform: 'translateY(28px)' },
        { opacity: 1, transform: 'translateY(0)' }
      ], { duration: 850, easing: 'cubic-bezier(.16, 1, .3, 1)' })
      animations.add(animation)
      animation.onfinish = () => animations.delete(animation)
    }
  }, { threshold: 0, rootMargin: '0px 0px -40px 0px' })
  targets.forEach(target => observer.observe(target))
})

onBeforeUnmount(() => {
  stop()
  preference?.removeEventListener('change', preferenceChanged)
})
</script>

<template><span hidden aria-hidden="true" data-home-reveal></span></template>
