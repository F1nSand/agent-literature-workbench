<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { useHealthStore } from './stores/health'
import { useWorkspaceChromeStore } from './stores/workspaceChrome'
import { useWorkspaceNotesStore } from './stores/workspaceNotes'
import { useWorkspaceAssistantStore } from './stores/workspaceAssistant'
import { useWorkspaceLayoutStore } from './stores/workspaceLayout'
import WorkspaceHost from './components/workspace/WorkspaceHost.vue'
import SvgIcon from './components/SvgIcon.vue'
import MotionBackdrop from './components/MotionBackdrop.vue'
import { useSurfaceMotion } from './composables/useSurfaceMotion'
import './workspace.css'

const route = useRoute()
const health = useHealthStore()
const chrome = useWorkspaceChromeStore()
const notes = useWorkspaceNotesStore()
const assistant = useWorkspaceAssistantStore()
const layout = useWorkspaceLayoutStore()
const { enabled: motionEnabled, reduced: motionReduced, setEnabled: setMotionEnabled } = useSurfaceMotion()
const viewportWidth = ref(1200)
const readerRoute = computed(() => route.path.startsWith('/documents/') && route.path !== '/documents')
const sidebarCollapsed = computed(() => layout.sidebarMode === 'collapsed' || (layout.sidebarMode === 'auto' && (readerRoute.value || viewportWidth.value <= 1000)))
function updateViewport() { viewportWidth.value = window.innerWidth }

const navigation = [
  { label: '概览', to: '/', icon: 'overview' },
  { label: '文献库', to: '/documents', icon: 'library' },
]

function isCurrent(to: string): boolean {
  if (to === '/documents') return route.path.startsWith('/documents')
  return route.path === to
}

async function startNewNote() {
  try { await notes.create(); chrome.newProject('notes') }
  catch (error) { window.alert(error instanceof Error ? error.message : '无法新建笔记。') }
}

async function startNewConversation() {
  try { await assistant.newConversation(); chrome.newProject('assistant') }
  catch (error) { window.alert(error instanceof Error ? error.message : '无法新建对话。') }
}

onMounted(() => { health.refresh(); updateViewport(); window.addEventListener('resize', updateViewport, { passive: true }) })
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))
</script>

<template>
  <MotionBackdrop />
  <div class="app-frame" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <aside class="app-sidebar" :aria-label="sidebarCollapsed ? '折叠的工作台导航' : '工作台导航'">
      <div class="sidebar-controls">
        <button type="button" class="sidebar-toggle" :aria-label="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" :title="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" @click="layout.toggleManual(sidebarCollapsed)">{{ sidebarCollapsed ? '›' : '‹' }}</button>
        <button v-if="layout.sidebarMode !== 'auto'" type="button" class="sidebar-auto" aria-label="恢复自动侧栏" title="恢复自动侧栏" @click="layout.setSidebarMode('auto')">自动</button>
      </div>
      <RouterLink class="brand" to="/" aria-label="研读工作台首页">
        <span class="brand-mark">研</span>
        <span class="brand-copy"><strong>文献研读</strong><small>RESEARCH DESK</small></span>
      </RouterLink>

      <div class="workspace-label">我的空间</div>
      <nav class="main-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navigation"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ 'is-current': isCurrent(item.to) }"
          :aria-label="item.label"
          :title="item.label"
        >
          <span class="nav-icon"><SvgIcon :name="item.icon" /></span>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="workspace-tools">
        <div class="workspace-tool-row">
          <button class="nav-item workspace-tool-main" :class="{ 'is-current': chrome.windows.notes.visible }" aria-label="恢复阅读笔记" title="阅读笔记" @click="chrome.show('notes')"><span class="nav-icon"><SvgIcon name="note" /></span><span>阅读笔记</span></button>
          <button class="workspace-tool-plus" aria-label="新建笔记" title="新建笔记" @click="startNewNote"><SvgIcon name="plus" :size="16" /></button>
          <button class="workspace-tool-history" aria-label="笔记历史" title="笔记历史" @click="chrome.setHistory('notes', true)"><SvgIcon name="history" :size="16" /></button>
        </div>
        <div class="workspace-tool-row">
          <button class="nav-item workspace-tool-main" :class="{ 'is-current': chrome.windows.assistant.visible }" aria-label="恢复研读助手" title="研读助手" @click="chrome.show('assistant')"><span class="nav-icon"><SvgIcon name="assistant" /></span><span>研读助手</span></button>
          <button class="workspace-tool-plus" aria-label="新建对话" title="新建对话" @click="startNewConversation"><SvgIcon name="plus" :size="16" /></button>
          <button class="workspace-tool-history" aria-label="对话历史" title="对话历史" @click="chrome.toggleHistory('assistant')"><SvgIcon name="history" :size="16" /></button>
        </div>
      </div>
      <div class="sidebar-spacer"></div>
      <nav class="secondary-nav" aria-label="辅助导航">
        <RouterLink class="nav-item" to="/settings" aria-label="设置" title="设置"><span class="nav-icon"><SvgIcon name="settings" /></span><span>设置</span></RouterLink>
      </nav>
      <div class="profile-row">
        <div class="profile-avatar">研</div>
        <div class="profile-copy"><strong>个人空间</strong><small>文献研读工作台</small></div>
      </div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div class="breadcrumbs"><span>文献研读</span><b>/</b><strong>{{ route.meta.title ?? '概览' }}</strong></div>
        <div class="topbar-actions">
          <button class="motion-toggle" type="button" :aria-pressed="motionEnabled" :aria-label="motionReduced ? `界面动效${motionEnabled ? '已开启' : '已暂停'}；系统减少动态效果已生效` : motionEnabled ? '关闭界面动效' : '开启界面动效'" @click="setMotionEnabled(!motionEnabled)"><i aria-hidden="true"></i><span>{{ motionReduced ? '系统静止' : motionEnabled ? '动效' : '静止' }}</span></button>
          <span class="environment-tag"><i></i> 本机工作台</span>
        </div>
      </header>
      <div v-if="health.status === 'unavailable'" class="service-banner service-unavailable" role="alert">
        <span class="service-dot"></span>
        <span>{{ health.message }}</span>
        <button class="service-retry" @click="health.refresh">重试</button>
      </div>
      <RouterView />
      <footer class="page-footer"><span>文献研读工作台</span><span>你的资料保存在本机</span></footer>
    </main>
  </div>
  <WorkspaceHost />
</template>
