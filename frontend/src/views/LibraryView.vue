<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import SvgIcon from '../components/SvgIcon.vue'
import { updateMetadata } from '../api/reader'
import { activateDocumentVersion, deleteDocument, getTask, listDocumentVersions, listDocuments, rebuildIndex, replaceDocumentVersion, retryDocumentVersion, uploadDocuments, type DocumentSummary, type DocumentUploadOutcome, type DocumentVersionSummary } from '../api/workbench'

const documents = ref<DocumentSummary[]>([])
const loading = ref(true)
const error = ref('')
const uploading = ref(false)
const uploadError = ref('')
const uploadResults = ref<DocumentUploadOutcome[]>([])
const versionsByDocument = ref<Record<string, DocumentVersionSummary[]>>({})
const versionLoading = ref<Record<string, boolean>>({})
const versionBusy = ref<Record<string, boolean>>({})
const versionMessage = ref<Record<string, string>>({})
const indexTaskId = ref('')
const indexUpdating = ref(false)
const indexMessage = ref('')
const fileInput = ref<HTMLInputElement>()
const replaceInputs = ref<Record<string, HTMLInputElement>>({})
const dragging = ref(false)
const query = ref(''), formatFilter = ref(''), readFilter = ref(''), favoriteOnly = ref(false), sort = ref('newest'), editingId = ref(''), metadataError = ref('')
const metadataDraft = ref({ title: '', authors: '', year: '', doi: '', tags: '', reading_status: 'unread' })
const formats = computed(() => [...new Set(documents.value.map(item => item.file_format || 'pdf'))].sort())
const visibleDocuments = computed(() => {
  const value = query.value.toLocaleLowerCase().trim()
  return documents.value.filter(item => (!value || [item.title, item.filename, ...(item.authors || []), ...(item.tags || []), item.doi || '', item.year || ''].join(' ').toLocaleLowerCase().includes(value)) && (!formatFilter.value || item.file_format === formatFilter.value) && (!readFilter.value || (item.reading_status || 'unread') === readFilter.value) && (!favoriteOnly.value || item.favorite)).sort((a, b) => sort.value === 'title' ? a.title.localeCompare(b.title, 'zh') : sort.value === 'year' ? (b.year || 0) - (a.year || 0) : b.created_at.localeCompare(a.created_at))
})
function editMetadata(item: DocumentSummary) { editingId.value = item.id; metadataError.value = ''; metadataDraft.value = { title: item.title, authors: (item.authors || []).join('；'), year: item.year ? String(item.year) : '', doi: item.doi || '', tags: (item.tags || []).join('，'), reading_status: item.reading_status || 'unread' } }
async function saveMetadata() {
  const value = metadataDraft.value
  try {
    const result = await updateMetadata<DocumentSummary>(editingId.value, { title: value.title, authors: value.authors.split(/[；;、]/).map(v => v.trim()).filter(Boolean), year: value.year ? Number(value.year) : null, doi: value.doi || null, tags: value.tags.split(/[,，]/).map(v => v.trim()).filter(Boolean), reading_status: value.reading_status })
    documents.value = documents.value.map(item => item.id === result.id ? result : item); editingId.value = ''
  } catch (cause) { metadataError.value = cause instanceof Error ? cause.message : '修改未保存，请重试。' }
}
async function favorite(item: DocumentSummary) {
  try { const result = await updateMetadata<DocumentSummary>(item.id, { favorite: !item.favorite }); documents.value = documents.value.map(doc => doc.id === item.id ? result : doc) } catch (cause) { uploadError.value = String(cause) }
}
let refreshTimer: ReturnType<typeof setInterval> | undefined

const readyCount = computed(() => documents.value.filter((item) => item.status === 'ready' || item.status === 'completed').length)
const processingCount = computed(() => documents.value.filter((item) => ['queued', 'pending', 'running', 'processing'].includes(item.status)).length)

function statusLabel(status: string): string {
  const labels: Record<string, string> = { ready: '可阅读', completed: '可阅读', queued: '等待处理', pending: '等待处理', running: '正在整理', processing: '正在整理', failed: '处理失败', error: '处理失败' }
  return labels[status] ?? '已添加'
}

function statusClass(status: string): string {
  if (['ready', 'completed'].includes(status)) return 'is-ready'
  if (['failed', 'error'].includes(status)) return 'is-failed'
  if (['queued', 'pending', 'running', 'processing'].includes(status)) return 'is-processing'
  return 'is-neutral'
}

function uploadResultState(result: DocumentUploadOutcome): string {
  if (!result.document) return result.status
  return documents.value.find((item) => item.id === result.document?.id)?.status ?? result.document.status
}

function uploadResultMark(result: DocumentUploadOutcome): string {
  const state = uploadResultState(result)
  if (result.status === 'duplicate') return '↻'
  if (state === 'failed' || result.status === 'rejected') return '!'
  if (state === 'processing') return '…'
  return '✓'
}

function uploadResultMessage(result: DocumentUploadOutcome): string {
  if (result.status === 'rejected') return result.error_message || '无法添加此文件'
  const state = uploadResultState(result)
  if (result.status === 'duplicate') {
    if (state === 'failed') return '已存在相同文献，当前整理未完成；可在文献库中重试'
    if (state === 'processing') return '已存在相同文献，正在整理'
    if (state === 'queued') return '已存在相同文献，等待整理'
    return '已存在相同文件，复用已整理文献'
  }
  if (state === 'ready') return '整理完成，可阅读'
  if (state === 'processing') return '正在整理阅读材料'
  if (state === 'failed') return '处理未完成，可在版本管理中重试'
  return '已加入文献库，等待整理'
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date)
}

async function loadDocuments(showLoading = false) {
  if (showLoading) loading.value = true
  error.value = ''
  try {
    documents.value = await listDocuments()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '暂时无法载入文献，请稍后重试。'
  } finally {
    loading.value = false
  }
}

async function refreshIndexTask() {
  if (!indexTaskId.value) return
  try {
    const task = await getTask(indexTaskId.value)
    if (task.status === 'queued' || task.status === 'running') {
      indexUpdating.value = true
      indexMessage.value = '正在更新文献检索，完成后即可使用最新内容。'
      return
    }
    indexUpdating.value = false
    indexMessage.value = task.status === 'succeeded'
      ? '文献检索已更新。'
      : '本次更新未完成，现有检索仍可使用；你可以稍后重试。'
    indexTaskId.value = ''
  } catch {
    indexUpdating.value = false
    indexMessage.value = '暂时无法读取更新状态，请稍后重试。'
    indexTaskId.value = ''
  }
}

async function updateIndex() {
  indexUpdating.value = true
  indexMessage.value = '正在准备更新文献检索…'
  try {
    const task = await rebuildIndex()
    indexTaskId.value = task.id
    await refreshIndexTask()
  } catch (cause) {
    indexUpdating.value = false
    indexMessage.value = cause instanceof Error ? cause.message : '暂时无法更新文献检索，请稍后重试。'
  }
}

function selectFile() { fileInput.value?.click() }

function setReplaceInput(documentId: string, element: Element | null) {
  if (element instanceof HTMLInputElement) replaceInputs.value[documentId] = element
  else delete replaceInputs.value[documentId]
}

async function loadVersions(documentId: string) {
  versionLoading.value[documentId] = true
  versionMessage.value[documentId] = ''
  try {
    versionsByDocument.value[documentId] = await listDocumentVersions(documentId)
  } catch (cause) {
    versionMessage.value[documentId] = cause instanceof Error ? cause.message : '暂时无法载入版本。'
  } finally {
    versionLoading.value[documentId] = false
  }
}

function onVersionDetailsToggle(documentId: string, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) void loadVersions(documentId)
}

async function activateVersion(documentId: string, versionId: string) {
  versionBusy.value[documentId] = true
  versionMessage.value[documentId] = ''
  try {
    const updated = await activateDocumentVersion(documentId, versionId)
    documents.value = documents.value.map((item) => item.id === documentId ? updated : item)
    versionMessage.value[documentId] = '已切换阅读版本。'
    await loadVersions(documentId)
  } catch (cause) {
    versionMessage.value[documentId] = cause instanceof Error ? cause.message : '切换没有完成，请重试。'
  } finally {
    versionBusy.value[documentId] = false
  }
}

async function retryVersion(documentId: string, versionId: string) {
  versionBusy.value[documentId] = true
  versionMessage.value[documentId] = ''
  try {
    await retryDocumentVersion(documentId, versionId)
    versionMessage.value[documentId] = '已重新加入整理队列。'
    await Promise.all([loadDocuments(), loadVersions(documentId)])
  } catch (cause) {
    versionMessage.value[documentId] = cause instanceof Error ? cause.message : '重试没有加入队列，请稍后再试。'
  } finally {
    versionBusy.value[documentId] = false
  }
}

async function replaceVersion(documentId: string, event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  versionBusy.value[documentId] = true
  versionMessage.value[documentId] = ''
  try {
    await replaceDocumentVersion(documentId, file)
    versionMessage.value[documentId] = '新版本已加入，原阅读版本会继续保留。'
    await Promise.all([loadDocuments(), loadVersions(documentId)])
  } catch (cause) {
    versionMessage.value[documentId] = cause instanceof Error ? cause.message : '新版本没有上传成功，请重试。'
  } finally {
    versionBusy.value[documentId] = false
  }
}

async function removeDocument(documentId: string, title: string) {
  if (!window.confirm(`从文献库移除“${title}”？`)) return
  versionBusy.value[documentId] = true
  versionMessage.value[documentId] = ''
  try {
    await deleteDocument(documentId)
    documents.value = documents.value.filter((item) => item.id !== documentId)
  } catch (cause) {
    versionMessage.value[documentId] = cause instanceof Error ? cause.message : '移除没有完成，请重试。'
  } finally {
    versionBusy.value[documentId] = false
  }
}

function versionStatusLabel(status: string) {
  return status === 'ready' ? '可阅读' : status === 'failed' ? '未完成' : ['queued', 'processing'].includes(status) ? '整理中' : '已添加'
}

async function acceptFiles(fileList?: FileList | File[] | null) {
  const files = fileList ? Array.from(fileList) : []
  if (!files.length) return
  uploadError.value = ''
  uploadResults.value = []
  if (files.length > 30) {
    uploadError.value = '一次最多添加 30 篇论文，请分批选择。'
    return
  }
  uploading.value = true
  try {
    uploadResults.value = await uploadDocuments(files)
    const accepted = [...new Map(uploadResults.value.flatMap((item) => item.document ? [[item.document.id, item.document] as const] : [])).values()]
    documents.value = [...accepted, ...documents.value.filter((item) => !accepted.some((added) => added.id === item.id))]
    await loadDocuments()
  } catch (cause) {
    uploadError.value = cause instanceof Error ? cause.message : '上传没有完成，请重试。'
  } finally {
    uploading.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

function onDrop(event: DragEvent) {
  dragging.value = false
  void acceptFiles(event.dataTransfer?.files)
}

onMounted(() => {
  void loadDocuments(true)
  refreshTimer = setInterval(() => {
    if (processingCount.value > 0) void loadDocuments()
    if (indexUpdating.value) void refreshIndexTask()
  }, 5000)
})
onUnmounted(() => { if (refreshTimer) clearInterval(refreshTimer) })
</script>

<template>
  <div class="page-content library-page">
    <div class="library-heading">
      <div>
        <h1>把重要研究，收进同一座书架</h1>
        <p>收录文献，管理阅读进度，随时回到原文。</p>
      </div>
      <div class="library-heading-actions">
        <button class="library-secondary" type="button" :disabled="indexUpdating || documents.length === 0" @click="updateIndex">{{ indexUpdating ? '正在更新…' : '更新文献检索' }}</button>
        <button class="library-primary" type="button" :disabled="uploading" @click="selectFile">
          <SvgIcon name="plus" :size="17" />{{ uploading ? '正在上传…' : '添加文献' }}
        </button>
      </div>
      <input ref="fileInput" class="library-visually-hidden" type="file" aria-label="选择要添加的文献" multiple accept=".pdf,.epub,.docx,.md,.markdown,.txt" @change="acceptFiles(($event.target as HTMLInputElement).files)">
    </div>
    <p v-if="indexMessage" class="index-message" :class="{ 'is-error': indexMessage.includes('未完成') || indexMessage.includes('无法') }" role="status" aria-live="polite">{{ indexMessage }}</p>

    <div class="library-summary" aria-label="文献概况">
      <div><strong>{{ documents.length }}</strong><span>篇文献</span></div>
      <i></i><div><strong>{{ readyCount }}</strong><span>篇可阅读</span></div>
      <i></i><div><strong>{{ processingCount }}</strong><span>篇处理中</span></div>
    </div>

    <button class="library-dropzone" :class="{ 'is-dragging': dragging, 'is-uploading': uploading }" type="button" :disabled="uploading" @click="selectFile" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="onDrop">
      <span class="dropzone-icon"><SvgIcon name="upload" :size="21" /></span>
      <span class="dropzone-copy"><strong>{{ uploading ? '正在添加文献…' : '将文献拖到这里，或点击选择文件' }}</strong><small>PDF · EPUB · DOCX · Markdown · TXT ｜每批最多 30 篇，每篇不超过 100 MB</small></span>
      <span class="dropzone-action">选择文件 <SvgIcon name="arrow" :size="16" /></span>
    </button>
    <p v-if="uploadError" class="library-alert" role="alert">{{ uploadError }}</p>
    <div v-if="uploadResults.length" class="upload-results" role="status" aria-live="polite">
      <div v-for="(result, index) in uploadResults" :key="`${result.filename}-${index}`" class="upload-result" :class="`is-${uploadResultState(result)}`">
        <span class="upload-result-mark" aria-hidden="true">{{ uploadResultMark(result) }}</span>
        <span class="upload-result-copy"><strong>{{ result.filename }}</strong><small>{{ uploadResultMessage(result) }}</small></span>
      </div>
    </div>

    <section class="library-list-section">
      <div class="collection-filters"><input v-model="query" type="search" aria-label="搜索文献" placeholder="搜索标题、作者、标签或 DOI"><select v-model="formatFilter" aria-label="格式筛选"><option value="">全部格式</option><option v-for="format in formats" :key="format" :value="format">{{ format.toUpperCase() }}</option></select><select v-model="readFilter" aria-label="阅读状态筛选"><option value="">全部阅读状态</option><option value="unread">未读</option><option value="reading">在读</option><option value="read">已读</option></select><select v-model="sort" aria-label="排序"><option value="newest">最近添加</option><option value="title">按标题</option><option value="year">按年份</option></select><label><input v-model="favoriteOnly" type="checkbox">仅收藏</label></div>
      <div class="library-list-heading"><div><h2>全部文献</h2></div><span v-if="documents.length" class="library-list-count">{{ documents.length }} 篇</span></div>
      <div v-if="loading" class="library-state" role="status"><span class="library-spinner"></span><span>正在载入文献…</span></div>
      <div v-else-if="error" class="library-state library-state-error" role="alert"><span>{{ error }}</span><button type="button" @click="loadDocuments(true)">重新载入</button></div>
      <div v-else-if="documents.length === 0" class="library-empty"><span class="empty-book"><SvgIcon name="book" :size="22" /></span><h3>书架还空着</h3><p>从一篇与你的研究有关的论文开始，添加文献后即可在这里继续阅读和提问。</p><button type="button" class="library-text-button" @click="selectFile">选择一篇文献 <SvgIcon name="arrow" :size="15" /></button></div>
      <div v-else class="document-list">
        <p v-if="!visibleDocuments.length" class="filter-empty">没有符合条件的文献。</p>
        <article v-for="document in visibleDocuments" :key="document.id" class="document-row">
          <div class="document-cover" aria-hidden="true"><span>{{ (document.file_format || 'pdf').toUpperCase() }}</span><i></i></div>
          <div class="document-main"><RouterLink class="document-title" :to="`/documents/${encodeURIComponent(document.id)}`">{{ document.title || document.filename }}</RouterLink><span class="document-filename">{{ document.filename }}</span><span v-if="document.error_message" class="document-error">{{ document.error_message }}</span>
            <div class="bibliography"><span v-if="document.authors?.length">{{ document.authors.join(' · ') }}</span><span v-if="document.year">{{ document.year }}</span><span v-for="tag in document.tags || []" :key="tag" class="literature-tag">{{ tag }}</span></div>
            <div class="metadata-actions"><button @click="favorite(document)" :aria-label="document.favorite ? '取消收藏' : '收藏文献'">{{ document.favorite ? '★ 已收藏' : '☆ 收藏' }}</button><button @click="editMetadata(document)">编辑资料</button><span>{{ ({ unread: '未读', reading: '在读', read: '已读' } as Record<string,string>)[document.reading_status || 'unread'] }}</span></div>
            <form v-if="editingId === document.id" class="metadata-editor" @submit.prevent="saveMetadata"><label>标题<input v-model="metadataDraft.title" required maxlength="500"></label><label>作者<input v-model="metadataDraft.authors" placeholder="多位作者以分号分隔"></label><label>年份<input v-model="metadataDraft.year" type="number" min="1000" max="2200"></label><label>DOI<input v-model="metadataDraft.doi"></label><label>标签<input v-model="metadataDraft.tags" placeholder="多个标签以逗号分隔"></label><label>阅读状态<select v-model="metadataDraft.reading_status"><option value="unread">未读</option><option value="reading">在读</option><option value="read">已读</option></select></label><p v-if="metadataError" role="alert">{{ metadataError }}</p><div><button type="submit">保存</button><button type="button" @click="editingId = ''">取消</button></div></form>
            <details class="document-versions" @toggle="onVersionDetailsToggle(document.id, $event)">
              <summary>版本与管理</summary>
              <div class="version-panel">
                <p v-if="versionLoading[document.id]" class="version-note">正在载入版本…</p>
                <ul v-else-if="versionsByDocument[document.id]?.length" class="version-list">
                  <li v-for="version in versionsByDocument[document.id]" :key="version.id">
                    <span><strong>版本 {{ version.version_number }}</strong><small>{{ version.filename }} · {{ version.page_count || '—' }} 页 · {{ versionStatusLabel(version.status) }}</small></span>
                    <span v-if="version.is_active" class="version-active">当前阅读</span>
                    <button v-else-if="version.status === 'ready'" type="button" :disabled="versionBusy[document.id]" @click="activateVersion(document.id, version.id)">切换</button>
                    <button v-else-if="version.status === 'failed'" type="button" :disabled="versionBusy[document.id]" @click="retryVersion(document.id, version.id)">重试</button>
                  </li>
                </ul>
                <p v-else-if="!versionMessage[document.id]" class="version-note">暂无可显示的版本。</p>
                <p v-if="versionMessage[document.id]" class="version-note" role="status">{{ versionMessage[document.id] }}</p>
                <div class="version-actions">
                  <label class="version-action" :class="{ 'is-disabled': versionBusy[document.id] }">添加新版本<input :ref="(element) => setReplaceInput(document.id, element as Element | null)" type="file" accept=".pdf,.epub,.docx,.md,.markdown,.txt" :disabled="versionBusy[document.id]" @change="replaceVersion(document.id, $event)"></label>
                  <button type="button" class="version-remove" :disabled="versionBusy[document.id]" @click="removeDocument(document.id, document.title || document.filename)">从文献库移除</button>
                </div>
              </div>
            </details>
          </div>
          <div class="document-meta"><span class="document-status" :class="statusClass(document.status)"><i></i>{{ statusLabel(document.status) }}</span><span>{{ document.page_count ? `${document.page_count} ${['pdf','docx'].includes(document.file_format || 'pdf') ? '页' : document.file_format === 'epub' ? '章节' : '段落'}` : '整理中' }}<b>·</b>{{ formatDate(document.created_at) }}</span></div>
          <RouterLink class="document-open" :to="`/documents/${encodeURIComponent(document.id)}`" :aria-label="`阅读 ${document.title || document.filename}`">打开 <span>→</span></RouterLink>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.library-page{padding:43px 0 35px}.library-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:24px}.library-heading h1{margin:14px 0 6px;color:#28362f;font:500 27px/1.45 'Noto Serif SC',serif;letter-spacing:-.03em}.library-heading p{margin:0;color:#899187;font-size:12px;line-height:1.8}.library-primary{height:40px;display:flex;align-items:center;gap:8px;padding:0 16px;border:1px solid #3f6b56;border-radius:5px;color:#fffefa;background:#416c5a;font-size:11px;cursor:pointer;transition:background .18s ease}.library-primary:hover{background:#315646}.library-primary:disabled{opacity:.65;cursor:wait}.library-primary>span{font-size:18px;font-weight:300}.library-visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;clip-path:inset(50%)}.library-summary{display:flex;align-items:center;gap:23px;margin:24px 0 17px;padding:12px 16px;border:1px solid #e8eae3;border-radius:6px;background:#fbfbf7}.library-summary>div{display:flex;align-items:baseline;gap:7px}.library-summary strong{color:#405b49;font:500 19px 'DM Sans',sans-serif}.library-summary span{color:#8b938a;font-size:10px}.library-summary>i{width:1px;height:17px;background:#e3e6de}.library-dropzone{width:100%;min-height:82px;display:flex;align-items:center;gap:15px;padding:15px 19px;text-align:left;border:1px dashed #cbd5c9;border-radius:6px;color:#526457;background:#f8f9f4;cursor:pointer;transition:background .16s ease,border-color .16s ease}.library-dropzone:hover,.library-dropzone.is-dragging{border-color:#6e9279;background:#eff4ec}.library-dropzone:disabled{cursor:wait}.dropzone-icon{width:39px;height:39px;display:grid;place-items:center;flex:0 0 auto;border-radius:50%;color:#557761;background:#e8efe6;font-size:23px}.dropzone-copy{display:flex;flex:1;flex-direction:column;gap:5px}.dropzone-copy strong{font-size:11px;font-weight:600}.dropzone-copy small{color:#92998f;font-size:9px}.dropzone-action{color:#527460;font-size:10px;font-weight:600}.dropzone-action b{padding-left:5px;font-size:14px}.library-alert{margin:10px 0 0;padding:10px 12px;border-radius:4px;color:#8c5544;background:#f8eee9;font-size:10px;line-height:1.6}.library-list-section{margin-top:31px}.library-list-heading{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:12px}.library-list-heading h2{margin:5px 0 0;color:#344038;font:500 17px/1.4 'Noto Serif SC',serif}.library-list-count{color:#9aa197;font-size:10px}.document-list{border-top:1px solid #e5e8e0}.document-row{min-height:93px;display:flex;align-items:center;gap:15px;padding:14px 9px;border-bottom:1px solid #e8eae3}.document-cover{position:relative;width:43px;height:54px;display:grid;place-items:center;flex:0 0 auto;overflow:hidden;border:1px solid #e6e8df;border-radius:3px;color:#768775;background:#f1f2eb;font:600 8px 'DM Mono',monospace;letter-spacing:.08em}.document-cover i{position:absolute;width:30px;height:30px;right:-14px;bottom:-13px;border:1px solid #d6ded1;border-radius:50%}.document-main{min-width:0;display:flex;flex:1;flex-direction:column;gap:5px}.document-title{max-width:100%;overflow:hidden;color:#35433a;font:500 12px/1.5 'Noto Serif SC',serif;text-overflow:ellipsis;white-space:nowrap}.document-title:hover{color:#527460}.document-filename{overflow:hidden;color:#9ba198;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.document-error{color:#995a48;font-size:9px;line-height:1.5}.document-meta{min-width:148px;display:flex;flex-direction:column;align-items:flex-start;gap:7px;color:#989f96;font-size:9px}.document-meta b{padding:0 6px;color:#c3c8bd;font-weight:400}.document-status{display:flex;align-items:center;gap:6px;color:#788479}.document-status i{width:6px;height:6px;border-radius:50%;background:#9aa398}.document-status.is-ready{color:#51745d}.document-status.is-ready i{background:#70967a}.document-status.is-processing{color:#9a784c}.document-status.is-processing i{background:#c39a5d}.document-status.is-failed{color:#a45f4a}.document-status.is-failed i{background:#c57e65}.document-open{padding:7px 0 7px 9px;color:#66806f;font-size:10px;white-space:nowrap}.document-open:hover{color:#315646}.document-open span{padding-left:3px}.library-state{min-height:180px;display:flex;align-items:center;justify-content:center;gap:10px;color:#8b9489;font-size:11px}.library-state-error{flex-direction:column;color:#965d4a}.library-state button,.library-text-button{padding:0;border:0;color:#547660;background:transparent;font-size:10px;font-weight:600;cursor:pointer}.library-spinner{width:15px;height:15px;border:2px solid #dce5d9;border-top-color:#5f8069;border-radius:50%;animation:library-spin .8s linear infinite}.library-empty{min-height:225px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}.empty-book{width:48px;height:48px;display:grid;place-items:center;margin-bottom:11px;border:1px solid #e4e8df;border-radius:50%;color:#78917b;background:#f3f5ef;font-size:18px}.library-empty h3{margin:0 0 7px;color:#4c5b50;font:500 14px 'Noto Serif SC',serif}.library-empty p{max-width:360px;margin:0;color:#969e94;font-size:10px;line-height:1.8}.library-text-button{margin-top:13px}.library-text-button span{padding-left:4px}@keyframes library-spin{to{transform:rotate(360deg)}}
@media(max-width:650px){.library-page{padding:32px 0}.library-heading{align-items:flex-start;flex-direction:column;gap:16px}.library-heading h1{font-size:22px}.library-summary{justify-content:space-between;gap:8px;padding:11px}.library-summary span{font-size:9px}.library-dropzone{min-height:76px;padding:12px;gap:10px}.dropzone-action{display:none}.document-row{gap:10px;padding:12px 2px}.document-cover{width:37px;height:48px}.document-meta{min-width:auto;align-items:flex-end;font-size:8px}.document-meta b{padding:0 3px}.document-open{font-size:9px}}
@media(prefers-reduced-motion:reduce){.library-spinner{animation:none}}
.upload-results{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:7px;margin-top:10px}.upload-result{min-width:0;display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid #e8eae3;border-radius:5px;background:#fcfcf8}.upload-result-mark{width:23px;height:23px;display:grid;place-items:center;flex:0 0 auto;border-radius:50%;color:#56765e;background:#e9f0e7;font-size:12px}.upload-result-copy{min-width:0;display:flex;flex-direction:column;gap:3px}.upload-result-copy strong{overflow:hidden;color:#536056;font-size:10px;font-weight:600;text-overflow:ellipsis;white-space:nowrap}.upload-result-copy small{color:#899287;font-size:9px;line-height:1.5}.upload-result.is-duplicate .upload-result-mark{color:#7b7559;background:#f0eee2}.upload-result.is-rejected .upload-result-mark{color:#9d5d4a;background:#f7ebe6}.upload-result.is-rejected .upload-result-copy small{color:#995a48}
.document-versions{margin-top:2px;color:#718072;font-size:9px}.document-versions summary{width:max-content;cursor:pointer;list-style:none}.document-versions summary::-webkit-details-marker{display:none}.document-versions summary::before{content:'＋';padding-right:4px;color:#63816b}.document-versions[open] summary::before{content:'−'}.version-panel{max-width:540px;margin-top:7px;padding:9px 11px;border:1px solid #e8eae3;border-radius:4px;background:#fbfcf8}.version-list{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none}.version-list li{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid #eef0eb}.version-list li:last-child{border-bottom:0}.version-list li>span:first-child{min-width:0;display:flex;flex-direction:column;gap:3px}.version-list strong{color:#536056;font-size:9px;font-weight:600}.version-list small{overflow:hidden;color:#969e94;font-size:8px;text-overflow:ellipsis;white-space:nowrap}.version-list button,.version-actions button{padding:4px 8px;border:1px solid #dce4d9;border-radius:3px;color:#527460;background:#fff;font-size:8px;cursor:pointer}.version-list button:disabled,.version-actions button:disabled{opacity:.55;cursor:wait}.version-active{color:#5b7a61;font-size:8px;white-space:nowrap}.version-note{margin:0 0 7px;color:#8f988e;font-size:8px;line-height:1.6}.version-actions{display:flex;align-items:center;gap:8px;margin-top:8px}.version-action{padding:4px 8px;border:1px solid #dce4d9;border-radius:3px;color:#527460;background:#fff;font-size:8px;cursor:pointer}.version-action input{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}.version-action.is-disabled{opacity:.55;cursor:wait}.version-actions .version-remove{border-color:#eadbd5;color:#99614f}.version-note[role=status]{margin-top:5px;color:#69806c}
@media(max-width:650px){.upload-results{grid-template-columns:1fr}}
.library-heading-actions{display:flex;align-items:center;gap:8px;flex:0 0 auto}.library-secondary{height:40px;padding:0 12px;border:1px solid #dce4d9;border-radius:5px;color:#527460;background:#f8faf5;font-size:10px;cursor:pointer}.library-secondary:hover{background:#edf3eb}.library-primary:disabled,.library-secondary:disabled{opacity:.65;cursor:wait}.index-message{margin:10px 0 0;color:#5e7a63;font-size:10px;line-height:1.6}.index-message.is-error{color:#995a48}
@media(max-width:650px){.library-heading-actions{width:100%;justify-content:space-between}}
.upload-result.is-processing .upload-result-mark{color:#8f7149;background:#f3eddf}.upload-result.is-ready .upload-result-mark{color:#56765e;background:#e9f0e7}.upload-result.is-failed .upload-result-mark,.upload-result.is-rejected .upload-result-mark{color:#9d5d4a;background:#f7ebe6}.upload-result.is-failed .upload-result-copy small{color:#995a48}
</style>

<style scoped>
.collection-filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 22px;padding:13px 14px;border:1px solid #e2e8df;border-radius:11px;background:#fbfcf8}.collection-filters>input{min-width:min(260px,100%);flex:1}.collection-filters input,.collection-filters select{min-height:42px;border:1px solid #d6e0d4;border-radius:8px;background:#fffefb;padding:8px 11px;color:#405847;font-size:14px}.collection-filters label{display:flex;align-items:center;gap:7px;min-height:42px;padding:0 8px;color:#405847;font-size:14px}.collection-filters label input{padding:0}.bibliography{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;color:#4f6556;line-height:1.6}.literature-tag{padding:3px 8px;background:#edf2e9;border-radius:999px;color:#4f6d53}.metadata-actions{display:flex;gap:15px;align-items:center;color:#526a57;font-size:13px}.metadata-actions button{border:0;padding:0;background:transparent;color:#426a4c;cursor:pointer;font-size:13px}.metadata-editor{padding:15px;background:#f7f9f2;border:1px solid #dfe6d7;border-radius:9px;display:grid;grid-template-columns:1fr 1fr;gap:12px}.metadata-editor label{display:flex;flex-direction:column;gap:5px;color:#435a48;font-size:13px}.metadata-editor input,.metadata-editor select{min-height:40px;padding:7px 9px;border:1px solid #d9e2d1;border-radius:7px;min-width:0;background:white}.metadata-editor>div{grid-column:1/-1;display:flex;gap:8px}.metadata-editor button{border:1px solid #d0dec6;padding:7px 12px;background:#fff;color:#466549;border-radius:7px;cursor:pointer}.document-title{font-size:17px;line-height:1.55;white-space:normal;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow-wrap:anywhere}.document-filename{font-size:12px;color:#627267}.document-versions,.document-meta{font-size:12px}.document-meta{min-width:145px;color:#526458}.document-status{font-size:13px}.document-open{font-size:13px}.filter-empty{padding:25px;text-align:center;color:#526458;font-size:14px}
.library-page{padding-top:48px}.library-heading h1{font-size:34px;line-height:1.42}.library-heading p{color:#5f7064;font-size:15px}.library-heading .eyebrow{display:none}.library-primary,.library-secondary{height:44px;border-radius:8px;padding:0 15px;font-size:14px}.library-summary{margin:25px 0 18px;padding:15px 19px;border-radius:11px;gap:27px}.library-summary strong{font-size:22px}.library-summary span{color:#53695a;font-size:13px}.library-dropzone{min-height:92px;padding:16px 20px;border-radius:11px}.dropzone-copy strong{font-size:15px}.dropzone-copy small{color:#52675a;font-size:13px}.dropzone-action{display:flex;align-items:center;gap:7px;font-size:13px}.library-list-section{margin-top:35px}.library-list-heading{margin-bottom:16px}.library-list-heading h2{font-size:21px}.library-list-count{color:#52675a;font-size:13px}.document-list{border-top:1px solid #dfe5dc}.document-row{min-height:122px;gap:17px;padding:19px 11px;align-items:flex-start}.document-cover{width:48px;height:61px;border-radius:5px;font-size:10px;background:#eef2e9;color:#48634e}.document-main{gap:7px}.document-title{color:#293d30}.document-error{font-size:13px}.document-meta{padding-top:3px;line-height:1.65}.document-meta b{padding:0 5px}.library-empty h3{font-size:18px}.library-empty p{color:#52675a;font-size:14px}.library-text-button{font-size:14px}.upload-result-copy strong{font-size:13px}.upload-result-copy small{font-size:12px}.version-list small,.version-note{font-size:12px}.version-list strong,.version-active,.document-versions{font-size:13px}.version-list button,.version-actions button,.version-action{padding:7px 10px;font-size:12px}
@media(max-width:850px){.document-row{display:grid;grid-template-columns:48px minmax(0,1fr) auto}.document-cover{grid-row:1/3}.document-main{grid-column:2;grid-row:1/span 2}.document-meta{grid-column:3;grid-row:1}.document-open{grid-column:3;grid-row:2;align-self:end}}
@media(max-width:650px){.library-page{padding-top:32px}.library-heading h1{font-size:27px}.library-heading-actions{justify-content:flex-start;gap:9px}.library-heading-actions button{flex:1;justify-content:center}.library-summary{gap:7px;padding:12px 9px}.library-summary>div{gap:5px}.library-summary strong{font-size:19px}.library-summary span{font-size:11px}.library-summary>i{height:22px}.library-dropzone{align-items:flex-start;min-height:84px;padding:13px 11px}.dropzone-icon{width:34px;height:34px}.dropzone-copy strong{font-size:13px}.dropzone-copy small{font-size:11px;line-height:1.55}.document-row{grid-template-columns:40px minmax(0,1fr);gap:8px;padding:15px 2px}.document-cover{grid-column:1;grid-row:1/4;width:38px;height:50px}.document-main{grid-column:2;grid-row:1;gap:6px}.document-title{font-size:15px;-webkit-line-clamp:3}.document-filename{font-size:11px}.bibliography{font-size:12px}.metadata-actions{flex-wrap:wrap;gap:12px;font-size:12px}.metadata-actions button{font-size:12px}.document-meta{grid-column:2;grid-row:2;align-items:flex-start;padding:0;flex-direction:row;flex-wrap:wrap;gap:8px;font-size:11px}.document-open{grid-column:2;grid-row:3;justify-self:start;padding:4px 0;font-size:12px}.collection-filters{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px}.collection-filters>input{grid-column:1/-1;min-width:0}.collection-filters input,.collection-filters select{min-width:0;width:100%;font-size:12px}.collection-filters label{grid-column:1/-1;min-height:34px}.metadata-editor{grid-template-columns:1fr}.metadata-editor>div{grid-column:auto}.document-versions{font-size:12px}}
.collection-filters input[type="checkbox"] { width:16px;height:16px;min-height:0;flex:0 0 16px;padding:0; }
.collection-filters label { white-space:nowrap; }
@media(max-width:650px){.library-dropzone .dropzone-action{display:none}.dropzone-copy{min-width:0;flex:1}.collection-filters label{grid-column:1/-1;white-space:nowrap}}
</style>
