<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { createDocumentQuestion, getTask, listDocuments, type Citation, type DocumentSummary, type WorkbenchTask } from '../api/workbench'

const route = useRoute()
const documentId = computed(() => String(route.params.id ?? route.params.documentId ?? route.query.documentId ?? ''))
const document = ref<DocumentSummary>()
const question = ref('')
const task = ref<WorkbenchTask>()
const submitting = ref(false)
const error = ref('')
let pollTimer: ReturnType<typeof setTimeout> | undefined

const isPending = computed(() => task.value && ['queued', 'pending', 'running', 'processing'].includes(task.value.status))
const isComplete = computed(() => task.value && ['completed', 'complete', 'succeeded', 'success', 'done'].includes(task.value.status))

async function loadDocument() {
  if (!documentId.value) return
  try { document.value = (await listDocuments()).find((item) => item.id === documentId.value) }
  catch { /* The question flow remains available if the document list is temporarily unavailable. */ }
}

function taskLabel(status?: string) {
  if (['queued', 'pending'].includes(status ?? '')) return '已收到问题，正在排队…'
  if (['running', 'processing'].includes(status ?? '')) return '正在查阅论文并整理回答…'
  return '正在等待回答…'
}

async function poll(taskId: string) {
  try {
    const current = await getTask(taskId)
    task.value = current
    if (['queued', 'pending', 'running', 'processing'].includes(current.status)) {
      pollTimer = setTimeout(() => void poll(taskId), 1200)
    } else if (!['completed', 'complete', 'succeeded', 'success', 'done'].includes(current.status)) {
      error.value = current.last_error_code ? `这次提问没有完成（${current.last_error_code}），可以稍后再试。` : '这次提问没有完成，请稍后再试。'
    }
  } catch (cause) {
    error.value = cause instanceof Error ? `暂时无法获取回答：${cause.message}` : '暂时无法获取回答，请重试。'
  } finally {
    if (!isPending.value) submitting.value = false
  }
}

async function ask() {
  const trimmed = question.value.trim()
  if (!trimmed || !documentId.value || submitting.value) return
  error.value = ''
  task.value = undefined
  submitting.value = true
  try {
    const created = await createDocumentQuestion(documentId.value, trimmed)
    task.value = { id: created.task_id, status: created.status }
    await poll(created.task_id)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '问题提交失败，请检查网络后重试。'
    submitting.value = false
  }
}

function citationHref(citation: Citation) {
  return `/documents/${encodeURIComponent(citation.document_id)}?page=${encodeURIComponent(String(citation.page_number))}`
}

function citationLabel(citation: Citation) {
  return citation.document_id === documentId.value ? `第 ${citation.page_number} 页` : `来源文献 · 第 ${citation.page_number} 页`
}

void loadDocument()
onUnmounted(() => { if (pollTimer) clearTimeout(pollTimer) })
</script>

<template>
  <div class="page-content study-page">
    <div class="study-breadcrumb"><RouterLink to="/documents">我的文献</RouterLink><span>/</span><RouterLink :to="`/documents/${encodeURIComponent(documentId)}`">{{ document?.title || document?.filename || '论文阅读' }}</RouterLink><span>/</span><span>论文问答</span></div>
    <header class="study-header"><div class="study-header-mark" aria-hidden="true">✳</div><div><span class="section-kicker">READ WITH EVIDENCE</span><h1>向这篇论文提问</h1><p>{{ document?.title || document?.filename || '答案将围绕所选论文，并附上可回看的出处。' }}</p></div></header>

    <div class="study-layout">
      <section class="study-composer panel">
        <label for="study-question">你想了解什么？</label>
        <textarea id="study-question" v-model="question" :disabled="submitting" maxlength="2000" placeholder="例如：作者如何定义核心概念？研究设计有哪些限制？" @keydown.ctrl.enter="ask" @keydown.meta.enter="ask"></textarea>
        <div class="study-composer-footer"><span>聚焦当前论文 · Ctrl / ⌘ + Enter 发送</span><button type="button" :disabled="question.trim().length === 0 || submitting || !documentId" @click="ask">{{ submitting ? '正在整理…' : '提问' }} <span>↑</span></button></div>
        <p v-if="error" class="study-error" role="alert">{{ error }}</p>
      </section>

      <section class="study-answer panel" aria-live="polite">
        <template v-if="isPending"><div class="study-pending"><span class="study-spinner"></span><div><strong>{{ taskLabel(task?.status) }}</strong><p>稍等片刻，完成后会在这里显示回答和引用出处。</p></div></div></template>
        <template v-else-if="task && isComplete && task.result">
          <div class="study-answer-heading"><span class="section-kicker">ANSWER</span><span class="answer-source-mark">已结合论文原文</span></div>
          <p class="study-answer-text">{{ task.result.answer }}</p>
          <div class="citation-section"><div class="citation-heading"><h2>引用出处</h2><span>{{ task.result.citations.length }} 条</span></div>
            <div v-if="task.result.citations.length" class="citation-list"><article v-for="(citation, index) in task.result.citations" :key="`${citation.document_id}-${citation.page_number}-${index}`" class="citation-item"><div class="citation-topline"><span class="citation-index">{{ String(index + 1).padStart(2, '0') }}</span><div class="citation-meta"><span v-if="citation.is_derived" class="citation-derived-tag">页码定位</span><RouterLink class="citation-page-link" :to="citationHref(citation)">{{ citationLabel(citation) }} <span>↗</span></RouterLink></div></div><blockquote v-if="citation.quote">“{{ citation.quote }}”</blockquote><p v-if="citation.reason" class="citation-reason">{{ citation.reason }}</p></article></div>
            <p v-else class="citation-empty">这次回答没有返回引用片段。</p>
          </div>
        </template>
        <div v-else-if="task && isComplete" class="study-answer-state"><span>✓</span><h2>回答已完成</h2><p>暂时没有可显示的回答内容。</p></div>
        <div v-else class="study-answer-empty"><div class="answer-ornament" aria-hidden="true">“</div><span class="section-kicker">A PLACE FOR YOUR ANSWER</span><h2>从一个问题开始</h2><p>回答会提供相关原文摘录和页码，方便你核对上下文。</p><div class="answer-rule"><span></span><i></i><span></span></div></div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.study-page{padding:29px 0 38px}.study-breadcrumb{display:flex;align-items:center;gap:9px;color:#9ba198;font-size:9px}.study-breadcrumb a{color:#66806f}.study-breadcrumb a:hover{color:#315646}.study-header{display:flex;align-items:center;gap:15px;margin:22px 0 19px}.study-header-mark{width:43px;height:43px;display:grid;place-items:center;border-radius:13px 13px 13px 4px;color:#55765e;background:#eaf0e8;font-size:19px}.study-header h1{margin:4px 0;color:#2c3b32;font:500 22px/1.5 'Noto Serif SC',serif}.study-header p{margin:0;color:#929a90;font-size:10px;line-height:1.65}.study-layout{display:grid;grid-template-columns:minmax(250px,.72fr) minmax(0,1.28fr);align-items:start;gap:14px}.study-composer{position:sticky;top:18px;padding:17px}.study-composer label{display:block;margin-bottom:10px;color:#46574b;font:500 13px 'Noto Serif SC',serif}.study-composer textarea{width:100%;min-height:164px;resize:vertical;padding:12px;border:1px solid #e5e8df;border-radius:5px;outline:0;color:#47564b;background:#fbfcf8;font:11px/1.8 'Noto Sans SC',sans-serif;transition:border-color .18s ease,box-shadow .18s ease}.study-composer textarea:focus{border-color:#96ad98;box-shadow:0 0 0 3px #78977b18}.study-composer textarea::placeholder{color:#abb1a8}.study-composer-footer{display:flex;align-items:center;justify-content:space-between;gap:9px;margin-top:11px}.study-composer-footer>span{color:#a1a79f;font-size:8px}.study-composer-footer button{display:inline-flex;align-items:center;gap:7px;padding:8px 12px;border:1px solid #416c5a;border-radius:4px;color:#fffefa;background:#416c5a;font-size:10px;cursor:pointer}.study-composer-footer button:hover{background:#315646}.study-composer-footer button:disabled{opacity:.55;cursor:default}.study-composer-footer button span{font-size:13px}.study-error{margin:10px 0 0;padding:9px 10px;border-radius:4px;color:#905b49;background:#f8eee9;font-size:9px;line-height:1.65}.study-answer{min-height:338px;padding:20px 22px}.study-answer-empty{min-height:295px;display:flex;align-items:center;justify-content:center;flex-direction:column;text-align:center}.answer-ornament{height:49px;color:#dce5da;font:72px/.95 Georgia,serif}.study-answer-empty .section-kicker{margin-top:8px}.study-answer-empty h2{margin:11px 0 6px;color:#435247;font:500 16px 'Noto Serif SC',serif}.study-answer-empty p{max-width:280px;margin:0;color:#969e94;font-size:10px;line-height:1.8}.answer-rule{display:flex;align-items:center;gap:7px;width:62px;margin-top:20px}.answer-rule span{height:1px;flex:1;background:#dce3d9}.answer-rule i{width:4px;height:4px;border:1px solid #91a591;border-radius:50%}.study-pending{min-height:280px;display:flex;align-items:center;justify-content:center;gap:13px}.study-spinner{width:24px;height:24px;border:2px solid #dce5d9;border-top-color:#5f8069;border-radius:50%;animation:study-spin .8s linear infinite}.study-pending strong{color:#536357;font:500 12px 'Noto Serif SC',serif}.study-pending p{margin:6px 0 0;color:#9aa196;font-size:9px}.study-answer-heading,.citation-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.answer-source-mark{color:#729078;font-size:8px}.study-answer-text{margin:16px 0 21px;color:#46554a;font:12px/2 'Noto Sans SC',sans-serif;white-space:pre-wrap;overflow-wrap:anywhere}.citation-section{padding-top:15px;border-top:1px solid #e9ebe4}.citation-heading{margin-bottom:10px}.citation-heading h2{margin:0;color:#405044;font:500 13px 'Noto Serif SC',serif}.citation-heading>span{color:#9ca399;font-size:8px}.citation-list{display:flex;flex-direction:column;gap:8px}.citation-item{padding:12px 13px;border:1px solid #eaede6;border-radius:4px;background:#fbfcf8}.citation-topline{display:flex;align-items:center;justify-content:space-between}.citation-index{color:#aeb6aa;font:9px 'DM Mono',monospace;letter-spacing:.08em}.citation-page-link{color:#54765d;font-size:9px;font-weight:600}.citation-page-link:hover{text-decoration:underline}.citation-page-link span{padding-left:4px}.citation-item blockquote{margin:8px 0 5px;color:#536057;font:11px/1.85 'Noto Sans SC',sans-serif}.citation-reason{margin:0;color:#9aa196;font-size:9px;line-height:1.65}.citation-empty{margin:0;color:#999f97;font-size:10px}.study-answer-state{min-height:280px;display:flex;align-items:center;justify-content:center;flex-direction:column;text-align:center}.study-answer-state>span{width:31px;height:31px;display:grid;place-items:center;border-radius:50%;color:#507259;background:#edf3eb}.study-answer-state h2{margin:10px 0 5px;color:#435247;font:500 15px 'Noto Serif SC',serif}.study-answer-state p{margin:0;color:#969e94;font-size:10px}@keyframes study-spin{to{transform:rotate(360deg)}}
@media(max-width:850px){.study-layout{grid-template-columns:1fr}.study-composer{position:static}.study-composer textarea{min-height:125px}}
@media(max-width:650px){.study-page{padding-top:25px}.study-header{gap:11px;margin:18px 0 15px}.study-header-mark{width:38px;height:38px}.study-header h1{font-size:19px}.study-answer{padding:17px 14px}.study-answer-text{font-size:11px}}
@media(prefers-reduced-motion:reduce){.study-spinner{animation:none}}
.citation-meta{display:flex;align-items:center;gap:9px}.citation-derived-tag{padding:3px 6px;border-radius:3px;color:#7d7153;background:#f4f0e6;font-size:8px}
</style>
