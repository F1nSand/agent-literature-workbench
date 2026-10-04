import { defineStore } from 'pinia'
import { reactive } from 'vue'

export interface WindowRect { x: number; y: number; w: number; h: number; visible: boolean; history: boolean; z: number }
const key = 'research-desk.workspace-windows'
const defaults = {
  notes: { x: 240, y: 100, w: 500, h: 520, visible: false, history: false, z: 20 },
  assistant: { x: 630, y: 80, w: 540, h: 600, visible: false, history: false, z: 21 },
}

export const useWorkspaceChromeStore = defineStore('workspace-chrome', () => {
  let saved: Partial<Record<keyof typeof defaults, Partial<WindowRect>>> = {}
  try { saved = JSON.parse(localStorage.getItem(key) ?? '{}') as typeof saved } catch { /* Ignore invalid local window preferences. */ }
  const windows = reactive({
    notes: { ...defaults.notes, ...saved.notes },
    assistant: { ...defaults.assistant, ...saved.assistant },
  })
  let zIndex = Math.max(windows.notes.z, windows.assistant.z)

  function persist() { localStorage.setItem(key, JSON.stringify(windows)) }
  function clamp(id: keyof typeof windows) {
    const item = windows[id]
    const minWidth = Math.min(id === 'notes' ? 360 : 420, Math.max(240, window.innerWidth - 16))
    const minHeight = Math.min(id === 'notes' ? 300 : 440, Math.max(280, window.innerHeight - 16))
    item.w = Math.max(minWidth, Math.min(item.w, Math.max(minWidth, window.innerWidth - 16)))
    item.h = Math.max(minHeight, Math.min(item.h, Math.max(minHeight, window.innerHeight - 16)))
    item.x = Math.max(0, Math.min(item.x, Math.max(0, window.innerWidth - item.w)))
    item.y = Math.max(0, Math.min(item.y, Math.max(0, window.innerHeight - item.h)))
  }
  function show(id: keyof typeof windows) { const item = windows[id]; clamp(id); item.visible = true; item.z = ++zIndex; persist() }
  function hide(id: keyof typeof windows) { windows[id].visible = false; persist() }
  function toggleHistory(id: keyof typeof windows) { const item = windows[id]; item.history = !item.history; show(id); persist() }
  function setHistory(id: keyof typeof windows, visible: boolean) { windows[id].history = visible; show(id); persist() }
  function update(id: keyof typeof windows, geometry: Partial<Pick<WindowRect, 'x' | 'y' | 'w' | 'h'>>) {
    Object.assign(windows[id], geometry); clamp(id); persist()
  }
  function focus(id: keyof typeof windows) { windows[id].z = ++zIndex; persist() }
  function minimum(id: keyof typeof windows) {
    return {
      w: Math.min(id === 'notes' ? 360 : 420, Math.max(240, window.innerWidth - 16)),
      h: Math.min(id === 'notes' ? 300 : 440, Math.max(280, window.innerHeight - 16)),
    }
  }
  function newProject(id: keyof typeof windows) { show(id); if (id === 'notes') windows.notes.history = false; else windows.assistant.history = false; persist() }
  window.addEventListener('resize', () => { clamp('notes'); clamp('assistant'); persist() })
  return { windows, show, hide, toggleHistory, setHistory, update, focus, newProject, minimum }
})
