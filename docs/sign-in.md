---
title: Sign in
aside: false
sidebar: false
head:
  - - meta
    - name: robots
      content: noindex,nofollow,noarchive
---

<script setup>
import { onMounted } from 'vue'

onMounted(() => {
  window.location.replace(`/sign-in${window.location.search}`)
})
</script>

# Opening the console…

If you are not redirected automatically, <a href="/sign-in">continue to the console</a>.
