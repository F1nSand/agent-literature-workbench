import type { Locator } from '../types/workspace'
export interface ReaderSection { number: number; label: string; locator: Locator; text: string }
export interface Annotation { id: string; locator: Locator; quote: string; comment: string; color: string; created_at: string }
export interface ReaderManifest {
  document_id: string; version_id: string; title: string; file_format: string; page_count: number
  location_kind: 'page' | 'epub' | 'text'; file_url: string; render_url: string
  sections: ReaderSection[]; reading_state: { locator: Locator } | null; annotations: Annotation[]
}
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === 'string' ? body.detail : '暂时无法保存，请重试。') }
  return response.status === 204 ? undefined as T : response.json()
}
export function getReader(id: string, version?: string) { return request<ReaderManifest>(`/api/v1/documents/${id}/reader${version ? `?version_id=${version}` : ''}`) }
export function savePosition(id: string, version: string, locator: Locator) {
  return request(`/api/v1/documents/${id}/reading-state`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version_id: version, locator }) })
}
export function addAnnotation(id: string, version: string, value: Omit<Annotation, 'id' | 'created_at'>) {
  return request<Annotation>(`/api/v1/documents/${id}/annotations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version_id: version, ...value }) })
}
export function removeAnnotation(id: string, annotation: string) { return request(`/api/v1/documents/${id}/annotations/${annotation}`, { method: 'DELETE' }) }
export function updateMetadata<T>(id: string, fields: Record<string, unknown>) {
  return request<T>(`/api/v1/documents/${id}/metadata`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fields) })
}
