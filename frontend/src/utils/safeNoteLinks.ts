const uuid = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

export function safeNoteLink(href: string | null | undefined): string | null {
  if (!href || href.length > 2048 || /[\u0000-\u001f\\]/.test(href)) return null
  if (/^https?:\/\//i.test(href)) {
    try { const url = new URL(href); return url.protocol === 'http:' || url.protocol === 'https:' ? href : null }
    catch { return null }
  }
  if (/^mailto:[^\s<>]+$/i.test(href)) return href
  if (href.startsWith('//') || !href.startsWith('/')) return null
  try {
    const url = new URL(href, 'https://research-desk.invalid')
    if (url.origin !== 'https://research-desk.invalid' || !new RegExp(`^/documents/${uuid}$`).test(url.pathname)) return null
    const allowed = new Set(['version', 'page', 'anchor', 'chapter', 'cfi'])
    for (const key of url.searchParams.keys()) if (!allowed.has(key)) return null
    const versions = url.searchParams.getAll('version')
    const pages = url.searchParams.getAll('page')
    if (versions.length > 1 || pages.length > 1) return null
    if (versions.length !== 1 || !new RegExp(`^${uuid}$`).test(versions[0])) return null
    const anchors = url.searchParams.getAll('anchor')
    const chapters = url.searchParams.getAll('chapter')
    const cfis = url.searchParams.getAll('cfi')
    if ([pages, anchors, chapters, cfis].some((values) => values.length > 1)) return null
    const primaryLocators = Number(pages.length > 0) + Number(anchors.length > 0) + Number(chapters.length > 0)
    if (primaryLocators > 1 || (cfis.length > 0 && pages.length > 0) || (cfis.length > 0 && anchors.length > 0)) return null
    if (cfis.length && chapters.length !== 1) return null
    if (pages.length && !/^[1-9]\d*$/.test(pages[0])) return null
    if (anchors.length && !/^p-[1-9]\d*$/.test(anchors[0])) return null
    if (chapters.length && (chapters[0].length > 500 || chapters[0].includes('..') || chapters[0].includes('://') || /[\u0000-\u001f]/.test(chapters[0]))) return null
    if (cfis.length && (cfis[0].length > 2000 || !cfis[0].startsWith('epubcfi(') || !cfis[0].endsWith(')') || /[\u0000-\u001f]/.test(cfis[0]))) return null
    return `${url.pathname}${url.search}`
  } catch { return null }
}
