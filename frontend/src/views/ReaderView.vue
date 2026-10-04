<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import PdfReader from '../components/reader/PdfReader.vue'
import EpubReader from '../components/reader/EpubReader.vue'
import { addAnnotation, getReader, removeAnnotation, savePosition, type Annotation, type ReaderManifest } from '../api/reader'
import { useWorkspaceContextStore } from '../stores/workspaceContext'
import { useWorkspaceChromeStore } from '../stores/workspaceChrome'
import { useWorkspaceNotesStore } from '../stores/workspaceNotes'
import SvgIcon from '../components/SvgIcon.vue'
import type { Locator, NoteContent } from '../types/workspace'
const route = useRoute(), router = useRouter(), context = useWorkspaceContextStore(), chrome = useWorkspaceChromeStore(), notes = useWorkspaceNotesStore()
const manifest = ref<ReaderManifest>(), error = ref(''), loading = ref(false), saving = ref(''), mode = ref<'outline' | 'thumbnails' | 'annotations'>('outline')
const locator = ref<Locator>({ kind: 'page', page: 1 }), selected = ref(''), outline = ref<Array<{ title: string; page: number }>>([]), highlightQuote = ref(''), comment = ref(''), textSearch = ref(''), textScale = ref(100)
const textHost = ref<HTMLDivElement>(), navVisible = ref(true)
const selectedLocator = ref<Locator>()
const documentId = computed(() => String(route.params.id ?? ''))
const md = new MarkdownIt({ html: false, linkify: false, breaks: true })
let generation = 0, positionTimer: ReturnType<typeof setTimeout> | undefined
function queryLocator(): Locator | undefined {
  if (route.query.page && Number(route.query.page) > 0) return { kind: 'page', page: Number(route.query.page) }
  if (route.query.cfi || route.query.chapter) return { kind: 'epub', ...(route.query.cfi ? { cfi: String(route.query.cfi) } : {}), ...(route.query.chapter ? { chapter: String(route.query.chapter) } : {}) }
  if (route.query.anchor) return { kind: 'text', anchor: String(route.query.anchor) }
}
async function load() {
  const attempt = ++generation
  loading.value = true; error.value = ''; selected.value = ''; outline.value = []
  context.setPageType('reader')
  try {
    const versionQuery = route.query.version || route.query.version_id
    const result = await getReader(documentId.value, typeof versionQuery === 'string' ? versionQuery : undefined)
    if (attempt !== generation) return
    manifest.value = result
    let local: Locator | null = null
    try { local = JSON.parse(localStorage.getItem(`reading:${result.version_id}`) ?? 'null') as Locator | null } catch { /* discard invalid saved positions */ }
    locator.value = queryLocator() || local || result.reading_state?.locator || result.sections[0]?.locator || { kind: 'page', page: 1 }
    context.setReaderContext({ documentId: result.document_id, versionId: result.version_id, locator: locator.value })
    context.setSelection(''); await nextTick(); scrollText()
  } catch (cause) { if (attempt === generation) error.value = cause instanceof Error ? cause.message : '文献暂不可用。' } finally { if (attempt === generation) loading.value = false }
}
function updatePosition(next: Locator) {
  if (!manifest.value) return
  locator.value = next; context.setLocator(next)
  const id = manifest.value.document_id, version = manifest.value.version_id
  localStorage.setItem(`reading:${version}`, JSON.stringify(next))
  if (positionTimer) clearTimeout(positionTimer)
  positionTimer = setTimeout(() => { void savePosition(id, version, next).then(() => { saving.value = '' }).catch(() => { saving.value = '位置已保存在本机，联网后继续同步。' }) }, 700)
}
function scrollText() { if (locator.value.kind === 'text' && typeof locator.value.anchor === 'string') textHost.value?.querySelector(`[id="${CSS.escape(locator.value.anchor)}"]`)?.scrollIntoView({ block: 'start' }) }
function go(next: Locator, quote = '') {
  highlightQuote.value = quote; updatePosition(next)
  const query: Record<string, string> = { version: manifest.value!.version_id }
  for (const key of ['page', 'chapter', 'cfi', 'anchor']) if (next[key] != null) query[key] = String(next[key])
  void router.replace({ query }); void nextTick(scrollText)
}
function selection(text: string, next = locator.value) { selected.value = text.slice(0, 4000); selectedLocator.value = JSON.parse(JSON.stringify(next)) as Locator; context.setSelection(selected.value, next) }
function textSelection() {
  const value = window.getSelection()
  if (!value?.rangeCount || !textHost.value?.contains(value.anchorNode)) return
  const anchor = value.anchorNode?.parentElement?.closest('[data-anchor]')?.getAttribute('data-anchor')
  selection(value.toString(), anchor ? { kind: 'text', anchor } : locator.value)
}
function readingTextScroll() {
  if (!textHost.value) return
  const top = textHost.value.getBoundingClientRect().top
  const current = Array.from(textHost.value.querySelectorAll<HTMLElement>('[data-anchor]')).find(el => el.getBoundingClientRect().bottom > top + 20)
  if (current?.dataset.anchor && locator.value.anchor !== current.dataset.anchor) updatePosition({ kind: 'text', anchor: current.dataset.anchor })
}
async function annotate(bookmark = false) {
  if (!manifest.value) return
  try {
    const item = await addAnnotation(documentId.value, manifest.value.version_id, { locator: bookmark ? locator.value : selectedLocator.value || locator.value, quote: bookmark ? '' : selected.value, comment: comment.value, color: bookmark ? 'bookmark' : 'yellow' })
    manifest.value.annotations.push(item); comment.value = ''; saving.value = '已保存批注。'
  } catch (cause) { saving.value = cause instanceof Error ? cause.message : '批注未保存，请重试。' }
}
async function erase(annotation: Annotation) { try { await removeAnnotation(documentId.value, annotation.id); manifest.value!.annotations = manifest.value!.annotations.filter(a => a.id !== annotation.id) } catch (cause) { saving.value = String(cause) } }
async function excerptToNote() {
  if (!selected.value || !manifest.value) return
  if (!notes.currentId) await notes.create()
  const loc = selectedLocator.value || locator.value
  const query = new URLSearchParams({ version: manifest.value.version_id })
  for (const key of ['page', 'chapter', 'cfi', 'anchor']) if (loc[key] != null) query.set(key, String(loc[key]))
  const reference = `${manifest.value.title} · ${loc.kind === 'page' ? `第 ${loc.page} 页` : loc.chapter || loc.anchor || '选区'}`
  const content: NoteContent = { type: 'doc', content: [...notes.content.content, { type: 'blockquote', content: [{ type: 'paragraph', content: [{ type: 'text', text: selected.value }] }] }, { type: 'paragraph', content: [{ type: 'text', text: reference, marks: [{ type: 'link', attrs: { href: `/documents/${documentId.value}?${query}` } }] }] }] }
  notes.edit(notes.title, content); chrome.show('notes')
}
const outlineItems = computed(() => manifest.value?.location_kind === 'page' ? outline.value.map(i => ({ label: i.title, locator: { kind: 'page', page: i.page } as Locator })) : (manifest.value?.sections ?? []))
function label(value: Locator) { return value.kind === 'page' ? `第 ${value.page} 页` : String(value.chapter || value.anchor || '原文位置') }
function renderText(text: string) { return DOMPurify.sanitize(md.render(text)) }
function findText() {
  if (!textSearch.value.trim() || !textHost.value) return
  const block = Array.from(textHost.value.querySelectorAll<HTMLElement>('[data-anchor]')).find(el => el.innerText.toLowerCase().includes(textSearch.value.toLowerCase()))
  if (block?.dataset.anchor) go({ kind: 'text', anchor: block.dataset.anchor }); else saving.value = '未找到匹配内容。'
}
function adjustTextScale(delta: number) {
  const scrollTop = textHost.value?.scrollTop ?? 0
  textScale.value = Math.min(200, Math.max(70, textScale.value + delta))
  void nextTick(() => { if (textHost.value) textHost.value.scrollTop = scrollTop })
}
watch([documentId, () => route.query.version || route.query.version_id], () => { void load() }, { immediate: true })
watch(() => [route.query.page, route.query.chapter, route.query.cfi, route.query.anchor], () => { const target = queryLocator(); if (target) { locator.value = target; context.setLocator(target); void nextTick(scrollText) } })
onBeforeUnmount(() => { generation++; if (positionTimer) clearTimeout(positionTimer); if (manifest.value) void savePosition(manifest.value.document_id, manifest.value.version_id, locator.value).catch(() => undefined) })
</script>
<template>
  <div class="page-content reading-page">
    <header class="reading-header"><div class="reading-heading-copy"><RouterLink to="/documents">文献库</RouterLink><h1>{{ manifest?.title || '文献阅读' }}</h1></div><div class="reading-actions"><button type="button" :aria-expanded="navVisible" @click="navVisible = !navVisible">{{ navVisible ? '收起导航' : '显示导航' }}</button><button type="button" @click="chrome.show('notes')"><SvgIcon name="note" :size="16" />笔记</button><button type="button" @click="chrome.show('assistant')"><SvgIcon name="assistant" :size="16" />AI 助手</button><a v-if="manifest" :href="manifest.file_url" download>下载原件</a></div></header>
    <p v-if="manifest?.file_format === 'docx'" class="reading-notice">当前展示 DOCX 的 PDF 转换版；字体或分页可能与 Word 不同，原件可下载。</p><p v-if="saving" class="reading-notice" role="status">{{ saving }}</p>
    <div v-if="loading" class="reading-empty">正在打开文献…</div><div v-else-if="error" class="reading-empty" role="alert">{{ error }}<button @click="load">重新载入</button></div>
    <div v-else-if="manifest" class="reading-layout" :class="{ 'without-nav': !navVisible }">
      <aside v-if="navVisible" class="reading-nav"><div class="nav-tabs"><button :class="{ active: mode === 'outline' }" @click="mode = 'outline'">目录</button><button v-if="manifest.location_kind === 'page'" :class="{ active: mode === 'thumbnails' }" @click="mode = 'thumbnails'">缩略图</button><button :class="{ active: mode === 'annotations' }" @click="mode = 'annotations'">批注</button></div>
        <div v-if="mode === 'outline'" class="nav-content"><p v-if="!outlineItems.length">这篇文献没有内置目录，可使用页码或缩略图导航。</p><button v-for="(item,index) in outlineItems" :key="index" class="outline-item" @click="go(item.locator)">{{ item.label }}</button></div>
        <div v-else-if="mode === 'thumbnails'" class="nav-content thumbnails"><button v-for="n in manifest.page_count" :key="n" @click="go({kind:'page',page:n})"><img loading="lazy" :src="`/api/v1/documents/${documentId}/pages/${n}/preview?version_id=${manifest.version_id}`" :alt="`第 ${n} 页缩略图`"><span>{{ n }}</span></button></div>
        <div v-else class="nav-content"><button class="bookmark-button" @click="annotate(true)">＋ 当前位置书签</button><p v-if="!manifest.annotations.length">选取原文添加摘录批注，或记录当前位置。</p><article v-for="a in manifest.annotations" :key="a.id" class="annotation"><button @click="go(a.locator,a.quote)"><small>{{ label(a.locator) }}</small><p>{{ a.quote || '书签' }}</p><span>{{ a.comment }}</span></button><button class="erase" @click="erase(a)" aria-label="删除批注">删除</button></article></div>
      </aside>
      <main class="reading-surface"><PdfReader v-if="manifest.location_kind === 'page'" :key="manifest.version_id" :url="manifest.render_url" :page="Number(locator.page || 1)" :quote="highlightQuote" @location="updatePosition({kind:'page',page:$event})" @selection="selection($event)" @outline="outline = $event" />
        <EpubReader v-else-if="manifest.location_kind === 'epub'" :key="manifest.version_id" :url="manifest.render_url" :locator="locator" :sections="manifest.sections" @location="updatePosition" @selection="selection" />
        <div v-else class="text-reading"><form @submit.prevent="findText"><input v-model="textSearch" placeholder="查找文内内容" aria-label="文内查找"><button>查找</button><span class="text-control-spacer"></span><button type="button" aria-label="缩小字号" title="缩小字号" @click="adjustTextScale(-10)">−</button><span class="text-scale" aria-live="polite">{{ textScale }}%</span><button type="button" aria-label="放大字号" title="放大字号" @click="adjustTextScale(10)">＋</button></form><div ref="textHost" class="text-scroll" :style="{ fontSize: `${16 * textScale / 100}px` }" @mouseup="textSelection" @scroll.passive="readingTextScroll"><section v-for="s in manifest.sections" :id="String(s.locator.anchor)" :key="s.number" :data-anchor="s.locator.anchor"><div v-if="manifest.file_format === 'md'" v-html="renderText(s.text)"></div><p v-else>{{ s.text }}</p></section></div></div>
      </main>
    </div>
    <div v-if="selected && !loading" class="selection-tools"><span>{{ selected.length }} 字选区</span><button @click="excerptToNote">摘录到笔记</button><button @click="chrome.show('assistant')">向 AI 提问</button><input v-model="comment" aria-label="批注内容" placeholder="批注（可选）"><button @click="annotate()">保存批注</button><button @click="selection('')" aria-label="清除选区">×</button></div>
  </div>
</template>
<style scoped>
.reading-page{padding:22px 0 24px}.reading-header{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:14px}.reading-header a{color:#5f7c65;font-size:12px}.reading-header h1{font:500 22px/1.5 'Noto Serif SC',serif;margin:6px 0 0;color:#2d4234}.reading-actions{display:flex;gap:7px;flex-wrap:wrap;flex-shrink:0}.reading-actions button,.reading-actions>a,.bookmark-button{padding:7px 11px;border:1px solid #d8e0d3;border-radius:4px;background:#fbfcf8;color:#466a51;font-size:12px;cursor:pointer}.reading-layout{height:max(560px,calc(100vh - 230px));display:grid;grid-template-columns:220px minmax(0,1fr);border:1px solid #dce2d6;border-radius:7px;overflow:hidden}.reading-layout.without-nav{grid-template-columns:minmax(0,1fr)}.reading-nav{background:#fafbf6;border-right:1px solid #dce2d6;display:flex;flex-direction:column;overflow:hidden}.nav-tabs{display:flex;border-bottom:1px solid #e1e5dc;padding:8px 6px;gap:2px}.nav-tabs button{flex:1;border:0;background:transparent;color:#879080;padding:6px 4px;cursor:pointer;font-size:12px}.nav-tabs button.active{background:#e8efe3;color:#42694d;border-radius:4px}.nav-content{overflow:auto;padding:12px;flex:1}.nav-content>p{font-size:12px;color:#899383;line-height:1.8}.outline-item{display:block;width:100%;text-align:left;border:0;background:transparent;color:#566b56;padding:9px 5px;border-bottom:1px solid #e7ebe1;font-size:12px;cursor:pointer;line-height:1.6}.reading-surface{min-width:0;height:100%;overflow:hidden}.reading-empty{min-height:450px;display:flex;align-items:center;justify-content:center;gap:15px;color:#7d8d78}.reading-notice{margin:5px 0 10px;font-size:12px;color:#856a43}.thumbnails button{display:flex;flex-direction:column;width:100%;align-items:center;background:transparent;border:0;margin-bottom:12px;cursor:pointer;color:#75836d}.thumbnails img{width:100%;border:1px solid #d7ded2;margin-bottom:4px}.annotation{border-bottom:1px solid #dfe5d9;margin:12px 0;padding-bottom:8px}.annotation button{border:0;background:transparent;text-align:left;padding:0;color:#526950;cursor:pointer;width:100%;font-size:12px}.annotation p{line-height:1.7;max-height:100px;overflow:auto}.annotation small{color:#879a7d}.annotation .erase{width:auto;margin-top:8px;color:#9d7460;font-size:10px}.text-reading{display:flex;height:100%;flex-direction:column;background:#fffef8}.text-reading>form{padding:10px 18px;background:#f7f9f3;border-bottom:1px solid #e4e8de;display:flex;align-items:center;gap:8px}.text-reading input,.text-reading button{border:1px solid #d5decf;border-radius:4px;padding:6px 9px;color:#4c644b;background:white}.text-reading form>button[type=button]{min-width:29px;padding-right:6px;padding-left:6px}.text-control-spacer{flex:1}.text-scale{min-width:40px;color:#778376;text-align:center;font:10px/1.2 'DM Mono',monospace;font-variant-numeric:tabular-nums}.text-scroll{overflow:auto;padding:28px max(30px,6%);flex:1;font:16px/1.9 'Noto Serif SC',Georgia,serif;color:#334330}.text-scroll section{scroll-margin:25px;white-space:pre-wrap}.text-scroll :deep(table){border-collapse:collapse;max-width:100%}.text-scroll :deep(td),.text-scroll :deep(th){border:1px solid #d8dfd0;padding:5px 9px}.text-scroll :deep(pre){overflow:auto;background:#f0f3ea;padding:14px;white-space:pre}.selection-tools{display:flex;align-items:center;gap:8px;padding:11px;background:#f2f6ec;border:1px solid #d9e4ce;border-radius:5px;margin-top:10px;font-size:12px;flex-wrap:wrap}.selection-tools span{color:#788970}.selection-tools button,.selection-tools input{padding:6px 8px;border:1px solid #cfdcc5;border-radius:4px;background:white;color:#4b704e;cursor:pointer}.selection-tools input{flex:1;min-width:120px}@media(max-width:1000px){.reading-header{align-items:flex-start;flex-direction:column}.reading-layout{grid-template-columns:175px 1fr}}@media(max-width:650px){.reading-layout{grid-template-columns:130px 1fr}.reading-header h1{font-size:18px}.reading-actions button{font-size:11px}.nav-content{padding:8px}.text-reading>form{padding:8px;gap:5px}.text-reading input{min-width:0;width:30%;}}
</style>

<style scoped>
.reading-page { padding:28px 0 32px; }
.reading-header { align-items:flex-start; margin-bottom:18px; }
.reading-heading-copy { min-width:0; flex:1; }
.reading-heading-copy>a { display:inline-flex; align-items:center; min-height:32px; color:#426c52; font-size:14px; }
.reading-header h1 { max-width:70ch; color:#293d30; font-size:24px; line-height:1.5; overflow-wrap:anywhere; }
.reading-actions { gap:8px; }
.reading-actions button,.reading-actions>a,.bookmark-button { min-height:40px;display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:7px 12px;border:1px solid #d7e1d5;border-radius:8px;color:#42654a;background:#fbfcf8;font-size:13px; }
.reading-actions button:hover,.reading-actions>a:hover,.bookmark-button:hover { background:#edf3eb;border-color:#c4d5c3; }
.reading-layout { height:max(560px,calc(100dvh - 240px)); min-height:500px;grid-template-columns:260px minmax(0,1fr);border:1px solid #dce4d9;border-radius:12px;background:#fffefa; }
.reading-layout.without-nav { grid-template-columns:minmax(0,1fr); }
.reading-nav { background:#f8faf5;border-color:#dfe6dd; }
.nav-tabs { padding:9px 8px;gap:4px; }
.nav-tabs button { min-height:38px;color:#526658;font-size:13px;border-radius:7px; }
.nav-tabs button.active { color:#355b40;background:#e6efe4; }
.nav-content { padding:14px; }
.nav-content>p { color:#53685a;font-size:13px;line-height:1.75; }
.outline-item { padding:10px 6px;color:#405c47;font-size:13px; }
.reading-empty { color:#506958;font-size:15px; }
.reading-notice { padding:10px 12px;border:1px solid #eadfca;border-radius:8px;color:#755b36;background:#faf5e9;font-size:13px; }
.annotation button { color:#405c47;font-size:13px;line-height:1.7; }
.annotation small { color:#5f785e;font-size:12px; }
.text-reading { background:#fffefa; }
.text-reading>form { min-height:56px;padding:9px 15px;background:#f7f9f4; }
.text-reading input,.text-reading button { min-height:38px;border-radius:7px;font-size:13px; }
.text-scroll { padding:34px clamp(24px,7vw,90px);color:#344638;line-height:1.95; }
.text-scroll section { max-width:72ch;margin:0 auto 1.5em; }
.selection-tools { position:sticky;bottom:8px;z-index:3;padding:12px;border:1px solid #d5e2d2;border-radius:10px;background:#f2f7ef;box-shadow:0 8px 24px #263a3014;font-size:13px; }
.selection-tools button,.selection-tools input { min-height:38px;padding:7px 10px;border-radius:7px;font-size:13px; }
@media(max-width:1000px){.reading-header{gap:12px}.reading-layout{grid-template-columns:220px minmax(0,1fr)}.reading-actions{flex-wrap:wrap}}
@media(max-width:650px){.reading-page{padding-top:21px}.reading-header{gap:10px}.reading-header h1{font-size:19px}.reading-actions{width:100%;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.reading-actions button,.reading-actions>a{min-height:38px;padding:5px 7px;font-size:12px}.reading-layout{height:max(560px,calc(100dvh - 288px));min-height:430px;grid-template-columns:minmax(0,1fr);grid-template-rows:155px minmax(0,1fr);border-radius:9px}.reading-layout.without-nav{grid-template-rows:minmax(0,1fr)}.reading-nav{min-width:0;min-height:0;max-height:190px;border-right:0;border-bottom:1px solid #dce2d6}.nav-tabs{flex-wrap:nowrap;padding:6px 4px;gap:3px}.nav-tabs button{min-width:0;min-height:34px;flex:1;padding:4px 2px;font-size:11px}.nav-content{min-height:0;padding:7px;overflow:auto}.outline-item{padding:7px 4px;font-size:11px;overflow-wrap:anywhere}.reading-surface{min-width:0;min-height:0}.reading-header>div:first-child{width:100%}.text-reading>form{flex-wrap:nowrap;padding:7px}.text-reading input{width:auto;min-width:0;flex:1}.text-reading form>button:first-of-type{white-space:nowrap;flex:0 0 auto}.text-scroll{padding:22px 15px}.selection-tools{gap:6px;padding:9px}.selection-tools>span{width:100%}.selection-tools input{min-width:100px}}
</style>
