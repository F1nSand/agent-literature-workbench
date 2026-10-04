export type NoteContent = {
  type: 'doc'
  content: Array<Record<string, unknown>>
}

export interface WorkspaceNote {
  id: string
  title: string
  revision: number
  current_version_id: string | null
  content: NoteContent
  plain_text: string
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface NoteVersion {
  id: string
  note_id: string
  version_number: number
  base_revision: number | null
  parent_version_id: string | null
  origin: 'user' | 'generated'
  status: string
  content: NoteContent
  plain_text: string
  source_run_id: string | null
  adopted_at: string | null
  created_at: string
}

export interface NoteCandidate {
  note_id: string
  base_revision: number
  candidate: NoteVersion
}

export interface Locator {
  kind?: 'page' | 'epub' | 'text'
  page?: number
  chapter?: string
  cfi?: string
  anchor?: string
  [key: string]: unknown
}

export interface PageContext {
  page_type: 'home' | 'library' | 'reader' | 'settings' | 'note' | 'assistant'
  document_id?: string
  version_id?: string
  locator?: Locator
  selection?: string
  note_id?: string
  note_revision?: number
}

export interface WorkspaceConversation {
  id: string
  title: string
  topic_id: string | null
  created_at: string
  updated_at: string
  archived_at?: string | null
}

export interface WorkspaceRun {
  id: string
  conversation_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'awaiting_user'
  scope_mode: 'selected' | 'library'
  created_at: string
  question: string
  answer: string | null
  events?: Array<{ sequence_number: number; event_type: string; payload: Record<string, unknown>; created_at: string }>
  messages?: Array<{ id: string; sequence_number: number; role: 'user' | 'assistant'; content: string; metadata: Record<string, unknown>; created_at: string }>
}

export interface WorkspaceUiAction {
  action_id: string
  type: 'open_document' | 'show_note_candidate' | 'focus_note' | 'show_document_choices'
  document_id?: string
  version_id?: string
  locator?: Locator
  candidate?: NoteVersion
  candidate_id?: string
  expected_revision?: number
  plain_text?: string
  candidate_title?: string
  note_id?: string
  query?: string
  candidates?: Array<{ document_id: string; version_id: string; title: string; locator?: Locator; location_label?: string }>
}
