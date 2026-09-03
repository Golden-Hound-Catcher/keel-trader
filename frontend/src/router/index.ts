import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'monitor',
    component: () => import('../views/MonitorView.vue'),
    meta: { isPublic: true, keelMonitor: true },
  },
  {
    path: '/monitor',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  },
})

export default router
