import { defineStore } from 'pinia'
import { computed, ref, toRaw } from 'vue'
import {
  adoptWorkspaceNoteVersion, deleteWorkspaceNote, createWorkspaceNote, createWorkspaceNoteCandidate,
  getWorkspaceNote, listWorkspaceNoteVersions, listWorkspaceNotes, updateWorkspaceNote,
} from '../api/workspaceNotes'
import type { NoteCandidate, NoteContent, NoteVersion, WorkspaceNote } from '../types/workspace'

interface NoteDraft { id: string; title: string; content: NoteContent; baseRevision: number; changedAt: number }
const DB_NAME = 'research-desk-workspace'
const DB_VERSION = 1
const STORE_NAME = 'note-drafts'
const blank: NoteContent = { type: 'doc', content: [{ type: 'paragraph' }] }
function plainText(nodes: unknown): string {
  if (!Array.isArray(nodes)) return ''
  return nodes.map((node) => {
    if (!node || typeof node !== 'object') return ''
    const value = node as { text?: unknown; content?: unknown; type?: unknown }
    if (typeof value.text === 'string') return value.text
    const child = plainText(value.content)
    return value.type === 'hardBreak' || value.type === 'paragraph' || value.type === 'heading' || value.type === 'listItem' || value.type === 'blockquote' ? `${child}\n` : child
  }).join('').trim()
}

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const opening = indexedDB.open(DB_NAME, DB_VERSION)
    opening.onupgradeneeded = () => opening.result.createObjectStore(STORE_NAME, { keyPath: 'id' })
    opening.onsuccess = () => resolve(opening.result)
    opening.onerror = () => reject(opening.error)
  })
}

async function draftRequest<T>(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await database()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, mode)
    const req = action(tx.objectStore(STORE_NAME))
    let result: T
    req.onsuccess = () => { result = req.result }
    req.onerror = () => reject(req.error)
    tx.oncomplete = () => { db.close(); resolve(result!) }
    tx.onabort = () => { db.close(); reject(tx.error) }
    tx.onerror = () => reject(tx.error)
  })
}

const key = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(toRaw(value))) as T
const draftStorageKey = (id: string) => `research-desk.note-draft.${id}`

export const useWorkspaceNotesStore = defineStore('workspace-notes', () => {
  const notes = ref<WorkspaceNote[]>([])
  const archivedNotes = ref<WorkspaceNote[]>([])
  const currentId = ref(localStorage.getItem('research-desk.current-note') ?? '')
  const title = ref('')
  const content = ref<NoteContent>(clone(blank))
  const revision = ref(0)
  const currentVersionId = ref<string | null>(null)
  const dirty = ref(false)
  const status = ref<'idle' | 'loading' | 'saved' | 'offline' | 'conflict' | 'error'>('idle')
  const error = ref('')
  const candidate = ref<NoteCandidate | null>(null)
  const versions = ref<NoteVersion[]>([])
  const saveFlights = new Map<string, Promise<void>>()
  const deletedIds = new Set<string>()
  const editGeneration = new Map<string, number>()
  const draftBaseRevision = new Map<string, number>()
  const saving = computed(() => saveFlights.has(currentId.value))
  const versionDiff = computed(() => {
    if (!candidate.value) return { before: '', after: '' }
    const current = plainText(content.value.content)
    return { before: current, after: candidate.value.candidate.plain_text }
  })
  const currentArchived = computed(() => false)
  let saveTimer = 0
  let persistChain = Promise.resolve()
  let selectionGeneration = 0
  let listLoadGeneration = 0

  function generation(id: string) { return editGeneration.get(id) ?? 0 }
  function advanceGeneration(id: string) { editGeneration.set(id, generation(id) + 1); return generation(id) }
  function deriveTitle(value: NoteContent): string {
    const lines: string[] = []
    const visit = (node: any) => {
      if (node.type === 'text') lines.push(node.text)
      else if (node.type === 'hardBreak') lines.push('\n')
      else {
        for (const child of node.content ?? []) visit(child)
        if (['paragraph', 'heading', 'listItem', 'blockquote', 'codeBlock'].includes(node.type)) lines.push('\n')
      }
    }
    visit(value)
    return lines.join('').split(/\r?\n/, 1)[0]?.trim().slice(0, 500) || '未命名笔记'
  }

  function clearCurrent(id: string) {
    if (currentId.value !== id) return
    currentId.value = ''
    localStorage.removeItem('research-desk.current-note')
    candidate.value = null; versions.value = []
    title.value = ''; content.value = clone(blank); revision.value = 0; dirty.value = false
    currentVersionId.value = null; status.value = 'idle'; error.value = ''
  }

  function setFailure(cause: unknown, fallback: string) {
    const statusCode = cause && typeof cause === 'object' && 'status' in cause ? Number(cause.status) : 0
    status.value = statusCode === 409 ? 'conflict' : statusCode >= 400 && statusCode < 500 ? 'error' : 'offline'
    const message = cause instanceof Error ? cause.message : ''
    error.value = statusCode === 422 && (!message || message.startsWith('请求失败'))
      ? '服务器未接受这次笔记保存（HTTP 422），本地草稿仍保留。请检查内容后重试。'
      : statusCode === 0
        ? '连接笔记服务失败，草稿已保存在本机。恢复连接后可以重试。'
        : message || fallback
  }

  async function persistLocal() {
    if (!currentId.value) return
    const id = currentId.value
    if (deletedIds.has(id)) return
    const draft: NoteDraft = { id, title: title.value, content: clone(content.value), baseRevision: draftBaseRevision.get(id) ?? revision.value, changedAt: Date.now() }
    try { localStorage.setItem(draftStorageKey(draft.id), JSON.stringify(draft)) }
    catch { throw new Error('浏览器空间不足，无法安全保存本地草稿。') }
    persistChain = persistChain.catch(() => undefined).then(() => draftRequest('readwrite', (store) => store.put(draft))).then(() => undefined)
    await persistChain
  }

  async function readDraft(id: string): Promise<NoteDraft | undefined> {
    const indexed = await draftRequest<NoteDraft | undefined>('readonly', (store) => store.get(id)).catch(() => undefined)
    const local = readLocalDraft(id)
    if (indexed && local) return indexed.changedAt >= local.changedAt ? indexed : local
    return indexed ?? local
  }

  function readLocalDraft(id: string): NoteDraft | undefined {
    try { return JSON.parse(localStorage.getItem(draftStorageKey(id)) ?? 'null') as NoteDraft | undefined }
    catch { return undefined }
  }

  function upsertNote(note: WorkspaceNote): WorkspaceNote {
    if (deletedIds.has(note.id)) return note
    const known = notes.value.find((item) => item.id === note.id) ?? archivedNotes.value.find((item) => item.id === note.id)
    if (known && (known.revision > note.revision || (known.archived_at && !note.archived_at))) return known
    if (note.archived_at) {
      notes.value = notes.value.filter((item) => item.id !== note.id)
      archivedNotes.value = [note, ...archivedNotes.value.filter((item) => item.id !== note.id)]
    } else {
      archivedNotes.value = archivedNotes.value.filter((item) => item.id !== note.id)
      notes.value = notes.value.some((item) => item.id === note.id)
        ? notes.value.map((item) => item.id === note.id ? note : item)
        : [note, ...notes.value]
    }
    return note
  }

  async function deleteDraft(id: string) {
    try { localStorage.removeItem(draftStorageKey(id)) } catch { /* Keep IndexedDB as the durable copy. */ }
    await draftRequest('readwrite', (store) => store.delete(id))
  }

  function applyNote(note: WorkspaceNote) {
    if (deletedIds.has(note.id)) return
    const latest = upsertNote(note)
    if (currentId.value !== latest.id) return
    revision.value = latest.revision
    draftBaseRevision.set(latest.id, latest.revision)
    currentVersionId.value = latest.current_version_id
    title.value = latest.title
    content.value = clone(latest.content)
    dirty.value = false
    status.value = 'saved'
  }

  async function load() {
    const request = ++listLoadGeneration
    const startingId = currentId.value
    const startingSelection = selectionGeneration
    const startingEditGeneration = startingId ? generation(startingId) : 0
    if (!dirty.value) status.value = 'loading'
    try {
      const loaded = await listWorkspaceNotes()
      if (request !== listLoadGeneration) return
      notes.value = loaded.filter((note) => !deletedIds.has(note.id))
      if (selectionGeneration !== startingSelection || currentId.value !== startingId) return
      if (startingId) await select(startingId)
      else if (loaded.length) await select(loaded[0].id)
      else if (!startingId || generation(startingId) === startingEditGeneration) { status.value = 'idle'; error.value = '' }
    } catch (cause) {
      if (request !== listLoadGeneration || selectionGeneration !== startingSelection || currentId.value !== startingId) return
      setFailure(cause, '笔记列表暂不可用。')
      const draftGeneration = startingId ? generation(startingId) : 0
      const localDraft = startingId ? await readDraft(startingId) : undefined
      if (request !== listLoadGeneration || selectionGeneration !== startingSelection || currentId.value !== startingId) return
      if (draftGeneration === generation(startingId) && startingEditGeneration === draftGeneration && localDraft) {
        title.value = localDraft.title; content.value = clone(localDraft.content); revision.value = localDraft.baseRevision; draftBaseRevision.set(startingId, localDraft.baseRevision); dirty.value = true
      }
    }
  }

  async function loadArchived() {
    const all = await listWorkspaceNotes(true)
    archivedNotes.value = all.filter((note) => Boolean(note.archived_at))
  }

  async function select(id: string) {
    if (deletedIds.has(id)) return
    const request = ++selectionGeneration
    await flush()
    if (request !== selectionGeneration) return
    const switchingNote = currentId.value !== id
    currentId.value = id
    if (switchingNote) {
      candidate.value = null
      versions.value = []
    }
    localStorage.setItem('research-desk.current-note', id)
    const cached = notes.value.find((item) => item.id === id) ?? archivedNotes.value.find((item) => item.id === id)
    const immediateDraft = readLocalDraft(id)
    const startingBaseRevision = immediateDraft?.baseRevision ?? cached?.revision ?? 0
    draftBaseRevision.set(id, startingBaseRevision)
    revision.value = cached?.revision ?? immediateDraft?.baseRevision ?? 0
    currentVersionId.value = cached?.current_version_id ?? null
    title.value = immediateDraft?.title ?? cached?.title ?? ''
    content.value = clone(immediateDraft?.content ?? cached?.content ?? blank)
    dirty.value = Boolean(immediateDraft)
    status.value = 'loading'
    error.value = ''
    const startingEdit = generation(id)
    const isCurrentSelection = () => request === selectionGeneration && currentId.value === id && !deletedIds.has(id)
    try {
      const [local, note] = await Promise.all([readDraft(id), getWorkspaceNote(id)])
      if (!isCurrentSelection()) return
      const latest = upsertNote(note)
      revision.value = Math.max(revision.value, latest.revision)
      currentVersionId.value = latest.current_version_id
      if (generation(id) !== startingEdit) {
        if (currentArchived.value) {
          dirty.value = true
          status.value = 'conflict'
          error.value = '这条笔记已归档；你刚才的编辑仍保存在本地草稿中。'
        } else if (!dirty.value) {
          status.value = 'saved'
          error.value = ''
        } else if ((draftBaseRevision.get(id) ?? startingBaseRevision) === revision.value) {
          status.value = 'idle'
          error.value = '载入期间的编辑已保留并将继续同步。'
          scheduleSave()
        } else {
          status.value = 'conflict'
          error.value = '笔记载入期间发生编辑；服务器版本不同，草稿已保留，请先处理冲突。'
        }
        return
      }
      if (latest.archived_at) {
        if (local) {
          draftBaseRevision.set(id, local.baseRevision)
          title.value = local.title; content.value = clone(local.content); dirty.value = true; status.value = 'conflict'
          error.value = '这条笔记已归档；本地草稿已保留，不能自动同步。'
        } else {
          title.value = latest.title; content.value = clone(latest.content); dirty.value = false; status.value = 'saved'; error.value = ''
        }
      } else if (local && local.baseRevision >= latest.revision) {
        draftBaseRevision.set(id, local.baseRevision)
        title.value = local.title; content.value = clone(local.content); dirty.value = true; status.value = 'offline'
        error.value = '已恢复尚未同步的本地草稿。'
        scheduleSave()
      } else {
        if (local && local.baseRevision < latest.revision) { status.value = 'conflict'; draftBaseRevision.set(id, local.baseRevision) }
        else draftBaseRevision.set(id, latest.revision)
        title.value = local && local.baseRevision < latest.revision ? local.title : latest.title
        content.value = clone(local && local.baseRevision < latest.revision ? local.content : latest.content)
        dirty.value = Boolean(local && local.baseRevision < latest.revision)
        status.value = dirty.value ? 'conflict' : 'saved'
        error.value = local && local.baseRevision < latest.revision ? '服务器已有较新版本；本地草稿已保留，请先处理冲突。' : ''
      }
    } catch (cause) {
      if (!isCurrentSelection()) return
      const local = await readDraft(id)
      if (!isCurrentSelection()) return
      if (generation(id) === startingEdit && local) {
        title.value = local.title; content.value = clone(local.content); revision.value = local.baseRevision; draftBaseRevision.set(id, local.baseRevision); dirty.value = true
      }
      setFailure(cause, '无法连接笔记服务。')
    }
  }

  async function create() {
    await flush()
    const note = await createWorkspaceNote('未命名笔记', clone(blank), key())
    notes.value = [note, ...notes.value]
    currentId.value = note.id
    candidate.value = null; versions.value = []
    localStorage.setItem('research-desk.current-note', note.id)
    editGeneration.set(note.id, 0)
    draftBaseRevision.set(note.id, note.revision)
    applyNote(note)
    await persistLocal()
    return note
  }

  function edit(nextTitleOrContent: string | NoteContent, maybeContent?: NoteContent) {
    if (currentArchived.value) return
    if (currentId.value && deletedIds.has(currentId.value)) return
    if (currentId.value) advanceGeneration(currentId.value)
    const nextContent = (typeof nextTitleOrContent === 'string' ? maybeContent : nextTitleOrContent) ?? content.value
    content.value = clone(nextContent)
    title.value = deriveTitle(content.value)
    dirty.value = true
    status.value = 'idle'
    error.value = ''
    void persistLocal().catch((cause) => { if (currentId.value) { status.value = 'error'; error.value = cause instanceof Error ? cause.message : '无法保存本地草稿。' } })
    scheduleSave()
  }

  function scheduleSave() {
    window.clearTimeout(saveTimer)
    saveTimer = window.setTimeout(() => { void save() }, 900)
  }

  async function save(): Promise<void> {
    const noteId = currentId.value
    if (!noteId || currentArchived.value || !dirty.value || status.value === 'conflict') return
    const running = saveFlights.get(noteId)
    if (running) { await running; return }
    const snapshot = { id: noteId, title: title.value, content: clone(content.value), revision: draftBaseRevision.get(noteId) ?? revision.value, generation: generation(noteId) }
    status.value = 'idle'
    let flight!: Promise<void>
    flight = (async () => {
      try {
        await persistChain.catch(() => undefined)
        const saved = await updateWorkspaceNote(noteId, { expected_revision: snapshot.revision, content: snapshot.content })
        if (deletedIds.has(noteId)) return
        notes.value = notes.value.some((item) => item.id === noteId)
          ? notes.value.map((item) => item.id === noteId ? saved : item)
          : [saved, ...notes.value]
        if (currentId.value === noteId) {
          revision.value = saved.revision
          draftBaseRevision.set(noteId, saved.revision)
          currentVersionId.value = saved.current_version_id
          if (generation(noteId) === snapshot.generation) {
            title.value = saved.title
            content.value = clone(saved.content)
            dirty.value = false
            status.value = 'saved'
            error.value = ''
            await deleteDraft(noteId).catch(() => undefined)
          } else {
            dirty.value = true
            status.value = 'idle'
            await persistLocal()
            scheduleSave()
          }
        } else if (generation(noteId) === snapshot.generation) {
          draftBaseRevision.set(noteId, saved.revision)
          await deleteDraft(noteId).catch(() => undefined)
        } else {
          draftBaseRevision.set(noteId, saved.revision)
          if (currentId.value === noteId) await persistLocal()
        }
      } catch (cause) {
        if (deletedIds.has(noteId)) return
        if (currentId.value === noteId) {
          const statusCode = cause && typeof cause === 'object' && 'status' in cause ? Number(cause.status) : 0
          if (statusCode === 409 && cause && typeof cause === 'object' && 'detail' in cause) {
            const detail = cause.detail
            if (detail && typeof detail === 'object' && 'current_revision' in detail && typeof detail.current_revision === 'number') revision.value = detail.current_revision
          }
          setFailure(cause, '笔记暂未同步，草稿仍保存在本机。')
        }
      } finally {
        if (saveFlights.get(noteId) === flight) saveFlights.delete(noteId)
      }
    })()
    saveFlights.set(noteId, flight)
    await flight
  }

  async function flush() {
    window.clearTimeout(saveTimer)
    await persistChain.catch(() => undefined)
    const noteId = currentId.value
    if (!noteId) return
    while (currentId.value === noteId) {
      const running = saveFlights.get(noteId)
      if (running) await running
      if (!dirty.value || ['conflict', 'offline', 'error'].includes(status.value)) return
      await save()
      if (!dirty.value || ['conflict', 'offline', 'error'].includes(status.value)) return
    }
  }

  async function reloadServerVersion() {
    const noteId = currentId.value
    if (!noteId || deletedIds.has(noteId)) return
    await saveFlights.get(noteId)
    const note = await getWorkspaceNote(noteId)
    if (deletedIds.has(noteId) || currentId.value !== noteId) return
    applyNote(note)
    await deleteDraft(note.id)
    error.value = ''
  }
  async function saveLocalOnLatestBase() {
    if (!currentId.value || currentArchived.value || deletedIds.has(currentId.value)) return
    status.value = 'idle'
    error.value = ''
    draftBaseRevision.set(currentId.value, revision.value)
    dirty.value = true
    await save()
  }

  async function remove(id: string) {
    if (!id) return
    if (deletedIds.has(id)) return
    deletedIds.add(id)
    if (currentId.value === id) {
      window.clearTimeout(saveTimer)
      advanceGeneration(id)
    }
    try {
      await persistChain.catch(() => undefined)
      await deleteWorkspaceNote(id)
      notes.value = notes.value.filter((item) => item.id !== id)
      archivedNotes.value = archivedNotes.value.filter((item) => item.id !== id)
      await deleteDraft(id)
      clearCurrent(id)
    } catch (cause) {
      deletedIds.delete(id)
      setFailure(cause, '笔记删除失败，请重试。')
      throw cause
    }
  }

  async function loadHistory() {
    const noteId = currentId.value
    if (!noteId || deletedIds.has(noteId)) return
    const history = await listWorkspaceNoteVersions(noteId)
    if (currentId.value === noteId && !deletedIds.has(noteId)) versions.value = history
  }
  async function propose(contentValue: NoteContent, sourceRunId?: string) {
    const noteId = currentId.value
    if (!noteId || deletedIds.has(noteId)) return
    const proposed = await createWorkspaceNoteCandidate(noteId, { expected_revision: revision.value, content: contentValue, source_run_id: sourceRunId, idempotency_key: key() })
    if (currentId.value === noteId && !deletedIds.has(noteId)) candidate.value = proposed
    return proposed
  }
  async function receiveCandidate(noteId: string, version: NoteVersion, baseRevision?: number) {
    if (deletedIds.has(noteId)) return
    if (currentId.value !== noteId) await select(noteId)
    if (currentId.value !== noteId || deletedIds.has(noteId)) return
    candidate.value = { note_id: noteId, base_revision: baseRevision ?? revision.value, candidate: version }
  }
  async function receiveCandidateById(noteId: string, candidateId: string, baseRevision?: number) {
    if (deletedIds.has(noteId)) throw new Error('这条笔记已删除。')
    if (currentId.value !== noteId) await select(noteId)
    if (currentId.value !== noteId || deletedIds.has(noteId)) throw new Error('已切换到其他笔记，请重新打开这项建议。')
    const history = await listWorkspaceNoteVersions(noteId)
    if (currentId.value !== noteId) throw new Error('已切换到其他笔记，请重新打开这项建议。')
    const version = history.find((item) => item.id === candidateId)
    if (!version) throw new Error('这项笔记建议已不可用，请刷新对话后重试。')
    if (version.status !== 'candidate') { candidate.value = null; versions.value = history; return }
    candidate.value = { note_id: noteId, base_revision: version.base_revision ?? baseRevision ?? revision.value, candidate: version }
  }
  async function adoptCandidate() {
    const noteId = currentId.value
    const selectedCandidate = candidate.value
    if (!noteId || !selectedCandidate) return
    if (dirty.value && status.value !== 'conflict') await flush()
    if (dirty.value || ['offline', 'conflict', 'error'].includes(status.value)) {
      status.value = 'conflict'
      error.value = '请先同步或处理本地草稿，再检查此候选。'
      return
    }
    try {
      const adopted = await adoptWorkspaceNoteVersion(noteId, { expected_revision: revision.value, version_id: selectedCandidate.candidate.id })
      if (deletedIds.has(noteId)) return
      applyNote(adopted)
      if (currentId.value === noteId && candidate.value?.candidate.id === selectedCandidate.candidate.id) candidate.value = null
      await deleteDraft(adopted.id)
    } catch (cause) {
      if (deletedIds.has(noteId)) return
      status.value = 'conflict'
      error.value = cause instanceof Error ? cause.message : '笔记已改变，请刷新后重新检查候选。'
    }
  }

  return { notes, archivedNotes, currentArchived, currentId, title, content, revision, currentVersionId, dirty, saving, status, error, candidate, versions, versionDiff, load, loadArchived, select, create, edit, save, flush, reloadServerVersion, saveLocalOnLatestBase, remove, loadHistory, propose, receiveCandidate, receiveCandidateById, adoptCandidate }
})

