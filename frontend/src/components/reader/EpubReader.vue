<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { Book } from 'epubjs'
import type { Locator } from '../../types/workspace'
import type { ReaderSection } from '../../api/reader'
const props = defineProps<{ url: string; locator: Locator; sections: ReaderSection[] }>()
const emit = defineEmits<{ location: [locator: Locator]; selection: [text: string, locator: Locator] }>()
const host = ref<HTMLDivElement>(), message = ref(''), fontSize = ref(100)
let book: Book | undefined
let rendition: ReturnType<NonNullable<typeof book>['renderTo']> | undefined
let lastCfi = ''
function sourcePath(value: string) {
  try { return decodeURIComponent(value).replace(/^\/+/, '') }
  catch { return value.replace(/^\/+/, '') }
}
function display(locator: Locator) {
  let target = locator.cfi
  if (!target && typeof locator.chapter === 'string' && book) {
    // Stored chapter paths include the package directory; EPUB.js indexes OPF-relative hrefs.
    const chapter = sourcePath(locator.chapter)
    const matches: string[] = []
    book.spine.each((section: { href: string }) => {
      const href = sourcePath(section.href)
      if (chapter === href || chapter.endsWith(`/${href}`)) matches.push(section.href)
    })
    if (matches.length === 1) target = matches[0]
  }
  if (typeof target !== 'string') { message.value = '这一阅读位置暂不可用。'; return }
  void rendition?.display(target).then(() => { message.value = '' }).catch(() => { message.value = '这一阅读位置暂不可用。' })
}
onMounted(async () => {
  try {
    book = new Book(props.url, { openAs: 'epub' })
    rendition = book.renderTo(host.value!, { width: '100%', height: '100%', flow: 'paginated', allowScriptedContent: false, spread: 'auto' })
    rendition.themes.default({ body: { 'font-family': 'Georgia, "Noto Serif SC", serif', 'line-height': '1.8', 'color': '#26392d' } })
    rendition.on('relocated', (location: { start: { href: string; cfi: string } }) => {
      lastCfi = location.start.cfi
      const href = sourcePath(location.start.href.split('#')[0]!)
      const chapter = props.sections.find(s => sourcePath(String(s.locator.chapter)).endsWith(`/${href}`) || sourcePath(String(s.locator.chapter)) === href)
      emit('location', { kind: 'epub', chapter: chapter?.locator.chapter || location.start.href, cfi: location.start.cfi, section: chapter?.number })
    })
    rendition.on('selected', (cfi: string, contents: { range: (cfi: string) => Range }) => {
      try { emit('selection', contents.range(cfi).toString(), { ...props.locator, cfi }) } catch { /* Ignore stale frame selections. */ }
    })
    await book.ready
    if (props.locator.cfi || props.locator.chapter) display(props.locator)
    else await rendition.display()
  } catch { message.value = 'EPUB 暂时无法显示，请检查文件后重试。' }
})
watch(() => props.locator, (next, prev) => { if (next.cfi && next.cfi === lastCfi) return; if (JSON.stringify(next) !== JSON.stringify(prev)) display(next) })
onBeforeUnmount(() => { rendition?.destroy(); book?.destroy() })
function font() { rendition?.themes.fontSize(`${fontSize.value}%`) }
function adjustFont(delta: number) { fontSize.value = Math.min(200, Math.max(70, fontSize.value + delta)); font() }
</script>
<template>
  <div class="epub-reading"><div class="epub-tools"><button @click="rendition?.prev()">上一屏</button><span>EPUB · 按章节定位</span><div class="epub-zoom"><button aria-label="缩小字号" title="缩小字号" @click="adjustFont(-10)">−</button><span aria-live="polite">{{ fontSize }}%</span><button aria-label="放大字号" title="放大字号" @click="adjustFont(10)">＋</button><label>字号 <select v-model="fontSize" @change="font"><option :value="85">85%</option><option :value="100">100%</option><option :value="120">120%</option><option :value="150">150%</option><option v-if="![85,100,120,150].includes(fontSize)" :value="fontSize">{{ fontSize }}%</option></select></label></div><button @click="rendition?.next()">下一屏</button></div><p v-if="message" role="alert">{{ message }}</p><div ref="host" class="epub-host"></div></div>
</template>
<style scoped>
.epub-reading{height:100%;display:flex;flex-direction:column;background:#fffef9}.epub-tools{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 14px;border-bottom:1px solid #e0e5dc;color:#6d7b6d;font-size:12px}.epub-tools button,.epub-tools select{background:white;border:1px solid #d6ded2;border-radius:4px;color:#47644f;padding:5px 9px;cursor:pointer}.epub-zoom{display:flex;align-items:center;gap:5px}.epub-zoom span{min-width:38px;text-align:center;font-variant-numeric:tabular-nums}.epub-zoom label{display:flex;align-items:center;gap:4px}.epub-host{flex:1;min-height:0}.epub-reading>p{color:#966649;padding:8px 16px}@media(max-width:650px){.epub-tools{flex-wrap:wrap;justify-content:center}.epub-tools>span{display:none}}
</style>
