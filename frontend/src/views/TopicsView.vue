<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  archiveTopic,
  createTopic,
  linkTopicDocument,
  listDocuments,
  listTopicDocuments,
  listTopics,
  restoreTopic,
  unlinkTopicDocument,
  updateTopic,
  type DocumentSummary,
  type TopicDocumentItem,
  type TopicSummary,
} from '../api/workbench'

const allTopics = ref<TopicSummary[]>([])
const documents = ref<DocumentSummary[]>([])
const linkedDocuments = ref<TopicDocumentItem[]>([])
const loading = ref(true)
const saving = ref(false)
const documentAction = ref(false)
const errorMessage = ref('')
const showArchived = ref(false)
const openTopicId = ref('')
const selectedDocumentId = ref('')
const editorOpen = ref(false)
const editingTopicId = ref('')
const title = ref('')
const goal = ref('')

const activeTopics = computed(() => allTopics.value.filter((topic) => !topic.archived_at))
const archivedTopics = computed(() => allTopics.value.filter((topic) => topic.archived_at))
const topicDocuments = computed(() => {
  const linked = new Set(linkedDocuments.value.map((item) => item.document.id))
  return documents.value.filter((document) => !linked.has(document.id))
})

async function loadTopics() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [topics, library] = await Promise.all([listTopics(true), listDocuments()])
    allTopics.value = topics
    documents.value = library
    if (openTopicId.value && !topics.some((topic) => topic.id === openTopicId.value && !topic.archived_at)) {
      openTopicId.value = ''
      linkedDocuments.value = []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法读取专题。'
  } finally {
    loading.value = false
  }
}

function openEditor(topic?: TopicSummary) {
  editingTopicId.value = topic?.id ?? ''
  title.value = topic?.title ?? ''
  goal.value = topic?.goal ?? ''
  editorOpen.value = true
  errorMessage.value = ''
}

function closeEditor() {
  editorOpen.value = false
  editingTopicId.value = ''
  title.value = ''
  goal.value = ''
}

async function saveTopic() {
  if (!title.value.trim() || saving.value) return
  saving.value = true
  errorMessage.value = ''
  try {
    const payload = { title: title.value.trim(), goal: goal.value.trim() }
    if (editingTopicId.value) await updateTopic(editingTopicId.value, payload)
    else await createTopic(payload)
    closeEditor()
    await loadTopics()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法保存专题。'
  } finally {
    saving.value = false
  }
}

async function toggleDocuments(topicId: string) {
  if (openTopicId.value === topicId) {
    openTopicId.value = ''
    linkedDocuments.value = []
    return
  }
  openTopicId.value = topicId
  selectedDocumentId.value = ''
  try {
    linkedDocuments.value = await listTopicDocuments(topicId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法读取关联文献。'
  }
}

async function addDocument(topicId: string) {
  if (!selectedDocumentId.value || documentAction.value) return
  documentAction.value = true
  errorMessage.value = ''
  try {
    await linkTopicDocument(topicId, selectedDocumentId.value)
    linkedDocuments.value = await listTopicDocuments(topicId)
    selectedDocumentId.value = ''
    await loadTopics()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法关联文献。'
  } finally {
    documentAction.value = false
  }
}

async function removeDocument(topicId: string, documentId: string) {
  if (documentAction.value) return
  documentAction.value = true
  errorMessage.value = ''
  try {
    await unlinkTopicDocument(topicId, documentId)
    linkedDocuments.value = await listTopicDocuments(topicId)
    await loadTopics()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法移除文献关联。'
  } finally {
    documentAction.value = false
  }
}

async function archive(topic: TopicSummary) {
  if (!window.confirm(`归档“${topic.title}”？文献原件不会被删除。`)) return
  errorMessage.value = ''
  try {
    await archiveTopic(topic.id)
    await loadTopics()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法归档专题。'
  }
}

async function restore(topic: TopicSummary) {
  errorMessage.value = ''
  try {
    await restoreTopic(topic.id)
    await loadTopics()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂时无法恢复专题。'
  }
}

function documentStatus(status: string) {
  return ({ queued: '等待整理', processing: '正在整理', ready: '可阅读', failed: '需要重试' } as Record<string, string>)[status] ?? status
}

onMounted(loadTopics)
</script>

<template>
  <div class="page-content topics-page">
    <section class="topics-heading">
      <div>
        <div class="eyebrow"><span class="eyebrow-line"></span> RESEARCH</div>
        <h1>专题研究</h1>
        <p class="welcome-description">围绕一个研究目标，组织相关文献与阅读线索。</p>
      </div>
      <button class="topic-primary-button" @click="openEditor()"><span aria-hidden="true">＋</span> 新建专题</button>
    </section>

    <p v-if="errorMessage" class="topic-feedback" role="alert">{{ errorMessage }}</p>

    <section v-if="editorOpen" class="panel topic-editor" aria-labelledby="topic-editor-heading">
      <div class="topic-editor-heading">
        <div><div class="section-kicker">RESEARCH SPACE</div><h2 id="topic-editor-heading">{{ editingTopicId ? '编辑专题' : '创建专题' }}</h2></div>
        <button class="topic-text-button" type="button" @click="closeEditor">关闭</button>
      </div>
      <form @submit.prevent="saveTopic">
        <label for="topic-title">专题名称</label>
        <input id="topic-title" v-model="title" maxlength="240" required placeholder="例如：多模态模型的评估方法" />
        <label for="topic-goal">研究目标 <span>选填</span></label>
        <textarea id="topic-goal" v-model="goal" maxlength="10000" rows="3" placeholder="记录想回答的问题或关注的研究方向" />
        <div class="topic-form-actions">
          <button class="topic-secondary-button" type="button" @click="closeEditor">取消</button>
          <button class="topic-primary-button" type="submit" :disabled="saving || !title.trim()">{{ saving ? '正在保存…' : '保存专题' }}</button>
        </div>
      </form>
    </section>

    <div v-if="loading" class="panel topics-loading"><span class="topic-spinner"></span><span>正在读取专题…</span></div>
    <section v-else-if="activeTopics.length" class="topic-grid" aria-label="专题列表">
      <article v-for="topic in activeTopics" :key="topic.id" class="panel topic-card">
        <div class="topic-card-topline"><span class="topic-card-mark" aria-hidden="true">研</span><span class="topic-card-date">{{ new Date(topic.updated_at).toLocaleDateString('zh-CN') }}</span></div>
        <h2>{{ topic.title }}</h2>
        <p class="topic-goal">{{ topic.goal || '围绕专题目标积累阅读线索与研究想法。' }}</p>
        <div class="topic-card-stats"><span><b>{{ topic.document_count }}</b> 篇文献</span><span><b>{{ topic.conversation_count }}</b> 个会话</span></div>
        <div class="topic-card-actions">
          <RouterLink class="topic-secondary-button topic-study-link" :to="`/topics/${encodeURIComponent(topic.id)}/study`">开始研读 <span aria-hidden="true">→</span></RouterLink>
          <button class="topic-secondary-button" type="button" @click="toggleDocuments(topic.id)">{{ openTopicId === topic.id ? '收起文献' : '管理文献' }}</button>
          <button class="topic-text-button" type="button" @click="openEditor(topic)">编辑</button>
          <button class="topic-text-button topic-archive-button" type="button" @click="archive(topic)">归档</button>
        </div>
        <div v-if="openTopicId === topic.id" class="topic-documents-panel">
          <div class="topic-documents-title"><strong>关联文献</strong><span>{{ linkedDocuments.length }} 篇</span></div>
          <div v-if="linkedDocuments.length" class="topic-document-list">
            <div v-for="item in linkedDocuments" :key="item.document.id" class="topic-document-row">
              <span class="topic-document-icon" aria-hidden="true">▤</span>
              <span class="topic-document-copy"><strong>{{ item.document.title }}</strong><small>{{ documentStatus(item.document.status) }}</small></span>
              <button class="topic-text-button" type="button" :disabled="documentAction" :aria-label="`移除 ${item.document.title}`" @click="removeDocument(topic.id, item.document.id)">移除</button>
            </div>
          </div>
          <p v-else class="topic-document-empty">从文献库添加相关论文。</p>
          <form class="topic-add-document" @submit.prevent="addDocument(topic.id)">
            <select v-model="selectedDocumentId" aria-label="选择文献">
              <option value="">选择文献</option>
              <option v-for="document in topicDocuments" :key="document.id" :value="document.id">{{ document.title }}</option>
            </select>
            <button class="topic-secondary-button" type="submit" :disabled="!selectedDocumentId || documentAction">添加</button>
          </form>
          <p v-if="!documents.length" class="topic-document-hint">文献库还是空的，可以先导入 PDF。</p>
        </div>
      </article>
    </section>
    <section v-else-if="!editorOpen" class="panel topics-empty">
      <div class="empty-illustration"><span class="empty-illustration-ring"></span><span class="empty-illustration-mark">⌕</span></div>
      <h2>从一个研究问题开始</h2>
      <p>创建专题，把相关文献与阅读线索放在一起。</p>
      <button class="topic-primary-button" type="button" @click="openEditor()">创建第一个专题</button>
    </section>

    <section class="topic-archive-section">
      <button class="topic-archive-toggle" type="button" :aria-expanded="showArchived" @click="showArchived = !showArchived">
        <span>{{ showArchived ? '收起已归档专题' : '查看已归档专题' }}</span><span>{{ archivedTopics.length }}</span><i :class="{ 'is-open': showArchived }" aria-hidden="true">⌄</i>
      </button>
      <div v-if="showArchived && archivedTopics.length" class="topic-archived-list">
        <article v-for="topic in archivedTopics" :key="topic.id" class="topic-archived-row">
          <div><strong>{{ topic.title }}</strong><p>{{ topic.goal || '没有填写研究目标' }}</p></div>
          <button class="topic-secondary-button" type="button" @click="restore(topic)">恢复专题</button>
        </article>
      </div>
      <p v-else-if="showArchived" class="topic-archive-empty">没有已归档的专题。</p>
    </section>
  </div>
</template>

<style scoped>
.topics-page { padding: 42px 0 28px; }
.topics-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 26px; }
.topics-heading h1 { margin: 15px 0 7px; color: #28362f; font: 500 28px/1.4 'Noto Serif SC', serif; letter-spacing: -.03em; }
.topic-primary-button, .topic-secondary-button { min-height: 36px; display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 0 13px; border: 1px solid #416c5a; border-radius: 4px; color: #fffefa; background: #416c5a; font-size: 10px; cursor: pointer; transition: background .18s ease, border-color .18s ease, transform .18s ease; }
.topic-primary-button:hover { background: #315646; border-color: #315646; }
.topic-primary-button:active, .topic-secondary-button:active { transform: translateY(1px); }
.topic-primary-button:disabled, .topic-secondary-button:disabled { opacity: .55; cursor: default; }
.topic-primary-button span { font-size: 15px; line-height: 1; }
.topic-secondary-button { min-height: 31px; border-color: #dfe5dc; color: #536b59; background: #f8faf6; }
.topic-secondary-button:hover { background: #eef3ed; border-color: #cad7cb; }
.topic-text-button { padding: 5px 7px; border: 0; color: #698272; background: transparent; font-size: 10px; cursor: pointer; }
.topic-text-button:hover { color: #345e4c; text-decoration: underline; }
.topic-text-button:disabled { opacity: .55; cursor: default; }
.topic-feedback { margin: -9px 0 17px; padding: 9px 11px; border-radius: 4px; color: #8d5d4a; background: #f8eee9; font-size: 10px; line-height: 1.65; }
.topic-editor { max-width: 680px; margin: 0 0 18px; padding: 20px; }
.topic-editor-heading { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 18px; }
.topic-editor-heading h2 { margin: 5px 0 0; color: #344038; font: 500 17px 'Noto Serif SC', serif; }
.topic-editor form { display: grid; gap: 8px; }
.topic-editor label { margin: 4px 0 1px; color: #4c5b50; font-size: 10px; font-weight: 600; }
.topic-editor label span { color: #9ca399; font-weight: 400; }
.topic-editor input, .topic-editor textarea, .topic-add-document select { width: 100%; padding: 10px 11px; border: 1px solid #e3e7de; border-radius: 4px; outline: none; color: #435247; background: #fbfcf8; font: 11px/1.7 'Noto Sans SC', sans-serif; }
.topic-editor input:focus, .topic-editor textarea:focus, .topic-add-document select:focus { border-color: #96ad98; box-shadow: 0 0 0 3px #78977b18; }
.topic-editor textarea { resize: vertical; }
.topic-form-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
.topic-form-actions .topic-secondary-button { min-height: 36px; }
.topics-loading { min-height: 180px; display: flex; align-items: center; justify-content: center; gap: 10px; color: #869287; font-size: 11px; }
.topic-spinner { width: 17px; height: 17px; border: 2px solid #e0e8df; border-top-color: #5f8069; border-radius: 50%; animation: topic-spin .8s linear infinite; }
.topic-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.topic-card { min-width: 0; padding: 19px 20px 16px; }
.topic-card-topline { display: flex; align-items: center; justify-content: space-between; }
.topic-card-mark { width: 31px; height: 31px; display: grid; place-items: center; border-radius: 9px 9px 9px 3px; color: #54715e; background: #eaf0e8; font: 12px 'Noto Serif SC', serif; }
.topic-card-date { color: #a1a79e; font-size: 9px; }
.topic-card h2 { margin: 15px 0 7px; color: #344038; font: 500 16px/1.5 'Noto Serif SC', serif; overflow-wrap: anywhere; }
.topic-goal { min-height: 37px; margin: 0; color: #92998f; font-size: 10px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.topic-card-stats { display: flex; gap: 18px; margin-top: 15px; padding: 11px 0; border-top: 1px solid #eef0e9; color: #92998f; font-size: 9px; }
.topic-card-stats b { margin-right: 3px; color: #536c5a; font: 600 12px 'DM Mono', monospace; }
.topic-card-actions { display: flex; align-items: center; gap: 2px; }
.topic-card-actions .topic-secondary-button { margin-right: auto; }
.topic-archive-button { color: #9a8278; }
.topic-documents-panel { margin: 15px -20px -16px; padding: 14px 19px 16px; border-top: 1px solid #eaede6; background: #fbfcf8; }
.topic-documents-title { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; color: #526156; font-size: 10px; }
.topic-documents-title span { color: #a0a69d; font-size: 9px; }
.topic-document-list { display: flex; flex-direction: column; gap: 3px; }
.topic-document-row { min-width: 0; display: flex; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 1px solid #f0f1ec; }
.topic-document-icon { width: 25px; height: 25px; display: grid; flex: 0 0 auto; place-items: center; border-radius: 5px; color: #748777; background: #edf1e9; font-size: 12px; }
.topic-document-copy { min-width: 0; display: flex; flex: 1; flex-direction: column; gap: 3px; }
.topic-document-copy strong { overflow: hidden; color: #58645b; font-size: 9px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.topic-document-copy small { color: #a0a69d; font-size: 8px; }
.topic-document-empty, .topic-document-hint { margin: 6px 0 10px; color: #a0a69d; font-size: 9px; line-height: 1.6; }
.topic-document-hint { margin: 8px 0 0; }
.topic-add-document { display: flex; align-items: center; gap: 7px; margin-top: 10px; }
.topic-add-document select { min-width: 0; min-height: 31px; flex: 1; padding: 5px 8px; font-size: 9px; }
.topic-add-document .topic-secondary-button { flex: 0 0 auto; }
.topics-empty { min-height: 360px; display: flex; align-items: center; justify-content: center; flex-direction: column; text-align: center; }
.topics-empty h2 { margin: 0 0 8px; color: #3c4940; font: 500 17px 'Noto Serif SC', serif; }
.topics-empty p { margin: 0 0 18px; color: #92998f; font-size: 10px; }
.topics-empty .empty-illustration { margin-bottom: 17px; }
.topic-archive-section { margin-top: 24px; border-top: 1px solid #e8eae3; }
.topic-archive-toggle { width: 100%; display: flex; align-items: center; gap: 8px; padding: 15px 2px; border: 0; color: #7e897f; background: transparent; text-align: left; font-size: 10px; cursor: pointer; }
.topic-archive-toggle > span:nth-child(2) { padding: 2px 6px; border-radius: 10px; color: #758275; background: #e9eee7; font: 9px 'DM Mono', monospace; }
.topic-archive-toggle i { margin-left: auto; color: #a0a69d; font-style: normal; font-size: 15px; transition: transform .18s ease; }
.topic-archive-toggle i.is-open { transform: rotate(180deg); }
.topic-archived-list { display: flex; flex-direction: column; border-top: 1px solid #ebece6; }
.topic-archived-row { display: flex; align-items: center; justify-content: space-between; gap: 15px; padding: 13px 5px; border-bottom: 1px solid #ebece6; }
.topic-archived-row strong { color: #667269; font: 500 11px 'Noto Serif SC', serif; }
.topic-archived-row p { margin: 4px 0 0; color: #a0a69d; font-size: 9px; }
.topic-archive-empty { padding: 2px; color: #a0a69d; font-size: 9px; }
@keyframes topic-spin { to { transform: rotate(360deg); } }
@media (max-width: 760px) { .topic-grid { grid-template-columns: 1fr; } }
@media (max-width: 650px) { .topics-page { padding: 30px 0 24px; }.topics-heading { align-items: flex-start; margin-bottom: 21px; }.topics-heading h1 { font-size: 23px; }.topics-heading > .topic-primary-button { margin-top: 18px; }.topic-card { padding: 17px 15px 14px; }.topic-documents-panel { margin: 14px -15px -14px; padding: 13px 14px 14px; }.topics-empty { min-height: 310px; }.topic-archived-row { align-items: flex-start; } }
@media (prefers-reduced-motion: reduce) { .topic-spinner { animation: none; }.topic-primary-button, .topic-secondary-button, .topic-archive-toggle i { transition: none; } }
</style>
