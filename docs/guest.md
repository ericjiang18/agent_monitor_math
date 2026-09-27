---
title: Guest access
aside: false
sidebar: false
head:
  - - meta
    - name: robots
      content: noindex,nofollow,noarchive
---

<script setup>
import { onMounted } from 'vue'

function continueAsGuest() {
  const secure = window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = `agent_monitor_guest_intent=1; Path=/; Max-Age=120; SameSite=Lax${secure}`
  // The fragment is not sent to Caddy, so it survives mount redirects even
  // when the redirect target is on the canonical console host.
  window.location.assign('/sign-in#guest')
}

onMounted(continueAsGuest)
</script>

# Opening a guest session…

If you are not redirected automatically,
<button type="button" class="guest-continue" @click="continueAsGuest">continue as guest</button>.

Your guest history is tied to the resulting browser session and is not moved
into a later account.

<style scoped>
.guest-continue {
  appearance: none;
  border: 0;
  padding: 0;
  color: var(--vp-c-brand-1);
  background: transparent;
  font: inherit;
  text-decoration: underline;
  cursor: pointer
}
</style>
