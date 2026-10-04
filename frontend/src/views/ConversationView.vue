<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  continueResearchRun,
  createConversation,
  createStudyArtifactCandidate,
  createResearchRun,
  createTopicMemory,
  getResearchRun,
  getTopic,
  listConversationRuns,
  listConversations,
  listDocuments,
  listTopicArtifacts,
  listTopicMemories,
  listTopicDocuments,
  stopResearchRun,
  streamResearchRunEvents,
  saveRunAsArtifact,
  updateTopicMemory,
  type ResearchMemory,
  type ConversationRunSummary,
  type ConversationSummary,
  type DocumentSummary,
  type ResearchRunDetail,
  type ResearchRunStreamEvent,
  type StudyArtifact,
  type TopicDocumentItem,
  type TopicSummary,
} from '../api/workbench'

const route = useRoute()
const topicId = computed(() => String(route.params.topicId ?? ''))
const topic = ref<TopicSummary>()
const conversations = ref<ConversationSummary[]>([])
const linkedDocuments = ref<TopicDocumentItem[]>([])
const libraryDocuments = ref<DocumentSummary[]>([])
const runs = ref<ConversationRunSummary[]>([])
const memories = ref<ResearchMemory[]>([])
const topicArtifacts = ref<StudyArtifact[]>([])
const memoryDrafts = ref<Record<string, string>>({})
const preferenceDraft = ref('')
const artifactTargetId = ref('')
const selectedConversationId = ref('')
const selectedRunId = ref('')
const selectedRun = ref<ResearchRunDetail>()
const scopeMode = ref<'topic' | 'selected' | 'library'>('topic')
const selectedDocumentIds = ref<string[]>([])
const question = ref('')
const loading = ref(true)
const creatingConversation = ref(false)
const submitting = ref(false)
const runAction = ref(false)
const refreshing = ref(false)
const workflowBusy = ref(false)
const errorMessage = ref('')
const workflowNotice = ref('')
let eventRunId = ''
let eventController: AbortController | undefined
let eventGeneration = 0
let eventSequence = 0
let refreshInFlight = false

const readyTopicDocuments = computed(() => linkedDocuments.value.filter((item) => item.document.status === 'ready'))
const readyLibraryDocuments = computed(() => libraryDocuments.value.filter((document) => document.status === 'ready'))
const active = computed(() => selectedRun.value && ['queued', 'running'].includes(selectedRun.value.status))
const citations = computed(() => {
  const last = selectedRun.value?.messages.filter((message) => message.role === 'assistant').at(-1)
  const value = last?.metadata.citations
  return Array.isArray(value) ? value as Array<{
    evidence_id: string
    document_id: string
    version_id: string
    page_number: number
    quote: string
    reason: string
    is_derived: boolean
  }> : []
})

function statusLabel(status: ConversationRunSummary['status']) {
  return ({
    queued: '正在排队',
    running: '正在研读',
    completed: '已完成',
    failed: '未能完成',
    cancelled: '已停止',
    interrupted: '等待继续',
    awaiting_user: '等待确认',
  } as Record<string, string>)[status] ?? '处理中'
}

function statusClass(status: ConversationRunSummary['status']) {
  if (status === 'completed') return 'run-status complete'
  if (status === 'failed' || status === 'interrupted' || status === 'awaiting_user') return 'run-status attention'
  if (status === 'cancelled') return 'run-status stopped'
  return 'run-status active'
}

function documentTitle(documentId: string) {
  return linkedDocuments.value.find((item) => item.document.id === documentId)?.document.title
    ?? libraryDocuments.value.find((item) => item.id === documentId)?.title
    ?? '研究文献'
}

function closeRunStream() {
  eventGeneration += 1
  eventController?.abort()
  eventController = undefined
  eventRunId = ''
  eventSequence = 0
}

function syncRunStream() {
  const run = selectedRun.value
  if (!run || !['queued', 'running'].includes(run.status)) {
    if (eventRunId) closeRunStream()
    return
  }
  if (eventRunId === run.id) return
  closeRunStream()
  eventRunId = run.id
  eventSequence = Math.max(0, ...run.events.map((event) => event.sequence_number))
  void watchRunEvents(run.id, eventGeneration)
}

async function refreshRunFromEvent(runId: string) {
  if (selectedRunId.value !== runId || !selectedConversationId.value) return
  try {
    const [detail, summaries] = await Promise.all([
      getResearchRun(runId),
      listConversationRuns(selectedConversationId.value),
    ])
    if (selectedRunId.value !== runId) return
    selectedRun.value = detail
    runs.value = summaries
    syncRunStream()
  } catch {
    // The stream reconnects from its last sequence and REST refresh can retry then.
  }
}

async function watchRunEvents(runId: string, generation: number) {
  let reconnectDelay = 500
  while (generation === eventGeneration && eventRunId === runId) {
    const controller = new AbortController()
    eventController = controller
    try {
      await streamResearchRunEvents(runId, eventSequence, controller.signal, async (eventName, event: ResearchRunStreamEvent) => {
        const sequence = Number(event.sequence_number)
        if (eventName === 'snapshot' && 'run' in event) {
          eventSequence = sequence
          if (selectedRunId.value === runId) selectedRun.value = event.run
          await refreshRunFromEvent(runId)
          return
        }
        if (!Number.isSafeInteger(sequence) || sequence <= eventSequence) return
        eventSequence = sequence
        await refreshRunFromEvent(runId)
      })
      if (generation !== eventGeneration || eventRunId !== runId) return
      await refreshRunFromEvent(runId)
      if (!['queued', 'running'].includes(selectedRun.value?.status ?? '')) return
    } catch {
      if (controller.signal.aborted) return
    } finally {
      if (eventController === controller) eventController = undefined
    }
    await new Promise((resolve) => window.setTimeout(resolve, reconnectDelay))
    reconnectDelay = Math.min(reconnectDelay * 2, 8000)
  }
}

async function loadConversationRuns(conversationId: string, preferredRunId?: string) {
  runs.value = await listConversationRuns(conversationId)
  const nextId = preferredRunId && runs.value.some((item) => item.id === preferredRunId)
    ? preferredRunId
    : runs.value[0]?.id ?? ''
  selectedRunId.value = nextId
  if (nextId) await loadSelectedRun(nextId)
  else {
    selectedRun.value = undefined
    closeRunStream()
  }
}

async function loadSelectedRun(runId: string) {
  if (eventRunId && eventRunId !== runId) closeRunStream()
  selectedRunId.value = runId
  workflowNotice.value = ''
  try {
    selectedRun.value = await getResearchRun(runId)
    syncRunStream()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法载入这次研读。'
  }
}

async function refreshSelectedRun(silent = false) {
  if (!selectedConversationId.value || refreshInFlight) return
  refreshInFlight = true
  if (!silent) refreshing.value = true
  try {
    runs.value = await listConversationRuns(selectedConversationId.value)
    const runId = selectedRunId.value || runs.value[0]?.id
    if (runId) {
      selectedRunId.value = runId
      selectedRun.value = await getResearchRun(runId)
    }
    syncRunStream()
    if (!silent) errorMessage.value = ''
  } catch (error) {
    if (!silent) errorMessage.value = error instanceof Error ? error.message : '暂时无法刷新研读状态。'
  } finally {
    refreshInFlight = false
    refreshing.value = false
  }
}

async function selectConversation(conversationId: string) {
  if (conversationId === selectedConversationId.value) return
  closeRunStream()
  selectedConversationId.value = conversationId
  selectedRunId.value = ''
  selectedRun.value = undefined
  runs.value = []
  question.value = ''
  errorMessage.value = ''
  workflowNotice.value = ''
  try {
    await loadConversationRuns(conversationId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法载入会话内容。'
  }
}

async function loadPage() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [topicResult, conversationsResult, linkedResult, libraryResult, memoriesResult, artifactsResult] = await Promise.all([
      getTopic(topicId.value),
      listConversations(topicId.value),
      listTopicDocuments(topicId.value),
      listDocuments(),
      listTopicMemories(topicId.value),
      listTopicArtifacts(topicId.value),
    ])
    topic.value = topicResult
    conversations.value = conversationsResult
    linkedDocuments.value = linkedResult
    libraryDocuments.value = libraryResult
    memories.value = memoriesResult
    memoryDrafts.value = Object.fromEntries(memoriesResult.map((item) => [item.id, item.content]))
    topicArtifacts.value = artifactsResult
    if (!topicArtifacts.value.some((item) => item.id === artifactTargetId.value)) artifactTargetId.value = ''
    if (selectedConversationId.value && conversationsResult.some((item) => item.id === selectedConversationId.value)) {
      await loadConversationRuns(selectedConversationId.value, selectedRunId.value)
    } else if (conversationsResult.length) {
      await selectConversation(conversationsResult[0].id)
    } else {
      selectedConversationId.value = ''
      selectedRun.value = undefined
      runs.value = []
      closeRunStream()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法打开专题研读。'
  } finally {
    loading.value = false
  }
}

function memoryStatusLabel(memory: ResearchMemory) {
  if (memory.status === 'pending') return '待你确认'
  if (memory.status === 'confirmed') return '已确认'
  if (memory.status === 'active') return '使用中'
  if (memory.status === 'rejected') return '已拒绝'
  if (memory.status === 'invalidated') return '来源已变化'
  return '已归档'
}

async function saveAnswerAsArtifact() {
  if (!selectedRun.value || selectedRun.value.status !== 'completed' || workflowBusy.value) return
  const answer = [...selectedRun.value.messages].reverse().find((item) => item.role === 'assistant')?.content.trim()
  if (!answer) return
  workflowBusy.value = true
  errorMessage.value = ''
  workflowNotice.value = ''
  try {
    const questionText = selectedRun.value.messages.find((item) => item.role === 'user')?.content ?? '专题研读'
    const existingTargetId = artifactTargetId.value
    if (existingTargetId) {
      await createStudyArtifactCandidate(existingTargetId, selectedRun.value.id)
    } else {
      const created = await saveRunAsArtifact(topicId.value, {
        title: questionText.slice(0, 72) || '专题研读', artifact_type: 'note', run_id: selectedRun.value.id,
      })
      topicArtifacts.value = [created, ...topicArtifacts.value]
      artifactTargetId.value = created.id
    }
    workflowNotice.value = existingTargetId ? '已生成候选版本，可在学习成果中比较或采纳。' : '已保存到学习成果。'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法保存这份研读成果。'
  } finally {
    workflowBusy.value = false
  }
}

async function saveConclusionMemory() {
  if (!selectedRun.value || selectedRun.value.status !== 'completed' || !citations.value.length || workflowBusy.value) return
  const answer = [...selectedRun.value.messages].reverse().find((item) => item.role === 'assistant')?.content.trim()
  if (!answer) return
  workflowBusy.value = true
  errorMessage.value = ''
  workflowNotice.value = ''
  try {
    const created = await createTopicMemory(topicId.value, {
      memory_type: 'research_conclusion',
      content: answer.slice(0, 4000),
      source_run_id: selectedRun.value.id,
      evidence_ids: citations.value.map((item) => item.evidence_id),
    })
    memories.value = [created, ...memories.value]
    memoryDrafts.value = { ...memoryDrafts.value, [created.id]: created.content }
    workflowNotice.value = '已保存为待确认结论；确认后才会用于后续研读。'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法保存待确认结论。'
  } finally {
    workflowBusy.value = false
  }
}

async function savePreferenceMemory() {
  const content = preferenceDraft.value.trim()
  if (!content || workflowBusy.value) return
  workflowBusy.value = true
  errorMessage.value = ''
  workflowNotice.value = ''
  try {
    const created = await createTopicMemory(topicId.value, { memory_type: 'preference', content })
    memories.value = [created, ...memories.value]
    memoryDrafts.value = { ...memoryDrafts.value, [created.id]: created.content }
    preferenceDraft.value = ''
    workflowNotice.value = '已保存偏好。'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法保存偏好。'
  } finally { workflowBusy.value = false }
}

async function actOnMemory(memory: ResearchMemory, action: 'edit' | 'confirm' | 'reject' | 'archive') {
  if (workflowBusy.value) return
  workflowBusy.value = true
  errorMessage.value = ''
  workflowNotice.value = ''
  try {
    const updated = await updateTopicMemory(memory.id, {
      expected_revision: memory.revision,
      action,
      ...(action === 'edit' ? { content: memoryDrafts.value[memory.id] ?? memory.content } : {}),
    })
    memories.value = memories.value.map((item) => item.id === updated.id ? updated : item)
    memoryDrafts.value = { ...memoryDrafts.value, [updated.id]: updated.content }
    workflowNotice.value = action === 'confirm' ? '结论已确认，可用于后续研读。' : action === 'reject' ? '已拒绝这条结论。' : action === 'archive' ? '已归档这条记忆。' : '已保存修改。'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法更新这条记忆。'
    try {
      memories.value = await listTopicMemories(topicId.value)
      memoryDrafts.value = Object.fromEntries(memories.value.map((item) => [item.id, item.content]))
    } catch { /* Keep the original user-facing error if refresh also fails. */ }
  } finally { workflowBusy.value = false }
}

async function newConversation() {
  if (creatingConversation.value) return
  creatingConversation.value = true
  errorMessage.value = ''
  try {
    const conversation = await createConversation(topicId.value)
    conversations.value = [conversation, ...conversations.value]
    await selectConversation(conversation.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法创建会话。'
  } finally {
    creatingConversation.value = false
  }
}

function makeIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function submitQuestion() {
  const cleanQuestion = question.value.trim()
  if (!cleanQuestion || submitting.value || !selectedConversationId.value) return
  if (scopeMode.value === 'selected' && !selectedDocumentIds.value.length) {
    errorMessage.value = '请选择至少一篇文献。'
    return
  }
  submitting.value = true
  errorMessage.value = ''
  try {
    const payload = {
      question: cleanQuestion,
      scope_mode: scopeMode.value,
      ...(scopeMode.value === 'selected' ? { document_ids: [...selectedDocumentIds.value] } : {}),
    }
    const receipt = await createResearchRun(selectedConversationId.value, payload, makeIdempotencyKey())
    question.value = ''
    await loadConversationRuns(selectedConversationId.value, receipt.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法提交问题。'
  } finally {
    submitting.value = false
  }
}

async function stopRun() {
  if (!selectedRunId.value || runAction.value) return
  runAction.value = true
  errorMessage.value = ''
  try {
    selectedRun.value = await stopResearchRun(selectedRunId.value)
    await refreshSelectedRun(true)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法停止本次研读。'
  } finally {
    runAction.value = false
  }
}

async function continueRun() {
  if (!selectedRunId.value || runAction.value) return
  runAction.value = true
  errorMessage.value = ''
  try {
    await continueResearchRun(selectedRunId.value, makeIdempotencyKey())
    await refreshSelectedRun(true)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法继续本次研读。'
  } finally {
    runAction.value = false
  }
}

function onScopeChanged() {
  selectedDocumentIds.value = []
}

onMounted(() => { void loadPage() })
onBeforeUnmount(closeRunStream)
watch(topicId, () => { closeRunStream(); selectedConversationId.value = ''; selectedRunId.value = ''; void loadPage() })
</script>

<template>
  <div class="page-content conversation-page">
    <div class="conversation-breadcrumb"><RouterLink to="/topics">专题研究</RouterLink><span>/</span><span>{{ topic?.title || '专题研读' }}</span></div>
    <header class="conversation-heading">
      <div>
        <div class="eyebrow"><span class="eyebrow-line"></span> READING SPACE</div>
        <h1>{{ topic?.title || '专题研读' }}</h1>
        <p>{{ topic?.goal || '围绕专题文献提问，整理可回到原文核对的证据。' }}</p>
      </div>
      <RouterLink class="workspace-back" to="/topics">返回专题</RouterLink>
    </header>

    <p v-if="errorMessage" class="conversation-alert" role="alert">{{ errorMessage }}</p>
    <div v-if="loading" class="conversation-loading panel" role="status"><span class="topic-spinner"></span>正在打开专题研读…</div>
    <div v-else class="conversation-layout">
      <aside class="conversation-rail panel" aria-label="会话列表">
        <div class="rail-heading"><div><span class="section-kicker">DISCUSSIONS</span><h2>研读会话</h2></div><button type="button" aria-label="新建会话" :disabled="creatingConversation" @click="newConversation">＋</button></div>
        <button v-if="!conversations.length" class="rail-new-card" type="button" :disabled="creatingConversation" @click="newConversation">
          <span class="rail-new-mark">＋</span><span><strong>开始新的研读</strong><small>提出一个问题，随时回来继续</small></span>
        </button>
        <button
          v-for="conversation in conversations"
          :key="conversation.id"
          type="button"
          class="conversation-choice"
          :class="{ 'is-selected': selectedConversationId === conversation.id }"
          @click="selectConversation(conversation.id)"
        >
          <span class="conversation-choice-mark" aria-hidden="true">聊</span>
          <span class="conversation-choice-copy"><strong>{{ conversation.title }}</strong><small>{{ new Date(conversation.updated_at).toLocaleDateString('zh-CN') }}</small></span>
          <span class="conversation-choice-arrow" aria-hidden="true">›</span>
        </button>
        <div v-if="conversations.length" class="rail-footer"><button type="button" :disabled="creatingConversation" @click="newConversation">＋ 新建会话</button></div>
      </aside>

      <main class="conversation-main">
        <section v-if="selectedConversationId" class="thread-panel panel">
          <div class="thread-toolbar">
            <div><span class="section-kicker">YOUR READING</span><h2>{{ conversations.find((item) => item.id === selectedConversationId)?.title || '研读会话' }}</h2></div>
            <button class="refresh-button" type="button" :disabled="refreshing" @click="refreshSelectedRun()">{{ refreshing ? '更新中…' : '刷新状态' }}</button>
          </div>
          <div v-if="!runs.length" class="thread-empty">
            <div class="empty-illustration"><span class="empty-illustration-ring"></span><span class="empty-illustration-mark">✳</span></div>
            <h3>从一个具体问题开始</h3>
            <p>选择本次要参考的文献范围，再提出问题。每个回答都可以回到对应原文页核对。</p>
          </div>
          <div v-else class="thread-messages" aria-live="polite">
            <article v-for="run in [...runs].reverse()" :key="run.id" class="run-exchange" :class="{ 'is-focused': selectedRunId === run.id }" @click="loadSelectedRun(run.id)">
              <div class="question-row"><span class="speaker-mark user-mark">你</span><div class="question-content"><p>{{ run.question }}</p><div class="question-meta"><span>{{ new Date(run.created_at).toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }) }}</span><span :class="statusClass(run.status)">{{ statusLabel(run.status) }}</span></div></div></div>
              <div class="answer-row"><span class="speaker-mark assistant-mark">研</span><div class="answer-content">
                <p v-if="run.answer" class="answer-text">{{ run.answer }}</p>
                <p v-else-if="run.status === 'queued' || run.status === 'running'" class="answer-pending"><span class="topic-spinner"></span>正在结合文献整理回答…</p>
                <p v-else-if="run.status === 'cancelled'" class="answer-muted">本次研读已停止。</p>
                <p v-else class="answer-muted">本次没有生成回答，可以调整问题或范围后重新提问。</p>
                <div v-if="selectedRunId === run.id && citations.length" class="citation-list" aria-label="回答引用">
                  <RouterLink
                    v-for="citation in citations"
                    :key="citation.evidence_id"
                    class="citation-card"
                    :to="`/documents/${encodeURIComponent(citation.document_id)}?page=${citation.page_number}`"
                    @click.stop
                  >
                    <span class="citation-page">{{ citation.page_number }}</span>
                    <span class="citation-copy"><strong>{{ documentTitle(citation.document_id) }} · 第 {{ citation.page_number }} 页</strong><q v-if="citation.quote">{{ citation.quote }}</q><small>{{ citation.is_derived ? '识别线索，请对照原文核验' : citation.reason || '查看原文' }}</small></span>
                    <span class="citation-arrow" aria-hidden="true">↗</span>
                  </RouterLink>
                </div>
                <div v-if="selectedRunId === run.id && selectedRun" class="answer-actions" @click.stop>
                  <button v-if="active" type="button" class="stop-run-button" :disabled="runAction" @click="stopRun">{{ runAction ? '正在停止…' : '停止研读' }}</button>
                  <button v-else-if="selectedRun.status === 'interrupted' || selectedRun.status === 'awaiting_user'" type="button" class="continue-run-button" :disabled="runAction" @click="continueRun">{{ runAction ? '正在继续…' : '继续研读' }}</button>
                  <span v-if="selectedRun.status === 'awaiting_user'" class="answer-note">继续前会重新确认专题和文献仍可用。</span>
                  <template v-if="selectedRun.status === 'completed' && selectedRun.messages.some((message) => message.role === 'assistant')">
                    <select v-model="artifactTargetId" class="answer-artifact-select" aria-label="学习成果保存位置">
                      <option value="">保存为新成果</option>
                      <option v-for="artifact in topicArtifacts" :key="artifact.id" :value="artifact.id">生成「{{ artifact.title }}」的新版本</option>
                    </select>
                    <button type="button" class="continue-run-button" :disabled="workflowBusy" @click="saveAnswerAsArtifact">{{ workflowBusy ? '正在保存…' : artifactTargetId ? '生成候选版本' : '保存到学习成果' }}</button>
                    <button v-if="citations.length" type="button" class="continue-run-button" :disabled="workflowBusy" @click="saveConclusionMemory">保存为待确认结论</button>
                  </template>
                </div>
              </div></div>
            </article>
          </div>
        </section>
        <section v-else class="thread-panel panel new-session-panel">
          <div class="empty-illustration"><span class="empty-illustration-ring"></span><span class="empty-illustration-mark">⌕</span></div>
          <h2>把文献放进一个问题里</h2><p>新建会话后，就可以围绕专题文献持续提问。</p>
          <button type="button" class="workspace-primary" :disabled="creatingConversation" @click="newConversation">{{ creatingConversation ? '正在创建…' : '新建研读会话' }}</button>
        </section>

        <section v-if="selectedConversationId" class="composer-panel panel" aria-label="提出研究问题">
          <div class="scope-heading"><div><span class="section-kicker">SOURCE SCOPE</span><strong>本次参考范围</strong></div><span class="scope-count">{{ scopeMode === 'topic' ? readyTopicDocuments.length : scopeMode === 'selected' ? selectedDocumentIds.length : readyLibraryDocuments.length }} 篇可用文献</span></div>
          <label class="scope-select-label" for="scope-mode">选择文献范围</label>
          <select id="scope-mode" v-model="scopeMode" class="scope-select" @change="onScopeChanged">
            <option value="topic">本专题文献</option><option value="selected">指定文献</option><option value="library">整个文献库</option>
          </select>
          <div v-if="scopeMode === 'selected'" class="scope-document-picker">
            <label v-for="item in readyTopicDocuments" :key="item.document.id" class="scope-document-option">
              <input v-model="selectedDocumentIds" type="checkbox" :value="item.document.id"><span>{{ item.document.title }}</span>
            </label>
            <p v-if="!readyTopicDocuments.length">专题还没有可用于研读的文献。</p>
          </div>
          <p v-else-if="scopeMode === 'topic' && !readyTopicDocuments.length" class="scope-note">先在专题中关联并整理至少一篇文献，再开始提问。</p>
          <p v-else-if="scopeMode === 'library'" class="scope-note">本次提问会明确使用整个文献库中已整理完成的文献。</p>
          <form class="question-composer" @submit.prevent="submitQuestion">
            <textarea v-model="question" rows="3" maxlength="2000" :disabled="!selectedConversationId" placeholder="例如：这些论文对多模态模型的评估方式有哪些共同点？" aria-label="研究问题" @keydown.ctrl.enter.prevent="submitQuestion" @keydown.meta.enter.prevent="submitQuestion" />
            <div class="composer-footer"><span>Enter 换行 · Ctrl/⌘ + Enter 提交</span><button type="submit" class="workspace-primary" :disabled="submitting || !question.trim() || (scopeMode === 'topic' && !readyTopicDocuments.length) || (scopeMode === 'selected' && !selectedDocumentIds.length) || (scopeMode === 'library' && !readyLibraryDocuments.length)">{{ submitting ? '正在提交…' : '开始研读' }} <span aria-hidden="true">→</span></button></div>
          </form>
        </section>

        <section v-if="selectedConversationId" class="memory-panel panel" aria-labelledby="memory-heading">
          <div class="memory-panel-heading"><div><span class="section-kicker">TOPIC MEMORY</span><h2 id="memory-heading">专题记忆</h2></div><span>{{ memories.length }} 条</span></div>
          <p v-if="workflowNotice" class="memory-notice" role="status">{{ workflowNotice }}</p>
          <form class="preference-form" @submit.prevent="savePreferenceMemory">
            <label for="preference-memory">记录一条偏好</label>
            <div><input id="preference-memory" v-model="preferenceDraft" maxlength="4000" placeholder="例如：优先比较研究设置与局限" /><button type="submit" class="memory-action" :disabled="workflowBusy || !preferenceDraft.trim()">保存偏好</button></div>
          </form>
          <div v-if="!memories.length" class="memory-empty">保存的偏好和待确认研究结论会显示在这里。</div>
          <article v-for="memory in memories" :key="memory.id" class="memory-row">
            <div class="memory-row-heading"><strong>{{ memory.memory_type === 'preference' ? '阅读偏好' : memory.memory_type === 'research_conclusion' ? '研究结论' : '过程摘要' }}</strong><span :class="['memory-status', `status-${memory.status}`]">{{ memoryStatusLabel(memory) }}</span></div>
            <textarea v-model="memoryDrafts[memory.id]" rows="2" maxlength="4000" :disabled="['archived', 'invalidated', 'rejected'].includes(memory.status)" :aria-label="`编辑${memory.memory_type === 'preference' ? '偏好' : '研究结论'}`" />
            <div class="memory-actions">
              <button v-if="memory.status === 'active' || memory.status === 'pending' || memory.status === 'confirmed'" type="button" class="memory-action" :disabled="workflowBusy || !memoryDrafts[memory.id]?.trim()" @click="actOnMemory(memory, 'edit')">保存修改</button>
              <button v-if="memory.status === 'pending'" type="button" class="memory-action primary" :disabled="workflowBusy" @click="actOnMemory(memory, 'confirm')">确认结论</button>
              <button v-if="memory.status === 'pending'" type="button" class="memory-action" :disabled="workflowBusy" @click="actOnMemory(memory, 'reject')">拒绝</button>
              <button v-if="!['archived', 'invalidated'].includes(memory.status)" type="button" class="memory-action quiet" :disabled="workflowBusy" @click="actOnMemory(memory, 'archive')">归档</button>
              <small v-if="memory.memory_type === 'research_conclusion'">依据 {{ memory.evidence_snapshot.length }} 条原文线索</small>
            </div>
          </article>
          <RouterLink class="memory-artifact-link" to="/artifacts">查看学习成果 <span>→</span></RouterLink>
        </section>
      </main>
    </div>
  </div>
</template>

<style scoped>
.conversation-page{padding:30px 0 36px}.conversation-breadcrumb{display:flex;align-items:center;gap:9px;color:#9ba198;font-size:9px}.conversation-breadcrumb a{color:#66806f}.conversation-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin:15px 0 22px}.conversation-heading h1{margin:10px 0 6px;color:#28362f;font:500 26px/1.4 'Noto Serif SC',serif;letter-spacing:-.03em}.conversation-heading p{max-width:720px;margin:0;color:#899187;font-size:11px;line-height:1.8}.workspace-back{padding:8px 10px;border:1px solid #e1e6dc;border-radius:4px;color:#607565;background:#fbfbf7;font-size:9px}.workspace-back:hover{background:#f1f4ed}.conversation-alert{margin:0 0 14px;padding:10px 12px;border:1px solid #edd7cd;border-radius:4px;color:#945e4b;background:#fcf5f1;font-size:10px;line-height:1.7}.conversation-loading{min-height:260px;display:flex;align-items:center;justify-content:center;gap:10px;color:#8d978c;font-size:10px}.conversation-layout{display:grid;grid-template-columns:minmax(220px,275px) minmax(0,1fr);align-items:start;gap:14px}.conversation-rail{position:sticky;top:16px;overflow:hidden;padding:15px 12px 10px}.rail-heading,.thread-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px}.rail-heading{padding:0 3px 13px;border-bottom:1px solid #eceee7}.rail-heading h2,.thread-toolbar h2{margin:4px 0 0;color:#3a483e;font:500 14px 'Noto Serif SC',serif}.rail-heading>button{width:28px;height:28px;border:1px solid #dce5da;border-radius:4px;color:#547360;background:#f7f9f4;font-size:16px;cursor:pointer}.rail-heading>button:disabled{opacity:.5}.conversation-choice{width:100%;display:flex;align-items:center;gap:9px;margin-top:7px;padding:10px 8px;border:1px solid transparent;border-radius:5px;color:#59665c;background:transparent;text-align:left;cursor:pointer}.conversation-choice:hover{background:#f7f8f3}.conversation-choice.is-selected{border-color:#dce7d9;background:#f0f4ed}.conversation-choice-mark,.rail-new-mark{width:27px;height:27px;display:grid;flex:0 0 auto;place-items:center;border:1px solid #e3e8df;border-radius:50%;color:#708677;background:#fff;font:10px 'Noto Serif SC',serif}.conversation-choice-copy{min-width:0;display:flex;flex:1;flex-direction:column;gap:4px}.conversation-choice-copy strong{overflow:hidden;color:#4c5b50;font-size:10px;font-weight:600;text-overflow:ellipsis;white-space:nowrap}.conversation-choice-copy small{color:#9ca39a;font-size:8px}.conversation-choice-arrow{color:#a4aea2;font-size:15px}.rail-new-card{width:100%;display:flex;align-items:center;gap:10px;margin-top:14px;padding:12px 8px;border:1px dashed #d7e0d5;border-radius:5px;color:#617366;background:#fafbf7;text-align:left;cursor:pointer}.rail-new-card:disabled{opacity:.6}.rail-new-card>span:last-child{display:flex;flex-direction:column;gap:4px}.rail-new-card strong{font:500 10px 'Noto Serif SC',serif}.rail-new-card small{color:#999f96;font-size:8px}.rail-footer{margin-top:11px;padding:9px 4px 0;border-top:1px solid #eceee7}.rail-footer button{border:0;color:#66806e;background:transparent;font-size:9px;cursor:pointer}.conversation-main{min-width:0}.thread-panel{overflow:hidden}.thread-toolbar{min-height:64px;padding:12px 18px;border-bottom:1px solid #e9ebe4;background:#fbfbf7}.thread-toolbar .section-kicker,.rail-heading .section-kicker,.scope-heading .section-kicker{color:#839184}.refresh-button{padding:6px 8px;border:1px solid #e2e7de;border-radius:4px;color:#748176;background:#fff;font-size:8px;cursor:pointer}.refresh-button:disabled{opacity:.6}.thread-empty{min-height:310px;display:flex;align-items:center;justify-content:center;flex-direction:column;padding:25px;text-align:center}.empty-illustration{width:68px;height:68px;position:relative;display:grid;place-items:center;margin-bottom:15px;border-radius:50%;background:#eff2eb}.empty-illustration-ring{position:absolute;inset:8px;border:1px solid #d7ded2;border-radius:50%}.empty-illustration-mark{color:#6d8975;font:23px 'Noto Serif SC',serif}.thread-empty h3,.new-session-panel h2{margin:0 0 7px;color:#3c4940;font:500 14px 'Noto Serif SC',serif}.thread-empty p,.new-session-panel>p{max-width:410px;margin:0;color:#92998f;font-size:9px;line-height:1.8}.thread-messages{display:flex;flex-direction:column;gap:0;padding:4px 18px}.run-exchange{margin:0 -7px;padding:18px 7px;border-bottom:1px solid #eef0e9;border-radius:4px;cursor:pointer}.run-exchange:last-child{border-bottom:0}.run-exchange.is-focused{background:linear-gradient(90deg,#f9faf6 0%,#fff 85%)}.question-row,.answer-row{display:grid;grid-template-columns:27px minmax(0,1fr);gap:10px}.answer-row{margin-top:15px}.speaker-mark{width:25px;height:25px;display:grid;place-items:center;border-radius:50%;font:9px 'Noto Serif SC',serif}.user-mark{color:#6a795e;background:#eef1e6}.assistant-mark{color:#faf9f2;background:#55745f}.question-content,.answer-content{min-width:0}.question-content>p,.answer-text{margin:1px 0 0;color:#404c43;font-size:11px;line-height:1.9;white-space:pre-wrap;overflow-wrap:anywhere}.question-meta{display:flex;align-items:center;gap:9px;margin-top:6px;color:#a0a69d;font-size:8px}.run-status{display:inline-flex;align-items:center;gap:4px}.run-status:before{width:5px;height:5px;display:inline-block;border-radius:50%;background:#9aa69a;content:''}.run-status.active{color:#708774}.run-status.active:before{background:#72a17b;animation:pulse 1.4s ease-in-out infinite}.run-status.complete{color:#6c8a70}.run-status.complete:before{background:#77a17b}.run-status.attention{color:#9b7753}.run-status.attention:before{background:#be9161}.run-status.stopped{color:#92998f}.answer-pending,.answer-muted{display:flex;align-items:center;gap:8px;margin:2px 0;color:#889388;font-size:10px;line-height:1.8}.answer-muted{color:#989e95}.answer-pending :deep(.topic-spinner){width:12px;height:12px}.citation-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:11px}.citation-card{min-width:0;display:flex;align-items:flex-start;gap:9px;padding:9px;border:1px solid #e5e9e0;border-radius:4px;color:#667269;background:#fbfcf8;transition:border-color .16s ease,background .16s ease}.citation-card:hover{border-color:#bfd0bd;background:#f5f8f2}.citation-page{width:25px;height:30px;display:grid;flex:0 0 auto;place-items:center;border:1px solid #e2e8df;border-radius:3px;color:#67806b;background:#fff;font:11px 'DM Mono',monospace}.citation-copy{min-width:0;display:flex;flex-direction:column;gap:4px}.citation-copy strong{color:#52675a;font-size:8px;font-weight:600;line-height:1.5}.citation-copy q{display:-webkit-box;overflow:hidden;color:#657267;font-size:8px;line-height:1.6;text-overflow:ellipsis;-webkit-line-clamp:2;-webkit-box-orient:vertical}.citation-copy small{color:#9ba298;font-size:7px;line-height:1.5}.citation-arrow{margin-left:auto;color:#91a092;font-size:11px}.answer-actions{display:flex;align-items:center;gap:9px;margin-top:9px}.stop-run-button,.continue-run-button{padding:6px 9px;border:1px solid #e4e5dd;border-radius:4px;color:#7e685e;background:#fff;font-size:8px;cursor:pointer}.continue-run-button{border-color:#d8e3d6;color:#58715c;background:#f8faf5}.stop-run-button:disabled,.continue-run-button:disabled{opacity:.6}.answer-note{color:#9a9f96;font-size:8px}.composer-panel{margin-top:13px;padding:14px 16px 12px}.scope-heading{display:flex;align-items:center;justify-content:space-between;gap:10px}.scope-heading>div{display:flex;flex-direction:column;gap:4px}.scope-heading strong{color:#46544a;font:500 11px 'Noto Serif SC',serif}.scope-count{color:#879487;font-size:8px}.scope-select-label{display:block;margin-top:12px;color:#838c83;font-size:8px}.scope-select{min-width:180px;height:31px;margin-top:5px;padding:0 8px;border:1px solid #e0e5dc;border-radius:4px;color:#54665a;background:#fff;font-size:9px}.scope-document-picker{max-height:105px;display:flex;flex-wrap:wrap;gap:7px;overflow:auto;margin-top:8px;padding:8px;border:1px solid #edf0e9;border-radius:4px;background:#fcfdf9}.scope-document-option{max-width:100%;display:flex;align-items:center;gap:6px;color:#637065;font-size:8px;cursor:pointer}.scope-document-option input{accent-color:#55745f}.scope-document-option span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.scope-document-picker>p,.scope-note{margin:0;color:#9a9f96;font-size:8px;line-height:1.7}.scope-note{margin-top:7px}.question-composer{margin-top:11px;border:1px solid #e5e9e0;border-radius:5px;background:#fff;transition:border-color .16s ease,box-shadow .16s ease}.question-composer:focus-within{border-color:#a8bba6;box-shadow:0 0 0 3px #6c8c7210}.question-composer textarea{width:100%;resize:vertical;min-height:74px;padding:11px 12px;border:0;outline:0;color:#455249;background:transparent;font:10px/1.8 'Noto Sans SC',sans-serif}.question-composer textarea::placeholder{color:#a5aaa2}.composer-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:7px 9px;border-top:1px solid #f0f1ec;color:#a2a79f;font-size:7px}.workspace-primary{min-height:31px;padding:0 11px;border:1px solid #4a725b;border-radius:4px;color:#fffdf7;background:#4a725b;font-size:9px;cursor:pointer;transition:background .16s ease,transform .16s ease}.workspace-primary:hover{background:#365d47}.workspace-primary:active{transform:translateY(1px)}.workspace-primary:disabled{opacity:.5;cursor:default}.new-session-panel{min-height:390px;display:flex;align-items:center;justify-content:center;flex-direction:column;text-align:center}.new-session-panel .workspace-primary{margin-top:15px}@keyframes pulse{50%{opacity:.45}}@media(max-width:930px){.conversation-layout{grid-template-columns:minmax(190px,230px) minmax(0,1fr)}.citation-list{grid-template-columns:1fr}}@media(max-width:720px){.conversation-page{padding-top:23px}.conversation-heading{align-items:flex-start}.conversation-heading h1{font-size:22px}.workspace-back{flex:0 0 auto}.conversation-layout{grid-template-columns:1fr;gap:10px}.conversation-rail{position:static;display:flex;align-items:center;gap:5px;overflow:auto;padding:9px}.rail-heading{min-width:112px;display:flex;align-items:center;padding:0 7px 0 0;border:0;border-right:1px solid #eceee7}.rail-heading h2{font-size:11px}.rail-heading>button{width:25px;height:25px;flex:0 0 auto}.conversation-choice{width:auto;min-width:135px;margin:0}.conversation-choice-arrow{display:none}.rail-new-card{min-width:175px;margin:0}.rail-footer{display:none}.thread-toolbar{padding:11px 13px}.thread-messages{padding:3px 12px}.composer-panel{padding:12px}.citation-list{grid-template-columns:1fr}.thread-empty{min-height:260px}}@media(max-width:480px){.conversation-heading{flex-direction:column;gap:12px}.conversation-heading h1{font-size:20px}.conversation-heading p{font-size:10px}.question-row,.answer-row{grid-template-columns:23px minmax(0,1fr);gap:8px}.speaker-mark{width:22px;height:22px}.question-content>p,.answer-text{font-size:10px}.scope-select{width:100%}.composer-footer{align-items:flex-end}.composer-footer>span{max-width:140px;line-height:1.5}}@media(prefers-reduced-motion:reduce){.run-status.active:before{animation:none}.citation-card,.workspace-primary{transition:none}}
.answer-artifact-select{max-width:205px;height:27px;padding:0 7px;border:1px solid #e0e5dc;border-radius:4px;color:#667469;background:#fff;font-size:8px}.memory-panel{margin-top:13px;padding:15px 16px}.memory-panel-heading{display:flex;align-items:center;justify-content:space-between;padding-bottom:10px;border-bottom:1px solid #eceee7}.memory-panel-heading>div{display:flex;flex-direction:column;gap:4px}.memory-panel-heading h2{margin:0;color:#3a483e;font:500 13px 'Noto Serif SC',serif}.memory-panel-heading>span{color:#9a9f96;font-size:8px}.memory-notice{margin:10px 0 0;padding:8px 9px;border:1px solid #dfe9dc;border-radius:4px;color:#5d775f;background:#f4f8f1;font-size:8px;line-height:1.6}.preference-form{padding:11px 0;border-bottom:1px solid #eceee7}.preference-form>label{display:block;margin-bottom:6px;color:#768276;font-size:8px}.preference-form>div{display:flex;gap:7px}.preference-form input,.memory-row textarea{width:100%;min-width:0;padding:7px 8px;border:1px solid #e5e9e0;border-radius:4px;color:#4c5b50;background:#fff;font:9px/1.7 'Noto Sans SC',sans-serif;outline:0}.preference-form input:focus,.memory-row textarea:focus{border-color:#a8bba6}.memory-action{flex:0 0 auto;padding:6px 8px;border:1px solid #dce5da;border-radius:4px;color:#59705e;background:#f9fbf7;font-size:8px;cursor:pointer}.memory-action.primary{color:#fff;background:#55745f;border-color:#55745f}.memory-action.quiet{color:#8a8f87;background:#fff}.memory-action:disabled{opacity:.5;cursor:default}.memory-empty{padding:14px 2px;color:#9a9f96;font-size:8px;line-height:1.7}.memory-row{padding:11px 1px;border-bottom:1px solid #f0f1ec}.memory-row-heading{display:flex;align-items:center;justify-content:space-between;gap:9px;margin-bottom:6px}.memory-row-heading strong{color:#5a685d;font-size:8px;font-weight:600}.memory-status{font-size:7px;color:#818980}.memory-status.status-pending{color:#98764f}.memory-status.status-confirmed,.memory-status.status-active{color:#66836b}.memory-status.status-invalidated{color:#a56655}.memory-row textarea{resize:vertical;min-height:43px}.memory-actions{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-top:6px}.memory-actions small{margin-left:auto;color:#a0a69d;font-size:7px}.memory-artifact-link{display:inline-block;margin-top:12px;color:#607b65;font-size:8px}.memory-artifact-link span{padding-left:3px}@media(max-width:720px){.answer-actions{align-items:flex-start;flex-wrap:wrap}.answer-artifact-select{max-width:100%}.memory-panel{padding:12px}.memory-actions small{margin-left:0}}
</style>
