import type { NoteCandidate, NoteContent, NoteVersion, WorkspaceNote } from '../types/workspace'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    let detail: unknown
    try { detail = (await response.json() as { detail?: unknown }).detail } catch { /* non-JSON response */ }
    const message = typeof detail === 'string'
      ? detail
      : detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string'
        ? detail.message
        : `请求失败（${response.status}）`
    const error = new Error(message) as Error & { status?: number; detail?: unknown }
    error.status = response.status
    error.detail = detail
    throw error
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
})

export async function listWorkspaceNotes(includeArchived = false): Promise<WorkspaceNote[]> {
  const items: WorkspaceNote[] = []
  let cursor: string | null = null
  do {
    const query = new URLSearchParams({ limit: '100', include_archived: String(includeArchived) })
    if (cursor) query.set('cursor', cursor)
    const result = await request<{ items: WorkspaceNote[]; next_cursor: string | null }>(`/api/v1/notes?${query}`)
    items.push(...result.items); cursor = result.next_cursor
  } while (cursor)
  return items
}

export function getWorkspaceNote(id: string): Promise<WorkspaceNote> {
  return request(`/api/v1/notes/${encodeURIComponent(id)}`)
}

export function createWorkspaceNote(title: string, content: NoteContent, key: string): Promise<WorkspaceNote> {
  return request('/api/v1/notes', json('POST', { title, content, idempotency_key: key }))
}

export function updateWorkspaceNote(id: string, payload: { expected_revision: number; title?: string; content?: NoteContent }): Promise<WorkspaceNote> {
  return request(`/api/v1/notes/${encodeURIComponent(id)}`, json('PATCH', payload))
}

export function deleteWorkspaceNote(id: string): Promise<void> {
  return request(`/api/v1/notes/${encodeURIComponent(id)}`, json('DELETE'))
}

export async function listWorkspaceNoteVersions(id: string): Promise<NoteVersion[]> {
  const result = await request<{ items: NoteVersion[] }>(`/api/v1/notes/${encodeURIComponent(id)}/versions`)
  return result.items
}

export function createWorkspaceNoteCandidate(id: string, payload: { expected_revision: number; content: NoteContent; source_run_id?: string; idempotency_key?: string }): Promise<NoteCandidate> {
  return request(`/api/v1/notes/${encodeURIComponent(id)}/candidates`, json('POST', payload))
}

export function adoptWorkspaceNoteVersion(id: string, payload: { expected_revision: number; version_id: string }): Promise<WorkspaceNote> {
  return request(`/api/v1/notes/${encodeURIComponent(id)}/adopt`, json('POST', payload))
}
