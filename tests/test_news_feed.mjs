import test from 'node:test'
import assert from 'node:assert/strict'
import { filterNews, newsFreshness, parseNewsFeed, safeNewsUrl } from '../docs/.vitepress/theme/newsFeed.mjs'

const item = (id, published_at, category = 'research') => ({ id, title: `Source headline ${id}`, summary: 'A source-attributed summary.', url: `https://example.org/news/${id}`, source: 'example', category, published_at, status: 'Published' })
const snapshot = items => ({ schema_version: 1, refresh_interval_minutes: 30, items, sources: [{ id: 'example', name: 'Example source', url: 'https://example.org' }] })

test('news is chronological, deduplicated, and filtered by the requested category', () => {
  const oldest = item('oldest', '2026-09-12T00:00:00Z', 'mathematics')
  const newest = item('newest', '2026-09-16T12:00:00Z', 'models')
  const feed = parseNewsFeed(snapshot([oldest, newest, { ...newest }]))
  assert.deepEqual(feed.items.map(value => value.id), ['newest', 'oldest'])
  assert.deepEqual(filterNews(feed.items, 'mathematics').map(value => value.id), ['oldest'])
  assert.equal(filterNews(feed.items), feed.items)
  assert.equal(filterNews(feed.items, 'research').length, 0)
})

test('source URLs permit HTTPS and reject active schemes and embedded credentials', () => {
  assert.equal(safeNewsUrl('https://example.org/news?item=1#claim'), 'https://example.org/news?item=1#claim')
  for (const value of ['http://example.org', 'javascript:alert(1)', '//example.org', 'https://user:secret@example.org', '/relative', null]) assert.equal(safeNewsUrl(value), null)
})

test('malformed stories are excluded without discarding other source stories', () => {
  const valid = item('valid', '2026-09-16T00:00:00Z')
  const feed = parseNewsFeed({ ...snapshot([valid, { ...valid, id: 'unsafe', url: 'javascript:alert(1)' }, { ...valid, id: 'undated', published_at: '' }, { ...valid, id: 'untitled', title: {} }, { ...valid, id: 'unknown', category: 'rumour' }]), sources: [null, { name: 'Unsafe', url: 'https://user:secret@example.org' }, { id: 'example', name: 'Example', url: 'https://example.org/' }] })
  assert.deepEqual(feed.items.map(value => value.id), ['valid'])
  assert.deepEqual(feed.sources.map(value => value.id), ['example'])
})

test('unsupported or entirely malformed snapshots fail, while a valid empty feed is allowed', () => {
  for (const value of [null, {}, { schema_version: 2, items: [] }, snapshot([{}])]) assert.throws(() => parseNewsFeed(value), /could not be read/)
  assert.deepEqual(parseNewsFeed(snapshot([])).items, [])
})

test('related source statements are preserved with the same HTTPS URL checks', () => {
  const story = { ...item('claim', '2026-09-16T00:00:00Z'), related_links: [{ label: 'Institution response', url: 'https://example.org/response' }, { label: 'Unsafe', url: 'https://user:secret@example.org' }, null] }
  assert.deepEqual(parseNewsFeed(snapshot([story])).items[0].related_links, [{ label: 'Institution response', url: 'https://example.org/response' }])
})

test('a dated source headline remains available when its feed provides no summary', () => {
  const story = { ...item('headline-only', '2026-09-16T00:00:00Z'), summary: '' }
  const parsed = parseNewsFeed(snapshot([story]))
  assert.equal(parsed.items[0].title, story.title)
  assert.equal(parsed.items[0].summary, '')
})

test('freshness follows successful checks, not repeated failed refresh attempts', () => {
  const now = Date.parse('2026-09-16T12:00:00Z')
  const feed = { last_checked_at: '2026-09-16T11:59:00Z', last_successful_refresh_at: '2026-09-16T09:00:00Z', refresh_interval_minutes: 30, source_errors: [{ source_id: 'example', error: 'Source unavailable' }] }
  assert.equal(newsFreshness(feed, now).stale, true)
  assert.equal(newsFreshness({ ...feed, last_successful_refresh_at: '2026-09-16T11:30:00Z' }, now).stale, false)
  assert.equal(newsFreshness({ last_checked_at: feed.last_checked_at, source_errors: [] }, now).stale, true)
  assert.equal(newsFreshness(null, now).stale, true)
})

test('freshness respects slower source schedules and normalizes invalid intervals', () => {
  const now = Date.parse('2026-09-16T12:00:00Z')
  const feed = { last_successful_refresh_at: '2026-09-16T09:30:00Z', refresh_interval_minutes: 120 }
  assert.equal(newsFreshness(feed, now).stale, false)
  assert.equal(newsFreshness({ ...feed, refresh_interval_minutes: 'bad' }, now).interval, 30)
  assert.equal(newsFreshness({ ...feed, refresh_interval_minutes: -20 }, now).interval, 5)
})
