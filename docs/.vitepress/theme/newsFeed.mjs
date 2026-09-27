export const newsCategories = [
  { id: 'mathematics', label: 'Mathematics' },
  { id: 'models', label: 'Models' },
  { id: 'research', label: 'Research' },
]
export function safeNewsUrl(value) {
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null }
  catch { return null }
}
export function parseNewsFeed(value) {
  if (!value || value.schema_version !== 1 || !Array.isArray(value.items)) throw new Error('The news feed could not be read.')
  const categories = new Set(newsCategories.map(item => item.id))
  const seen = new Set()
  const items = value.items.filter(item => {
    if (!item || typeof item.id !== 'string' || !item.id || seen.has(item.id) || typeof item.title !== 'string' || !item.title.trim() || typeof item.summary !== 'string' || !safeNewsUrl(item.url) || !categories.has(item.category) || typeof item.published_at !== 'string' || !Number.isFinite(Date.parse(item.published_at))) return false
    seen.add(item.id)
    return true
  }).map(item => ({ ...item, url: safeNewsUrl(item.url), related_links: (Array.isArray(item.related_links) ? item.related_links : []).filter(link => link && typeof link.label === 'string' && link.label.trim() && safeNewsUrl(link.url)).map(link => ({ label: link.label, url: safeNewsUrl(link.url) })) }))
  if (value.items.length && !items.length) throw new Error('The news feed could not be read.')
  items.sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at) || String(a.id).localeCompare(String(b.id)))
  const sources = (Array.isArray(value.sources) ? value.sources : []).filter(source => source && typeof source.name === 'string' && source.name.trim() && safeNewsUrl(source.url)).map(source => ({ ...source, url: safeNewsUrl(source.url) }))
  return { ...value, items, sources, source_errors: Array.isArray(value.source_errors) ? value.source_errors : [] }
}
export function newsFreshness(feed, now = Date.now()) {
  const interval = Math.max(5, Math.min(1440, Number(feed?.refresh_interval_minutes) || 30))
  const success = feed?.last_successful_refresh_at
  const lastSuccess = Date.parse(success || '')
  return { interval, stale: !Number.isFinite(lastSuccess) || now - lastSuccess > Math.max(90, interval * 2) * 60_000, success: Number.isFinite(lastSuccess) ? success : null }
}
export function filterNews(items, category = 'all') {
  return category === 'all' ? items : items.filter(item => item.category === category)
}
