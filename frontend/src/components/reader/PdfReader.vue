<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy } from 'pdfjs-dist'
import { EventBus, PDFViewer, PDFLinkService, PDFFindController } from 'pdfjs-dist/web/pdf_viewer.mjs'
import 'pdfjs-dist/web/pdf_viewer.css'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
GlobalWorkerOptions.workerSrc = workerUrl
const props = defineProps<{ url: string; page: number; quote?: string }>()
const emit = defineEmits<{ location: [page: number]; selection: [text: string]; outline: [items: Array<{ title: string; page: number }>]; loaded: [count: number] }>()
const container = ref<HTMLDivElement>()
const viewerElement = ref<HTMLDivElement>()
const toolbar = ref<HTMLDivElement>(), toolbarHeight = ref(55)
let toolbarObserver: ResizeObserver | undefined
const total = ref(0), current = ref(1), pageInput = ref(1), zoom = ref('page-width'), scalePercent = ref(100), rotation = ref(0), search = ref(''), message = ref(''), busy = ref(true)
let viewer: PDFViewer | undefined, link: PDFLinkService | undefined, pdf: PDFDocumentProxy | undefined
let loadingTask: ReturnType<typeof getDocument> | undefined
const bus = new EventBus()
async function load() {
  busy.value = true; message.value = ''
  try {
    loadingTask = getDocument({ url: props.url, cMapUrl: '/pdfjs/cmaps/', cMapPacked: true, standardFontDataUrl: '/pdfjs/standard_fonts/', wasmUrl: '/pdfjs/wasm/' })
    pdf = await loadingTask.promise
    if (!viewer) return
    total.value = pdf.numPages; emit('loaded', total.value)
    viewer.setDocument(pdf); link?.setDocument(pdf)
    const outline = await pdf.getOutline()
    const items: Array<{ title: string; page: number }> = []
    async function visit(nodes: typeof outline) {
      for (const item of nodes ?? []) {
        const dest = typeof item.dest === 'string' ? await pdf!.getDestination(item.dest) : item.dest
        if (dest?.length) { try { items.push({ title: item.title, page: typeof dest[0] === 'number' ? dest[0] + 1 : await pdf!.getPageIndex(dest[0]) + 1 }) } catch { /* skip broken external destinations */ } }
        await visit(item.items)
      }
    }
    await visit(outline); emit('outline', items)
  } catch (error) { message.value = error instanceof Error ? error.message : '原文暂时无法显示。' } finally { busy.value = false }
}
function go(page: number) { if (viewer && total.value) viewer.currentPageNumber = Math.min(total.value, Math.max(1, page)) }
function find(again = false, previous = false) { bus.dispatch('find', { source: null, type: again ? 'again' : '', query: search.value, phraseSearch: true, caseSensitive: false, entireWord: false, highlightAll: true, findPrevious: previous }) }
function setZoom() { if (viewer) viewer.currentScaleValue = zoom.value }
function adjustZoom(delta: number) {
  if (!viewer) return
  const next = Math.min(400, Math.max(25, Math.round(viewer.currentScale * 100) + delta))
  zoom.value = String(next / 100)
  viewer.currentScaleValue = zoom.value
  scalePercent.value = next
}
function rotate() { rotation.value = (rotation.value + 90) % 360; if (viewer) viewer.pagesRotation = rotation.value }
function captureSelection() { emit('selection', window.getSelection()?.toString() || '') }
onMounted(() => {
  toolbarObserver = new ResizeObserver(() => { toolbarHeight.value = toolbar.value?.offsetHeight || 55 })
  if (toolbar.value) toolbarObserver.observe(toolbar.value)
  link = new PDFLinkService({ eventBus: bus, externalLinkTarget: 2 })
  const finder = new PDFFindController({ eventBus: bus, linkService: link })
  viewer = new PDFViewer({ container: container.value!, viewer: viewerElement.value!, eventBus: bus, linkService: link, findController: finder, textLayerMode: 1, annotationMode: 2 })
  link.setViewer(viewer)
  bus.on('pagesinit', () => { setZoom(); go(props.page); if (props.quote) { search.value = props.quote; find() } })
  bus.on('scalechanging', (e: { scale: number }) => { scalePercent.value = Math.round(e.scale * 100) })
  bus.on('pagechanging', (e: { pageNumber: number }) => { current.value = e.pageNumber; pageInput.value = e.pageNumber; emit('location', e.pageNumber) })
  bus.on('updatefindmatchescount', (e: { matchesCount: { current: number; total: number } }) => { message.value = search.value ? `${e.matchesCount.current} / ${e.matchesCount.total} 处匹配` : '' })
  void load()
})
watch(() => props.page, go)
watch(() => props.quote, value => { if (value) { search.value = value; find() } })
onBeforeUnmount(() => { toolbarObserver?.disconnect(); void loadingTask?.destroy() })
</script>
<template>
  <div class="pdf-reading">
    <div ref="toolbar" class="pdf-tools">
      <button aria-label="上一页" :disabled="current <= 1" @click="go(current - 1)">‹</button>
      <form @submit.prevent="go(Number(pageInput))"><input v-model="pageInput" type="number" min="1" :max="total" aria-label="页码"><span>/ {{ total }}</span></form>
      <button aria-label="下一页" :disabled="current >= total" @click="go(current + 1)">›</button>
      <button aria-label="缩小" title="缩小" @click="adjustZoom(-10)">−</button><span class="zoom-readout" aria-live="polite">{{ scalePercent }}%</span><button aria-label="放大" title="放大" @click="adjustZoom(10)">＋</button>
      <select v-model="zoom" aria-label="缩放" @change="setZoom"><option value="page-width">适合宽度</option><option value="page-fit">整页</option><option value="0.75">75%</option><option value="1">100%</option><option value="1.5">150%</option><option value="2">200%</option><option v-if="!['page-width','page-fit','0.75','1','1.5','2'].includes(zoom)" :value="zoom">{{ scalePercent }}%</option></select>
      <button @click="rotate" aria-label="顺时针旋转">↻</button>
      <form class="pdf-search" @submit.prevent="find()"><input v-model="search" placeholder="文内查找" aria-label="文内查找"><button>查找</button></form><button @click="find(true,true)" aria-label="上一个匹配">↑</button><button @click="find(true)" aria-label="下一个匹配">↓</button>
    </div>
    <div v-if="busy" class="pdf-status">正在打开原文…</div><div v-if="message" class="pdf-status" role="status">{{ message }}</div>
    <div ref="container" class="pdf-scroll" :style="{top: `${toolbarHeight}px`}" @mouseup="captureSelection"><div ref="viewerElement" class="pdfViewer"></div></div>
  </div>
</template>
<style scoped>
.pdf-reading{height:100%;position:relative;background:#e8ebe5;display:flex;flex-direction:column}.pdf-tools{display:flex;align-items:center;gap:7px;flex-wrap:wrap;padding:10px 12px;background:#fafbf8;border-bottom:1px solid #dce2d8;font-size:12px;z-index:1}.pdf-tools button,.pdf-tools select,.pdf-tools input{border:1px solid #d7dfd3;border-radius:4px;background:white;padding:5px 7px;color:#425c49}.pdf-tools button{cursor:pointer}.pdf-tools button:disabled{opacity:.4}.pdf-tools form{display:flex;align-items:center;gap:5px}.pdf-tools input[type=number]{width:55px}.zoom-readout{min-width:36px;text-align:center;color:#728072;font-variant-numeric:tabular-nums}.pdf-search{margin-left:auto}.pdf-search input{width:145px}.pdf-scroll{position:absolute;inset:55px 0 0;overflow:auto;--scale-factor:1}.pdf-status{position:absolute;top:58px;right:16px;background:#fffdf3;padding:6px 10px;border-radius:4px;z-index:2;font-size:12px;color:#685c40;max-width:80%;overflow-wrap:anywhere}@media(max-width:950px){.pdf-scroll{top:96px}.pdf-status{top:100px}.pdf-search{margin-left:0}}
</style>
