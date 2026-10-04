<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  adoptStudyArtifactVersion,
  compareStudyArtifactVersions,
  createStudyArtifact,
  downloadStudyArtifact,
  listStudyArtifactVersions,
  listTopicArtifacts,
  listTopics,
  updateStudyArtifact,
  type StudyArtifact,
  type StudyArtifactVersion,
  type TopicSummary,
} from '../api/workbench'

const topics = ref<TopicSummary[]>([])
const artifacts = ref<StudyArtifact[]>([])
const selectedId = ref('')
const versions = ref<StudyArtifactVersion[]>([])
const titleDraft = ref('')
const bodyDraft = ref('')
const newTitle = ref('')
const newBody = ref('')
const newTopicId = ref('')
const newType = ref<'note' | 'comparison_table' | 'draft'>('note')
const baseVersionId = ref('')
const compareVersionId = ref('')
const diffText = ref('')
const loading = ref(true)
const saving = ref(false)
const exporting = ref<'markdown' | 'csv' | ''>('')
const error = ref('')
const notice = ref('')

const selected = computed(() => artifacts.value.find((item) => item.id === selectedId.value))
const dirty = computed(() => Boolean(selected.value && selected.value.current_version && (
  titleDraft.value !== selected.value.title || bodyDraft.value !== selected.value.current_version.body_text
)))

function topicTitle(topicId: string) {
  return topics.value.find((item) => item.id === topicId)?.title ?? '专题'
}

async function loadVersions(artifactId: string) {
  const artifact = artifacts.value.find((item) => item.id === artifactId)
  if (!artifact) return
  const rows = await listStudyArtifactVersions(artifactId)
  versions.value = rows
  titleDraft.value = artifact.title
  bodyDraft.value = artifact.current_version?.body_text ?? ''
  const current = artifact.current_version_id ?? rows[0]?.id ?? ''
  const candidate = rows.find((item) => item.status === 'candidate')?.id ?? current
  baseVersionId.value = current
  compareVersionId.value = candidate
  diffText.value = ''
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const topicList = await listTopics()
    topics.value = topicList
    const lists = await Promise.all(topicList.map((topic) => listTopicArtifacts(topic.id)))
    artifacts.value = lists.flat()
    if (!newTopicId.value && topicList.length) newTopicId.value = topicList[0].id
    const next = selectedId.value && artifacts.value.some((item) => item.id === selectedId.value)
      ? selectedId.value
      : artifacts.value[0]?.id ?? ''
    selectedId.value = next
    if (next) await loadVersions(next)
    else versions.value = []
  } catch (err) {
    error.value = err instanceof Error ? err.message : '暂时无法读取学习成果。'
  } finally {
    loading.value = false
  }
}

async function selectArtifact(id: string) {
  selectedId.value = id
  notice.value = ''
  error.value = ''
  try { await loadVersions(id) } catch (err) {
    error.value = err instanceof Error ? err.message : '暂时无法读取成果版本。'
  }
}

async function createArtifact() {
  if (!newTopicId.value || !newTitle.value.trim() || saving.value) return
  saving.value = true
  error.value = ''
  try {
    const created = await createStudyArtifact(newTopicId.value, {
      title: newTitle.value.trim(), artifact_type: newType.value, body_text: newBody.value,
    })
    newTitle.value = ''
    newBody.value = ''
    notice.value = '成果已保存。'
    await refresh()
    await selectArtifact(created.id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '暂时无法保存成果。'
  } finally { saving.value = false }
}

async function saveCurrent() {
  if (!selected.value || !dirty.value || saving.value) return
  saving.value = true
  error.value = ''
  try {
    const updated = await updateStudyArtifact(selected.value.id, {
      expected_revision: selected.value.revision, title: titleDraft.value.trim(), body_text: bodyDraft.value,
    })
    artifacts.value = artifacts.value.map((item) => item.id === updated.id ? updated : item)
    await loadVersions(updated.id)
    notice.value = '已保存为新版本。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '暂时无法保存更改。'
  } finally { saving.value = false }
}

function exportFilename(title: string, extension: 'md' | 'csv') {
  let base = title.replace(/[\u0000-\u001f\u007f<>:"\/\\|?*]/g, '_').replace(/[. ]+$/g, '').slice(0, 120)
  if (!base || /^\.+$/.test(base)) base = '学习成果'
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$/i.test(base)) base = `_${base}`
  return `${base}.${extension}`
}

async function exportCurrent(format: 'markdown' | 'csv') {
  const artifact = selected.value
  if (!artifact?.current_version || dirty.value || exporting.value) return
  exporting.value = format
  error.value = ''
  notice.value = ''
  try {
    const blob = await downloadStudyArtifact(artifact.id, format)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = exportFilename(artifact.title, format === 'markdown' ? 'md' : 'csv')
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    notice.value = format === 'markdown' ? 'Markdown 文件已导出。' : 'CSV 文件已导出。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '暂时无法导出这份成果。'
  } finally {
    exporting.value = ''
  }
}

async function compareVersions() {
  if (!selected.value || !baseVersionId.value || !compareVersionId.value || baseVersionId.value === compareVersionId.value) return
  error.value = ''
  try {
    const result = await compareStudyArtifactVersions(selected.value.id, baseVersionId.value, compareVersionId.value)
    diffText.value = result.unified_diff || '两个版本没有正文差异。'
  } catch (err) { error.value = err instanceof Error ? err.message : '暂时无法比较版本。' }
}

async function adopt(version: StudyArtifactVersion) {
  if (!selected.value || saving.value) return
  saving.value = true
  error.value = ''
  try {
    const updated = await adoptStudyArtifactVersion(selected.value.id, {
      expected_revision: selected.value.revision, version_id: version.id,
    })
    artifacts.value = artifacts.value.map((item) => item.id === updated.id ? updated : item)
    await loadVersions(updated.id)
    notice.value = `已将第 ${version.version_number} 版设为当前版本。`
  } catch (err) { error.value = err instanceof Error ? err.message : '暂时无法采纳此版本。' }
  finally { saving.value = false }
}

watch(() => artifacts.value.map((item) => item.id).join(','), () => {
  if (!artifacts.value.some((item) => item.id === selectedId.value)) selectedId.value = artifacts.value[0]?.id ?? ''
})

onMounted(refresh)
</script>

<template>
  <div class="page-content artifacts-page">
    <div class="eyebrow"><span class="eyebrow-line"></span> YOUR RESEARCH</div>
    <header class="artifacts-heading">
      <div><h1>学习成果</h1><p>整理阅读笔记与研究草稿，保留版本和文献出处。</p></div>
      <button class="artifact-secondary" type="button" :disabled="loading" @click="refresh">{{ loading ? '载入中…' : '刷新' }}</button>
    </header>

    <p v-if="error" class="artifact-alert" role="alert">{{ error }}</p>
    <p v-if="notice" class="artifact-notice" role="status">{{ notice }}</p>

    <section class="artifact-create panel" aria-labelledby="new-artifact-title">
      <div class="artifact-section-heading"><div><span class="section-kicker">NEW NOTE</span><h2 id="new-artifact-title">保存一份成果</h2></div></div>
      <form class="artifact-create-form" @submit.prevent="createArtifact">
        <label>所属专题<select v-model="newTopicId" :disabled="!topics.length"><option v-for="topic in topics" :key="topic.id" :value="topic.id">{{ topic.title }}</option></select></label>
        <label>成果类型<select v-model="newType"><option value="note">阅读笔记</option><option value="comparison_table">文献比较</option><option value="draft">研究草稿</option></select></label>
        <label class="artifact-field-wide">名称<input v-model="newTitle" maxlength="500" placeholder="为这份成果命名" /></label>
        <label class="artifact-field-wide">正文<textarea v-model="newBody" rows="4" maxlength="200000" placeholder="记录要保留的想法、比较或草稿内容" /></label>
        <div class="artifact-form-footer"><span>也可以在专题研读中直接保存回答。</span><button class="workspace-primary" type="submit" :disabled="saving || !topics.length || !newTitle.trim()">{{ saving ? '正在保存…' : '保存成果' }}</button></div>
      </form>
    </section>

    <div v-if="loading" class="artifact-loading panel"><span class="topic-spinner"></span>正在载入成果…</div>
    <div v-else-if="!artifacts.length" class="artifact-empty panel">
      <div class="empty-illustration"><span class="empty-illustration-ring"></span><span class="empty-illustration-mark">▧</span></div>
      <h2>{{ topics.length ? '还没有保存的成果' : '先创建一个专题' }}</h2>
      <p>{{ topics.length ? '保存笔记，或从专题研读中收集回答。' : '学习成果会归属到专题中，创建专题后即可开始整理。' }}</p>
      <RouterLink v-if="!topics.length" class="stage-home-link" to="/topics">前往专题研究 <span>→</span></RouterLink>
    </div>
    <div v-else class="artifact-workspace">
      <aside class="artifact-list panel" aria-label="成果列表">
        <div class="artifact-list-heading"><span class="section-kicker">SAVED WORK</span><strong>{{ artifacts.length }} 份成果</strong></div>
        <button v-for="item in artifacts" :key="item.id" type="button" class="artifact-choice" :class="{ selected: selectedId === item.id }" @click="selectArtifact(item.id)">
          <span class="artifact-choice-mark">{{ item.artifact_type === 'comparison_table' ? '比' : item.artifact_type === 'draft' ? '稿' : '记' }}</span>
          <span class="artifact-choice-copy"><strong>{{ item.title }}</strong><small>{{ topicTitle(item.topic_id) }} · {{ item.current_version ? `第 ${item.current_version.version_number} 版` : '尚无正文' }}</small></span>
        </button>
      </aside>

      <main v-if="selected" class="artifact-editor panel">
        <div class="artifact-editor-header"><div><span class="section-kicker">{{ topicTitle(selected.topic_id) }}</span><p class="artifact-version-note">当前版本 {{ selected.current_version?.version_number ?? '—' }} · 共 {{ versions.length }} 个版本</p></div><span v-if="selected.current_version?.origin === 'generated'" class="artifact-origin">来自专题研读</span></div>
        <label class="artifact-editor-label">名称<input v-model="titleDraft" maxlength="500" /></label>
        <label class="artifact-editor-label">正文<textarea v-model="bodyDraft" rows="12" maxlength="200000" /></label>
        <div class="artifact-editor-actions artifact-export-actions">
          <span>{{ dirty ? '先保存更改，再导出。' : '导出当前保存版本。' }}</span>
          <div class="artifact-action-buttons">
            <button class="artifact-secondary" type="button" :disabled="saving || Boolean(exporting) || dirty || !selected.current_version" @click="exportCurrent('markdown')">{{ exporting === 'markdown' ? '正在准备…' : '导出 Markdown' }}</button>
            <button class="artifact-secondary" type="button" :disabled="saving || Boolean(exporting) || dirty || !selected.current_version" @click="exportCurrent('csv')">{{ exporting === 'csv' ? '正在准备…' : '导出 CSV' }}</button>
            <button class="workspace-primary" type="button" :disabled="saving || Boolean(exporting) || !dirty || !titleDraft.trim()" @click="saveCurrent">{{ saving ? '正在保存…' : '保存新版本' }}</button>
          </div>
        </div>

        <section class="artifact-versions" aria-labelledby="versions-heading">
          <div class="artifact-section-heading"><div><span class="section-kicker">VERSIONS</span><h2 id="versions-heading">版本记录</h2></div></div>
          <article v-for="version in versions" :key="version.id" class="artifact-version-row">
            <div class="artifact-version-info"><strong>第 {{ version.version_number }} 版</strong><span>{{ version.status === 'current' ? '当前版本' : version.status === 'candidate' ? '待采纳候选' : '历史版本' }} · {{ version.origin === 'generated' ? '研读生成' : '手动编辑' }}</span></div>
            <button v-if="version.status === 'candidate'" type="button" class="artifact-secondary" :disabled="saving" @click="adopt(version)">采纳此版本</button>
          </article>
          <div v-if="versions.length > 1" class="artifact-compare">
            <label>比较<select v-model="baseVersionId"><option v-for="version in versions" :key="`base-${version.id}`" :value="version.id">第 {{ version.version_number }} 版</option></select></label>
            <span aria-hidden="true">→</span>
            <label>与<select v-model="compareVersionId"><option v-for="version in versions" :key="`compare-${version.id}`" :value="version.id">第 {{ version.version_number }} 版</option></select></label>
            <button type="button" class="artifact-secondary" :disabled="!baseVersionId || !compareVersionId || baseVersionId === compareVersionId" @click="compareVersions">查看差异</button>
          </div>
          <pre v-if="diffText" class="artifact-diff">{{ diffText }}</pre>
          <p v-if="selected.current_version?.evidence_snapshot.length" class="artifact-evidence-note">此成果保留了 {{ selected.current_version.evidence_snapshot.length }} 条来源线索；请通过专题研读中的引用链接回到原文核对。</p>
          <div v-if="selected.current_version?.evidence_snapshot.length" class="artifact-source-links" aria-label="来源文献页">
            <RouterLink v-for="source in selected.current_version.evidence_snapshot" :key="source.evidence_id" :to="`/documents/${encodeURIComponent(source.document_id)}?page=${source.page_number}`">原文第 {{ source.page_number }} 页 ↗</RouterLink>
          </div>
        </section>
      </main>
    </div>
  </div>
</template>

<style scoped>
.artifacts-page{padding:42px 0 38px}.artifacts-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin:13px 0 21px}.artifacts-heading h1{margin:0 0 6px;color:#28362f;font:500 27px/1.4 'Noto Serif SC',serif}.artifacts-heading p{margin:0;color:#899187;font-size:11px;line-height:1.8}.panel{background:#fffefa;border:1px solid #e8eae3;border-radius:7px}.artifact-alert,.artifact-notice{margin:0 0 12px;padding:10px 12px;border-radius:4px;font-size:10px;line-height:1.7}.artifact-alert{border:1px solid #edd7cd;color:#945e4b;background:#fcf5f1}.artifact-notice{border:1px solid #dfe9dc;color:#5d775f;background:#f4f8f1}.artifact-create{padding:18px 20px;margin-bottom:14px}.artifact-section-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:13px}.artifact-section-heading h2{margin:4px 0 0;color:#3a483e;font:500 14px 'Noto Serif SC',serif}.section-kicker{color:#839184;font:8px 'DM Mono',monospace;letter-spacing:.16em}.artifact-create-form{display:grid;grid-template-columns:1fr 1fr;gap:10px 12px}.artifact-create-form label,.artifact-editor-label{display:flex;flex-direction:column;gap:5px;color:#737f73;font-size:9px}.artifact-create-form input,.artifact-create-form textarea,.artifact-create-form select,.artifact-editor-label input,.artifact-editor-label textarea,.artifact-compare select{width:100%;padding:8px 9px;border:1px solid #e5e9e0;border-radius:4px;color:#455249;background:#fff;font:10px/1.7 'Noto Sans SC',sans-serif;outline:0}.artifact-create-form textarea,.artifact-editor-label textarea{resize:vertical}.artifact-create-form input:focus,.artifact-create-form textarea:focus,.artifact-create-form select:focus,.artifact-editor-label input:focus,.artifact-editor-label textarea:focus{border-color:#a8bba6}.artifact-field-wide{grid-column:1/-1}.artifact-form-footer,.artifact-editor-actions{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:1px}.artifact-form-footer>span,.artifact-editor-actions>span{color:#9a9f96;font-size:8px}.workspace-primary{min-height:31px;padding:0 11px;border:1px solid #4a725b;border-radius:4px;color:#fffdf7;background:#4a725b;font-size:9px;cursor:pointer}.workspace-primary:hover{background:#365d47}.workspace-primary:disabled,.artifact-secondary:disabled{opacity:.5;cursor:default}.artifact-secondary{padding:6px 9px;border:1px solid #e1e6dc;border-radius:4px;color:#607565;background:#fbfbf7;font-size:8px;cursor:pointer}.artifact-loading,.artifact-empty{min-height:240px;display:flex;align-items:center;justify-content:center;gap:9px;color:#899187;font-size:10px}.artifact-empty{flex-direction:column;text-align:center}.artifact-empty .empty-illustration{width:68px;height:68px;position:relative;display:grid;place-items:center;margin-bottom:5px;border-radius:50%;background:#eff2eb}.empty-illustration-ring{position:absolute;inset:8px;border:1px solid #d7ded2;border-radius:50%}.empty-illustration-mark{color:#6d8975;font:23px 'Noto Serif SC',serif}.artifact-empty h2{margin:0;color:#3c4940;font:500 14px 'Noto Serif SC',serif}.artifact-empty p{margin:0;color:#92998f;font-size:9px;line-height:1.8}.artifact-workspace{display:grid;grid-template-columns:minmax(200px,260px) minmax(0,1fr);align-items:start;gap:13px}.artifact-list{position:sticky;top:15px;overflow:hidden;padding:14px 10px}.artifact-list-heading{display:flex;align-items:center;justify-content:space-between;padding:0 4px 10px;border-bottom:1px solid #eceee7}.artifact-list-heading>strong{color:#9a9f96;font-size:8px;font-weight:400}.artifact-choice{width:100%;display:flex;align-items:center;gap:9px;margin-top:7px;padding:9px 7px;border:1px solid transparent;border-radius:5px;color:#59665c;background:transparent;text-align:left;cursor:pointer}.artifact-choice:hover{background:#f7f8f3}.artifact-choice.selected{border-color:#dce7d9;background:#f0f4ed}.artifact-choice-mark{width:27px;height:27px;display:grid;flex:0 0 auto;place-items:center;border:1px solid #e3e8df;border-radius:50%;color:#708677;background:#fff;font:10px 'Noto Serif SC',serif}.artifact-choice-copy{min-width:0;display:flex;flex-direction:column;gap:4px}.artifact-choice-copy strong{overflow:hidden;color:#4c5b50;font-size:9px;text-overflow:ellipsis;white-space:nowrap}.artifact-choice-copy small{overflow:hidden;color:#9ca39a;font-size:7px;text-overflow:ellipsis;white-space:nowrap}.artifact-editor{min-width:0;padding:18px 19px}.artifact-editor-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #eceee7}.artifact-version-note{margin:5px 0 0;color:#999f96;font-size:8px}.artifact-origin{padding:5px 7px;border-radius:3px;color:#5f7863;background:#f0f5ed;font-size:8px}.artifact-editor-label{margin-bottom:11px}.artifact-editor-label textarea{min-height:150px}.artifact-editor-actions{padding:10px 0 16px;border-bottom:1px solid #eceee7}.artifact-versions{padding-top:15px}.artifact-versions .artifact-section-heading{margin-bottom:8px}.artifact-version-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 2px;border-bottom:1px solid #f0f1ec}.artifact-version-info{display:flex;flex-direction:column;gap:4px}.artifact-version-info strong{color:#59685c;font-size:9px;font-weight:600}.artifact-version-info span{color:#9a9f96;font-size:8px}.artifact-compare{display:flex;align-items:center;gap:8px;margin-top:14px}.artifact-compare label{display:flex;align-items:center;gap:5px;color:#818980;font-size:8px}.artifact-compare select{min-width:94px;padding:5px 7px;font-size:8px}.artifact-compare>span{color:#a4aaa1}.artifact-diff{overflow:auto;max-height:280px;margin:12px 0 0;padding:12px;border:1px solid #eceee7;border-radius:4px;color:#536154;background:#fafbf7;font:9px/1.8 'DM Mono',monospace;white-space:pre-wrap;overflow-wrap:anywhere}.artifact-evidence-note{margin:12px 0 0;color:#939b91;font-size:8px;line-height:1.7}@media(max-width:760px){.artifact-workspace{grid-template-columns:1fr}.artifact-list{position:static;display:flex;gap:5px;overflow:auto;padding:9px}.artifact-list-heading{min-width:105px;display:flex;flex-direction:column;align-items:flex-start;justify-content:center;padding:0 8px 0 0;border:0;border-right:1px solid #eceee7}.artifact-choice{width:auto;min-width:150px;margin:0}.artifact-compare{align-items:flex-start;flex-wrap:wrap}.artifact-compare label{flex:1;flex-direction:column;align-items:stretch}.artifact-compare select{width:100%}}@media(max-width:520px){.artifacts-page{padding-top:30px}.artifacts-heading{align-items:flex-start}.artifacts-heading h1{font-size:22px}.artifact-create{padding:15px}.artifact-create-form{grid-template-columns:1fr}.artifact-field-wide{grid-column:auto}.artifact-form-footer,.artifact-editor-actions{align-items:flex-start;flex-direction:column}.artifact-editor{padding:15px}.artifact-editor-label textarea{min-height:120px}}
.artifact-source-links{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}.artifact-source-links a{padding:5px 7px;border:1px solid #e4e9e1;border-radius:3px;color:#607b65;background:#fafbf7;font-size:8px}.artifact-source-links a:hover{border-color:#bfd0bd;background:#f4f7f0}
</style>
