import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'

const values = new Map()
const indexedValues = new Map()
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: (key) => values.delete(key),
}
globalThis.window = {
  setTimeout: (callback, delay) => { const timer = setTimeout(callback, delay); timer.unref(); return timer },
  clearTimeout: (timer) => clearTimeout(timer),
}
const database = {
  createObjectStore() {},
  transaction() {
    const transaction = { error: null, oncomplete: null, onabort: null, onerror: null }
    const store = {
      get(id) { return request(() => indexedValues.get(id), transaction) },
      put(draft) { return request(() => { indexedValues.set(draft.id, draft); return draft.id }, transaction) },
      delete(id) { return request(() => { indexedValues.delete(id); return undefined }, transaction) },
    }
    transaction.objectStore = () => store
    return transaction
  },
  close() {},
}
function request(read, transaction) {
  const req = { result: undefined, error: null, onsuccess: null, onerror: null, transaction }
  queueMicrotask(() => {
    req.result = read()
    req.onsuccess?.()
    queueMicrotask(() => req.transaction?.oncomplete?.())
  })
  return req
}
globalThis.indexedDB = {
  open() {
    const opening = { result: database, onupgradeneeded: null, onsuccess: null, onerror: null }
    queueMicrotask(() => { opening.onupgradeneeded?.(); opening.onsuccess?.() })
    return opening
  },
}

let server
let useWorkspaceNotesStore
before(async () => {
  server = await createServer({ configFile: false, root: process.cwd(), cacheDir: join(tmpdir(), 'research-desk-workspace-notes-vite-test-cache'), optimizeDeps: { noDiscovery: true, include: [], entries: [] }, server: { middlewareMode: true, hmr: { port: 0 } }, appType: 'custom' })
  ;({ useWorkspaceNotesStore } = await server.ssrLoadModule('/src/stores/workspaceNotes.ts'))
})
after(async () => { await server?.close() })

test('a late version history belongs only to the note that requested it', async () => {
  const store = freshStore()
  const a = note('a', 'Note A'), b = note('b', 'Note B')
  store.notes = [a, b]
  const history = deferred()
  globalThis.fetch = async (url) => {
    if (String(url).endsWith('/notes/a/versions')) return history.promise
    if (String(url).endsWith('/notes/a')) return response(a)
    if (String(url).endsWith('/notes/b')) return response(b)
    throw new Error(`Unexpected request: ${url}`)
  }
  await store.select('a')
  const loadingHistory = store.loadHistory()
  await store.select('b')
  history.resolve(response({ items: [{ id: 'a-version', note_id: 'a' }] }))
  await loadingHistory
  assert.equal(store.currentId, 'b')
  assert.deepEqual(store.versions, [])
})

test('opening an AI candidate shows its preview without also opening version history', async () => {
  const store = freshStore()
  const a = note('a', 'Note A')
  const candidate = { id: 'candidate', note_id: 'a', status: 'candidate', base_revision: 3, plain_text: 'Suggested note', content: a.content }
  store.notes = [a]
  globalThis.fetch = async (url) => {
    if (String(url).endsWith('/notes/a/versions')) return response({ items: [candidate] })
    if (String(url).endsWith('/notes/a')) return response(a)
    throw new Error(`Unexpected request: ${url}`)
  }
  await store.select('a')
  await store.receiveCandidateById('a', 'candidate', 3)
  assert.equal(store.candidate.candidate.id, 'candidate')
  assert.deepEqual(store.versions, [])
})

test('refreshing the selected note from history preserves its candidate preview and open versions panel', async () => {
  const store = freshStore()
  const a = note('a', 'Note A')
  const candidate = { note_id: 'a', base_revision: 3, candidate: { id: 'candidate-a' } }
  const version = { id: 'version-a', note_id: 'a' }
  store.notes = [a]
  globalThis.fetch = async (url) => {
    if (String(url).endsWith('/notes/a')) return response(a)
    throw new Error(`Unexpected request: ${url}`)
  }

  await store.select('a')
  store.candidate = candidate
  store.versions = [version]
  await store.select('a')

  assert.equal(store.candidate.candidate.id, 'candidate-a')
  assert.deepEqual(store.versions, [version])
})

function note(id, title, revision = 3) {
  return {
    id, title, revision, current_version_id: `${id}-version`,
    content: { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: `${title} body` }] }] },
    plain_text: `${title} body`, archived_at: null, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
  }
}
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
const response = (value) => ({ ok: true, status: 200, json: async () => value })
function freshStore() {
  values.clear(); indexedValues.clear()
  setActivePinia(createPinia())
  return useWorkspaceNotesStore()
}

test('a slow earlier selection cannot replace the newer note, and a clean note ends in saved state', async () => {
  const store = freshStore()
  const a = note('a', 'Note A'), b = note('b', 'Note B')
  store.notes = [a, b]
  const startedA = deferred(), finishA = deferred()
  globalThis.fetch = async (url) => {
    if (String(url).endsWith('/notes/a')) { startedA.resolve(); return finishA.promise }
    if (String(url).endsWith('/notes/b')) return response(b)
    throw new Error(`Unexpected request: ${url}`)
  }

  const selectingA = store.select('a')
  await startedA.promise
  await store.select('b')
  assert.equal(store.currentId, 'b')
  assert.equal(store.title, 'Note B')
  assert.equal(store.status, 'saved')

  finishA.resolve(response(a))
  await selectingA
  assert.equal(store.currentId, 'b')
  assert.equal(store.title, 'Note B')
  assert.equal(store.content.content[0].content[0].text, 'Note B body')
})

test('the initial list load leaves a clean selected note in saved state', async () => {
  const store = freshStore()
  const onlyNote = note('only', 'A saved note')
  globalThis.fetch = async (url) => {
    if (String(url).includes('/api/v1/notes?')) return response({ items: [onlyNote], next_cursor: null })
    if (String(url).endsWith('/notes/only')) return response(onlyNote)
    throw new Error(`Unexpected request: ${url}`)
  }

  await store.load()
  assert.equal(store.currentId, 'only')
  assert.equal(store.title, 'A saved note')
  assert.equal(store.dirty, false)
  assert.equal(store.status, 'saved')
})

test('editing while the initial note request is pending preserves and persists the edit', async () => {
  const store = freshStore()
  const cached = note('a', 'Server title')
  store.notes = [cached]
  const started = deferred(), finish = deferred()
  globalThis.fetch = async (url) => {
    if (String(url).endsWith('/notes/a')) { started.resolve(); return finish.promise }
    throw new Error(`Unexpected request: ${url}`)
  }

  const selecting = store.select('a')
  await started.promise
  const edited = { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'typed during load' }] }] }
  store.edit('My edit', edited)
  finish.resolve(response(cached))
  await selecting

  assert.equal(store.title, 'typed during load')
  assert.equal(store.content.content[0].content[0].text, 'typed during load')
  assert.equal(store.dirty, true)
  assert.match(store.status, /^(idle|saved)$/)
  const persisted = JSON.parse(localStorage.getItem('research-desk.note-draft.a'))
  assert.equal(persisted.title, 'typed during load')
  assert.equal(persisted.content.content[0].content[0].text, 'typed during load')
})

test('a slow pre-save load cannot roll back an edit already saved during loading', async () => {
  const store = freshStore()
  const oldNote = note('a', 'Original', 3)
  const savedNote = note('a', 'Saved edit', 4)
  store.notes = [oldNote]
  const started = deferred(), finish = deferred()
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).endsWith('/notes/a') && !init.method) { started.resolve(); return finish.promise }
    if (String(url).endsWith('/notes/a') && init.method === 'PATCH') return response(savedNote)
    throw new Error(`Unexpected request: ${url}`)
  }

  const selecting = store.select('a')
  await started.promise
  const edited = { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'saved while loading' }] }] }
  store.edit('Saved edit', edited)
  await store.save()
  finish.resolve(response(oldNote))
  await selecting

  assert.equal(store.currentId, 'a')
  assert.equal(store.title, 'Saved edit')
  assert.equal(store.revision, 4)
  assert.equal(store.status, 'saved')
  assert.equal(store.dirty, false)
  assert.equal(store.content.content[0].content[0].text, 'Saved edit body')
})

test('a validation response is actionable error state, not offline', async () => {
  const store = freshStore()
  const savedNote = note('a', 'Original')
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).endsWith('/notes/a') && !init.method) return response(savedNote)
    if (String(url).endsWith('/notes/a') && init.method === 'PATCH') {
      return { ok: false, status: 422, json: async () => ({ detail: [{ msg: 'invalid text style' }] }) }
    }
    throw new Error(`Unexpected request: ${url}`)
  }

  await store.select('a')
  const edited = { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'bad style' }] }] }
  store.edit('Original', edited)
  await store.save()

  assert.equal(store.status, 'error')
  assert.match(store.error, /HTTP 422/)
  assert.equal(store.dirty, true)
})

test('deleting the current note clears drafts and a late save response cannot revive it', async () => {
  const store = freshStore()
  const original = note('delete-me', 'Delete me')
  store.notes = [original]
  let finishPatch
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).endsWith('/notes/delete-me') && !init.method) return response(original)
    if (String(url).endsWith('/notes/delete-me') && init.method === 'PATCH') {
      return new Promise((resolve) => { finishPatch = () => resolve(response(note('delete-me', 'Late save', 4))) })
    }
    if (String(url).endsWith('/notes/delete-me') && init.method === 'DELETE') return { ok: true, status: 204 }
    throw new Error(`Unexpected request: ${url}`)
  }

  await store.select('delete-me')
  store.edit({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'pending edit' }] }] })
  const saving = store.save()
  await new Promise((resolve) => setTimeout(resolve, 0))
  const removing = store.remove('delete-me')
  await removing
  finishPatch()
  await saving

  assert.equal(store.currentId, '')
  assert.equal(store.title, '')
  assert.equal(store.notes.some((item) => item.id === 'delete-me'), false)
  assert.equal(localStorage.getItem('research-desk.note-draft.delete-me'), null)
  assert.equal(indexedValues.has('delete-me'), false)
})
