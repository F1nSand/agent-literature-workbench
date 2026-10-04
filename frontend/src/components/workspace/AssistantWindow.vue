<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { listDocuments, type DocumentSummary } from '../../api/workbench'
import { useWorkspaceContextStore } from '../../stores/workspaceContext'
import { useWorkspaceAssistantStore } from '../../stores/workspaceAssistant'
import { useWorkspaceChromeStore } from '../../stores/workspaceChrome'
import { useWorkspaceNotesStore } from '../../stores/workspaceNotes'
import SvgIcon from '../SvgIcon.vue'
import type { WorkspaceUiAction } from '../../types/workspace'

const emit = defineEmits<{ close: []; minimize: []; toggleHistory: [] }>()
const router = useRouter()
const store = useWorkspaceAssistantStore()
const context = useWorkspaceContextStore()
const chrome = useWorkspaceChromeStore()
const notes = useWorkspaceNotesStore()
const documents = ref<DocumentSummary[]>([])
const showScope = ref(false)
const pageHint = computed(() => {
  const value = context.pageContext
  const document = documents.value.find((item) => item.id === value.document_id)
  if (!document) return value.page_type === 'reader' ? '正在载入文献' : value.page_type === 'library' ? '文献库' : value.page_type === 'home' ? '工作台首页' : value.page_type === 'settings' ? '设置' : value.page_type === 'note' ? '笔记历史' : '全局助手'
  const position = value.locator && 'page' in value.locator ? ` · 第 ${value.locator.page} 页` : ''
  return `${document.title}${position}`
})

onMounted(() => {
  if (!store.conversations.length) void store.load()
  void refreshDocuments()
})
async function refreshDocuments() {
  try { documents.value = (await listDocuments()).filter((item) => item.status === 'ready') } catch { /* Keep the last available document list. */ }
}
watch(() => chrome.windows.assistant.visible, (visible) => { if (visible) void refreshDocuments() })
watch(showScope, (visible) => { if (visible) void refreshDocuments() })
watch(() => context.pageContext.document_id, () => { void refreshDocuments() })

function messageTime(value: string) { return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }
function setSelected(document: DocumentSummary, checked: boolean) {
  const ids = new Set(store.selectedDocumentIds)
  if (checked) ids.add(document.id); else ids.delete(document.id)
  store.selectedDocumentIds = [...ids]
}
function promptRename(id: string, title: string) {
  const value = window.prompt('为这段对话命名', title)
  if (value !== null) void store.rename(id, value)
}
function openCitation(citation: Record<string, unknown>) {
  if (typeof citation.document_id !== 'string') return
  const query: Record<string, string> = {}
  if (typeof citation.version_id === 'string') query.version = citation.version_id
  const locator = citation.locator && typeof citation.locator === 'object' ? citation.locator as Record<string, unknown> : undefined
  if (locator && typeof locator.page === 'number') query.page = String(locator.page)
  if (locator && typeof locator.chapter === 'string') query.chapter = locator.chapter
  if (locator && typeof locator.cfi === 'string') query.cfi = locator.cfi
  if (locator && typeof locator.anchor === 'string') query.anchor = locator.anchor
  if (!Object.keys(query).some((key) => ['page', 'chapter', 'cfi', 'anchor'].includes(key)) && typeof citation.page_number === 'number') query.page = String(citation.page_number)
  void router.push({ path: `/documents/${encodeURIComponent(citation.document_id)}`, query })
}
async function openAction(action: WorkspaceUiAction) {
  if (store.claimedActions.includes(action.action_id)) {
    await store.retryActionAck(action.action_id).catch(() => undefined)
    return
  }
  if (!store.claimAction(action.action_id)) return
  try {
    if (action.type === 'open_document' && action.document_id) {
      const query: Record<string, string> = {}
      if (action.version_id) query.version = action.version_id
      const locator = action.locator
      if (locator && typeof locator.page === 'number') query.page = String(locator.page)
      if (locator && typeof locator.chapter === 'string') query.chapter = locator.chapter
      if (locator && typeof locator.cfi === 'string') query.cfi = locator.cfi
      if (locator && typeof locator.anchor === 'string') query.anchor = locator.anchor
      await router.push({ path: `/documents/${encodeURIComponent(action.document_id)}`, query })
    } else if (action.type === 'show_note_candidate' && action.note_id && action.candidate_id) {
      await useWorkspaceNotesStore().receiveCandidateById(action.note_id, action.candidate_id, action.expected_revision)
      chrome.show('notes')
    } else if (action.type === 'focus_note') chrome.show('notes')
    await store.acknowledgeAction(action.action_id, { status: 'succeeded' })
  } catch (cause) {
    store.error = cause instanceof Error ? cause.message : '页面操作失败。'
    await store.acknowledgeAction(action.action_id, { status: 'failed', error: store.error }).catch(() => undefined)
  }
}
async function chooseDocument(entry: { runId: string; action: WorkspaceUiAction }, candidate: NonNullable<WorkspaceUiAction['candidates']>[number]) {
  const { action } = entry
  if (store.claimedActions.includes(action.action_id)) {
    await store.retryActionAck(action.action_id).catch(() => undefined)
    return
  }
  if (!store.claimAction(action.action_id)) return
  try {
    const query: Record<string, string> = { version: candidate.version_id }
    const locator = candidate.locator
    if (locator && typeof locator.page === 'number') query.page = String(locator.page)
    if (locator && typeof locator.chapter === 'string') query.chapter = locator.chapter
    if (locator && typeof locator.cfi === 'string') query.cfi = locator.cfi
    if (locator && typeof locator.anchor === 'string') query.anchor = locator.anchor
    await router.push({ path: `/documents/${encodeURIComponent(candidate.document_id)}`, query })
  } catch (cause) {
    await store.acknowledgeAction(action.action_id, { status: 'failed', error: cause instanceof Error ? cause.message : '无法打开这篇文献。' }).catch(() => undefined)
    return
  }
  await store.acknowledgeAction(action.action_id, { status: 'succeeded', selected_document_id: candidate.document_id, selected_version_id: candidate.version_id }).catch(() => undefined)
}
</script>

<template>
  <section class="workspace-window-content assistant-window-content">
    <div class="workspace-window-heading assistant-heading">
      <div><h2>{{ store.activeConversation?.title ?? '全局助手' }}</h2></div>
      <div class="workspace-heading-actions">
        <span v-if="store.streamState !== 'idle'" class="stream-status" :class="`stream-${store.streamState}`"><i></i>{{ store.streamState === 'connected' ? '进行中' : '恢复连接' }}</span>
        <button class="workspace-icon-button" title="对话历史" aria-label="对话历史" @click="emit('toggleHistory')">◷</button>
        <button class="workspace-icon-button" title="新建对话" aria-label="新建对话" @click="store.newConversation">＋</button>
      </div>
    </div>

    <div class="assistant-context-bar"><span class="context-pin">⌖</span><span class="context-title">{{ pageHint }}</span><span v-if="context.pageContext.selection" class="selection-badge">含选区</span><button class="context-scope-button" @click="showScope = !showScope">{{ store.scopeMode === 'library' ? '全库检索' : `已选 ${store.selectedDocumentIds.length} 篇` }}⌄</button></div>
    <Transition name="workspace-panel">
    <div v-if="showScope" class="scope-popover">
      <div class="scope-popover-tabs"><button :class="{ active: store.scopeMode === 'library' }" @click="store.scopeMode = 'library'">全库</button><button :class="{ active: store.scopeMode === 'selected' }" @click="store.scopeMode = 'selected'">指定文献</button></div>
      <p>{{ store.scopeMode === 'library' ? '助手可在整座文献库中检索。' : '请选择本轮可检索的文献。' }}</p>
      <label v-for="document in documents" v-show="store.scopeMode === 'selected'" :key="document.id" class="scope-document"><input type="checkbox" :checked="store.selectedDocumentIds.includes(document.id)" @change="setSelected(document, ($event.target as HTMLInputElement).checked)" /><span>{{ document.title }}</span></label>
      <button class="scope-done" @click="showScope = false">完成</button>
    </div>
    </Transition>

    <div class="assistant-message-scroll" aria-live="polite">
      <div v-if="!store.orderedMessages.length" class="assistant-welcome">
        <span class="assistant-sun"><SvgIcon name="assistant" :size="27" /></span><h3>从眼前的问题开始</h3><p>助手会带上当前文献、位置、选区和笔记。你也可以随时检索整座文献库。</p>
        <div class="suggestion-row"><button @click="store.setDraft('概括这篇文献的核心论点，并指出证据位置。')">梳理论点 <span>↗</span></button><button @click="store.setDraft('帮我核查当前选区的论据与上下文。')">核查选区 <span>↗</span></button></div>
      </div>
      <article v-for="message in store.orderedMessages" :key="message.id" class="assistant-message" :class="`message-${message.role}`">
        <div class="message-avatar">{{ message.role === 'user' ? '我' : '研' }}</div>
        <div class="message-body"><div class="message-meta"><strong>{{ message.role === 'user' ? '你' : '研读助手' }}</strong><time>{{ messageTime(message.created_at) }}</time></div><div class="message-content">{{ message.content }}</div>
          <div v-if="Array.isArray(message.metadata.citations) && message.metadata.citations.length" class="message-citations">
            <span class="citation-label">依据</span><button v-for="(cite, index) in (message.metadata.citations as Array<Record<string, unknown>>).slice(0, 5)" :key="index" @click="openCitation(cite)">{{ cite.location_label || (cite.locator && typeof cite.locator === 'object' && 'page' in cite.locator ? `第 ${(cite.locator as Record<string, unknown>).page} 页` : '查看依据') }} <span>↗</span></button>
          </div>
        </div>
      </article>
      <div v-if="store.activeRun" class="assistant-thinking"><span class="thinking-mark">✳</span><span>{{ store.stoppingRunId === store.activeRun.id ? '正在停止本轮，等待服务端确认' : store.activeRun.status === 'queued' ? '正在排队' : '正在阅读文献并整理依据' }}</span><i></i><i></i><i></i><button v-if="store.stoppingRunId !== store.activeRun.id" class="stop-run-button" @click="() => store.stopActiveRun()">停止</button></div>
      <div v-else-if="store.runs[0]?.status === 'cancelled'" class="assistant-run-stopped">本轮已停止，已生成的内容仍保留在对话记录中。</div>
      <div v-for="entry in store.pendingActions" v-show="['show_note_candidate','show_document_choices'].includes(entry.action.type) || store.claimedActions.includes(entry.action.action_id)" :key="entry.action.action_id" class="assistant-action-card" :class="{ 'choices-card': entry.action.type === 'show_document_choices' }">
        <span class="action-card-mark">↗</span><div class="action-card-copy">
          <strong>{{ entry.action.type === 'show_note_candidate' ? entry.action.candidate_title || '笔记修改建议' : entry.action.type === 'show_document_choices' ? '选择要打开的文献' : '文献页面操作' }}</strong>
          <p v-if="entry.action.type === 'show_note_candidate'">{{ entry.action.plain_text?.slice(0, 180) || '查看修改前后的差异，再决定是否采纳。' }}</p>
          <template v-if="entry.action.type === 'show_document_choices'"><p>{{ entry.action.query || '找到多篇可能匹配的文献' }}</p><button v-for="candidate in entry.action.candidates" :key="`${candidate.document_id}:${candidate.version_id}`" class="document-choice" @click="chooseDocument(entry, candidate)"><span><strong>{{ candidate.title }}</strong><small>{{ candidate.location_label || '打开匹配位置' }}</small></span><b>↗</b></button></template>
          <p v-if="store.claimedActions.includes(entry.action.action_id)" class="action-ack-pending">页面已处理，确认结果尚未同步。</p>
        </div>
        <button v-if="entry.action.type !== 'show_document_choices'" class="workspace-primary" :disabled="entry.action.type === 'show_note_candidate' && notes.currentVersionId === entry.action.candidate_id" @click="entry.action.type === 'show_note_candidate' ? openAction(entry.action) : store.retryActionAck(entry.action.action_id)">{{ entry.action.type === 'show_note_candidate' && notes.currentVersionId === entry.action.candidate_id ? '已采纳' : store.claimedActions.includes(entry.action.action_id) ? '重试确认' : '查看' }}</button>
      </div>
      <div v-if="store.error" class="assistant-error">{{ store.error }} <button @click="() => store.refreshMessages()">重试</button></div>
    </div>

    <div v-if="store.noteSyncNotice" class="note-sync-notice">{{ store.noteSyncNotice }}</div>
    <div class="assistant-composer">
      <textarea :value="store.draft" :disabled="Boolean(store.activeConversation?.archived_at)" rows="3" maxlength="12000" :placeholder="store.activeConversation?.archived_at ? '已归档的对话仅供查看' : '提出问题，或让助手核查当前选区…'" aria-label="向研读助手提问" @input="store.setDraft(($event.target as HTMLTextAreaElement).value)" @keydown.ctrl.enter.prevent="store.send" @keydown.meta.enter.prevent="store.send"></textarea>
      <div class="composer-footer"><span>当前状态会随本轮固定发送</span><span class="composer-shortcut">⌘ / Ctrl ↵</span><button class="assistant-send-button" :disabled="!store.draft.trim() || store.sending || Boolean(store.activeRun) || (store.scopeMode === 'selected' && !store.selectedDocumentIds.length) || Boolean(store.activeConversation?.archived_at)" :aria-label="store.activeConversation?.archived_at ? '归档对话只能查看' : '发送问题'" @click="store.send">{{ store.sending ? '发送中' : '↑' }}</button></div>
    </div>

    <Transition name="workspace-panel">
    <div v-if="chrome.windows.assistant.history" class="assistant-history-overlay">
      <div class="assistant-history-head"><div><h3>对话历史</h3></div><div><button class="workspace-quiet-button" @click="store.newConversation">＋ 新建</button><button class="workspace-icon-button" aria-label="关闭历史" @click="chrome.windows.assistant.history = false">×</button></div></div>
      <div class="history-section-label">最近对话</div>
      <button v-for="conversation in store.conversations" :key="conversation.id" class="assistant-history-row" :class="{ selected: conversation.id === store.activeConversationId }" @click="store.loadConversation(conversation.id); chrome.windows.assistant.history = false">
        <span><strong>{{ conversation.title }}</strong><small>{{ new Date(conversation.updated_at).toLocaleString('zh-CN') }}</small></span><span class="history-row-actions"><i v-if="conversation.id === store.activeConversationId" @click.stop="promptRename(conversation.id, conversation.title)">改名</i><i v-if="conversation.id === store.activeConversationId" @click.stop="store.archive(conversation.id)">归档</i></span>
      </button>
      <div v-if="store.archivedConversations.length" class="history-section-label archived-label">已归档</div>
      <button v-for="conversation in store.archivedConversations" :key="conversation.id" class="assistant-history-row archived-row" :class="{ selected: conversation.id === store.activeConversationId }" @click="store.loadConversation(conversation.id); chrome.windows.assistant.history = false">
        <span><strong>{{ conversation.title }}</strong><small>{{ new Date(conversation.updated_at).toLocaleString('zh-CN') }}</small></span><span class="history-row-actions"><i>只读</i></span>
      </button>
      <p v-if="!store.conversations.length && !store.archivedConversations.length" class="workspace-history-empty">还没有对话记录。</p>
    </div>
    </Transition>
  </section>
</template>
