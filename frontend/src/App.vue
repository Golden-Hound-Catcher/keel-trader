<script setup lang="ts">
// Root — router-view
// `/` → MonitorView (Keel U1/U2 read-only)
// `/admin/*` → AdminLayout (legacy control panel; needs r20_backend opt-in)
</script>

<template>
  <router-view />
</template>
