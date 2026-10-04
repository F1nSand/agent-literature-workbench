<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useWorkspaceNotesStore } from '../../stores/workspaceNotes'
import { useWorkspaceChromeStore } from '../../stores/workspaceChrome'
import type { WorkspaceNote } from '../../types/workspace'

const store = useWorkspaceNotesStore()
const chrome = useWorkspaceChromeStore()
const notes = computed(() => store.notes.filter((note) => !note.archived_at))
const pendingDeleteId = ref('')
const deletingId = ref('')
const deleteError = ref('')

onMounted(() => { void store.load() })

function currentText(note: WorkspaceNote) {
  if (note.id !== store.currentId || !store.dirty) return note.plain_text
  const pieces: string[] = []
  const visit = (node: any) => {
    if (node.type === 'text') pieces.push(node.text)
    else if (node.type === 'hardBreak') pieces.push('\n')
    else {
      for (const child of node.content ?? []) visit(child)
      if (['paragraph', 'heading', 'listItem', 'blockquote', 'codeBlock'].includes(node.type)) pieces.push('\n')
    }
  }
  visit(store.content)
  return pieces.join('').trim()
}

function titleOf(note: WorkspaceNote) {
  return currentText(note).split(/\r?\n/, 1)[0]?.trim().slice(0, 500) || '未命名笔记'
}

function summaryOf(note: WorkspaceNote) {
  const lines = currentText(note).split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  const summary = lines.slice(1).join(' · ') || lines[0] || ''
  return summary.length > 180 ? `${summary.slice(0, 180)}…` : summary
}

function displayTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

async function openNote(note: WorkspaceNote) {
  await store.select(note.id)
  chrome.setHistory('notes', false)
}

async function createNote() {
  await store.create()
  chrome.setHistory('notes', false)
}

function requestDelete(note: WorkspaceNote) {
  deleteError.value = ''
  pendingDeleteId.value = pendingDeleteId.value === note.id ? '' : note.id
}

async function confirmDelete(note: WorkspaceNote) {
  if (deletingId.value) return
  deletingId.value = note.id
  deleteError.value = ''
  try {
    await store.remove(note.id)
    pendingDeleteId.value = ''
  } catch (error) {
    deleteError.value = error instanceof Error ? error.message : '笔记删除失败，请重试。'
  } finally {
    deletingId.value = ''
  }
}
</script>

<template>
  <section class="notes-history-page" aria-label="笔记历史">
    <header class="notes-history-header">
      <button class="workspace-quiet-button" @click="chrome.setHistory('notes', false)">返回笔记</button>
      <span class="notes-history-count">{{ notes.length }} 条笔记</span>
      <button class="workspace-primary" @click="createNote">新建笔记</button>
    </header>
    <section v-if="notes.length" class="notes-history-grid" aria-label="阅读笔记">
      <article v-for="note in notes" :key="note.id" class="notes-history-card">
        <button class="notes-history-card-main" @click="openNote(note)">
          <h2>{{ titleOf(note) }}</h2>
          <p>{{ summaryOf(note) || '（空白笔记）' }}</p>
          <time :datetime="note.updated_at">{{ displayTime(note.updated_at) }}</time>
        </button>
        <footer>
          <button class="workspace-primary" @click="openNote(note)">打开</button>
          <button class="workspace-quiet-button" :aria-expanded="pendingDeleteId === note.id" @click="requestDelete(note)">{{ pendingDeleteId === note.id ? '取消' : '删除' }}</button>
        </footer>
        <div v-if="pendingDeleteId === note.id" class="notes-delete-confirm" role="alertdialog" aria-modal="false" :aria-label="`确认删除${titleOf(note)}`" :aria-busy="deletingId === note.id">
          <p>永久删除“{{ titleOf(note) }}”及其全部版本和候选？此操作无法撤销。</p>
          <p v-if="deleteError" role="alert">{{ deleteError }}</p>
          <div><button class="workspace-quiet-button" :disabled="deletingId === note.id" @click="pendingDeleteId = ''">取消</button><button class="notes-delete-confirm-action" :disabled="deletingId === note.id" @click="confirmDelete(note)">{{ deletingId === note.id ? '正在删除…' : '永久删除' }}</button></div>
        </div>
      </article>
    </section>
    <div v-else-if="store.status === 'loading'" class="notes-history-empty" role="status">正在载入笔记…</div>
    <div v-else-if="store.error" class="notes-history-empty" role="alert">{{ store.error }} <button class="workspace-quiet-button" @click="store.load">重新载入</button></div>
    <div v-else class="notes-history-empty">还没有笔记。</div>
  </section>
</template>

<style scoped>
.notes-history-page { flex:1; min-height:0; overflow:auto; padding:17px; background:#fffefa; }
.notes-history-header { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
.notes-history-count { flex:1; color:#52695a; font-size:13px; }
.notes-history-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr)); gap:12px; }
.notes-history-card { position:relative; min-height:200px; display:flex; flex-direction:column; justify-content:space-between; padding:17px; border:1px solid #dfe6dd; border-radius:11px; background:#fffefa; box-shadow:0 8px 24px #3541320a; }
.notes-history-card-main { display:block; padding:0; border:0; color:inherit; background:none; text-align:left; cursor:pointer; }
.notes-history-card h2 { margin:0 0 10px; overflow:hidden; color:#304538; font-size:16px; font-weight:600; text-overflow:ellipsis; white-space:nowrap; }
.notes-history-card p { min-height:42px; margin:0 0 14px; overflow:hidden; color:#52685a; font-size:13px; line-height:1.7; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; }
.notes-history-card time { color:#5b6e5e; font-size:12px; }
.notes-history-card footer { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
.notes-delete-confirm { position:absolute; z-index:2; inset:auto 8px 8px; padding:12px; border:1px solid #e1b8ad; border-radius:8px; background:#fff9f6; box-shadow:0 8px 24px #38180f20; }
.notes-delete-confirm p { margin:0 0 10px; color:#65463d; font-size:13px; line-height:1.6; }
.notes-delete-confirm>div { display:flex; justify-content:flex-end; gap:8px; }
.notes-delete-confirm-action { padding:6px 10px; border:0; border-radius:5px; color:white; background:#a54e3d; cursor:pointer; }
.notes-history-empty { padding:60px 20px; color:#566b5a; text-align:center; font-size:14px; }
</style>
