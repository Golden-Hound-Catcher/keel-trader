import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '../stores/auth'

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
  {
    // Legacy R20 5-tab dashboard — still calls /api/all (needs r20_backend). Not U1 path.
    path: '/legacy',
    name: 'legacy-dashboard',
    component: () => import('../views/DashboardView.vue'),
    meta: { isPublic: true, legacy: true },
  },
  {
    path: '/admin',
    component: () => import('../views/AdminLayout.vue'),
    meta: { requiresAuth: true, isPublic: false, legacy: true },
    children: [
      { path: '', redirect: '/admin/overview' },
      { path: 'overview', name: 'admin-overview', component: () => import('../views/admin/OverviewPage.vue') },
      { path: 'security', name: 'admin-security', component: () => import('../views/admin/SecurityPage.vue') },
      { path: 'council', name: 'admin-council', component: () => import('../views/admin/CouncilPage.vue') },
      { path: 'llm', name: 'admin-llm', component: () => import('../views/admin/LlmPage.vue') },
      { path: 'notify', name: 'admin-notify', component: () => import('../views/admin/NotifyPage.vue') },
      { path: 'about', name: 'admin-about', component: () => import('../views/admin/AboutPage.vue') },
      { path: 'decisions', name: 'admin-decisions', component: () => import('../views/admin/DecisionsPage.vue') },
      { path: 'gateway', name: 'admin-gateway', component: () => import('../views/admin/GatewayPage.vue') },
      { path: 'promptlib', name: 'admin-promptlib', component: () => import('../views/admin/PromptStudioPage.vue') },
      { path: 'agents', name: 'admin-agents', component: () => import('../views/admin/AgentsPage.vue') },
      { path: 'backup', name: 'admin-backup', component: () => import('../views/admin/BackupPage.vue') },
      { path: 'plugins', name: 'admin-plugins', component: () => import('../views/admin/PluginsPage.vue') },
      { path: 'audit', name: 'admin-audit', component: () => import('../views/admin/AuditPage.vue') },
      { path: 'adminsys', name: 'admin-adminsys', component: () => import('../views/admin/AdminSysPage.vue') },
    ],
  },
  {
    path: '/admin/login',
    name: 'admin-login',
    component: () => import('../views/admin/LoginPage.vue'),
    meta: { isPublic: true, legacy: true },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  },
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    auth.restoreSession()
    if (!auth.isAuthenticated) {
      return { name: 'admin-login' }
    }
  }
  if (to.name === 'admin-login' && auth.isAuthenticated) {
    return { name: 'admin-overview' }
  }
})

export default router
