import type { PageContext, WorkspaceConversation, WorkspaceRun, WorkspaceUiAction } from '../types/workspace'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    let detail = ''
    try { const value = (await response.json() as { detail?: unknown }).detail; detail = typeof value === 'string' ? value : '' } catch { /* non-JSON response */ }
    throw new Error(detail || `请求失败（${response.status}）`)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}
const json = (method: string, body?: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })

export async function listWorkspaceConversations(includeArchived = false): Promise<WorkspaceConversation[]> {
  const result = await request<{ items: WorkspaceConversation[] }>(`/api/v1/conversations?include_archived=${includeArchived}`)
  return result.items
}
export function createWorkspaceConversation(title = '新的对话'): Promise<WorkspaceConversation> {
  return request('/api/v1/conversations', json('POST', { title }))
}
export function updateWorkspaceConversation(id: string, title: string): Promise<WorkspaceConversation> {
  return request(`/api/v1/conversations/${encodeURIComponent(id)}`, json('PATCH', { title }))
}
export function archiveWorkspaceConversation(id: string): Promise<WorkspaceConversation> {
  return request(`/api/v1/conversations/${encodeURIComponent(id)}`, json('DELETE'))
}
export async function getWorkspaceConversationMessages(id: string): Promise<Array<{ id: string; sequence_number: number; role: 'user' | 'assistant'; content: string; metadata: Record<string, unknown>; created_at: string }>> {
  const result = await request<{ conversation_id: string; messages: Array<{ id: string; sequence_number: number; role: 'user' | 'assistant'; content: string; metadata: Record<string, unknown>; created_at: string }> }>(`/api/v1/conversations/${encodeURIComponent(id)}/messages`)
  return result.messages
}
export async function listWorkspaceRuns(conversationId: string): Promise<WorkspaceRun[]> {
  const result = await request<{ items: WorkspaceRun[] }>(`/api/v1/conversations/${encodeURIComponent(conversationId)}/runs`)
  return result.items
}
export interface WorkspaceRunDetail extends WorkspaceRun {
  events?: Array<{ sequence_number: number; event_type: string; payload: Record<string, unknown>; created_at: string }>
  ui_actions?: Array<{ action_id: string; action_type: WorkspaceUiAction['type']; payload: Record<string, unknown>; status: string; error?: string | null }>
}
export function getWorkspaceRunDetail(runId: string): Promise<WorkspaceRunDetail> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}`)
}
export async function getWorkspaceRunEvents(runId: string): Promise<Array<{ sequence_number: number; event_type: string; payload: Record<string, unknown>; created_at: string }>> {
  return (await getWorkspaceRunDetail(runId)).events ?? []
}
export function createWorkspaceRun(conversationId: string, payload: { question: string; idempotency_key: string; scope: { mode: 'library' | 'selected'; document_ids?: string[] }; page_context: PageContext }): Promise<WorkspaceRun> {
  return request(`/api/v1/conversations/${encodeURIComponent(conversationId)}/runs`, json('POST', payload))
}
export function stopWorkspaceRun(runId: string): Promise<WorkspaceRun> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/stop`, json('POST'))
}
export function acknowledgeWorkspaceAction(runId: string, actionId: string, payload: { status: 'succeeded' | 'failed'; error?: string; selected_document_id?: string; selected_version_id?: string }): Promise<unknown> {
  return request(`/api/v1/runs/${encodeURIComponent(runId)}/actions/${encodeURIComponent(actionId)}/ack`, json('POST', payload))
}

export async function streamWorkspaceRunEvents(runId: string, afterSequence: number, signal: AbortSignal, onEvent: (name: string, event: WorkspaceStreamEvent) => void | Promise<void>) {
  const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/events`, {
    headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(afterSequence) }, signal,
  })
  if (!response.ok || !response.body) throw new Error(`状态连接暂不可用（${response.status}）`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  async function dispatch(frame: string) {
    let name = 'message'; const data: string[] = []
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith('event:')) name = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
    if (data.length) await onEvent(name, JSON.parse(data.join('\n')) as WorkspaceStreamEvent)
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/); buffer = frames.pop() ?? ''
      for (const frame of frames) await dispatch(frame)
      if (done) { if (buffer.trim()) await dispatch(buffer); return }
    }
  } finally { reader.releaseLock() }
}

export type WorkspaceStreamEvent = { sequence_number: number; event_type: string; payload: Record<string, unknown>; created_at: string } | { sequence_number: number; run: WorkspaceRun }
