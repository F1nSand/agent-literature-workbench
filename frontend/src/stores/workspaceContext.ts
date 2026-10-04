import { defineStore } from 'pinia'
import { computed, ref, toRaw } from 'vue'
import type { Locator, PageContext } from '../types/workspace'

const emptyContext = (): PageContext => ({ page_type: 'assistant' })

export const useWorkspaceContextStore = defineStore('workspace-context', () => {
  const context = ref<PageContext>(emptyContext())
  const copy = <T>(value: T): T => JSON.parse(JSON.stringify(toRaw(value))) as T
  const pageContext = computed(() => copy(context.value))

  function setPageType(pageType: PageContext['page_type']) {
    context.value = { page_type: pageType, note_id: context.value.note_id, note_revision: context.value.note_revision }
  }

  function setReaderContext(input: {
    documentId: string
    versionId: string
    locator?: Locator
    pageType?: 'reader'
  }) {
    context.value = {
      page_type: input.pageType ?? 'reader',
      document_id: input.documentId,
      version_id: input.versionId,
      ...(input.locator ? { locator: copy(input.locator) } : {}),
      note_id: context.value.note_id,
      note_revision: context.value.note_revision,
      selection: undefined,
    }
  }

  function setLocator(locator: Locator) {
    context.value = { ...context.value, locator: copy(locator) }
  }

  function setSelection(text: string, locator?: Locator, bbox?: number[]) {
    void bbox
    context.value = {
      ...context.value,
      ...(locator ? { locator: copy(locator) } : {}),
      selection: text ? text.slice(0, 4000) : undefined,
    }
  }

  function setNoteContext(noteId?: string, noteRevision?: number) {
    context.value = { ...context.value, note_id: noteId, note_revision: noteRevision }
  }

  return { context, pageContext, setPageType, setReaderContext, setLocator, setSelection, setNoteContext }
})
