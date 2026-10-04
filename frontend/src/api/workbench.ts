export interface DocumentSummary {
  id: string
  title: string
  filename: string
  status: string
  page_count: number | null
  created_at: string
  error_message?: string | null
  active_version_id?: string | null
  file_format?: string
  authors?: string[]
  year?: number | null
  doi?: string | null
  tags?: string[]
  favorite?: boolean
  reading_status?: 'unread' | 'reading' | 'read'
}

export interface DocumentUploadOutcome {
  filename: string
  status: string
  document?: DocumentSummary | null
  error_message?: string | null
}

export interface DocumentVersionSummary {
  id: string
  version_number: number
  filename: string
  status: string
  page_count: number
  created_at: string
  is_active: boolean
}

export interface DocumentPage {
  document_id: string
  version_id: string
  page_number: number
  text: string
  extraction_method: string
  is_derived: boolean
  content_units: Array<{
    id: string
    text: string
    content_type: 'text' | 'chart' | 'table' | 'formula'
    source: string
    is_derived: boolean
    quote_eligible: boolean
    bbox: number[] | null
  }>
}

export interface Citation {
  document_id: string
  page_number: number
  quote: string
  reason: string
  is_derived?: boolean
}

export interface TaskResult {
  answer: string
  citations: Citation[]
  usage?: Record<string, unknown>
}

export interface WorkbenchTask {
  id: string
  status: string
  result?: TaskResult
  last_error_code?: string | null
}

export interface TopicSummary {
  id: string
  title: string
  goal: string
  archived_at: string | null
  created_at: string
  updated_at: string
  document_count: number
  conversation_count: number
}

export interface ResearchMemory {
  id: string
  topic_id: string
  memory_type: 'preference' | 'research_conclusion' | 'process_summary'
  status: 'active' | 'pending' | 'confirmed' | 'rejected' | 'invalidated' | 'archived'
  content: string
  revision: number
  source_run_id: string | null
  evidence_snapshot: Array<Record<string, unknown>>
  confirmed_at: string | null
  invalidated_at: string | null
  created_at: string
  updated_at: string
}

export interface StudyArtifactVersion {
  id: string
  artifact_id: string
  version_number: number
  parent_version_id: string | null
  origin: 'user' | 'generated'
  status: 'current' | 'candidate' | 'superseded'
  body_text: string
  structured_content: Record<string, unknown> | unknown[] | null
  content_sha256: string
  source_run_id: string | null
  evidence_snapshot: StudyArtifactEvidence[]
  adopted_at: string | null
  created_at: string
}

export interface StudyArtifactEvidence {
  evidence_id: string
  document_id: string
  version_id: string
  page_number: number
  content_unit_id: string | null
  content_sha256: string
}

export interface StudyArtifact {
  id: string
  topic_id: string
  title: string
  artifact_type: 'note' | 'comparison_table' | 'draft'
  current_version_id: string | null
  revision: number
  created_at: string
  updated_at: string
  current_version: StudyArtifactVersion | null
}

export interface TopicDocumentItem {
  linked_at: string
  document: DocumentSummary
}

export interface ConversationSummary {
  id: string
  topic_id: string
  title: string
  created_at: string
  updated_at: string
}

export interface ConversationRunSummary {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'awaiting_user'
  scope_mode: 'topic' | 'selected' | 'library'
  created_at: string
  question: string
  answer: string | null
}

export interface ResearchRunDetail {
  id: string
  conversation_id: string
  status: ConversationRunSummary['status']
  scope_mode: ConversationRunSummary['scope_mode']
  scope_snapshot: Record<string, unknown>
  scope_hash: string
  budget_snapshot: Record<string, unknown>
  attempt_count: number
  tool_calls_used: number
  model_steps_used: number
  input_tokens: number | null
  output_tokens: number | null
  error_code: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  messages: Array<{
    id: string
    sequence_number: number
    role: 'user' | 'assistant'
    content: string
    metadata: Record<string, unknown>
    created_at: string
  }>
  evidence: Array<{
    id: string
    document_id: string
    version_id: string
    page_number: number
    content_unit_id: string | null
    content_type: string
    source: string
    is_derived: boolean
    quote_eligible: boolean
    content_sha256: string
    excerpt: string | null
    is_validated: boolean
    is_invalidated: boolean
  }>
  events: Array<{ sequence_number: number; event_type: string; payload: Record<string, unknown>; created_at: string }>
}

export type ResearchRunReceipt = Omit<ResearchRunDetail, 'messages' | 'evidence' | 'events'>

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    let detail = ''
    try {
      const payload = await response.json() as { detail?: unknown; message?: unknown }
      const nested = payload.detail && typeof payload.detail === 'object' ? payload.detail as { message?: unknown } : null
      detail = typeof payload.detail === 'string'
        ? payload.detail
        : typeof nested?.message === 'string'
          ? nested.message
          : typeof payload.message === 'string'
            ? payload.message
            : ''
    } catch { /* Keep the user-facing error concise when the response is not JSON. */ }
    throw new Error(detail || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export async function listDocuments(): Promise<DocumentSummary[]> {
  const result = await request<{ items: DocumentSummary[] }>('/api/v1/documents')
  return result.items
}

export async function listTopics(includeArchived = false): Promise<TopicSummary[]> {
  const query = includeArchived ? '?include_archived=true' : ''
  const result = await request<{ items: TopicSummary[] }>(`/api/v1/topics${query}`)
  return result.items
}

export async function listTopicMemories(topicId: string): Promise<ResearchMemory[]> {
  const result = await request<{ items: ResearchMemory[] }>(`/api/v1/topics/${encodeURIComponent(topicId)}/memories`)
  return result.items
}

export function createTopicMemory(topicId: string, payload: {
  memory_type: 'preference' | 'research_conclusion'
  content: string
  source_run_id?: string
  evidence_ids?: string[]
}): Promise<ResearchMemory> {
  return request<ResearchMemory>(`/api/v1/topics/${encodeURIComponent(topicId)}/memories`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
}

export function updateTopicMemory(memoryId: string, payload: {
  expected_revision: number
  action: 'edit' | 'confirm' | 'reject' | 'archive'
  content?: string
}): Promise<ResearchMemory> {
  return request<ResearchMemory>(`/api/v1/memories/${encodeURIComponent(memoryId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
}

export async function listTopicArtifacts(topicId: string): Promise<StudyArtifact[]> {
  const result = await request<{ items: StudyArtifact[] }>(`/api/v1/topics/${encodeURIComponent(topicId)}/artifacts`)
  return result.items
}

export function saveRunAsArtifact(topicId: string, payload: {
  title: string
  artifact_type: StudyArtifact['artifact_type']
  run_id: string
}): Promise<StudyArtifact> {
  return request<StudyArtifact>(`/api/v1/topics/${encodeURIComponent(topicId)}/artifacts/from-run`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
}

export function createStudyArtifact(topicId: string, payload: {
  title: string
  artifact_type: StudyArtifact['artifact_type']
  body_text: string
}): Promise<StudyArtifact> {
  return request<StudyArtifact>(`/api/v1/topics/${encodeURIComponent(topicId)}/artifacts`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
}

export function updateStudyArtifact(artifactId: string, payload: {
  expected_revision: number
  title?: string
  body_text?: string
}): Promise<StudyArtifact> {
  return request<StudyArtifact>(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
}

export async function listStudyArtifactVersions(artifactId: string): Promise<StudyArtifactVersion[]> {
  const result = await request<{ items: StudyArtifactVersion[] }>(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/versions`)
  return result.items
}

export function createStudyArtifactCandidate(artifactId: string, runId: string): Promise<StudyArtifactVersion> {
  return request<StudyArtifactVersion>(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/candidates`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: runId }),
  })
}

export function compareStudyArtifactVersions(artifactId: string, baseVersionId: string, compareVersionId: string): Promise<{
  base: StudyArtifactVersion
  compare: StudyArtifactVersion
  unified_diff: string
}> {
  const params = new URLSearchParams({ base_version_id: baseVersionId, compare_version_id: compareVersionId })
  return request(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/compare?${params}`)
}

export function adoptStudyArtifactVersion(artifactId: string, payload: {
  expected_revision: number
  version_id: string
}): Promise<StudyArtifact> {
  return request<StudyArtifact>(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/adopt`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
}

export async function downloadStudyArtifact(
  artifactId: string,
  format: 'markdown' | 'csv',
): Promise<Blob> {
  const params = new URLSearchParams({ format })
  const response = await fetch(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/export?${params}`)
  if (!response.ok) {
    let message = ''
    try {
      const payload = await response.json() as { detail?: unknown }
      if (typeof payload.detail === 'string') message = payload.detail
    } catch { /* Use a concise fallback for non-JSON responses. */ }
    throw new Error(message || '暂时无法导出这份成果。')
  }
  return response.blob()
}

export function createTopic(payload: { title: string; goal: string }): Promise<TopicSummary> {
  return request<TopicSummary>('/api/v1/topics', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function updateTopic(topicId: string, payload: { title: string; goal: string }): Promise<TopicSummary> {
  return request<TopicSummary>(`/api/v1/topics/${encodeURIComponent(topicId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export async function archiveTopic(topicId: string): Promise<void> {
  const response = await fetch(`/api/v1/topics/${encodeURIComponent(topicId)}`, { method: 'DELETE' })
  if (!response.ok) throw new Error(`请求失败（${response.status}）`)
}

export function restoreTopic(topicId: string): Promise<TopicSummary> {
  return request<TopicSummary>(`/api/v1/topics/${encodeURIComponent(topicId)}/restore`, { method: 'POST' })
}

export async function listTopicDocuments(topicId: string): Promise<TopicDocumentItem[]> {
  const result = await request<{ items: TopicDocumentItem[] }>(`/api/v1/topics/${encodeURIComponent(topicId)}/documents`)
  return result.items
}

export function getTopic(topicId: string): Promise<TopicSummary> {
  return request<TopicSummary>(`/api/v1/topics/${encodeURIComponent(topicId)}`)
}

export async function listConversations(topicId: string): Promise<ConversationSummary[]> {
  const result = await request<{ items: ConversationSummary[] }>(`/api/v1/topics/${encodeURIComponent(topicId)}/conversations`)
  return result.items
}

export function createConversation(topicId: string, title = '新的研读'): Promise<ConversationSummary> {
  return request<ConversationSummary>(`/api/v1/topics/${encodeURIComponent(topicId)}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export async function listConversationRuns(conversationId: string): Promise<ConversationRunSummary[]> {
  const result = await request<{ items: ConversationRunSummary[] }>(`/api/v1/conversations/${encodeURIComponent(conversationId)}/runs`)
  return result.items
}

export function getResearchRun(runId: string): Promise<ResearchRunDetail> {
  return request<ResearchRunDetail>(`/api/v1/runs/${encodeURIComponent(runId)}`)
}

export type ResearchRunStreamEvent =
  | { sequence_number: number; event_type: string; payload: Record<string, unknown>; created_at: string }
  | { sequence_number: number; run: ResearchRunDetail }

export async function streamResearchRunEvents(
  runId: string,
  afterSequence: number,
  signal: AbortSignal,
  onEvent: (eventName: string, event: ResearchRunStreamEvent) => void | Promise<void>,
): Promise<void> {
  const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/events`, {
    headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(afterSequence) },
    signal,
  })
  if (!response.ok) throw new Error(`状态连接暂不可用（${response.status}）`)
  if (!response.body) throw new Error('状态连接暂不可用。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatch = async (frame: string) => {
    let eventName = 'message'
    const data: string[] = []
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith(':')) continue
      if (line.startsWith('event:')) eventName = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
    if (!data.length) return
    await onEvent(eventName, JSON.parse(data.join('\n')) as ResearchRunStreamEvent)
  }

  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() ?? ''
      for (const frame of frames) await dispatch(frame)
      if (done) {
        if (buffer.trim()) await dispatch(buffer)
        return
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export function createResearchRun(
  conversationId: string,
  payload: { question: string; scope_mode: 'topic' | 'selected' | 'library'; document_ids?: string[] },
  idempotencyKey: string,
): Promise<ResearchRunReceipt> {
  return request<ResearchRunReceipt>(`/api/v1/conversations/${encodeURIComponent(conversationId)}/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(payload),
  })
}

export function stopResearchRun(runId: string): Promise<ResearchRunDetail> {
  return request<ResearchRunDetail>(`/api/v1/runs/${encodeURIComponent(runId)}/stop`, { method: 'POST' })
}

export function continueResearchRun(runId: string, idempotencyKey: string): Promise<ResearchRunReceipt> {
  return request<ResearchRunReceipt>(`/api/v1/runs/${encodeURIComponent(runId)}/continue`, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export function linkTopicDocument(topicId: string, documentId: string): Promise<TopicDocumentItem> {
  return request<TopicDocumentItem>(`/api/v1/topics/${encodeURIComponent(topicId)}/documents/${encodeURIComponent(documentId)}`, { method: 'PUT' })
}

export async function unlinkTopicDocument(topicId: string, documentId: string): Promise<void> {
  const response = await fetch(`/api/v1/topics/${encodeURIComponent(topicId)}/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
  if (!response.ok) throw new Error(`请求失败（${response.status}）`)
}

export function uploadDocument(file: File): Promise<DocumentSummary> {
  const data = new FormData()
  data.append('file', file)
  return request<DocumentSummary>('/api/v1/documents', { method: 'POST', body: data })
}

export async function uploadDocuments(files: File[]): Promise<DocumentUploadOutcome[]> {
  const data = new FormData()
  files.forEach((file) => data.append('files', file))
  const result = await request<{ items: DocumentUploadOutcome[] }>('/api/v1/documents/batch', { method: 'POST', body: data })
  return result.items
}

export async function listDocumentVersions(documentId: string): Promise<DocumentVersionSummary[]> {
  const result = await request<{ items: DocumentVersionSummary[] }>(`/api/v1/documents/${encodeURIComponent(documentId)}/versions`)
  return result.items
}

export function replaceDocumentVersion(documentId: string, file: File): Promise<DocumentSummary> {
  const data = new FormData()
  data.append('file', file)
  return request<DocumentSummary>(`/api/v1/documents/${encodeURIComponent(documentId)}/versions`, { method: 'POST', body: data })
}

export function activateDocumentVersion(documentId: string, versionId: string): Promise<DocumentSummary> {
  return request<DocumentSummary>(`/api/v1/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}/activate`, { method: 'POST' })
}

export function retryDocumentVersion(documentId: string, versionId: string): Promise<DocumentVersionSummary> {
  return request<DocumentVersionSummary>(`/api/v1/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}/retry`, { method: 'POST' })
}

export async function deleteDocument(documentId: string): Promise<void> {
  const response = await fetch(`/api/v1/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
  if (!response.ok) {
    let detail = ''
    try {
      const payload = await response.json() as { detail?: unknown }
      detail = typeof payload.detail === 'string' ? payload.detail : ''
    } catch { /* Keep the user-facing error concise when the response is not JSON. */ }
    throw new Error(detail || `请求失败（${response.status}）`)
  }
}

export function getDocumentPage(documentId: string, pageNumber: number): Promise<DocumentPage> {
  return request<DocumentPage>(`/api/v1/documents/${encodeURIComponent(documentId)}/pages/${encodeURIComponent(String(pageNumber))}`)
}

export function documentFileUrl(documentId: string): string {
  return `/api/v1/documents/${encodeURIComponent(documentId)}/file`
}

export function documentPagePreviewUrl(documentId: string, pageNumber: number, versionId: string): string {
  return `/api/v1/documents/${encodeURIComponent(documentId)}/pages/${encodeURIComponent(String(pageNumber))}/preview?version_id=${encodeURIComponent(versionId)}`
}

export function createDocumentQuestion(documentId: string, question: string): Promise<{ task_id: string; status: string }> {
  return request(`/api/v1/documents/${encodeURIComponent(documentId)}/questions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
}

export function getTask(taskId: string): Promise<WorkbenchTask> {
  return request<WorkbenchTask>(`/api/v1/tasks/${encodeURIComponent(taskId)}`)
}

export function rebuildIndex(): Promise<WorkbenchTask> {
  return request<WorkbenchTask>('/api/v1/index/rebuild', { method: 'POST' })
}
