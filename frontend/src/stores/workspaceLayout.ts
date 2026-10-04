import { defineStore } from 'pinia'
import { ref } from 'vue'

export type SidebarMode = 'auto' | 'expanded' | 'collapsed'

const STORAGE_KEY = 'workspace-layout-v1'

function readMode(): SidebarMode {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null') as { sidebarMode?: unknown } | null
    if (value?.sidebarMode === 'expanded' || value?.sidebarMode === 'collapsed') return value.sidebarMode
  } catch { /* Use automatic behavior when persisted preferences are unavailable. */ }
  return 'auto'
}

export const useWorkspaceLayoutStore = defineStore('workspaceLayout', () => {
  const sidebarMode = ref<SidebarMode>(readMode())

  function setSidebarMode(mode: SidebarMode) {
    sidebarMode.value = mode
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ sidebarMode: mode })) }
    catch { /* Layout remains usable when browser storage is disabled. */ }
  }

  function toggleManual(currentlyCollapsed: boolean) {
    setSidebarMode(currentlyCollapsed ? 'expanded' : 'collapsed')
  }

  return { sidebarMode, setSidebarMode, toggleManual }
})
