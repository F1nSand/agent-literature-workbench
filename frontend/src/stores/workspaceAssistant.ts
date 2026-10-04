import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  acknowledgeWorkspaceAction, archiveWorkspaceConversation, createWorkspaceConversation, createWorkspaceRun,
  getWorkspaceConversationMessages, getWorkspaceRunDetail, listWorkspaceConversations, listWorkspaceRuns, streamWorkspaceRunEvents,
  stopWorkspaceRun, updateWorkspaceConversation,
} from '../api/workspaceAssistant'
import { useWorkspaceContextStore } from './workspaceContext'
import { useWorkspaceNotesStore } from './workspaceNotes'
import type { PageContext, WorkspaceConversation, WorkspaceRun, WorkspaceUiAction } from '../types/workspace'

type Message = { id: string; sequence_number: number; role: 'user' | 'assistant'; content: string; metadata: Record<string, unknown>; created_at: string }
type ActionEntry = { runId: string; action: WorkspaceUiAction; historical?: boolean; acknowledged?: boolean }
const localKey = 'research-desk.workspace-assistant'
const idempotencyKey = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`

export const useWorkspaceAssistantStore = defineStore('workspace-assistant', () => {
  const state = (() => { try { return JSON.parse(localStorage.getItem(localKey) ?? '{}') as { conversationId?: string; draft?: string; drafts?: Record<string, string>; pendingSend?: { question: string; key: string; conversationId: string } } } catch { return {} } })()
  const draftsByConversation = ref<Record<string, string>>(state.drafts ?? {})
  const legacyDraftKey = state.conversationId || '__new__'
  if (state.draft !== undefined && draftsByConversation.value[legacyDraftKey] === undefined) draftsByConversation.value[legacyDraftKey] = state.draft
  const conversations = ref<WorkspaceConversation[]>([])
  const archivedConversations = ref<WorkspaceConversation[]>([])
  const activeConversationId = ref(state.conversationId ?? '')
  const messages = ref<Message[]>([])
  const runs = ref<WorkspaceRun[]>([])
  const selectedDocumentIds = ref<string[]>([])
  const scopeMode = ref<'library' | 'selected'>('library')
  const draft = ref(draftsByConversation.value[legacyDraftKey] ?? '')
  const pendingSend = ref(state.pendingSend)
  const noteSyncNotice = ref('')
  const loading = ref(false)
  const sending = ref(false)
  const stoppingRunId = ref('')
  const streamState = ref<'idle' | 'connecting' | 'connected' | 'reconnecting'>('idle')
  const error = ref('')
  const pendingActions = ref<ActionEntry[]>([])
  const claimedActions = ref<string[]>([])
  const actionAckPayloads = ref<Record<string, { status: 'succeeded' | 'failed'; error?: string; selected_document_id?: string; selected_version_id?: string }>>({})
  const activeConversation = computed(() => conversations.value.find((item) => item.id === activeConversationId.value))
  const activeRun = computed(() => runs.value.find((run) => ['queued', 'running'].includes(run.status)))
  // The API orders runs chronologically; message sequence numbers restart in each run.
  const orderedMessages = computed(() => messages.value)
  let activeStreamRun = ''
  let streamGeneration = 0
  let streamAbort: AbortController | undefined
  let lastSequence = 0
  let conversationLoadGeneration = 0
  let conversationListGeneration = 0

  function draftKey(id = activeConversationId.value) { return id || '__new__' }
  function persist() {
    draftsByConversation.value[draftKey()] = draft.value
    localStorage.setItem(localKey, JSON.stringify({ conversationId: activeConversationId.value, draft: draft.value, drafts: draftsByConversation.value, pendingSend: pendingSend.value }))
  }
  function stopStream() { streamGeneration++; streamAbort?.abort(); streamAbort = undefined; activeStreamRun = ''; lastSequence = 0; streamState.value = 'idle' }

  async function loadConversation(id: string) {
    persist()
    const generation = ++conversationLoadGeneration
    activeConversationId.value = id
    draft.value = draftsByConversation.value[id] ?? ''
    persist()
    stopStream()
    messages.value = []
    runs.value = []
    pendingActions.value = []
    claimedActions.value = []
    actionAckPayloads.value = {}
    stoppingRunId.value = ''
    const isCurrent = () => generation === conversationLoadGeneration && activeConversationId.value === id
    try {
      const [messageList, runList] = await Promise.all([getWorkspaceConversationMessages(id), listWorkspaceRuns(id)])
      if (!isCurrent()) return
      messages.value = messageList
      runs.value = runList
      const historyDetails = await Promise.all(runList.slice(0, 30).map((run) => getWorkspaceRunDetail(run.id).catch(() => undefined)))
      if (!isCurrent()) return
      for (const detail of historyDetails) {
        if (!detail) continue
        for (const savedAction of detail.ui_actions ?? []) {
          const action = { ...savedAction.payload, action_id: savedAction.action_id, type: savedAction.action_type } as unknown as WorkspaceUiAction
          addAction(detail.id, action, true, savedAction.status !== 'pending')
        }
      }
      const running = runList.find((run) => ['queued', 'running'].includes(run.status))
      if (running) startStream(running.id, true, id)
      error.value = ''
    } catch (cause) { if (isCurrent()) error.value = cause instanceof Error ? cause.message : '暂时无法载入对话。' }
  }

  async function load() {
    const generation = ++conversationListGeneration
    loading.value = true
    try {
      const [active, archived] = await Promise.all([listWorkspaceConversations(), listWorkspaceConversations(true)])
      if (generation !== conversationListGeneration) return
      conversations.value = active.filter((item) => !item.archived_at)
      archivedConversations.value = archived.filter((item) => Boolean(item.archived_at))
      if (activeConversationId.value && conversations.value.some((item) => item.id === activeConversationId.value)) await loadConversation(activeConversationId.value)
      else if (conversations.value.length) await loadConversation(conversations.value[0].id)
      else {
        ++conversationLoadGeneration; stopStream(); activeConversationId.value = ''; messages.value = []; runs.value = []; pendingActions.value = []; stoppingRunId.value = ''; draft.value = draftsByConversation.value.__new__ ?? ''; persist()
      }
    } catch (cause) { if (generation === conversationListGeneration) error.value = cause instanceof Error ? cause.message : '对话服务暂不可用。' }
    finally { if (generation === conversationListGeneration) loading.value = false }
  }

  async function newConversation() {
    const conversation = await createWorkspaceConversation()
    conversations.value = [conversation, ...conversations.value]
    await loadConversation(conversation.id)
    return conversation
  }

  async function rename(id: string, title: string) {
    const updated = await updateWorkspaceConversation(id, title.trim() || '未命名对话')
    conversations.value = conversations.value.map((item) => item.id === id ? updated : item)
  }

  async function archive(id: string) {
    const archived = await archiveWorkspaceConversation(id)
    archivedConversations.value = [archived, ...archivedConversations.value.filter((item) => item.id !== id)]
    conversations.value = conversations.value.filter((item) => item.id !== id)
    if (activeConversationId.value === id) {
      ++conversationLoadGeneration; stopStream(); activeConversationId.value = ''; messages.value = []; runs.value = []; pendingActions.value = []; stoppingRunId.value = ''; draft.value = draftsByConversation.value.__new__ ?? ''
      if (conversations.value.length) await loadConversation(conversations.value[0].id)
      else persist()
    }
  }

  function setDraft(value: string) { draft.value = value; persist() }

  function addAction(runId: string, action: WorkspaceUiAction, historical = false, acknowledged = false) {
    const existing = pendingActions.value.find((item) => item.runId === runId && item.action.action_id === action.action_id)
    if (existing) {
      existing.historical = existing.historical || historical
      existing.acknowledged = existing.acknowledged || acknowledged
      existing.action = action
    } else pendingActions.value.push({ runId, action, historical, acknowledged })
  }

  async function send() {
    const question = draft.value.trim()
    if (!question || sending.value || activeRun.value) return
    const visibleContext = useWorkspaceContextStore().pageContext
    if (visibleContext.page_type === 'reader' && !visibleContext.version_id) {
      error.value = '文献还在载入，请稍候再发送。'
      return
    }
    sending.value = true; error.value = ''
    try {
      if (!activeConversationId.value) { await newConversation(); setDraft(question) }
      const targetConversationId = activeConversationId.value
      const noteStore = useWorkspaceNotesStore()
      noteSyncNotice.value = ''
      if (noteStore.dirty) {
        await noteStore.flush()
        if (noteStore.dirty || ['offline', 'conflict', 'error'].includes(noteStore.status)) {
          noteSyncNotice.value = `当前笔记尚未同步，本轮将使用服务器上的版本 ${noteStore.revision}。`
        }
      }
      const contextStore = useWorkspaceContextStore()
      const pageContext: PageContext = JSON.parse(JSON.stringify(contextStore.pageContext)) as PageContext
      if (noteStore.currentId) {
        pageContext.note_id = noteStore.currentId
        pageContext.note_revision = noteStore.revision
      }
      const requestKey = pendingSend.value?.question === question && pendingSend.value.conversationId === targetConversationId
        ? pendingSend.value.key
        : idempotencyKey()
      pendingSend.value = { question, key: requestKey, conversationId: targetConversationId }
      persist()
      const result = await createWorkspaceRun(targetConversationId, {
        question,
        idempotency_key: requestKey,
        scope: scopeMode.value === 'selected' ? { mode: 'selected', document_ids: [...selectedDocumentIds.value] } : { mode: 'library' },
        page_context: pageContext,
      })
      if (activeConversationId.value === targetConversationId) {
        setDraft('')
        pendingSend.value = undefined
        persist()
        runs.value = [result, ...runs.value.filter((item) => item.id !== result.id)]
        await refreshMessages(targetConversationId)
        startStream(result.id, false, targetConversationId)
      }
    } catch (cause) { error.value = cause instanceof Error ? cause.message : '发送失败，输入内容已保留。' }
    finally { sending.value = false }
  }

  async function refreshMessages(conversationId = activeConversationId.value) {
    if (!conversationId) return
    const [messageList, runList] = await Promise.all([getWorkspaceConversationMessages(conversationId), listWorkspaceRuns(conversationId)])
    if (activeConversationId.value !== conversationId) return
    messages.value = messageList; runs.value = runList
    if (runList.some((run) => run.id === stoppingRunId.value && !['queued', 'running'].includes(run.status))) stoppingRunId.value = ''
  }

  async function startStream(runId: string, resumeHistory = false, conversationId = activeConversationId.value) {
    if (activeStreamRun === runId) return
    stopStream(); activeStreamRun = runId
    const generation = streamGeneration
    const isCurrent = () => generation === streamGeneration && activeStreamRun === runId && activeConversationId.value === conversationId
    if (resumeHistory) {
      try {
        const detail = await getWorkspaceRunDetail(runId)
        if (!isCurrent()) return
        lastSequence = Math.max(0, ...(detail.events ?? []).map((event) => event.sequence_number))
        for (const savedAction of detail.ui_actions ?? []) {
          const action = { ...savedAction.payload, action_id: savedAction.action_id, type: savedAction.action_type } as unknown as WorkspaceUiAction
          addAction(runId, action, true, savedAction.status !== 'pending')
        }
      }
      catch (cause) {
        if (!isCurrent()) return
        activeStreamRun = ''
        streamState.value = 'reconnecting'
        error.value = cause instanceof Error ? cause.message : '无法恢复对话状态。'
        return
      }
    }
    let delay = 500
    while (isCurrent()) {
      const controller = new AbortController(); streamAbort = controller
      streamState.value = delay > 500 ? 'reconnecting' : 'connecting'
      try {
        await streamWorkspaceRunEvents(runId, lastSequence, controller.signal, async (eventName, event) => {
          if (!isCurrent()) return
          if (!('event_type' in event)) {
            lastSequence = Math.max(lastSequence, event.sequence_number)
            await refreshMessages(conversationId).catch(() => undefined)
            return
          }
          if (!Number.isSafeInteger(event.sequence_number) || event.sequence_number <= lastSequence) return
          lastSequence = event.sequence_number
          streamState.value = 'connected'
          if (eventName === 'ui_action' || event.event_type === 'ui_action') {
            const action = event.payload as unknown as WorkspaceUiAction
            if (action.action_id) addAction(runId, action)
          }
          await refreshMessages(conversationId).catch(() => undefined)
        })
        if (!isCurrent()) return
        await refreshMessages(conversationId).catch(() => undefined)
        const run = runs.value.find((item) => item.id === runId)
        if (run && !['queued', 'running'].includes(run.status)) { stoppingRunId.value = ''; stopStream(); return }
      } catch (cause) {
        if (controller.signal.aborted || !isCurrent()) return
        streamState.value = 'reconnecting'
        error.value = cause instanceof Error ? cause.message : '连接中断，正在恢复。'
      }
      await new Promise((resolve) => window.setTimeout(resolve, delay))
      delay = Math.min(delay * 2, 8000)
    }
  }

  async function stopActiveRun() {
    const run = activeRun.value
    if (!run || stoppingRunId.value === run.id) return
    stoppingRunId.value = run.id
    error.value = ''
    try {
      const stopped = await stopWorkspaceRun(run.id)
      if (activeConversationId.value !== run.conversation_id) return
      runs.value = [stopped, ...runs.value.filter((item) => item.id !== stopped.id)]
      await refreshMessages(run.conversation_id)
      if (!['queued', 'running'].includes(stopped.status)) stoppingRunId.value = ''
    } catch (cause) {
      if (activeConversationId.value === run.conversation_id) error.value = cause instanceof Error ? cause.message : '无法停止本轮。'
      if (stoppingRunId.value === run.id) stoppingRunId.value = ''
    }
  }

  function claimAction(actionId: string) {
    if (claimedActions.value.includes(actionId)) return false
    claimedActions.value = [...claimedActions.value, actionId]
    return true
  }

  async function acknowledgeAction(actionId: string, result: { status: 'succeeded' | 'failed'; error?: string; selected_document_id?: string; selected_version_id?: string }) {
    const index = pendingActions.value.findIndex((item) => item.action.action_id === actionId && !item.acknowledged)
    if (index < 0) return
    const item = pendingActions.value[index]
    actionAckPayloads.value = { ...actionAckPayloads.value, [actionId]: result }
    await acknowledgeWorkspaceAction(item.runId, actionId, result)
    if (item.historical) item.acknowledged = true
    else pendingActions.value.splice(index, 1)
    claimedActions.value = claimedActions.value.filter((value) => value !== actionId)
    const { [actionId]: _completed, ...remaining } = actionAckPayloads.value
    actionAckPayloads.value = remaining
  }

  async function retryActionAck(actionId: string) {
    const payload = actionAckPayloads.value[actionId]
    if (payload) await acknowledgeAction(actionId, payload)
  }

  function dismissAction(actionId: string) { pendingActions.value = pendingActions.value.filter((item) => item.action.action_id !== actionId) }
  function reconnect() { if (activeRun.value) void startStream(activeRun.value.id, true) }

  return { conversations, archivedConversations, activeConversationId, activeConversation, messages, orderedMessages, runs, activeRun, selectedDocumentIds, scopeMode, draft, noteSyncNotice, loading, sending, stoppingRunId, streamState, error, pendingActions, claimedActions, load, loadConversation, newConversation, rename, archive, setDraft, send, stopActiveRun, reconnect, refreshMessages, claimAction, acknowledgeAction, retryActionAck, dismissAction }
})
