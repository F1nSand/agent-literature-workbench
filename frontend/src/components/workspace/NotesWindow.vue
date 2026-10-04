<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Color } from '@tiptap/extension-color'
import { FontFamily, FontSize, TextStyle } from '@tiptap/extension-text-style'
import StarterKit from '@tiptap/starter-kit'
import { EditorContent, useEditor } from '@tiptap/vue-3'
import type { NoteContent, NoteVersion } from '../../types/workspace'
import { useWorkspaceNotesStore } from '../../stores/workspaceNotes'
import { useWorkspaceChromeStore } from '../../stores/workspaceChrome'
import SvgIcon from '../SvgIcon.vue'
import NotesHistoryPanel from './NotesHistoryPanel.vue'
import { safeNoteLink } from '../../utils/safeNoteLinks'

const emit = defineEmits<{ close: []; minimize: []; toggleHistory: [] }>()
const store = useWorkspaceNotesStore()
const chrome = useWorkspaceChromeStore()
const router = useRouter()
const styleRevision = ref(0)
const selectedVersion = ref<NoteVersion | null>(null)
const mixedStyle = '__mixed__'
const fontFamilyOptions = ['Noto Sans SC', 'Noto Serif SC', 'Georgia', 'Arial', 'serif', 'sans-serif']
const fontSizeOptions = ['12px', '14px', '16px', '18px', '22px']
const editor = useEditor({
  extensions: [StarterKit.configure({ link: {
    openOnClick: false,
    autolink: false,
    linkOnPaste: false,
    isAllowedUri: (href) => safeNoteLink(href) !== null,
    shouldAutoLink: () => false,
  } }), TextStyle, Color, FontFamily, FontSize],
  content: store.content,
  editorProps: { attributes: { class: 'note-editor-body', 'aria-label': '笔记正文' } },
  onUpdate: ({ editor: current }) => { styleRevision.value++; store.edit(current.getJSON() as NoteContent) },
  onSelectionUpdate: () => { styleRevision.value++ },
  onTransaction: () => { styleRevision.value++ },
})
function selectedStyles() {
  const current = editor.value
  if (!current) return { fontFamily: 'Noto Sans SC', fontSize: '14px', color: '' }
  const { state } = current
  const values: Array<{ fontFamily: string; fontSize: string; color: string }> = []
  const forText = (marks: readonly { type: { name: string }; attrs: Record<string, unknown> }[], blockName: string, blockAttrs: Record<string, unknown>) => {
    const attrs = marks.find((mark) => mark.type.name === 'textStyle')?.attrs ?? {}
    const headingLevel = Number(blockAttrs.level ?? 0)
    const defaultFamily = blockName === 'heading' ? 'Noto Serif SC' : 'Noto Sans SC'
    const defaultSize = blockName === 'heading' ? ({ 1: '19px', 2: '16px', 3: '13px' }[headingLevel] ?? '14px') : blockName === 'codeBlock' ? '9px' : '14px'
    values.push({
      fontFamily: typeof attrs.fontFamily === 'string' && attrs.fontFamily ? attrs.fontFamily : defaultFamily,
      fontSize: typeof attrs.fontSize === 'string' && attrs.fontSize ? attrs.fontSize : defaultSize,
      color: typeof attrs.color === 'string' ? attrs.color : '',
    })
  }
  if (state.selection.empty) {
    const parent = state.selection.$from.parent
    const marks = state.storedMarks ?? state.selection.$from.marks()
    forText(marks, parent.type.name, parent.attrs as Record<string, unknown>)
  } else {
    state.doc.nodesBetween(state.selection.from, state.selection.to, (node, _pos, parent) => {
      if (node.isText) forText(node.marks, parent?.type.name ?? 'paragraph', (parent?.attrs ?? {}) as Record<string, unknown>)
    })
  }
  if (!values.length) return { fontFamily: 'Noto Sans SC', fontSize: '14px', color: '' }
  const common = (key: 'fontFamily' | 'fontSize' | 'color') => {
    const unique = new Set(values.map((value) => value[key]))
    return unique.size === 1 ? values[0]![key] : mixedStyle
  }
  return { fontFamily: common('fontFamily'), fontSize: common('fontSize'), color: common('color') }
}
const currentStyles = computed(() => { styleRevision.value; return selectedStyles() })
const savingLabel = computed(() => ({ idle: '', loading: '正在载入…', saved: '已同步', offline: '等待网络同步', conflict: '版本冲突，草稿仍保留', error: '保存未完成' }[store.status]))

watch(() => store.content, (value) => {
  if (editor.value && JSON.stringify(editor.value.getJSON()) !== JSON.stringify(value)) editor.value.commands.setContent(value, { emitUpdate: false })
}, { deep: true })
watch(() => store.currentId, () => { selectedVersion.value = null; if (editor.value) editor.value.commands.setContent(store.content, { emitUpdate: false }) })
watch(() => store.currentArchived, (archived) => editor.value?.setEditable(!archived))
onBeforeUnmount(() => { editor.value?.destroy() })

function setColor(value: string) {
  const chain = editor.value?.chain().focus()
  if (!chain) return
  if (value) chain.setColor(value).run()
  else chain.unsetColor().run()
}
function setFontFamily(value: string) { if (value && value !== mixedStyle) editor.value?.chain().focus().setFontFamily(value).run() }
function setFontSize(value: string) { if (value && value !== mixedStyle) editor.value?.chain().focus().setFontSize(value).run() }
function handleNoteLink(event: MouseEvent) {
  const anchor = (event.target as HTMLElement | null)?.closest('a')
  if (!anchor) return
  const safeHref = safeNoteLink(anchor.getAttribute('href'))
  event.preventDefault()
  if (!safeHref) return
  if (safeHref.startsWith('/documents/')) {
    const url = new URL(safeHref, window.location.origin)
    void router.push({ path: url.pathname, query: Object.fromEntries(url.searchParams.entries()) })
  } else if (safeHref.startsWith('mailto:')) window.location.href = safeHref
  else window.open(safeHref, '_blank', 'noopener,noreferrer')
}
async function createNote() { await store.flush(); await store.create(); chrome.setHistory('notes', false) }
function openHistory() { chrome.setHistory('notes', true) }
async function retryFailure() {
  if (store.dirty) await store.save()
  else if (store.currentId) await store.select(store.currentId)
  else await store.load()
}
function keepLocalDraft() {
  if (!window.confirm('这会用当前本地草稿生成服务器新版本。')) return
  void store.saveLocalOnLatestBase()
}
</script>

<template>
  <Transition name="workspace-panel"><NotesHistoryPanel v-if="chrome.windows.notes.history" /></Transition>
  <section v-show="!chrome.windows.notes.history" class="workspace-window-content note-window-content">
    <div class="note-compact-actions">
      <div class="workspace-heading-actions">
        <button class="workspace-icon-button" title="历史笔记" aria-label="历史笔记" @click="openHistory"><SvgIcon name="history" :size="17" /></button>
        <button class="workspace-icon-button" title="新建笔记" aria-label="新建笔记" @click="createNote"><SvgIcon name="plus" :size="17" /></button>
      </div>
    </div>

    <div v-if="['offline', 'error'].includes(store.status) && store.error" class="workspace-callout is-warning" role="alert">
      {{ store.error }} <button v-if="!store.currentId" class="note-recovery-action" @click="retryFailure">重试载入</button>
    </div>

    <div v-else-if="!store.currentId" class="workspace-empty">
      <div class="workspace-empty-mark">记</div><h3>为想法留一页空白</h3><p>每条笔记自动保存并生成版本，AI 修改会先以候选形式呈现。</p><button class="workspace-primary" @click="createNote">新建笔记 <span>＋</span></button>
    </div>

    <template v-if="store.currentId">
      <div class="note-format-toolbar" :class="{ 'toolbar-readonly': store.currentArchived }" role="toolbar" aria-label="文字样式">
        <button :disabled="store.currentArchived" :class="{ active: editor?.isActive('bold') }" aria-label="粗体" title="粗体" @click="editor?.chain().focus().toggleBold().run()"><b>B</b></button>
        <button :disabled="store.currentArchived" :class="{ active: editor?.isActive('italic') }" aria-label="斜体" title="斜体" @click="editor?.chain().focus().toggleItalic().run()"><i>I</i></button>
        <button :disabled="store.currentArchived" :class="{ active: editor?.isActive('underline') }" aria-label="下划线" title="下划线" @click="editor?.chain().focus().toggleUnderline().run()"><u>U</u></button>
        <button :disabled="store.currentArchived" :class="{ active: editor?.isActive('bulletList') }" aria-label="项目列表" title="项目列表" @click="editor?.chain().focus().toggleBulletList().run()">☷</button>
        <button :disabled="store.currentArchived" :class="{ active: editor?.isActive('heading', { level: 2 }) }" aria-label="小标题" title="小标题" @click="editor?.chain().focus().toggleHeading({ level: 2 }).run()">H₂</button>
        <select :value="currentStyles.fontFamily" class="note-style-select font-select" aria-label="字体" title="字体" :disabled="store.currentArchived" @change="setFontFamily(($event.target as HTMLSelectElement).value)"><option v-if="currentStyles.fontFamily === mixedStyle" :value="mixedStyle" disabled>混合样式</option><option v-else-if="!fontFamilyOptions.includes(currentStyles.fontFamily)" :value="currentStyles.fontFamily" disabled>{{ currentStyles.fontFamily }}（当前样式）</option><option v-for="family in fontFamilyOptions" :key="family">{{ family }}</option></select>
        <select :value="currentStyles.fontSize" class="note-style-select size-select" aria-label="字号" title="字号" :disabled="store.currentArchived" @change="setFontSize(($event.target as HTMLSelectElement).value)"><option v-if="currentStyles.fontSize === mixedStyle" :value="mixedStyle" disabled>混合样式</option><option v-else-if="!fontSizeOptions.includes(currentStyles.fontSize)" :value="currentStyles.fontSize" disabled>{{ currentStyles.fontSize }}（当前样式）</option><option v-for="size in fontSizeOptions" :key="size">{{ size }}</option></select>
        <select :value="currentStyles.color" class="note-style-select color-select" aria-label="文字颜色" title="文字颜色" :disabled="store.currentArchived" @change="setColor(($event.target as HTMLSelectElement).value)"><option v-if="currentStyles.color === mixedStyle" :value="mixedStyle" disabled>混合颜色</option><option value="">默认颜色</option><option value="#416c5a">松绿</option><option value="#826743">赭金</option><option value="#557585">雾蓝</option><option value="#b05d4b">砖红</option><option value="#26332f">墨色</option></select>
        <span class="toolbar-spacer"></span><button class="history-action" @click="store.loadHistory">版本</button>
      </div>
      <EditorContent :editor="editor" class="note-editor" @click="handleNoteLink" />
      <div class="note-savebar">
        <span :class="['note-save-state', `state-${store.status}`]">{{ savingLabel }}</span>
        <span class="note-revision">版本 {{ store.revision }}</span>
        <button v-if="store.status === 'conflict'" class="note-recovery-action" @click="store.reloadServerVersion">载入服务器版本</button>
        <button v-if="store.status === 'conflict'" class="note-recovery-action" @click="keepLocalDraft">以本地草稿保存新版本</button>
        <button v-else-if="['offline', 'error'].includes(store.status)" class="note-recovery-action" @click="retryFailure">{{ store.dirty ? '重试同步' : '重新载入' }}</button>
      </div>

      <div v-if="store.candidate" class="candidate-review">
        <div class="candidate-review-header"><div><strong>修改候选 · 基于版本 {{ store.candidate.base_revision }}</strong></div><button class="workspace-icon-button" aria-label="关闭候选预览" @click="store.candidate = null">×</button></div>
        <div class="candidate-diff"><div><small>当前笔记</small><pre>{{ store.versionDiff.before || '（空白）' }}</pre></div><div><small>建议内容</small><pre>{{ store.versionDiff.after || '（空白）' }}</pre></div></div>
        <p v-if="store.revision !== store.candidate.base_revision" class="candidate-conflict">笔记已在候选生成后修改。采纳会检查版本并保留你的编辑。</p>
        <div class="candidate-footer"><button class="workspace-quiet-button" @click="store.candidate = null">暂不采纳</button><button class="workspace-primary" @click="store.adoptCandidate">查看并采纳</button></div>
      </div>

      <div v-if="store.versions.length" class="note-history">
        <div class="note-history-heading"><strong>版本历史</strong><button class="workspace-icon-button" aria-label="关闭版本历史" @click="store.versions = []; selectedVersion = null">×</button></div>
        <button v-for="version in store.versions" :key="version.id" class="note-version-row" @click="selectedVersion = version">
          <span>版本 {{ version.version_number }} <small>{{ version.origin === 'generated' ? 'AI 候选' : '手动保存' }}</small></span><time>{{ new Date(version.created_at).toLocaleString('zh-CN') }}</time>
        </button>
        <div v-if="selectedVersion" class="version-preview"><div class="version-preview-head"><strong>版本 {{ selectedVersion.version_number }}</strong><button class="workspace-icon-button" aria-label="关闭版本预览" @click="selectedVersion = null">×</button></div><pre>{{ selectedVersion.plain_text || '（空白笔记）' }}</pre></div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.note-compact-actions { display:flex; justify-content:flex-end; min-height:38px; padding:5px 8px 0; }
</style>
