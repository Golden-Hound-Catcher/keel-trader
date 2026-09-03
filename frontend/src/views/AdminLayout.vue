<script setup lang="ts">
// LEGACY R20 ADMIN — needs r20_backend; not Phase U1 Keel monitor

import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import {
  LayoutGrid,
  ShieldAlert,
  Brain,
  Zap,
  MessageCircle,
  HardDrive,
  Package,
  Scroll,
  UserCog,
  Info,
  Cpu,
  FileText,
  Users,
  RefreshCw,
  LogOut,
} from 'lucide-vue-next'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const navGroups = [
  {
    label: '日常运行',
    items: [
      { id: 'overview', label: '运行总览', icon: LayoutGrid },
      { id: 'security', label: '账户与交易', icon: ShieldAlert },
      { id: 'decisions', label: '决策与日志', icon: Brain },
      { id: 'gateway', label: '任务与事件', icon: Zap },
    ],
  },
  {
    label: '策略配置',
    items: [
      { id: 'promptlib', label: '提示词策略', icon: FileText },
      { id: 'council', label: '模型委员会', icon: Users },
      { id: 'llm', label: '模型连接', icon: Cpu },
      { id: 'agents', label: '运行单元', icon: Package },
    ],
  },
  {
    label: '集成与保障',
    items: [
      { id: 'notify', label: '通知渠道', icon: MessageCircle },
      { id: 'backup', label: '备份与恢复', icon: HardDrive },
      { id: 'plugins', label: '插件能力', icon: Package },
    ],
  },
  {
    label: '治理',
    items: [
      { id: 'audit', label: '操作审计', icon: Scroll },
      { id: 'adminsys', label: '管理员与密码', icon: UserCog },
      { id: 'about', label: '版本与更新', icon: Info },
    ],
  },
] as const

function navigateTo(id: string) {
  router.push(`/admin/${id}`)
}

function handleLogout() {
  auth.logout()
  router.push('/admin/login')
}

const currentView = computed<string>(() => {
  const seg = route.path.split('/').filter(Boolean).pop() || 'overview'
  return seg
})
const currentLabel = computed<string>(() => {
  for (const group of navGroups) {
    const hit = (group.items as readonly { id: string; label: string }[]).find((i) => i.id === currentView.value)
    if (hit) return hit.label
  }
  return currentView.value
})
</script>

<template>
  <div class="min-h-screen bg-[#080B10] text-[#F3F4F6] flex flex-col md:flex-row">
    <!-- Sidebar (desktop) / horizontal nav (mobile) -->
    <aside class="w-full md:w-[210px] md:shrink-0 border-b md:border-b-0 md:border-r border-[#1A2232] bg-[#0A0D14] md:flex md:flex-col md:h-screen md:sticky md:top-0">
      <!-- Brand -->
      <div class="px-4 py-2.5 md:py-4 border-b border-[#1A2232] hidden md:flex items-center space-x-2.5">
        <div class="w-8 h-8 rounded-lg bg-gradient-to-tr from-blue-600 via-indigo-600 to-cyan-400 flex items-center justify-center shadow-lg shadow-blue-500/20 ring-1 ring-white/20">
          <span class="text-white font-black text-base tracking-wider">R</span>
        </div>
        <div>
          <div class="text-sm font-bold text-white tracking-wide">R20 CONTROL</div>
          <div class="text-[10px] text-[#707E94] font-mono">QUANTUM TRADER v6.2.1</div>
        </div>
      </div>

      <!-- Nav Groups: horizontal scroll strip on mobile, vertical list on desktop -->
      <nav class="overflow-x-auto md:overflow-y-auto md:overflow-x-hidden md:flex-1 py-1.5 md:py-2 px-2 md:space-y-1 flex md:block whitespace-nowrap">
        <div v-for="group in navGroups" :key="group.label" class="mb-2 md:mb-2 inline-block md:block mr-4 md:mr-0 align-top">
          <div class="text-[10px] font-mono font-bold text-[#556677] uppercase tracking-wider px-3 py-1.5">{{ group.label }}</div>
          <div class="flex md:block space-x-1 md:space-x-0">
            <button
              v-for="item in group.items"
              :key="item.id"
              @click="navigateTo(item.id)"
              class="flex items-center space-x-1.5 md:space-x-2.5 px-2.5 md:px-3 py-1.5 md:py-2 rounded-lg text-[11px] md:text-xs font-mono font-medium transition-all cursor-pointer shrink-0"
              :class="currentView === item.id
                ? 'bg-gradient-to-r from-blue-600/20 to-transparent text-white border border-blue-500/40 md:border-0 md:border-l-2 md:border-l-blue-400'
                : 'text-[#707E94] hover:text-white hover:bg-[#121824]'"
            >
              <component :is="item.icon" class="w-3.5 h-3.5 shrink-0" />
              <span>{{ item.label }}</span>
            </button>
          </div>
        </div>
      </nav>

      <!-- Footer (desktop only) -->
      <div class="hidden md:block px-4 py-3 border-t border-[#1A2232] text-[10px] font-mono text-[#556677] space-y-1">
        <div class="flex items-center space-x-1">
          <span class="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
          <span>www.r20.cn</span>
        </div>
        <div>配置落盘 .env · 0600</div>
        <div>危险动作强制二次确认</div>
      </div>
    </aside>

    <!-- Main -->
    <div class="flex-1 flex flex-col min-w-0">
      <!-- Topbar -->
      <header class="min-h-14 border-b border-[#1A2232] bg-[#0A0D14]/80 backdrop-blur-md flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 px-3 sm:px-5 py-2 md:py-0 shrink-0 sticky top-0 z-30">
        <div>
          <div class="text-sm font-bold text-white">{{ currentLabel }}</div>
          <div class="text-[11px] text-[#707E94] font-mono">R20 Quantum Trader 控制面</div>
        </div>
        <div class="flex items-center space-x-2 sm:space-x-3">
          <div class="flex items-center space-x-2 bg-[#0D121B] px-2.5 sm:px-3 py-1.5 rounded-lg border border-[#1A2232]">
            <div class="w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center font-bold text-xs">
              {{ auth.user?.username?.[0]?.toUpperCase() || 'A' }}
            </div>
            <div class="text-xs">
              <div class="font-bold text-white">{{ auth.user?.username || 'admin' }}</div>
              <div class="text-[10px] text-[#707E94]">{{ auth.user?.role || '超级管理员' }}</div>
            </div>
          </div>
          <a href="/" target="_blank" class="flex items-center space-x-1 px-2.5 sm:px-3 py-1.5 rounded-lg bg-[#0D121B] hover:bg-[#141B26] border border-[#1A2232] text-xs font-mono text-[#707E94] hover:text-white transition-colors">
            <LayoutGrid class="w-3.5 h-3.5" />
            <span>大屏</span>
          </a>
          <button @click="handleLogout" class="flex items-center space-x-1 px-2.5 sm:px-3 py-1.5 rounded-lg bg-[#4d1924] hover:bg-[#5d2230] border border-[#873044] text-xs font-mono text-[#ffdce1] transition-colors cursor-pointer">
            <LogOut class="w-3.5 h-3.5" />
            <span>退出</span>
          </button>
        </div>
      </header>

      <!-- Content -->
      <main class="flex-1 p-3 sm:p-5 overflow-x-hidden">
        <router-view />
      </main>
    </div>
  </div>
</template>
