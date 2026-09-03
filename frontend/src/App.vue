<script setup lang="ts">
// Root — router-view
// `/` → MonitorView (Keel U1 read-only)
// `/legacy` → DashboardView (R20 shell; needs r20_backend)
// `/admin/*` → AdminLayout (legacy control panel)
</script>

<template>
  <router-view />
</template>
