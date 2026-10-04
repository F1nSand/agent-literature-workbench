<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import VueDraggableResizable from 'vue-draggable-resizable'
import NotesWindow from './NotesWindow.vue'
import AssistantWindow from './AssistantWindow.vue'
import SvgIcon from '../SvgIcon.vue'
import { useWorkspaceAssistantStore } from '../../stores/workspaceAssistant'
import { useWorkspaceChromeStore } from '../../stores/workspaceChrome'
import { useWorkspaceContextStore } from '../../stores/workspaceContext'
import { useWorkspaceNotesStore } from '../../stores/workspaceNotes'
import type { WorkspaceUiAction } from '../../types/workspace'

const chrome = useWorkspaceChromeStore()
const assistant = useWorkspaceAssistantStore()
const notes = useWorkspaceNotesStore()
const context = useWorkspaceContextStore()
const route = useRoute()
const router = useRouter()

function syncPageContext() {
  const path = route.path
  if (path.startsWith('/documents/')) context.setPageType('reader')
  else if (path.startsWith('/documents')) context.setPageType('library')
  else if (path.startsWith('/settings')) context.setPageType('settings')
  else if (path === '/') context.setPageType('home')
  else context.setPageType('assistant')
}
watch(() => route.path, syncPageContext, { immediate: true })
watch(() => [notes.currentId, notes.revision] as const, ([noteId, revision]) => {
  context.setNoteContext(noteId || undefined, noteId ? revision : undefined)
}, { immediate: true })
onMounted(() => {
  void assistant.load()
  void notes.load()
  window.addEventListener('keydown', onGlobalShortcut)
})

function onGlobalShortcut(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'n') {
    event.preventDefault(); chrome.show('notes')
  }
}
function geometryChanged(id: 'notes' | 'assistant', x: number, y: number) { chrome.update(id, { x, y }) }
function sizeChanged(id: 'notes' | 'assistant', x: number, y: number, w: number, h: number) { chrome.update(id, { x, y, w, h }) }

async function runAction(action: WorkspaceUiAction) {
  try {
    if (action.type === 'open_document' && action.document_id) {
      const query: Record<string, string> = {}
      if (action.version_id) query.version = action.version_id
      const locator = action.locator
      if (locator && 'page' in locator && typeof locator.page === 'number') query.page = String(locator.page)
      if (locator && typeof locator.chapter === 'string') query.chapter = locator.chapter
      if (locator && typeof locator.cfi === 'string') query.cfi = locator.cfi
      if (locator && typeof locator.anchor === 'string') query.anchor = locator.anchor
      await router.push({ path: `/documents/${encodeURIComponent(action.document_id)}`, query })
    } else if (action.type === 'focus_note') {
      if (action.note_id) await notes.select(action.note_id)
      chrome.setHistory('notes', false)
    } else throw new Error('不支持这项页面操作。')
  } catch (error) {
    await assistant.acknowledgeAction(action.action_id, { status: 'failed', error: error instanceof Error ? error.message : '页面操作失败' }).catch(() => undefined)
    return
  }
  await assistant.acknowledgeAction(action.action_id, { status: 'succeeded' }).catch(() => undefined)
}
watch(() => assistant.pendingActions.map(({ runId, action, historical }) => `${runId}:${action.action_id}:${historical}`).join(','), () => {
  const pending = [...assistant.pendingActions]
  for (const entry of pending) {
    if (!entry.historical && !entry.acknowledged && (entry.action.type === 'open_document' || entry.action.type === 'focus_note') && assistant.claimAction(entry.action.action_id)) void runAction(entry.action)
  }
})

</script>

<template>
  <div class="workspace-host" aria-label="全局工具窗口">
    <VueDraggableResizable class-name="workspace-vdr" class-name-draggable="workspace-vdr-dragging" :class="{ 'workspace-window-hidden': !chrome.windows.notes.visible }" :disable-user-select="false" :x="chrome.windows.notes.x" :y="chrome.windows.notes.y" :w="chrome.windows.notes.w" :h="chrome.windows.notes.h" :z="chrome.windows.notes.z" :min-width="chrome.minimum('notes').w" :min-height="chrome.minimum('notes').h" :parent="true" :drag-handle="'.workspace-drag-handle'" :handles="['tl','tm','tr','mr','br','bm','bl','ml']" @dragging="(x:number,y:number) => geometryChanged('notes',x,y)" @resizing="(x:number,y:number,w:number,h:number) => sizeChanged('notes',x,y,w,h)" @activated="chrome.focus('notes')" @mousedown="chrome.focus('notes')">
      <Transition name="workspace-window">
      <div v-show="chrome.windows.notes.visible" class="workspace-floating-window note-floating-window">
        <header class="workspace-window-bar workspace-drag-handle"><div class="window-mark note-mark"><SvgIcon name="note" :size="15" /></div><strong>{{ chrome.windows.notes.history ? '笔记历史' : '阅读笔记' }}</strong><span class="window-bar-spacer"></span><button class="window-bar-button" title="最小化" aria-label="最小化笔记" @mousedown.stop @click.stop="chrome.hide('notes')"><SvgIcon name="minimize" :size="15" /></button><button class="window-bar-button close-window" title="隐藏" aria-label="隐藏笔记" @mousedown.stop @click.stop="chrome.hide('notes')"><SvgIcon name="close" :size="15" /></button></header>
        <NotesWindow @close="chrome.hide('notes')" @minimize="chrome.hide('notes')" />
      </div>
      </Transition>
    </VueDraggableResizable>

    <VueDraggableResizable class-name="workspace-vdr" class-name-draggable="workspace-vdr-dragging" :class="{ 'workspace-window-hidden': !chrome.windows.assistant.visible }" :disable-user-select="false" :x="chrome.windows.assistant.x" :y="chrome.windows.assistant.y" :w="chrome.windows.assistant.w" :h="chrome.windows.assistant.h" :z="chrome.windows.assistant.z" :min-width="chrome.minimum('assistant').w" :min-height="chrome.minimum('assistant').h" :parent="true" :drag-handle="'.workspace-drag-handle'" :handles="['tl','tm','tr','mr','br','bm','bl','ml']" @dragging="(x:number,y:number) => geometryChanged('assistant',x,y)" @resizing="(x:number,y:number,w:number,h:number) => sizeChanged('assistant',x,y,w,h)" @activated="chrome.focus('assistant')" @mousedown="chrome.focus('assistant')">
      <Transition name="workspace-window">
      <div v-show="chrome.windows.assistant.visible" class="workspace-floating-window assistant-floating-window">
        <header class="workspace-window-bar workspace-drag-handle"><div class="window-mark assistant-mark"><SvgIcon name="assistant" :size="15" /></div><strong>研读助手</strong><span v-if="assistant.activeConversation" class="window-current-project">{{ assistant.activeConversation.title }}</span><span class="window-bar-spacer"></span><button class="window-bar-button" title="最小化" aria-label="最小化助手" @mousedown.stop @click.stop="chrome.hide('assistant')"><SvgIcon name="minimize" :size="15" /></button><button class="window-bar-button close-window" title="隐藏" aria-label="隐藏助手" @mousedown.stop @click.stop="chrome.hide('assistant')"><SvgIcon name="close" :size="15" /></button></header>
        <AssistantWindow @close="chrome.hide('assistant')" @minimize="chrome.hide('assistant')" @toggle-history="chrome.toggleHistory('assistant')" />
      </div>
      </Transition>
    </VueDraggableResizable>
  </div>
</template>
